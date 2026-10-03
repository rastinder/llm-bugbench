"""Build tasks from fixes that opencode itself made, and tested.

The timeline module can recover the file as it stood on either side of any recorded edit.
A fix on its own is only half an oracle, though: a benchmark needs something that fails
before and passes after. When the same session added test cases shortly after changing
source, those recorded tests are exactly that oracle -- written by the agent that made the
fix, against the real code, in the real repo.

That pairing is the whole supply of this module. Nothing here is hand-authored: the buggy
source, the fixed source, and the test both come out of the saved-change database, so a
task cannot accidentally grade against an oracle that was written to fit it.

Only a fix that is verifiably graded is returned. A candidate is discarded unless the
recorded test fails on the before-state and passes on the after-state, in a sandbox built
from the real repository. That check is what makes the yield trustworthy, and it is also
the check that killed the earlier mutant panels.
"""

from __future__ import annotations

import ast
import json
import shutil
import sqlite3
import subprocess
import sys
import ast as _ast
from dataclasses import dataclass, field
from pathlib import Path

from .sandbox import build_sandbox, grader_env
from .timeline import State, Timeline, load_timeline, python_states

#: How long after a source fix a test edit still counts as its oracle.
ORACLE_WINDOW_MS = 30 * 60 * 1000

#: Repositories larger than this are not copied into a sandbox.
MAX_REPO_BYTES = 300 * 1024 * 1024

#: A directory carrying one of these is treated as the root of a project, so that a sandbox
#: covers the whole package rather than one directory of it. The hardcoded prefix list in
#: history.py covers ten known repositories; these markers find the rest, which matters
#: because the richest recorded fixes live in projects that list never mentioned.
PROJECT_MARKERS = (".git", "pyproject.toml", "setup.py", "requirements.txt",
                   "setup.cfg", "tox.ini")


def discover_root(path: str, stop: str = "/home/ras") -> tuple[str, str] | None:
    """(root, path_relative_to_root) inferred from project markers above ``path``.

    Walks up looking for a project marker; the sandbox needs the whole project because the
    fixed module usually imports its siblings. Falls back to the file's own directory when
    no marker is found, which is still correct for a single-module script.
    """
    f = Path(path)
    for parent in f.parents:
        # A marker directly in the home directory says nothing about which project a loose
        # script belongs to, and accepting it makes the "sandbox" the whole home tree.
        if str(parent) == stop:
            break
        if any((parent / m).exists() for m in PROJECT_MARKERS):
            rel = f.relative_to(parent)
            if ".." in rel.parts:
                return None
            return str(parent), str(rel)
        if str(parent) == stop or parent == parent.parent:
            break
    d = f.parent
    if not str(d).startswith(stop):
        return None
    return str(d), f.name


@dataclass
class RecordedTask:
    """One graded before/after pair, with the oracle that was recorded alongside it."""

    task_id: str
    repo: str                       # repository root, used to build the sandbox
    module_rel: str                 # path of the fixed module, relative to repo
    test_rel: str                   # path of the test file, relative to repo
    before_source: str
    after_source: str
    test_source: str
    fix_time: int
    test_time: int
    session_id: str = ""
    struggle_hits: int = 0
    title: str = ""
    verified: bool = False
    reject_reason: str = ""
    focus_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "repo": self.repo,
            "module_rel": self.module_rel,
            "test_rel": self.test_rel,
            "fix_time": self.fix_time,
            "test_time": self.test_time,
            "session_id": self.session_id,
            "struggle_hits": self.struggle_hits,
            "title": self.title,
            "verified": self.verified,
            "reject_reason": self.reject_reason,
        }


def _repo_of(path: str, roots: dict[str, str]) -> tuple[str, str] | None:
    """(repo_root, path_relative_to_root) for a known repository, else None."""
    for prefix, root in sorted(roots.items(), key=lambda kv: -len(kv[0])):
        if path.startswith(prefix + "/"):
            rel = path[len(prefix) + 1:]
            if ".." in rel.split("/"):
                return None
            return root, rel
    return None


def _test_candidates(module_rel: str) -> list[str]:
    """Plausible test files for a module, by the naming conventions in use here."""
    stem = module_rel.rsplit("/", 1)[-1][:-3]
    parent = module_rel.rsplit("/", 1)[0] if "/" in module_rel else ""
    pre = f"{parent}/" if parent else ""
    out = []
    if stem.startswith("test_"):
        return out                       # a test is not a source file
    for name in (f"{pre}test_{stem}.py", f"{pre}{stem}_test.py", f"{pre}test{stem}.py"):
        if name not in out:
            out.append(name)
    return out


