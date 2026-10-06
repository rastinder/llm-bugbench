#!/usr/bin/env python3
"""Bug Identification & Localization Benchmark Track.

Evaluates an LLM's ability to:
1. Detect and pinpoint the exact defective function, method, or class within a source file.
2. Formulate the precise root cause before code repair.

Modes:
- blind: Evaluates if model can find the defect purely from reading the source code without hints.
- guided: Evaluates if model can localize the defect given the user-reported symptom / failure report.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TASKS_PATH = REPO_ROOT / "data" / "host_tasks.json"
SOURCES_PATH = REPO_ROOT / "data" / "host_tasks_sources.json"
RESULTS_DIR = REPO_ROOT / "data" / "results"

BLIND_PROMPT = """You are an expert static analyzer and software security auditor.

Analyze the following source code for a subtle, real-world defect or bug:

```python
{source}
```

Task:
1. Identify if there is a defect or behavioral bug in this code.
2. State the exact name of the buggy function, method, or class.
3. Detail the precise root cause.

Output your diagnosis strictly as valid JSON with keys:
{{
  "has_bug": true,
  "buggy_symbol": "<exact name of the buggy function/method/class>",
  "root_cause": "<detailed explanation of the bug and how it manifests>"
}}
"""

GUIDED_PROMPT = """You are an expert software engineer resolving a defect.

A regression test has reported the following defect in `{module}`:
"-- {description} --"

Source code of `{module}`:
```python
{source}
```

Task:
1. Localize which function, method, or class contains this defect.
2. Explain how the defect manifests in the code.

Output your diagnosis strictly as valid JSON with keys:
{{
  "has_bug": true,
  "buggy_symbol": "<exact name of the buggy function/method/class>",
  "root_cause": "<explanation of why this symbol causes the described defect>"
}}
"""


def extract_ground_truth(source_before: str, source_after: str) -> list[str]:
    b_lines = source_before.splitlines()
    a_lines = source_after.splitlines()
    matcher = difflib.SequenceMatcher(None, b_lines, a_lines)
    changed_lines = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete", "insert"):
            changed_lines.append((i1 + 1, max(i1 + 1, i2)))

    symbols = []
    try:
        tree = ast.parse(source_before)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                st = getattr(node, "lineno", 0)
                en = getattr(node, "end_lineno", 0)
                for c1, c2 in changed_lines:
                    if (st <= c1 <= en) or (st <= c2 <= en) or (c1 <= st and en <= c2):
                        symbols.append(node.name)
    except Exception:
        pass
    return sorted(set(symbols))


def query_llm(model: str, prompt: str, gateway: str = "http://127.0.0.1:8000/v1") -> dict:
    url = f"{gateway.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a precise code analysis engine. Respond only in valid JSON."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.0,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            raw = data["choices"][0]["message"]["content"]
            dt = time.monotonic() - t0
            return {"raw": raw, "latency": dt, "error": None}
    except Exception as e:
        return {"raw": "", "latency": time.monotonic() - t0, "error": str(e)}


def parse_response(raw: str) -> dict:
    cleaned = raw.strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(1)
    else:
        match_obj = re.search(r"(\{.*\})", cleaned, re.DOTALL)
        if match_obj:
            cleaned = match_obj.group(1)
    try:
        return json.loads(cleaned)
    except Exception:
        return {"buggy_symbol": "", "root_cause": raw[:300]}


def is_symbol_match(predicted: str, ground_truth: list[str]) -> bool:
    pred = predicted.strip().lower()
    for gt in ground_truth:
        gt_lower = gt.lower()
        if pred == gt_lower:
            return True
        if pred in gt_lower or gt_lower in pred:
            return True
    return False


def main():
    parser = argparse.ArgumentParser(description="Run Bug Identification Benchmark")
    parser.add_argument("--model", default="qwen-3.8", help="Model identifier on gateway")
    parser.add_argument("--mode", choices=["blind", "guided"], default="guided", help="Benchmark mode")
    parser.add_argument("--limit", type=int, default=0, help="Max tasks to run (0 for all)")
    parser.add_argument("--gateway", default="http://127.0.0.1:8000/v1", help="Gateway URL")
    args = parser.parse_args()

    with open(TASKS_PATH) as f:
        tasks = {t["task_id"]: t for t in json.load(f)}
    with open(SOURCES_PATH) as f:
        sources = json.load(f)

    task_list = sources if not args.limit else sources[:args.limit]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_file = RESULTS_DIR / f"identification-{args.mode}-{args.model.replace('/', '_')}.jsonl"

    print(f"=== Running Bug Identification Track ({args.mode.upper()}) ===")
    print(f"Model: {args.model}")
    print(f"Tasks: {len(task_list)}")
    print(f"Output: {out_file}\n")

    correct = 0
    total = len(task_list)

    with open(out_file, "w") as out_fp:
        for i, item in enumerate(task_list, 1):
            tid = item["task_id"]
            meta = tasks.get(tid, {})
            gt_symbols = extract_ground_truth(item["before_source"], item["after_source"])

            desc = meta.get("description", "")
            if args.mode == "guided" and desc:
                prompt = GUIDED_PROMPT.format(
                    module=meta.get("module", "module.py"),
                    description=desc,
                    source=item["before_source"]
                )
            else:
                prompt = BLIND_PROMPT.format(source=item["before_source"])

            res = query_llm(args.model, prompt, gateway=args.gateway)
            parsed = parse_response(res["raw"])
            pred_symbol = parsed.get("buggy_symbol", "")
            matched = is_symbol_match(pred_symbol, gt_symbols)

            if matched:
                correct += 1

            record = {
                "task_id": tid,
                "module": meta.get("module"),
                "mode": args.mode,
                "model": args.model,
                "identified": matched,
                "predicted_symbol": pred_symbol,
                "ground_truth_symbols": gt_symbols,
                "root_cause": parsed.get("root_cause", "")[:300],
                "latency": round(res["latency"], 2),
                "error": res["error"],
            }
            out_fp.write(json.dumps(record) + chr(10))
            out_fp.flush()

            status = "MATCH" if matched else "MISS"
            print(f"[{i:2d}/{total:2d}] {tid[-32:]} -> {status} (pred: {pred_symbol!r}, gt: {gt_symbols}) [{res['latency']:.1f}s]")

    pct = (correct / total) * 100 if total else 0
    print(f"{chr(10)}Identification Complete: {correct}/{total} correctly localized ({pct:.1f}%)")


if __name__ == "__main__":
    main()
