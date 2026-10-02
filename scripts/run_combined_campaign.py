"""Run a cohort against the combined (multi-bug) panel.

Same grading path as the single-bug panel -- the only difference is that a task now carries
several defects and a verified ``bug_id -> test_node`` map, so a model that finds two of
three earns partial credit and a model that finds none earns zero. That is what the
per-bug scoring was always meant to measure and what a single-mutant file could never
exercise.

Isolation is unchanged and still enforced: the agent's workspace is built with
``drop_tests=True``, so it contains no test file at all.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bugbench.attribution import score_combined                # noqa: E402
from bugbench.combine import apply_all                          # noqa: E402
from bugbench.mutants import generate                           # noqa: E402
from bugbench.panel import freeze, select                       # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_agent_campaign import AGENT_PROMPT, agent_context     # noqa: E402
from bugbench.sandbox import build_sandbox, grader_env         # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
COMBINED = REPO_ROOT / "data" / "combined_tasks.json"
ROOTS = {
    ".opencode-telegram-bot": Path.home() / ".opencode-telegram-bot",
    ".fix-backend": Path.home() / ".fix-backend",
    "copilot-model-audit": Path.home() / "copilot-model-audit",
}
WORK = Path.home() / ".cache" / "bugbench-combined"


def source_with_bugs(repo: Path, module: str, bug_ids: list[str]) -> tuple[str, dict]:
    pristine = (repo / module).read_text()
    mm = {m.bug_id: m for m in generate(pristine, module, limit=150).mutants}
    return apply_all(pristine, [mm[b] for b in bug_ids if b in mm]), mm


def build_focus_lines(repo: Path, module: str, bug_ids: list[str]) -> dict[int, str]:
    """Per-bug one-line hints.

    Deliberately *not* given to the model in aggregate form. A combined task is harder
    precisely because the model must find every defect itself, so the prompt says only which
    region the file has problems in, never how many defects there are or where each is.
    """
    pristine = (repo / module).read_text()
    mm = {m.bug_id: m for m in generate(pristine, module, limit=150).mutants}
    return {mm[b].line: mm[b].operator for b in bug_ids if b in mm}


def run_agent(agy_bin: str, model: str, task: dict, repo: Path, buggy: str,
              workdir: Path, timeout: int) -> dict:
    workdir = Path(workdir)
    shutil.rmtree(workdir, ignore_errors=True)
    code = build_sandbox(repo, workdir / "code", drop_tests=True)
    (code / task["module"]).write_text(buggy)

    lines = sorted(b["line"] for b in task["bugs"])
    region = f"{min(lines)}-{max(lines)}" if len(lines) > 1 else str(lines[0])
    prompt = AGENT_PROMPT.format(
        module=task["module"],
        line=region,
        context=("The file has one or more defects in the region above. Find and fix every "
                 "one of them. A partial fix will be scored per defect, so fix as many as "
                 "you can find.\n\nRelevant region:\n"
                 + agent_context(buggy, min(lines))),
    )
    env = grader_env({"PATH": str(Path.home() / ".local/bin") + ":" +
                      __import__("os").environ.get("PATH", "")})
    cmd = [agy_bin, "--print", "--sandbox", "--dangerously-skip-permissions",
           "--add-dir", str(code), "--model", model,
           "--print-timeout", f"{timeout}s", "-p", prompt]
    start = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=code, capture_output=True, text=True,
                              timeout=timeout + 60, env=env)
        reply, rc = (proc.stdout or "")[-3000:], proc.returncode
    except subprocess.TimeoutExpired:
        reply, rc = "", -1
    after = (code / task["module"]).read_text(encoding="utf-8", errors="replace")
    return {"reply": reply, "rc": rc, "latency_s": time.monotonic() - start,
            "changed": after != buggy, "source_after": after,
            "smuggled": [p.name for p in code.rglob("test_*.py")]}


import subprocess  # noqa: E402  (used inside run_agent)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agy", default=str(Path.home() / ".local/bin/agy"))
    ap.add_argument("--model", required=True)
    ap.add_argument("--combined", default=str(COMBINED))
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--min-bugs", type=int, default=2)
    args = ap.parse_args()

    tasks = [t for t in json.loads(Path(args.combined).read_text())
             if t["bugs_total"] >= args.min_bugs]
    tasks.sort(key=lambda t: (-t["bugs_total"], t["task_id"]))
    tasks = tasks[: args.limit]
    if not tasks:
        print("no combined tasks", file=sys.stderr)
        return 1

    WORK.mkdir(parents=True, exist_ok=True)
    rows, per_bug_rows = [], []
    print(f"combined panel: {len(tasks)} files, "
          f"{sum(t['bugs_total'] for t in tasks)} scored bugs -> {args.model}", flush=True)

    for i, task in enumerate(tasks, 1):
        repo = ROOTS[task["codebase"]]
        bug_ids = [b["bug_id"] for b in task["bugs"]]
        buggy, _mm = source_with_bugs(repo, task["module"], bug_ids)
        work = WORK / task["task_id"][-24:]

        res = run_agent(args.agy, args.model, task, repo, buggy, work, args.timeout)
        graded = score_combined(repo, task, res["source_after"], work / "graded")
        outcome = "policy_violation" if res["smuggled"] else (
            "scored" if res["changed"] else "declined_work")

        rows.append({
            "model": f"agy/{args.model}", "task_id": task["task_id"],
            "outcome": outcome, "ok": res["changed"],
            "latency_s": round(res["latency_s"], 2),
            "bugs_total": graded["bugs_total"], "bugs_fixed": graded["bugs_fixed"],
            "per_bug_score": graded["per_bug_score"], "file_clear": graded["file_clear"],
            "codebase": task["codebase"], "language": "python",
            "difficulty": task["difficulty"],
            "error": f"rc={res['rc']}" if res["rc"] != 0 else "",
            "smuggled_tests": res["smuggled"],
        })
        for b in graded["bugs"]:
            per_bug_rows.append({"model": f"agy/{args.model}", "task_id": task["task_id"],
                                 **b})
        print(f"  [{i}/{len(tasks)}] {graded['bugs_fixed']}/{graded['bugs_total']} "
              f"score={graded['per_bug_score']:.2f} clear={str(graded['file_clear']):5s} "
              f"{task['difficulty']:8s} {res['latency_s']:5.0f}s", flush=True)
        shutil.rmtree(work, ignore_errors=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(r) for r in rows))
    bugout = out.with_suffix(".bugs.jsonl")
    bugout.write_text("\n".join(json.dumps(r) for r in per_bug_rows))

    tot = sum(r["bugs_total"] for r in rows)
    fix = sum(r["bugs_fixed"] for r in rows)
    print(f"\nagy/{args.model}: {fix}/{tot} bugs, "
          f"{sum(1 for r in rows if r['file_clear'])}/{len(rows)} files cleared -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