def _defect_size(state: State) -> int:
    return abs(len(state.after) - len(state.before))


def _added_test_ids(before: str, after: str) -> list[str]:
    """Node ids of test methods the edit added or changed.

    These are the assertions that actually discriminate a fix: they run on the buggy
    source and must fail there, and run on the fixed source and must pass. Running the
    whole test file would mix them in with pre-existing assertions that were already
    passing and that, on some intermediate state, may not have moved at all -- that is
    how a legitimate fix got reported as "the recorded test does not pass on the fixed
    state" when in fact only one added assertion was doing the work.
    """
    def methods(text: str) -> dict[str, str]:
        try:
            tree = _ast.parse(text)
        except SyntaxError:
            return {}
        out: dict[str, str] = {}
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef):
                for item in node.body:
                    if isinstance(item, _ast.FunctionDef) and item.name.startswith("test"):
                        out[f"{node.name}::{item.name}"] = _ast.get_source_segment(text, item) or ""
            elif isinstance(node, _ast.FunctionDef) and node.name.startswith("test"):
                out.setdefault(node.name, _ast.get_source_segment(text, node) or "")
        return out
    b, a = methods(before), methods(after)
    added = {k: v for k, v in a.items() if k not in b or b[k] != v}
    return sorted(added)


def content_at(tl: Timeline, when: int) -> str | None:
    """The file's recorded content at one moment, forward-replayed.

    Base is the newest full-content anchor at or before ``when``, then every edit
    recorded up to and including ``when`` is applied in order. This is the correct "source
    as it stood" for a test written at that moment -- using the after-state of just the
    fix that the test happens to follow mixes two different times when more than one
    source edit happened in the window, which we saw directly: the source "after" for one
    edit satisfied the new assertions in the test, while the same test also expected a
    behaviour that a *later* edit introduced. Pairing the test with the source at its own
    timestamp, not the timestamp of whatever fix came first, removes that mismatch.
    """
    anchor = max((a for a in tl.anchors if a.time <= when),
                 key=lambda a: a.time, default=None)
    if anchor is None:
        return None
    content = anchor.content
    for e in tl.edits:
        if e.time > when:
            break
        nxt = tl._apply(content, e.old, e.new)
        if nxt is not None:
            content = nxt
    return content


def candidate_tasks(db: sqlite3.Connection, roots: dict[str, str],
                    limit: int = 40) -> list[RecordedTask]:
    """Every (fix, recorded oracle) pair found in the database, unverified.

    Ordered by how much the fix changed and how close the test followed, so a caller that
    wants only the strongest slice gets it by truncating.
    """
    from .timeline import edited_files

    by_path: dict[str, list[State]] = {}
    for path, n_edits, _w in edited_files(db, "%"):
        if not path.endswith(".py") or n_edits < 2:
            continue
        found = _repo_of(path, roots) or discover_root(path)
        if not found:
            continue
        try:
            states = python_states(db, path)
        except Exception:
            continue
        if states:
            by_path[path] = states

    out: list[RecordedTask] = []
    tl_by: dict[str, Timeline] = {}
    for path, states in by_path.items():
        repo, rel = _repo_of(path, roots) or discover_root(path)
        if repo is None:
            continue
        tests = [t for t in _test_candidates(rel) if t in by_path or _exists(repo, t)]
        if not tests:
            continue
        try:
            tl = load_timeline(db, path)
        except Exception:
            continue
        tl_by[path] = tl
        for test_path in tests:
            test_abs = f"{repo}/{test_path}"
            test_states = by_path.get(test_abs) or python_states(db, test_abs)
            for ts in test_states:
                grew = len(ts.after) - len(ts.before)
                if grew <= 0:
                    continue            # a test edit that only shrank adds no oracle
                # The oracle is the latest source edit caught by this window, paired with
                # the source exactly as it stood when the test was written. Pairing
                # ts.after with st.after of some earlier edit mixes two different times
                # when more than one change landed inside the window.
                past_states = [x for x in states if x.edit.time <= ts.time]
                if not past_states:
                    continue
                st = max(past_states, key=lambda x: x.edit.time)
                if ts.time - st.edit.time > ORACLE_WINDOW_MS:
                    continue
                if _defect_size(st) < 20:
                    continue
                after = content_at(tl, ts.time)
                if after is None or after.strip() == st.before.strip():
                    continue
                focus = []
                out.append(RecordedTask(
                    task_id=f"rec__{Path(rel).stem}__{st.time}",
                    repo=repo,
                    module_rel=rel,
                    test_rel=test_path,
                    before_source=st.before,
                    after_source=after,
                    test_source=ts.after,
                    fix_time=st.time,
                    test_time=ts.time,
                    session_id=st.edit.session_id,
                    focus_ids=focus or [],
                ))
    out.sort(key=lambda t: (t.test_time - t.fix_time, -_approx(t)))
    return out[:limit]


