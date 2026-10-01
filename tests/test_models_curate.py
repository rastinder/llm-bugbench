"""Block 1 + 2: task store loading, validation, and curation."""
from __future__ import annotations

import json

import pytest

from bugbench.models import Task, TaskStoreError, load_tasks

from conftest import (
    BUGGY_ARITH, BUGGY_REGEX, BUGGY_RENAME, FIXED_ARITH, FIXED_REGEX,
    FIXED_RENAME, make_task, write_task_file,
)

REQUIRED = ["task_id", "language", "goal", "buggy", "reference_fix"]


# ---------------------------------------------------------------- store
def test_load_tasks_reads_all_records(tmp_path):
    write_task_file(tmp_path, [make_task(tmp_path, "B0001"),
                               make_task(tmp_path, "B0002")])
    tasks = load_tasks(str(tmp_path / "tasks.jsonl"))
    assert [t.task_id for t in tasks] == ["B0001", "B0002"]
    assert isinstance(tasks[0], Task)


@pytest.mark.parametrize("field", REQUIRED)
def test_load_tasks_rejects_record_missing_required_field(tmp_path, field):
    t = make_task(tmp_path).to_dict()
    t.pop(field)
    p = write_task_file(tmp_path, [t])
    with pytest.raises(TaskStoreError) as e:
        load_tasks(str(p))
    assert field in str(e.value)


def test_load_tasks_rejects_duplicate_task_id(tmp_path):
    write_task_file(tmp_path, [make_task(tmp_path, "B0001"),
                               make_task(tmp_path, "B0001")])
    with pytest.raises(TaskStoreError) as e:
        load_tasks(str(tmp_path / "tasks.jsonl"))
    assert "duplicate" in str(e.value).lower()


def test_load_tasks_missing_file_raises(tmp_path):
    with pytest.raises(TaskStoreError):
        load_tasks(str(tmp_path / "nope.jsonl"))


def test_load_tasks_tolerates_blank_lines(tmp_path):
    t = make_task(tmp_path).to_dict()
    p = tmp_path / "tasks.jsonl"
    p.write_text("\n" + json.dumps(t) + "\n\n")
    assert len(load_tasks(str(p))) == 1


# ---------------------------------------------------------------- curation
from bugbench.curate import heuristic_category, curate  # noqa: E402


def test_heuristic_labels_missing_empty_guard_as_bug_fix():
    cat, conf, reason = heuristic_category(
        make_task(tmp_path=None) if False else {
            "goal": "average() throws ZeroDivisionError on an empty list",
            "buggy": BUGGY_ARITH, "reference_fix": FIXED_ARITH,
            "file_name": "mod.py"})
    assert cat == "bug_fix"
    assert 0.0 < conf <= 1.0
    assert reason


def test_heuristic_labels_pure_rename_as_refactor():
    cat, conf, _ = heuristic_category({
        "goal": "rename variables for clarity",
        "buggy": BUGGY_RENAME, "reference_fix": FIXED_RENAME,
        "file_name": "price.py"})
    assert cat == "refactor"


def test_heuristic_labels_description_only_change_as_documentation():
    buggy = ('TOOL_DESCRIPTION = (\n    "Ask GLM 5.3 via web chat browser automation"\n'
             '    " (https://chatglm.cn/)"\n)\n')
    fixed = ('TOOL_DESCRIPTION = (\n    "Ask GLM 5.3 via web chat browser automation"\n'
             '    " (https://chatglm.cn/). This is the glm-latest council seat."\n)\n')
    cat, _, _ = heuristic_category({
        "goal": "describe the seat clearly",
        "buggy": buggy, "reference_fix": fixed, "file_name": "mcp-server.js"})
    assert cat == "documentation"


def test_heuristic_labels_new_symbol_call_without_guard_as_feature():
    buggy = "ctx = launch_persistent_context(\n    PROFILE_DIR,\n    headless=False,\n)\n"
    fixed = ("ctx = launch_persistent_context(\n    PROFILE_DIR,\n    headless=False,\n"
             "    stealth_args=True,\n)\n")
    cat, _, _ = heuristic_category({
        "goal": "make the browser harder to detect",
        "buggy": buggy, "reference_fix": fixed, "file_name": "daemon.py"})
    assert cat == "feature_addition"


