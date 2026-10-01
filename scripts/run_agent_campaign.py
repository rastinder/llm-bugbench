"""Run a tool-using agent cohort (Antigravity/agy) over the panel.

The diff-returning adapter in ``bugbench.attempt`` is the wrong interface for an agentic
model, and forcing it through one would understate it for a reason that has nothing to do
with debugging ability. Two agents produced different shapes on the first trial: one emitted
unified diffs, the other emitted ``<tool_call>`` XML asking for a shell. The latter can
already edit files directly, so asking it to hand back a diff is measuring format compliance
rather than repair.

So the agent runs in a sandbox directory, is given the file to fix, and is graded on the
files it actually leaves behind. The read-only rules are identical to the diff path:

  * the agent is pointed at ``code/`` only -- no test directory exists in its workspace,
    so there is nothing to read even if it searches;
  * it cannot reach the panel's hidden tests because they are not on its filesystem at all,
    rather than being on it and denied;
  * the same prompt content, the same module scoping and the same grading path are used, so
    the two adapter families are comparable.

The workspace is rebuilt from scratch per task. Reusing one would let residue from an
earlier task leak into a later score.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bugbench.feedback import FeedbackChannel     # noqa: E402
from bugbench.mutants import apply_mutation, generate   # noqa: E402
from bugbench.panel import verify_rows            # noqa: E402
from bugbench.sandbox import build_sandbox, grader_env    # noqa: E402
from bugbench.scoring import score_from_rows      # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
PANEL = REPO_ROOT / "tasks" / "panel25.json"
ROOTS = {
    ".opencode-telegram-bot": Path.home() / ".opencode-telegram-bot",
    ".fix-backend": Path.home() / ".fix-backend",
    "copilot-model-audit": Path.home() / "copilot-model-audit",
}
WORK = Path.home() / ".cache" / "bugbench-agent"

#: The task text handed to an agent. Derived only from the module and the mutated line --
#: never from a test -- so the agent has exactly the information the diff path gives.
AGENT_PROMPT = """You are fixing one real bug in a real codebase.

The file you need is `{module}`. It contains a defect near line {line}.

Rules:
- Work only inside the directory you have been given.
- Make the minimal change that fixes the defect. Do not refactor, reformat or rename.
- Do not create test files, and do not try to find or read any test suite.
- When you are done, reply with a one-line summary of what you changed.

