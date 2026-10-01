"""The feedback channel: what a model is allowed to see when a test fails.

This module exists because the obvious implementation leaks. pytest's traceback prints
the failing assertion's own source, so "show the model the failure" and "never show the
model the test" are contradictory if you feed it pytest's stdout. Empirically (pytest
9.1.1) no `--tb` value escapes this:

    long / short / line   print the hidden test's source lines
    --tb=no                still prints the assertion message in the summary line

So the channel never renders pytest's output. It runs the suite, reads a machine-readable
report, and rebuilds the feedback from node IDs alone -- every failing test at once,
which is the triage behaviour we want, with nothing from the test body in it.

Two secondary findings are enforced here rather than left to operator discipline:

  * Python writes `test/__pycache__/*.pyc` at the default umask (world-readable) and the
    hidden source is recoverable from it as plain text, which bypasses every other
    isolation layer. Bytecode writing is therefore disabled, not merely discouraged.
  * Node's default reporter leaks the test *name*, message, expected/actual and stack.
    A title like "returns 401 when token expired" is semantic leakage, so JS node IDs
    are reduced to an opaque positional token.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

#: pytest flags chosen so that IF our reporter ever fails, the fallback output is still
#: not the full traceback. Belt and braces: we do not read stdout at all, but a future
#: debugging session might.
PYTEST_FLAGS = (
    "--tb=no",            # no source frames
    "--no-header",        # no platform/plugin noise
    "--show-capture=no",  # captured stdout is a model-code exfiltration channel
    "-p", "no:cacheprovider",
)

#: coarse outcome buckets only. Internal repr class names leak the runner's internals.
_COARSE = {
    "failed": "Failure",
    "error": "Error",
}


@dataclass(frozen=True)
class FeedbackRow:
    """One test outcome, reduced to the minimum a model needs to triage."""

    nodeid: str
    outcome: str
    exc_type: str | None = None

    def as_dict(self) -> dict:
        return {"nodeid": self.nodeid, "outcome": self.outcome, "exc_type": self.exc_type}


@dataclass
class FeedbackChannel:
    """Runs a hidden test file and returns leak-free feedback rows."""

    root: Path
    python: str = "python3"
    node: str = "node"
    timeout: int = 300
    _report: Path = field(default=None, repr=False)

    def run(self, test_path: str, runner: str = "pytest") -> list[dict]:
        if runner == "node":
            return [r.as_dict() for r in collect_node_rows(self.root, self.node, test_path)]
        return collect_pytest_rows(self.root, self.python, test_path, timeout=self.timeout)


def _opaque(nodeid: str) -> str:
    """A stable, content-free label for a test.

    Used for JavaScript, where the test *name* is authored by us and is often a sentence
    describing the expected behaviour ("returns 401 when token expired"). Hashing keeps
    the failure list usable for triage across attempts without exposing the semantics.
    """
    digest = hashlib.sha256(nodeid.encode("utf-8", "replace")).hexdigest()[:8]
    return f"case_{digest}"


def _env() -> dict:
    """A deterministic, leak-resistant environment for the child run."""
    env = dict(os.environ)
    # Bytecode would otherwise be written world-readable next to the hidden tests.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONHASHSEED"] = "0"
    env["TZ"] = "UTC"
    env["LC_ALL"] = "C"
    env["NO_COLOR"] = "1"
    env["COLUMNS"] = "80"
    env["TERM"] = "dumb"
    return env


def _umask_077() -> None:
    """Child processes inherit our umask; 077 makes anything they *do* write private."""
    os.umask(0o077)


def _module_to_path(root: Path, classname: str | None) -> str:
    """Turn JUnit's dotted ``classname`` back into a path relative to ``root``.

    JUnit reports ``test.test_hidden`` for a file at ``test/test_hidden.py``. Using that
    verbatim would hand the model the hidden test's package layout for free, and the
    label would not match any real path, so it is converted back deliberately.
    """
    parts = [p for p in (classname or "").split(".") if p and p != "rootdir"]
    if not parts:
        return "<unknown>"
    rel = Path(*parts).with_suffix(".py")
    try:
        return str(rel.relative_to(root))
    except ValueError:
        # classname escaped the project root; keep only the basename rather than
        # exposing an absolute path.
        return rel.name


def collect_pytest_rows(root, python: str, test_path: str, timeout: int = 300) -> list[dict]:
    """Run pytest, then read back only node IDs from its JUnit report.

    pytest's own stdout is captured to DEVNULL rather than parsed: it is the single
    largest leak surface, and parsing it is exactly the mistake this module documents.
    """
    root = Path(root)
    report = root / ".bugbench-junit.xml"
    try:
        report.unlink()
    except FileNotFoundError:
        pass

    cmd = [
        python, "-m", "pytest", test_path,
        *PYTEST_FLAGS,
        "--junit-xml", str(report),
        "-q",
    ]
    _umask_077()
    try:
        subprocess.run(
            cmd,
            cwd=root,
            env=_env(),
            stdout=subprocess.DEVNULL,   # quarantined: never shown to a model
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return [{"nodeid": test_path, "outcome": "error", "exc_type": "Error"}]

    if not report.exists():
        # The run died before writing a report. The overwhelmingly common cause is a
        # collection error (bad import, syntax error, missing package), which MUST
        # surface as a failure: a test that cannot even be collected has not passed, and
        # silently returning [] here would let a broken export look like a green run.
        return [{"nodeid": test_path, "outcome": "error", "exc_type": "Error"}]

    try:
        tree = ET.parse(report)
    except ET.ParseError:
        return [{"nodeid": test_path, "outcome": "error", "exc_type": "Error"}]
    finally:
        try:
            report.unlink()
        except OSError:
            pass

    rows: list[dict] = []
    for case in tree.iter("testcase"):
        nodeid = f"{_module_to_path(root, case.get('classname'))}::{case.get('name')}"
        outcome, exc = "passed", None
        for child in case:
            if child.tag in ("failure", "error"):
                outcome = "failed" if child.tag == "failure" else "error"
                exc = _COARSE.get(outcome)
                break
            if child.tag == "skipped":
                outcome = "skipped"
                break
        rows.append({"nodeid": nodeid, "outcome": outcome, "exc_type": exc})

    if not rows:
        # pytest writes a well-formed but EMPTY junit report when it collects nothing
        # (a collection error, a bad path, a deselected suite). That is indistinguishable
        # from "everything passed" unless it is caught here, and reading it as success
        # would let a vanished test suite score as a clean fix.
        return [{"nodeid": test_path, "outcome": "error", "exc_type": "Error"}]
    return rows


#: node's TAP reporter emits "not ok N - <test name>" plus expected/actual and a stack.
_TAP_FAIL = re.compile(r"^not ok \d+ - (?P<title>.*)$")


def collect_node_rows(root, node: str, test_path: str, timeout: int = 300) -> list[dict]:
    """Run `node --test` and reduce failures to opaque tokens.

    `--test-reporter=tap` gives one parseable line per test. The title is discarded on
    purpose: it is a sentence about expected behaviour and therefore a leak channel that
    pytest's `classname::name` does not have.
    """
    root = Path(root)
    _umask_077()
    try:
        proc = subprocess.run(
            [node, "--test", "--test-concurrency=1", test_path],
            cwd=root,
            env=_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return [{"nodeid": _opaque(test_path), "outcome": "error", "exc_type": "Error"}]

    rows: list[dict] = []
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("ok ") or re.match(r"^ok \d+ - ", stripped):
            nodeid = stripped.split(" - ", 1)[1] if " - " in stripped else stripped
            rows.append({"nodeid": _opaque(nodeid), "outcome": "passed", "exc_type": None})
        elif _TAP_FAIL.match(stripped):
            title = _TAP_FAIL.match(stripped).group("title").strip()
            rows.append({"nodeid": _opaque(title), "outcome": "failed", "exc_type": "Failure"})

    if not rows:
        rows.append({"nodeid": _opaque(test_path), "outcome": "error", "exc_type": "Error"})
    return rows


def render_feedback(rows: list[dict], label: str = "") -> str:
    """All failures at once, node IDs only.

    Deliberately terse. Every token here is shown to a model, so the format carries no
    prose, no counts of how many cases exist beyond what failed, and nothing derived
    from the test body.
    """
    failures = [r for r in rows if r.get("outcome") in {"failed", "error"}]
    if not failures:
        return ""
    lines = []
    for row in failures:
        kind = row.get("exc_type") or "Failure"
        lines.append(f"FAILED {row['nodeid']} [{kind}]")
    total = len(rows)
    lines.append(f"--- {len(failures)} failed / {total} executed ---")
    if label:
        lines.insert(0, f"# {label}")
    return "\n".join(lines) + "\n"
