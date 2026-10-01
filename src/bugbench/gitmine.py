"""Mining real fix commits into benchmark tasks.

The DB replay path was measured and abandoned (see PLAN.md: 1 clean replay out of 163
candidate files). Git history is the replacement source, and AutoPilot-Jobs is unusually
well suited to it -- 42 of its 88 commits are fixes, the repo is private, and every fix
commit ships its own tests. That means a commit already *is* the triple we need:

    parent tree  -> buggy state (the defect is present)
    commit tree  -> fixed state (the defect is gone)
    commit tests -> the oracle, written and believed by the original author

This module only extracts and verifies those triples. It never invents them, and a
commit whose tests do not actually discriminate buggy from fixed is rejected rather
than admitted with a weak oracle -- a test that passes on both sides proves nothing.
"""
from __future__ import annotations

import re
import sys
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

#: Conventional-commit prefixes that denote a behavioural fix.
FIX_PREFIX = re.compile(r"^(fix|bugfix|hotfix)(\(|:)", re.IGNORECASE)

#: A usable fix touches at least one non-test source file AND ships at least one test.
MIN_SOURCE_FILES = 1
MIN_TEST_FILES = 1

#: A test lives in a tests/spec directory, OR the filename itself marks it as one.
#: The second half matters for JS/TS repos that keep specs beside sources
#: (``src/x.spec.ts``), which a directory-only rule would miss -- and a missed test
#: means a mined "fix commit" is scored with no oracle at all.
_TEST_PATH = re.compile(
    r"(^|/)(tests?|spec|specs)/"          # tests/, test/, spec/, specs/
    r"|(^|/)test_[^/]*\.[a-z]+$"           # test_foo.py
    r"|_test\.[a-z]+$"                     # foo_test.go
    r"|\.(spec|test)\.[jt]sx?$"           # foo.spec.ts, foo.test.jsx
)


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path.replace("\\", "/")))


@dataclass
class CommitTriple:
    """One mined benchmark task, with its oracle and provenance."""

    commit: str
    subject: str
    repo: str
    source_files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    buggy_tree: str = ""
    fixed_tree: str = ""
    verified: bool = False
    reject_reason: str = ""

    @property
    def task_id(self) -> str:
        return f"{self.repo.replace('/', '_')}__{self.commit[:10]}"

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "commit": self.commit,
            "subject": self.subject,
            "repo": self.repo,
            "source_files": self.source_files,
            "test_files": self.test_files,
            "buggy_tree": self.buggy_tree,
            "fixed_tree": self.fixed_tree,
            "verified": self.verified,
            "reject_reason": self.reject_reason,
        }


def _git(repo: Path, *args: str) -> str:
    return _git_bytes(repo, *args).decode("utf-8", errors="replace")


def _git_bytes(repo: Path, *args: str) -> bytes:
    """Run git and return raw bytes.

    Repositories contain binary assets (screenshots, PDFs, .docx fixtures) and decoding
    them as text raises UnicodeDecodeError part-way through an export, which previously
    surfaced as a blanket "export error" and silently cost us 35 of 42 candidate commits.
    """
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, check=False,
    ).stdout


def _is_binary(blob: bytes) -> bool:
    return b"\x00" in blob[:8000]


def list_fix_commits(repo: Path, limit: int | None = None) -> list[tuple[str, str]]:
    """Every commit whose subject declares a fix, newest first."""
    rng = f"-{limit}" if limit else ""
    out = _git(repo, "log", f"--max-count={limit or 100000}", "--format=%H\t%s")
    found = []
    for line in out.splitlines():
        if "\t" not in line:
            continue
        sha, subject = line.split("\t", 1)
        if FIX_PREFIX.match(subject.strip()):
            found.append((sha, subject.strip()))
    return found


def changed_files(repo: Path, commit: str) -> list[str]:
    return [p for p in _git(repo, "show", "--name-only", "--format=", commit).splitlines() if p.strip()]


def build_triple(repo_path: Path, repo_name: str, commit: str, subject: str) -> CommitTriple:
    """Assemble a triple, rejecting anything that cannot serve as a benchmark task."""
    files = changed_files(repo_path, commit)
    tests = [f for f in files if is_test_path(f)]
    sources = [f for f in files if not is_test_path(f) and f.endswith((".py", ".js", ".ts", ".sh"))]

    t = CommitTriple(
        commit=commit,
        subject=subject,
        repo=repo_name,
        source_files=sources,
        test_files=tests,
        buggy_tree=f"{commit}^",
        fixed_tree=commit,
    )

    if len(sources) < MIN_SOURCE_FILES:
        t.reject_reason = "no source file changed (test-only or docs commit)"
    elif len(tests) < MIN_TEST_FILES:
        t.reject_reason = "no test file shipped with the fix (no oracle)"
    return t


    for rel in _git(repo_path, "ls-tree", "-r", "--name-only", treeish).splitlines():
        if not rel.endswith(".py") or is_test_path(rel):
            continue
        pkg_dir = (code_dir / rel).parent
        while pkg_dir != code_dir:          # deliberately stops short of the root
            marker = pkg_dir / "__init__.py"
            if not marker.exists():
                marker.write_text("", encoding="utf-8")
            pkg_dir = pkg_dir.parent


