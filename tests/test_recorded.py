"""Tests for recorded task extraction and test discrimination."""
from __future__ import annotations

import tempfile
from pathlib import Path
from bugbench.recorded import (
    _test_candidates, _added_test_ids, content_at, discover_root,
    RecordedTask, verify
)
from bugbench.timeline import Timeline, Anchor, Edit


def test_test_candidates():
    cands = _test_candidates("src/app.py")
    assert "src/test_app.py" in cands
    assert "tests/test_app.py" in cands
    assert "test/test_app.py" in cands

    # A test file produces no candidate
    assert _test_candidates("tests/test_foo.py") == []


def test_added_test_ids():
    before = """
class MyTest:
    def test_one(self):
        assert True
"""
    after = """
class MyTest:
    def test_one(self):
        assert True
    def test_two(self):
        assert 1 == 1

def test_standalone():
    assert True
"""
    added = _added_test_ids(before, after)
    assert "MyTest::test_two" in added
    assert "test_standalone" in added
    assert "MyTest::test_one" not in added
    assert "test_two" not in added  # Not duplicated as top-level method


def test_content_at():
    tl = Timeline(file_path="foo.py")
    tl.anchors.append(Anchor(100, "write", "v = 1\n"))
    tl.edits.append(Edit(200, "v = 1", "v = 2", "s1", "foo.py"))
    tl.edits.append(Edit(300, "v = 2", "v = 3", "s1", "foo.py"))

    assert content_at(tl, 150) == "v = 1\n"
    assert content_at(tl, 250) == "v = 2\n"
    assert content_at(tl, 350) == "v = 3\n"
