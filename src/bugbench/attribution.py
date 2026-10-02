"""Verify a combined task can actually be scored per bug.

A combination is only usable if each defect can be attributed individually. The test is
strict on purpose, because a combination that fails it cannot be scored honestly:

  1. **all present**  every bug's test fails.
  2. **all fixed**    un-mutating everything makes every test pass.
  3. **one fixed**    fixing a single bug must make exactly that bug's test pass and leave
     the others failing.

Step 3 is the one that matters and the one that catches bad combinations. If un-mutating bug
A also turns bug B's test green, the two defects are not independent, and any per-bug score
would be double-counting one fix. Such tasks are rejected, not scored.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bugbench.combine import CombinedTask, apply_all          # noqa: E402
from bugbench.feedback import FeedbackChannel                  # noqa: E402
from bugbench.mutants import Mutant                            # noqa: E402
from bugbench.sandbox import build_sandbox, grader_env         # noqa: E402
from bugbench.scoring import score_from_rows                   # noqa: E402


@dataclass
class Attribution:
    """Outcome of verifying one combined task."""

    task: CombinedTask
    ok: bool
    reason: str = ""
    all_present_failing: int = 0
    all_fixed_passing: int = 0
    isolated_ok: int = 0
    per_bug_scores: dict[str, float] = field(default_factory=dict)


def _run(repo: Path, task, source: str, work: Path) -> list[dict]:
    """Grade one candidate source state for a task.

    Accepts either a CombinedTask or the plain dict loaded from JSON, because the campaign
    driver works from the serialised manifest and should not have to rebuild the dataclass
    just to grade a run.
    """
    module = getattr(task, "module", None) or task["module"]
    tests = getattr(task, "green_tests", None) or task["green_tests"]
    d = build_sandbox(repo, work)
    (d / module).write_text(source)
    rows = [r for t in tests for r in FeedbackChannel(d, "python3").run(t)]
    shutil.rmtree(d, ignore_errors=True)
    return rows


def _status(task: CombinedTask, rows: list[dict]) -> dict[str, bool]:
    by_node = {r["nodeid"]: r for r in rows}
    return {b.bug_id: by_node.get(b.test_node, {}).get("outcome") == "passed"
            for b in task.bugs}


def verify(repo: Path, pristine: str, task: CombinedTask,
           mutants_by_id: dict[str, Mutant], work: Path) -> Attribution:
    """Check all-present, all-fixed and per-bug isolation."""
    selected = [mutants_by_id[b.bug_id] for b in task.bugs if b.bug_id in mutants_by_id]
    if len(selected) != len(task.bugs):
        return Attribution(task, False, "a bug_id no longer regenerates its mutant")

    # 1. all present -> every bug's test fails
    rows = _run(repo, task, apply_all(pristine, selected), work / "all_present")
    st = _status(task, rows)
    present_failing = sum(1 for v in st.values() if not v)
    if present_failing != len(task.bugs):
        return Attribution(task, False,
                           f"only {present_failing}/{len(task.bugs)} bugs fail when all are "
                           f"present; a mutant may not be killed by this suite",
                           all_present_failing=present_failing)

    # 2. all fixed -> every bug's test passes
    rows = _run(repo, task, pristine, work / "all_fixed")
    st = _status(task, rows)
    fixed_passing = sum(1 for v in st.values() if v)
    if fixed_passing != len(task.bugs):
        return Attribution(task, False,
                           f"only {fixed_passing}/{len(task.bugs)} pass on pristine source",
                           all_present_failing=present_failing, all_fixed_passing=fixed_passing)

    # 3. per-bug isolation
    isolated = 0
    scores: dict[str, float] = {}
    for i, bug in enumerate(task.bugs):
        keep = [m for j, m in enumerate(selected) if j != i]
        rows = _run(repo, task, apply_all(pristine, keep), work / f"fix_{i}")
        st = _status(task, rows)
        target_ok = st.get(bug.bug_id, False)
        others_ok = [v for k, v in st.items() if k != bug.bug_id]
        if target_ok and not any(others_ok):
            isolated += 1
            scores[bug.bug_id] = 1.0
        elif target_ok and any(others_ok):
            return Attribution(task, False,
                               f"fixing {bug.bug_id} also turned "
                               f"{[k for k, v in st.items() if k != bug.bug_id and v]} green; "
                               f"these defects are not independent, so per-bug credit "
                               f"would double-count one fix",
                               all_present_failing=present_failing,
                               all_fixed_passing=fixed_passing, isolated_ok=isolated)
        else:
            scores[bug.bug_id] = 0.0

    return Attribution(task, True, "",
                       all_present_failing=present_failing,
                       all_fixed_passing=fixed_passing,
                       isolated_ok=isolated, per_bug_scores=scores)


def score_combined(repo: Path, task: CombinedTask, source: str, work: Path) -> dict:
    """Grade a candidate repair of a combined task, per bug."""
    rows = _run(repo, task, source, work)
    task_id = getattr(task, "task_id", None) or task["task_id"]
    codebase = getattr(task, "codebase", None) or task["codebase"]
    language = getattr(task, "language", None) or task.get("language", "python")
    bug_map = task.bug_map if hasattr(task, "bug_map") else {
        b["bug_id"]: b["test_node"] for b in task["bugs"]}
    fs = score_from_rows(task_id, codebase, language, rows, bug_map)
    return fs.as_dict()
