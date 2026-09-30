"""Prompts for the two benchmark stages.

Hard invariant enforced by a test: `reference_fix` never appears in any prompt.
"""
from __future__ import annotations

import json
from typing import Any

DIAGNOSE_SYSTEM = """You are a meticulous code reviewer. You find the SINGLE most likely \
defect in a short code snippet and name its exact location.

You are given a GOAL (what the developer was trying to achieve) and a snippet of CODE as \
it currently exists. Identify the defect that prevents the goal from being met.

Answer with a single JSON object and nothing else:
{
  "root_cause": "<=30 words: the mechanism by which the code fails>",
  "fault_location": "<exact quoted line from the CODE that must change>",
  "bug_category": "one of: boundary, null_handling, logic_error, race_condition, resource_leak, validation, off_by_one, concurrency, error_handling, performance, other",
  "confidence": 0.0-1.0
}

Rules:
- Quote `fault_location` verbatim from the CODE block. It must be a real line.
- Do NOT rewrite the code and do NOT propose a fix; only diagnose.
- If you cannot find a defect, return root_cause "none", fault_location "" and confidence 0.0.
- Output raw JSON only. No markdown fence, no commentary."""

REPAIR_SYSTEM = """You are a meticulous engineer fixing a bug.

You are given a GOAL (what the developer was trying to achieve) and a snippet of CODE that \
fails to achieve it. Return the corrected snippet.

Rules:
- Return the COMPLETE corrected snippet, not a diff and not a fragment.
- Change only what is necessary to meet the goal. Do not restyle, rename things you were not
  asked to rename, reorder unrelated statements, or add comments.
- Preserve the existing signature, naming, indentation style and structure.
- Add a guard or a branch only if that is what the goal requires.
- Put the answer in ONE fenced block, tagged with the language.
- Add a short note (2 lines max) after the block stating the root cause in one sentence.

Format:
```<language>
<corrected snippet>
```
Root cause: <one sentence>"""

CLASSIFY_SYSTEM = """You label dataset items for a bug-fixing benchmark. Reply with JSON only."""


def _code_block(task: Any, code: str) -> str:
    lang = getattr(task, "language", "") or "text"
    return f"```{lang}\n{code}\n```"


def build_diagnose_prompt(task: Any) -> str:
    return (
        f"GOAL:\n{getattr(task, 'goal', '')}\n\n"
        f"FILE: {getattr(task, 'file_name', '')}\n\n"
        f"CODE:\n{_code_block(task, getattr(task, 'buggy', ''))}\n\n"
        "Find the defect. Reply with JSON only."
    )


def build_repair_prompt(task: Any) -> str:
    return (
        f"GOAL:\n{getattr(task, 'goal', '')}\n\n"
        f"FILE: {getattr(task, 'file_name', '')}\n\n"
        f"CURRENT CODE:\n{_code_block(task, getattr(task, 'buggy', ''))}\n\n"
        "Return the corrected snippet in one fenced block."
    )


def build_classify_prompt(task: Any) -> str:
    from .curate import LLM_CLASSIFY_PROMPT
    return LLM_CLASSIFY_PROMPT.format(
        goal=getattr(task, "goal", "")[:1200],
        buggy=getattr(task, "buggy", "")[:2500],
        fixed=getattr(task, "reference_fix", "")[:2500],
    )


DIAGNOSE_JUDGE_SYSTEM = """You are an exacting grader for CODE DIAGNOSIS.

You are shown a GOAL, a snippet of code that contains a bug, and a CANDIDATE DIAGNOSIS \
produced by another engineer (their stated root cause, the line they blame, and a category).

Decide whether the candidate identified the SAME defect that the code actually has.

Score each dimension 0.0-1.0 and require the reasoning to match the MECHANISM, not the \
wording:
- mechanism: does their explanation describe how the code actually fails?
- location: is the line they blame genuinely part of the problem (not merely adjacent)?

A diagnosis that is vague, blames an unrelated line, or describes a different bug scores \
low even if it is confidently worded. A diagnosis that names the right mechanism in \
different words scores high.

Reply with JSON only:
{"mechanism": 0.0-1.0, "location_ok": true|false, "reason": "<=20 words"}"""

DIAGNOSE_JUDGE_PROMPT = """GOAL:
{goal}

CODE (contains the bug):
```{lang}
{buggy}
```

CANDIDATE DIAGNOSIS:
{diagnosis}

Grade the mechanism match and whether the blamed line is genuinely part of the defect.
Reply with JSON only."""


def build_diagnose_judge_prompt(task: Any, diagnosis: dict) -> str:
    return DIAGNOSE_JUDGE_PROMPT.format(
        goal=getattr(task, "goal", ""),
        lang=getattr(task, "language", "") or "text",
        buggy=getattr(task, "buggy", ""),
        diagnosis=json.dumps(diagnosis, indent=1)[:1200],
    )


