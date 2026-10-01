#!/usr/bin/env python3
"""Find user messages that (a) contain pasted code and (b) express a fix attempt
or alternatives ('try x or y', 'instead', 'use ... instead', 'why not ...').
Score + print for manual triage."""
import json
import re
import sys

IN = "/tmp/opencode/bench/data/all_user.jsonl"

CODE_RE = re.compile(
    r"(^\s*(def |class |import |from \w+ import|const |let |var |function |async |await |export |"
    r"public |private |protected |static |void |int |return |if\s*\(|for\s*\(|while\s*\(|"
    r"switch|case |try:|except|elif |#include|package |func |fn |impl |struct |require\()"
    r")"
    r"|(=>|\bself\.|\.\w+\(.*\)|\{\}|\}\s*$|=>\s*\{)"
    r"|(</\w+>|<\w+\s+\w+|/>)"
    r"|(^[\w./-]+\.(py|js|ts|sh|json|md|service|conf|yaml|yml)\b)"
    r"|(curl\s+-|sudo\s+|systemctl\s+|journalctl\s+|pip\s+install|npm\s+install|ffmpeg\s+)"
    r"|(^\s*[-*]\s*\[?[ x]\]?)"
)
BUG_RE = re.compile(
    r"\b(bug|bugz|broken|breaks?|break\b|doesn'?t work|not work|does not work|fail(?:s|ed|ure)?|"
    r"error|wrong|issue|hang(?:s|ing)?|crash(?:es|ed)?|deadlock|dead lock|stuck|leak|"
    r"race condition|infinite|loop forever|truncat\w*|timeout|timed out|blank|empty output|"
    r"silently|no output|not installing|not applied|refus\w*|reject\w*|invalid|undefined|null|"
    r"NaN|regression|reverted|stopped working|freez\w*|corrupt|reversed|swap(?:ped)?|"
    r"inverted|off by one|dup(?:e|licate)|memory leak|stack|500|502|503|429|404|403)\b", re.I)

ALT_RE = re.compile(
    r"\b(try\b|instead|alternativ\w*|why (?:not|did|does|don't|is)|should be|shouldn'?t|"
    r"change (?:it |this |that )?to|replace|swap|use [\w.#-]+ instead|or (?:else )?|"
    r"maybe|how about|what if|let'?s|pls use|use best judgment|option)\b", re.I)

rows = [json.loads(l) for l in open(IN)]
sess = {json.loads(l)["id"]: json.loads(l) for l in open(
    "/tmp/opencode/bench/data/sessions.jsonl")}

scored = []
for r in rows:
    t = r["text"]
    lines = t.split("\n")
    cl = sum(1 for l in lines if CODE_RE.search(l))
    fenced = t.count("```")
    bug = len(BUG_RE.findall(t))
    alt = len(ALT_RE.findall(t))
    # needs real pasted code: 3+ code lines OR a fenced block
    if cl < 3 and fenced < 2:
        continue
    score = min(cl, 12) * 2 + fenced * 4 + min(bug, 6) + min(alt, 4)
    scored.append({
        "score": score, "code_lines": cl, "fenced": fenced,
        "bugs": bug, "alts": alt, "len": len(t),
        "session": r["s"], "title": sess.get(r["s"], {}).get("title", "?"),
        "dir": sess.get(r["s"], {}).get("directory", ""),
        "pid": r["pid"], "ts": r["t"], "text": t,
    })

scored.sort(key=lambda x: -x["score"])
json.dump(scored, open("/tmp/opencode/bench/data/candidates.json", "w"), indent=1)
print(f"candidates: {len(scored)}")
for c in scored[:60]:
    print(f"[{c['score']:3d}] code={c['code_lines']:3d} fence={c['fenced']} "
          f"bug={c['bugs']:2d} alt={c['alts']:2d} len={c['len']:6d} "
          f"{(c['dir'] or '')[-34:]:34s} {c['title'][:44]}")
