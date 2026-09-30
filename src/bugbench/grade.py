"""Grading: two stages scored independently.

Council constraints implemented here:
  * no single arbitrary weighted score hides a regression -> every sub-signal is returned
  * diff similarity is explicitly labelled `patch_reproduction`, never "correctness"
  * the oracle term only appears for tasks whose oracle passed the validity gate
"""
from __future__ import annotations

import json
import re
from difflib import unified_diff
from typing import Any

DIAGNOSE_WEIGHTS = {"location_match": 0.5, "judge_agreement": 0.3,
                    "category_match": 0.2}

CATEGORIES = {"boundary", "null_handling", "logic_error", "race_condition",
              "resource_leak", "validation", "off_by_one", "concurrency",
              "error_handling", "performance", "other", "none"}

_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\n(.*?)(?:```|\Z)", re.S)


# ---------------------------------------------------------------- parsing
def parse_json_block(text: str) -> dict | None:
    """Extract one JSON object from a model reply, fenced or not."""
    if not text:
        return None
    m = _FENCE.search(text)
    if m:
        text = m.group(1)
    depth, start = 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    start = None
    # last resort: whole-string parse
    try:
        v = json.loads(text.strip())
        return v if isinstance(v, dict) else None
    except Exception:
        return None


def parse_diagnosis(text: str) -> dict | None:
    """Parse a stage-1 answer, tolerating the key aliases models actually emit."""
    d = parse_json_block(text)
    if not d:
        return None
    alias = {
        "root_cause": ("root_cause", "defect", "cause", "reason", "explanation",
                       "bug", "issue", "problem", "root cause"),
        "fault_location": ("fault_location", "location", "line", "faulty_line",
                           "buggy_line", "offending_line", "line_number"),
        "bug_category": ("bug_category", "category", "type", "bug_type", "class"),
        "confidence": ("confidence", "conf"),
    }
    out = {}
    low = {str(k).lower().replace(" ", "_"): v for k, v in d.items()}
    for canon, names in alias.items():
        for n in names:
            if n in low and low[n] not in (None, ""):
                out[canon] = low[n]
                break
    return out or None


def extract_code(text: str) -> str:
    """Pull the repaired snippet out of a model reply."""
    if not text:
        return ""
    blocks = _FENCE.findall(text)
    if blocks:
        return max(blocks, key=len).strip("\n")
    return text.strip()


# ---------------------------------------------------------------- similarity
_TOK = re.compile(r"[A-Za-z_]\w*|\d+\.?\d*|\S")


def tokenize(s: str) -> list[str]:
    return _TOK.findall(s or "")


def normalize_identifiers(s: str) -> str:
    """Replace every identifier with a positional placeholder.

    Two snippets that differ only in identifier names collapse to the same string, which
    is how the refactor detector recognises a rename.
    """
    mapping: dict[str, str] = {}
    out = []
    for t in tokenize(s):
        if t.isidentifier() and not t.isdigit():
            if t not in mapping:
                mapping[t] = f"ID{len(mapping)}"
            out.append(mapping[t])
        else:
            out.append(t)
    return " ".join(out)


def _lcs_len(a: list[str], b: list[str]) -> int:
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b):
            cur.append(prev[j] + 1 if x == y else max(prev[j + 1], cur[j]))
        prev = cur
    return prev[-1]


def diff_similarity(candidate: str, reference: str) -> float:
    """Token-sequence similarity to the developer's historical fix.

    This measures *patch reproduction*, not correctness -- reported under that name so a
    leaderboard reader cannot mistake it for a correctness metric.
    """
    a, b = tokenize(candidate), tokenize(reference)
    if not a or not b:
        return 0.0
    lcs = _lcs_len(a, b)
    return round(2.0 * lcs / (len(a) + len(b)), 4)


def changed_line_numbers(buggy: str, fixed: str) -> list[int]:
    """1-based line numbers in `buggy` that the historical fix touched."""
    nums, offset = [], 0
    for line in unified_diff(buggy.split("\n"), fixed.split("\n"),
                             lineterm="", n=0):
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("-"):
            offset += 1
            nums.append(offset)
        elif line.startswith("+"):
            pass
        elif line.startswith(" "):
            offset += 1
    return sorted(set(nums))


def _quote_matches(candidate_line: str, buggy: str, fixed: str,
                    changed: list[int]) -> bool:
    c = candidate_line.strip()
    if not c:
        return False
    blines = buggy.split("\n")
    flines = fixed.split("\n")
    # exact line match in either side
    for i, l in enumerate(blines, 1):
        if l.strip() == c and i in changed:
            return True
    for l in flines:
        if l.strip() == c:
            return True
    # quote is a prefix/substring of a changed line (models often drop the indent)
    for i, l in enumerate(blines, 1):
        if i in changed and (c in l or l.strip() in c):
            return True
    return False


def infer_category(task: Any) -> str:
    """Cheap deterministic category guess from the reference diff, for judge agreement."""
    b, f = getattr(task, "buggy", ""), getattr(task, "reference_fix", "")
    added = "".join(l[1:] for l in unified_diff(b.split("\n"), f.split("\n"),
                                               lineterm="", n=0)
                    if l.startswith("+"))
    if re.search(r"is\s+None|not\s+\w+\s*$|if\s+not\s+", added):
        return "null_handling"
    if re.search(r"if\s+len\(|not\s+\w+\s*:|==\s*\[\]|==\s*\{\}|empty", added):
        return "boundary"
    if re.search(r"except|try:", added):
        return "error_handling"
    if re.search(r"finally|close\(\)|cancel\(|dispose|shutdown", added):
        return "resource_leak"
    if re.search(r"await\s|\basync\b|thread|Lock|queue", added):
        return "concurrency"
    if re.search(r"range\(|max\(0|min\(", added):
        return "off_by_one"
    if re.search(r"if\s+.*:", added):
        return "logic_error"
    return "other"


def _category_match(pred: Any, task: Any, judge: float) -> float:
    p = (str(pred or "")).strip().lower()
    if not p or p == "none":
        return 0.0
    ref = infer_category(task)
    if p == ref:
        return 1.0
    return 0.5 if judge >= 0.6 else 0.0


def score_diagnose(task: Any, pred: dict | None) -> dict:
    """Grade a stage-1 answer. Components are returned separately."""
    pred = pred or {}
    changed = changed_line_numbers(getattr(task, "buggy", ""),
                                  getattr(task, "reference_fix", ""))
    loc = str(pred.get("fault_location") or "")
    if loc and _quote_matches(loc, getattr(task, "buggy", ""),
                              getattr(task, "reference_fix", ""), changed):
        location = 1.0
    elif loc and re.search(r"\bline\s*(\d+)", loc, re.I):
        n = int(re.search(r"\bline\s*(\d+)", loc, re.I).group(1))
        location = 1.0 if n in changed else 0.0
    else:
        location = 0.0

    try:
        judge = max(0.0, min(1.0, float(pred.get("judge_agreement", 0.0))))
    except (TypeError, ValueError):
        judge = 0.0

    cat = _category_match(pred.get("bug_category"), task, judge)
    total = (DIAGNOSE_WEIGHTS["location_match"] * location
             + DIAGNOSE_WEIGHTS["judge_agreement"] * judge
             + DIAGNOSE_WEIGHTS["category_match"] * cat)
    return {
        "total": round(total, 4),
        "location_match": round(location, 4),
        "judge_agreement": round(judge, 4),
        "category_match": round(cat, 4),
        "expected_category": infer_category(task),
        "changed_lines": changed,
    }


UNCHANGED_THRESHOLD = 0.97


def is_unchanged(candidate: str, buggy: str) -> bool:
    """True when the 'fix' is byte-equivalent to the input.

    This is checked BEFORE the LLM judge and cannot be overridden by it. Measured on the
    first benchmark run: 22 rows returned the buggy snippet unchanged and the judge still
    awarded `fixed: true` on 15 of them, handing out 12.3 points of credit for doing
    nothing. An LLM must not be able to argue its way past "you returned the input".
    """
    from difflib import SequenceMatcher
    a, b = re.sub(r"\s+", " ", (candidate or "").strip()), \
        re.sub(r"\s+", " ", (buggy or "").strip())
    if len(b) < 40 or not a:
        return False
    return SequenceMatcher(None, a, b).ratio() >= UNCHANGED_THRESHOLD


def score_repair(task: Any, candidate_text: str, judge_agreement: float,
                 oracle=None) -> dict:
    """Grade a stage-2 answer.

    When the task has a validated oracle the score includes it; otherwise the oracle term
    is dropped and re-normalised, and the result is tagged so the executable and
    non-executable subsets are never silently averaged together.
    """
    cand = extract_code(candidate_text) if "```" in (candidate_text or "") \
        else (candidate_text or "").strip()

    if is_unchanged(cand, getattr(task, "buggy", "")):
        return {
            "total": 0.0,
            "diff_similarity": 0.0,
            "patch_reproduction": 0.0,
            "judge_agreement": 0.0,
            "oracle_score": 0.0,
            "oracle": "none",
            "combined_weights": {"judge": 0.0, "note": "returned the buggy input unchanged"},
            "penalty": "unchanged_input",
        }

    sim = diff_similarity(cand, getattr(task, "reference_fix", ""))
    judge = max(0.0, min(1.0, float(judge_agreement or 0.0)))

    o = 0.0
    has_oracle = False
    if oracle is not None and getattr(oracle, "kind", "none") == "exec_diff":
        o = float(oracle.score(cand))
        has_oracle = True

    if has_oracle:
        total = 0.5 * judge + 0.3 * sim + 0.2 * o
    else:
        total = 0.5 * judge + 0.5 * sim

    return {
        "total": round(total, 4),
        "diff_similarity": sim,
        "patch_reproduction": sim,
        "judge_agreement": round(judge, 4),
        "oracle_score": round(o, 4),
        "oracle": "exec_diff" if has_oracle else "none",
        "combined_weights": ({"judge": 0.5, "patch_reproduction": 0.3,
                              "oracle": 0.2} if has_oracle
                             else {"judge": 0.5, "patch_reproduction": 0.5}),
    }
