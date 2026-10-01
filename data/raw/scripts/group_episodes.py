#!/usr/bin/env python3
"""Group edit pairs into bug-fix EPISODES (one user goal + sequential edits to the
same file), then emit benchmark tasks: buggy snippet + goal + hidden fixed snippet."""
import json
import re
from collections import defaultdict

IN = "/tmp/opencode/bench/data/pairs_bugfix.json"
OUT = "/tmp/opencode/bench/data/episodes.jsonl"

rows = json.load(open(IN))

# group key: session + goal_ts + file path  -> all edits for one bug on one file
g = defaultdict(list)
for r in rows:
    g[(r["session_id"], r["goal_ts"], r["path"])].append(r)

episodes = []
for key, edits in g.items():
    edits.sort(key=lambda r: r["ts"])
    first = edits[0]
    last = edits[-1]
    buggy = first["buggy"]
    fixed = last["fixed"]
    if buggy.strip() == fixed.strip():
        continue
    episodes.append({
        "episode_id": f"EP{len(episodes)+1:04d}",
        "session_id": key[0],
        "title": first["title"],
        "directory": first["directory"],
        "path": first["path"],
        "goal": first["goal"],
        "n_edits": len(edits),
        "buggy": buggy,
        "fixed": fixed,
        "fix_signal": max(e["fix_signal"] for e in edits),
        "feat_signal": max(e["feat_signal"] for e in edits),
        "ts": first["ts"],
        "all_edits": [{"buggy": e["buggy"], "fixed": e["fixed"]} for e in edits],
    })

episodes.sort(key=lambda e: (-e["fix_signal"], -e["n_edits"]))
json.dump(episodes, open("/tmp/opencode/bench/data/episodes.json", "w"), indent=1)
with open(OUT, "w") as f:
    for e in episodes:
        f.write(json.dumps(e) + "\n")

print("episodes:", len(episodes))
good = [e for e in episodes if len(e["goal"]) > 40 and e["fix_signal"] >= 2]
print("episodes with real goal + fix signal >=2:", len(good))
for e in good[:25]:
    print(f"  [{e['fix_signal']:2d}] edits={e['n_edits']:2d} "
          f"{e['path'].split('/')[-1]:26s} {e['goal'][:70]!r}")
