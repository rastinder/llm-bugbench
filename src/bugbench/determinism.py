"""The noise-floor gate: is our own measurement stable?

Every TIED verdict in the final report is a statement about *models*, and it is only
meaningful if the harness itself contributes no variance. If running the same frozen task
twice can change a score, then "model A and model B are indistinguishable" may really mean
"our runner is flaky", and there is no way to tell the difference after the fact.

So this gate is deliberately narrow and mechanical: for a stratified subset of tasks, run
the identical buggy state and the identical fixed state several times each and require
byte-identical feedback. Pooled across tasks rather than per-cell, because the campaign
budget only affords a handful of repeats and per-cell repeats would prove almost nothing
at this sample size.

Flaky tests are not a hypothetical here: three of the target codebases touch the network
and wall-clock time, and a task that reaches either is a harness defect, not a model one.
"""

from __future__ import annotations

import hashlib
import statistics
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class Trial:
    """One repeated execution of one frozen task state."""

    task_id: str
    state: str            # "buggy" | "fixed"
    repeat: int
    passed: int           # bugs fixed
    total: int
    feedback: str = ""

    @property
    def score(self) -> float:
        return (self.passed / self.total) if self.total else 0.0

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.feedback.encode("utf-8", "replace")).hexdigest()[:12]


@dataclass
class NoiseFloor:
    """Result of the determinism check."""

    passed: bool = True
    unstable_tasks: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"passed": self.passed, "unstable_tasks": self.unstable_tasks,
                "failures": self.failures, "detail": self.detail}


def check(trials: list[Trial], min_repeats: int = 3) -> NoiseFloor:
    """Require identical scores (and identical feedback) within each (task, state).

    Grouped by (task, state) rather than by task, because the buggy and fixed states are
    different code and a task can be perfectly stable in one and flaky in the other -- most
    often when only the fixed state reaches the network.
    """
    groups: dict[tuple[str, str], list[Trial]] = defaultdict(list)
    for t in trials:
        groups[(t.task_id, t.state)].append(t)

    unstable: list[str] = []
    failures: list[str] = []
    digests_seen = 0

    for (task_id, state), ts in sorted(groups.items()):
        if len(ts) < min_repeats:
            failures.append(
                f"{task_id}/{state}: only {len(ts)} repeat(s), need {min_repeats}"
            )
            continue
        scores = {round(t.score, 9) for t in ts}
        digests = {t.digest for t in ts if t.feedback}
        digests_seen += 1
        if len(scores) > 1:
            unstable.append(f"{task_id}/{state}")
            failures.append(
                f"{task_id}/{state}: scores varied across repeats: {sorted(scores)}"
            )
        elif len(digests) > 1:
            unstable.append(f"{task_id}/{state}")
            failures.append(
                f"{task_id}/{state}: score was stable but the feedback text was not"
            )

    spread = 0.0
    unstable_scores: list[float] = []
    if trials:
        by_cell_scores: dict[tuple[str, str], list[float]] = defaultdict(list)
        for t in trials:
            by_cell_scores[(t.task_id, t.state)].append(t.score)
        # Per-cell, not global: a global max-minus-min compares DIFFERENT tasks against
        # each other, so a panel spanning 0.0 and 1.0 would always look "unstable" and the
        # number would say nothing about repeatability. Within one cell every trial is the
        # same task and state, so any spread there is real harness noise.
        unstable_scores = [
            max(v) - min(v) for v in by_cell_scores.values() if v
        ]
        spread = max(unstable_scores, default=0.0)

    if not trials:
        failures.append("no trials were supplied: a gate cannot pass on zero evidence")

    return NoiseFloor(
        passed=not failures,
        unstable_tasks=unstable,
        failures=failures,
        detail={
            "trials": len(trials),
            "cells": len(groups),
            "cells_measured": digests_seen,
            "min_repeats": min_repeats,
            "max_within_cell_spread": round(spread, 9),
            "distinct_tasks": len({t.task_id for t in trials}),
        },
    )


def stratified_subset(tasks: list[dict], n: int = 6, seed: int = 0) -> list[str]:
    """Pick a spread of tasks, not the first ``n``.

    First-N would concentrate on one codebase, so a flaky module could hide behind an
    unrelated stable one. Round-robin over codebases gives coverage of each.
    """
    by_cb: dict[str, list[str]] = defaultdict(list)
    for t in tasks:
        by_cb[t["codebase"]].append(t["task_id"])

    ordered = sorted(by_cb)
    out: list[str] = []
    idx = 0
    while len(out) < n and any(by_cb[c] for c in ordered):
        cb = ordered[idx % len(ordered)]
        if by_cb[cb]:
            out.append(by_cb[cb].pop(0))
        idx += 1
        if idx > 10_000:
            break
    return out[:n]
