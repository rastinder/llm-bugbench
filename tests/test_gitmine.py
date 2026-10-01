"""Tests for mining real fix commits into benchmark tasks."""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from bugbench.gitmine import (
    CommitTriple,
    build_triple,
    changed_files,
    export_state,
    is_test_path,
    list_fix_commits,
)


@pytest.fixture
def fake_repo(tmp_path):
    """A git repo shaped like the real thing: one fix commit with source + tests,
    one test-only commit, one docs commit."""
    repo = tmp_path / "demo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "tests").mkdir()

    def run(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True,
                       capture_output=True)

    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")

    (repo / "pkg" / "mod.py").write_text("def score(x):\n    return x * 0.6\n")
    (repo / "README.md").write_text("demo\n")
    run("add", "-A")
    run("commit", "-qm", "chore: initial")

    # the fix commit: source change + a test that pins the new behaviour
    (repo / "pkg" / "mod.py").write_text("def score(x):\n    return x\n")
    (repo / "pkg" / "tests" / "test_mod.py").write_text(
        "from pkg.mod import score\n\n\ndef test_identity():\n    assert score(3) == 3\n"
    )
    run("add", "-A")
    run("commit", "-qm", "fix(mod): score must not attenuate the input")

    # test-only commit -- no source change, so not a benchmark task
    (repo / "pkg" / "tests" / "test_extra.py").write_text("def test_x():\n    assert True\n")
    run("add", "-A")
    run("commit", "-qm", "test: extra coverage")

    # docs-only commit
    (repo / "README.md").write_text("demo\n\nmore\n")
    run("add", "-A")
    run("commit", "-qm", "docs: expand readme")

    return repo


class TestIsTestPath:
    @pytest.mark.parametrize("p,expected", [
        ("pkg/tests/test_mod.py", True),
        ("pkg/test_mod.py", True),
        ("tests/helper.js", True),
        ("src/x.spec.ts", True),
        ("src/widget.test.jsx", True),
        ("pkg/mod.py", False),
        ("src/main.js", False),
        ("docs/guide.md", False),
    ])
    def test_classification(self, p, expected):
        assert is_test_path(p) is expected


class TestListFixCommits:
    def test_finds_only_fix_subjects(self, fake_repo):
        found = list_fix_commits(fake_repo)
        subjects = [s for _, s in found]

        assert len(subjects) == 1, subjects
        assert subjects[0].startswith("fix(mod)")

    def test_excludes_docs_and_test_only(self, fake_repo):
        subjects = " ".join(s for _, s in list_fix_commits(fake_repo))

        assert "docs:" not in subjects
        assert "test: extra" not in subjects


class TestBuildTriple:
    def test_separates_source_from_tests(self, fake_repo):
        sha, subject = list_fix_commits(fake_repo)[0]
        t = build_triple(fake_repo, "r/demo", sha, subject)

        assert t.source_files == ["pkg/mod.py"]
        assert t.test_files == ["pkg/tests/test_mod.py"]

    def test_test_only_commit_is_rejected(self, fake_repo):
        log = subprocess.run(["git", "-C", str(fake_repo), "log", "--format=%H\t%s"],
                             capture_output=True, text=True).stdout
        sha, subject = next(
            (l.split("\t")[0], l.split("\t")[1]) for l in log.splitlines()
            if "extra coverage" in l
        )
        t = build_triple(fake_repo, "r/demo", sha, subject)

        assert not t.verified
        assert "no source file" in t.reject_reason

    def test_task_id_is_stable_and_namespaced(self, fake_repo):
        sha, subject = list_fix_commits(fake_repo)[0]
        t = build_triple(fake_repo, "r/demo", sha, subject)

        assert t.task_id.startswith("r_demo__")
        assert sha[:10] in t.task_id

    def test_round_trips_through_dict(self, fake_repo):
        sha, subject = list_fix_commits(fake_repo)[0]
        t = build_triple(fake_repo, "r/demo", sha, subject)

        assert CommitTriple(**{k: v for k, v in t.as_dict().items()
                               if k in CommitTriple.__dataclass_fields__}).task_id == t.task_id


class TestExportState:
    def test_buggy_tree_lacks_the_fix(self, fake_repo, tmp_path):
        sha, subject = list_fix_commits(fake_repo)[0]
        t = build_triple(fake_repo, "r/demo", sha, subject)

        out = export_state(fake_repo, f"{sha}^", tmp_path / "buggy", t.test_files)
        assert (out / "code" / "pkg" / "mod.py").read_text() == "def score(x):\n    return x * 0.6\n"

    def test_fixed_tree_has_the_fix(self, fake_repo, tmp_path):
        sha, subject = list_fix_commits(fake_repo)[0]
        t = build_triple(fake_repo, "r/demo", sha, subject)

        out = export_state(fake_repo, sha, tmp_path / "fixed", t.test_files)
        assert (out / "code" / "pkg" / "mod.py").read_text() == "def score(x):\n    return x\n"

    def test_tests_are_written_to_the_separate_test_dir(self, fake_repo, tmp_path):
        """Tests must never sit beside the source: the runner mounts them separately
        and the model must not be able to read them."""
        sha, subject = list_fix_commits(fake_repo)[0]
        t = build_triple(fake_repo, "r/demo", sha, subject)

        out = export_state(fake_repo, sha, tmp_path / "fixed", t.test_files)

        assert (out / "test" / "pkg" / "tests" / "test_mod.py").exists()
        assert not (out / "code" / "pkg" / "tests").exists()

    def test_exported_oracle_fails_buggy_and_passes_fixed(self, fake_repo, tmp_path):
        """The admission criterion that makes a triple usable at all."""
        from bugbench.feedback import FeedbackChannel, render_feedback

        sha, subject = list_fix_commits(fake_repo)[0]
        t = build_triple(fake_repo, "r/demo", sha, subject)

        buggy = export_state(fake_repo, f"{sha}^", tmp_path / "b", t.test_files)
        fixed = export_state(fake_repo, sha, tmp_path / "f", t.test_files)

        b = render_feedback(FeedbackChannel(buggy, "python3").run("test/pkg/tests/test_mod.py"))
        f = render_feedback(FeedbackChannel(fixed, "python3").run("test/pkg/tests/test_mod.py"))

        assert b.strip(), "oracle must FAIL on the buggy tree"
        assert f == "", "oracle must PASS on the fixed tree"
