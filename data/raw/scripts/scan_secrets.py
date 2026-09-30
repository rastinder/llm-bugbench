#!/usr/bin/env python3
"""Scan the benchmark dataset for secrets, PII and identifying paths BEFORE any publish."""
from __future__ import annotations

import json
import re
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
from collections import Counter, defaultdict

sys.path.insert(0, str(ROOT / "/src"))
from bugbench.models import load_tasks  # noqa: E402

PATTERNS = {
    "openai_key": r"\bsk-[A-Za-z0-9_-]{20,}",
    "anthropic_key": r"\bsk-ant-[A-Za-z0-9_-]{20,}",
    "litellm_key": r"\bsk-litellm-[A-Za-z0-9_-]{10,}",
    "cerebras_key": r"\bcsk-[A-Za-z0-9]{20,}",
    "cloudflare_token": r"\bcf[a-zA-Z0-9_-]{30,}\b",
    "github_token": r"\bgh[pousr]_[A-Za-z0-9]{30,}",
    "aws_key": r"\bAKIA[0-9A-Z]{16}\b",
    "slack_token": r"\bxox[baprs]-[A-Za-z0-9-]{10,}",
    "stripe_key": r"\b[sr]k_(live|test)_[A-Za-z0-9]{20,}",
    "google_api": r"\bAIza[0-9A-Za-z_-]{30,}",
    "bearer_header": r"(?i)authorization:\s*bearer\s+\S{15,}",
    "basic_auth_url": r"https?://[^/\s:@]+:[^/\s@]+@",
    "jwt": r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}",
    "private_key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "email": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
    "phone": r"(?<!\d)(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?!\d)",
    "ipv4": r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b",
    "user_path": r"/(?:home|Users)/[A-Za-z0-9._-]+",
    "windows_path": r"[A-Z]:\\\\?Users\\\\?[A-Za-z0-9._-]+",
    "aws_creds_file": r"\.aws/credentials",
    "db_conn_string": r"(?i)\b[a-z0-9+]+://[^\s:@/]+:[^\s:@/]+@[^\s/]+",
    "phone_numbered_id": r"\b\d{3}-\d{3}-\d{4}\b",
    "hivemail": r"\b8976091@ups\.com\b",
    "ip_like_hash": r"\b10\.0\.0\.\d{1,3}\b",
    "domain_expensive": r"\bmycondovalue\.ca\b|\bkingerhomes\.ca\b",
    "m365_upn": r"\b8976091\b",
}

COMPILED = {k: re.compile(v) for k, v in PATTERNS.items()}

# allowlist: values that are NOT secrets (our own benchmark config, documented public hosts)
ALLOW = {
    ("litellm_key", "sk-litellm-vps-2026"),
    ("domain_expensive", "mycondovalue.ca"),
}


def main() -> int:
    tasks = load_tasks()
    fields = ["goal", "original_goal", "buggy", "reference_fix", "file_name",
              "project", "source_session", "source_title", "category_reason",
              "source_title"]
    hits = defaultdict(Counter)
    examples = defaultdict(list)

    for t in tasks:
        for f in fields:
            val = getattr(t, f, "") or ""
            if not isinstance(val, str):
                continue
            for name, rx in COMPILED.items():
                for m in rx.findall(val):
                    if (name, m) in ALLOW:
                        continue
                    hits[name][m] += 1
                    if len(examples[name]) < 3:
                        examples[name].append((t.task_id, f, m[:90]))

    total = sum(sum(c.values()) for c in hits.values())
    print(f"scanned {len(tasks)} tasks x {len(fields)} fields")
    print(f"TOTAL findings: {total}\n")
    for name in sorted(hits, key=lambda k: -sum(hits[k].values())):
        n = sum(hits[name].values())
        uniq = len(hits[name])
        print(f"[{name}] {n} occurrences, {uniq} distinct")
        for tid, f, ex in examples[name]:
            print(f"    {tid} [{f}] {ex!r}")
        others = [v for v in hits[name] if v not in [e[2] for e in examples[name]]][:5]
        if others:
            print(f"    others: {others}")
        print()
    if not total:
        print("CLEAN - no findings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