def test_heuristic_labels_ignored_stage_as_bug_fix():
    buggy = ("first = stage(process, 180)\n"
             "if first.get('stage') != 'ready_for_live':\n"
             "    raise RuntimeError('did not reach live')\n"
             "transaction = first['transaction']\n")
    fixed = ("first = stage(process, 180)\n"
             "if first.get('stage') == 'failure':\n"
             "    raise RuntimeError('remote transaction failed')\n"
             "if first.get('stage') != 'ready_for_live':\n"
             "    raise RuntimeError('did not reach live')\n"
             "transaction = first['transaction']\n")
    cat, _, _ = heuristic_category({
        "goal": "a failed deploy must abort instead of continuing",
        "buggy": buggy, "reference_fix": fixed, "file_name": "deploy.py"})
    assert cat == "bug_fix"


def test_heuristic_detects_regex_only_first_match_bug():
    cat, _, _ = heuristic_category({
        "goal": "is_safe misses a later banned claim after a negated one",
        "buggy": BUGGY_REGEX, "reference_fix": FIXED_REGEX, "file_name": "safety.py"})
    assert cat == "bug_fix"


def test_curate_labels_every_task_and_keeps_non_bug_tasks(tmp_path):
    tasks = [
        make_task(tmp_path, "B0001", category="unclassified", classifier=""),
        make_task(tmp_path, "B0002", buggy=BUGGY_RENAME, fixed=FIXED_RENAME,
                  goal="rename args for clarity", category="unclassified",
                  classifier=""),
    ]
    out = curate(tasks, llm=None)
    assert len(out) == 2
    assert all(t.category for t in out)
    assert all(t.classifier == "heuristic" for t in out)
    assert out[1].category == "refactor"


def test_curated_tasks_never_overwrite_an_existing_label(tmp_path):
    t = make_task(tmp_path, "B0001", category="feature_addition",
                  category_reason="already curated by hand")
    out = curate([t], llm=None)
    assert out[0].category == "feature_addition"
    assert out[0].category_reason == "already curated by hand"


def test_primary_selection_excludes_non_bug_fixes(tmp_path):
    tasks = [make_task(tmp_path, "B0001"),
             make_task(tmp_path, "B0002", category="documentation"),
             make_task(tmp_path, "B0003", category="refactor")]
    primary = curate(tasks, llm=None)
    ids = [t.task_id for t in primary if t.category == "bug_fix"]
    assert ids == ["B0001"]


# ---------------------------------------------------------------- goal relevance
from bugbench.models import goal_relevance, primary_tasks  # noqa: E402


def test_goal_relevance_is_high_when_goal_names_symbol_and_defect():
    t = make_task(None, goal="average() throws ZeroDivisionError on an empty list")
    assert goal_relevance(t) >= 0.6


def test_goal_relevance_is_low_for_a_generic_standing_instruction():
    t = make_task(None, goal="do whatever u like, we have a vps that can help")
    assert goal_relevance(t) < 0.6


def test_goal_relevance_is_zero_for_an_empty_goal():
    assert goal_relevance(make_task(None, goal="")) == 0.0


def test_primary_tasks_excludes_bug_fixes_with_irrelevant_goals():
    good = make_task(None, task_id="P1",
                     goal="average() throws ZeroDivisionError on an empty list")
    bad = make_task(None, task_id="P2", goal="do whatever u like")
    notbug = make_task(None, task_id="P3", category="refactor",
                       goal="rename for clarity")
    tasks = [good, bad, notbug]
    for t in tasks:
        t.goal_relevance = goal_relevance(t)
    assert [t.task_id for t in primary_tasks(tasks)] == ["P1"]


def test_primary_tasks_accepts_a_synthesised_defect_specific_goal():
    t = make_task(None, task_id="P4", goal="Lists open files in /proc Defect: never "
                                            "iterates the fd directory Trigger: >1 fd")
    t.goal_source = "synth"
    t.goal_relevance = 0.0
    assert [x.task_id for x in primary_tasks([t])] == ["P4"]