def _approx(t: RecordedTask) -> int:
    return abs(len(t.after_source) - len(t.before_source))


def _exists(repo: str, rel: str) -> bool:
    return (Path(repo) / rel).is_file()


def verify(task: RecordedTask, work: Path) -> RecordedTask:
    """Run the recorded test against both states; keep the task only if it discriminates.

    The test must FAIL on the buggy state and PASS on the fixed one. Either other outcome
    discards the task: passing on both grades nothing, and failing on both means the test is
    broken or depends on the environment, so admitting it would put a coin flip in the
    results.

    The sandbox is built once and reused for both runs. Rebuilding per state cost more than
    the tests did, because several roots here are large trees whose bulk is skipped
    directories (a Chromium build, a Chrome profile) and pruning them twice is pure waste.
    """
    repo = Path(task.repo)
    if not repo.is_dir():
        task.reject_reason = f"repository missing: {repo}"
        return task
    for text, name in ((task.before_source, "before"), (task.after_source, "after"),
                       (task.test_source, "test")):
        try:
            ast.parse(text)
        except SyntaxError as exc:
            task.reject_reason = f"{name} does not parse: {exc.msg}"
            return task

    shutil.rmtree(work, ignore_errors=True)
    try:
        box = build_sandbox(repo, work)
    except OSError as exc:
        task.reject_reason = f"sandbox build failed: {exc}"
        return task
    if _dir_bytes(box) > MAX_REPO_BYTES:
        task.reject_reason = "sandbox larger than the limit"
        return task

    (box / task.module_rel).parent.mkdir(parents=True, exist_ok=True)
    (box / task.test_rel).parent.mkdir(parents=True, exist_ok=True)
    (box / task.test_rel).write_text(task.test_source)

    before = _run_test(box, task, task.before_source)
    if before == PASSED:
        task.reject_reason = "recorded test already passes on the buggy state"
        return task
    # FAILED on a genuine assertion, or BROKE because the buggy source lacks the helper
    # this fix introduced -- both are evidence the fix is the difference between the two
    # states, and either would admit a non-discriminating oracle if the after-side were
    # not held to a strict PASS.

    after = _run_test(box, task, task.after_source)
    if after is not PASSED:
        task.reject_reason = "recorded test does not pass on the fixed state"
        return task

    task.verified = True
    return task


#: Outcomes of one pytest run against one source state.
PASSED = "passed"
FAILED = "failed"
BROKE = "broke"


def _run_test(box: Path, task: RecordedTask, source: str) -> str:
    """Run the recorded test against one version of the module."""
    (box / task.module_rel).write_text(source)
    # The before and after source are written moments apart, so Python's timestamp-based
    # invalidation sees no change and reuses the stale .pyc -- the "after" run would test
    # the "before" code. Clear bytecode caches so the fresh source is always recompiled.
    for pycache in box.rglob("__pycache__"):
        shutil.rmtree(pycache, ignore_errors=True)
    if task.focus_ids:
        node_ids = [f"{task.test_rel}::{fid}" for fid in task.focus_ids]
    else:
        node_ids = [task.test_rel]
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", *node_ids, "-q",
             "--no-header", "-p", "no:cacheprovider"],
            cwd=box, capture_output=True, text=True, timeout=300, env=grader_env())
    except subprocess.TimeoutExpired:
        return BROKE
    # pytest's exit codes for "nothing usable ran" (collection errors, usage errors, no
    # tests). Everything else that is non-zero is a genuine test failure, which is exactly
    # what the buggy state is supposed to produce.
    if proc.returncode in (2, 3, 4, 5) or "INTERNALERROR" in proc.stdout:
        return BROKE
    if proc.returncode == 0:
        return PASSED
    (box / ".pytest-out.txt").write_text(proc.stdout[-4000:] + "\n" + proc.stderr[-1500:])
    return FAILED


def _dir_bytes(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
