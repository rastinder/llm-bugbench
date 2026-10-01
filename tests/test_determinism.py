"""Tests for the noise-floor gate."""
from __future__ import annotations

from bugbench.determinism import NoiseFloor, Trial, check, stratified_subset


def trials(task, state, scores, feedback=None):
    """Repeated trials of one state.

    Feedback defaults to a CONSTANT per cell, because a deterministic run produces the
    same bytes every time -- verified against a real mutant, which graded byte-identically
    across three repeats. Varying it by default would have manufactured instability.
    """
    fb = feedback if feedback is not None else f"fb-{task}-{state}"
    if isinstance(fb, (list, tuple)):
        out = []
        for i, item in enumerate(fb):
            score, text = item if isinstance(item, tuple) else (scores[i], item)
            out.append(Trial(task, state, i, score, 5, text))
        return out
    return [Trial(task, state, i, s, 5, fb) for i, s in enumerate(scores)]


class TestStability:
    def test_identical_repeats_pass(self):
        t = trials("t1", "buggy", [0.0, 0.0, 0.0]) + trials("t1", "fixed", [1.0, 1.0, 1.0])
        res = check(t)

        assert res.passed, res.failures
        assert res.detail["cells_measured"] == 2

    def test_a_flaky_score_fails_the_gate(self):
        t = trials("t1", "buggy", [0.0, 0.4, 0.0])
        res = check(t)

        assert not res.passed
        assert "t1/buggy" in res.unstable_tasks

    def test_unstable_feedback_with_stable_score_fails(self):
        """A stable score with drifting feedback still means the harness is not
        deterministic, and the model would have seen a different prompt each attempt."""
        t = trials("t1", "fixed", [1.0, 1.0, 1.0], feedback=["a", "b", "c"])
        res = check(t)

        assert not res.passed
        assert any("feedback text" in f for f in res.failures)

    def test_buggy_and_fixed_states_are_checked_independently(self):
        t = trials("t1", "buggy", [0.0, 0.0, 0.0]) + trials("t1", "fixed", [0.0, 1.0, 1.0])
        res = check(t)

        assert not res.passed
        assert res.unstable_tasks == ["t1/fixed"], "only the fixed state was flaky"


class TestSufficiency:
    def test_too_few_repeats_is_a_failure_not_a_pass(self):
        """Two repeats cannot demonstrate determinism; accepting them would let an
        under-measured gate report green."""
        res = check(trials("t1", "buggy", [0.0, 0.0]), min_repeats=3)

        assert not res.passed
        assert any("need 3" in f for f in res.failures)

    def test_empty_input_fails_loudly(self):
        """A gate that passes on zero evidence is worse than no gate: it reports green and
        licenses every downstream TIED verdict."""
        res = check([])

        assert not res.passed
        assert res.detail["trials"] == 0

    def test_detail_reports_spread_and_coverage(self):
        t = trials("t1", "buggy", [0.0, 0.0, 0.0]) + trials("t2", "buggy", [0.0, 0.0, 0.0])
        res = check(t)

        assert res.detail["distinct_tasks"] == 2
        assert res.detail["max_within_cell_spread"] == 0.0

    def test_spread_is_within_cell_not_across_tasks(self):
        """A panel spanning 0.0 and 1.0 is normal and must not read as unstable: the
        comparison has to be between repeats of the SAME task and state."""
        t = (trials("t1", "buggy", [0.0, 0.0, 0.0])
             + trials("t2", "buggy", [1.0, 1.0, 1.0]))
        res = check(t)

        assert res.passed, res.failures
        assert res.detail["max_within_cell_spread"] == 0.0


class TestStratifiedSubset:
    def test_spreads_across_codebases(self):
        tasks = ([{"codebase": "a", "task_id": f"a{i}"} for i in range(10)] +
                 [{"codebase": "b", "task_id": f"b{i}"} for i in range(10)])
        picked = stratified_subset(tasks, n=6)

        assert len(picked) == 6
        assert len({p[0] for p in picked}) == 2, "both codebases must be represented"

    def test_never_takes_only_the_first_codebase(self):
        """First-N would concentrate on one repo and let a flaky module hide behind an
        unrelated stable one."""
        tasks = ([{"codebase": "a", "task_id": f"a{i}"} for i in range(50)] +
                 [{"codebase": "b", "task_id": f"b{i}"} for i in range(3)])
        picked = stratified_subset(tasks, n=6)

        assert sum(p.startswith("b") for p in picked) == 3

    def test_is_deterministic(self):
        tasks = [{"codebase": "a", "task_id": f"a{i}"} for i in range(5)]
        assert stratified_subset(tasks, 3) == stratified_subset(tasks, 3)

    def test_handles_fewer_tasks_than_requested(self):
        assert len(stratified_subset([{"codebase": "a", "task_id": "a0"}], n=6)) == 1
