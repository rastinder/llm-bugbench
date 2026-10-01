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


def build_sandbox(repo: Path, dest: Path) -> Path:
    """Materialise a cheap, runnable copy of ``repo`` at ``dest``.

    Source files are copied; ``venv``/``node_modules`` are symlinked so imports resolve
    without duplicating gigabytes; everything else large is skipped.
    """
    repo, dest = Path(repo), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    for item in repo.iterdir():
        name = item.name
        if name in SKIP:
            continue
        if _skippable(item):
            continue
        if item.is_dir():
            if name in LINK:
                target = dest / name
                if not target.exists():
                    os.symlink(item.resolve(), target)
                continue
            shutil.copytree(item, dest / name, ignore=IGNORE, dirs_exist_ok=True)
        else:
            if name.endswith((".pyc", ".pyo")):
                continue
            shutil.copy2(item, dest / name)
    return dest


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


def size_bytes(path: Path) -> int:
    """Recursive size, counting symlinked targets as negligible (they are shared)."""
    total = 0
    for p in Path(path).rglob("*"):
        try:
            if p.is_symlink():
                continue
            total += p.stat().st_size
        except OSError:
            continue
    return total
