"""Building a runnable sandbox for one task, cheaply and hermetically.

Mutant and commit trials both need a scratch copy of a real repository. Copying the
directory wholesale is not viable: several of these repos carry a multi-gigabyte `venv`,
and the first probe filled a 250 GB tmpfs before finishing. The copy is therefore
source-only, with heavy interpreter directories symlinked so imports still resolve.

The sandbox is also the natural place to hold the isolation the design calls for, so the
graders run under a scrubbed environment: fixed timezone and locale, no inherited
`PYTHONPATH` that could smuggle in the real tree, and no bytecode written.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

#: Copied never -- caches and VCS metadata are irrelevant to a test run and huge.
IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".git", "node_modules", ".tox", ".coverage", "htmlcov",
)

#: Symlinked rather than copied: large, and required on the import path.
LINK = ("venv", ".venv", "node_modules", "env")

#: Skipped entirely: large, and never needed to import or run a test.
SKIP = (".git", "node_modules", "models", ".cache", "dist", "build", "target",
        ".pytest_cache", "__pycache__", "screenshots", "downloads", "output",
        # Browser state: enormous, live sockets and lock files that cannot be copied
        # (copytree raises ENXIO/EINVAL on SingletonSocket), and never imported by a test.
        "shared-profile", "chrome-profile", "profile", "Default", "Crashpad",
        ".browser-profile")

#: Directories whose *contents* are runtime output rather than source. These accumulate
#: without bound -- one repo carried 4.8 GB of generated reports in `data/`, which was
#: copied into every mutant sandbox until a disk filled. Recursive skip patterns mean a
#: nested `data/` is skipped too, so this cannot be side-stepped by a subproject layout.
SKIP_ANYWHERE = ("data", "logs", "var", "tmp", ".cache", "cache", "coverage",
                 "fixtures_large", "snapshots", "artifacts", "reports")

#: Any single directory larger than this is not source we need.
MAX_DIR_BYTES = 8 * 1024 * 1024


def _skippable(item: Path) -> bool:
    """True for anything we must not copy: sockets, fifos, devices, dead symlinks."""
    try:
        if item.is_symlink() and not item.exists():
            return True
        import stat
        mode = item.lstat().st_mode
        if stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode) or stat.S_ISBLK(mode) or stat.S_ISCHR(mode):
            return True
    except OSError:
        return True
    return False


def _copy_filter(src_dir: str, names: list[str]) -> set[str]:
    """shutil copytree filter: drop caches, runtime-output dirs, and anything huge."""
    dropped = set()
    for name in names:
        if name in _IGNORE_NAMES or name in SKIP_ANYWHERE or name.startswith("."):
            dropped.add(name)
            continue
        path = Path(src_dir) / name
        if path.is_dir() and not path.is_symlink():
            try:
                if dir_size(path) > MAX_DIR_BYTES:
                    dropped.add(name)
            except OSError:
                dropped.add(name)
    return dropped


#: Literal names dropped during the recursive copy. Expressed as a plain set because
#: shutil.ignore_patterns is a factory and exposes no `.patterns` attribute.
_IGNORE_NAMES = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".git",
    "node_modules", ".coverage", "htmlcov", "venv", ".venv",
}


def build_sandbox(repo: Path, dest: Path, drop_tests: bool = False) -> Path:
    """Materialise a cheap, runnable copy of ``repo`` at ``dest``.

    Source files are copied; ``venv``/``node_modules`` are symlinked so imports resolve
    without duplicating gigabytes; runtime-output directories and anything oversized are
    skipped.

    ``drop_tests`` removes every test file from the copy. This is not hygiene, it is the
    isolation boundary: a copy handed to an agent that still contains the repository's own
    test suite hands over the answers to any task drawn from that repository. The grader
    keeps its tests (it needs them); the agent's workspace must not have them at all, so
    there is nothing to deny rather than something to protect.
    """
    repo, dest = Path(repo), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    for item in repo.iterdir():
        name = item.name
        if name in SKIP or name in SKIP_ANYWHERE:
            continue
        if _skippable(item):
            continue
        if item.is_dir():
            if name in LINK:
                target = dest / name
                if not target.exists():
                    os.symlink(item.resolve(), target)
                continue
            try:
                if dir_size(item, skip_prunable=True) > MAX_DIR_BYTES:
                    continue
            except OSError:
                continue
            shutil.copytree(item, dest / name, ignore=_copy_filter, dirs_exist_ok=True)
        else:
            if name.endswith((".pyc", ".pyo")):
                continue
            shutil.copy2(item, dest / name)

    if drop_tests:
        for path in sorted(dest.rglob("*"), reverse=True):
            if path.is_file() and _is_test_file(path):
                path.unlink()
            elif path.is_dir() and _looks_like_test_dir(path):
                shutil.rmtree(path, ignore_errors=True)
    return dest


def _is_test_file(path: Path) -> bool:
    name = path.name
    return (name.startswith("test_") or name.endswith(("_test.py", ".test.js",
            ".spec.ts", ".test.ts", "_test.go"))
            or name in {"conftest.py"})


def _looks_like_test_dir(path: Path) -> bool:
    return path.name in {"tests", "test", "spec", "specs", "__tests__"}


def dir_size(path: Path, skip_prunable: bool = False) -> int:
    """Total bytes under ``path``, not following symlinks (they are shared).

    With ``skip_prunable`` the traversal ignores SKIP_ANYWHERE directories, so a source
    directory that merely *contains* a 9 MB `data/` blob is not itself judged oversized --
    otherwise the whole package would be discarded along with the blob it was pruning.
    """
    total = 0
    root = Path(path)
    for dirpath, dirnames, filenames in os.walk(root):
        if skip_prunable:
            dirnames[:] = [d for d in dirnames if d not in SKIP_ANYWHERE]
        for f in filenames:
            p = Path(dirpath) / f
            try:
                if p.is_symlink():
                    continue
                total += p.stat().st_size
            except OSError:
                continue
    return total


def grader_env(extra: dict | None = None) -> dict:
    """A deterministic, leak-resistant environment for grading.

    Inheriting the caller's ``PYTHONPATH`` would let the real (unmutated) source tree
    shadow the sandbox and silently turn every mutant green, so it is dropped rather than
    extended.
    """
    env = {
        k: v for k, v in os.environ.items()
        if k not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONDONTWRITEBYTECODE"}
    }
    env.update({
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "TZ": "UTC",
        "LC_ALL": "C",
        "LANG": "C",
        "NO_COLOR": "1",
        "COLUMNS": "80",
        "TERM": "dumb",
    })
    if extra:
        env.update(extra)
    return env


#: Backwards-compatible alias used by the tests.
size_bytes = dir_size
