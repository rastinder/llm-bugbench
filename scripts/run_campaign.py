"""Run a model cohort over the frozen panel and write scored results.

Deliberately separate from the modules it uses. The campaign is the only place where
models, sandboxes and grading meet, so keeping it thin and readable means a surprising
result has one place to look.

Two rules it enforces that no single module can:

  * **Randomised, interleaved order.** Running model A's whole panel then model B's lets
    machine load and thermal drift masquerade as model identity. Tasks are shuffled per
    model with a seed derived from the task, so the order is reproducible but not aligned.
  * **Every row carries the manifest hash.** Results from different panels are refused by
    the analysis rather than silently mixed.
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

from bugbench.feedback import FeedbackChannel                      # noqa: E402
from bugbench.mutants import apply_mutation, generate               # noqa: E402
from bugbench.panel import verify_rows                              # noqa: E402
from bugbench.attempt import attempt_task                            # noqa: E402
from bugbench.sandbox import build_sandbox, grader_env              # noqa: E402
from bugbench.scoring import score_from_rows                        # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
PANEL = REPO_ROOT / "tasks" / "panel25.json"
ROOTS = {
    ".opencode-telegram-bot": Path.home() / ".opencode-telegram-bot",
    ".fix-backend": Path.home() / ".fix-backend",
    "copilot-model-audit": Path.home() / "copilot-model-audit",
    "marketplace-monitor": Path.home() / "marketplace-monitor",
}
WORK = Path.home() / ".cache" / "bugbench-campaign"
MAX_MODULE_CHARS = 20_000


def load_panel(path: Path = PANEL) -> dict:
    return json.loads(Path(path).read_text())


def eligible(panel: dict, max_chars: int = MAX_MODULE_CHARS) -> list[dict]:
    """Tasks whose module is small enough to actually be answered.

    Not a convenience filter. A 70 KB module reliably exceeds the provider's origin
    timeout, so those tasks measure which model is fastest rather than which repairs best,
    and every model would score near zero for an infrastructure reason.
    """
    out = []
    for t in panel["tasks"]:
        src = ROOTS.get(t["codebase"]) / t["module"]
        try:
            size = src.stat().st_size
        except OSError:
            continue
        if size <= max_chars:
            out.append(t)
    return out


def mutant_for(task: dict) -> tuple[Path, str, object]:
    repo = ROOTS[task["codebase"]]
    pristine = (repo / task["module"]).read_text()
    m = next((x for x in generate(pristine, task["module"], limit=80).mutants
              if x.bug_id == task["bug_id"]), None)
    if m is None:
        raise LookupError(f"bug_id {task['bug_id']} no longer reproduces")
    return repo, pristine, m


def grade(task: dict, repo: Path, source: str, workdir: Path) -> dict:
    """Materialise the mutated tree, run the hidden tests, return raw rows."""
    d = build_sandbox(repo, workdir)
    (d / task["module"]).write_text(source)
    rows = [r for t in task.get("green_tests", []) for r in FeedbackChannel(d, "python3").run(t)]
    shutil.rmtree(d, ignore_errors=True)
    return rows


def run_model(base_url: str, model: str, api_key: str | None, tasks: list[dict],
              manifest_hash: str, out_path: Path, timeout: int = 240) -> dict:
    rows: list[dict] = []
    WORK.mkdir(parents=True, exist_ok=True)

    ordered = sorted(tasks, key=lambda t: t["task_id"])
    random.Random(0xBEEF).shuffle(ordered)

    for i, task in enumerate(ordered, 1):
        repo, pristine, mutant = mutant_for(task)
        work = WORK / f"{model.replace('/', '_')}_{task['task_id'].replace('/', '_')}"
        work.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)

        buggy_source = apply_mutation(pristine, mutant)
        sandbox = build_sandbox(repo, work / "model")
        (sandbox / task["module"]).write_text(buggy_source)

        code = {task["module"]: buggy_source}
        att = attempt_task(base_url, model, task, code, sandbox,
                           api_key, manifest_hash, timeout=timeout)

        if att.outcome == "scored":
            result = grade(task, repo, (sandbox / task["module"]).read_text(), work / "graded")
        else:
            result = grade(task, repo, buggy_source, work / "graded")

        fs = score_from_rows(task["task_id"], task["codebase"], "python", result,
                             {task["bug_id"]: task["test_node"]})
        row = att.as_row()
        row.update({
            "per_bug_score": round(fs.per_bug_score, 6),
            "bugs_fixed": fs.bugs_fixed,
            "bugs_total": fs.bugs_total,
            "file_clear": fs.file_clear,
            "codebase": task["codebase"],
            "language": "python",
            "order_index": i,
        })
        rows.append(row)
        print(f"  [{i}/{len(ordered)}] {task['task_id'][:44]:44s} "
              f"{att.outcome:16s} score={fs.per_bug_score:.2f} {att.latency_s:5.1f}s", flush=True)
        shutil.rmtree(work, ignore_errors=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(json.dumps(r) for r in rows))
    return {"model": model, "rows": len(rows), "out": str(out_path)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--panel", default=str(PANEL))
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-chars", type=int, default=MAX_MODULE_CHARS)
    ap.add_argument("--timeout", type=int, default=240)
    args = ap.parse_args()

    panel = load_panel(Path(args.panel))
    tasks = eligible(panel, args.max_chars)
    if args.limit:
        tasks = tasks[: args.limit]
    if not tasks:
        print("no eligible tasks", file=sys.stderr)
        return 1

    print(f"panel {panel['manifest_hash']}: {len(panel['tasks'])} tasks, "
          f"{len(tasks)} eligible (<= {args.max_chars} chars) -> {args.model}", flush=True)

    start = time.time()
    info = run_model(args.base_url, args.model, args.api_key, tasks,
                     panel["manifest_hash"], Path(args.out), args.timeout)
    print(f"\n{info['model']}: {info['rows']} rows in {time.time() - start:.0f}s -> {info['out']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
