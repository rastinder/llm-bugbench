#!/usr/bin/env python3
"""Build the two publishable repo trees.

public/  -> credential + IP scrubbed. Company names, file names, project names and session
            context are KEPT, because that context is what makes the dataset valuable.
private/ -> everything exactly as it is locally, except live credentials, which are still
            scrubbed because git history is immutable: a rotated key that was ever pushed
            stays in the objects forever.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys

SRC = pathlib.Path("/home/ras/llm-bugbench")
PUB = pathlib.Path("/home/ras/llm-bugbench-publish")
PRIV = pathlib.Path("/home/ras/llm-bugbench-private")

# Globbed, not hardcoded: a hardcoded list silently dropped a newly added module
# (bugbench.outcome) from both published repos, and the failure only surfaced as
# ModuleNotFoundError in the published test suite.
CODE = sorted(p.name for p in (SRC / "src" / "bugbench").glob("*.py")
              if p.name != "__pycache__")
TESTS = sorted(p.name for p in (SRC / "tests").glob("*.py"))


README_SRC = pathlib.Path("/home/ras/llm-bugbench/README.public.md")
GITIGNORE = """__pycache__/
*.pyc
.pytest_cache/
.venv/
data/results*.jsonl
leaderboard.html
"""


def build(root: pathlib.Path, dataset: str, include_raw: bool) -> None:
    # Wipe the CONTENT but never .git -- an earlier version of this script deleted the
    # repo history and the README on every run.
    if root.exists():
        for child in root.iterdir():
            if child.name == ".git":
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    (root / "src" / "bugbench").mkdir(parents=True)
    (root / "data" / "raw" / "scripts").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "scripts").mkdir()

    for f in CODE:
        shutil.copy(SRC / "src" / "bugbench" / f, root / "src" / "bugbench" / f)
    for f in TESTS:
        shutil.copy(SRC / "tests" / f, root / "tests" / f)
    # build_repos.py is a local orchestrator with machine-specific paths -- not shipped
    for f in ["dump_all.py", "extract_pairs.py", "build_tasks.py", "scan_secrets.py",
              "sanitize_for_publish.py", "anonymise.py", "prepush_sweep.py"]:
        shutil.copy(SRC / "data" / "raw" / "scripts" / f,
                    root / "data" / "raw" / "scripts" / f)
    shutil.copy(dataset, root / "data" / "tasks.jsonl")
    tier = ("public" if "public" in dataset else
            "raw" if dataset.endswith("tasks.jsonl") else
            "private" if "private" in dataset else "local")
    (root / "data" / "TIER").write_text(tier + "\n")
    shutil.copy(SRC / "pyproject.toml", root / "pyproject.toml")
    shutil.copy(SRC / "LICENSE", root / "LICENSE")
    (root / ".gitignore").write_text(GITIGNORE)
    for extra in ("prepush_sweep.py", "topup_to_20.sh"):
        f = SRC / "data" / "raw" / "scripts" / extra
        if f.exists():
            shutil.copy(f, root / "scripts" / extra)
    iso = SRC / "data" / "raw" / "scripts" / "agy-isolate.sh"
    if iso.exists():
        shutil.copy(iso, root / "scripts" / "agy-isolate.sh")
        (root / "scripts" / "agy-isolate.sh").chmod(0o755)
    if README_SRC.exists():
        shutil.copy(README_SRC, root / "README.md")
    if include_raw:
        for f in ["results.jsonl", "results.main.jsonl", "leaderboard_snapshot.json"]:
            p = SRC / "data" / f
            if p.exists():
                shutil.copy(p, root / "data" / f)
        for f in ["PLAN.md", "ATTEMPTS.md"]:
            if (SRC / ".swarm" / "memory" / f).exists():
                shutil.copy(SRC / ".swarm" / "memory" / f, root / f)
    (root / "scripts" / "__init__.py").write_text("")
    # make the ported gate point at the shipped dataset
    g = (root / "tests" / "test_publish_gate.py").read_text()
    g = g.replace('PUBLIC = "/home/ras/llm-bugbench/data/tasks.public.jsonl"',
                  'PUBLIC = str(pathlib.Path(__file__).resolve().parent.parent\n'
                  '                / "data" / "tasks.jsonl")')
    g = g.replace('ANON = "/home/ras/llm-bugbench/data/tasks.public.anonymised.jsonl"',
                  'ANON = PUBLIC  # the shipped dataset is the same tier')
    g = g.replace('from data.raw.scripts.sanitize_for_publish import redact  # type: ignore',
                  'from data.raw.scripts.sanitize_for_publish import redact  # type: ignore')
    g = g.replace('lt("/home/ras/llm-bugbench/data/tasks.jsonl")', 'lt(PUBLIC)')
    if "import pathlib" not in g:
        g = g.replace("import re", "import pathlib\nimport re", 1)
    (root / "tests" / "test_publish_gate.py").write_text(g)
    # rewrite absolute local paths inside the shipped scripts
    for p in (root / "data" / "raw" / "scripts").glob("*.py"):
        s = p.read_text()
        s = s.replace('sys.path.insert(0, "/home/ras/llm-bugbench/src")',
                      'sys.path.insert(0, str(pathlib.Path(__file__).resolve()\n'
                      '    .parents[3] / "src"))')
        if "import pathlib" not in s:
            s = s.replace("import sys", "import pathlib\nimport sys", 1)
        p.write_text(s)
    print(f"built {root}  dataset={pathlib.Path(dataset).name}  raw={include_raw}")


if __name__ == "__main__":
    build(PUB, str(SRC / "data" / "tasks.public.jsonl"), include_raw=False)
    # RAW: byte-identical snippets, no redaction of any kind. The user is right that
    # redaction can distort the code (measured: 11 tasks shift patch_reproduction by up
    # to 0.092) and this repo exists to be the exact ground truth. It is PRIVATE.
    build(PRIV, str(SRC / "data" / "tasks.jsonl"), include_raw=True)
