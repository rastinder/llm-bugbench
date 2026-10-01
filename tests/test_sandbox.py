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
