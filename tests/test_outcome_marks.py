"""TDD: outcome taxonomy + a decision-oriented leaderboard.

Covers the integrity bugs found in the 432-row results file:
  * infra errors were being scored as 0% model failures
  * the quarantine split (clean_rows) was dead code, so a file-read-leaked run was
    ranked #1
  * patch_reproduction (recall) was carrying half the repair score
  * availability and latency -- the things that actually decide whether a model is
    usable for agent work -- were not reported at all
"""
from __future__ import annotations

import json

import pytest

from bugbench.outcome import (
    Retryable,
    classify_error,
    outcome_state,
    is_scored,
    SCORED_STATES,
)


# --------------------------------------------------------------------------- taxonomy


def _row(diag_err="", rep_err="", taint=None):
    r = {"task_id": "B0001", "model": "m",
         "diagnose": {"total": 0.9, "latency_ms": 1000},
         "repair": {"total": 0.8, "judge_agreement": 1.0,
                    "patch_reproduction": 0.2, "latency_ms": 2000},
         "diagnose_error": diag_err, "repair_error": rep_err}
    if taint is not None:
        r["tainted"] = taint
    return r


def test_completed_row_is_scored():
    assert outcome_state(_row()) == "completed"
    assert is_scored(_row()) is True


def test_infra_error_row_is_not_scored():
    """The headline bug: an HTTP 429 must not look like a 0% model."""
    r = _row(rep_err="ModelError: HTTP 429: no deployments")
    assert outcome_state(r) == "infra_error"
    assert is_scored(r) is False


def test_429_is_retryable_but_400_is_not():
    """Decides whether the top-up should burn more attempts on a model."""
    assert classify_error("ModelError: HTTP 429: rate limited").retryable is True
    assert classify_error("ModelError: HTTP 503: unavailable").retryable is True
    assert classify_error("ModelError: TimeoutError: read timed out").retryable is True
    assert classify_error("ModelError: HTTP 400: invalid model name").retryable is False
    assert classify_error("ModelError: HTTP 401: auth").retryable is False
    assert classify_error("ModelError: HTTP 404: not found").retryable is False


def test_taint_outranks_timeout():
    """B1: a leaked answer is untrustworthy even when the transport also failed."""
    r = _row(rep_err="ModelError: HTTP 429: rate limited", taint="file_read_leak")
    assert outcome_state(r) == "tainted"


def test_policy_blocked_is_its_own_state():
    r = _row(rep_err="agy lane disabled: no filesystem isolation")
    assert outcome_state(r) == "policy_blocked"
    assert is_scored(r) is False


def test_invalid_response_when_transport_ok_but_answer_unusable():
    r = _row()
    r["repair"] = {"total": 0.0, "judge_agreement": 0.0, "patch_reproduction": 0.0}
    r["empty_reply"] = True
    assert outcome_state(r) == "invalid_response"


def test_scored_states_are_exhaustive_and_unique():
    assert SCORED_STATES == ("completed",)


# --------------------------------------------------------------------------- marks


def _mkrow(model, tid, combined, *, err="", taint=None, lat=1000):
    r = {"model": model, "task_id": tid, "diagnose": {"total": combined, "latency_ms": lat},
         "repair": {"total": combined, "judge_agreement": 1.0,
                    "patch_reproduction": 0.0, "latency_ms": lat},
         "diagnose_error": "", "repair_error": err, "combined": combined}
    if taint:
        r["tainted"] = taint
    return r


def _tasks(ids):
    class T:
        def __init__(self, i):
            self.task_id = i
            self.buggy = "def f():\n    return 1\n" + "x" * 60
            self.reference_fix = "def f():\n    return 2\n" + "y" * 60
    return {i: T(i) for i in ids}


def test_utility_is_capability_times_availability():
    from bugbench.marks import utility
    # perfect answers, but half the attempts never landed
    assert utility(capability=80.0, completed=10, attempted=20) == pytest.approx(40.0)
    # everything landed
    assert utility(capability=80.0, completed=20, attempted=20) == pytest.approx(80.0)


def test_wilson_ci_known_bounds():
    from bugbench.marks import wilson_ci
    lo, hi = wilson_ci(10, 20)
    assert 0.29 < lo < 0.33
    assert 0.66 < hi < 0.71
    # degenerate cases must not explode
    assert wilson_ci(0, 0) == (0.0, 0.0)
    lo0, hi0 = wilson_ci(0, 20)
    assert lo0 == 0.0 and 0 < hi0 < 0.2


def test_tainted_row_never_enters_ranking():
    from bugbench.marks import decision_board
    ids = [f"B{i:04d}" for i in range(1, 21)]
    tasks = _tasks(ids)
    good = [_mkrow("honest", t, 0.9) for t in ids]
    cheater = [_mkrow("cheater", t, 0.99, taint="file_read_leak") for t in ids]
    board, quarantined = decision_board(good + cheater, tasks)
    assert "cheater" not in [b["model"] for b in board]
    assert "cheater" in quarantined


