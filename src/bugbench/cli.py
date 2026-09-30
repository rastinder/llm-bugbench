"""Command line: curate -> build -> run -> report."""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from pathlib import Path

from .curate import curate
from .models import (DATA, RESULTS, counts_by_category, load_tasks, primary_tasks,
                     save_tasks)
from .report import report as build_report
from .runner import Judge, append_results, load_results, run_model
from .runners import ModelSpec, json_runner, registry


def _judge_spec() -> ModelSpec:
    return ModelSpec(name="__judge__", base_url="https://aitshirts.in/litellm/v1",
                     api_key="sk-litellm-vps-2026", model="auto", max_tokens=200)


def cmd_build(args) -> int:
    from .pipeline import build_dataset
    tasks = build_dataset(args.raw, use_llm=not args.no_llm, llm_model=args.llm)
    save_tasks(tasks, args.out)
    counts = counts_by_category(tasks)
    print(f"wrote {len(tasks)} tasks -> {args.out}")
    for k, v in counts.items():
        print(f"  {k:20s} {v}")
    return 0


def cmd_list(args) -> int:
    tasks = load_tasks(args.data)
    if args.category:
        tasks = [t for t in tasks if t.category == args.category]
    print(f"{len(tasks)} tasks; categories: {counts_by_category(load_tasks(args.data))}")
    for t in tasks[:args.limit]:
        flag = "*" if t.goal_had_alternatives else " "
        print(f"{flag} {t.task_id} {t.language:11s} {t.category:17s} "
              f"{t.file_name[:30]:30s} {t.goal[:60]!r}")
    return 0


def cmd_models(args) -> int:
    for m in registry():
        print(f"{m['name']:24s} {m['kind']:8s} {m['model']:26s} {m.get('notes','')}")
    return 0


def cmd_run(args) -> int:
    tasks = load_tasks(args.data)
    if args.category != "all":
        tasks = [t for t in tasks if t.category == args.category]
    if not tasks:
        print(f"no tasks in category {args.category!r}", file=sys.stderr)
        return 2
    if args.shuffle:
        import random
        random.Random(args.seed).shuffle(tasks)
    tasks = tasks[:args.limit]
    judge = (Judge(runner=json_runner(min_chars=10)) if not args.no_judge
             else Judge(spec=None))
    # persist incrementally: a timeout on a slow/rate-limited model used to throw away
    # every task that had already completed
    sink = (lambda r: append_results([r], args.results)) if args.append else None
    rows = run_model(args.model, tasks, stages=tuple(args.stages), judge=judge,
                     workers=args.workers, judge_ref_aware=args.judge_ref_aware,
                     on_row=sink)
    if args.append and not sink:
        append_results(rows, args.results)
    if args.append:
        print(f"appended {len(rows)} rows -> {args.results}")
    for r in rows:
        d = (r.get("diagnose") or {}).get("total", 0)
        p = (r.get("repair") or {}).get("total", 0)
        print(f"  {r['task_id']} {r['language']:11s} diagnose={d:.3f} repair={p:.3f} "
              f"combined={r.get('combined', 0):.3f}"
              f"  {('ERR ' + (r.get('repair_error') or r.get('diagnose_error') or '')) if (r.get('repair_error') or r.get('diagnose_error')) else ''}")
    return 0


