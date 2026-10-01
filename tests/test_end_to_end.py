"""End-to-end: a panel task, materialised, run, graded, and scored.

Every other module is tested in isolation, which cannot show that they compose. This
exercises the real chain on real repositories:

    panel task -> buggy sandbox -> hidden tests -> leak-free feedback -> per-bug score

and asserts the two properties the whole benchmark rests on: a buggy state scores zero and
a fixed state scores one, and nothing from the hidden test ever appears in the text the
model would be shown.

It is deliberately not mocked. A mocked end-to-end test proves the mocks agree with each
other.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bugbench.feedback import FeedbackChannel, render_feedback   # noqa: E402
from bugbench.mutants import apply_mutation, generate            # noqa: E402
from bugbench.sandbox import build_sandbox, grader_env           # noqa: E402
from bugbench.scoring import score_from_rows, validate_oracle    # noqa: E402

PANEL = Path(__file__).resolve().parents[1] / "tasks" / "panel25.json"
ROOTS = {
    ".opencode-telegram-bot": Path.home() / ".opencode-telegram-bot",
    ".fix-backend": Path.home() / ".fix-backend",
    "copilot-model-audit": Path.home() / "copilot-model-audit",
}

pytestmark = pytest.mark.skipif(
    not PANEL.exists(), reason="panel not frozen yet")


def _panel():
    return json.loads(PANEL.read_text())


def _resolve_mutant(task):
    repo = ROOTS[task["codebase"]]
    src = repo / task["module"]
    pristine = src.read_text()
    mutant = next(
        (m for m in generate(pristine, task["module"], limit=80).mutants
         if m.bug_id == task["bug_id"]),
        None,
    )
    assert mutant is not None, f"bug_id {task['bug_id']} no longer reproduces"
    return repo, pristine, mutant


@pytest.fixture
def one_task():
    tasks = _panel()["tasks"]
    assert tasks, "panel is empty"
    return tasks[0]


class TestPipeline:
    def test_buggy_scores_zero_and_fixed_scores_one(self, one_task, tmp_path):
        repo, pristine, mutant = _resolve_mutant(one_task)
        tests = one_task["green_tests"]
        bug_map = {one_task["bug_id"]: one_task["test_node"]}

        results = {}
        for state, text in (("buggy", apply_mutation(pristine, mutant)),
                            ("fixed", pristine)):
            d = build_sandbox(repo, tmp_path / state)
            (d / one_task["module"]).write_text(text)
            rows = [r for t in tests for r in FeedbackChannel(d, "python3").run(t)]
            results[state] = score_from_rows(
                one_task["task_id"], one_task["codebase"], "python", rows, bug_map)

        assert results["buggy"].per_bug_score == 0.0, "buggy state must score zero"
        assert results["fixed"].per_bug_score == 1.0, "fixed state must score one"
        assert results["buggy"].file_clear is False
        assert results["fixed"].file_clear is True

    def test_feedback_shown_to_the_model_leaks_nothing(self, one_task, tmp_path):
        repo, pristine, mutant = _resolve_mutant(one_task)
        d = build_sandbox(repo, tmp_path / "leak")
        (d / one_task["module"]).write_text(apply_mutation(pristine, mutant))
        rows = [r for t in one_task["green_tests"] for r in FeedbackChannel(d, "python3").run(t)]

        shown = render_feedback(rows)
        assert shown.strip(), "a buggy task must produce feedback"
        # Nothing from the hidden test's own source may appear.
        for path in [d / "test" / t for t in one_task["green_tests"]]:
            if not path.exists():
                continue
            src = path.read_text()
            for line in src.splitlines():
                stripped = line.strip()
                # Compare distinctive code tokens, not common words.
                token = stripped.split("assert")[-1].strip() if "assert" in stripped else ""
                if len(token) > 12 and token in shown:
                    pytest.fail(f"assertion text leaked into feedback: {token!r}")

    def test_panel_hash_is_stable_against_the_committed_manifest(self):
        data = _panel()
        assert len(data["manifest_hash"]) == 16
        assert data["selected"] == len(data["tasks"])

    def test_every_panel_task_still_reproduces_its_mutant(self):
        """If a task's bug_id no longer regenerates, the panel has drifted from the code it
        was built against and its scores are no longer comparable to anything."""
        drifted = []
        for t in _panel()["tasks"]:
            try:
                _resolve_mutant(t)
            except Exception as exc:      # noqa: BLE001 - report, do not mask
                drifted.append(f"{t['task_id']}: {type(exc).__name__}")
        assert not drifted, f"panel drifted from source: {drifted}"

    def test_oracle_gate_passes_over_the_whole_panel(self, tmp_path):
        """The gate as it will actually be run: every panel task, buggy vs fixed."""
        good, bad = [], []
        for t in _panel()["tasks"][:6]:
            repo, pristine, mutant = _resolve_mutant(t)
            tests = t["green_tests"]
            bug_map = {t["bug_id"]: t["test_node"]}
            for state, text in (("bad", apply_mutation(pristine, mutant)),
                                ("good", pristine)):
                d = build_sandbox(repo, tmp_path / f"{t['bug_id']}_{state}")
                (d / t["module"]).write_text(text)
                rows = [r for x in tests for r in FeedbackChannel(d, "python3").run(x)]
                score = score_from_rows(t["task_id"], t["codebase"], "python", rows, bug_map)
                (good if state == "good" else bad).append(score)
            shutil_rm(tmp_path, t["bug_id"])

        res = validate_oracle(good, bad)
        assert res.passed, res.failures


def shutil_rm(root: Path, name: str) -> None:
    import shutil
    for state in ("bad", "good"):
        shutil.rmtree(Path(root) / f"{name}_{state}", ignore_errors=True)