{context}"""

_ELIDED = re.compile(r"^# \.\.\. \[.*\]$")


def agent_context(text: str, focus_line: int, budget: int = 20_000) -> str:
    """The same scoped region the diff path shows, so both adapters see equal context."""
    from bugbench.attempt import scope_source
    scoped, _ = scope_source(text, focus_line, budget)
    return scoped


def run_agent_task(agy_bin: str, model: str, task: dict, repo: Path,
                   buggy_source: str, workdir: Path, timeout: int = 600) -> dict:
    """Give an agent the file and grade whatever it leaves on disk."""
    workdir = Path(workdir)
    shutil.rmtree(workdir, ignore_errors=True)
    # drop_tests: the agent gets no test file at all, so there is nothing to read.
    code = build_sandbox(repo, workdir / "code", drop_tests=True)
    (code / task["module"]).write_text(buggy_source)

    prompt = AGENT_PROMPT.format(
        module=task["module"],
        line=task["line"],
        context="Relevant region:\n" + agent_context(buggy_source, task["line"]),
    )

    cmd = [agy_bin, "--print", "--sandbox", "--dangerously-skip-permissions",
           "--add-dir", str(code), "--model", model, "--print-timeout", f"{timeout}s",
           "-p", prompt]
    start = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=code, capture_output=True, text=True,
                              timeout=timeout + 60,
                              env=grader_env({"PATH": str(Path.home() / ".local/bin")
                                              + ":" + __import__("os").environ.get("PATH", "")}))
        reply = (proc.stdout or "")[-4000:]
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        reply, rc = "", -1
    latency = time.monotonic() - start

    after = (code / task["module"]).read_text(encoding="utf-8", errors="replace")
    changed = after != buggy_source
    # An agent that created test files, or reached outside its workspace, is not a
    # legitimate repair regardless of whether the tests now pass.
    smuggled = [p.name for p in (code).rglob("test_*.py")]

    return {"reply": reply, "rc": rc, "latency_s": latency, "changed": changed,
            "source_after": after, "smuggled_tests": smuggled,
            "workspace": str(code)}


def grade(task: dict, repo: Path, source: str, workdir: Path) -> list[dict]:
    d = build_sandbox(repo, workdir)
    (d / task["module"]).write_text(source)
    rows = [r for t in task.get("green_tests", []) for r in FeedbackChannel(d, "python3").run(t)]
    shutil.rmtree(d, ignore_errors=True)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agy", default=str(Path.home() / ".local/bin/agy"))
    ap.add_argument("--model", required=True)
    ap.add_argument("--panel", default=str(PANEL))
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=4)
    ap.add_argument("--max-chars", type=int, default=20_000)
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    panel = json.loads(Path(args.panel).read_text())
    tasks = []
    for t in panel["tasks"]:
        src = ROOTS.get(t["codebase"]) / t["module"]
        try:
            if src.stat().st_size <= args.max_chars:
                tasks.append(t)
        except OSError:
            continue
    tasks = tasks[: args.limit]
    if not tasks:
        print("no eligible tasks", file=sys.stderr)
        return 1

    random.Random(0xBEEF).shuffle(tasks)
    print(f"panel {panel['manifest_hash']}: {len(tasks)} tasks -> agy/{args.model}", flush=True)

    WORK.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, task in enumerate(tasks, 1):
        repo = ROOTS[task["codebase"]]
        pristine = (repo / task["module"]).read_text()
        mutant = next((x for x in generate(pristine, task["module"], limit=80).mutants
                       if x.bug_id == task["bug_id"]), None)
        if mutant is None:
            print(f"  [{i}/{len(tasks)}] {task['task_id'][:40]:40s} SKIP (bug_id gone)")
            continue
        buggy = apply_mutation(pristine, mutant)
        work = WORK / task["task_id"].replace("/", "_").replace(".", "")

        res = run_agent_task(args.agy, args.model, task, repo, buggy, work, args.timeout)
        graded = grade(task, repo, res["source_after"], work / "graded")
        fs = score_from_rows(task["task_id"], task["codebase"], "python", graded,
                             {task["bug_id"]: task["test_node"]})

        outcome = "scored" if res["changed"] else "declined_work"
        if res["smuggled_tests"]:
            outcome = "policy_violation"
        row = {
            "model": f"agy/{args.model}",
            "task_id": task["task_id"],
            "outcome": outcome,
            "ok": res["changed"],
            "latency_s": round(res["latency_s"], 3),
            "per_bug_score": round(fs.per_bug_score, 6),
            "bugs_fixed": fs.bugs_fixed,
            "bugs_total": fs.bugs_total,
            "file_clear": fs.file_clear,
            "codebase": task["codebase"],
            "language": "python",
            "manifest_hash": panel["manifest_hash"],
            "patch_chars": len(res["source_after"]) - len(buggy),
            "error": f"rc={res['rc']}" if res["rc"] != 0 else "",
            # Kept short but present: an agent harness fails for many reasons (unknown model
            # slug, auth, sandbox refusal) and "rc=1" alone is not diagnosable.
            "reply_tail": res["reply"][-400:],
            "smuggled_tests": res["smuggled_tests"],
        }
        rows.append(row)
        print(f"  [{i}/{len(tasks)}] {task['task_id'][:40]:40s} {outcome:16s} "
              f"score={fs.per_bug_score:.2f} {res['latency_s']:5.0f}s", flush=True)
        shutil.rmtree(work, ignore_errors=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(r) for r in rows))
    print(f"\nagy/{args.model}: {len(rows)} rows -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
