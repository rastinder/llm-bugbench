"""Stack multiple defects into real, hard-won files and grade each one individually.

Where this sits and why. Two earlier approaches both topped out:

  * single mutants in ordinary code -- three frontier models scored 1.00;
  * combined mutants -- 0.67, 0/8 files cleared, which broke the ceiling but stalled at
    roughly two thirds.

The blocker for both was supply, not statistics. Mining the transcripts for genuinely hard
bugs found plenty of them -- the top one cost an agent 226 turns and 75 edits to land -- but
the recorded fixes cannot be reversed: those files are now 4,000 to 10,000 lines and have
been rewritten dozens of times since, so the recorded ``oldString`` no longer occurs even
once. That was measured, not assumed, and it is why 41 of the mined fixes are unusable.

What survives is the *host* rather than the recorded patch: eight files that are still
parseable and still expose hermetically-callable functions, chosen because a hard bug was
actually fixed in each. Those files are real code from real debugging sessions, which is a
better host than anything invented.

Into those hosts, defects are injected that reproduce the *classes* those sessions actually
struggled with, stacked several per file. Stacking is what carries the score down, because
per-bug credit means a model that finds two of six still scores about a third.

Every injected defect is verified individually before it enters the panel:

  * the file must import and run with the defect present;
  * a probe must distinguish the two states, and for the right reason -- a value difference,
    not one side merely raising;
  * fixing every other defect must leave this one still failing, or the bugs are not
    independent and per-bug credit would double-count a single fix.

Anything failing those three is dropped rather than admitted ambiguously.
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
from bugbench.probe import extract_callables, literal_args     # noqa: E402
from bugbench.sandbox import build_sandbox, grader_env         # noqa: E402

#: Defect classes observed in the mined sessions, named for the sessions that produced them
#: so the panel can be described honestly as "these bug shapes, in these real files" rather
#: than as generic mutation.
BUG_CLASSES = {
    "off_by_one": "a boundary moved by one",
    "inverted_guard": "a guard condition reversed, so the wrong branch is taken",
    "swapped_operands": "operand order swapped in a comparison",
    "dropped_guard": "a guard clause deleted, so an unchecked value is used",
    "wrong_accumulator": "accumulation applied to the wrong variable",
    "silent_fallback": "an error path replaced by a silent default",
}


@dataclass
class InjectedBug:
    """One defect placed in a host file."""

    bug_id: str
    class_name: str
    line: int
    original: str
    mutated: str
    probe: str = ""
    probe_value_before: str = ""
    probe_value_after: str = ""

    def as_dict(self) -> dict:
        # The probe values are REQUIRED by the grader: it compares what a candidate
        # actually produces against the recorded correct behaviour. They were missing from
        # this dict, which made the grader award 0/6 even on the fixed state -- a broken
        # oracle that would have looked exactly like a very hard task.
        return {"bug_id": self.bug_id, "class": self.class_name, "line": self.line,
                "original": self.original, "mutated": self.mutated,
                "probe": self.probe,
                "probe_value_before": self.probe_value_before,
                "probe_value_after": self.probe_value_after}


@dataclass
class HardHostTask:
    """One host file carrying several verified defects."""

    task_id: str
    codebase: str
    module: str
    language: str = "python"
    origin_session: str = ""
    origin_struggle: int = 0
    origin_title: str = ""
    before_source: str = ""
    after_source: str = ""
    test_source: str = ""
    bugs: list[InjectedBug] = field(default_factory=list)
    verified: bool = False
    reject_reason: str = ""

    @property
    def bugs_total(self) -> int:
        return len(self.bugs)

    def as_dict(self) -> dict:
        # Sources are deliberately NOT in the manifest: it describes tasks and must not
        # embed the answers. They live in a gitignored sidecar that the campaign reads.
        return {
            "task_id": self.task_id, "codebase": self.codebase, "module": self.module,
            "origin_session": self.origin_session, "origin_struggle": self.origin_struggle,
            "origin_title": self.origin_title, "bugs_total": self.bugs_total,
            "difficulty": "hard" if self.bugs_total >= 4 else "moderate",
            "verified": self.verified, "reject_reason": self.reject_reason,
            "bugs": [b.as_dict() for b in self.bugs],
            "test_source": self.test_source,
        }


# ---------------------------------------------------------------------------
# defect injection
# ---------------------------------------------------------------------------

def _compile(src: str) -> ast.AST | None:
    try:
        return ast.parse(src)
    except SyntaxError:
        return None


def find_defect_sites(source: str, limit: int = 40) -> list[tuple[str, int, str, str, str]]:
    """Candidate (class, lineno, original, mutated, why) sites in a host file.

    Only mutations with a *semantic* justification are considered -- a boundary moved, a
    guard reversed, an operand swapped. Mutating a constant was measured to produce a panel
    that frontier models ace, so the operator set here deliberately excludes it.
    """
    tree = _compile(source)
    if tree is None:
        return []
    lines = source.splitlines(keepends=True)
    out: list[tuple[str, int, str, str, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
            op = type(node.ops[0]).__name__
            swap = {"Lt": "Gt", "Gt": "Lt", "LtE": "GtE", "GtE": "LtE"}
            if op in swap:
                ln = node.lineno
                line = lines[ln - 1] if 0 < ln <= len(lines) else ""
                for sym, repl in (("<=", ">="), (">=", "<="), ("<", ">"), (">", "<")):
                    if sym in line and not line.lstrip().startswith("#"):
                        out.append(("inverted_guard", ln, line.strip(),
                                    line.strip().replace(sym, repl, 1),
                                    f"{sym} -> {repl}"))
                        break
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Sub, ast.Add)):
            ln = node.lineno
            line = lines[ln - 1] if 0 < ln <= len(lines) else ""
            if not line.lstrip().startswith("#"):
                sym = "-" if isinstance(node.op, ast.Sub) else "+"
                repl = "+" if sym == "-" else "-"
                if sym in line:
                    out.append(("off_by_one", ln, line.strip(),
                                line.strip().replace(sym, repl, 1), f"{sym} -> {repl}"))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            ln = node.lineno
            line = lines[ln - 1] if 0 < ln <= len(lines) else ""
            if "not " in line and not line.lstrip().startswith("#"):
                out.append(("inverted_guard", ln, line.strip(),
                            line.strip().replace("not ", "", 1), "dropped a negation"))
    return out[:limit]


def apply_bug(source: str, original: str, mutated: str) -> str | None:
    """Replace exactly one occurrence, and only if it is unique."""
    if source.count(original) != 1:
        return None
    out = source.replace(original, mutated, 1)
    return out if _compile(out) is not None else None


# ---------------------------------------------------------------------------
# probing and verification
# ---------------------------------------------------------------------------

def _probe_source(module_src: str, calls: list[tuple[str, str]], tag: str) -> str:
    lines = ["import json, sys", "sys.path.insert(0, '.')", "", "def _norm(v):",
             "    try:",
             "        return json.dumps(v, sort_keys=True, default=repr)",
             "    except Exception:",
             "        return 'UNSERIALISABLE'", ""]
    for i, (fn, args) in enumerate(calls):
        lines += [
            f"def test_{tag}_{i}():",
            "    ns = {}",
            f"    exec(compile({module_src!r}, 'm.py', 'exec'), ns)",
            "    try:",
            f"        _r = ns[{fn!r}]({args})",
            "    except Exception as e:",
            "        print('PROBE' + " + repr(fn) + " + '=RAISED:' + type(e).__name__)",
            "    else:",
            "        print('PROBE' + " + repr(fn) + " + '=' + _norm(_r))",
        ]
    return "\n".join(lines) + "\n"


def _run(repo: Path, module_rel: str, module_src: str, probe_src: str,
         work: Path) -> dict[str, str]:
    d = build_sandbox(repo, work)
    t = d / module_rel
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_text(module_src)
    (d / "test_probe.py").write_text(probe_src)
    import subprocess
    r = subprocess.run([sys.executable, "-m", "pytest", "test_probe.py", "-q",
                        "--tb=no", "-s"],
                       cwd=d, capture_output=True, text=True, timeout=240, env=grader_env())
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        if line.startswith("PROBE"):
            k, _, v = line[5:].partition("=")
            out[k.strip()] = v.strip()
    shutil.rmtree(work, ignore_errors=True)
    return out


def build_host_task(repo: Path, module_rel: str, after_src: str, task_id: str,
                    origin_session: str = "", origin_struggle: int = 0,
                    origin_title: str = "", n_bugs: int = 5,
                    work: Path | None = None) -> HardHostTask:
    """Stack up to ``n_bugs`` verified defects into one host file."""
    work = work or (Path.home() / ".cache" / "bugbench-inject")
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)

    task = HardHostTask(
        task_id=task_id, codebase=repo.name.lstrip("."), module=module_rel,
        origin_session=origin_session, origin_struggle=origin_struggle,
        origin_title=origin_title, after_source=after_src, before_source=after_src,
    )

    calls = [(n, a) for n, _ in extract_callables(after_src)
             if (a := literal_args(after_src, n)) is not None]
    if not calls:
        task.reject_reason = "no hermetically-callable function in the host"
        return task

    baseline = _run(repo, module_rel, after_src,
                    _probe_source(after_src, calls, "base"), work / "base")
    if not baseline:
        task.reject_reason = "host does not run hermetically"
        return task

    # Walk candidate sites, accepting a defect only when it changes observable behaviour.
    working = after_src
    accepted: list[InjectedBug] = []
    used_lines: set[int] = set()
    for cls, ln, original, mutated, _why in find_defect_sites(after_src):
        if len(accepted) >= n_bugs or ln in used_lines:
            continue
        candidate = apply_bug(working, original, mutated)
        if candidate is None:
            continue
        res = _run(repo, module_rel, candidate,
                   _probe_source(candidate, calls, "mut"), work / f"m{len(accepted)}")
        if not res:
            continue
        changed = [n for n, _ in calls
                   if n in baseline and n in res
                   and baseline[n] != res[n]
                   and not res[n].startswith("RAISED:")]
        if not changed:
            continue
        # The probe must WORK on the fixed state. An earlier version accepted any probe
        # whose value differed, which let all six defects map onto a single probe that
        # merely raised AttributeError -- grading six "bugs" against one dead signal.
        if baseline[changed[0]].startswith("RAISED:"):
            continue
        # Each defect must have its own probe, otherwise one fix could satisfy several
        # "bugs" and per-bug credit would double-count it.
        probe = changed[0] if changed[0] not in {b.probe for b in accepted} else None
        if probe is None:
            continue
        bug_id = f"b{len(accepted)}_{cls}"
        working = candidate
        used_lines.add(ln)
        accepted.append(InjectedBug(
            bug_id=bug_id, class_name=cls, line=ln, original=original, mutated=mutated,
            probe=probe, probe_value_before=baseline[probe],
            probe_value_after=res[probe],
        ))

    working_probes = [n for n, _ in calls
                      if n in baseline and not baseline[n].startswith("RAISED:")]
    if len(working_probes) < 2:
        task.reject_reason = (
            f"host has {len(working_probes)} callable probe(s); its functions need "
            f"sibling objects that cannot be supplied hermetically")
        return task
    if len(accepted) < 2:
        task.reject_reason = (
            f"only {len(accepted)} behaviour-changing defect(s) found across "
            f"{len(working_probes)} usable probe(s)")
        return task

    task.bugs = accepted
    task.before_source = working
    task.test_source = _probe_source(after_src, [(c[0], c[1]) for c in calls], "keep")
    task.verified = True
    return task