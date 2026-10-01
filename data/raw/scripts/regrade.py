#!/usr/bin/env python3
"""Re-grade stored results offline.

Scoring is deterministic given (candidate, task), so a grading fix can be applied to
results that were already collected -- no model calls, no re-running, no wasted tokens.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]

sys.path.insert(0, str(ROOT / "/src"))

from bugbench.grade import score_diagnose, score_repair  # noqa: E402
from bugbench.models import load_tasks  # noqa: E402
from bugbench.oracle import build_oracle  # noqa: E402
from bugbench.runner import load_results  # noqa: E402


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "/data/results.jsonl")
    dst = sys.argv[2] if len(sys.argv) > 2 else src
    tasks = {t.task_id: t for t in load_tasks()}
    rows = load_results(src)
    changed = 0
    out = []
    for r in rows:
        t = tasks.get(r.get("task_id"))
        if t is None:
            out.append(r)
            continue
        if "diagnose" in r and r["diagnose"].get("parsed"):
            parsed = dict(r["diagnose"]["parsed"])
            # the judge score lives on the graded dict, not inside `parsed`
            parsed["judge_agreement"] = r["diagnose"].get("judge_agreement", 0.0)
            new = score_diagnose(t, parsed)
            for k in ("total", "location_match", "judge_agreement", "category_match"):
                if new.get(k) != r["diagnose"].get(k):
                    changed += 1
            r["diagnose"].update({k: new[k] for k in
                                  ("total", "location_match", "judge_agreement",
                                   "category_match")})
        if "repair" in r and r["repair"].get("candidate") is not None:
            o = build_oracle(t) if r["repair"].get("oracle") == "exec_diff" else None
            new = score_repair(t, r["repair"].get("candidate") or "",
                               r["repair"].get("judge_agreement", 0.0), oracle=o)
            if new["total"] != r["repair"].get("total"):
                changed += 1
            r["repair"].update(new)
        if "diagnose" in r and "repair" in r:
            r["combined"] = round(0.5 * r["diagnose"]["total"]
                                  + 0.5 * r["repair"]["total"], 4)
        out.append(r)

    with open(dst, "w") as f:
        for r in out:
            f.write(json.dumps(r) + "\n")
    print(f"re-graded {len(out)} rows, {changed} score(s) changed -> {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