def _write_path_conftest(out_dir: Path) -> None:
    """Put the sys.path shim at the pytest rootdir, not inside ``code/``.

    pytest only auto-loads a conftest that sits at or above the collected test's rootdir,
    so a conftest inside ``code/`` is never loaded and the oracle's
    ``from pkg.mod import score`` fails at collection. Placing it beside ``test/`` fixes
    the import and keeps anything grader-side out of the tree the model can see.
    """
    (out_dir / "conftest.py").write_text(
        "import sys, pathlib\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).parent / 'code'))\n",
        encoding="utf-8",
    )


def export_state(repo_path: Path, treeish: str, out_dir: Path, test_files: list[str]) -> Path:
    """Materialise one tree into ``out_dir``, keeping tests separate from source.

    Tests are written to a sibling ``test/`` directory rather than beside the source,
    because the runner mounts them separately and the model must never see them.
    """
    code_dir = out_dir / "code"
    test_dir = out_dir / "test"
    code_dir.mkdir(parents=True, exist_ok=True)
    test_dir.mkdir(parents=True, exist_ok=True)

    for rel in files_of(repo_path, treeish, test_files):
        dest = test_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_git_bytes(repo_path, "show", f"{treeish}:{rel}"))

    listing = _git(repo_path, "ls-tree", "-r", "--name-only", treeish)
    skipped_binary = 0
    missing: list[str] = []
    for rel in listing.splitlines():
        if is_test_path(rel):
            continue
        blob = _git_bytes(repo_path, "show", f"{treeish}:{rel}")
        if _is_binary(blob):
            # Assets are irrelevant to a Python/JS oracle and cannot be round-tripped
            # through text, so they are left out rather than corrupting the export.
            skipped_binary += 1
            continue
        dest = code_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)

    # An oracle imports whatever the package needs, not just what the commit happened to
    # touch -- "fix(match)" pulls in a sibling rag_resume_extractor. Exporting only the
    # commit's own files produced 34 of 42 triples whose FIXED tree failed to import,
    # which is indistinguishable from a broken fix. The whole non-test tree is exported
    # so the oracle runs against the real repository state.
    missing = _missing_imports(code_dir, test_dir, test_files)
    export_state.missing_imports = missing  # type: ignore[attr-defined]

    _ensure_importable(code_dir, treeish, repo_path)
    _write_path_conftest(out_dir)
    export_state.skipped_binary = skipped_binary  # type: ignore[attr-defined]
    return out_dir


def _missing_imports(code_dir: Path, test_dir: Path, test_files: list[str]) -> list[str]:
    """Top-level modules the oracles import that the export does not contain.

    Reported rather than raised, because a task can still be usable when the missing
    import is optional -- but it is the first thing to check when a triple is rejected
    for the fixed tree failing.
    """
    import re as _re

    names: set[str] = set()
    pattern = _re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w]*)", _re.M)
    for rel in test_files:
        p = test_dir / rel
        if p.exists():
            names.update(pattern.findall(p.read_text(encoding="utf-8", errors="replace")))

    stdlib = set(sys.stdlib_module_names)
    missing = []
    for name in sorted(names):
        if name in stdlib or name in {"pkg", "code", "test", "conftest"}:
            continue
        if (code_dir / name).exists() or (code_dir / f"{name}.py").exists():
            continue
        if (code_dir / name).is_dir() and (code_dir / name / "__init__.py").exists():
            continue
        if any(p.stem == name for p in code_dir.rglob("*.py")):
            continue
        missing.append(name)
    return missing


def _ensure_importable(code_dir: Path, treeish: str, repo_path: Path) -> None:
    """Add ``__init__.py`` where the commit assumed one, so the oracle can import.

    Exported trees are graded from a different working directory than the original repo,
    so implicit namespace packages are not enough: pytest imports the test module, which
    does ``from pkg.mod import score``, and without a package marker that raises
    ImportError -- a collection error, which correctly counts as a failure and would make
    every triple look broken.

    # Two traps this avoids:
    #   * the export root is named ``code``, which shadows the stdlib ``code`` module that
    #     ``pdb`` imports during pytest start-up. Adding ``code/__init__.py`` turns that
    #     into an INTERNALERROR, so the root itself is never made a package.
    #   * a conftest inside ``code/`` is loaded *before* the rootdir one and puts the
    #     wrong directory on sys.path, breaking the oracle's import. Any stray marker
    #     from an earlier export is removed so the export is reproducible.
    """
    (code_dir / "conftest.py").unlink(missing_ok=True)
    for rel in _git(repo_path, "ls-tree", "-r", "--name-only", treeish).splitlines():
        if not rel.endswith(".py") or is_test_path(rel):
            continue
        pkg_dir = (code_dir / rel).parent
        while pkg_dir != code_dir:          # deliberately stops short of the root
            marker = pkg_dir / "__init__.py"
            if not marker.exists():
                marker.write_text("", encoding="utf-8")
            pkg_dir = pkg_dir.parent

    # The project root must be importable for the `from pkg.mod import ...` form.
    (code_dir / "conftest.py").write_text(
        "import sys, pathlib\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).parent))\n",
        encoding="utf-8",
    )


def files_of(repo_path: Path, treeish: str, paths: list[str]) -> list[str]:
    out = []
    for rel in paths:
        if _git(repo_path, "cat-file", "-e", f"{treeish}:{rel}").strip() == "" and \
           subprocess.run(["git", "-C", str(repo_path), "cat-file", "-e", f"{treeish}:{rel}"],
                          capture_output=True).returncode != 0:
            continue
        out.append(rel)
    return out
