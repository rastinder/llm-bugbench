"""Run a cohort against the host panel: real files, genuine historical defects.

Same guarantees as the other runners -- the agent's workspace is built with
`drop_tests=True` so there is no test file on disk to read, feedback is node-ID only, and
the same grading path scores every model.

The host panel contains real historical bugs mined from real debugging sessions in
opencode.db, paired with temporal test oracles that fail on the buggy state and pass on
the fix.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bugbench.feedback import FeedbackChannel                  # noqa: E402
from bugbench.history import ROOTS_BY_PREFIX                   # noqa: E402
from bugbench.sandbox import build_sandbox, grader_env         # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
HOSTS = REPO_ROOT / "data" / "host_tasks.json"
WORK = Path.home() / ".cache" / "bugbench-host-campaign"

PROMPT = """You are fixing a real bug in a real codebase.

The file `{module}` has a defect. Find and fix it.

Rules:
- Work strictly inside the directory you have been given. Do NOT inspect, list, or search parent directories or other paths outside this directory.
- Make the minimal change that fixes the defect. Do not refactor or reformat.
- Do not create test files, and do not try to find or read any test suite.
- When done, reply with a brief summary of what defect you fixed.

Your edit is graded by whether the relevant test suite passes.

Start by reading `{module}`."""


def grade_task(task: dict, repo: Path, source: str, work: Path) -> dict[str, bool]:
    """Grade whether the defect was fixed using the genuine recorded test oracle."""
    import urllib.request
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:5000/grade",
            data=json.dumps({"task_id": task["task_id"], "source": source}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            res = json.loads(resp.read().decode())
            return {task["task_id"]: res.get("fixed", False)}
    except Exception:
        pass
    shutil.rmtree(work, ignore_errors=True)
    d = build_sandbox(repo, work)
    (d / task["module"]).parent.mkdir(parents=True, exist_ok=True)
    (d / task["module"]).write_text(source)
    test_rel = task.get("test_rel", "test_recorded.py")
    (d / test_rel).parent.mkdir(parents=True, exist_ok=True)
    (d / test_rel).write_text(task["test_source"])
    for pycache in d.rglob("__pycache__"):
        shutil.rmtree(pycache, ignore_errors=True)

    if task.get("focus_ids"):
        node_ids = [f"{test_rel}::{fid}" for fid in task["focus_ids"]]
    else:
        node_ids = [test_rel]

    r = subprocess.run([sys.executable, "-m", "pytest", *node_ids, "-q",
                        "--no-header", "-p", "no:cacheprovider"],
                       cwd=d, capture_output=True, text=True, timeout=120, env=grader_env())
    shutil.rmtree(work, ignore_errors=True)
    fixed = (r.returncode == 0)
    return {task["task_id"]: fixed}


def run_agent(agy: str, model: str, task: dict, repo: Path, workdir: Path,
              timeout: int) -> dict:
    shutil.rmtree(workdir, ignore_errors=True)
    code = build_sandbox(repo, workdir / "code", drop_tests=True)
    (code / task["module"]).parent.mkdir(parents=True, exist_ok=True)
    (code / task["module"]).write_text(task["before_source"])
    desc = task.get("description", "")
    if desc:
        prompt = (
            f"You are fixing a real bug in a real codebase.\n\n"
            f"The file `{task['module']}` has a defect:\n{desc}\n\n"
            f"Rules:\n"
            f"- Work strictly inside the directory you have been given. Do NOT inspect, list, or search parent directories or other paths outside this directory.\n"
            f"- Make the minimal change that fixes the defect. Do not refactor or reformat.\n"
            f"- Do not create test files, and do not try to find or read any test suite.\n"
            f"- When done, reply with a brief summary of what defect you fixed.\n\n"
            f"Start by reading `{task['module']}`."
        )
    else:
        prompt = PROMPT.format(module=task["module"])
    env = grader_env({"PATH": str(Path.home() / ".local/bin") + ":" +
                      __import__("os").environ.get("PATH", "")})
    cmd = [agy, "--sandbox", "--dangerously-skip-permissions",
           "--add-dir", str(code), "--model", model,
           "--print-timeout", f"{timeout}s", "--print", prompt]
    start = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=code, capture_output=True, text=True,
                              timeout=timeout + 90, env=env)
        reply, rc = (proc.stdout or "")[-3000:], proc.returncode
    except subprocess.TimeoutExpired:
        reply, rc = "", -1
    after = (code / task["module"]).read_text(encoding="utf-8", errors="replace")
    return {"reply": reply, "rc": rc, "latency_s": time.monotonic() - start,
            "changed": after != task["before_source"], "source_after": after,
            "smuggled": [p.name for p in code.rglob("test_*.py")]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agy", default=str(Path.home() / ".local/bin/agy"))
    ap.add_argument("--model", required=True)
    ap.add_argument("--hosts", default=str(HOSTS))
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--task-id", default="")
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    tasks = json.loads(Path(args.hosts).read_text())
    if args.task_id:
        tasks = [t for t in tasks if t["task_id"] == args.task_id]
    elif args.limit > 0:
        tasks = tasks[:args.limit]
    side = Path(args.hosts).with_name("host_tasks_sources.json")
    sources = {t["task_id"]: t for t in json.loads(side.read_text())} if side.exists() else {}
    if not tasks:
        print("no host tasks", file=sys.stderr)
        return 1
    WORK.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    completed_ids = set()
    if args.resume and out_path.exists():
        for line in out_path.read_text().splitlines():
            if line.strip():
                try:
                    r = json.loads(line)
                    rows.append(r)
                    completed_ids.add(r["task_id"])
                except Exception:
                    pass
        print(f"Resuming: found {len(completed_ids)} completed tasks in {out_path}", flush=True)

    total_bugs = sum(t["bugs_total"] for t in tasks)
    print(f"host panel: {len(tasks)} files, {total_bugs} defects -> {args.model}", flush=True)

    for i, task in enumerate(tasks, 1):
        if task["task_id"] in completed_ids:
            print(f"  [{i}/{len(tasks)}] skip {task['task_id']}: already completed", flush=True)
            continue
        repo = None
        if task.get("repo") and Path(task["repo"]).is_dir() and (Path(task["repo"]) / task["module"]).exists():
            repo = Path(task["repo"])
        else:
            roots = sorted((Path(r) for r in ROOTS_BY_PREFIX.values()),
                           key=lambda p: len(str(p)), reverse=True)
            repo = next((r for r in roots if (r / task["module"]).exists()), None)
        if repo is None:
            print(f"  skip {task['task_id']}: host repo/module not resolvable", flush=True)
            continue
        work = WORK / task["task_id"][-24:]
        full = {**task, **sources.get(task["task_id"], {})}
        res = run_agent(args.agy, args.model, full, repo, work, args.timeout)
        fixed = grade_task(full, repo, res["source_after"], work / "graded")
        n_fixed = sum(1 for v in fixed.values() if v)
        outcome = "policy_violation" if res["smuggled"] else (
            "scored" if res["changed"] else "declined_work")
        row = {
            "model": f"agy/{args.model}", "task_id": task["task_id"], "outcome": outcome,
            "ok": res["changed"], "latency_s": round(res["latency_s"], 1),
            "bugs_total": task["bugs_total"], "bugs_fixed": n_fixed,
            "per_bug_score": round(n_fixed / task["bugs_total"], 4),
            "file_clear": n_fixed == task["bugs_total"],
            "codebase": task["codebase"], "difficulty": task["difficulty"],
            "error": f"rc={res['rc']}" if res["rc"] != 0 else "",
            "per_bug": fixed,
        }
        rows.append(row)
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        print(f"  [{i}/{len(tasks)}] {n_fixed}/{task['bugs_total']} defects  "
              f"score={n_fixed / task['bugs_total']:.2f}  clear={n_fixed == task['bugs_total']}  "
              f"{res['latency_s']:.0f}s", flush=True)
        shutil.rmtree(work, ignore_errors=True)

    out = out_path
    fixed = sum(r["bugs_fixed"] for r in rows)
    bugs = sum(r["bugs_total"] for r in rows)
    print(f"\nagy/{args.model}: {fixed}/{bugs} = {fixed / max(bugs,1):.0%}  -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
