"""Tests for recovering real historical code from session transcripts.

The point of this module is to get bugs that actually happened rather than mutants we
invented, so the tests are mostly about refusing to produce a wrong pair. Every rejection
below corresponds to a way the DB could hand back a plausible-looking pair that is not
real, which is the failure mode that would quietly poison the panel.
"""
from __future__ import annotations

import pytest

from bugbench.history import (
    FORBIDDEN, HistoricalPair, Snapshot, find_pairs, is_forbidden,
    parse_read_output,
)

READ = """<path>/tmp/x.py</path>
<type>file</type>
<content>
1: def f():
2:     return 1
3:
4: def g():
5:     return 2
</content>

(End of file - total 5 lines)
"""


class TestParseReadOutput:
    def test_extracts_the_body(self):
        out = parse_read_output(READ)
        assert "def f():" in out and "return 2" in out

    def test_strips_line_number_prefixes(self):
        out = parse_read_output(READ)
        assert not out.splitlines()[0].startswith("1: ")

    def test_returns_none_for_a_non_file_payload(self):
        assert parse_read_output("just some output") is None
        assert parse_read_output("") is None


class TestForbiddenPaths:
    @pytest.mark.parametrize("p", [
        "/home/ras/Desktop/KEYS/api.txt",
        "/home/ras/.ssh/id_rsa",
        "/home/ras/.config/opencode/opencode.json",
        "/srv/.env",
    ])
    def test_secret_bearing_paths_are_refused(self, p):
        """The corpus contains a read of the credential store, verbatim. A task built from
        one would put a live key into a model prompt and into a results file."""
        assert is_forbidden(p)

    def test_ordinary_source_is_allowed(self):
        assert not is_forbidden("/home/ras/whatsedit-local/styles.py")


def snap(path, t, content):
    return Snapshot(path, t, "read", content)


class TestFindPairs:
    def test_produces_a_pair_when_disk_matches_the_newer_snapshot(self, tmp_path):
        f = tmp_path / "m.py"
        final = "def f():\n    return 2\n"
        f.write_text(final)
        earlier = "def f():\n    return 1\n"
        snaps = {str(f): [snap(str(f), 1, earlier), snap(str(f), 2, final)]}

        rep = find_pairs(snaps)
        assert len(rep.pairs) == 1
        assert rep.pairs[0].before.content == earlier
        assert rep.pairs[0].after.content == final

    def test_drifted_file_still_yields_a_pair_from_consecutive_snapshots(self, tmp_path):
        """Anchoring alone yielded 3 pairs from 124 multi-read files, because most files
        were edited again after their last read. Two consecutive snapshots are still two
        real states at two real moments, so the pair is kept and flagged as unanchored."""
        f = tmp_path / "m.py"
        f.write_text("def f():\n    return 999\n")   # drifted past both snapshots
        snaps = {str(f): [snap(str(f), 1, "def f():\n    return 1\n"),
                          snap(str(f), 2, "def f():\n    return 2\n")]}

        pairs = find_pairs(snaps).pairs
        assert len(pairs) == 1
        assert pairs[0].before.content == "def f():\n    return 1\n"
        assert pairs[0].on_disk_matches_after is False, "must be flagged unanchored"

    def test_identical_snapshots_are_not_a_pair(self, tmp_path):
        f = tmp_path / "m.py"
        same = "def f():\n    return 1\n"
        f.write_text(same)
        snaps = {str(f): [snap(str(f), 1, same), snap(str(f), 2, same)]}

        assert find_pairs(snaps).pairs == []

    def test_a_single_snapshot_yields_nothing(self, tmp_path):
        f = tmp_path / "m.py"
        f.write_text("x = 1\n")
        assert find_pairs({str(f): [snap(str(f), 1, "x = 1\n")]}).pairs == []

    def test_anchored_pairs_are_marked_as_such(self, tmp_path):
        f = tmp_path / "m.py"
        final = "def f():\n    return 3\n"
        f.write_text(final)
        snaps = {str(f): [snap(str(f), 1, "def f():\n    return 1\n"),
                          snap(str(f), 2, final)]}
        assert find_pairs(snaps).pairs[0].on_disk_matches_after is True

    def test_anchor_is_the_newest_matching_snapshot(self, tmp_path):
        """With several reads of one file, the pair that matters is the one whose 'after'
        survived to disk -- not merely the last two reads."""
        f = tmp_path / "m.py"
        final = "def f():\n    return 3\n"
        f.write_text(final)
        snaps = {str(f): [
            snap(str(f), 1, "def f():\n    return 1\n"),
            snap(str(f), 2, "def f():\n    return 2\n"),
            snap(str(f), 3, final),
        ]}
        rep = find_pairs(snaps)

        assert rep.pairs[0].after.content == final
        assert rep.pairs[0].before.content == "def f():\n    return 2\n"

    def test_report_counts_are_reported(self, tmp_path):
        f = tmp_path / "m.py"
        final = "def f():\n    return 2\n"
        f.write_text(final)
        rep = find_pairs({str(f): [snap(str(f), 1, "x\n"), snap(str(f), 2, final)]})

        d = rep.as_dict()
        assert d["files_with_snapshots"] == 1
        assert d["real_pairs"] == 1
        assert d["anchored_on_disk"] == 1


