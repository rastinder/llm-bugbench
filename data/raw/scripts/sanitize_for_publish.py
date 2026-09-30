#!/usr/bin/env python3
"""Produce a REDACTED, publishable copy of the benchmark dataset.

The raw dataset is derived from a private development history, so it carries:
  * a hardcoded third-party API key used as an env fallback
  * the operator's home directory name and a VPS/LAN IP
  * absolute filesystem paths that identify the machine

This script rewrites every task into a public form and then re-scans the result. It
refuses to write if any secret pattern survives.
"""
from __future__ import annotations

import hashlib
import json
import re
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve()
    .parents[3] / "src"))
from bugbench.models import Task, load_tasks, save_tasks  # noqa: E402

# --- secret patterns: these must NEVER survive into a public file ---------------
SECRETS = {
    "openai": r"\bsk-[A-Za-z0-9_-]{20,}",
    "anthropic": r"\bsk-ant-[A-Za-z0-9_-]{20,}",
    "litellm": r"\bsk-litellm-[A-Za-z0-9_-]{10,}",
    "cerebras": r"\bcsk-[A-Za-z0-9]{20,}",
    "cloudflare": r"\bcf[a-zA-Z0-9_-]{30,}\b",
    "github": r"\bgh[pousr]_[A-Za-z0-9]{30,}",
    "aws": r"\bAKIA[0-9A-Z]{16}\b",
    "slack": r"\bxox[baprs]-[A-Za-z0-9-]{10,}",
    "stripe": r"\b[sr]k_(live|test)_[A-Za-z0-9]{20,}",
    "google": r"\bAIza[0-9A-Za-z_-]{30,}",
    "jwt": r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}",
    "private_key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "bearer": r"(?i)(authorization:\s*bearer\s+)(\S{8,})",
    "creds_url": r"(?i)\b([a-z0-9+]+://[^\s:@/]+:)([^\s:@/]+)(@[^\s/]+)",
}
SECRET_RX = {k: re.compile(v) for k, v in SECRETS.items()}

# --- PII: identity, not credentials -------------------------------------------
HOME_RX = re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+")
PUBLIC_IP_RX = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}"
                          r"(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
LAN_RX = re.compile(r"\b10\.0\.0\.\d{1,3}\b")
M365_RX = re.compile(r"\b\d{7}@(?:ups|outlook|hotmail|gmail)\.com\b", re.I)
EMAIL_RX = re.compile(r"\b[A-Za-z0-9._%+-]+@(?!example\.(?:com|org))[A-Za-z0-9.-]"
                      r"+\.[A-Za-z]{2,}\b")
PHONE_RX = re.compile(r"(?<!\d)(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?!\d)")


def _hash(tag: str, value: str) -> str:
    """Deterministic, non-reversible pseudonym so the same secret maps to one token."""
    return f"<{tag}:{hashlib.sha256(value.encode()).hexdigest()[:8]}>"


def redact(text: str) -> tuple[str, int]:
    if not text:
        return text, 0
    n = 0

    def sub_secret(m, key):
        return m.group(1) + _hash(key.upper(), m.group(0)) if m.re.groups else _hash(key.upper(), m.group(0))

    for key, rx in SECRET_RX.items():
        if key in ("bearer", "creds_url"):
            text, k = rx.subn(lambda m: (m.group(1) + _hash(key.upper(), m.group(2))
                                         if m.re.groups else
                                         _hash(key.upper(), m.group(0))), text)
        else:
            text, k = rx.subn(lambda m, key=key: _hash(key.upper(), m.group(0)), text)
        n += k
    for rx, tag in ((HOME_RX, "HOME"), (PUBLIC_IP_RX, "IP"), (LAN_RX, "IP"),
                    (M365_RX, "EMAIL"), (EMAIL_RX, "EMAIL"), (PHONE_RX, "PHONE")):
        text, k = rx.subn(lambda m, tag=tag: _hash(tag, m.group(0)), text)
        n += k
    return text, n


def sanitize(task: Task) -> tuple[Task, int]:
    d = task.to_dict()
    total = 0
    for f in ("goal", "original_goal", "buggy", "reference_fix", "file_name",
              "project", "category_reason", "source_title"):
        if f in d and isinstance(d[f], str):
            d[f], k = redact(d[f])
            total += k
    d["source_session"] = ""
    d["file_name"] = d["file_name"].split("/")[-1]
    d["project"] = d["project"] or "unknown"
    return Task.from_dict(d), total


def _secrets_only(text: str) -> tuple[str, int]:
    if not text:
        return text, 0
    n = 0
    for key, rx in SECRET_RX.items():
        if key in ("bearer", "creds_url"):
            text, k = rx.subn(lambda m, key=key: (m.group(1) + _hash(key.upper(), m.group(2))
                                                 if m.re.groups else
                                                 _hash(key.upper(), m.group(0))), text)
        else:
            text, k = rx.subn(lambda m, key=key: _hash(key.upper(), m.group(0)), text)
        n += k
    return text, n


def _verify(path: str, strict: bool = True) -> int:
    bad = 0
    for t in load_tasks(path):
        blob = " ".join(str(getattr(t, f, "") or "") for f in
                        ("goal", "original_goal", "buggy", "reference_fix"))
        for key, rx in SECRET_RX.items():
            for m in rx.findall(blob):
                if isinstance(m, tuple):
                    m = "".join(m)
                print(f"  LEAK {t.task_id} {key}: {m[:60]}")
                bad += 1
        if strict:
            for rx, tag in ((HOME_RX, "HOME"), (M365_RX, "UPN"), (EMAIL_RX, "EMAIL"),
                            (PUBLIC_IP_RX, "IP")):
                for m in rx.findall(blob):
                    print(f"  PII  {t.task_id} {tag}: {m[:60]}")
                    bad += 1
    if bad:
        print(f"\nREFUSING TO PUBLISH: {bad} residual findings")
        return 1
    print("\nverified: no secret pattern survives in the written file")
    return 0


def main() -> int:
    tasks = load_tasks()
    if "--credentials-only" in sys.argv:
        # PRIVATE tier: replace live credentials only. Paths, IPs, session ids, project
        # labels and company context are all preserved -- this is the full-fidelity copy.
        # Credentials still go, because git history is immutable: a key that was ever
        # pushed stays in the objects forever, even after it is rotated.
        out2, n2 = [], 0
        for t in tasks:
            d = t.to_dict()
            for f in ("goal", "original_goal", "buggy", "reference_fix", "file_name",
                      "project", "category_reason", "source_title"):
                if isinstance(d.get(f), str):
                    d[f], k = _secrets_only(d[f])
                    n2 += k
            out2.append(Task.from_dict(d))
        p2 = "/home/ras/llm-bugbench/data/tasks.private.jsonl"
        save_tasks(out2, p2)
        print(f"wrote {len(out2)} private tasks -> {p2} ({n2} credential replacements)")
        return _verify(p2, strict=False)

    out, total = [], 0
    for t in tasks:
        st, k = sanitize(t)
        total += k
        out.append(st)

    path = "/home/ras/llm-bugbench/data/tasks.public.jsonl"
    save_tasks(out, path)
    print(f"wrote {len(out)} sanitized tasks -> {path}")
    print(f"replacements: {total}")
    return _verify(path, strict=True)


if __name__ == "__main__":
    raise SystemExit(main())
