#!/usr/bin/env python3
"""Extract bug-fix edit pairs (oldString -> newString) from opencode history and
attach the goal statement = nearest preceding user message in the same session."""
import json
import re
import sqlite3
from collections import defaultdict

DB = os.environ.get("BUGBENCH_HISTORY_DB",
                    str(pathlib.Path.home() / ".local/share/opencode/opencode.db"))
OUT = "/tmp/opencode/bench/data/edit_pairs.jsonl"

CODE_EXT = (".py", ".js", ".ts", ".tsx", ".jsx", ".sh", ".mjs", ".cjs", ".go",
            ".rs", ".json", ".service", ".conf", ".yaml", ".yml", ".html", ".css")
MIN_CHARS = 120          # ignore trivial 1-line edits
MAX_CHARS = 6000         # ignore giant blobs

# signals that the change was a BUG FIX, not a feature
FIX_RE = re.compile(
    r"\b(bug|fix(?:e[ds])?|broken|does ?n[o']?t work|not work(?:ing)?|crash|hang|"
    r"deadlock|stuck|freeze|leak|race|timeout|timed out|error|incorrect|wrong|"
    r"regression|revert|silently|no output|blank|empty|nan|undefined|null|"
    r"invert|revers|off.by.one|double|count leak|respawn|restart|retry|429|"
    r"500|502|503|403|404|truncat|garbage|corrupt|invalidat|drop|duplicat|"
    r"swapp|margv|order|restore|prevent|avoid|guard|clamp|validate)\w*", re.I)

# signals the change is a pure FEATURE / refactor (demote)
FEAT_RE = re.compile(
    r"\b(add|implement|create|new feature|introduce|build the|support for|"
    r"refactor|rename|move|extract|update the docs?|readme|comment|docstring)\b", re.I)


def looks_like_code(s: str) -> bool:
    if not s:
        return False
    lines = s.split("\n")
    if len(lines) < 3:
        return False
    codey = sum(1 for l in lines if re.search(
        r"(^\s*(def |class |import |from |const |let |var |function |async |export |"
        r"return |if\s|for\s|while\s|try:|except|elif |public |private |static |"
        r"#include|func |fn |impl |struct |\$\(|curl |sudo |systemctl ))"
        r"|(=\s*\S+\()|(=>)|(\{\s*$)|(^\s*[\w.-]+=)", l))
    return codey >= 2


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    sessions = {r["id"]: dict(r) for r in con.execute(
        "SELECT id,title,directory,model,agent FROM session")}

    # user texts per session, ordered
    user_msgs = defaultdict(list)
    for r in con.execute("""
        SELECT p.session_id, p.time_created, json_extract(p.data,'$.text') AS text
        FROM part p JOIN message m ON m.id=p.message_id
        WHERE json_extract(p.data,'$.type')='text'
          AND json_extract(m.data,'$.role')='user'
        ORDER BY p.session_id, p.time_created
    """):
        if r["text"]:
            user_msgs[r["session_id"]].append((r["time_created"], r["text"]))

    n = 0
    with open(OUT, "w") as f:
        for r in con.execute("""
            SELECT p.session_id, p.time_created,
                   json_extract(p.data,'$.state.input') AS inp
            FROM part p
            WHERE json_extract(p.data,'$.type')='tool'
              AND json_extract(p.data,'$.tool') IN ('edit','filesystem_edit_file','write','filesystem_write_file')
        """):
            try:
                inp = json.loads(r["inp"] or "{}")
            except Exception:
                continue
            path = inp.get("filePath") or inp.get("path") or ""
            if not path.endswith(CODE_EXT):
                continue
            old = inp.get("oldText") or inp.get("oldString") or ""
            new = inp.get("newText") or inp.get("newString") or ""
            if not old or not new:
                continue
            if not (MIN_CHARS <= len(old) <= MAX_CHARS):
                continue
            if not looks_like_code(old) and not looks_like_code(new):
                continue
            if old.strip() == new.strip():
                continue

            # nearest preceding user message = goal context
            ctx = user_msgs.get(r["session_id"], [])
            goal = ""
            goal_ts = 0
            for ts, txt in ctx:
                if ts <= r["time_created"]:
                    goal, goal_ts = txt, ts
                else:
                    break
            blob = goal + "\n" + old
            fix_hits = len(FIX_RE.findall(blob))
            feat_hits = len(FEAT_RE.findall(blob))

            rec = {
                "session_id": r["session_id"],
                "title": sessions.get(r["session_id"], {}).get("title", ""),
                "directory": sessions.get(r["session_id"], {}).get("directory", ""),
                "model": sessions.get(r["session_id"], {}).get("model", ""),
                "path": path,
                "ts": r["time_created"],
                "goal_ts": goal_ts,
                "goal": goal,
                "buggy": old,
                "fixed": new,
                "fix_signal": fix_hits,
                "feat_signal": feat_hits,
            }
            f.write(json.dumps(rec) + "\n")
            n += 1
    print("edit pairs written:", n)


if __name__ == "__main__":
    main()