class TestChangeSize:
    def test_changed_lines_counts_real_edits(self):
        p = HistoricalPair(
            "/x.py",
            Snapshot("/x.py", 1, "read", "a\nb\nc\n"),
            Snapshot("/x.py", 2, "read", "a\nB\nc\n"),
        )
        assert p.changed_lines == 2, "one removal and one addition"

    def test_unchanged_pair_has_no_changes(self):
        p = HistoricalPair("/x.py", Snapshot("/x.py", 1, "read", "a\n"),
                           Snapshot("/x.py", 2, "read", "a\n"))
        assert p.changed_lines == 0


class TestForbiddenSetIsNotEmpty:
    def test_the_set_covers_the_corpus_hazards(self):
        assert any("KEYS" in m for m in FORBIDDEN)
        assert any(".ssh" in m for m in FORBIDDEN)

class TestProbeSafety:
    """Generated probes must be hermetic: anything touching the clock, network or
    filesystem would make the oracle unstable and the task unscoreable."""

    def _safe(self, src, name="f", expected_ok=True):
        """Check probe_safety on the first statement, and separately whether the callable
        survives extraction. Both must agree, so each is asserted independently rather than
        short-circuiting on the first failure."""
        import ast
        from bugbench.probe import extract_callables, probe_safe
        node = ast.parse(src).body[0]
        ok, why = probe_safe(node)
        assert ok is expected_ok, f"safety said {ok} ({why})"
        found = extract_callables(src)
        if not expected_ok:
            assert found == [], "an unsafe callable must not be extractable"
        return found

    def test_pure_function_is_accepted(self):
        assert self._safe("def f():\n    return 1 + 1\n")

    @pytest.mark.parametrize("src", [
        "def f():\n    return open('/etc/passwd').read()\n",
        "def f():\n    import socket\n    return 1\n",
        "def f():\n    return os.environ.get('X')\n",
        "def f():\n    return random.random()\n",
        "def f():\n    import time\n    return time.time()\n",
    ])
    def test_unsafe_callables_are_rejected_and_not_extracted(self, src):
        import ast
        from bugbench.probe import extract_callables, probe_safe
        ok, why = probe_safe(ast.parse(src).body[0])
        assert not ok, why
        assert extract_callables(src) == []

    def test_annotated_parameters_are_allowed(self):
        """Regression: requiring zero-argument callables found a candidate in 1 of 28 real
        pairs, because production code takes arguments. Annotated params can be given a
        literal, so they are admitted."""
        from bugbench.probe import extract_callables
        assert [n for n, _ in extract_callables("def f(a: int) -> int:\n    return a\n")]

    def test_unannotated_parameters_are_skipped(self):
        """No annotation means no principled literal, and a fabricated fixture would make
        the oracle a test of the fixture."""
        from bugbench.probe import extract_callables
        assert extract_callables("def f(a, b):\n    return a + b\n") == []

    def test_too_many_parameters_are_skipped(self):
        from bugbench.probe import extract_callables
        assert extract_callables("def f(a: int, b: int, c: int) -> int:\n    return a\n") == []

    def test_literal_args_are_derived_from_annotations(self):
        from bugbench.probe import literal_args
        assert literal_args("def f(n: int) -> int:\n    return n\n", "f") == "1"
        assert literal_args("def f(s: str) -> str:\n    return s\n", "f") == '"x"'
        assert literal_args("def f() -> int:\n    return 1\n", "f") == ""

    def test_unknown_annotation_yields_no_args(self):
        from bugbench.probe import literal_args
        assert literal_args("def f(x: Widget) -> None:\n    return None\n", "f") is None

    def test_dunder_methods_are_skipped(self):
        assert self._safe("def __repr__():\n    return 'x'\n") == []

    def test_unparseable_source_yields_nothing(self):
        from bugbench.probe import extract_callables
        assert extract_callables("def broken(:\n") == []


class TestTruncatedSnapshots:
    """Regression: 26 of the 28 largest historical pairs were unparseable because the reader
    caps output at 50 KB and paginates. A naive ends-with-a-closing-token check accepted
    those, since the footer ends in a period."""

    CAPPED = ("<path>/tmp/x.py</path>\n<type>file</type>\n<content>\n"
              "1: def f():\n2:     return 1\n\n"
              "(Output capped at 50 KB. Showing lines 2400-3556. Use offset=3557 to continue.)\n"
              "</content>")

    PAGED = ("<path>/tmp/x.py</path>\n<type>file</type>\n<content>\n"
             "1: def f():\n\n(Showing lines 1-1305)\n</content>")

    def test_capped_output_is_rejected(self):
        assert parse_read_output(self.CAPPED) is None

    def test_paginated_output_is_rejected(self):
        assert parse_read_output(self.PAGED) is None

    def test_complete_output_is_accepted(self):
        assert "def f():" in (parse_read_output(READ) or "")

    def test_truncation_detector_is_directly_usable(self):
        from bugbench.history import is_truncated
        assert is_truncated("Output capped at 50 KB")
        assert not is_truncated("def f():\n    return 1\n")
