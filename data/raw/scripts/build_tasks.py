#!/usr/bin/env python3
"""Build benchmark tasks from SINGLE edit pairs.

Rationale: an `edit` tool call gives an exact (oldString -> newString) pair for
one contiguous region, so the "before" and "after" are guaranteed to be the same
code region. Grouping several edits produced mismatched snippets, so we dropped
that. The goal statement is the nearest preceding user message in the session.

Also emits a `hint_alternatives` field when the user's own message proposed
alternatives ('try X or Y', 'instead', 'why not') — the pattern the user asked for.
"""
import json
import re
from difflib import unified_diff

IN = "/tmp/opencode/bench/data/pairs_bugfix.json"
OUT = "/tmp/opencode/bench/data/tasks_raw.jsonl"

# ---- quality gates -----------------------------------------------------------
MIN_LEN, MAX_LEN = 160, 3000
MIN_CHANGED_LINES = 2

# user's message is uninformative for a task prompt
GENERIC_GOAL = re.compile(
    r"^\s*(continue\.?|continue if you have next steps\.?|ok\.?|yes\.?|no\.?|go\.?|"
    r"do it\.?|stats\.?|next\.?|thanks?\.?|thank you\.?|perfect\.?|good\.?|nice\.?|"
    r"done\.?|cool\.?|great\.?|yep\.?|sure\.?|run\.?|proceed\.?|go ahead\.?|"
    r"now\.?|again\.?|same\.?|fix\.?|fix it\.?|yes do\.?|carry on\.?|continue\b.{0,20})",
    re.I)

# alternative-proposal signals ("try x or y")
ALT_RE = re.compile(
    r"\b(try\b|instead|alternativ\w*|why (?:not|did|does|do|is|are)|should be|"
    r"change (?:it|this|that|to)|swap\b|replace\b|or (?:use|try|do)|maybe use|"
    r"how about|what if|pls use|use [\w.#-]+ instead|i think)\b", re.I)

# the goal should hint at a defect
BUG_RE = re.compile(
    r"\b(bug|bugz|broken|does ?n[o']?t work|not work(?:ing)?|fail(?:s|ed|ure)?|"
    r"error|wrong|hang(?:s|ing)?|crash(?:es|ed)?|deadlock|stuck|freeze[sd]?|leak|"
    r"race|timeout|timed out|silently|no output|blank|infinite|loop forever|"
    r"truncat\w*|regression|revert(?:ed|s)?|reversed|inverted|swapped|"
    r"not (?:install|apply|start|send|show|display|render|load|connect|read|write)|"
    r"mismatch|wrong (?:answer|output|value|order|position|scale)|invalid|"
    r"null|nan|undefined|missing|double|duplicate|many times|too many|"
    r"never|always|only (?:first|last)|every time)\b", re.I)

LOGIC = re.compile(
    r"(^\s*(def |class |if |elif |else|for |while |try:|except|finally|return|"
    r"await |async |const |let |var |function |export |raise |throw |try|catch|"
    r"switch|case |break|continue)\b)"
    r"|(=\s*(None|null|True|False|true|false|\d|\[|\{|'))"
    r"|(==|!=|<=|>=|\+=|-=|\*=|/=)"
    r"|(\?\?|\|\||&&)"
    r"|(\.(get|append|pop|join|close|cancel|read|write|update|filter|map|sort)\()"
    r"|(^\s*[A-Za-z_]\w*\s*=[^=])", re.M)

TEST_PATH = re.compile(r"(^|/)(tests?/|test_[^/]*\.|[^/]*_test\.|[^/]*\.test\.|"
                       r"spec\.|[^/]*\.spec\.)", re.I)


def changed_lines(a, b):
    sm = unified_diff(a.split("\n"), b.split("\n"), lineterm="", n=0)
    return [l for l in sm if (l.startswith("+") or l.startswith("-"))
            and not l.startswith(("+++", "---"))]


def substantive(changed):
    """Count changed lines that carry logic, not whitespace/blank/comment churn."""
    n = 0
    for l in changed:
        s = l[1:].strip()
        if len(s) < 4:
            continue
        if s.startswith(("#", "//", "*", "/*")):
            continue
        n += 1
    return n


def lang_of(path):
    m = path.rsplit(".", 1)[-1].lower()
    return {"py": "python", "js": "javascript", "mjs": "javascript",
            "cjs": "javascript", "ts": "typescript", "tsx": "tsx",
            "sh": "shell", "html": "html", "css": "css", "go": "go",
            "rs": "rust"}.get(m, m)


def main():
    rows = json.load(open(IN))
    tasks, skips = [], {}

    def skip(reason):
        skips[reason] = skips.get(reason, 0) + 1

    seen_buggy = {}
    for r in rows:
        goal = (r["goal"] or "").strip()
        buggy, fixed = r["buggy"], r["fixed"]

        if not (MIN_LEN <= len(buggy) <= MAX_LEN):
            skip("len")
            continue
        if not (MIN_LEN <= len(fixed) <= MAX_LEN):
            skip("len")
            continue
        if goal and GENERIC_GOAL.match(goal):
            skip("generic_goal")
            continue

        ch = changed_lines(buggy, fixed)
        if len(ch) < MIN_CHANGED_LINES:
            skip("tiny_diff")
            continue
        if substantive(ch) < 1:
            skip("non_logic_diff")
            continue

        lang = lang_of(r["path"])
        if lang not in ("python", "javascript", "typescript", "tsx", "shell"):
            skip("lang")
            continue

        # de-dup identical (buggy,fixed) pairs
        key = hash((buggy, fixed))
        if key in seen_buggy:
            skip("dupe")
            continue
        seen_buggy[key] = True

        alts = bool(ALT_RE.search(goal))
        task = {
            "task_id": f"T{len(tasks)+1:04d}",
            "language": lang,
            "file": r["path"],
            "file_name": r["path"].rsplit("/", 1)[-1],
            "project": (r["directory"] or "").split("/")[-1] or "unknown",
            "is_test_file": bool(TEST_PATH.search(r["path"])),
            "goal": goal,
            "goal_had_alternatives": alts,
            "buggy": buggy,
            "fixed": fixed,
            "n_changed_lines": len(ch),
            "n_substantive": substantive(ch),
            "source_session": r["session_id"],
            "source_title": r["title"],
        }
        tasks.append(task)

    with open(OUT, "w") as f:
        for t in tasks:
            f.write(json.dumps(t) + "\n")

    import collections
    print("tasks:", len(tasks))
    print("skipped:", skips)
    print("langs:", collections.Counter(t["language"] for t in tasks).most_common())
    print("with goal:", sum(1 for t in tasks if len(t["goal"]) >= 25))
    print("goal had alternatives:", sum(1 for t in tasks if t["goal_has_alternatives"]))
    print("source files:", sum(1 for t in tasks if not t["is_test_file"]))


if __name__ == "__main__":
    main()
