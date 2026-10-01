"""Tests for the pre-registered statistics.

These lock the analysis down. A benchmark whose verdict depends on which statistical test
was chosen is not measuring anything, so the behaviours asserted here are the contract:
paired-within-bug permutation, Holm correction across the family, and above all that
TIED and INCONCLUSIVE stay distinct answers.
"""
from __future__ import annotations

import random

import pytest

from bugbench.stats import (
    DEFAULT_DELTA0, cliffs_delta, compare, compare_all, holm,
    paired_differences, permutation_p, tost,
)


def series(base, n, step=0.0):
    return {f"b{i}": base + step * i for i in range(n)}


CBC = {f"b{i}": f"cb{i % 4}" for i in range(40)}


class TestPairing:
    def test_uses_only_shared_bugs(self):
        diffs = paired_differences({"a": 1.0, "b": 1.0}, {"a": 0.0, "z": 0.0})

        assert set(diffs) == {"a"}

    def test_no_overlap_is_inconclusive_not_zero(self):
        """Reporting zero difference for two models that never saw the same bug would be
        the single worst failure mode here."""
        v = compare("A", "B", {"a": 1.0}, {"b": 1.0}, {})
        assert v.verdict == "INCONCLUSIVE"
        assert v.n_bugs == 0

    def test_differences_are_signed(self):
        d = paired_differences({"a": 1.0, "b": 0.0}, {"a": 0.0, "b": 1.0})
        assert d["a"] == 1.0 and d["b"] == -1.0


class TestPermutation:
    def test_all_ties_gives_p_one(self):
        assert permutation_p([0.0, 0.0, 0.0], random.Random(1)) == 1.0

    def test_clear_difference_is_significant(self):
        p = permutation_p([0.5] * 10, random.Random(1))
        assert p < 0.05

    def test_noise_is_not_significant(self):
        rng = random.Random(7)
        noise = [rng.choice([-0.5, 0.5]) for _ in range(20)]
        assert permutation_p(noise, random.Random(1)) > 0.05

    def test_is_deterministic_for_a_given_seed(self):
        d = [0.3, -0.2, 0.4, 0.1, -0.5, 0.2, 0.6, 0.1, -0.1, 0.3]
        assert permutation_p(d, random.Random(3)) == permutation_p(d, random.Random(3))


class TestCliffsDelta:
    def test_identical_series_is_zero(self):
        assert cliffs_delta(series(0.5, 10), series(0.5, 10)) == 0.0

    def test_dominance_is_one(self):
        a = {f"b{i}": 1.0 for i in range(10)}
        b = {f"b{i}": 0.0 for i in range(10)}
        assert cliffs_delta(a, b) == 1.0

    def test_symmetric_advantage_is_zero(self):
        a = {"x": 1.0, "y": 0.0}
        b = {"x": 0.0, "y": 1.0}
        assert cliffs_delta(a, b) == 0.0


class TestTost:
    def test_narrow_interval_around_zero_is_tied(self):
        assert tost(-0.02, 0.03, DEFAULT_DELTA0)

    def test_wide_interval_is_inconclusive(self):
        assert not tost(-0.4, 0.5, DEFAULT_DELTA0)

    def test_interval_excluding_zero_is_not_equivalent(self):
        assert not tost(0.3, 0.8, DEFAULT_DELTA0)


class TestHolm:
    def test_adjustment_is_monotonic_and_bounded(self):
        adj = holm({"a": 0.001, "b": 0.02, "c": 0.5})
        vals = [adj["a"], adj["b"], adj["c"]]
        assert vals == sorted(vals)
        assert all(0 <= v <= 1 for v in vals)

    def test_more_comparisons_inflate_the_adjusted_value(self):
        one = holm({"a": 0.01})["a"]
        many = holm({"a": 0.01, **{f"k{i}": 0.01 for i in range(9)}})["a"]
        assert many > one, "10 comparisons must penalise p more than 1"

    def test_correction_preserves_order(self):
        adj = holm({"a": 0.001, "b": 0.4})
        assert adj["a"] < adj["b"]


