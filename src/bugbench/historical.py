"""Turn real historical (before, after) file pairs into benchmark tasks.

A historical pair is a genuine bug and a genuine fix, which is exactly what mutants are not.
But a pair alone is not a task: something has to *tell* you the fix worked, and the
transcripts do not carry a test for it.

So a task needs a discriminator, obtained in one of two ways, in order of preference:

  1. **A test the repo already has.** Run the file's repo test suite against before and
     after. If some test fails on before and passes on after, that test is a verified
     oracle for a real historical bug -- the strongest possible task, because both the bug
     and the oracle are authentic.
  2. **A differential probe.** When no test discriminates, generate one: ask the model to
     read the file and write assertions that hold on `after` but not on `before`. Those are
     authored, not organic, and are labelled as such rather than passed off as historical.

Pairs that yield neither are dropped. A "task" whose oracle was written after seeing the fix
is a weaker claim about real-world debugging, and the label is what keeps it honest.

Difficulty is set from the size of the real change, not from a mutation operator: a 500-line
rewriter and a two-token tweak are not the same kind of problem, and collapsing them into one
difficulty bucket is how a panel ends up with no headroom.
"""
from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bugbench.feedback import FeedbackChannel                  # noqa: E402
from bugbench.history import HistoricalPair, Snapshot           # noqa: E402
from bugbench.sandbox import build_sandbox, grader_env         # noqa: E402


def difficulty_for(changed_lines: int, spans: int = 1) -> str:
    """Grade a real change by how much it moves, not by an operator table.

    Mutant difficulty is a property of the operator; a real fix has no operator, so size is
    the only honest proxy. Thresholds are deliberately conservative because the first panel
    used single-token mutants and three frontier models scored 1.00 on it.
    """
    if changed_lines >= 60 or spans >= 4:
        return "hard"
    if changed_lines >= 12:
        return "moderate"
    return "trivial"


@dataclass
class HistoricalTask:
    """One real historical bug with a verified discriminator."""

    task_id: str
    codebase: str
    module: str
    language: str
    before_source: str
    after_source: str
    test_node: str
    test_file: str
    changed_lines: int
    difficulty: str
    oracle_kind: str            # "repo_test" | "differential"
    disk_anchored: bool = False
    verified: bool = False
    reject_reason: str = ""

    def as_dict(self) -> dict:
        # Sources are excluded: the manifest describes tasks, it does not embed answers.
        return {
            "task_id": self.task_id, "codebase": self.codebase, "module": self.module,
            "language": self.language, "test_node": self.test_node,
            "test_file": self.test_file, "changed_lines": self.changed_lines,
            "difficulty": self.difficulty, "oracle_kind": self.oracle_kind,
            "disk_anchored": self.disk_anchored, "verified": self.verified,
            "reject_reason": self.reject_reason,
            "before_lines": self.before_source.count("\n") + 1,
            "after_lines": self.after_source.count("\n") + 1,
        }


def _tests_for(repo: Path) -> list[str]:
    return [str(p.relative_to(repo)) for p in sorted(repo.rglob("test_*.py"))
            if "venv" not in str(p)]


def find_discriminating_tests(repo: Path, pair: HistoricalPair, work: Path,
                              timeout: int = 300) -> tuple[str, str] | None:
    """Look for a test the repo already has that fails on before and passes on after.

    The real bug's own oracle, if one survived. Both states are run against the same suite
    and the first test whose outcome differs is the discriminator.
    """
    tests = _tests_for(repo)
    if not tests:
        return None
    greens = [t for t in tests
              if _suite_passes(repo, t, work / f"probe_{abs(hash(t))}", timeout)]
    if not greens:
        return None

    rows_before = _run_suite(repo, pair.file_path, pair.before.content, greens, work / "before")
    rows_after = _run_suite(repo, pair.file_path, pair.after.content, greens, work / "after")

    by_b = {r["nodeid"]: r for r in rows_before}
    for r in rows_after:
        was = by_b.get(r["nodeid"])
        if was and was["outcome"] != "passed" and r["outcome"] == "passed":
            # The test failed on the buggy state and passes on the fixed one.
            owner = _owning_file(r["nodeid"])
            return (r["nodeid"], owner if owner in greens else greens[0])
    return None


