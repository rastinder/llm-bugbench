"""Tests for test isolation.

The benchmark's validity rests entirely on the model being unable to read the tests that
judge it. These tests therefore assert the negative -- that reads FAIL -- because a
positive assertion ("the file can be read by the grader") proves nothing about the
property that matters.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from pathlib import Path

from bugbench.isolation import (
    assert_unreadable, drop_privileges, install_read_denial, seal_test_tree,
)


@pytest.fixture
def secret(tmp_path):
    root = tmp_path / "test"
    root.mkdir()
    f = root / "test_hidden.py"
    f.write_text('CANARY = "CANARY_7f3a91"\ndef test_x():\n    assert True\n')
    return root


class TestAuditHook:
    def test_denies_reading_the_protected_file(self, secret):
        target = secret / "test_hidden.py"
        install_read_denial([secret])

        with pytest.raises(PermissionError):
            open(target).read()

    def test_canary_never_reaches_the_caller(self, secret):
        install_read_denial([secret])
        try:
            data = open(secret / "test_hidden.py").read()
        except PermissionError:
            data = ""
        assert "CANARY_7f3a91" not in data

    def test_unprotected_files_stay_readable(self, tmp_path):
        ok = tmp_path / "code.py"
        ok.write_text("x = 1\n")
        install_read_denial([tmp_path / "test"])

        assert ok.read_text() == "x = 1\n"

    def test_relative_paths_cannot_bypass_it(self, secret, monkeypatch):
        """Comparing raw strings is defeated by ./ and by symlinks, so both sides are
        resolved before comparison."""
        target = secret / "test_hidden.py"
        install_read_denial([secret])
        monkeypatch.chdir(secret)

        for attempt in ("./test_hidden.py", str(target)):
            with pytest.raises(PermissionError):
                open(attempt).read()

    def test_relative_protected_path_still_blocks(self, secret, monkeypatch):
        monkeypatch.chdir(secret.parent)
        install_read_denial([Path("test")])
        target = secret / "test_hidden.py"

        with pytest.raises(PermissionError):
            open(target).read()

    def test_grader_may_still_read_before_the_hook_is_installed(self, secret):
        """The hook is installed after parsing; the grader must be able to read the tests
        up to that point, or nothing would ever run."""
        target = secret / "test_hidden.py"
        assert "CANARY_7f3a91" in target.read_text()

        install_read_denial([secret])
        with pytest.raises(PermissionError):
            open(target).read()

    def test_hook_survives_repeated_installs(self, secret):
        install_read_denial([secret])
        install_read_denial([secret])
        with pytest.raises(PermissionError):
            open(secret / "test_hidden.py").read()

    def test_report_names_the_protected_paths(self, secret):
        rep = install_read_denial([secret])
        assert rep.audit_hook_installed
        assert any(p.endswith("test_hidden.py") or p.endswith("test")
                   for p in rep.protected_paths)


class TestSealing:
    def test_sealed_tree_is_mode_700(self, secret):
        seal_test_tree(secret)
        assert oct(secret.stat().st_mode)[-3:] == "700"

    def test_sealed_files_are_mode_600(self, secret):
        seal_test_tree(secret)
        assert oct((secret / "test_hidden.py").stat().st_mode)[-3:] == "600"

    def test_absent_tree_is_reported_not_fatal(self, tmp_path):
        out = seal_test_tree(tmp_path / "nope")
        assert out["applied"] is False

    def test_assert_unreadable_raises_when_readable(self, secret):
        with pytest.raises(AssertionError):
            assert_unreadable(secret / "test_hidden.py")

    def test_assert_unreadable_passes_when_denied(self, secret, tmp_path):
        install_read_denial([secret])
        try:
            open(secret / "test_hidden.py").read()
        except PermissionError:
            assert_unreadable(secret / "test_hidden.py")
        else:
            pytest.fail("the hook did not deny the read")


class TestSubprocessEscape:
    """The hook's known hole: a fresh interpreter has no hook installed."""

    def test_a_child_interpreter_does_not_inherit_the_hook(self, secret):
        install_read_denial([secret])
        target = secret / "test_hidden.py"
        r = subprocess.run(
            [sys.executable, "-c", f"print(open({str(target)!r}).read())"],
            capture_output=True, text=True,
        )
        # The hook does NOT protect here -- which is exactly why drop_privileges exists.
        assert r.returncode == 0 and "CANARY_7f3a91" in r.stdout

    def test_privilege_drop_is_the_actual_boundary(self, secret):
        """With the tree sealed and privileges dropped, even a fresh interpreter is
        refused -- proving the uid layer, not the hook, is the real control."""
        if os.getuid() != 0:
            pytest.skip("needs root to drop privileges")
        seal_test_tree(secret, owner_uid=0)
        assert drop_privileges(10002, 10002)
        try:
            r = subprocess.run(
                [sys.executable, "-c", f"open({str(secret / 'test_hidden.py')!r}).read()"],
                capture_output=True, text=True,
            )
            assert r.returncode != 0, "a child interpreter must not read the test tree"
        finally:
            os.setuid(0)
            os.setgid(0)


class TestDropPrivileges:
    def test_reports_honestly_when_not_root(self):
        if os.getuid() == 0:
            pytest.skip("running as root")
        assert drop_privileges(os.getuid(), os.getgid()) is True

    def test_uid_is_unprivileged(self):
        if os.getuid() != 0:
            pytest.skip("needs root")
        assert drop_privileges(10002, 10002)
        try:
            assert os.getuid() == 10002
            assert os.geteuid() == 10002
        finally:
            os.setuid(0)
            os.setgid(0)