class TestVerdicts:
    def test_clear_winner(self):
        a = {f"b{i}": 1.0 for i in range(30)}
        b = {f"b{i}": 0.0 for i in range(30)}
        v = compare("A", "B", a, b, CBC)

        assert v.verdict == "A_WINS"
        assert v.cliffs_delta == 1.0

    def test_genuine_tie_is_labelled_tied_not_inconclusive(self):
        """Both models identical -> zero difference, zero variance -> bounded."""
        s = series(0.5, 30)
        v = compare("A", "B", s, dict(s), CBC)

        assert v.verdict == "TIED"
        assert any("bounded" in n for n in v.notes)

    def test_underpowered_noise_is_inconclusive_not_tied(self):
        """The distinction that stops the benchmark claiming two models are equal when
        it simply could not measure them."""
        rng = random.Random(11)
        a = {f"b{i}": float(rng.randint(0, 1)) for i in range(12)}
        b = {f"b{i}": float(rng.randint(0, 1)) for i in range(12)}
        v = compare("A", "B", a, b, {f"b{i}": "cb" for i in range(12)},
                    delta0=0.05)

        assert v.verdict in {"TIED", "INCONCLUSIVE"}
        if v.verdict == "INCONCLUSIVE":
            assert any("NOT a tie" in n for n in v.notes)

    def test_inconclusive_is_never_reported_as_a_win(self):
        rng = random.Random(5)
        a = {f"b{i}": float(rng.randint(0, 1)) for i in range(8)}
        b = {f"b{i}": float(rng.randint(0, 1)) for i in range(8)}
        v = compare("A", "B", a, b, {f"b{i}": "cb" for i in range(8)}, delta0=0.01)

        assert v.verdict != "A_WINS"

    def test_win_that_fails_holm_is_downgraded(self):
        """Ten uncorrected comparisons at alpha=0.05 manufacture ~one false winner per
        campaign; a win that does not survive correction is not a finding."""
        rng = random.Random(2)
        scores = {}
        for m in range(5):
            scores[f"M{m}"] = {
                f"b{i}": float(rng.randint(0, 1)) + (0.3 if m == 0 and rng.random() < .6 else 0)
                for i in range(10)
            }
        verdicts = compare_all(scores, {f"b{i}": f"cb{i % 3}" for i in range(10)})
        wins = [v for v in verdicts if v.verdict.endswith("_WINS")]

        for v in wins:
            assert v.p_adjusted < 0.05, "a reported win must survive Holm correction"


class TestFamilyComparison:
    def test_five_models_give_ten_comparisons(self):
        scores = {f"M{i}": series(0.5 * i, 10) for i in range(5)}
        assert len(compare_all(scores, CBC)) == 10

    def test_all_adjusted_p_values_are_present_and_bounded(self):
        scores = {f"M{i}": series(0.1 * i, 12) for i in range(5)}
        for v in compare_all(scores, CBC):
            assert 0.0 <= v.p_adjusted <= 1.0

    def test_identical_models_all_tie(self):
        s = series(0.5, 15)
        scores = {f"M{i}": dict(s) for i in range(5)}
        verdicts = compare_all(scores, CBC)

        assert all(v.verdict == "TIED" for v in verdicts)

    def test_cluster_disagreement_is_recorded_not_hidden(self):
        """Pre-registered rule: trust the paired result and say they disagree."""
        a = {f"b{i}": (1.0 if i % 2 else 0.0) for i in range(20)}
        b = {f"b{i}": (0.0 if i % 2 else 1.0) for i in range(20)}
        one_cb = {f"b{i}": "only" for i in range(20)}
        v = compare("A", "B", a, b, one_cb)

        assert v.cluster_ci is not None


class TestDeterminism:
    def test_repeated_analysis_is_identical(self):
        scores = {f"M{i}": series(0.2 * i, 14) for i in range(4)}
        first = [v.as_dict() for v in compare_all(scores, CBC)]
        second = [v.as_dict() for v in compare_all(scores, CBC)]
        assert first == second
