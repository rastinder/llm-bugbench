"""Build hard tasks from mined hard-won fixes, and verify each one actually discriminates.

A mined fix gives a real symptom, a real before state and a real after state, all from a
session where the agent demonstrably failed many times before landing it. That is a much
stronger claim about difficulty than anything the mutant path can make, and it is the
category the user identified as the one models cannot solve unaided.

What still has to be proved before any of it is scorable:

  * the before state must **import and run** hermetically -- the module has to be callable
    with literal arguments, or the probe produces nothing;
  * the generated probe must distinguish the two states, and must do so for the *right*
    reason: a value difference, not one side merely raising;
  * the probe must be stable across repeats, or a score would be noise.

A mined fix that fails any of these is dropped. The count that survives is what the panel
actually gets, and it is reported as measured rather than as the size of the session list.
"""

from __future__ import annotations

import ast
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bugbench.feedback import FeedbackChannel                  # noqa: E402
from bugbench.hardbugs import HardBug                          # noqa: E402
from bugbench.history import ROOTS_BY_PREFIX                   # noqa: E402
from bugbench.probe import (                                   # noqa: E402
    extract_callables, generate_oracle, literal_args,
)
from bugbench.sandbox import build_sandbox, grader_env         # noqa: E402

WORK = Path.home() / ".cache" / "bugbench-hard"


@dataclass
class HardTask:
    """One verified, hard-won bug reduced to a scoreable task."""

    task_id: str
    codebase: str
    module: str
    before_source: str
    after_source: str
    test_source: str
    kept_probes: list[str]
    session_id: str
    struggle_hits: int
    fix_changed_lines: int
    categories: list[str]
    title: str
    repeats: int = 3
    stable: bool = False

    @property
    def difficulty(self) -> str:
        """Difficulty is the struggle, not the diff size.

        A 40-line fix an agent found in one shot is easier than a 40-line fix it burned 200
        turns on. The evidence for difficulty is how long it took, so that is what the
        label is built from.
        """
        if self.struggle_hits >= 30 or self.fix_changed_lines >= 150:
            return "hard"
        if self.struggle_hits >= 10 or self.fix_changed_lines >= 60:
            return "moderate"
        return "trivial"

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id, "codebase": self.codebase, "module": self.module,
            "session_id": self.session_id, "struggle_hits": self.struggle_hits,
            "fix_changed_lines": self.fix_changed_lines,
            "difficulty": self.difficulty, "categories": self.categories,
            "title": self.title, "kept_probes": self.kept_probes,
            "repeats": self.repeats, "stable": self.stable,
            "test_source": self.test_source,
        }


def _repo_for(path: str) -> Path | None:
    return next((Path(r) for pre, r in ROOTS_BY_PREFIX.items()
                 if path.startswith(pre + "/")), None)


def _before_state(bug: HardBug, after: str) -> str | None:
    """Reconstruct the buggy file by reversing the recorded fix.

    Only sound when the fix's oldString still occurs exactly once in the current file. That
    is the same exact-match rule the DB replay path uses, and it fails closed: if the file
    has moved on, the before state cannot be trusted and the task is dropped.
    """
    if not bug.fix_old:
        return None
    if after.count(bug.fix_old) != 1:
        return None
    return after.replace(bug.fix_old, bug.fix_new, 1)


def _run_probe(repo: Path, module_rel: str, source: str, probe_src: str,
               work: Path) -> dict[str, str]:
    d = build_sandbox(repo, work)
    t = d / module_rel
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_text(source)
    (d / "test_generated_probe.py").write_text(probe_src)
    import subprocess
    r = subprocess.run([sys.executable, "-m", "pytest", "test_generated_probe.py",
                        "-q", "--tb=no", "-s"],
                       cwd=d, capture_output=True, text=True, timeout=240,
                       env=grader_env())
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        if line.startswith("PROBE"):
            k, _, v = line[5:].partition("=")
            out[k.strip()] = v.strip()
    shutil.rmtree(work, ignore_errors=True)
    return out


def build_from_bug(bug: HardBug, work: Path, max_probes: int = 10) -> tuple[HardTask | None, str]:
    """Turn one mined fix into a verified task, or explain why it cannot be one."""
    repo = _repo_for(bug.fix_file)
    if repo is None:
        return None, "fix is not in a gradeable repo"
    path = Path(bug.fix_file)
    if not path.exists():
        return None, "fixed file no longer exists on disk"
    module_rel = str(path.relative_to(repo))
    after = path.read_text(encoding="utf-8", errors="replace")
    try:
        ast.parse(after)
    except SyntaxError as e:
        return None, f"after state does not parse: {e}"

    before = _before_state(bug, after)
    if before is None:
        return None, "fix oldString no longer matches exactly once; before state unprovable"

    # Probeable functions must exist in BOTH states.
    calls = []
    for name, _ in extract_callables(after):
        a_after = literal_args(after, name)
        a_before = literal_args(before, name)
        if a_after is not None and a_before is not None:
            calls.append((name, a_after))
        if len(calls) >= max_probes:
            break
    if not calls:
        return None, "no hermetically-callable function in the fixed module"

    from bugbench.probe import _probe_file
    probe_after = _probe_file(after, calls, "a")
    probe_before = _probe_file(before, calls, "b")

    res_after = _run_probe(repo, module_rel, after, probe_after, work / "after")
    if not res_after:
        return None, "probe produced no output on the fixed state"
    res_before = _run_probe(repo, module_rel, before, probe_before, work / "before")

    kept = [n for n, _ in calls
            if n in res_after and n in res_before
            and res_after[n] != res_before[n]
            # A difference where the fixed side merely raised is a crash, not a repair.
            and not res_after[n].startswith("RAISED:")]
    if not kept:
        return None, "no probe distinguishes the two states"

    test_src = _probe_file(after, [c for c in calls if c[0] in kept], "k")

    # Stability: the same probe run twice on the fixed state must agree, or a score is noise.
    runs = [_run_probe(repo, module_rel, after, test_src, work / f"stable{i}") for i in range(2)]
    stable = bool(runs[0]) and runs[0] == runs[1]

    task = HardTask(
        task_id=f"hard__{repo.name.lstrip('.')}__{module_rel.replace('/', '_').replace('.', '_')}"
                f"__{bug.session_id[-8:]}",
        codebase=repo.name.lstrip("."),
        module=module_rel,
        before_source=before,
        after_source=after,
        test_source=test_src,
        kept_probes=kept,
        session_id=bug.session_id,
        struggle_hits=bug.struggle_hits,
        fix_changed_lines=bug.fix_changed_lines,
        categories=bug.categories,
        title=bug.title,
        stable=stable,
    )
    if not stable:
        return task, "probe is not stable across repeats"
    return task, ""


def build_all(bugs: list[HardBug], work: Path = WORK) -> tuple[list[HardTask], dict]:
    work = Path(work)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    tasks: list[HardTask] = []
    reasons: dict[str, int] = {}
    for i, bug in enumerate(bugs):
        task, why = build_from_bug(bug, work / f"b{i}")
        if task is not None and task.stable:
            tasks.append(task)
        else:
            key = why or "not stable"
            reasons[key.split(";")[0][:60]] = reasons.get(key.split(";")[0][:60], 0) + 1
    shutil.rmtree(work, ignore_errors=True)
    return tasks, reasons