def test_dead_endpoint_is_not_a_zero_percent_model():
    """The other headline bug: 20x HTTP 400 must read as 'dead', not '0% model'."""
    from bugbench.marks import decision_board
    ids = [f"B{i:04d}" for i in range(1, 21)]
    tasks = _tasks(ids)
    dead = [_mkrow("dead-endpoint", t, 0.0, err="ModelError: HTTP 400: invalid model name")
            for t in ids]
    board, excluded = decision_board(dead, tasks)
    entry = board[0]
    assert entry["model"] == "dead-endpoint"
    assert entry["availability_pct"] == 0.0
    assert entry["ranking_eligible"] is False
    assert entry["status"] == "unavailable"      # NOT a capability score of 0
    assert entry["utility"] == 0.0


def test_coverage_below_threshold_is_not_ranked():
    from bugbench.marks import decision_board
    ids = [f"B{i:04d}" for i in range(1, 21)]
    tasks = _tasks(ids)
    # 14 completed (>= MIN_COVERAGE 15? no -> below) with perfect answers
    rows = [_mkrow("partial", t, 1.0) for t in ids[:14]]
    board, _ = decision_board(rows, tasks)
    assert board[0]["ranking_eligible"] is False
    assert board[0]["status"] == "insufficient_coverage"


def test_non_filesystem_lane_is_not_quarantined_for_a_verbatim_answer(tmp_path):
    """A plain API model has no disk access, so a byte-identical answer is NOT a leak.

    Without the lane-capability gate this quarantined healthy API models and discarded
    the #2 and #3 results on the board over one matching task each.
    """
    from bugbench.marks import clean_rows
    tid = "B0001"
    (tmp_path / "widget.py").write_text("x = 1\n")   # the real source IS on disk
    t = _tasks([tid])[tid]
    t.file_name = "widget.py"
    row = {"model": "openrouter-space-bunny-alpha", "task_id": tid,
           "diagnose": {"total": 0.5}, "repair": {"total": 0.9, "candidate": t.reference_fix,
                                                  "judge_agreement": 1.0,
                                                  "patch_reproduction": 1.0},
           "diagnose_error": "", "repair_error": "", "combined": 0.9}
    trusted, quarantined = clean_rows([row], {tid: t}, roots=(str(tmp_path),))
    assert trusted and not quarantined, \
        "an API lane cannot read the file, so it must not be treated as a file-read leak"


def test_filesystem_lane_IS_quarantined_for_a_verbatim_answer(tmp_path):
    from bugbench.marks import clean_rows
    tid = "B0001"
    (tmp_path / "widget.py").write_text("x = 1\n")
    t = _tasks([tid])[tid]
    t.file_name = "widget.py"
    row = {"model": "agy-gemini-3.8-flash-high", "task_id": tid,
           "diagnose": {"total": 0.5}, "repair": {"total": 0.9, "candidate": t.reference_fix,
                                                  "judge_agreement": 1.0,
                                                  "patch_reproduction": 1.0},
           "diagnose_error": "", "repair_error": "", "combined": 0.9}
    trusted, quarantined = clean_rows([row], {tid: t}, roots=(str(tmp_path),))
    assert not trusted and quarantined
    assert row["tainted"] == "file_read_leak"      # verdict is stamped on the row


def test_latency_is_surfaced():
    from bugbench.marks import decision_board
    ids = [f"B{i:04d}" for i in range(1, 21)]
    tasks = _tasks(ids)
    rows = [_mkrow("slow", t, 0.5, lat=30000) for t in ids]
    board, _ = decision_board(rows, tasks)
    assert board[0]["median_latency_ms"] == 30000


def test_patch_similarity_excluded_from_rank_score():
    """Recall must not be half the correctness score.

    The candidate is byte-identical to the historical fix (max recall) but the task is
    NOT an unchanged-input case, so the only thing the old formula rewarded was
    memorisation. rank_score must reflect the judge only.
    """
    from bugbench.grade import score_repair
    buggy = "def f(x):\n    return x - 1\n" + "a" * 60
    ref = "def f(x):\n    return x + 1\n" + "b" * 60
    t = type("T", (), {"buggy": buggy, "reference_fix": ref})()
    out = score_repair(t, ref, judge_agreement=1.0)
    assert out["patch_similarity"] == pytest.approx(1.0)      # it did reproduce the fix
    assert out["rank_score"] == pytest.approx(1.0)           # judge says correct
    assert out["total"] == pytest.approx(1.0)                # recall adds nothing on top
    assert "patch_similarity" not in out["rank_weights"]      # and is not a rank weight
