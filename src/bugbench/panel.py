"""Selecting and freezing the panel.

Supply is measured and uneven: some codebases yield dozens of admissible tasks and others
yield none at all, because a codebase with no hermetic tests cannot produce a task no
matter how many bugs it historically fixed. A naive "take the first 25" would then be
decided entirely by whichever repo happened to be swept first, and the result would read
as a model comparison when it is really a supply accident.

So selection is equal-weight per codebase: each contributing codebase gets the same
budget, and the leftovers are reported rather than quietly used. Codebases that
contribute nothing are named, because "these five repos are absent from the panel" is a
fact the user needs, not a gap to hide.

The manifest is hashed on write. Every later run refuses to mix rows from different
manifests, which is what makes "identical frozen conditions for every model" a mechanical
property instead of an intention.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

#: Target shape agreed with the user: 25 tasks, weighted equally per contributing codebase.
TARGET_TASKS = 25

#: Exposure classes. A public repo's fixes may already be memorised by frontier models, so
#: those tasks are legitimate -- they measure recall of public history -- but they must be
#: labelled and kept out of the headline aggregate.
PUBLIC_CODEBASES = {"marketplace-monitor"}


@dataclass
class Selection:
    """The chosen panel plus an honest account of what was left out."""

    tasks: list[dict] = field(default_factory=list)
    per_codebase_quota: dict[str, int] = field(default_factory=dict)
    excluded: dict[str, int] = field(default_factory=dict)
    empty_codebases: list[str] = field(default_factory=list)
    manifest_hash: str = ""

    def as_dict(self) -> dict:
        return {
            "manifest_hash": self.manifest_hash,
            "target_tasks": TARGET_TASKS,
            "selected": len(self.tasks),
            "per_codebase_quota": self.per_codebase_quota,
            "excluded_surplus": self.excluded,
            "codebases_with_no_tasks": self.empty_codebases,
            "tasks": self.tasks,
        }


def load_shards(paths: list[Path]) -> list[dict]:
    tasks: list[dict] = []
    for p in paths:
        if not Path(p).exists():
            continue
        data = json.loads(Path(p).read_text())
        tasks.extend(data if isinstance(data, list) else data.get("tasks", []))
    return tasks


def dedupe(tasks: list[dict]) -> list[dict]:
    """One task per bug_id.

    The same mutant can be reached from two sweeps of overlapping modules, and a duplicate
    would double-weight that bug in every aggregate while looking like two independent
    pieces of evidence.
    """
    seen: set[str] = set()
    out = []
    for t in tasks:
        key = f"{t['codebase']}::{t['module']}::{t['bug_id']}"
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def select(tasks: list[dict], target: int = TARGET_TASKS,
           prefer_depth: bool = True) -> Selection:
    """Pick ``target`` tasks with an equal budget per contributing codebase.

    Ordering within a codebase is deterministic: deeper (function-level) mutants first,
    because a mutated comparison inside a function is a behavioural defect, whereas a
    mutated module-level constant is closer to a rename and says less about debugging.
    """
    tasks = dedupe(tasks)
    by_cb: dict[str, list[dict]] = defaultdict(list)
    for t in tasks:
        by_cb[t["codebase"]].append(t)

    contributing = sorted(by_cb)
    if not contributing:
        return Selection(empty_codebases=[], manifest_hash="")

    quota = target // len(contributing)
    # Any remainder goes to the largest pools, deterministically, so the target is met
    # exactly rather than leaving capacity unused.
    remainder = target - quota * len(contributing)
    order = sorted(contributing, key=lambda cb: (-len(by_cb[cb]), cb))
    quotas = {cb: quota for cb in contributing}
    for cb in order[:remainder]:
        quotas[cb] += 1

    chosen: list[dict] = []
    excluded: dict[str, int] = {}
    for cb in contributing:
        pool = sorted(
            by_cb[cb],
            key=lambda t: (0 if (prefer_depth and t.get("depth") == "function") else 1,
                           t["module"], t["line"], t["bug_id"]),
        )
        take = quotas[cb]
        chosen.extend(pool[:take])
        if len(pool) > take:
            excluded[cb] = len(pool) - take

    chosen.sort(key=lambda t: (t["codebase"], t["module"], t["line"], t["bug_id"]))
    sel = Selection(
        tasks=chosen,
        per_codebase_quota=quotas,
        excluded=excluded,
        manifest_hash="",
    )
    sel.manifest_hash = compute_hash(sel)
    return sel


def compute_hash(sel: Selection) -> str:
    """Stable hash over the task identity set.

    Deliberately excludes anything environmental (paths, timestamps, sandbox dirs) so the
    same panel re-swept on another machine hashes identically, while any change to *which*
    bugs are in the panel changes the hash.
    """
    payload = [
        [t["codebase"], t["module"], t["bug_id"], t["test_node"]]
        for t in sorted(sel.tasks, key=lambda x: (x["codebase"], x["module"], x["bug_id"]))
    ]
    blob = json.dumps({"target": TARGET_TASKS, "tasks": payload}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def freeze(sel: Selection, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sel.as_dict(), indent=2, sort_keys=True))
    return path


def verify_rows(rows: list[dict], manifest_hash: str) -> tuple[bool, list[str]]:
    """Refuse to score rows produced under a different manifest.

    This is the mechanical half of "every model saw identical frozen conditions": mixing
    panels silently produces a comparison that means nothing, and the historical 552-row
    archive is exactly that failure.
    """
    mismatched = [r for r in rows if r.get("manifest_hash") != manifest_hash]
    if not mismatched:
        return True, []
    hashes = sorted({r.get("manifest_hash") for r in mismatched})
    return False, [
        f"{len(mismatched)} row(s) carry manifest {h!r}, expected {manifest_hash!r}"
        for h in hashes if h != manifest_hash
    ]
