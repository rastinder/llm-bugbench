"""Shared fixtures + fake task builders."""
from __future__ import annotations

import json

BUGGY_ARITH = '''\
def average(values):
    """Return the arithmetic mean of a list of numbers."""
    total = 0.0
    for v in values:
        total += v
    return total / len(values)
'''

FIXED_ARITH = '''\
def average(values):
    """Return the arithmetic mean of a list of numbers, or None when empty."""
    if not values:
        return None
    total = 0.0
    for v in values:
        total += v
    return total / len(values)
'''

# a pair where buggy and fixed behave IDENTICALLY (renaming only) -- the negative
# control that proves the oracle validity gate discriminates.
BUGGY_RENAME = '''\
def calc_price(unit_price, qty):
    base = unit_price * qty
    tax = base * 0.13
    return base + tax
'''

FIXED_RENAME = '''\
def price_with_tax(unit_price, quantity):
    base = unit_price * quantity
    tax = base * 0.13
    return base + tax
'''

# a genuine bug: only the first regex match is inspected, so a negated promise slips through
BUGGY_REGEX = '''\
import re

BANNED = re.compile(r"guaranteed sales")

def is_safe(text):
    m = BANNED.search(text)
    return m is None
'''

FIXED_REGEX = '''\
import re

BANNED = re.compile(r"guaranteed sales")

def is_safe(text):
    for m in BANNED.finditer(text):
        if not _negated_before(text, m.start()):
            return False
    return True


def _negated_before(text, index):
    window = text[max(0, index - 40):index].lower()
    return "not " in window or "no " in window
'''


def make_task(tmp_path=None, task_id="B0001", buggy=BUGGY_ARITH, fixed=FIXED_ARITH,
              goal="average() blows up on an empty list", **over):
    t = {
        "task_id": task_id,
        "language": "python",
        "file_name": "mod.py",
        "project": "demo",
        "category": "bug_fix",
        "category_confidence": 1.0,
        "category_reason": "test fixture",
        "classifier": "test",
        "goal": goal,
        "buggy": buggy,
        "reference_fix": fixed,
        "oracle": {"kind": "none"},
        "derived_from": "test",
        "source_session": "ses_test",
        "source_title": "test",
        "is_test_file": False,
        "goal_had_alternatives": False,
    }
    t.update(over)
    if tmp_path is not None:
        (tmp_path / f"{task_id}.json").write_text(json.dumps(t))
    from bugbench.models import Task
    return Task.from_dict(t)


def write_task_file(tmp_path, tasks):
    from bugbench.models import Task
    p = tmp_path / "tasks.jsonl"
    with p.open("w") as f:
        for t in tasks:
            f.write(json.dumps(t.to_dict() if isinstance(t, Task) else t) + "\n")
    return p
