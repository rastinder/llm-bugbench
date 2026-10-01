"""The model adapter: one call, one attempt, one graded result.

Deliberately the smallest thing that can work. The benchmark's job is to observe whether a
model can repair code, and every layer of agent scaffolding -- tool loops, retries, memory,
self-critique -- changes the thing being measured while adding failure modes that look like
model failure. So a run is: hand the model the source, ask for the fix, apply the diff it
returned, grade the result, and record what happened.

Two properties are load-bearing:

  * **The model never sees a test.** It gets ``code/`` only. Passing a test path, a test
    name, or a hint about which assertion fails would turn the measurement into a
    memorisation check, which is the one failure mode no downstream gate can detect.
  * **An unparseable answer is a failed attempt, not a crash.** A model that replies with
    prose instead of a diff has failed the task; it must score zero rather than abort the
    campaign.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

#: Cloudflare answers 403 "error code: 1010" to Python's default User-Agent, so a run
#: against the VPS proxy fails as a transport error and looks like a broken model. Verified:
#: the identical payload returns 200 the moment a UA is set. Pinned here rather than left to
#: the caller so every run presents the same identity.
DEFAULT_UA = "curl/8.5.0"

#: Refusals and non-answers. Recorded as their own outcome so that "declined to work" can
#: never be laundered into a capability score by scoring it as a fast zero.
REFUSAL_MARKERS = (
    "i can't help", "i cannot help", "i can't assist", "i cannot assist",
    "i'm unable to", "i am unable to", "as an ai", "i won't", "i will not",
    "sorry, but i", "i apologize",
)

#: An explicit "there is nothing to do". This is the sanctioned no-op reply in the system
#: prompt, so it has to be recognised verbatim as well as by phrase -- a model that says
#: exactly NO_CHANGES is declining the task, not failing to produce a diff, and the two must
#: stay distinguishable in the results.
NO_EDIT_MARKERS = (
    "no_changes", "no changes", "nothing to change", "already correct",
    "looks correct", "no bug", "cannot reproduce", "unable to reproduce",
)

#: Transport failures that are the infrastructure's fault, not the model's. Scoring a
#: Cloudflare 524 as a zero would penalise whichever model happened to be slowest, which is
#: the exact survivorship bias this benchmark is supposed to avoid. 524 is Cloudflare's
#: origin-timeout: measured at a deterministic 125 s on this cohort, and it disappears
#: entirely once the prompt is scoped to a size the model can answer comfortably.
RETRYABLE_TRANSPORT = ("http_524", "http_429", "http_502", "http_503", "http_504",
                       "transport:", "timed out")

MAX_PROMPT_CHARS = 24_000
"""Ceiling on the source handed to a model.

Not a politeness limit. Two hard reasons:

* A 70 KB module makes every request exceed the provider's 100 s origin timeout, so slow
  models are recorded as transport failures and scored zero for being slow rather than
  wrong -- a measurement error that favours fast models.
* Handing a model a whole large module measures reading comprehension, not debugging. The
  comparison stops being about repair ability.

When a file exceeds the ceiling, only the region around the mutation is sent, with the rest
marked elided, so the model still sees real surrounding context.
"""

SYSTEM = """You are fixing a real bug in a real codebase.

You will be shown a source file. Reply with a unified diff that repairs the defect.

Rules:
- Reply with ONLY the diff. No explanation, no commentary, no markdown fences needed.
- The diff must use `--- a/<path>` and `+++ b/<path>` headers.
- Change as little as possible. Do not refactor, reformat, or rename things.
- Do not add tests, comments, or docstrings.

