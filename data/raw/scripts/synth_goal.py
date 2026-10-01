#!/usr/bin/env python3
"""Third pass: synthesise a precise, self-contained bug report from the diff.

The user's own message before the edit is often a broad standing request ("do whatever u
like", "run the loop and fix it"), which does not describe the specific defect. Asking a
model to fix a bug from such a prompt measures something ill-defined. So for the PRIMARY
task set we synthesise a crisp bug report from the (buggy, reference_fix) pair -- the same
ground truth the grader uses -- and keep the user's original wording in `original_goal`
for provenance and for the secondary "user-goal" track.

The synthesised report is derived from the reference fix, so it is a *task input*, never a
*model answer*: models still never see `reference_fix`.
"""
from __future__ import annotations

import json
import re
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from difflib import unified_diff

sys.path.insert(0, str(ROOT / "/src"))

from bugbench.grade import parse_json_block  # noqa: E402
from bugbench.models import load_tasks, save_tasks  # noqa: E402
from bugbench.runners import json_runner  # noqa: E402

RUNNER = json_runner(min_chars=40)

PROMPT = """You are writing a bug report for a benchmark task.

You are given a function and the CHANGED version of it. The changed version is the \
DEVELOPER'S OWN FIX. From the difference, write the bug report that the developer would \
have written BEFORE fixing it.

Write for a developer who can see the function but not the fix. Be specific about the \
observable wrong behaviour. Do not mention that a fix exists, do not mention diffs, files \
or line numbers, and do not use the words "bug" in a hand-wavy way -- state the actual \
defect.

Return JSON only:
{{"summary": "<=25 words: what the function should do>",
 "defect": "<=35 words: precisely what is wrong and under which input/condition it shows",
 "trigger": "<=20 words: the concrete input or condition that exposes it"}}"""


def diff_of(buggy: str, fixed: str, limit: int = 1600) -> str:
    d = [l for l in unified_diff(buggy.split("\n"), fixed.split("\n"), lineterm="", n=2)
         if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    return "\n".join(d)[:limit] or "(no textual change)"


def main() -> int:
    only = sys.argv[1:] or None
    tasks = load_tasks()
    todo = [t for t in tasks if t.category == "bug_fix"
            and (only is None or t.task_id in only)]
    print(f"synthesising goal reports for {len(todo)} bug-fix tasks", flush=True)

    def work(t):
        fn = t.buggy.strip()[:4000]
        p = PROMPT.replace("{", "{{").replace("}", "}}") + \
            f"\n\nCURRENT FUNCTION:\n```\n{fn}\n```\n" \
            f"\nDEVELOPER'S CHANGED VERSION (diff of the fix):\n```\n{diff_of(t.buggy, t.reference_fix)}\n```"
        try:
            r = RUNNER.complete_messages([{"role": "user", "content": p}])
            d = parse_json_block(r.text) if r.ok else None
            if not d or not (d.get("summary") and d.get("defect")):
                return t.task_id, None
            return t.task_id, d
        except Exception:
            return t.task_id, None

    got = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for tid, d in ex.map(work, todo):
            got[tid] = d
            print(f"  {tid} {'ok' if d else 'FAILED'}", flush=True)

    ok = 0
    for t in todo:
        d = got.get(t.task_id)
        if not d:
            continue
        t.original_goal = t.goal
        t.goal_source = "synth"
        t.goal = (f"{d['summary'].strip()} "
                  f"Defect: {d['defect'].strip()}"
                  + (f" Trigger: {d['trigger'].strip()}" if d.get("trigger") else ""))
        ok += 1
    save_tasks(tasks)
    print(f"\nsynthesised {ok}/{len(todo)} goal reports")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
