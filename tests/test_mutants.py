"""Tests for mutant generation: the last remaining source of benchmark tasks."""
from __future__ import annotations

import ast

import pytest

from bugbench.mutants import Mutant, apply_mutation, generate

SAMPLE = '''\
def clamp(value, low, high):
    """Keep value inside the inclusive range."""
    if value < low:
        value = low
    if value > high:
        value = high
    return value


def total_price(items, tax):
    subtotal = 0
    for it in items:
        subtotal += it["price"] * it["qty"]
    return subtotal * (1 + tax)
'''


class TestGeneration:
    def test_finds_comparison_and_arithmetic_operators(self):
        suite = generate(SAMPLE, "m.py")

        ops = {m.operator for m in suite.mutants}
        assert "gt" in ops, "a `>` comparison should be mutable"
        assert "aug_add" in ops, "a `+=` should be mutable"

    def test_mutant_ids_are_stable_across_runs(self):
        """The frozen manifest must be re-verifiable, so identical source must always
        yield identical bug_ids."""
        a = [m.bug_id for m in generate(SAMPLE, "m.py").mutants]
        b = [m.bug_id for m in generate(SAMPLE, "m.py").mutants]

        assert a == b
        assert len(a) == len(set(a)), "bug_ids must be unique"

    def test_bug_id_changes_with_source_file(self):
        a = generate(SAMPLE, "x.py").mutants
        b = generate(SAMPLE, "y.py").mutants

        assert not ({m.bug_id for m in a} & {m.bug_id for m in b})

    def test_records_enclosing_symbol(self):
        suite = generate(SAMPLE, "m.py")

        assert all(m.symbol in {"clamp", "total_price"} for m in suite.mutants)

    def test_no_duplicate_operator_site(self):
        suite = generate(SAMPLE, "m.py")
        sites = [(m.symbol, m.operator, m.line) for m in suite.mutants]

        assert len(sites) == len(set(sites))

    def test_limit_is_respected(self):
        assert len(generate(SAMPLE, "m.py", limit=3).mutants) == 3

    def test_unparseable_source_yields_nothing(self):
        assert generate("def broken(:\n", "b.py").mutants == []

    def test_boolean_operators_are_mutated(self):
        suite = generate("def f(a, b):\n    return a and b\n", "m.py")

        assert any(m.operator == "and" for m in suite.mutants)


class TestApplication:
    def test_every_generated_mutant_actually_applies(self):
        """A mutant that does not change the source is not a bug -- it would score the
        model on a task whose correct answer is the empty patch."""
        suite = generate(SAMPLE, "m.py")
        changed = [m for m in suite.mutants if apply_mutation(SAMPLE, m) != SAMPLE]

        assert len(changed) == len(suite.mutants), (
            f"{len(suite.mutants) - len(changed)} mutants did not change the source"
        )

    def test_mutation_stays_parseable(self):
        for m in generate(SAMPLE, "m.py").mutants:
            ast.parse(apply_mutation(SAMPLE, m))

    def test_exactly_one_line_differs(self):
        m = generate(SAMPLE, "m.py").mutants[0]
        out = apply_mutation(SAMPLE, m)

        diff = [i for i, (a, b) in enumerate(zip(SAMPLE.splitlines(), out.splitlines())) if a != b]
        assert len(diff) == 1

    def test_unapplicable_mutant_returns_source_unchanged(self):
        ghost = Mutant("mut_x", "m.py", "f", "gt", 9999, "a", "b")

        assert apply_mutation(SAMPLE, ghost) == SAMPLE


class TestBehaviourChange:
    """The point of a mutant: the original tests must notice it."""

    def test_clamp_mutant_is_caught_by_a_real_assertion(self):
        suite = generate(SAMPLE, "m.py")
        gt = next(m for m in suite.mutants if m.operator == "gt")
        mutated = apply_mutation(SAMPLE, gt)

        ns: dict = {}
        exec(SAMPLE, ns)          # noqa: S102 - test fixture, our own source
        assert ns["clamp"](15, 0, 10) == 10

        ns2: dict = {}
        exec(mutated, ns2)        # noqa: S102
        assert ns2["clamp"](15, 0, 10) != 10, "mutant must change observable behaviour"

    def test_every_mutant_changes_behaviour_of_its_symbol(self):
        """A mutation that parses but is semantically inert (e.g. mutating a value only
        used in a dead branch) would be an unanswerable task."""
        suite = generate(SAMPLE, "m.py")
        inert = []
        for m in suite.mutants:
            mutated = apply_mutation(SAMPLE, m)
            base, mod = {}, {}
            try:
                exec(SAMPLE, base)   # noqa: S102
                exec(mutated, mod)   # noqa: S102
            except Exception:
                continue
            for args in [(15, 0, 10), (5, 0, 10), (-3, 0, 10), ([{"price": 2, "qty": 3}], 0.2)]:
                try:
                    fn = m.symbol
                    if base.get(fn) and mod.get(fn) and base[fn](*args) != mod[fn](*args):
                        break
                except Exception:
                    continue
            else:
                inert.append(m.bug_id)

        assert not inert, f"inert mutants (unanswerable tasks): {inert}"


class TestDifficulty:
    """A panel that only contains trivial mutants cannot rank anything competent.

    Measured: three frontier models scored 1.00 on every task, and 73% of generated
    mutants were constant flips.
    """

    def test_constant_flips_are_trivial(self):
        assert generate("def f():\n    return 0\n", "m.py").mutants[0].difficulty == "trivial"

    def test_comparison_flips_are_moderate(self):
        s = generate("def f(x):\n    return x > 3\n", "m.py")
        gt = next(m for m in s.mutants if m.operator == "gt")
        assert gt.difficulty == "moderate"

    def test_accumulator_arithmetic_is_hard(self):
        s = generate("def f(xs):\n    t = 0\n    for x in xs:\n        t += x\n    return t\n", "m.py")
        aug = next((m for m in s.mutants if m.operator == "aug_add"), None)
        assert aug is not None and aug.difficulty == "hard"

    def test_every_operator_has_a_difficulty(self):
        s = generate("def f(a, b):\n    if a and b:\n        return 1\n    return 2\n", "m.py")
        assert all(m.difficulty in {"trivial", "moderate", "hard"} for m in s.mutants)

    def test_difficulty_appears_in_serialised_form(self):
        m = generate("def f():\n    return 0\n", "m.py").mutants[0]
        assert m.as_dict()["difficulty"] == "trivial"
