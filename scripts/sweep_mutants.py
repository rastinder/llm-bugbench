"""Sweep a repository for mutant tasks that its own tests actually kill.

One repo, one process, so several can run in parallel. Writes its own JSON shard rather
than a shared file, because the sweep is the slowest part of building the panel and
losing a completed repo's work to a timeout is pure waste.

A mutant becomes a task only when the repository's green tests fail against it. That
single condition is the whole admission rule, and it is what makes a mutant a real task
rather than a hypothetical: the oracle exists, and it is the project's own.
"""
from __future__ import annotations

import argparse
import collections
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bugbench.feedback import FeedbackChannel          # noqa: E402
from bugbench.mutants import apply_mutation, generate  # noqa: E402
from bugbench.sandbox import build_sandbox, grader_env  # noqa: E402

PY = sys.executable


def green_tests(repo: Path, timeout: int = 150) -> list[str]:
    """Test files that pass on the pristine tree.

    Only a green suite can prove anything: a suite that already fails cannot distinguish
    a mutant's damage from pre-existing breakage.
    """
    out = []
    for p in sorted(repo.rglob("test_*.py")):
        if "venv" in str(p):
            continue
        rel = str(p.relative_to(repo))
        try:
            r = subprocess.run([PY, "-m", "pytest", rel, "-q", "--tb=no"],
                               cwd=repo, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            continue
        if r.returncode == 0:
            out.append(rel)
    return out


def sweep(repo: Path, workdir: Path, per_module: int = 25,
          max_modules: int | None = None) -> list[dict]:
    tests = green_tests(repo)
    if not tests:
        print(f"{repo.name}: no green test file", flush=True)
        return []

    mods = [p for p in sorted(repo.rglob("*.py"))
            if "test" not in p.name and "venv" not in str(p) and p.stat().st_size < 400_000]
    if max_modules:
        mods = mods[:max_modules]
    print(f"{repo.name}: {len(tests)} green test file(s), {len(mods)} modules", flush=True)

    found: list[dict] = []
    for sp in mods:
        rel = str(sp.relative_to(repo))
        try:
            pristine = sp.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for m in generate(pristine, rel, limit=per_module).mutants:
            d = build_sandbox(repo, workdir / m.bug_id)
            try:
                (d / rel).write_text(apply_mutation(pristine, m))
            except OSError:
                shutil.rmtree(d, ignore_errors=True)
                continue
            try:
                r = subprocess.run([PY, "-m", "pytest", *tests, "-q", "--tb=no"],
                                   cwd=d, capture_output=True, timeout=150,
                                   env=grader_env())
                killed = r.returncode != 0
            except subprocess.TimeoutExpired:
                killed = False
            if not killed:
                shutil.rmtree(d, ignore_errors=True)
                continue
            rows = [x for t in tests for x in FeedbackChannel(d, "python3").run(t)]
            failed = sorted({x["nodeid"] for x in rows
                             if x["outcome"] in {"failed", "error"}})
            shutil.rmtree(d, ignore_errors=True)
            if not failed:
                continue
            found.append({
                "task_id": f"{repo.name}__{m.bug_id}",
                "codebase": repo.name,
                "module": rel,
                "bug_id": m.bug_id,
                "symbol": m.symbol,
                "operator": m.operator,
                "line": m.line,
                "test_node": failed[0],
                "n_failing": len(failed),
                "depth": "module" if m.symbol == "<module>" else "function",
                "difficulty": m.difficulty,
                "green_tests": tests,
            })
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--out", required=True)
    ap.add_argument("--work", default=None)
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    work = Path(args.work) if args.work else Path(
        tempfile.mkdtemp(prefix=f"sw-{repo.name}-", dir="/home/ras/.cache"))
    work.mkdir(parents=True, exist_ok=True)
    try:
        found = sweep(repo, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(found, indent=2))
    print(f"{repo.name}: {len(found)} tasks -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
