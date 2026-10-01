#!/usr/bin/env python3
"""Second curation pass: does the goal statement plausibly describe THIS defect?

The relevance heuristic only checks lexical overlap. This pass asks a model directly, and
keeps the annotation on every task so the decision is auditable.
"""
from __future__ import annotations

import json
import re
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(ROOT / "/src"))

from bugbench.grade import parse_json_block  # noqa: E402
from bugbench.models import load_tasks, save_tasks  # noqa: E402
from bugbench.runners import OpenAIChatRunner, json_runner  # noqa: E402

SPEC = json_runner()

PROMPT = """A developer was given this GOAL and then changed the code in this diff.

GOAL:
{goal}

DIFF (old -> new):
{diff}

Question: is the GOAL a description of the specific defect in this diff, or is it a
generic standing instruction / unrelated request that merely happened to be the most recent
message before the edit?

Answer JSON only:
{{"specific": true|false, "why": "<=20 words"}}"""


def diff_of(buggy: str, fixed: str, limit: int = 900) -> str:
    from difflib import unified_diff
    d = [l for l in unified_diff(buggy.split("\n"), fixed.split("\n"),
                                lineterm="", n=1)
         if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    return "\n".join(d)[:limit] or "(no textual diff)"


def main() -> int:
    tasks = load_tasks()
    runner = SPEC

    def work(t):
        p = PROMPT.format(goal=(t.goal or "")[:700], diff=diff_of(t.buggy, t.reference_fix))
        try:
            r = runner.complete_messages([{"role": "user", "content": p}])
            d = parse_json_block(r.text) if r.ok else None
            if not d or "specific" not in d:
                return t.task_id, None, "unparsed"
            return t.task_id, bool(d["specific"]), str(d.get("why", ""))[:120]
        except Exception as e:
            return t.task_id, None, f"{type(e).__name__}"

    out = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for tid, spec, why in ex.map(work, tasks):
            out[tid] = (spec, why)
            print(f"{tid} specific={spec} {why}", flush=True)

    for t in tasks:
        s, why = out.get(t.task_id, (None, "missing"))
        t.category_reason = f"{t.category_reason} | goal_specific={s} {why}".strip()
    save_tasks(tasks)
    ok = sum(1 for t in tasks if out.get(t.task_id, (None,))[0] is True)
    print(f"\nannotated {len(tasks)}; specific=True for {ok}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