def build_diagnose_judge_messages(task: Any, diagnosis: dict) -> list[dict]:
    return [{"role": "system", "content": DIAGNOSE_JUDGE_SYSTEM},
            {"role": "user",
             "content": build_diagnose_judge_prompt(task, diagnosis)}]


JUDGE_SYSTEM = """You are an exacting, deliberately sceptical grader.

You are shown a GOAL, a snippet of CODE that is KNOWN TO CONTAIN A BUG, and a CANDIDATE \
repaired version of that same snippet. Decide whether the candidate actually fixes the \
defect and achieves the goal.

Judge ONLY correctness. Explicitly IGNORE:
- formatting, whitespace and indentation style
- renamed identifiers, reordered imports, added type hints
- added comments or docstrings
- any difference in wording from some other version you might imagine

Accept a DIFFERENT fix from the one you would have chosen, as long as it is correct and \
complete. Reject cosmetic rewrites that leave the defect in place.

Reply with JSON only:
{"fixed": true|false, "reason": "<=25 words", "confidence": 0.0-1.0}"""

JUDGE_REFERENCE_SYSTEM = """You are an exacting, deliberately sceptical grader with an \
additional signal: the developer's own historical fix.

You are shown a GOAL, buggy CODE, a CANDIDATE fix, and the DEVELOPER'S OWN historical fix.
Score how well the candidate achieves the same behaviour as the developer's fix.

Ignore formatting, renaming, comments and wording. A candidate that is behaviourally \
equivalent to the developer's fix should score high even if the code differs structurally. \
A candidate that changes more than needed, or leaves the defect in place, scores low.

Reply with JSON only:
{"agreement": 0.0-1.0, "reason": "<=25 words>"}"""


def build_judge_prompt(task: Any, candidate: str, reference: str | None = None,
                       reference_aware: bool = False) -> str:
    parts = [
        f"GOAL:\n{getattr(task, 'goal', '')}",
        f"BUGGY CODE:\n{_code_block(task, getattr(task, 'buggy', ''))}",
        f"CANDIDATE FIX:\n{_code_block(task, candidate)}",
    ]
    if reference_aware and reference:
        parts.append(
            f"DEVELOPER'S OWN HISTORICAL FIX:\n{_code_block(task, reference)}")
    parts.append("Grade the candidate. Reply with JSON only.")
    return "\n\n".join(parts)


def build_diagnose_messages(task: Any) -> list[dict]:
    return [{"role": "system", "content": DIAGNOSE_SYSTEM},
            {"role": "user", "content": build_diagnose_prompt(task)}]


def build_repair_messages(task: Any) -> list[dict]:
    return [{"role": "system", "content": REPAIR_SYSTEM},
            {"role": "user", "content": build_repair_prompt(task)}]


def build_judge_messages(task: Any, candidate: str, reference: str | None = None,
                         reference_aware: bool = False) -> list[dict]:
    system = JUDGE_REFERENCE_SYSTEM if reference_aware else JUDGE_SYSTEM
    return [
        {"role": "system", "content": system},
        {"role": "user",
         "content": build_judge_prompt(task, candidate, reference, reference_aware)},
    ]


def build_classify_messages(task: Any) -> list[dict]:
    return [{"role": "system", "content": CLASSIFY_SYSTEM},
            {"role": "user", "content": build_classify_prompt(task)}]


def dump(obj) -> str:
    return json.dumps(obj, indent=2)


# ---------------------------------------------------------------- stage 2 (layer 2)
# The layer-2 ablation: the model is TOLD the defect and only has to repair it. This
# isolates repair skill from diagnosis skill -- a model can be a poor diagnostician and a
# strong repairer, and the leaderboard already shows exactly that split.
REPAIR_GIVEN_SYSTEM = """You are a meticulous engineer fixing a known bug.

You are given a BUG REPORT (written by the engineer who found it) and a snippet of CODE that contains that bug. Fix the code.

Rules:
- Return the COMPLETE corrected snippet, not a diff and not a fragment.
- Change only what the bug report requires. Do not restyle, rename things you were not
  asked to rename, reorder unrelated statements, or add comments.
- Preserve the existing signature, naming, indentation style and structure.
- Put the answer in ONE fenced block, tagged with the language.

Format:
```{lang}
<corrected snippet>
```"""


def build_repair_given_messages(task: Any) -> list[dict]:
    lang = getattr(task, "language", "") or "text"
    return [
        {"role": "system", "content": REPAIR_GIVEN_SYSTEM.format(lang=lang)},
        {"role": "user", "content": (
            f"BUG REPORT:\n{getattr(task, 'goal', '')}\n\n"
            f"FILE: {getattr(task, 'file_name', '')}\n\n"
            f"CODE:\n{_code_block(task, getattr(task, 'buggy', ''))}\n\n"
            "Return the corrected snippet in one fenced block.")},
    ]
