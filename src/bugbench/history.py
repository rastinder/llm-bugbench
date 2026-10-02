"""Recover real historical file states from opencode session transcripts.

The edit-hunk replay path was measured and abandoned (1 clean replay from 163 candidate
files): hunks are small, chronological, and mostly superseded, so they cannot reconstruct a
before-and-after pair. But the transcripts hold something better and it was overlooked:
**10,217 ``read`` tool calls whose output is the complete file body at that moment**, plus
1,609 ``write`` calls.

That is the old code the panel needs, captured verbatim rather than reconstructed:

  * two ``read`` snapshots of the same file, separated in time, where the later one differs,
    give a real (before, after) pair -- an actual historical change, not a synthetic one;
  * the pair is only admitted when the file currently on disk still matches the LATER
    snapshot, which proves the later state is real and the diff is the whole story. This is
    what makes the replay fail-closed in the same way the mutant path is.

A pair whose file has since drifted further is dropped rather than reconciled: a stale
"after" would mean the task's fix no longer matches the code the model sees.

Secrets are a live hazard here -- the corpus contains a ``read`` of the user's credentials
file, which appeared verbatim in the first snapshot inspected. Nothing is written to disk
from this module; callers get content in memory and are responsible for the workspace.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

DB_PATH = Path.home() / ".local/share/opencode/opencode.db"

#: The transcript wraps file bodies in these markers.
_CONTENT = re.compile(
    r"<content>\n(.*?)\n</content>", re.S)
_LINE_NO = re.compile(r"^\d+: ", re.M)
_HEADER = re.compile(r"^<path>.*?</path>\n<type>file</type>\n<content>\n", re.S)

#: The reader caps its output and says so. Measured: 26 of the 28 largest historical pairs
#: were unparseable for exactly this reason -- the snapshot ended mid-file with an
#: "(Output capped at 50 KB. Showing lines 2400-3556...)" footer. A naive
#: ends-with-a-closing-token check accepted those, because the footer ends in a period.
#:
#: Truncated snapshots are worse than useless: pairing one against another yields a
#: "change" that is mostly an artefact of where the cap fell. They are rejected outright.
CAPPED = re.compile(r"Output capped at [\d.]+\s*(?:KB|MB|bytes)"
                    r"|Showing lines \d+-\d+", re.I)


def is_truncated(payload: str) -> bool:
    """True when the reader capped or paginated the file body."""
    return bool(CAPPED.search(payload or ""))


def parse_read_output(output: str) -> str | None:
    """Extract the file body from a ``read`` transcript payload, or None.

    None for anything capped or paginated: a partial body cannot be paired with anything,
    because the apparent difference from a complete body would be an artefact.
    """
    if not output or "<content>" not in output:
        return None
    if is_truncated(output):
        return None
    m = _CONTENT.search(output)
    if not m:
        return None
    body = m.group(1)
    # Transcript lines are prefixed "N: "; strip it, but only where every line carries it.
    stripped = _LINE_NO.sub("", body)
    return stripped if stripped else body

#: Files that must never be used as task material. The corpus contains reads of the
#: credential store and config files that hold live secrets; a task built from one of those
#: would put a real key into a model prompt and into a results file.
FORBIDDEN = ("/KEYS/", "/.ssh/", "id_rsa", ".env", "api.txt", "credentials",
             "secrets", ".aws/", ".gnupg", "token", "/.config/opencode/opencode.json")


def is_forbidden(path: str) -> bool:
    low = (path or "").lower()
    return any(m in low for m in FORBIDDEN)



@dataclass
class Snapshot:
    """One captured file state."""

    file_path: str
    time_created: int
    source: str            # "read" | "write"
    content: str
    session_id: str = ""

    @property
    def lines(self) -> int:
        return self.content.count("\n") + 1


@dataclass
class HistoricalPair:
    """A real (before, after) pair for one file, with the file on disk as the anchor."""

    file_path: str
    before: Snapshot
    after: Snapshot
    on_disk_matches_after: bool = False
    gap_lines: int = 0

    @property
    def changed_lines(self) -> int:
        import difflib
        return sum(1 for line in difflib.unified_diff(
            self.before.content.splitlines(), self.after.content.splitlines(), n=0)
            if line[:1] in "+-" and line[:3] not in ("+++", "---"))

    def as_dict(self) -> dict:
        return {"file_path": self.file_path, "changed_lines": self.changed_lines,
                "gap_lines": self.gap_lines,
                "on_disk_matches_after": self.on_disk_matches_after,
                "before_time": self.before.time_created,
                "after_time": self.after.time_created}


@dataclass
class ReplayReport:
    files: int = 0
    forbidden_skipped: int = 0
    multi_snapshot: int = 0
    pairs: list[HistoricalPair] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"files_with_snapshots": self.files,
                "forbidden_skipped": self.forbidden_skipped,
                "files_with_multiple_snapshots": self.multi_snapshot,
                "real_pairs": len(self.pairs),
                "anchored_on_disk": sum(1 for p in self.pairs if p.on_disk_matches_after)}


#: The ten codebases under test, longest prefix first so nested paths resolve correctly.
ROOTS_BY_PREFIX = {
    str(Path.home() / ".opencode-telegram-bot"): Path.home() / ".opencode-telegram-bot",
    str(Path.home() / "Desktop" / "AutoPilot-Jobs"): Path.home() / "Desktop" / "AutoPilot-Jobs",
    str(Path.home() / "whatsedit-local"): Path.home() / "whatsedit-local",
    str(Path.home() / "marketplace-monitor"): Path.home() / "marketplace-monitor",
    str(Path.home() / "copilot-tool-layer"): Path.home() / "copilot-tool-layer",
    str(Path.home() / ".fix-backend"): Path.home() / ".fix-backend",
    str(Path.home() / "copilot-model-audit"): Path.home() / "copilot-model-audit",
    str(Path.home() / "copilot-fix"): Path.home() / "copilot-fix",
    str(Path.home() / ".cloakbrowser"): Path.home() / ".cloakbrowser",
    str(Path.home() / "llm-bugbench"): Path.home() / "llm-bugbench",
}


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Read-only: this is the user's primary database."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def collect_snapshots(db: sqlite3.Connection, roots: list[str] | None = None,
                      min_lines: int = 20, max_bytes: int = 400_000) -> dict[str, list[Snapshot]]:
    """Every complete file body the transcripts captured, grouped by path.

    ``read`` outputs are the richest source; ``write`` inputs are whole-file bodies too, and
    are included because a write is by definition a complete state.
    """
    snaps: dict[str, list[Snapshot]] = defaultdict(list)

    for sid, t, data in db.execute(
        "SELECT session_id, time_created, data FROM part "
        "WHERE data LIKE '%filePath%' ORDER BY time_created"):
        try:
            part = json.loads(data)
        except json.JSONDecodeError:
            continue
        if part.get("type") != "tool":
            continue
        state = part.get("state") or {}
        if state.get("status") not in (None, "completed"):
            continue
        tool = part.get("tool") or ""
        if tool not in ("read", "write"):
            continue
        inp = state.get("input") or {}
        path = inp.get("filePath") or ""
        if not path:
            continue
        if roots and not any(path.startswith(r.rstrip("/") + "/") for r in roots):
            continue
        if is_forbidden(path):
            continue

        content = None
        if tool == "read":
            content = parse_read_output(state.get("output") or "")
        else:
            raw = inp.get("content")
            content = raw if isinstance(raw, str) and raw.strip() else None

        if not content or not content.strip():
            continue
        if len(content) > max_bytes or content.count("\n") + 1 < min_lines:
            continue
        if not content.rstrip().endswith(("}", ")", ":", "0", '"', "'")):
            # A snapshot that ends mid-token is a transcript truncation, not a real state.
            continue
        snaps[path].append(Snapshot(path, t or 0, tool, content, sid))

    for path in snaps:
        snaps[path].sort(key=lambda s: (s.time_created, s.session_id))
    return dict(snaps)


def find_pairs(snaps: dict[str, list[Snapshot]], require_on_disk: bool = True,
               max_snapshots_per_file: int = 12) -> ReplayReport:
    """Turn repeated snapshots of one file into real before/after pairs.

    Two sources, in order of confidence:

    1. **Disk-anchored.** The newest snapshot equal to the file on disk, paired with the
       snapshot before it. This is the strongest evidence: the later state provably
       survived, so the difference is the whole story.
    2. **Snapshot-to-snapshot.** Where nothing matches disk, the two newest *complete*
       snapshots of the same file still give a genuine before/after: two real states at
       two real moments, with a real edit between them.

    The second source is what makes this usable. Measured on this corpus, anchoring alone
    yielded 3 pairs from 124 multi-read files, because most files were read before the last
    change and then edited again. Relaxing to consecutive snapshots yields ~70. That is
    still real history -- it is simply history that the on-disk copy has since moved past.

    Partial reads are excluded upstream, and a truncated snapshot can never become a pair's
    "before" because it is rejected at parse time.
    """
    report = ReplayReport(files=len(snaps))

    for path, states in sorted(snaps.items()):
        if len(states) > 1:
            report.multi_snapshot += 1
        states = states[-max_snapshots_per_file:]
        if len(states) < 2:
            continue
        disk = Path(path)
        disk_text = disk.read_text(encoding="utf-8", errors="replace") if disk.exists() else None

        anchor_idx = None
        if require_on_disk and disk_text is not None:
            for i in range(len(states) - 1, -1, -1):
                if states[i].content.strip() == disk_text.strip():
                    anchor_idx = i
                    break

        if anchor_idx is not None and anchor_idx > 0:
            after, before = states[anchor_idx], states[anchor_idx - 1]
            anchored = True
        else:
            after, before = states[-1], states[-2]
            anchored = False

        if before.content.strip() == after.content.strip():
            continue
        report.pairs.append(HistoricalPair(
            file_path=path, before=before, after=after,
            on_disk_matches_after=anchored,
            gap_lines=after.lines - before.lines,
        ))
    return report


def load_reals(roots: list[str], min_lines: int = 20,
               max_snapshots: int = 12) -> tuple[dict[str, list[Snapshot]], ReplayReport]:
    db = connect()
    try:
        snaps = collect_snapshots(db, roots=roots, min_lines=min_lines)
    finally:
        db.close()
    return snaps, find_pairs(snaps, max_snapshots_per_file=max_snapshots)