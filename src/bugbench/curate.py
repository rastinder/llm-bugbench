"""Dataset curation (council block 1: mandatory, first-class stage).

Every candidate edit-pair must be classified before it can appear on the primary
leaderboard, otherwise the benchmark measures *patch imitation* rather than bug-fixing.

Classification is deterministic-first: `heuristic_category` inspects the actual diff and
the goal text. An optional LLM pass can refine the label; when no LLM is available the
heuristic label stands and `classifier` records which path produced it.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from .models import Task

# ---------------------------------------------------------------- signals

DEFENCE_ADDED = re.compile(
    r"(if\s+not\b|if\s+\w+\s+is\s+None|if\s+\w+\s+is\s+not\b|is\s+None\b|"
    r"if\s+len\(|if\s+not\s+values|raise\s+\w*(Error|Exception)|"
    r"return\s+None|return\s+\{\}|return\s+\[\]|defaultdict|"
    r"except\b|try:|finally:|try\s*:|continue\b|pass\b|"
    r"max\(0,|min\(|clamp|guard|break\b)", re.I)

BRANCH_ADDED = re.compile(
    r"(^\s*(if|elif|else|for|while|try|except|switch|case)\b)", re.M)

CALL_ADDED = re.compile(r"^\s*\+?\s*[\w.]+\s*\(", re.M)

STRING_ONLY = re.compile(r"""(['"`])""")

GOAL_DEFECT = re.compile(
    r"\b(bug|bugz|broken|does ?n[o']?t work|not work(?:ing)?|fail(?:s|ed|ure)?|"
    r"error|wrong|hang(?:s|ing)?|crash(?:es|ed)?|deadlock|stuck|freeze[sd]?|leak|"
    r"race|timeout|timed out|silently|no output|blank|infinite|forever|"
    r"truncat\w*|regression|revert(?:ed|s)?|reversed|inverted|swapped|"
    r"not (?:install|apply|start|send|show|display|render|load|connect|read|write)|"
    r"mismatch|wrong (?:answer|output|value|order|position|scale|stage)|"
    r"miss(?:es|ing)|only (?:first|last)|double|duplicate|too many|"
    r"abort|prevent|guard|ignore[sd]?|skip(?:s|ped)?|leak|overflow|"
    r"not applying|never (?:shows|appears|works))", re.I)

GOAL_FEATURE = re.compile(
    r"\b(add|implement|create|introduce|build the|support|refactor|rename|"
    r"move|extract|describ\w*|document|clearer|nice|clean ?up|hardening|"
    r"proactively|so we can|for next)\b", re.I)

GOAL_RENAME = re.compile(
    r"\b(rename|typo|wording|consisten\w*|clarity|clearer|naming|style)\b", re.I)


def _changed_lines(buggy: str, fixed: str) -> list[str]:
    from difflib import unified_diff
    return [l for l in unified_diff(buggy.split("\n"), fixed.split("\n"),
                                   lineterm="", n=0)
            if (l.startswith("+") or l.startswith("-"))
            and not l.startswith(("+++", "---"))]


def _added_lines(buggy: str, fixed: str) -> list[str]:
    return [l[1:] for l in _changed_lines(buggy, fixed) if l.startswith("+")]


def _code_lines_only(lines: list[str]) -> list[str]:
    """Strip pure string/comment lines -- a change confined to those is not logic."""
    out = []
    for l in lines:
        s = l.strip()
        if not s or s.startswith("#") or s.startswith("//") or s.startswith("*"):
            continue
        if re.fullmatch(r"""['"`]?[\w\s.,:/'"\[\]{}(),\-]*['"`]?[,)]\s*""", s):
            # a bare string literal line
            if s.count('"') >= 2 or s.count("'") >= 2:
                continue
        out.append(l)
    return out


def _logic_changes(buggy: str, fixed: str) -> list[str]:
    return _code_lines_only(_added_lines(buggy, fixed))


def _is_documentation_only(buggy: str, fixed: str) -> bool:
    if not _changed_lines(buggy, fixed):
        return False
    # every changed line must be string-literal dominated
    for l in _changed_lines(buggy, fixed):
        s = l[1:].strip()
        if not s:
            continue
        quotes = s.count('"') + s.count("'") + s.count("`")
        letters = sum(ch.isalpha() for ch in s)
        if quotes < 2 or letters < 3:
            return False
        # a code-looking line with logic inside a string still counts as docs only if
        # there is no code outside the quotes
        outside = re.sub(r"""(['"]).*?\1""", "", s)
        if CALL_ADDED.search(outside) or BRANCH_ADDED.search(outside):
            return False
    return True


def _is_rename_only(buggy: str, fixed: str) -> bool:
    """Same token stream except identifier names (and their uses)."""
    from .grade import normalize_identifiers
    return normalize_identifiers(buggy) == normalize_identifiers(fixed)


def heuristic_category(task: dict[str, Any]) -> tuple[str, float, str]:
    """Return (category, confidence, reason) for one candidate task."""
    buggy = task.get("buggy", "") or ""
    fixed = task.get("reference_fix", "") or ""
    goal = task.get("goal", "") or ""
    fname = (task.get("file_name") or "").lower()

    if not buggy.strip() or not fixed.strip():
        return "ambiguous", 0.2, "empty snippet"

    if buggy.strip() == fixed.strip():
        return "ambiguous", 0.9, "no change"

    logic = _logic_changes(buggy, fixed)
    added = _added_lines(buggy, fixed)

    if _is_documentation_only(buggy, fixed):
        return "documentation", 0.85, "every changed line is string-literal text"

    if _is_rename_only(buggy, fixed):
        conf = 0.9 if GOAL_RENAME.search(goal) else 0.75
        return "refactor", conf, "token stream identical after identifier normalisation"

    added_defensive = sum(1 for l in logic if DEFENCE_ADDED.search(l))
    added_branches = sum(1 for l in added if BRANCH_ADDED.search(l))
    goal_defect = bool(GOAL_DEFECT.search(goal))
    goal_feature = bool(GOAL_FEATURE.search(goal))

    score = (min(added_defensive, 3) + min(added_branches, 2)) + (2 if goal_defect else 0)

    if score >= 3 and added_defensive >= 1:
        conf = min(0.95, 0.55 + 0.1 * score)
        return ("bug_fix", round(conf, 2),
                f"defensive/branching logic added ({added_defensive} guard-ish, "
                f"{added_branches} branch) and goal describes a defect")

    if goal_defect and added_branches >= 1 and logic:
        return ("bug_fix", 0.62,
                "goal describes a defect and the fix adds a control-flow branch")

    if fname.endswith((".json", ".yaml", ".yml", ".service", ".conf", ".toml")):
        return "configuration", 0.7, "config file"

    # a change that ONLY adds an option/argument line, with no new branch and no new
    # guard, is new capability rather than a repair of broken behaviour
    if logic and not added_branches and not added_defensive:
        if all(re.fullmatch(r"[\w.]+\s*=\s*[^,]+,?", l.strip()) or
               re.fullmatch(r"[\w.]+\s*,?", l.strip()) for l in logic):
            return ("feature_addition", 0.72,
                    "fix only adds option/argument lines -- no new branch or guard")

    if goal_feature and not goal_defect:
        if added and all(CALL_ADDED.match(l.strip()) for l in logic):
            return "feature_addition", 0.7, "fix only adds new call arguments"
        return "feature_addition", 0.6, "goal describes new capability, not a defect"

    if not logic:
        return "refactor", 0.6, "no logic lines changed"

    return "ambiguous", 0.3, "signals conflict"


def curate(tasks: Iterable[Task], llm=None, workers: int = 6) -> list[Task]:
    """Label every task; never overwrite a label that already exists."""
    todo = [t for t in tasks if not t.category or t.category == "unclassified"]
    if llm is not None and todo:
        from concurrent.futures import ThreadPoolExecutor

        def work(t: Task):
            cat, conf, reason = heuristic_category(t.to_dict())
            res = _llm_classify(llm, t, cat, conf, reason)
            if res is not None:
                return (t, res[0], res[1], res[2], "heuristic+llm")
            return (t, cat, conf, reason, "heuristic")

        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for t, cat, conf, reason, how in ex.map(work, todo):
                t.category, t.category_confidence = cat, conf
                t.category_reason, t.classifier = reason, how
    else:
        for t in todo:
            cat, conf, reason = heuristic_category(t.to_dict())
            t.category, t.category_confidence = cat, conf
            t.category_reason, t.classifier = reason, "heuristic"
    return list(tasks)


LLM_CLASSIFY_PROMPT = """You are labelling dataset items for a bug-fixing LLM benchmark.

Classify the code change below into exactly one category:
- bug_fix: the OLD code is defective (wrong behaviour, crash, wrong result, race, leak,
  missing validation of untrusted input) and the NEW code repairs that defect.
- feature_addition: new capability, new option, new call argument, new code path that was
  not broken before.
- refactor: behaviour-preserving restructure, rename, reordering, comment/docstring edits.
- documentation: only prose, descriptions or comments changed.
- configuration: settings, keys, paths, service units.
- ambiguous: cannot tell.

GOAL the developer was working towards:
{goal}

OLD (buggy) code:
{buggy}

NEW (reference) code:
{fixed}

Reply with JSON only: {{"category": "...", "confidence": 0.0-1.0, "reason": "<=25 words"}}"""


def _ask(llm, prompt: str) -> str:
    """Call whichever interface the runner exposes, tolerating an empty completion."""
    if hasattr(llm, "complete_messages"):
        r = llm.complete_messages([{"role": "user", "content": prompt}])
        return getattr(r, "text", "") or ""
    return getattr(llm, "complete", lambda p: type("R", (), {"text": ""})())(prompt).text


def _llm_classify(llm, task: Task, cat: str, conf: float, reason: str):
    from .prompts import build_classify_prompt
    from .grade import parse_json_block
    try:
        text = _ask(llm, build_classify_prompt(task))
        d = parse_json_block(text)
        if not d:
            return None
        c = (d.get("category") or "").strip()
        if c not in ("bug_fix", "feature_addition", "refactor", "documentation",
                     "configuration", "ambiguous"):
            return None
        try:
            cf = float(d.get("confidence", conf))
        except (TypeError, ValueError):
            cf = conf
        return (c, round(max(0.0, min(1.0, cf)), 2),
                (d.get("reason") or reason)[:200])
    except Exception:
        return None