def _owning_file(nodeid: str) -> str:
    return nodeid.split("::")[0].replace(".py", ".py")


def _suite_passes(repo: Path, test: str, work: Path, timeout: int) -> bool:
    import subprocess
    work = Path(work)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", test, "-q", "--tb=no"],
                           cwd=repo, capture_output=True, timeout=timeout,
                           env=grader_env())
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _relative(repo: Path, file_path: str) -> str:
    """Where the file lives inside its repo, so a snapshot can be written into a sandbox.

    Falls back to the basename -- never to the source text. An earlier version returned
    ``Path(source).name`` when the prefix did not match, which on a large source produced a
    filename of the whole file and raised ENAMETOOLONG.
    """
    from bugbench.history import ROOTS_BY_PREFIX
    text = str(file_path)
    for prefix in ROOTS_BY_PREFIX:
        if text.startswith(prefix.rstrip("/") + "/"):
            return str(Path(text).relative_to(prefix))
    return Path(text).name


def _run_suite(repo: Path, file_path: str, source: str, tests: list[str],
               work: Path) -> list[dict]:
    work = Path(work)
    d = build_sandbox(repo, work)
    rel = _relative(repo, file_path)
    target = d / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source)
    rows = [r for t in tests for r in FeedbackChannel(d, "python3").run(t)]
    shutil.rmtree(work, ignore_errors=True)
    return rows


@dataclass
class BuildReport:
    tasks: list[HistoricalTask] = field(default_factory=list)
    no_discriminator: int = 0
    too_small: int = 0

    def as_dict(self) -> dict:
        return {"tasks": len(self.tasks), "no_discriminator": self.no_discriminator,
                "too_small": self.to_small if hasattr(self, "to_small") else self.too_small,
                "difficulty_mix": _mix(self.tasks)}


def _mix(tasks: list[HistoricalTask]) -> dict:
    out: dict[str, int] = {}
    for t in tasks:
        out[t.difficulty] = out.get(t.difficulty, 0) + 1
    return out


def build_tasks(pairs: list[HistoricalPair], work: Path, min_changed: int = 12,
                max_tasks: int = 40) -> BuildReport:
    """Admit real pairs that a repo test can discriminate, hardest first."""
    report = BuildReport()
    ranked = sorted(pairs, key=lambda p: -p.changed_lines)
    for pair in ranked:
        if len(report.tasks) >= max_tasks:
            break
        if pair.changed_lines < min_changed:
            report.too_small += 1
            continue
        from bugbench.history import ROOTS_BY_PREFIX
        repo = next((Path(root) for prefix, root in ROOTS_BY_PREFIX.items()
                     if pair.file_path.startswith(prefix)), None)
        if repo is None or not repo.exists():
            report.no_discriminator += 1
            continue
        try:
            found = find_discriminating_tests(repo, pair, work / "probe")
        except Exception:
            found = None
        if not found:
            report.no_discriminator += 1
            continue
        node, test_file = found
        report.tasks.append(HistoricalTask(
            task_id=f"hist__{pair.file_path.replace('/', '_').replace('.', '_')[-48:]}",
            codebase=repo.name.lstrip("."),
            module=str(Path(pair.file_path).relative_to(repo)),
            language="python",
            before_source=pair.before.content,
            after_source=pair.after.content,
            test_node=node,
            test_file=test_file,
            changed_lines=pair.changed_lines,
            difficulty=difficulty_for(pair.changed_lines),
            oracle_kind="repo_test",
            disk_anchored=pair.on_disk_matches_after,
            verified=True,
        ))
    return report