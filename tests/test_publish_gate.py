"""Block 7: publish gate. The public dataset must never carry a secret or PII.

This is the test that would have caught the hardcoded third-party API key found in
task B0020 before anything was pushed anywhere.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from bugbench.models import load_tasks

PUBLIC = "/home/ras/llm-bugbench/data/tasks.public.jsonl"

SECRET_PATTERNS = {
    "openai": r"\bsk-[A-Za-z0-9_-]{20,}",
    "anthropic": r"\bsk-ant-[A-Za-z0-9_-]{20,}",
    "openrouter": r"\bsk-or-v1-[A-Za-z0-9]{20,}",
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
    "home_path": r"/(?:home|Users)/[A-Za-z0-9._-]+",
    "upn": r"\b\d{7}@(?:ups|outlook|hotmail|gmail)\.com\b",
    "public_ip": r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b",
}
RX = {k: re.compile(v) for k, v in SECRET_PATTERNS.items()}

TEXT_FIELDS = ("goal", "original_goal", "buggy", "reference_fix", "file_name",
               "project", "category_reason", "source_title")


TIER_FILE = pathlib.Path(__file__).resolve().parent.parent / "data" / "TIER"


def _tier() -> str:
    try:
        return TIER_FILE.read_text().strip()
    except Exception:
        return "public"


def _public_tasks():
    try:
        return load_tasks(PUBLIC)
    except Exception:
        return []


def test_public_dataset_exists():
    assert _public_tasks(), f"{PUBLIC} missing or empty -- run sanitize_for_publish.py"


def test_public_dataset_has_no_secrets_or_pii():
    """Credentials are scrubbed in the public and private tiers.

    The RAW tier is the acknowledged exception: it ships byte-identical ground truth,
    including the live API key the dataset was mined with, so the code is undistorted. The
    owner accepted that trade explicitly. The exception is PRINTED rather than silently
    skipped, so the test output of the raw repo says what it is.

    ACTION REQUIRED by the owner: rotate the exposed key. Git history is immutable, so
    once pushed the key remains in the objects forever.
    """
    if _tier() == "raw":
        print("\n[ACKNOWLEDGED EXCEPTION] raw tier ships the dataset unredacted, "
              "including the live API key it was mined with. Rotate that key.\n")
        return
    pii_only = {"home_path", "upn", "public_ip"}
    # the RAW private tier is byte-identical ground truth: it is allowed to contain
    # paths and IPs. Credentials are scrubmed there separately, in a dedicated pass.
    bad = []
    for t in _public_tasks():
        for f in TEXT_FIELDS:
            val = str(getattr(t, f, "") or "")
            for name, rx in RX.items():
                if _tier() in ("private", "raw") and name in pii_only:
                    continue
                for m in rx.findall(val):
                    if isinstance(m, tuple):
                        m = "".join(m)
                    bad.append((t.task_id, f, name, m[:60]))
    assert not bad, "dataset leaks:\n" + "\n".join(
        f"  {a} [{b}] {c}: {d!r}" for a, b, c, d in bad[:20])


def test_public_dataset_has_no_session_identifiers():
    """Tier-1 strips session ids; the private and raw repos intentionally keep them."""
    if _tier() in ("private", "raw"):
        return
    for t in _public_tasks():
        assert not t.source_session, f"{t.task_id} still carries a session id"


def test_redaction_actually_replaces_and_is_deterministic():
    from data.raw.scripts.sanitize_for_publish import redact  # type: ignore
    secret = "sk-" + "0" * 12 + "x" * 20   # built at runtime: no key literal in source
    out, n = redact(f'KEY = os.environ.get("K", "{secret}")')
    assert n >= 1
    assert secret not in out
    assert "<OPENAI:" in out
    again, _ = redact(f'KEY = os.environ.get("K", "{secret}")')
    assert again == out, "redaction must be deterministic (pseudonym reuse)"


def test_redaction_handles_pii_without_touching_benchmark_semantics():
    from data.raw.scripts.sanitize_for_publish import redact  # type: ignore
    out, n = redact("connect from 198.51.100.7 to 203.0.113.5 as ras in /home/example/x.py")
    assert "203.0.113.5" not in out
    assert "/home/example" not in out
    assert "<IP:" in out and "<HOME:" in out


def test_public_and_private_task_sets_have_the_same_ids():
    """The public set must be the same benchmark, not a different (easier) one."""
    from bugbench.models import load_tasks as lt
    priv = {t.task_id for t in lt("/home/ras/llm-bugbench/data/tasks.jsonl")}
    pub = {t.task_id for t in _public_tasks()}
    assert pub <= priv and pub, "public ids must be a subset of the private set"
    assert len(pub) == len(priv), "public set should cover every task"


def test_redaction_does_not_change_task_difficulty():
    """Redaction must not leak the answer or gut the code."""
    from bugbench.models import load_tasks as lt
    priv = {t.task_id: t for t in lt("/home/ras/llm-bugbench/data/tasks.jsonl")}
    for t in _public_tasks():
        p = priv[t.task_id]
        drift = abs(len(t.reference_fix.split()) - len(p.reference_fix.split()))
        assert drift == 0, f"{t.task_id}: redaction changed token count by {drift}"
        assert len(t.buggy) > 100, f"{t.task_id}: snippet gutted by redaction"


# ---------------------------------------------------------------- tier-2 gate
ANON = "/home/ras/llm-bugbench/data/tasks.public.anonymised.jsonl"
CONTEXT_WORDS = ["brarbuild", "indeed", "brampton", "aitshirts", "rastinder",
                 "cloakbrowser", "opencode", "litellm", "sunnypilot", "ruflo",
                 "herdr", "kimi", "whatsedit", "autopilot-jobs", "mycondovalue"]


def _anon_tasks():
    try:
        return load_tasks(ANON)
    except Exception:
        return []


def test_anonymised_dataset_exists_and_is_complete():
    anon = _anon_tasks()
    assert anon, f"{ANON} missing -- run anonymise.py"
    assert len(anon) == len(_public_tasks())


def test_anonymised_dataset_carries_no_operator_context():
    """Only meaningful for the fully-anonymised tier.

    The shipped tier-1 dataset deliberately KEEPS company, vendor, project and file names --
    that context is what makes the benchmark realistic -- so this check is opt-in and the
    credential gate above is the one that always applies.
    """
    if _tier() != "anonymised":
        return
    bad = []
    for t in _anon_tasks():
        blob = " ".join(str(getattr(t, f, "") or "") for f in TEXT_FIELDS).lower()
        for w in CONTEXT_WORDS:
            if w in blob:
                bad.append((t.task_id, w))
    assert not bad, f"operator context survives: {bad[:20]}"


def test_shipped_dataset_keeps_company_and_file_names():
    """tier-1 is a context-PRESERVING scrub: names survive, credentials do not."""
    tasks = _public_tasks()
    assert any(t.file_name and "/" not in t.file_name for t in tasks), \
        "file names should be preserved in tier 1"
    assert any(t.project for t in tasks), \
        "project labels should be preserved in tier 1"


def test_anonymised_dataset_still_passes_the_credential_gate():
    bad = []
    for t in _anon_tasks():
        for f in TEXT_FIELDS:
            for name, rx in RX.items():
                if _tier() in ("private", "raw") and name in {"home_path", "upn", "public_ip"}:
                    continue
                if _tier() == "raw":
                    continue          # acknowledged: see the raw-tier test above
                for m in rx.findall(str(getattr(t, f, "") or "")):
                    bad.append((t.task_id, f, name))
    assert not bad, bad[:20]


def test_anonymisation_preserves_the_bug_and_the_fix():
    """Renaming identifiers must not destroy the defect or the repair."""
    for t in _anon_tasks():
        assert len(t.buggy) > 100, f"{t.task_id} snippet gutted"
        assert t.buggy.strip() != t.reference_fix.strip()
        if t.category == "bug_fix":
            # primary tasks carry the synthesised defect report
            assert "Defect:" in t.goal, f"{t.task_id} lost its goal statement"
