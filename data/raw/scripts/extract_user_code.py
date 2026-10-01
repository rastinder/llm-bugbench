#!/usr/bin/env python3
"""Extract user messages containing pasted code snippets from opencode history DB."""
import json
import sqlite3
import sys
import time

DB = "/home/ras/.local/share/opencode/opencode.db"
OUT = "/tmp/opencode/bench/data/user_code_raw.jsonl"

CODE_HINTS = [
    "def ", "import ", "function ", "const ", "let ", "var ", "async ", "await ",
    "return ", "<script", "</div>", "</html>", "class ", "self.", "=>", "();",
    "```", "public ", "private ", "static ", "void ", "printf(", "SELECT ",
    "npm ", "pip ", "sudo ", "systemctl ", "journalctl ", "curl ",
]


def is_codey(text: str) -> bool:
    return any(h in text for h in CODE_HINTS)


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute("""
        SELECT p.id AS pid, p.session_id, p.time_created,
               json_extract(p.data,'$.text') AS text,
               json_extract(m.data,'$.role') AS role
        FROM part p
        JOIN message m ON m.id = p.message_id
        WHERE json_extract(p.data,'$.type')='text'
          AND json_extract(m.data,'$.role')='user'
        ORDER BY p.time_created ASC
    """)
    rows = cur.fetchall()
    print(f"user text parts: {len(rows)}", file=sys.stderr)

    # session metadata
    sess = {}
    for r in con.execute("SELECT id, title, directory, model FROM session"):
        sess[r["id"]] = {
            "title": r["title"],
            "directory": r["directory"],
            "model": r["model"],
        }
    print(f"sessions: {len(sess)}", file=sys.stderr)

    kept = 0
    with open(OUT, "w") as f:
        for r in rows:
            t = r["text"] or ""
            if not is_codey(t):
                continue
            rec = {
                "part_id": r["pid"],
                "session_id": r["session_id"],
                "ts": r["time_created"],
                "session": sess.get(r["session_id"], {}),
                "text": t,
            }
            f.write(json.dumps(rec) + "\n")
            kept += 1
    print(f"kept codey user msgs: {kept} -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"elapsed {time.time()-t0:.1f}s", file=sys.stderr)
