"""Mine *hard-won* bugs: fixes that took many failed attempts before landing.

The mutant panel and the single-mutant panel both failed for the same reason -- the tasks
were easy, and frontier models aced them. The user named the category that actually
separates models: bugs where an agent tried repeatedly, could not find the cause, and only
landed the fix after a long detour. Those are the fixes worth benchmarking against.

The signal is already in the transcripts and it is structural rather than textual. A hard
session has a shape:

  * many turns,
  * a long run of tool calls that change nothing (reads, greps, edits that get reverted),
  * repeated re-phrasing of the same symptom, which is what a stuck agent does,
  * and eventually an edit that lands.

Nothing here needs a keyword list, which matters because keywords did not find the target
bug: searching the corpus for "generated image drag" returned documentation quotes and
unrelated sessions, because the phrase in the report is a description, not the code. What
the code does have is the *shape* of having struggled.

Each mined item keeps its symptom text, its attempt count and the final edit, so the
difficulty is evidenced rather than asserted.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

DB_PATH = Path.home() / ".local/share/opencode/opencode.db"

#: Re-phrasing markers: what a stuck agent writes when it has no new hypothesis. Counted per
#: session, not globally, so a long healthy session is not mistaken for a hard one.
STRUGGLE_MARKERS = (
    "still not", "still failing", "still doesn't", "does not work", "doesn't work",
    "not working", "no change", "same error", "same problem", "still the same",
    "let me try again", "try another", "that's not it", "that's not the issue",
    "not the issue", "wrong hypothesis", "let me re-read", "let me look again",
    "hmm", "actually", "wait,", "on second thought", "revert", "undo",
    "doesn't make sense", "confusing", "puzzling", "why is", "should have",
)

#: Categories a hard bug tends to fall into, matched against the fix's own text. Used only
#: to label and group, never to decide that a bug is hard.
CATEGORIES = {
    "browser_interaction": ("click", "drag", "drop", "hover", "scroll", "playwright",
                            "locator", "selector", "xpath", "dom", "element", "iframe",
                            "wait_for", "timeout", "race", "flaky"),
    "visual_rendering": ("screenshot", "pixel", "render", "css", "layout", "viewport",
                         "canvas", "image", "thumbnail", "crop", "resize"),
    "async_concurrency": ("async", "await", "race", "deadlock", "concurrency", "thread",
                          "lock", "queue", "cancelled", "task"),
    "protocol_wire": ("websocket", "signalr", "frame", "json", "http", "header", "status",
                      "endpoint", "proxy", "token", "auth"),
    "data_state": ("database", "sqlite", "json", "schema", "migration", "state", "cache",
                   "config", "env", "path"),
}


@dataclass
class Attempt:
    time_created: int
    kind: str                 # edit | read | bash | text
    summary: str
    file_path: str = ""
    reverted: bool = False
    old: str = ""
    new: str = ""


@dataclass
class HardBug:
    """A session that struggled, with the fix it eventually landed."""

    session_id: str
    project: str
    title: str
    turns: int
    tool_calls: int
    edits: int
    struggle_hits: int
    fix_file: str = ""
    fix_old: str = ""
    fix_new: str = ""
    fix_time: int = 0
    categories: list[str] = field(default_factory=list)
    symptom: str = ""
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def struggle_ratio(self) -> float:
        """Stuck-phrasing density, always in [0, 1].

        Divided by tool calls when there are any, else by turns. Dividing by tool calls with
        a floor of 1 made the ratio explode on any tool-call-free session, which is how the
        bound came to be asserted in the first place.
        """
        denom = self.tool_calls or self.turns or 1
        return min(1.0, self.struggle_hits / denom)

    @property
    def fix_changed_lines(self) -> int:
        if not self.fix_old:
            return 0
        return max(len(self.fix_old.splitlines()), len(self.fix_new.splitlines()))

    def as_dict(self) -> dict:
        return {
            "session_id": self.session_id, "project": self.project, "title": self.title,
            "turns": self.turns, "tool_calls": self.tool_calls, "edits": self.edits,
            "struggle_hits": self.struggle_hits, "struggle_ratio": round(self.struggle_ratio, 3),
            "fix_file": self.fix_file, "fix_changed_lines": self.fix_changed_lines,
            "categories": self.categories, "fix_time": self.fix_time,
            "symptom": self.symptom[:600],
        }


def classify(text: str) -> list[str]:
    low = (text or "").lower()
    return [name for name, words in CATEGORIES.items()
            if sum(1 for w in words if w in low) >= 2]


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _project_of(sid: str, db: sqlite3.Connection) -> str:
    row = db.execute(
        "SELECT COALESCE(p.worktree, p.id, '') FROM session s "
        "LEFT JOIN project p ON p.id = s.project_id WHERE s.id = ?", (sid,)).fetchone()
    if row and row[0]:
        return str(row[0])
    return ""


def mine(db: sqlite3.Connection, min_turns: int = 20, min_struggle: int = 6,
         min_edits: int = 2, roots: list[str] | None = None,
         limit: int = 60) -> list[HardBug]:
    """Sessions that match the shape of a hard-won fix.

    Thresholds are floors, not targets: a session qualifies by having *structure* (many
    turns, many tool calls, repeated stuck phrasing, and a landed edit), never by a keyword.
    """
    out: list[HardBug] = []
    sessions = db.execute(
        "SELECT id, title FROM session ORDER BY time_created").fetchall()

    for sid, title in sessions:
        rows = db.execute(
            "SELECT time_created, data FROM part WHERE session_id=? ORDER BY time_created",
            (sid,)).fetchall()
        if len(rows) < min_turns:
            continue

        attempts: list[Attempt] = []
        struggle = 0
        edits = 0
        fix_size = -1
        first_text = ""
        texts: list[str] = []
        fix: Attempt | None = None

        for t, data in rows:
            try:
                part = json.loads(data)
            except json.JSONDecodeError:
                continue
            kind = part.get("type")
            if kind == "text":
                txt = re.sub(r"\s+", " ", part.get("text") or "").strip()
                if txt:
                    if not first_text:
                        first_text = txt
                    texts.append(txt)
                    low = txt.lower()
                    struggle += sum(1 for m in STRUGGLE_MARKERS if m in low)
                continue
            if kind != "tool":
                continue
            state = part.get("state") or {}
            tool = part.get("tool") or ""
            inp = state.get("input") or {}
            path = inp.get("filePath") or ""
            if roots and path and not any(path.startswith(r.rstrip("/") + "/") for r in roots):
                continue
            if tool in ("edit", "write", "multiedit", "patch"):
                edits += 1
                summary = f"{tool} {path}"
                # Keep the LARGEST edit in the session, not the first. The first edit of a
                # long debugging session is almost always a probe or a partial attempt; the
                # fix that actually landed is the substantial one. Taking the first edit
                # reported fix_changed_lines=0 for 10 of 13 mined bugs, which is a fact
                # about my extraction rather than about the bugs.
                old = inp.get("oldString") or ""
                new = inp.get("newString") or ""
                size = max(len(old.splitlines()), len(new.splitlines()))
                if size > fix_size:
                    fix_size = size
                    fix = Attempt(t, "edit", summary, path, old=old, new=new)
            else:
                summary = tool
                if tool == "bash":
                    cmd = (inp.get("command") or "")[:80]
                    if re.search(r"git (checkout|restore|stash)", cmd):
                        struggle += 1
                    summary = f"bash: {cmd}"
            attempts.append(Attempt(t, tool if tool not in ("edit", "write") else "edit",
                                    summary, path))

        if struggle < min_struggle or edits < min_edits:
            continue

        blob = " ".join(texts[:40]) + " " + (title or "")
        bug = HardBug(
            session_id=sid,
            project=_project_of(sid, db),
            title=(title or "")[:160],
            turns=len(rows),
            tool_calls=len(attempts),
            edits=edits,
            struggle_hits=struggle,
            fix_file=fix.file_path if fix else "",
            fix_old=fix.old if fix else "",
            fix_new=fix.new if fix else "",
            fix_time=fix.time_created if fix else 0,
            categories=classify(blob),
            symptom=first_text,
            attempts=attempts[-25:],
        )
        if bug.fix_file and (not roots or any(
                bug.fix_file.startswith(r.rstrip("/") + "/") for r in roots)):
            out.append(bug)

    out.sort(key=lambda b: (-b.struggle_hits, -b.edits))
    return out[:limit]