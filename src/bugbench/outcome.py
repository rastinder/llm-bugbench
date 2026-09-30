"""Outcome taxonomy: a row is either SCORED or it is not.

The 432-row results file was 78% infrastructure errors, and every one of those rows was
carried into the leaderboard as a legitimate 0% score. That made the board a measure of
LiteLLM/OpenRouter uptime rather than of the models. A row now resolves to exactly one
state, and only `completed` is ever given a number.

Precedence is fixed and total. The first matching rule wins, highest first:

  1. tainted            an answer cannot be trusted at all (file-read leak / verbatim
                        reproduction). Outranks everything, including a timeout -- a
                        leaked answer is untrustworthy whether or not the transport
                        also failed.
  2. policy_blocked     our own guard refused to run the lane (e.g. the agentic lane
                        without filesystem isolation). Not a model result.
  3. infra_error        the provider/transport failed. Split by whether retrying can
                        possibly help -- see `Retryable`.
  4. invalid_response   transport succeeded but the answer is unusable (empty, a
                        refusal, no code). The model did answer; it answered nothing.
  5. completed          scored.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: the only state that receives a numeric score
SCORED_STATES = ("completed",)

#: states that mean "this was never a model result"
UNSCORED_STATES = ("tainted", "policy_blocked", "infra_error", "invalid_response")


@dataclass(frozen=True)
class Retryable:
    """Classification of a transport failure."""

    retryable: bool
    kind: str          # "rate_limited" | "server" | "timeout" | "bad_request" | "auth" | "not_found"
    detail: str = ""

    @property
    def status(self) -> str:
        return "infra_error"


#: patterns that mean "the endpoint is alive but busy" -- retrying later can work
_RETRY = (
    (re.compile(r"HTTP\s*429"), "rate_limited"),
    (re.compile(r"HTTP\s*5\d\d"), "server"),
    (re.compile(r"HTTP\s*408"), "timeout"),
    (re.compile(r"timed? ?out", re.I), "timeout"),
    (re.compile(r"TimeoutError", re.I), "timeout"),
    (re.compile(r"timeout after \d+s", re.I), "timeout"),
    (re.compile(r"connection (reset|aborted|refused)", re.I), "server"),
    (re.compile(r"temporarily unavailable|try again in", re.I), "rate_limited"),
)

#: patterns that mean "this endpoint/model is wrong" -- retrying is pure waste
_NO_RETRY = (
    (re.compile(r"HTTP\s*400"), "bad_request"),
    (re.compile(r"HTTP\s*401"), "auth"),
    (re.compile(r"HTTP\s*403"), "auth"),
    (re.compile(r"HTTP\s*404"), "not_found"),
    (re.compile(r"invalid model (selection|name)", re.I), "bad_request"),
    (re.compile(r"model not found", re.I), "not_found"),
    (re.compile(r"NotFoundError", re.I), "not_found"),
)

_POLICY_BLOCKED = re.compile(
    r"no filesystem isolation|lane disabled|isolation|policy_blocked", re.I)


def classify_error(err: str) -> Retryable | None:
    """Classify a transport error string. Returns None when it is not an error."""
    e = (err or "").strip()
    if not e:
        return None
    for rx, kind in _NO_RETRY:
        if rx.search(e):
            return Retryable(False, kind, e[:200])
    for rx, kind in _RETRY:
        if rx.search(e):
            return Retryable(True, kind, e[:200])
    # an unrecognised error is NOT assumed retryable: burning attempts on it wastes quota
    return Retryable(False, "unknown", e[:200])


def _has_code(text: str) -> bool:
    return bool(re.search(r"[A-Za-z_]\w*\s*[({=\[]", text or ""))


def outcome_state(row: dict) -> str:
    """Resolve a result row to exactly one outcome state."""
    # 1. taint -- always wins
    t = row.get("tainted") or row.get("taint_reason")
    if t:
        return "tainted"

    err = " ".join(str(row.get(k) or "") for k in
                   ("diagnose_error", "repair_error", "repair_given_error"))

    # 2. our own guard refused to run the lane
    if _POLICY_BLOCKED.search(err):
        return "policy_blocked"

    # 3. transport/provider failure
    if classify_error(err) is not None:
        return "infra_error"

    # 4. transport fine, answer unusable
    if row.get("empty_reply"):
        return "invalid_response"
    rep = row.get("repair") or {}
    cand = rep.get("candidate")
    if "candidate" in rep and not (cand or "").strip():
        return "invalid_response"
    if "candidate" in rep and not _has_code(cand):
        return "invalid_response"

    return "completed"


def is_scored(row: dict) -> bool:
    return outcome_state(row) in SCORED_STATES


def retryable_rows(rows: list[dict]) -> list[dict]:
    """Rows worth re-attempting: infra failures that are NOT policy blocks or taint."""
    out = []
    for r in rows:
        if outcome_state(r) != "infra_error":
            continue
        err = " ".join(str(r.get(k) or "") for k in
                       ("diagnose_error", "repair_error", "repair_given_error"))
        c = classify_error(err)
        if c is not None and c.retryable:
            out.append(r)
    return out
