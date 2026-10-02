"""Combining mutants into multi-bug tasks.

A single-mutant file is a one-token diff: ``return x * 1`` becomes ``return x``. That is not
debugging, and it is why three frontier models all scored 1.00 on the first panel. The fix
is the thing the design always called for and never did: put **several defects in one file**,
so the model must find each one, and partial credit becomes meaningful.

Combining is only useful if it is genuinely harder, which rules out the naive version:

  * Mutants are chosen to be **nearby**. Two defects 400 lines apart are two easy tasks
    stapled together, not one hard one.
  * Mutants that share a test node cannot both be scored. One fix may satisfy both, so
    attributing credit per bug would be unsound. Such clusters are split across files
    instead of merged.
  * Every combination is re-verified: all bugs present must fail, un-mutating all must pass,
    and un-mutating **one** must leave exactly that bug failing. A combination whose
    individual verdicts cannot be separated is rejected rather than scored ambiguously.

The result is a task with a real ``bug_id -> test_node`` map, which is what per-bug scoring
requires and what a single-mutant task never exercised.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import dataclass, field

from .mutants import DIFFICULTY_ORDER, Mutant, apply_mutation


@dataclass
class CombinedBug:
    """One defect inside a combined task."""

    bug_id: str
    line: int
    operator: str
    difficulty: str
    symbol: str
    test_node: str
    mutants: list[Mutant] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"bug_id": self.bug_id, "line": self.line, "operator": self.operator,
                "difficulty": self.difficulty, "symbol": self.symbol,
                "test_node": self.test_node}


@dataclass
class CombinedTask:
    """A file carrying several defects and a verified per-bug oracle."""

    task_id: str
    codebase: str
    module: str
    language: str
    bugs: list[CombinedBug] = field(default_factory=list)
    green_tests: list[str] = field(default_factory=list)
    span_lines: int = 0
    verified: bool = False
    reject_reason: str = ""

    @property
    def bug_map(self) -> dict[str, str]:
        return {b.bug_id: b.test_node for b in self.bugs}

    @property
    def span(self) -> int:
        if not self.bugs:
            return 0
        lines = [b.line for b in self.bugs]
        return max(lines) - min(lines)

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "codebase": self.codebase,
            "module": self.module,
            "language": self.language,
            "bugs_total": len(self.bugs),
            "span_lines": self.span,
            "difficulty": _task_difficulty(self.bugs),
            "bugs": [b.as_dict() for b in self.bugs],
            "green_tests": self.green_tests,
            "verified": self.verified,
            "reject_reason": self.reject_reason,
        }


def _task_difficulty(bugs: list[CombinedBug]) -> str:
    """A combined task is as hard as its hardest bug, with a bonus for count.

    Two trivial bugs are still trivial: stacking easy defects does not manufacture
    difficulty, it only manufactures length. The bump applies once a non-trivial bug is
    present, which is the case worth ranking.
    """
    if not bugs:
        return "trivial"
    if any(b.difficulty == "hard" for b in bugs):
        return "hard"
    if any(b.difficulty == "moderate" for b in bugs):
        return "hard" if len(bugs) >= 3 else "moderate"
    return "trivial"


def apply_all(source: str, mutants: list[Mutant]) -> str:
    """Apply every mutant in one pass.

    Applied highest-line-first so an earlier mutation cannot shift the line numbers the
    later ones are keyed on.
    """
    out = source
    for m in sorted(mutants, key=lambda x: x.line, reverse=True):
        out = apply_mutation(out, m)
    return out


def _cluster_by_proximity(bugs: list[dict], max_gap: int) -> list[list[dict]]:
    """Group bugs whose lines lie within ``max_gap`` of each other.

    Proximity is what makes a combined task harder than its parts: a model that reads the
    surrounding code has to reason about interacting state rather than scan for one token.
    """
    ordered = sorted(bugs, key=lambda b: b["line"])
    clusters: list[list[dict]] = [[ordered[0]]]
    for b in ordered[1:]:
        if b["line"] - clusters[-1][-1]["line"] <= max_gap:
            clusters[-1].append(b)
        else:
            clusters.append([b])
    return clusters


def build_candidates(tasks: list[dict], target_bugs: int = 3, max_gap: int = 40,
                     min_bugs: int = 2, require_distinct_tests: bool = True,
                     ) -> list[CombinedTask]:
    """Enumerate plausible multi-bug tasks from swept single-mutant findings.

    Combinations are grown by taking the hardest available bugs within one cluster and
    capping at ``target_bugs``, so a file yields a few sensible tasks rather than the
    combinatorial explosion of every subset.
    """
    by_module: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in tasks:
        by_module[(t["codebase"], t["module"])].append(t)

    out: list[CombinedTask] = []
    for (codebase, module), pool in sorted(by_module.items()):
        clusters = _cluster_by_proximity(pool, max_gap)
        for cluster in clusters:
            usable = cluster
            if require_distinct_tests:
                # One node per bug, or a single fix could satisfy two "bugs" at once.
                seen: set[str] = set()
                usable = []
                for b in sorted(cluster, key=lambda x: DIFFICULTY_ORDER.get(
                        x.get("difficulty", "moderate"), 1)):
                    if b["test_node"] in seen:
                        continue
                    seen.add(b["test_node"])
                    usable.append(b)
            for size in range(min_bugs, target_bugs + 1):
                if len(usable) < size:
                    continue
                ordered = sorted(
                    usable, key=lambda b: (DIFFICULTY_ORDER.get(b.get("difficulty",
                                              "moderate"), 1), b["line"]))
                # Try the hardest-first window at each start, so we keep a few genuinely
                # hard variants instead of every subset.
                for start in range(0, len(ordered) - size + 1):
                    chosen = ordered[start:start + size]
                    if len({c["test_node"] for c in chosen}) != size:
                        continue
                    out.append(_make_task(codebase, module, chosen))
    return out


def _make_task(codebase: str, module: str, chosen: list[dict]) -> CombinedTask:
    seed = "+".join(c["bug_id"] for c in chosen)
    task = CombinedTask(
        task_id=f"{codebase}__{module.replace('/', '_').replace('.', '_')}__{seed[:16]}",
        codebase=codebase,
        module=module,
        language="python",
        green_tests=sorted({c.get("green_tests", []) and c["green_tests"][0]
                            or c.get("green_test", "") for c in chosen} - {""}),
    )
    if not task.green_tests:
        task.green_tests = list(chosen[0].get("green_tests", []))
    task.bugs = [
        CombinedBug(
            bug_id=c["bug_id"], line=c["line"], operator=c["operator"],
            difficulty=c.get("difficulty", "moderate"), symbol=c.get("symbol", ""),
            test_node=c["test_node"],
        )
        for c in chosen
    ]
    task.span_lines = task.span
    return task


def mutants_for(task: CombinedTask, mutants_by_id: dict[str, Mutant]) -> list[Mutant]:
    """Resolve a task's bug_ids back to Mutant objects."""
    return [mutants_by_id[b.bug_id] for b in task.bugs if b.bug_id in mutants_by_id]
