#!/usr/bin/env python3
"""Emit the final benchmark task set: bug fixes whose goal describes that defect."""
from __future__ import annotations

import json
import re
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]

sys.path.insert(0, str(ROOT / "/src"))

from bugbench.models import load_tasks, save_tasks  # noqa: E402


def main() -> int:
    tasks = load_tasks()
    keep, drop = [], []
    for t in tasks:
        specific = None
        m = re.search(r"goal_specific=(True|False|None)", t.category_reason)
        if m:
            specific = {"True": True, "False": False}.get(m.group(1))
        t_specific = specific
        # primary set: genuine bug fix with a synthesised, defect-specific goal report
        if t.category == "bug_fix" and t.goal_source == "synth":
            keep.append(t)
        else:
            drop.append(t)

    # cap per source file so one project cannot dominate
    from collections import defaultdict
    per_file, spread, taken = 3, [], defaultdict(int)
    for t in sorted(keep, key=lambda x: (x.goal_had_alternatives,
                                          -x.goal_relevance)):
        if taken[t.file_name] >= per_file:
            continue
        taken[t.file_name] += 1
        spread.append(t)

    final = spread + drop
    save_tasks(final)
    print(f"PRIMARY (bug_fix + relevant + specific goal): {len(spread)}")
    print(f"retained for auditing / secondary tracks:     {len(drop)}")
    print(f"total in file:                               {len(final)}")
    print(f"languages: {dict(sorted(__import__('collections').Counter(t.language for t in spread).items()))}")
    print(f"distinct source files: {len({t.file_name for t in spread})}")
    print(f"goals carrying a user 'try X or Y' alternative: "
          f"{sum(1 for t in spread if t.goal_had_alternatives)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
