"""Run a cohort against the host panel: real files, several injected defects each.

Same guarantees as the other runners -- the agent's workspace is built with
``drop_tests=True`` so there is no test file on disk to read, feedback is node-ID only, and
the same grading path scores every model.

What differs is what is being measured. The host panel is not synthetic snippets: each task
is a real file from a real debugging session, carrying several semantically-motivated
defects (an inverted guard, a boundary moved by one, a dropped condition) rather than
single-token constant flips. That is the change that broke the ceiling, because a model must
now reason about control flow instead of pattern-matching a literal.

Per-bug credit is what makes stacking work: a model that finds two of six defects scores
about a third, which is the intended signal rather than a failure of the model.
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

The file `{module}` has one or more defects. Find and fix every one of them.

Rules:
- Work only inside the directory you have been given.
- Make the minimal change that fixes each defect. Do not refactor or reformat.
- Do not create test files, and do not try to find or read any test suite.
- When done, reply with one line per defect you fixed.

Your edits are graded per defect, so fixing some is worth far more than fixing none.
Partial credit exists; a wrong guess costs nothing.

Start by reading `{module}`."""


def grade_task(task: dict, repo: Path, source: str, work: Path) -> dict[str, bool]:
    """Did each injected defect end up fixed? Compare probe values, not pass/fail.

    The injected bug's recorded probe value is the *after* behaviour; the buggy source
    produces something different for exactly those probes. Comparing values avoids the trap
    where a model deletes the function and the probe simply errors.
    """
    d = build_sandbox(repo, work)
    (d / task["module"]).write_text(source)
    (d / "test_probe.py").write_text(task["test_source"])
    r = subprocess.run([sys.executable, "-m", "pytest", "test_probe.py", "-q",
                        "--tb=no", "-s"],
                       cwd=d, capture_output=True, text=True, timeout=300, env=grader_env())
    got: dict[str, str] = {}
    for line in r.stdout.splitlines():
        if line.startswith("PROBE"):
            k, _, v = line[5:].partition("=")
            got[k.strip()] = v.strip()
    shutil.rmtree(work, ignore_errors=True)

    fixed: dict[str, bool] = {}
    for bug in task["bugs"]:
        expected = bug.get("probe_value_after", "")
        fixed[bug["bug_id"]] = bool(expected) and got.get(bug["probe"]) == expected
    return fixed


def run_agent(agy: str, model: str, task: dict, repo: Path, workdir: Path,
              timeout: int) -> dict:
    shutil.rmtree(workdir, ignore_errors=True)
    code = build_sandbox(repo, workdir / "code", drop_tests=True)
    (code / task["module"]).write_text(task["before_source"])
    prompt = PROMPT.format(module=task["module"])
    env = grader_env({"PATH": str(Path.home() / ".local/bin") + ":" +
                      __import__("os").environ.get("PATH", "")})
    cmd = [agy, "--print", "--sandbox", "--dangerously-skip-permissions",
           "--add-dir", str(code), "--model", model,
           "--print-timeout", f"{timeout}s", "-p", prompt]
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
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    tasks = json.loads(Path(args.hosts).read_text())
    # The manifest carries no source; the answers live in a gitignored sidecar.
    side = Path(args.hosts).with_name("host_tasks_sources.json")
    sources = {t["task_id"]: t for t in json.loads(side.read_text())} if side.exists() else {}
    if not tasks:
        print("no host tasks", file=sys.stderr)
        return 1
    WORK.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    total_bugs = sum(t["bugs_total"] for t in tasks)
    print(f"host panel: {len(tasks)} files, {total_bugs} defects -> {args.model}", flush=True)

    for i, task in enumerate(tasks, 1):
        # Resolve by longest root path that actually exists; matching on the codebase
        # name alone is ambiguous because several roots are dot-directories.
        # Resolve by asking each root whether it actually holds the host module. Matching
        # on the codebase name alone is ambiguous because several roots are dot-directories.
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
        rows.append({
            "model": f"agy/{args.model}", "task_id": task["task_id"], "outcome": outcome,
            "ok": res["changed"], "latency_s": round(res["latency_s"], 1),
            "bugs_total": task["bugs_total"], "bugs_fixed": n_fixed,
            "per_bug_score": round(n_fixed / task["bugs_total"], 4),
            "file_clear": n_fixed == task["bugs_total"],
            "codebase": task["codebase"], "difficulty": task["difficulty"],
            "error": f"rc={res['rc']}" if res["rc"] != 0 else "",
            "per_bug": fixed,
        })
        print(f"  [{i}/{len(tasks)}] {n_fixed}/{task['bugs_total']} defects  "
              f"score={n_fixed / task['bugs_total']:.2f}  clear={n_fixed == task['bugs_total']}  "
              f"{res['latency_s']:.0f}s", flush=True)
        shutil.rmtree(work, ignore_errors=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(r) for r in rows))
    fixed = sum(r["bugs_fixed"] for r in rows)
    bugs = sum(r["bugs_total"] for r in rows)
    print(f"\nagy/{args.model}: {fixed}/{bugs} = {fixed / max(bugs,1):.0%}  -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())