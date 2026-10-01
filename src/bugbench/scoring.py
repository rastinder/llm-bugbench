"""Per-bug scoring and the oracle-validation gate.

The panel's central risk is a scorer that cannot tell a repair from a non-repair. If a
partial-credit grader awards credit for an untouched file, or for a change that fixes one
case while breaking another, every model comparison downstream is noise with a confident
face. The gate therefore requires three properties, all observable from the artefacts:

  1. KNOWN GOOD scores 1.0 per bug  -- the un-mutation is the reference repair.
  2. KNOWN BAD  scores 0.0 per bug  -- pristine source, and the negated fix.
  3. MONOTONE            -- fixing more bugs never scores lower.

Scoring is per bug, never per file. A file is 1 bug and 4 bugs and must not score the same,
because "any test passed" is exactly the gaming vector equal-permission aggregation opens.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BugScore:
    """One bug's outcome for one model on one file."""

    bug_id: str
    test_node: str
    passed: bool
    errored: bool = False

    @property
    def value(self) -> float:
        """Full credit for pass, zero otherwise.

        There is no partial value: the tests are pass/fail assertions, and inventing a
        fractional score from "how close" the output looked would reward guessing.
        """
        return 1.0 if self.passed and not self.errored else 0.0

    def as_dict(self) -> dict:
        return {"bug_id": self.bug_id, "test_node": self.test_node,
                "passed": self.passed, "errored": self.errored, "value": self.value}


@dataclass
class FileScore:
    """A file's per-bug breakdown plus the aggregates reported alongside it."""

    task_id: str
    codebase: str
    language: str
    bugs: list[BugScore] = field(default_factory=list)

    @property
    def bugs_total(self) -> int:
        return len(self.bugs)

    @property
    def bugs_fixed(self) -> int:
        return sum(1 for b in self.bugs if b.value == 1.0)

    @property
    def per_bug_score(self) -> float:
        """Primary metric: mean credit across this file's bugs."""
        if not self.bugs:
            return 0.0
        return sum(b.value for b in self.bugs) / len(self.bugs)

    @property
    def file_clear(self) -> bool:
        """All bugs fixed. Reported as the anti-gaming headline, never as the primary."""
        return bool(self.bugs) and self.bugs_fixed == self.bugs_total

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "codebase": self.codebase,
            "language": self.language,
            "bugs_total": self.bugs_total,
            "bugs_fixed": self.bugs_fixed,
            "per_bug_score": round(self.per_bug_score, 6),
            "file_clear": self.file_clear,
            "bugs": [b.as_dict() for b in self.bugs],
        }


def score_from_rows(task_id: str, codebase: str, language: str,
                    rows: list[dict], bug_map: dict[str, str]) -> FileScore:
    """Turn raw feedback rows into a per-bug score.

    ``bug_map`` is the pre-registered ``bug_id -> test_node`` map. A bug with no matching
    test in the results is scored 0.0 and flagged, never dropped: silently omitting it
    would inflate every rate it appears in.
    """
    by_node = {r["nodeid"]: r for r in rows}
    scores = []
    for bug_id, node in bug_map.items():
        row = by_node.get(node)
        if row is None:
            scores.append(BugScore(bug_id, node, passed=False, errored=True))
            continue
        outcome = row.get("outcome")
        scores.append(BugScore(
            bug_id, node,
            passed=(outcome == "passed"),
            errored=(outcome in {"failed", "error"}),
        ))
    return FileScore(task_id, codebase, language, scores)


# ---------------------------------------------------------------------------
# Oracle-validation gate
# ---------------------------------------------------------------------------

@dataclass
class GateResult:
    passed: bool
    failures: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)


def validate_oracle(
    good: list[FileScore],
    bad: list[FileScore],
    tolerance: float = 0.0,
) -> GateResult:
    """Known-good must earn full credit; known-bad must earn none.

    ``tolerance`` exists only to absorb float noise, and defaults to zero on purpose: the
    inputs are exact 0.0/1.0 fractions, so any slack here would mask a real defect.
    """
    failures: list[str] = []

    for fs in good:
        if fs.bugs_fixed != fs.bugs_total:
            failures.append(
                f"{fs.task_id}: known-good fixed {fs.bugs_fixed}/{fs.bugs_total}, expected all"
            )
    for fs in bad:
        if fs.bugs_fixed > 0:
            failures.append(
                f"{fs.task_id}: known-bad scored {fs.bugs_fixed}/{fs.bugs_total}, expected 0"
            )

    monotonic = _is_monotone(bad, good)
    if not monotonic:
        failures.append("scoring is not monotone: known-bad outscored known-good somewhere")

    return GateResult(
        passed=not failures,
        failures=failures,
        detail={
            "known_good_files": len(good),
            "known_bad_files": len(bad),
            "good_mean": round(sum(f.per_bug_score for f in good) / len(good), 6) if good else None,
            "bad_mean": round(sum(f.per_bug_score for f in bad) / len(bad), 6) if bad else None,
            "tolerance": tolerance,
        },
    )


def _is_monotone(bad: list[FileScore], good: list[FileScore]) -> bool:
    """No known-bad file may beat any known-good file."""
    if not bad or not good:
        return True
    worst_good = min(f.per_bug_score for f in good)
    best_bad = max(f.per_bug_score for f in bad)
    return worst_good >= best_bad
