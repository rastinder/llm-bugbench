"""Reconstruct a file's earlier states by replaying chronological edit hunks.

The corpus is an opencode SQLite DB: ~253k parts, of which the useful ones are completed
`edit`/`write` tool calls, each holding a `filePath` plus an `oldString`/`newString` pair.
Nothing on disk is versioned -- 8 of the 10 target codebases have no git at all -- so the
DB is the only record of what those files looked like before they were fixed.

The replay is deliberately FAIL-CLOSED. A wrong reconstruction is worse than no
reconstruction: it produces a "buggy" state that never existed, and every score derived
from it is fiction that still looks like data. So a hunk applies only if its oldString
matches exactly once. Zero matches and multiple matches are both quarantine conditions,
never a licence to patch by hand.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

DB_PATH = Path.home() / ".local/share/opencode/opencode.db"

EDIT_TOOLS = {"edit", "write", "multiedit", "patch"}


@dataclass(frozen=True)
class Hunk:
    """One recorded edit."""

    session_id: str
    time_created: int
    file_path: str
    old: str
    new: str
    seq: int

    @property
    def order_key(self) -> tuple:
        # time alone can tie within a millisecond; seq breaks ties deterministically.
        return (self.time_created, self.seq)


@dataclass
class ReplayResult:
    """Outcome of replaying hunks for one file."""

    file_path: str
    ok: bool
    reason: str = ""
    applied: int = 0
    skipped: int = 0
    states: list[str] = field(default_factory=list)  # snapshot after each hunk
    hunk_order_hash: str = ""

    @property
    def digest(self) -> str:
        return hashlib.sha256("\n".join(self.states).encode("utf-8", "replace")).hexdigest()


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Open the corpus read-only. It is the user's primary database."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def load_hunks(
    db: sqlite3.Connection,
    file_path: str,
    session_ids: set[str] | None = None,
) -> list[Hunk]:
    """All recorded edits to one file, in a deterministic order.

    Reads the raw `data` column and pulls out the tool envelope rather than trusting a
    schema that has already changed shape once during development.
    """
    rows = db.execute(
        "SELECT session_id, time_created, data FROM part "
        "WHERE data LIKE ? ORDER BY time_created, id",
        (f'%"filePath": "{file_path}"%',),
    ).fetchall()

    hunks: list[Hunk] = []
    for seq, (sid, t_created, data) in enumerate(rows):
        if session_ids is not None and sid not in session_ids:
            continue
        try:
            part = json.loads(data)
        except json.JSONDecodeError:
            continue
        state = part.get("state") or {}
        if state.get("status") not in (None, "completed"):
            continue
        tool = (part.get("tool") or "").strip()
        if tool not in EDIT_TOOLS:
            continue
        inp = state.get("input") or {}
        if inp.get("filePath") != file_path:
            continue
        hunks.append(
            Hunk(
                session_id=sid,
                time_created=t_created or 0,
                file_path=file_path,
                old=inp.get("oldString") or "",
                new=inp.get("newString") or "",
                seq=seq,
            )
        )
    hunks.sort(key=lambda h: h.order_key)
    return hunks


def replay(hunks: list[Hunk], final_text: str | None = None) -> ReplayResult:
    """Walk hunks backwards from the final text to recover each earlier state.

    Reverse replay is what makes exact matching meaningful: the newest hunk must match
    the file we can actually see, which validates the whole chain. A hunk that does not
    match is recorded as skipped and the run is marked not-ok, because a chain that
    cannot be walked cannot be trusted to describe a state that existed.
    """
    if not hunks:
        return ReplayResult(hunks[0].file_path if hunks else "", False, "no hunks")

    path = hunks[0].file_path

    if final_text is None:
        p = Path(path)
        if not p.exists():
            return ReplayResult(path, False, "no final text and file absent from disk")
        final_text = p.read_text(encoding="utf-8", errors="replace")

    text = final_text
    states: list[str] = []
    applied = 0
    skipped = 0
    problems: list[str] = []

    for hunk in reversed(hunks):
        if not hunk.old:
            # A `write` of a whole file has no oldString; there is nothing to verify.
            skipped += 1
            problems.append(f"{hunk.order_key}: empty oldString")
            continue
        count = text.count(hunk.old)
        if count == 1:
            text = text.replace(hunk.old, hunk.new, 1)
            states.append(text)
            applied += 1
        else:
            skipped += 1
            problems.append(f"{hunk.order_key}: oldString matched {count} times")

    order_hash = hashlib.sha256(
        "|".join(f"{h.order_key[0]}:{h.order_key[1]}" for h in hunks).encode()
    ).hexdigest()

    states.reverse()
    ok = skipped == 0 and applied > 0
    reason = "" if ok else f"{skipped} hunk(s) did not apply exactly once"
    return ReplayResult(
        file_path=path,
        ok=ok,
        reason=reason,
        applied=applied,
        skipped=skipped,
        states=states,
        hunk_order_hash=order_hash,
    )


def quarantine_report(results: list[ReplayResult]) -> dict:
    """Summarise a replay batch. Quarantined files never enter the panel."""
    ok = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]
    return {
        "attempted": len(results),
        "admissible": len(ok),
        "quarantined": len(bad),
        "quarantine_reasons": sorted({r.reason for r in bad}),
        "files": [
            {"file_path": r.file_path, "ok": r.ok, "applied": r.applied,
             "skipped": r.skipped, "states": len(r.states)}
            for r in results
        ],
    }
