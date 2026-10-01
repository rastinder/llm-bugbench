"""Tests for panel selection and manifest freezing."""
from __future__ import annotations

import json

from bugbench.panel import (
    PUBLIC_CODEBASES, Selection, compute_hash, dedupe, freeze, load_shards,
    select, verify_rows,
)


def task(cb, bug_id, module="m.py", line=1, depth="function", node=None):
    return {
        "task_id": f"{cb}__{bug_id}", "codebase": cb, "module": module,
        "bug_id": bug_id, "symbol": "f", "operator": "gt", "line": line,
        "test_node": node or f"{module}::test_{bug_id}",
        "depth": depth, "n_failing": 1,
    }


class TestDedupe:
    def test_same_bug_twice_is_one_task(self):
        t = task("a", "b1")
        assert len(dedupe([t, dict(t)])) == 1

    def test_same_bug_id_in_different_modules_is_kept(self):
        both = [task("a", "b1", "x.py"), task("a", "b1", "y.py")]
        assert len(dedupe(both)) == 2

    def test_same_bug_id_in_different_codebases_is_kept(self):
        assert len(dedupe([task("a", "b1"), task("b", "b1")])) == 2


class TestEqualWeighting:
    def test_each_codebase_gets_the_same_budget(self):
        """The point of equal weighting: a repo with 40 supply must not dominate a repo
        with 20, or the panel silently becomes one codebase."""
        tasks = [task("big", f"b{i}") for i in range(40)] + \
                [task("small", f"b{i}") for i in range(20)]
        sel = select(tasks, target=20)

        assert sel.per_codebase_quota == {"big": 10, "small": 10}
        assert len(sel.tasks) == 20
        assert sum(1 for t in sel.tasks if t["codebase"] == "big") == 10

    def test_quota_is_a_target_capped_by_actual_supply(self):
        """A quota larger than a codebase's supply must not be padded from elsewhere:
        borrowing would defeat the equal weighting it exists to provide."""
        tasks = [task("big", f"b{i}") for i in range(40)] + \
                [task("small", f"b{i}") for i in range(4)]
        sel = select(tasks, target=20)

        assert sel.per_codebase_quota == {"big": 10, "small": 10}
        assert len(sel.tasks) == 14, "small contributes only its 4, big caps at its quota"
        assert sum(1 for t in sel.tasks if t["codebase"] == "small") == 4

    def test_surplus_is_reported_not_hidden(self):
        tasks = [task("big", f"b{i}") for i in range(30)] + \
                [task("small", f"b{i}") for i in range(20)]
        sel = select(tasks, target=20)

        assert sel.excluded == {"big": 20, "small": 10}
        assert sum(sel.excluded.values()) == 30

    def test_remainder_is_allocated_deterministically(self):
        tasks = [task("a", f"b{i}") for i in range(10)] + \
                [task("b", f"b{i}") for i in range(10)] + \
                [task("c", f"b{i}") for i in range(10)]
        sel = select(tasks, target=25)

        assert len(sel.tasks) == 25
        assert sum(sel.per_codebase_quota.values()) == 25

    def test_function_depth_is_preferred_over_module_constants(self):
        tasks = ([task("a", f"m{i}", depth="module") for i in range(5)] +
                 [task("a", f"f{i}", depth="function") for i in range(5)])
        sel = select(tasks, target=3)

        assert all(t["depth"] == "function" for t in sel.tasks)

    def test_selection_is_deterministic(self):
        tasks = [task("a", f"b{i}", line=i) for i in range(20)]
        assert [t["bug_id"] for t in select(tasks, 10).tasks] == \
               [t["bug_id"] for t in select(tasks, 10).tasks]

    def test_supply_below_target_uses_all_available(self):
        sel = select([task("a", f"b{i}") for i in range(3)], target=25)

        assert len(sel.tasks) == 3

    def test_no_tasks_yields_empty_selection(self):
        sel = select([], target=25)

        assert sel.tasks == []


class TestManifestHash:
    def test_same_panel_hashes_identically(self):
        a = select([task("a", f"b{i}") for i in range(5)], 5)
        b = select([task("a", f"b{i}") for i in range(5)], 5)
        assert compute_hash(a) == compute_hash(b)

    def test_different_panel_hashes_differently(self):
        a = select([task("a", f"b{i}") for i in range(5)], 5)
        b = select([task("a", f"b{i}") for i in range(4)], 5)
        assert a.manifest_hash != b.manifest_hash

    def test_hash_is_independent_of_input_order(self):
        t = [task("a", "b1"), task("a", "b2"), task("b", "b1")]
        assert select(t, 3).manifest_hash == select(list(reversed(t)), 3).manifest_hash


