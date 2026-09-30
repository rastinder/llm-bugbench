#!/usr/bin/env python3
"""Dump all user text parts + assistant replies to JSONL for analysis."""
import json
import sqlite3

DB = "/home/ras/.local/share/opencode/opencode.db"

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

out = open("/tmp/opencode/bench/data/all_user.jsonl", "w")
n = 0
for r in con.execute("""
    SELECT p.session_id, p.time_created, p.id AS pid,
           json_extract(p.data,'$.text') AS text
    FROM part p JOIN message m ON m.id=p.message_id
    WHERE json_extract(p.data,'$.type')='text'
      AND json_extract(m.data,'$.role')='user'
    ORDER BY p.session_id, p.time_created
"""):
    out.write(json.dumps({"s": r["session_id"], "t": r["time_created"],
                          "pid": r["pid"], "text": r["text"]}) + "\n")
    n += 1
out.close()
print("dumped", n)

# session meta
sm = open("/tmp/opencode/bench/data/sessions.jsonl", "w")
for r in con.execute("SELECT id, title, directory, model, agent FROM session"):
    sm.write(json.dumps(dict(r)) + "\n")
sm.close()
print("sessions done")
