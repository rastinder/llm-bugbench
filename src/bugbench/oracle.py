"""Execution oracle with a strict validity gate (council requirement).

An oracle is admitted ONLY if it FAILS on the buggy snippet and PASSES on the developer's
reference fix. A check that cannot tell them apart carries no information and is discarded
-- that is exactly the negative control in the test suite.

Nothing is ever eval'd in-process: every snippet runs in its own subprocess with a fresh
temp cwd, no proxy env, a wall-clock timeout and a memory cap.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any

TIMEOUT_S = 10
MEM_LIMIT_BYTES = 512 * 1024 * 1024

DANGEROUS = re.compile(
    r"\b(socket|urllib|requests|httpx|http\.client|subprocess|os\.system|"
    r"os\.popen|shutil\.rmtree|multiprocessing|ctypes|pickle\.loads|eval\(|exec\()")


@dataclass
class Oracle:
    kind: str = "none"
    score_fn: object = None
    probes: list = field(default_factory=list)
    bug_fails: bool = False
    ref_passes: bool = False
    reason: str = ""

    def validate(self) -> bool:
        if self.kind != "exec_diff" or self.score_fn is None:
            return False
        self.bug_fails = self.score_fn(self._buggy) < 1.0
        self.ref_passes = self.score_fn(self._reference) >= 1.0
        if self.bug_fails and self.ref_passes:
            self.kind = "exec_diff"
            return True
        self.reason = (f"rejected: buggy_passes={not self.bug_fails} "
                       f"reference_passes={self.ref_passes}")
        self.kind = "none"
        return False

    def score(self, candidate_code: str) -> float:
        if self.kind != "exec_diff" or self.score_fn is None:
            return 0.0
        if not candidate_code or not candidate_code.strip():
            return 0.0
        if DANGEROUS.search(candidate_code):
            return 0.0
        try:
            return float(self.score_fn(candidate_code))
        except Exception:
            return 0.0

    _buggy: str = ""
    _reference: str = ""


def _importable(code: str) -> bool:
    try:
        compile(code, "<snippet>", "exec")
        return True
    except SyntaxError:
        return False


def _wrap(code: str, cases: list) -> str:
    """Build a probe program that defines the snippet then runs each case."""
    return (
        "import json, sys\n"
        "NS = {}\n"
        "SRC = json.loads(sys.stdin.read())\n"
        "exec(compile(SRC['code'], '<snippet>', 'exec'), NS)\n"
        "FUNCS = {k: v for k, v in NS.items()\n"
        "         if callable(v) and not k.startswith('_') and\n"
        "         getattr(v, '__module__', None) is None}\n"
        "if not FUNCS:\n"
        "    print(json.dumps({'error': 'no callable defined'}))\n"
        "    sys.exit(0)\n"
        "name = SRC.get('fn') or sorted(FUNCS)[0]\n"
        "fn = FUNCS[name]\n"
        "out = []\n"
        "for args in SRC['cases']:\n"
        "    try:\n"
        "        r = fn(*args)\n"
        "        out.append({'ok': True, 'repr': repr(r)[:400]})\n"
        "    except BaseException as e:\n"
        "        out.append({'ok': False, 'err': type(e).__name__})\n"
        "print(json.dumps({'results': out}))\n"
    )


def _run(code: str, fn: str, cases: list, timeout: int = TIMEOUT_S):
    """Execute the snippet on the cases in an isolated subprocess."""
    import json as _json
    payload = _json.dumps({"code": code, "fn": fn, "cases": cases})
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_PROXY": "*",
        "no_proxy": "*",
    }
    with tempfile.TemporaryDirectory() as td:
        try:
            p = subprocess.run([sys.executable, "-c", _wrap(code, cases)],
                               input=payload, capture_output=True, text=True,
                               timeout=timeout, cwd=td, env=env,
                               preexec_fn=_limit)
        except subprocess.TimeoutExpired:
            return None
        except Exception:
            return None
    out = (p.stdout or "").strip().splitlines()
    if not out:
        return None
    try:
        return _json.loads(out[-1])
    except Exception:
        return None


def _limit() -> None:  # pragma: no cover - runs in the child
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (MEM_LIMIT_BYTES, MEM_LIMIT_BYTES))
        resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    except Exception:
        pass
    os.setsid()


def _probe_cases(fn_name: str, nargs: int) -> list[list]:
    """Input cases sized to the function's arity, covering the common failure shapes.

    A fixed case list silently fails for any function that does not take one list
    argument, which is why the previous version admitted an oracle on 1/60 real tasks.
    """
    if nargs <= 0:
        return [[]]
    one_arg = [
        [], [1], [0], [-1], [2.5], [1, 2, 3], ["a"], [None], [""],
        [1, 2, 3, 4, 5, 6], [[1, 2], [3]], [True, False],
        ["not guaranteed", "guaranteed sales"], [{"a": 1}], [[1, 2]],
    ]
    if nargs == 1:
        return [[[]]] + [[c] for c in one_arg]
    cases = []
    for a in one_arg:
        for b in ([], [2], "x", 1):
            cases.append([a, b])
    return cases[:40]


def _pick_fn(code: str) -> str | None:
    import ast
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            return node.name
    return None


def _arity(code: str) -> int:
    """Number of positional parameters of the first public function (0 if unknown)."""
    import ast
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return 0
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            a = node.args
            return len(a.posonlyargs) + len(a.args)
    return 0


def build_oracle(task: Any) -> Oracle:
    """Construct (and validate) an execution oracle for a task, if one is possible."""
    lang = (getattr(task, "language", "") or "").lower()
    buggy = getattr(task, "buggy", "") or ""
    fixed = getattr(task, "reference_fix", "") or ""

    if lang != "python":
        return Oracle(kind="none", reason=f"language {lang!r} not executable")
    if not (_importable(buggy) and _importable(fixed)):
        return Oracle(kind="none", reason="snippet does not compile")
    if DANGEROUS.search(buggy) or DANGEROUS.search(fixed):
        return Oracle(kind="none", reason="snippet contains restricted constructs")

    fn = _pick_fn(buggy) or _pick_fn(fixed)
    if not fn:
        return Oracle(kind="none", reason="no public function found")

    nargs = _arity(buggy) or _arity(fixed) or 1
    cases = _probe_cases(fn, nargs)
    ref = _run(fixed, fn, cases)
    bug = _run(buggy, fn, cases)
    if not ref or not bug or "results" not in ref or "results" not in bug:
        return Oracle(kind="none", reason="probe produced no results")

    # The oracle's answer key is the reference's behaviour; a candidate passes when it
    # reproduces it on cases where the buggy version demonstrably differs.
    key = ref["results"]
    discriminating = [i for i, (b, r) in enumerate(zip(bug["results"], key))
                      if b != r]
    if not discriminating:
        return Oracle(kind="none",
                      reason="probe cannot distinguish buggy from reference")
    probes = [cases[i] for i in discriminating]
    key_disc = [key[i] for i in discriminating]

    def score_fn(candidate_code: str, _f=fn, _p=probes, _k=key_disc) -> float:
        res = _run(candidate_code, _f, _p)
        if not res or "results" not in res:
            return 0.0
        got = res["results"]
        if len(got) != len(_k):
            return 0.0
        hits = sum(1 for g, k in zip(got, _k) if g == k)
        return round(hits / len(_k), 4)

    o = Oracle(kind="exec_diff", score_fn=score_fn, probes=probes,
               reason=f"probes={len(probes)}")
    o._buggy, o._reference = buggy, fixed
    o.validate()
    return o
