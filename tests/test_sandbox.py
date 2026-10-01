import os, pathlib, shutil, subprocess, sys
sys.path.insert(0, "/home/ras/llm-bugbench/src")
from bugbench.sandbox import build_sandbox, grader_env, size_bytes
import pytest

def test_sandbox_is_small_and_runnable(tmp_path):
    src = tmp_path / "repo"
    (src / "pkg").mkdir(parents=True)
    (src / "venv" / "lib").mkdir(parents=True)
    (src / "venv" / "lib" / "big.bin").write_bytes(b"x" * 5_000_000)
    (src / "pkg" / "m.py").write_text("def f():\n    return 1\n")
    (src / "pkg" / "__init__.py").write_text("")
    (src / "test_f.py").write_text("from pkg.m import f\n\n\ndef test_f():\n    assert f() == 1\n")
    sb = build_sandbox(src, tmp_path / "sb")
    assert (sb / "pkg" / "m.py").exists()
    assert (sb / "venv").is_symlink(), "heavy dirs are symlinked, not copied"
    assert size_bytes(sb) < 100_000, "sandbox must not duplicate the venv"

def test_sandbox_runs_tests(tmp_path):
    src = tmp_path / "repo"; (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "m.py").write_text("def f():\n    return 1\n")
    (src / "pkg" / "__init__.py").write_text("")
    (src / "test_f.py").write_text("from pkg.m import f\n\n\ndef test_f():\n    assert f() == 1\n")
    sb = build_sandbox(src, tmp_path / "sb")
    (sb / "pkg" / "m.py").write_text("def f():\n    return 2\n")
    r = subprocess.run([sys.executable, "-m", "pytest", "test_f.py", "-q", "--tb=no"],
                       cwd=sb, capture_output=True, text=True, env=grader_env(), timeout=120)
    assert r.returncode != 0, "mutated source must fail its test"

def test_grader_env_is_deterministic_and_scrubbed():
    env = grader_env()
    assert env["TZ"] == "UTC" and env["LC_ALL"] == "C"
    assert env["PYTHONHASHSEED"] == "0"
    assert "PYTHONPATH" not in env, "inherited PYTHONPATH could shadow the sandbox"

def test_skips_vcs_and_caches(tmp_path):
    src = tmp_path / "repo"; src.mkdir()
    (src / ".git").mkdir(); (src / ".git" / "HEAD").write_text("ref")
    (src / "a.py").write_text("x=1")
    sb = build_sandbox(src, tmp_path / "sb")
    assert not (sb / ".git").exists()
    assert (sb / "a.py").exists()


def test_skips_browser_profiles_and_sockets(tmp_path):
    """A Chromium profile holds live sockets and lock files that copytree cannot read
    (ENXIO on SingletonSocket). They are also enormous and never imported by a test."""
    import socket
    src = tmp_path / "repo"; src.mkdir()
    (src / "a.py").write_text("x=1")
    prof = src / "shared-profile"; prof.mkdir()
    (prof / "Cookies").write_text("junk")
    s = socket.socket(socket.AF_UNIX); s.bind(str(prof / "SingletonSocket"))
    sb = build_sandbox(src, tmp_path / "sb")
    s.close()
    assert (sb / "a.py").exists()
    assert not (sb / "shared-profile").exists()

def test_survives_a_dead_symlink(tmp_path):
    src = tmp_path / "repo"; src.mkdir()
    (src / "a.py").write_text("x=1")
    (src / "broken").symlink_to("/nonexistent/path")
    sb = build_sandbox(src, tmp_path / "sb")
    assert (sb / "a.py").exists()


def test_skips_runtime_output_dirs(tmp_path):
    """Regression: one repo carried 4.8 GB of generated reports under data/, which was
    copied into every mutant sandbox until the disk filled."""
    src = tmp_path / "repo"; src.mkdir()
    (src / "a.py").write_text("x=1")
    data = src / "data"; data.mkdir()
    (data / "huge.bin").write_bytes(b"x" * 9_000_000)   # over MAX_DIR_BYTES
    sb = build_sandbox(src, tmp_path / "sb")
    assert (sb / "a.py").exists()
    assert not (sb / "data").exists()


def test_skips_nested_runtime_dirs(tmp_path):
    src = tmp_path / "repo"; (src / "pkg" / "data").mkdir(parents=True)
    (src / "pkg" / "m.py").write_text("x=1")
    (src / "pkg" / "data" / "junk.bin").write_bytes(b"x" * 9_000_000)
    sb = build_sandbox(src, tmp_path / "sb")
    assert (sb / "pkg" / "m.py").exists()
    assert not (sb / "pkg" / "data").exists()


def test_sandbox_stays_small_for_a_data_heavy_repo(tmp_path):
    src = tmp_path / "repo"; (src / "data").mkdir(parents=True)
    (src / "data" / "blob.bin").write_bytes(b"x" * 20_000_000)
    (src / "m.py").write_text("x=1")
    sb = build_sandbox(src, tmp_path / "sb")
    assert size_bytes(sb) < 1_000_000


class TestDropTests:
    """The agent's workspace must contain no test file at all, so there is nothing to deny."""

    def _repo(self, tmp_path):
        src = tmp_path / "repo"; (src / "pkg" / "tests").mkdir(parents=True)
        (src / "pkg" / "m.py").write_text("x=1\n")
        (src / "pkg" / "__init__.py").write_text("")
        (src / "pkg" / "tests" / "test_m.py").write_text("def test_x(): assert True\n")
        (src / "test_top.py").write_text("def test_y(): assert True\n")
        (src / "conftest.py").write_text("import sys\n")
        (src / "spec.js.test.ts").write_text("test('a',()=>{})\n")
        return src

    def test_tests_are_removed_when_requested(self, tmp_path):
        sb = build_sandbox(self._repo(tmp_path), tmp_path / "sb", drop_tests=True)
        assert not list(sb.rglob("test_*.py"))
        assert not list(sb.rglob("conftest.py"))
        assert not (sb / "spec.js.test.ts").exists()

    def test_source_survives_the_removal(self, tmp_path):
        sb = build_sandbox(self._repo(tmp_path), tmp_path / "sb", drop_tests=True)
        assert (sb / "pkg" / "m.py").read_text() == "x=1\n"
        assert (sb / "pkg" / "__init__.py").exists()

    def test_grading_sandbox_keeps_its_tests_by_default(self, tmp_path):
        """The grader needs the tests; dropping them by default would silently disable
        every oracle."""
        sb = build_sandbox(self._repo(tmp_path), tmp_path / "sb")
        assert list(sb.rglob("test_*.py"))
