"""Tests for combining mutants into genuinely harder multi-bug tasks.

The reason this module exists: three frontier models scored 1.00 on a panel of single-mutant
files, because a single-mutant file is a one-token diff. These tests hold the combiner to
the property that actually creates difficulty -- interacting defects in one file -- and,
just as importantly, refuse combinations whose per-bug verdicts cannot be separated.
"""
from __future__ import annotations

import pytest

from bugbench.combine import (
    CombinedBug, CombinedTask, apply_all, build_candidates,
)
from bugbench.mutants import Mutant, apply_mutation

SOURCE = '''\
def encode(x, a, b):
    """Encode a value into a bounded buffer."""
    if x > a:
        x = a
    if x < b:
        x = b
    return [x]


def decode(buf, limit):
    """Decode a buffer, dropping the oldest when full."""
    out = list(buf)
    while len(out) > limit:
        out.pop(0)
    return out
'''


def bug(bid, line, op, node, difficulty="moderate"):
    return {"bug_id": bid, "line": line, "operator": op, "symbol": "f",
            "test_node": node, "difficulty": difficulty,
            "green_tests": ["tests/test_x.py"],
            "codebase": "repo", "module": "pkg/mod.py"}


def mut(bid, line, original, mutated):
    return Mutant(bid, "m.py", "f", "gt", line, original, mutated)


class TestApplyAll:
    def test_every_mutant_is_applied(self):
        ms = [mut("a", 3, "    if x > a:", "    if x < a:"),
              mut("b", 5, "    if x < b:", "    if x > b:")]
        out = apply_all(SOURCE, ms)
        assert "if x < a:" in out and "if x > b:" in out

    def test_the_source_is_not_corrupted(self):
        ms = [mut("a", 3, "    if x > a:", "    if x < a:")]
        out = apply_all(SOURCE, ms)
        assert "def decode(buf, limit):" in out
        assert "out.pop(0)" in out

    def test_line_order_does_not_matter(self):
        a = mut("a", 3, "    if x > a:", "    if x < a:")
        b = mut("b", 5, "    if x < b:", "    if x > b:")
        assert apply_all(SOURCE, [a, b]) == apply_all(SOURCE, [b, a])


class TestProximity:
    def test_nearby_bugs_are_clustered(self):
        pool = [bug("a", 10, "gt", "t::x"), bug("b", 20, "gt", "t::y")]
        tasks = build_candidates(pool, target_bugs=2, max_gap=40)
        assert any(len(t.bugs) == 2 for t in tasks)

    def test_distant_bugs_are_not_combined(self):
        """Two defects 400 lines apart are two easy tasks stapled together, not one hard
        one -- so they must not merge."""
        pool = [bug("a", 10, "gt", "t::x"), bug("b", 410, "gt", "t::y")]
        tasks = build_candidates(pool, target_bugs=2, max_gap=40)
        assert all(len(t.bugs) < 2 for t in tasks)


class TestDistinctTests:
    def test_bugs_sharing_one_test_node_are_not_combined(self):
        """A single fix could satisfy both 'bugs', so per-bug credit would be unsound."""
        pool = [bug("a", 10, "gt", "t::same"), bug("b", 12, "lt", "t::same")]
        tasks = build_candidates(pool, target_bugs=2, max_gap=40)
        assert all(len(t.bugs) < 2 for t in tasks)

    def test_distinct_test_nodes_are_allowed(self):
        pool = [bug("a", 10, "gt", "t::x"), bug("b", 12, "lt", "t::y")]
        tasks = build_candidates(pool, target_bugs=2, max_gap=40)
        assert any(len(t.bugs) == 2 for t in tasks)


class TestDifficulty:
    def test_hardest_bug_sets_the_task_difficulty(self):
        t = CombinedTask("t", "r", "m.py", "python", bugs=[
            CombinedBug("a", 1, "gt", "moderate", "f", "t::x"),
            CombinedBug("b", 2, "aug_add", "hard", "f", "t::y")])
        assert t.as_dict()["difficulty"] == "hard"

    def test_two_trivial_bugs_are_still_trivial(self):
        """Stacking easy defects does not manufacture difficulty, only length."""
        t = CombinedTask("t", "r", "m.py", "python", bugs=[
            CombinedBug("a", 1, "zero", "trivial", "f", "t::x"),
            CombinedBug("b", 2, "true", "trivial", "f", "t::y")])
        assert t.as_dict()["difficulty"] == "trivial"

    def test_three_moderate_bugs_are_hard(self):
        t = CombinedTask("t", "r", "m.py", "python", bugs=[
            CombinedBug(f"b{i}", i, "gt", "moderate", "f", f"t::x{i}") for i in range(3)])
        assert t.as_dict()["difficulty"] == "hard"


class TestBugMap:
    def test_bug_map_is_one_to_one(self):
        t = CombinedTask("t", "r", "m.py", "python", bugs=[
            CombinedBug("a", 1, "gt", "moderate", "f", "t::x"),
            CombinedBug("b", 2, "lt", "moderate", "f", "t::y")])
        assert t.bug_map == {"a": "t::x", "b": "t::y"}

    def test_span_measures_how_clustered_the_defects_are(self):
        t = CombinedTask("t", "r", "m.py", "python", bugs=[
            CombinedBug("a", 10, "gt", "moderate", "f", "t::x"),
            CombinedBug("b", 90, "lt", "moderate", "f", "t::y")])
        assert t.span == 80


class TestShape:
    def test_task_ids_are_unique_across_candidates(self):
        pool = ([bug(f"a{i}", 10 + i, "gt", f"t::x{i}") for i in range(4)] +
                [bug(f"b{i}", 60 + i, "lt", f"t::y{i}") for i in range(4)])
        tasks = build_candidates(pool, target_bugs=3, max_gap=40)
        ids = [t.task_id for t in tasks]
        assert len(ids) == len(set(ids))

    def test_selection_is_deterministic(self):
        pool = [bug(f"a{i}", 10 + i, "gt", f"t::x{i}") for i in range(5)]
        a = [t.task_id for t in build_candidates(pool, target_bugs=3, max_gap=40)]
        b = [t.task_id for t in build_candidates(pool, target_bugs=3, max_gap=40)]
        assert a == b

    def test_candidates_carry_their_test_files(self):
        pool = [bug("a", 10, "gt", "t::x"), bug("b", 12, "lt", "t::y")]
        for t in build_candidates(pool, target_bugs=2, max_gap=40):
            assert t.green_tests == ["tests/test_x.py"]

    def test_empty_pool_yields_nothing(self):
        assert build_candidates([], target_bugs=3) == []