def cmd_report(args) -> int:
    rows = load_results(args.results)
    if not rows:
        print("no results yet", file=sys.stderr)
        return 2
    rep = build_report(rows)
    if not args.json:
        from bugbench.marks import leaderboard_marks
        from bugbench.models import load_tasks as _lt
        try:
            tasks = {t.task_id: t for t in _lt(args.data)}
            from bugbench.marks import common_task_set
            common = common_task_set(rows)
            print(f"{'model':32s} {'found':>13s} {'fixed':>13s} {'same':>9s} "
                  f"{'untouch':>9s} {'score':>7s}")
            for m in leaderboard_marks(rows, tasks):
                print(f"{m['model']:32s} {m['found_str']:>13s} {m['fixed_str']:>13s} "
                      f"{m['same_fix']:>4}/{m['tasks']:<4d} {m['untouched']:>4}/{m['tasks']:<4d} "
                      f"{m['score_pct']:6.1f}%")
            print(f"\n-- SAME-TASK COMPARISON ({len(common)} tasks every model above "
                  f"attempted) --")
            print(f"{'model':32s} {'found':>13s} {'fixed':>13s} {'tried':>7s} {'score':>7s}")
            for m in leaderboard_marks(rows, tasks, restrict_to=common):
                flag = "" if m["attempted"] == m["tasks"] else "  <- partial"
                print(f"{m['model']:32s} {m['found_str']:>13s} {m['fixed_str']:>13s} "
                      f"{m['attempted']:>3}/{m['tasks']:<3d} {m['score_pct']:6.1f}%{flag}")
            print()
        except Exception as e:
            print(f"(markers unavailable: {e})", file=sys.stderr)
        from bugbench.cheat import screen
        scr = screen(rows, {t.task_id: t for t in _lt(args.data)}
                     if args.data and pathlib.Path(args.data).exists() else {})
        print(f"CHEAT SCREEN: {scr['total_flagged']} flagged of {scr['rows_screened']} rows")
        for m in scr["per_model"]:
            if m["flagged"]:
                print(f"  FLAG {m['model']}: {m['flagged']}/{m['rows']} {m['by_signal']}")
        print()
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        for entry in rep["leaderboard_combined"]:
            print(f"{entry['model']:24s} {entry['score']*100:5.1f}% "
                  f"[{entry['ci95_lo']*100:.1f},{entry['ci95_hi']*100:.1f}] "
                  f"n={entry['tasks']} solved={entry['solved']} errors={entry['errors']}")
    if args.html:
        from .html_report import render_html
        from bugbench.cheat import screen
        from bugbench.models import load_tasks as _lt2
        tk = {t.task_id: t for t in _lt2(args.data)}
        Path(args.html).write_text(render_html(rows, tk, screen(rows, tk)))
        print(f"wrote {args.html}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="bugbench",
                                description="LLM bug-fixing benchmark")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="curate raw candidates into the task set")
    b.add_argument("--raw", default=str(Path(__file__).parent.parent.parent /
                                        "data/raw/tasks_raw.jsonl"))
    b.add_argument("--out", default=DATA)
    b.add_argument("--no-llm", action="store_true", help="heuristic labels only")
    b.add_argument("--llm", default="litellm-auto", help="model for LLM labels")
    b.set_defaults(func=cmd_build)

    l = sub.add_parser("list", help="list curated tasks")
    l.add_argument("--data", default=DATA)
    l.add_argument("--category", default=None)
    l.add_argument("--limit", type=int, default=40)
    l.set_defaults(func=cmd_list)

    m = sub.add_parser("models", help="list available models")
    m.set_defaults(func=cmd_models)

    r = sub.add_parser("run", help="run a model over the task set")
    r.add_argument("model")
    r.add_argument("--data", default=DATA)
    r.add_argument("--category", default="bug_fix")
    r.add_argument("--limit", type=int, default=20)
    r.add_argument("--workers", type=int, default=3)
    r.add_argument("--stages", nargs="+",
                   default=["diagnose", "repair", "repair_given"])
    r.add_argument("--shuffle", action="store_true")
    r.add_argument("--seed", type=int, default=7)
    r.add_argument("--no-judge", action="store_true")
    r.add_argument("--judge-ref-aware", action="store_true")
    r.add_argument("--results", default=RESULTS)
    r.add_argument("--append", action="store_true")
    r.set_defaults(func=cmd_run)

    rep = sub.add_parser("report", help="leaderboard")
    rep.add_argument("--results", default=RESULTS)
    rep.add_argument("--data", default=DATA)
    rep.add_argument("--json", action="store_true")
    rep.add_argument("--html", default=None)
    rep.set_defaults(func=cmd_report)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