If the code is already correct, reply with exactly: NO_CHANGES"""


@dataclass
class Attempt:
    """One model's answer to one task."""

    model: str
    task_id: str
    ok: bool
    outcome: str                    # scored | invalid_response | refused | declined_work | transport
    patch: str = ""
    reply: str = ""
    latency_s: float = 0.0
    error: str = ""
    detail: dict = field(default_factory=dict)

    def as_row(self) -> dict:
        """One line for the results file, carrying the manifest hash for verification."""
        return {
            "model": self.model,
            "task_id": self.task_id,
            "outcome": self.outcome,
            "ok": self.ok,
            "latency_s": round(self.latency_s, 3),
            "patch_chars": len(self.patch),
            "reply_chars": len(self.reply),
            "error": self.error,
            "manifest_hash": self.detail.get("manifest_hash", ""),
            **{k: v for k, v in self.detail.items() if k != "manifest_hash"},
        }


# ---------------------------------------------------------------------------
# patch extraction
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:diff|patch)?\s*\n(.*?)```", re.S)


def extract_patch(reply: str) -> str:
    """Pull a unified diff out of a model reply.

    Models wrap diffs in fences, prefix them with chatter, or emit them bare. Only a
    well-formed ``diff --git`` / ``--- a/`` + ``+++ b/`` pair counts, so a reply that
    merely discusses a change cannot be mistaken for one.
    """
    if not reply:
        return ""
    for candidate in [m.strip() for m in _FENCE.findall(reply)] + [reply]:
        if ("--- " in candidate and "+++ " in candidate) or candidate.startswith("diff "):
            lines = [ln for ln in candidate.splitlines()
                     if ln.startswith(("--- ", "+++ ", "@@", "+", "-", "diff ", "index "))]
            if len(lines) >= 3:
                return "\n".join(lines)
    return ""


def classify(reply: str, patch: str) -> tuple[str, bool]:
    """Map a reply to an outcome. Returns ``(outcome, scored?)``."""
    low = (reply or "").lower()
    if not reply.strip():
        return "invalid_response", False
    if any(m in low for m in REFUSAL_MARKERS):
        return "refused", False
    if not patch:
        if any(m in low for m in NO_EDIT_MARKERS):
            # Declining to edit is not the same as failing to try, and averaging the two
            # would hide a model that quietly refuses hard tasks.
            return "declined_work", False
        return "invalid_response", False
    return "scored", True


def is_retryable(error: str) -> bool:
    return any(marker in (error or "").lower() for marker in RETRYABLE_TRANSPORT)


# ---------------------------------------------------------------------------
# prompt construction
# ---------------------------------------------------------------------------

def scope_source(text: str, focus_line: int | None = None,
                 budget: int = MAX_PROMPT_CHARS) -> tuple[str, bool]:
    """Return ``(source, truncated)`` bounded by ``budget`` characters.

    With a known ``focus_line`` the window is centred on it, because for a mutant task that
    line is where the defect is. Without one, the head of the file is kept: for a real
    historical bug the interesting code is not randomly distributed.
    """
    if len(text) <= budget:
        return text, False

    if focus_line and focus_line > 0:
        lines = text.splitlines(keepends=True)
        idx = min(max(focus_line - 1, 0), len(lines) - 1)
        per_line = max(budget // 60, 1)
        half = max(per_line // 2, 1)
        start = max(0, idx - half)
        end = min(len(lines), start + per_line)
        start = max(0, end - per_line)
        window = "".join(lines[start:end])
        before = f"\n# ... [{idx - start} lines elided before] ...\n" if start else "\n"
        after = (f"\n# ... [{len(lines) - end} lines elided after] ...\n"
                 if end < len(lines) else "\n")
        return before + window + after, True

    return text[: budget - 40] + "\n# ... [truncated] ...\n", True


def build_prompt(code_files: dict[str, str], hint: str = "",
                 focus_lines: dict[str, int] | None = None) -> list[dict]:
    parts = ["Here is the file to fix.\n"]
    focus_lines = focus_lines or {}
    for path, text in sorted(code_files.items()):
        scoped, truncated = scope_source(text, focus_lines.get(path))
        if truncated:
            parts.append(
                f"({path} is large; only the relevant region is shown, marked with elisions.)"
            )
        parts.append(f"--- a/{path}\n+++ b/{path}\n{scoped}")
    if hint:
        parts.append(hint)
    parts.append("\nReply with the unified diff that fixes the bug.")
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": "\n".join(parts)}]


# ---------------------------------------------------------------------------
# the transport
# ---------------------------------------------------------------------------

def call_openai(base_url: str, model: str, messages: list[dict],
                api_key: str | None = None, timeout: int = 180,
                temperature: float = 0.0, max_tokens: int = 8192,
                user_agent: str = DEFAULT_UA) -> tuple[str, float, str]:
    """One chat completion. Returns ``(content, latency_s, error)``.

    Temperature is pinned to 0 and recorded per run: the noise-floor gate cannot attribute
    variance to the harness if the sampler is left to wander.

    ``max_tokens`` is measured, not guessed. Reasoning models emit thousands of reasoning
    tokens before answering, so too small a budget truncates the reply mid-thought and it
    arrives empty. But too large a budget *lengthens* the reasoning: on this cohort 2048
    tokens completed in 21 s, 8192 in 42 s, and 16384 pushed the request past Cloudflare's
    100 s origin timeout and returned HTTP 524 every time. 8192 is the largest budget that
    still returns, and it is the value frozen here so every model is measured identically.
    """
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": user_agent,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        content = body["choices"][0]["message"].get("content") or ""
        return content, time.monotonic() - start, ""
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        return "", time.monotonic() - start, f"http_{e.code}: {detail}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return "", time.monotonic() - start, f"transport: {e}"
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        return "", time.monotonic() - start, f"malformed_response: {e}"


def attempt_task(base_url: str, model: str, task: dict, code_files: dict[str, str],
                 code_root: Path, api_key: str | None = None,
                 manifest_hash: str = "", timeout: int = 240,
                 retries: int = 2, retry_delay: float = 3.0) -> Attempt:
    """Ask a model to fix one task and apply whatever diff it returned.

    The diff is applied to a scratch copy, never to the panel tree: the buggy state is the
    frozen reference and mutating it would corrupt every later run.

    The prompt is scoped around the task's own mutation line, so the model sees the defect
    in real surrounding context rather than a truncated file head. Transport failures are
    retried rather than scored, because a provider timeout says nothing about the model.
    """
    focus = {task["module"]: task["line"]} if task.get("line") else {}
    # The window shown is centred on the mutation, but reasoning models reliably edit
    # somewhere else in the file when they are not told where to look. Naming the region is
    # the same information the elision markers already give, and it is derived from the
    # task, never from the test.
    hint = ""
    if task.get("line"):
        hint = (f"The defect is in {task['module']} near line {task['line']}. "
                f"Concentrate your change there.")
    messages = build_prompt(code_files, hint=hint, focus_lines=focus)
    prompt_chars = len(json.dumps(messages))

    reply, latency, error = "", 0.0, ""
    transport_attempts = 0
    for attempt_no in range(retries + 1):
        reply, latency, error = call_openai(base_url, model, messages, api_key, timeout)
        transport_attempts += 1
        if not error or not is_retryable(error) or attempt_no == retries:
            break
        time.sleep(retry_delay * (attempt_no + 1))

    patch = extract_patch(reply)
    outcome, scored = classify(reply, patch)

    if error:
        outcome, scored = "transport", False
    elif scored:
        if not apply_patch_to_tree(code_root, patch):
            outcome, scored = "invalid_response", False
            error = "patch did not apply to the provided file"

    return Attempt(
        model=model, task_id=task["task_id"], ok=scored, outcome=outcome,
        patch=patch, reply=reply, latency_s=latency, error=error,
        detail={"manifest_hash": manifest_hash, "provider_model": model,
                "prompt_chars": prompt_chars,
                "transport_attempts": transport_attempts,
                "retryable": is_retryable(error)},
    )


# ---------------------------------------------------------------------------
# patch application
# ---------------------------------------------------------------------------

def apply_patch_to_tree(root: Path, patch: str) -> bool:
    """Apply a unified diff to a directory tree. Returns whether anything changed.

    Written as verify-then-apply rather than edit-while-scanning, because the obvious
    implementation corrupts files: if a context line is not found it still emits the ``+``
    lines, which silently replaces unrelated code with whatever the model proposed. That is
    the worst possible failure here -- a hallucinated edit would be graded as a real attempt
    and could score.

    So each hunk must locate its full context exactly, in order. If any hunk cannot, the
    file is left untouched and the attempt is reported as unappliable.
    """
    root = Path(root).resolve()
    hunks = _parse_hunks(patch)
    if not hunks:
        return False

    changed = False
    for rel, file_hunks in hunks.items():
        target = (root / rel).resolve()
        # A diff header naming ../.. must not be able to escape the tree.
        if not str(target).startswith(str(root) + "/"):
            continue
        if not target.is_file():
            continue

        original = target.read_text(encoding="utf-8", errors="replace")
        lines = original.splitlines(keepends=True)
        rebuilt: list[str] | None = []
        cursor = 0

        for hunk in file_hunks:
            at = _locate(lines, hunk, cursor)
            if at is None:
                rebuilt = None      # abort the whole file rather than guess
                break
            rebuilt.extend(lines[cursor:at])
            for sign, body in hunk:
                if sign == " ":
                    rebuilt.append(_with_newline(body, lines[at] if at < len(lines) else ""))
                elif sign == "+":
                    rebuilt.append(body + "\n")
                # '-' lines are consumed, not emitted
            cursor = min(at + sum(1 for sign, _ in hunk if sign in " -"), len(lines))

        if rebuilt is None:
            continue
        rebuilt.extend(lines[cursor:])
        new_text = "".join(rebuilt)
        if new_text != original:
            target.write_text(new_text, encoding="utf-8")
            changed = True
    return changed


def _with_newline(body: str, sample: str) -> str:
    """Keep the original line's trailing newline style for context lines."""
    if body.endswith("\n"):
        return body
    return body + ("\n" if sample.endswith("\n") or not sample else "\n")


def _parse_hunks(patch: str) -> dict[str, list[list[tuple[str, str]]]]:
    """Parse a unified diff into ``{path: [[(sign, body), ...], ...]}``.

    ``@@`` hunk headers are mandatory delimiters: without them there is no way to know
    where one hunk ends, and a model that omits them gets a clear rejection rather than a
    misapplied edit.
    """
    files: dict[str, list[list[tuple[str, str]]]] = {}
    current: str | None = None
    hunk: list[tuple[str, str]] | None = None

    for line in patch.splitlines():
        if line.startswith("--- "):
            if hunk and current:
                files[current].append(hunk)
            hunk = None
            target = line[4:].strip()
            current = target[2:] if target[:2] in ("a/", "b/") else target.lstrip("/")
            files.setdefault(current, [])
        elif line.startswith("+++ "):
            continue
        elif line.startswith("@@"):
            if hunk and current:
                files[current].append(hunk)
            hunk = []
        elif hunk is not None and line[:1] in (" ", "+", "-"):
            hunk.append((line[0], line[1:]))

    if hunk and current:
        files[current].append(hunk)
    return {k: v for k, v in files.items() if v}


def _locate(lines: list[str], hunk: list[tuple[str, str]], start: int) -> int | None:
    """Index of the first line at or after ``start`` where the hunk's context matches."""
    expected = [b for sign, b in hunk if sign in " -"]
    if not expected:
        return None
    for i in range(start, len(lines) - len(expected) + 1):
        window = [ln.rstrip("\n") for ln in lines[i:i + len(expected)]]
        if window == [e.rstrip("\n") for e in expected]:
            return i
    return None
