"""Dataset construction: raw candidates -> curated, deduped task set."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .curate import curate
from .grade import normalize_identifiers
from .models import Task, goal_relevance


def _load_raw(path: str) -> list[dict]:
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _dedupe(tasks: list[Task]) -> list[Task]:
    """Drop near-identical tasks (council: dedupe so one bug cannot be counted twice)."""
    seen: set[str] = set()
    out: list[Task] = []
    for t in tasks:
        key = normalize_identifiers(t.buggy) + "||" + normalize_identifiers(t.reference_fix)
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def _spread_by_file(tasks: list[Task], per_file: int = 2) -> list[Task]:
    """Cap how many tasks any single source file can contribute."""
    from collections import defaultdict
    byfile = defaultdict(list)
    for t in tasks:
        byfile[t.file_name].append(t)
    out, taken = [], defaultdict(int)
    for t in sorted(tasks, key=lambda x: (x.goal_had_alternatives,
                                          x.category_confidence), reverse=True):
        if taken[t.file_name] >= per_file:
            continue
        taken[t.file_name] += 1
        out.append(t)
    return out


def build_dataset(raw_path: str, use_llm: bool = True,
                  llm_model: str = "litellm-auto",
                  per_file: int = 2, max_tasks: int = 200,
                  min_goal_relevance: float = 0.6) -> list[Task]:
    """Curate raw candidates into the task set.

    `min_goal_relevance` keeps the primary leaderboard honest: a task whose goal statement
    is a generic standing instruction ("continue", "do whatever you like") rather than a
    description of THIS defect would make the task prompt misleading.
    """
    raw = _load_raw(raw_path)
    tasks = []
    for r in raw:
        r = dict(r)
        if "fixed" in r and "reference_fix" not in r:
            r["reference_fix"] = r.pop("fixed")
        if "goal_has_alternatives" in r:
            r["goal_had_alternatives"] = r.pop("goal_has_alternatives")
        tasks.append(Task.from_dict(r))
    for i, t in enumerate(tasks, 1):
        if t.task_id.startswith("T"):
            t.task_id = f"B{i:04d}"
    tasks = curate(tasks, llm=_llm(llm_model) if use_llm else None, workers=8)
    tasks = _dedupe(tasks)
    bugs = [t for t in tasks if t.category == "bug_fix"]
    others = [t for t in tasks if t.category != "bug_fix"]
    bugs = _spread_by_file(bugs, per_file=per_file)
    tasks = bugs + others
    for t in tasks:
        t.goal_relevance = goal_relevance(t)
        if t.original_goal:
            t.goal_source = "synth"
    return tasks[:max_tasks] if max_tasks else tasks


def primary(tasks: list[Task], min_goal_relevance: float = 0.6) -> list[Task]:
    """The set that belongs on the primary leaderboard."""
    return [t for t in tasks
            if t.category == "bug_fix" and t.goal_relevance >= min_goal_relevance]


def _llm(model_name: str):
    from .runners import ModelError, get, runner_for
    try:
        return runner_for(get(model_name))
    except ModelError:
        return None
