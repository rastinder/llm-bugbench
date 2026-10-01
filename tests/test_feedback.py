"""Feedback-channel tests: the model must never see hidden-test source.

The benchmark's whole validity rests on this: if a model can read the assertion that
judges it, every score measures recall of our test file instead of repair ability.

These tests assert the *negative* -- that specific secret strings do NOT appear in
what the model is shown -- because a leak is invisible to positive assertions.
"""
from __future__ import annotations

import pytest

from bugbench.feedback import (
    FeedbackChannel,
    collect_pytest_rows,
    collect_node_rows,
    render_feedback,
)

# A hidden test whose every distinctive string is a canary. If any of these reach the
# model, the channel leaks.
SECRET_SOURCE = '''
import pytest

PREMIUM_ENTITLEMENT = "CANARY_7f3a91"
LIFETIME_PRICE_CENTS = 49900


def test_expired_token_is_rejected():
    assert classify("expired") == "unauthorised", "expired token must not authenticate"


def test_active_token_is_accepted():
    assert classify("active") == "ok"
'''


@pytest.fixture
def pytest_project(tmp_path):
    """A project whose hidden test leaks several canaries if it is ever echoed."""
    tdir = tmp_path / "test"
    tdir.mkdir()
    (tdir / "test_hidden.py").write_text(SECRET_SOURCE)
    (tmp_path / "code").mkdir()
    (tmp_path / "code" / "auth.py").write_text(
        "def classify(token):\n    return 'ok'\n"
    )
    return tmp_path


def _canaries():
    return [
        "CANARY_7f3a91",
        "PREMIUM_ENTITLEMENT",
        "LIFETIME_PRICE_CENTS",
        "49900",
        "expired token must not authenticate",
        "def test_expired_token_is_rejected",
    ]


class TestNoLeak:
    def test_rendered_feedback_contains_no_test_source(self, pytest_project):
        ch = FeedbackChannel(pytest_project, python="python3")
        rows = ch.run("test/test_hidden.py")
        shown = render_feedback(rows)

        for canary in _canaries():
            assert canary not in shown, f"LEAK: {canary!r} reached the model"

    def test_feedback_reports_every_failure_at_once(self, pytest_project):
        """All failures in one report -- the approved triage behaviour."""
        ch = FeedbackChannel(pytest_project, python="python3")
        rows = ch.run("test/test_hidden.py")
        shown = render_feedback(rows)

        assert "test_expired_token_is_rejected" in shown
        assert "test_active_token_is_accepted" in shown
        assert shown.count("FAILED") == 2

    def test_no_passed_tests_are_announced(self, pytest_project):
        """Only failures are surfaced; a green test is not useful triage signal and
        its existence leaks that a case exists at all."""
        ch = FeedbackChannel(pytest_project, python="python3")
        shown = render_feedback(ch.run("test/test_hidden.py"))

        assert "PASSED" not in shown


class TestDeterminism:
    def test_same_input_yields_byte_identical_feedback(self, pytest_project):
        ch = FeedbackChannel(pytest_project, python="python3")
        first = render_feedback(ch.run("test/test_hidden.py"))
        second = render_feedback(FeedbackChannel(pytest_project, "python3").run("test/test_hidden.py"))

        assert first == second

    def test_no_timing_or_path_noise(self, pytest_project):
        """Durations, temp paths and pids differ every run and would make feedback
        non-identical across models."""
        import re

        shown = render_feedback(FeedbackChannel(pytest_project, "python3").run("test/test_hidden.py"))

        assert not re.search(r"\d+\.\d+s\b", shown), "duration leaked into feedback"
        assert "/tmp/" not in shown
        assert "pid" not in shown.lower()


class TestBytecodeSuppression:
    def test_no_pyc_is_written(self, pytest_project):
        """Regression: default umask writes test/__pycache__/*.pyc world-readable and
        the hidden source is recoverable from it as plain text. That silently bypasses
        every isolation layer."""
        FeedbackChannel(pytest_project, "python3").run("test/test_hidden.py")

        assert not list(pytest_project.rglob("*.pyc")), "bytecode cache written"


class TestRowShape:
    def test_rows_carry_nodeid_and_outcome_only(self, pytest_project):
        rows = FeedbackChannel(pytest_project, "python3").run("test/test_hidden.py")

        assert rows, "no rows collected"
        for r in rows:
            assert set(r) <= {"nodeid", "outcome", "exc_type"}
            assert r["outcome"] in {"passed", "failed", "error", "skipped"}

    def test_exception_type_is_coarse_not_internal(self, pytest_project):
        """Internal repr classes leak implementation detail of the runner."""
        rows = FeedbackChannel(pytest_project, "python3").run("test/test_hidden.py")

        for r in rows:
            assert r.get("exc_type") in {None, "", "Error", "Failure"}, (
                f"exc_type too specific: {r.get('exc_type')!r}"
            )


class TestCollectors:
    def test_pytest_collector_parses_junit(self, pytest_project):
        rows = collect_pytest_rows(pytest_project, "python3", "test/test_hidden.py")

        assert {r["nodeid"] for r in rows} == {
            "test_hidden.py::test_expired_token_is_rejected",
            "test_hidden.py::test_active_token_is_accepted",
        }

    def test_node_collector_strips_titles(self, tmp_path):
        """Node's default reporter leaks the test NAME, message, expected/actual and
        stack. A name like 'returns 401 when token expired' is semantic leakage."""
        js = tmp_path / "test"
        js.mkdir()
        (js / "hidden.test.js").write_text(
            "const test=require('node:test');const assert=require('node:assert');\n"
            "test('CANARY_7f3a91 premium tier', () => {\n"
            "  assert.strictEqual(2+2, 5, 'LIFETIME_PRICE_CENTS 49900');\n"
            "});\n"
        )
        rows = collect_node_rows(tmp_path, "node", "test/hidden.test.js")

        assert rows
        blob = " ".join(r["nodeid"] for r in rows)
        for canary in ("CANARY_7f3a91", "premium tier", "LIFETIME_PRICE_CENTS", "49900"):
            assert canary not in blob, f"LEAK: {canary!r} in node nodeid"


class TestEmptyAndError:
    def test_no_tests_is_an_error_not_a_pass(self, tmp_path):
        """pytest writes a well-formed but EMPTY junit report when it collects nothing.
        That is indistinguishable from "all passed" unless caught explicitly, and reading
        it as success would let a vanished test suite score as a clean fix."""
        (tmp_path / "empty").mkdir()
        rows = collect_pytest_rows(tmp_path, "python3", "empty")

        assert [r["outcome"] for r in rows] == ["error"]
        assert render_feedback(rows).strip() != ""

    def test_collection_error_surfaces_as_error_row(self, tmp_path):
        (tmp_path / "test").mkdir()
        (tmp_path / "test" / "test_broken.py").write_text("this is not python(((\n")
        rows = collect_pytest_rows(tmp_path, "python3", "test/test_broken.py")

        assert any(r["outcome"] in {"error", "failed"} for r in rows)
