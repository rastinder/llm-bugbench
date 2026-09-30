#!/usr/bin/env python3
"""Pre-push secret sweep over exactly the files git is about to publish.

The unit tests check the DATASET. This checks the WHOLE REPO -- source files and test
fixtures too -- because a key can also leak through a hardcoded constant in a script or a
"harmless" example in a test.

Covers tracked AND untracked non-ignored files: only reading tracked files is a hole,
because a freshly copied secret that has not been `git add`ed yet would sail through.

Usage:  python3 scripts/prepush_sweep.py [repo_root]
Exit 0 = clean, 1 = blocked.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

PATTERNS = {
    "openai_key": r"sk-[A-Za-z0-9_-]{20,}",
    "anthropic_key": r"sk-ant-[A-Za-z0-9_-]{20,}",
    "openrouter_key": r"sk-or-v1-[A-Za-z0-9]{20,}",
    "litellm_key": r"sk-litellm-[A-Za-z0-9_-]{10,}",
    "cerebras_key": r"csk-[A-Za-z0-9]{20,}",
    "cloudflare_token": r"\bcf[a-zA-Z0-9_-]{30,}\b",
    "github_token": r"\bgh[pousr]_[A-Za-z0-9]{30,}",
    "aws_key": r"\bAKIA[0-9A-Z]{16}\b",
    "slack_token": r"\bxox[baprs]-[A-Za-z0-9-]{10,}",
    "stripe_key": r"\b[sr]k_(live|test)_[A-Za-z0-9]{20,}",
    "google_key": r"\bAIza[0-9A-Za-z_-]{30,}",
    "jwt": r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}",
    "private_key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "credentialed_url": r"https?://[^/\s:@]+:[^/\s@]+@",
    "vps_ip": r"\b152\.67\.24\.166\b",
    # built at runtime so this file does not trip its own sweep
    "operator_home": r"/home/" + "ra" + r"s\b",
    "tenant_upn": r"\b\d{7}@(?:ups|outlook|hotmail|gmail)\.com\b",
}

# Only a violation in the PUBLIC tier. The private/raw tiers keep real paths and IPs on
# purpose. Credentials are enforced in every tier except the acknowledged raw tier.
PII_ONLY = {"operator_home", "tenant_upn", "vps_ip"}

SKIP_SUFFIX = {".pyc", ".png", ".jpg", ".pdf", ".zip", ".so"}


def tier(root: pathlib.Path) -> str:
    p = root / "data" / "TIER"
    return p.read_text().strip() if p.exists() else "public"


def active_patterns(root: pathlib.Path) -> dict[str, re.Pattern]:
    items = PATTERNS.items()
    if tier(root) in ("private", "raw"):
        items = ((k, v) for k, v in items if k not in PII_ONLY)
    if tier(root) == "raw":
        # acknowledged exception, recorded in tests/test_publish_gate.py
        return {k: re.compile(v) for k, v in items
                if k not in PII_ONLY and k not in {
                    "openai_key", "anthropic_key", "openrouter_key", "litellm_key",
                    "cerebras_key", "cloudflare_token", "github_token", "aws_key",
                    "slack_token", "stripe_key", "google_key", "jwt", "private_key",
                    "credentialed_url"}}
    return {k: re.compile(v) for k, v in items}


def files_to_sweep(root: pathlib.Path) -> list[pathlib.Path]:
    def run(args):
        return subprocess.run(args, cwd=root, capture_output=True,
                              text=True).stdout.split()

    out = [root / f for f in run(["git", "ls-files"])]
    out += [root / f for f in run(["git", "ls-files", "--others", "--exclude-standard"])]
    if not out:
        out = [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts]
    seen, keep = set(), []
    for f in out:
        r = f.resolve()
        if r not in seen:
            seen.add(r)
            keep.append(f)
    return [f for f in keep if f.exists() and f.suffix not in SKIP_SUFFIX]


def main() -> int:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    pats = active_patterns(root)
    files = files_to_sweep(root)
    findings = []
    for f in files:
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        for name, rx in pats.items():
            for m in rx.findall(text):
                if isinstance(m, tuple):
                    m = "".join(m)
                line = text[:text.find(m)].count("\n") + 1
                findings.append((str(f.relative_to(root)), line, name, m[:48]))
    print(f"tier={tier(root)} · swept {len(files)} files for {len(pats)} patterns")
    for path, line, name, val in findings:
        print(f"  LEAK {path}:{line} [{name}] {val!r}")
    if findings:
        print(f"\nBLOCKED: {len(findings)} findings. Refusing to publish.")
        return 1
    print("CLEAN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
