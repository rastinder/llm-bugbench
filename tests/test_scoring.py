"""Tests for per-bug scoring and the oracle-validation gate."""
from __future__ import annotations

import pytest

from bugbench.scoring import (
    BugScore, FileScore, GateResult, score_from_rows, validate_oracle,
)


def rows(*pairs):
    return [{"nodeid": n, "outcome": o, "exc_type": None} for n, o in pairs]


class TestPerBugScoring:
    def test_full_credit_only_on_pass(self):
        assert BugScore("b1", "t.py::x", passed=True).value == 1.0
        assert BugScore("b1", "t.py::x", passed=False, errored=True).value == 0.0

    def test_an_errored_node_is_not_a_pass(self):
        """A collection/transport error must never be credited as a fix."""
        assert BugScore("b1", "t.py::x", passed=True, errored=True).value == 0.0

    def test_a_multi_bug_file_scores_fractionally(self):
        fs = score_from_rows("t1", "repo", "python", rows(
            ("t.py::a", "passed"), ("t.py::b", "failed"),
            ("t.py::c", "passed"), ("t.py::d", "passed"),
        ), {"b1": "t.py::a", "b2": "t.py::b", "b3": "t.py::c", "b4": "t.py::d"})

        assert fs.bugs_fixed == 3
        assert fs.per_bug_score == 0.75
        assert not fs.file_clear

    def test_file_clear_requires_every_bug(self):
        ok = score_from_rows("t", "r", "python", rows(("t.py::a", "passed")), {"b1": "t.py::a"})
        no = score_from_rows("t", "r", "python",
                             rows(("t.py::a", "passed"), ("t.py::b", "failed")),
                             {"b1": "t.py::a", "b2": "t.py::b"})

        assert ok.file_clear
        assert not no.file_clear

    def test_missing_test_node_scores_zero_and_is_kept(self):
        """Dropping an unmatched bug would inflate every rate it appears in."""
        fs = score_from_rows("t", "r", "python", rows(("t.py::a", "passed")),
                             {"b1": "t.py::a", "b2": "t.py::never_ran"})

        assert fs.bugs_total == 2
        assert fs.bugs_fixed == 1

    def test_one_bug_file_is_not_scored_as_a_full_file(self):
        """Guards the 'any test passed' gaming vector: one bug cannot equal five.

        Both files clear the same single test node, so both report bugs_fixed == 1, but
        their per-bug scores differ because the denominators differ. Aggregation is over
        bugs, never over files."""
        one = score_from_rows("t", "r", "python", rows(("t.py::a", "passed")), {"b1": "t.py::a"})
        five = score_from_rows("t", "r", "python",
                               rows(("t.py::a", "passed"), ("t.py::b", "failed")),
                               {"b1": "t.py::a", "b2": "t.py::b"})

        assert five.per_bug_score == 0.5
        # Two bugs cleared, but they are not a whole file: per-bug aggregation means a
        # partially-repaired large file never scores like a fully-repaired small one.
        assert one.per_bug_score > five.per_bug_score

    def test_empty_bug_map_is_zero_not_a_pass(self):
        assert score_from_rows("t", "r", "python", [], {}).per_bug_score == 0.0


class TestOracleGate:
    def _good(self):
        return [score_from_rows(f"g{i}", "r", "python",
                                rows(("t.py::a", "passed"), ("t.py::b", "passed")),
                                {"b1": "t.py::a", "b2": "t.py::b"}) for i in range(3)]

    def _bad(self):
        return [score_from_rows(f"x{i}", "r", "python",
                                rows(("t.py::a", "failed"), ("t.py::b", "failed")),
                                {"b1": "t.py::a", "b2": "t.py::b"}) for i in range(3)]

    def test_clean_run_passes(self):
        res = validate_oracle(self._good(), self._bad())

        assert res.passed, res.failures
        assert res.detail["good_mean"] == 1.0
        assert res.detail["bad_mean"] == 0.0

    def test_known_good_failing_a_bug_fails_the_gate(self):
        good = self._good()
        good[1] = score_from_rows("g1", "r", "python",
                                  rows(("t.py::a", "failed"), ("t.py::b", "passed")),
                                  {"b1": "t.py::a", "b2": "t.py::b"})
        res = validate_oracle(good, self._bad())

        assert not res.passed
        assert any("known-good" in f for f in res.failures)

    def test_known_bad_scoring_fails_the_gate(self):
        # Built fresh, not mutated: BugScore.value is derived, so flipping `passed`
        # after construction would leave every aggregate unchanged.
        bad = self._bad()
        bad[0] = score_from_rows("x0", "r", "python",
                                 rows(("t.py::a", "passed"), ("t.py::b", "failed")),
                                 {"b1": "t.py::a", "b2": "t.py::b"})
        res = validate_oracle(self._good(), bad)

        assert not res.passed
        assert any("known-bad" in f for f in res.failures)

    def test_non_monotone_scoring_fails_the_gate(self):
        """A grader that ranks garbage above the true fix invalidates every result.

        Constructed as a genuine inversion: the "known good" file has one unfixed bug
        (0.5), while a known-bad file clears both of its own (1.0)."""
        good = [FileScore("g", "r", "python",
                          [BugScore("b1", "n", True), BugScore("b2", "n", False)])]
        bad = [FileScore("x", "r", "python",
                         [BugScore("b1", "n", True), BugScore("b2", "n", True)])]
        res = validate_oracle(good, bad)

        assert not res.passed
        assert any("monotone" in f for f in res.failures)

    def test_gate_reports_counts_for_the_record(self):
        res = validate_oracle(self._good(), self._bad())

        assert res.detail["known_good_files"] == 3
        assert res.detail["known_bad_files"] == 3
