"""Run the local deepseek-v4.1-flash model (served via llama-server on port 8083)

Evaluates the model across the 13 genuine historical bug tasks in data/host_tasks.json.
Scores whether the model finds and repairs each real defect using the recorded test oracle.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bugbench.attempt import extract_patch, apply_patch_to_tree
from scripts.run_host_campaign import grade_task, WORK

REPO_ROOT = Path(__file__).resolve().parents[1]
HOSTS = REPO_ROOT / "data" / "host_tasks.json"
SOURCES = REPO_ROOT / "data" / "host_tasks_sources.json"


def apply_search_replace(original: str, reply: str) -> str:
    pattern = re.compile(r'<{5,}\s*SEARCH\s*\n(.*?)\n={5,}\s*\n(.*?)\n>{5,}\s*REPLACE', re.S)
    matches = pattern.findall(reply)
    if matches:
        curr = original
        for s, r in matches:
            if s in curr:
                curr = curr.replace(s, r, 1)
            elif s.strip() in curr:
                curr = curr.replace(s.strip(), r.strip(), 1)
        if curr != original:
            return curr
    return original


def apply_model_fix(module: str, original: str, reply: str) -> str:
    # 1. Search/Replace
    sr_res = apply_search_replace(original, reply)
    if sr_res != original:
        return sr_res

    # 2. Unified diff
    patch = extract_patch(reply)
    if patch:
        tmp_box = Path("/tmp/local_patch_box")
        shutil.rmtree(tmp_box, ignore_errors=True)
        tmp_box.mkdir(parents=True, exist_ok=True)
        (tmp_box / module).parent.mkdir(parents=True, exist_ok=True)
        (tmp_box / module).write_text(original)
        if apply_patch_to_tree(tmp_box, patch):
            mod = (tmp_box / module).read_text()
            shutil.rmtree(tmp_box, ignore_errors=True)
            return mod
        shutil.rmtree(tmp_box, ignore_errors=True)

    # 3. Code fence full replacement
    fences = re.findall(r'```(?:python|py)?\s*\n(.*?)```', reply, re.S)
    for code in fences:
        if len(code) >= len(original) * 0.7:
            try:
                import ast
                ast.parse(code)
                if code.strip() != original.strip():
                    return code
            except SyntaxError:
                pass

    return original


def query_deepseek(module: str, code: str, timeout: int = 300) -> tuple[str, float]:
    prompt = f"""You are fixing a real bug in a real codebase.

The file `{module}` has a defect. Find and fix it.

Here is `{module}`:
```python
{code}
```

Rules:
- Reply ONLY with the fix using a SEARCH/REPLACE block.
- Do not output commentary or explanations before or after.
- Keep the fix minimal.

Format:
<<<<<<< SEARCH
exact lines to replace
=======
replacement lines
>>>>>>> REPLACE
"""
    t0 = time.time()
    req = urllib.request.Request(
        "http://127.0.0.1:8083/v1/chat/completions",
        headers={"Content-Type": "application/json"},
        data=json.dumps({
            "model": "deepseek-v4.1-flash",
            "messages": [
                {"role": "system", "content": "You are an automated code repair tool. Reply ONLY with the SEARCH/REPLACE block."},
                {"role": "user", "content": prompt}
            ],
            "chat_template_kwargs": {"thinking": False},
            "max_tokens": 1200,
            "temperature": 0.0
        }).encode()
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        res = json.loads(resp.read())
        reply = res["choices"][0]["message"].get("content", "")
    except Exception as e:
        reply = f"ERROR: {e}"
    elapsed = time.time() - t0
    return reply, elapsed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hosts", default=str(HOSTS))
    ap.add_argument("--out", default=str(REPO_ROOT / "data" / "results" / "host-deepseek-4.1.jsonl"))
    ap.add_argument("--limit", type=int, default=13)
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()

    tasks = json.loads(Path(args.hosts).read_text())[:args.limit]
    sources = {t["task_id"]: t for t in json.loads(SOURCES.read_text())}

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    print(f"Running deepseek-v4.1-flash over {len(tasks)} tasks...")

    for i, task in enumerate(tasks, 1):
        tid = task["task_id"]
        full = {**task, **sources[tid]}
        repo = Path(task["repo"])
        module = task["module"]
        before = full["before_source"]

        print(f"[{i}/{len(tasks)}] {tid} ({module})...", end=" ", flush=True)
        reply, latency_s = query_deepseek(module, before, timeout=args.timeout)

        if reply.startswith("ERROR:"):
            outcome = "transport"
            fixed = False
            error = reply
        else:
            after = apply_model_fix(module, before, reply)
            if after != before:
                work = WORK / f"ds_{tid[-20:]}"
                grade_res = grade_task(full, repo, after, work)
                fixed = grade_res.get(tid, False)
                outcome = "scored"
                error = ""
            else:
                outcome = "invalid_response"
                fixed = False
                error = "no applicable patch or search/replace produced"

        row = {
            "model": "local/deepseek-v4.1-flash",
            "task_id": tid,
            "outcome": outcome,
            "ok": outcome == "scored",
            "latency_s": round(latency_s, 1),
            "bugs_total": 1,
            "bugs_fixed": 1 if fixed else 0,
            "per_bug_score": 1.0 if fixed else 0.0,
            "file_clear": fixed,
            "codebase": task["codebase"],
            "difficulty": task["difficulty"],
            "error": error,
            "reply_snippet": reply[:200]
        }
        rows.append(row)

        with open(out_path, "a") as f:
            f.write(json.dumps(row) + "\n")

        print(f"outcome={outcome} fixed={fixed} score={row['per_bug_score']} ({latency_s:.1f}s)", flush=True)

    total_fixed = sum(r["bugs_fixed"] for r in rows)
    total_bugs = sum(r["bugs_total"] for r in rows)
    print(f"\nFinal: local/deepseek-v4.1-flash: {total_fixed}/{total_bugs} = {total_fixed / max(total_bugs, 1):.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