class TestRowVerification:
    def test_rows_from_the_frozen_manifest_are_accepted(self):
        rows = [{"manifest_hash": "abc123", "model": "m"}]
        ok, problems = verify_rows(rows, "abc123")
        assert ok and not problems

    def test_rows_from_a_stale_manifest_are_refused(self):
        """Mixing panels silently produces a comparison that means nothing -- the exact
        failure that made the historical 552-row archive unrankable."""
        rows = [{"manifest_hash": "old"}, {"manifest_hash": "new"}]
        ok, problems = verify_rows(rows, "new")

        assert not ok
        assert any("old" in p for p in problems)

    def test_missing_manifest_hash_is_refused(self):
        ok, problems = verify_rows([{"model": "m"}], "abc123")
        assert not ok


class TestFreezeRoundTrip:
    def test_frozen_manifest_reloads_with_its_hash(self, tmp_path):
        sel = select([task("a", f"b{i}") for i in range(4)], 4)
        path = freeze(sel, tmp_path / "panel.json")
        data = json.loads(path.read_text())

        assert data["manifest_hash"] == sel.manifest_hash
        assert len(data["tasks"]) == 4

    def test_shards_load_from_disk(self, tmp_path):
        (tmp_path / "s1.json").write_text(json.dumps([task("a", "b1")]))
        (tmp_path / "s2.json").write_text(json.dumps([task("b", "b2")]))

        assert len(load_shards(sorted(tmp_path.glob("*.json")))) == 2

    def test_missing_shard_is_skipped_not_fatal(self, tmp_path):
        (tmp_path / "s1.json").write_text(json.dumps([task("a", "b1")]))
        assert len(load_shards([tmp_path / "nope.json", tmp_path / "s1.json"])) == 1


class TestPublicExposure:
    def test_public_codebase_is_known(self):
        """marketplace-monitor is public, so its tasks are memorisation probes and must
        be labelled rather than silently mixed into the headline."""
        assert "marketplace-monitor" in PUBLIC_CODEBASES

    def test_selection_records_the_codebase_of_every_task(self):
        sel = select([task("marketplace-monitor", "b1"), task("private", "b1")], 2)
        assert {t["codebase"] for t in sel.tasks} == {"marketplace-monitor", "private"}


class TestDifficultyOrdering:
    """The panel that produced 1.00 across three frontier models was 73% constant flips.
    Selection must prefer hard mutants, and must be able to exclude trivial ones entirely.
    """

    def _t(self, cb, bug_id, difficulty):
        t = task(cb, bug_id)
        t["difficulty"] = difficulty
        return t

    def test_hard_mutants_are_preferred_over_trivial(self):
        tasks = ([self._t("a", f"triv{i}", "trivial") for i in range(5)] +
                 [self._t("a", f"hard{i}", "hard") for i in range(5)])
        sel = select(tasks, target=3)

        assert all(t["difficulty"] == "hard" for t in sel.tasks)

    def test_trivial_tasks_are_excluded_by_default(self):
        tasks = ([self._t("a", f"triv{i}", "trivial") for i in range(9)] +
                 [self._t("a", f"mod{i}", "moderate") for i in range(9)])
        sel = select(tasks, target=6)

        assert all(t["difficulty"] != "trivial" for t in sel.tasks)

    def test_trivial_used_only_when_nothing_else_exists(self):
        """A codebase with only trivial mutants must still contribute something rather
        than vanishing from the panel."""
        tasks = [self._t("a", f"triv{i}", "trivial") for i in range(4)]
        sel = select(tasks, target=2)

        assert len(sel.tasks) == 2

    def test_manifest_hash_changes_with_difficulty_mix(self):
        easy = [self._t("a", "x", "trivial")]
        hard = [self._t("a", "x", "hard")]
        assert select(easy, 1).manifest_hash != select(hard, 1).manifest_hash

    def test_difficulty_mix_is_reported(self):
        tasks = ([self._t("a", f"h{i}", "hard") for i in range(3)] +
                 [self._t("a", f"m{i}", "moderate") for i in range(3)])
        sel = select(tasks, target=4)
        assert set(sel.as_dict()["difficulty_mix"]) <= {"hard", "moderate", "trivial"}
