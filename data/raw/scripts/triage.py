#!/usr/bin/env python3
"""Triage episodes into benchmark tasks.

Quality gates for a usable benchmark task:
  - the buggy/fixed snippets are real code (not config/JSON)
  - the diff is a genuine behavioural change (not whitespace/rename-only)
  - the goal is informative (user's own words) OR synthesizable from the diff
  - snippets are self-contained enough for a model to reason about
"""
import json
import re
from difflib import unified_diff

IN = "/tmp/opencode/bench/data/episodes.json"
OUT = "/tmp/opencode/bench/data/tasks.jsonl"

JUNK_GOAL = re.compile(
    r"^(continue if you have next steps|continue\.|continue$|ok$|yes$|go$|do it|"
    r"stats$|next$|thanks?$|perfect\.|good\.|nice\.|done\.?$)", re.I)

# goals so generic they carry no information for a task prompt
GENERIC = re.compile(
    r"continue if you have next steps|^\W*$|^ok\b|^yes\b|^go\b|^do it\b|^stats\b|"
    r"^next\b|^thanks?\b|^perfect|^good\.|^nice\.|^done\b", re.I)


def diff_score(a: str, b: str) -> float:
    """Fraction of changed lines that are non-trivial."""
    al = [l for l in a.split("\n")]
    bl = [l for l in b.split("\n")]
    sm = unified_diff(al, bl, lineterm="", n=0)
    changed = [l for l in sm if (l.startswith("+") or l.startswith("-"))
               and not l.startswith(("+++", "---"))]
    if not changed:
        return 0.0
    trivial = sum(1 for l in changed
                  if re.fullmatch(r"\s*[+-]\s*", l) or len(l.strip()) <= 3)
    return 1.0 - trivial / len(changed)


def has_behavioural_signal(a: str, b: str) -> bool:
    """Look for logic-bearing tokens that appear on one side only."""
    pats = [r"if\b", r"else\b", r"for\b", r"while\b", r"try\b", r"except\b",
            r"return\b", r"await\b", r"async\b", r"\bNone\b", r"\bnull\b",
            r"\bTrue\b|\btrue\b", r"\bFalse\b|\bfalse\b", r"\+=|-=|=.*\+",
            r"\boffset\b|\btimeout\b|\bretry|\bretries\b|\bcount\b|\bmax\b|\bmin\b",
            r"\.get\(|\[.*\]", r"\bdef\b|\bfunction\b|\bclass\b", r"\+\+|--",
            r"\?\?|\|\||&&", r"await\b", r"\.close\(\)|\.cancel\(\)|\.join\("]
    joined_a, joined_b = a, b
    for p in pats:
        na = len(re.findall(p, joined_a))
        nb = len(re.findall(p, joined_b))
        if abs(na - nb) > 0:
            return True
    return diff_score(a, b) > 0.25


def language_of(path: str) -> str:
    m = path.rsplit(".", 1)[-1].lower()
    return {"py": "python", "js": "javascript", "mjs": "javascript",
            "cjs": "javascript", "ts": "typescript", "tsx": "tsx",
            "sh": "shell", "html": "html", "css": "css",
            "go": "go", "rs": "rust"}.get(m, m)


def main():
    eps = json.load(open(IN))
    tasks = []
    skipped = {"no_goal": 0, "junk_goal": 0, "tiny": 0, "weak_diff": 0,
               "huge": 0, "config": 0}
    for e in eps:
        goal = (e["goal"] or "").strip()
        if len(goal) < 15:
            skipped["no_goal"] += 1
            continue
        if JUNK_GOAL.match(goal):
            skipped["junk_goal"] += 1
            continue
        if GENERIC.match(goal):
            skipped["junk_goal"] += 1
            continue
        if len(e["buggy"]) < 150 or len(e["fixed"]) < 150:
            skipped["tiny"] += 1
            continue
        if len(e["buggy"]) > 4000 or len(e["fixed"]) > 4000:
            skipped["huge"] += 1
            continue
        ds = diff_score(e["buggy"], e["fixed"])
        if ds < 0.3:
            skipped["weak_diff"] += 1
            continue
        lang = language_of(e["path"])
        if lang in ("json", "yaml", "service", "conf"):
            skipped["config"] += 1
            continue
        if not has_behavioural_signal(e["buggy"], e["fixed"]):
            skipped["weak_diff"] += 1
            continue

        tasks.append({
            "task_id": e["episode_id"],
            "language": lang,
            "file": e["path"],
            "project": (e["directory"] or "").split("/")[-1],
            "goal": goal,
            "buggy": e["buggy"],
            "fixed": e["fixed"],
            "n_edits": e["n_edits"],
            "fix_signal": e["fix_signal"],
            "diff_strength": round(ds, 3),
            "source_session": e["session_id"],
            "source_title": e["title"],
        })

    with open(OUT, "w") as f:
        for t in tasks:
            f.write(json.dumps(t) + "\n")
    print("usable tasks:", len(tasks))
    print("skipped:", skipped)
    import collections
    print("by language:", collections.Counter(t["language"] for t in tasks).most_common())
    print("by project:", collections.Counter(t["project"] for t in tasks).most_common(12))


if __name__ == "__main__":
    main()
