"""Recover historical file states from opencode's saved-change database.

The `/sessions` UI offers a revert action on a message only when opencode holds the
changes for it. Those changes live in the `part` table of `opencode.db`: every `write`
part stores the complete file content, and every `edit` part stores the exact
`oldString` and `newString` that were swapped.

Earlier attempts to recover a pre-fix state applied the recorded `oldString` to the
*current* file on disk. That fails on any file that has been edited since, which is
almost every interesting one. The fix is to rebuild the file the way the editor built
it: start from a full-content anchor (`write`, or an untruncated `read`) and replay each
subsequent `edit` in timestamp order. The state immediately before any edit is then the
buggy state, and the edit itself is the fix.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

#: `read` output wraps the file in <path>/<type>/<content> tags. The close tag is often
#: followed by an injected reminder block, so it cannot be anchored to end-of-string.
_READ_RE = re.compile(
    r"<path>.*?</path>\s*<type>\w+</type>\s*<content>\n?(.*?)(?:\n</content>|\Z)", re.S)

#: opencode's `read` output prefixes every line with its 1-based number, e.g. "42:     x".
#: Those prefixes are display formatting and must come back off, or the recovered source
#: will not parse and no oldString will ever match.
_LINENO_RE = re.compile(r"^\s*\d+: ?", re.M)

#: An offset read returns a numbered window of the file, not the file. It looks complete to
#: a length check but is missing everything outside the window, so it can never serve as a
#: reconstruction anchor. Truncation is detected by markers plus the line-1 test below.
PARTIAL_MARKERS = ("lines truncated", "lines omitted", "[...", "output too large")

#: opencode appends this footer to *every* read, including whole-file ones, so it says
#: nothing about completeness. It has to be removed before the body can be parsed.
_FOOTER_RE = re.compile(
    r"\n*\(Showing lines (\d+)-(\d+) of (\d+)\.[^)]*\)\s*(?:\Z|</system-reminder>\s*\Z)")


@dataclass
class Anchor:
    """A complete copy of a file, valid at one moment."""

    time: int
    source: str          # "write" | "read"
    content: str


@dataclass
class Edit:
    """One recorded change: `old` was replaced by `new`."""

    time: int
    old: str
    new: str
    session_id: str = ""
    file_path: str = ""


@dataclass
class State:
    """The file content on either side of one recorded edit."""

    time: int
    before: str
    after: str
    edit: Edit
    #: Set when replay had to skip an earlier edit because its `oldString` was absent.
    degraded: bool = False


@dataclass
class Timeline:
    file_path: str
    anchors: list[Anchor] = field(default_factory=list)
    edits: list[Edit] = field(default_factory=list)

    def states(self) -> list[State]:
        """Rebuild the file across time, one entry per edit that could be replayed.

        Picks the latest anchor at or before each edit rather than one anchor for the whole
        timeline: anchors are rare (a file is often edited many times without ever being
        written), and using the newest one before a given edit minimises the number of
        deltas that must replay successfully.
        """
        out: list[State] = []
        for edit in self.edits:
            anchor = self._anchor_before(edit.time)
            if anchor is None:
                continue
            content, degraded = self._replay(anchor.content, edit.time)
            if content is None:
                continue
            after = self._apply(content, edit.old, edit.new)
            if after is None:
                continue
            out.append(State(edit.time, content, after, edit, degraded))
        return out

    def _anchor_before(self, when: int) -> Anchor | None:
        usable = [a for a in self.anchors if a.time <= when]
        return max(usable, key=lambda a: a.time) if usable else None

    def _replay(self, content: str, until: int) -> tuple[str | None, bool]:
        """Apply every earlier edit in turn, in order."""
        degraded = False
        for e in self.edits:
            if e.time >= until:
                break
            nxt = self._apply(content, e.old, e.new)
            if nxt is None:
                degraded = True   # anchor is not the true base; keep going, flag it
                continue
            content = nxt
        return content, degraded

    @staticmethod
    def _apply(content: str, old: str, new: str) -> str | None:
        """Swap `old` for `new` in `content`, or None when the match is not unique.

        opencode refuses an ambiguous edit, so a recorded oldString that appears more than
        once means our reconstruction has drifted from the editor's. Returning None keeps
        that state out of the results instead of silently grading a wrong file.
        """
        if not old or content.count(old) != 1:
            return None
        return content.replace(old, new, 1)


def _strip_read(raw: str | None) -> str | None:
    """Recover whole-file source from a `read` tool output, or None if it is not that.

    None means "not usable as an anchor", which is the common case: most reads are offset
    windows, and mistaking one for the file would silently reconstruct a fragment.
    """
    if not raw:
        return None
    if any(m in raw for m in PARTIAL_MARKERS):
        return None
    m = _READ_RE.search(raw)
    if not m:
        return None
    captured = m.group(1)
    # Completeness, in two parts. A read that starts at line 1 can still be a truncated
    # window (opencode caps read output by bytes), and such a fragment ends mid-statement:
    # one captcha_solver read looked whole because it began at line 1 yet held 129 of 1327
    # lines and did not parse. The footer names the window and the file length, so
    # start==1 and end==total is the test that actually holds.
    if not re.match(r"\s*1: ?", captured):
        return None
    fm = _FOOTER_RE.search(captured)
    if not fm:
        return None
    start, end, total = (int(g) for g in fm.groups())
    if start != 1 or end != total:
        return None
    body = _FOOTER_RE.sub("", _LINENO_RE.sub("", captured))
    return body or None


def load_timeline(db: sqlite3.Connection, file_path: str) -> Timeline:
    """Collect every saved change recorded for one file path."""
    tl = Timeline(file_path=file_path)
    rows = db.execute(
        """
        SELECT time_created, session_id, json_extract(data,'$.tool')            AS tool,
               json_extract(data,'$.state.input.content')                       AS content,
               json_extract(data,'$.state.input.oldString')                    AS old_s,
               json_extract(data,'$.state.input.newString')                    AS new_s,
               json_extract(data,'$.state.output')                             AS output
        FROM part
        WHERE json_extract(data,'$.type') = 'tool'
          AND json_extract(data,'$.state.input.filePath') = ?
        ORDER BY time_created
        """,
        (file_path,),
    ).fetchall()

    for t, sess, tool, content, old_s, new_s, output in rows:
        if tool == "write" and isinstance(content, str) and content:
            tl.anchors.append(Anchor(t, "write", content))
        elif tool == "read":
            got = _strip_read(output)
            if got is not None:
                tl.anchors.append(Anchor(t, "read", got))
        elif tool == "edit" and isinstance(old_s, str) and isinstance(new_s, str) and old_s:
            tl.edits.append(Edit(t, old_s, new_s, sess, file_path))
    return tl


def edited_files(db: sqlite3.Connection, pattern: str = "%") -> list[tuple[str, int, int]]:
    """(file_path, edit_count, write_or_read_anchor_count) for every file with changes."""
    rows = db.execute(
        """
        SELECT p,
               SUM(CASE WHEN t IN ('edit') THEN 1 ELSE 0 END)          AS edits,
               SUM(CASE WHEN t = 'write' THEN 1 ELSE 0 END)            AS writes
        FROM (
            SELECT json_extract(data,'$.state.input.filePath') AS p,
                   json_extract(data,'$.tool')                AS t
            FROM part
            WHERE json_extract(data,'$.type') = 'tool'
              AND json_extract(data,'$.state.input.filePath') LIKE ?
        )
        GROUP BY p
        """,
        (pattern,),
    ).fetchall()
    return [(r[0], r[1] or 0, r[2] or 0) for r in rows]


def python_states(db: sqlite3.Connection, file_path: str) -> list[State]:
    """Reconstructed states whose before and after both parse as Python."""
    out = []
    for st in load_timeline(db, file_path).states():
        for text in (st.before, st.after):
            try:
                ast.parse(text)
            except SyntaxError:
                break
        else:
            out.append(st)
    return out


def load_all_timelines(db: sqlite3.Connection, pattern: str = "%.py") -> dict[str, Timeline]:
    """Bulk-load timelines for all matching files in a single pass over the part table."""
    cur = db.cursor()
    cur.execute(
        """
        SELECT
            json_extract(data,'$.state.input.filePath') AS path,
            time_created,
            session_id,
            json_extract(data,'$.tool') AS tool,
            json_extract(data,'$.state.input.content') AS content,
            json_extract(data,'$.state.input.oldString') AS old_s,
            json_extract(data,'$.state.input.newString') AS new_s,
            json_extract(data,'$.state.output') AS output
        FROM part
        WHERE json_extract(data,'$.type') = 'tool'
          AND path LIKE ?
        ORDER BY time_created
        """,
        (pattern,),
    )
    timelines: dict[str, Timeline] = {}
    for path, t, sess, tool, content, old_s, new_s, output in cur.fetchall():
        if not path:
            continue
        if path not in timelines:
            timelines[path] = Timeline(file_path=path)
        tl = timelines[path]
        if tool == 'write' and isinstance(content, str) and content:
            tl.anchors.append(Anchor(t, 'write', content))
        elif tool == 'read':
            got = _strip_read(output)
            if got is not None:
                tl.anchors.append(Anchor(t, 'read', got))
        elif tool == 'edit' and isinstance(old_s, str) and isinstance(new_s, str) and old_s:
            tl.edits.append(Edit(t, old_s, new_s, sess, path))
    return timelines
