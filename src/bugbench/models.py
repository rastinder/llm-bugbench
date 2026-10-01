"""Task schema + JSONL store with strict validation."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

__version__ = "1.0.0"

PKG_ROOT = Path(__file__).resolve().parent.parent.parent
DATA = str(PKG_ROOT / "data" / "tasks.jsonl")
RESULTS = str(PKG_ROOT / "data" / "results.jsonl")

REQUIRED_FIELDS = ("task_id", "language", "goal", "buggy", "reference_fix")
CATEGORIES = ("bug_fix", "feature_addition", "refactor", "documentation",
              "configuration", "ambiguous")

DEFECT_RE = re.compile(
    r"\b(bug|bugz|broken|does ?n[o']?t work|not work(?:ing)?|fail(?:s|ed|ure)?|"
    r"error|wrong|hang(?:s|ing)?|crash(?:es|ed)?|deadlock|stuck|freeze[sd]?|leak|"
    r"race|timeout|timed out|silently|no output|blank|infinite|forever|"
    r"truncat\w*|regression|revert(?:ed|s)?|reversed|inverted|swapped|"
    r"not (?:install|apply|start|send|show|display|render|load|connect|read|write)|"
    r"mismatch|miss(?:es|ing)|only (?:first|last)|double|duplicate|too many|"
    r"abort|prevent|ignore[sd]?|skip(?:s|ped)?|overflow|never (?:shows|appears|works)|"
    r"throw[sd]?|empty|zero|null|none|undefined|raises?|blank|silent(?:ly)?|"
    r"fix|repair|reproduc\w*|instead|should be|wrong)\b", re.I)

_SYMBOL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")


def goal_relevance(task) -> float:
    """How well the user's goal statement actually describes THIS edit.

    A goal that names neither a symbol from the snippet nor a defect is a generic
    standing instruction ("continue", "do whatever you like") and would make the task
    prompt misleading. Scored so the pipeline can keep only well-matched goals on the
    primary leaderboard.
    """
    goal = (getattr(task, "goal", "") or "").lower()
    code = ((getattr(task, "buggy", "") or "") + "\n" +
            (getattr(task, "file_name", "") or "")).lower()
    if not goal:
        return 0.0
    toks = {t for t in _SYMBOL_RE.findall(code) if t not in _STOPWORDS}
    hits = sum(1 for t in toks if t in goal)
    defect = bool(DEFECT_RE.search(goal))
    score = 0.0
    score += min(0.5, 0.25 * hits)         # names a symbol from the snippet
    score += 0.5 if defect else 0.0        # describes a defect
    return round(min(1.0, score), 2)


_STOPWORDS = {
    "self", "return", "import", "print", "value", "values", "result", "results",
    "def", "class", "function", "const", "let", "true", "false", "none", "null",
    "data", "item", "items", "text", "line", "lines", "name", "path", "file",
    "with", "from", "this", "that", "have", "been", "were", "will", "your",
}


class TaskStoreError(RuntimeError):
    pass


@dataclass
class Task:
    task_id: str
    language: str
    goal: str
    buggy: str
    reference_fix: str
    file_name: str = ""
    project: str = "unknown"
    category: str = "unclassified"
    category_confidence: float = 0.0
    category_reason: str = ""
    classifier: str = ""
    oracle: dict = field(default_factory=lambda: {"kind": "none"})
    derived_from: str = "edit-pair"
    source_session: str = ""
    source_title: str = ""
    is_test_file: bool = False
    goal_had_alternatives: bool = False
    goal_relevance: float = 0.0
    goal_source: str = "user"
    original_goal: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Task":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict:
        return asdict(self)


def load_tasks(path: str = DATA) -> list[Task]:
    if not os.path.exists(path):
        raise TaskStoreError(f"task file not found: {path}")
    tasks: list[Task] = []
    seen: set[str] = set()
    with open(path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError as e:
                raise TaskStoreError(f"{path}:{lineno}: invalid JSON: {e}") from e
            missing = [k for k in REQUIRED_FIELDS if not d.get(k)]
            if missing:
                raise TaskStoreError(
                    f"{path}:{lineno}: task missing required field(s): {missing}")
            tid = d["task_id"]
            if tid in seen:
                raise TaskStoreError(f"{path}:{lineno}: duplicate task_id {tid!r}")
            seen.add(tid)
            tasks.append(Task.from_dict(d))
    return tasks


def save_tasks(tasks: list[Task], path: str = DATA) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for t in tasks:
            f.write(json.dumps(t.to_dict()) + "\n")
    return path


def primary_tasks(tasks: list[Task],
                  min_goal_relevance: float = 0.6) -> list[Task]:
    """Only true bug fixes with a goal that actually describes the defect.

    A generic standing instruction ("continue", "do whatever you like") is not a task
    prompt, so those tasks are kept in the file but excluded from the leaderboard. A task
    whose goal was synthesised from the diff is, by construction, defect-specific.
    """
    return [t for t in tasks
            if t.category == "bug_fix"
            and (t.goal_source == "synth" or t.goal_relevance >= min_goal_relevance)]


def counts_by_category(tasks: list[Task]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in tasks:
        out[t.category] = out.get(t.category, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
