"""Tests for the model adapter.

Two of these matter more than the rest. The no-leak test is the anti-cheating guarantee: if
a model can see a test, every number this benchmark produces is a measurement of
memorisation. The outcome tests exist so that declining to work cannot be laundered into a
capability score.
"""
from __future__ import annotations

import json

import pytest

from bugbench.attempt import (
    Attempt, apply_patch_to_tree, attempt_task, build_prompt, call_openai,
    classify, extract_patch,
)


DIFF = """--- a/mod.py
+++ b/mod.py
@@ -1,3 +1,3 @@
 def f(x):
-    return x * 0.6
+    return x
"""


class TestPatchExtraction:
    def test_extracts_a_bare_diff(self):
        assert "return x" in extract_patch(DIFF)

    def test_extracts_from_a_fenced_diff(self):
        reply = f"Here is the fix:\n\n```diff\n{DIFF}```\n\nHope that helps."
        assert "return x" in extract_patch(reply)

    def test_prose_about_a_fix_is_not_a_patch(self):
        """A reply that only *describes* a change must not be applied, or the model would
        score on the strength of its explanation."""
        reply = "You should change the multiplier from 0.6 to 1.0 in the return line."
        assert extract_patch(reply) == ""

    def test_empty_reply_yields_no_patch(self):
        assert extract_patch("") == ""
        assert extract_patch(None) == ""


class TestClassification:
    def test_a_patch_is_scored(self):
        outcome, scored = classify(DIFF, extract_patch(DIFF))
        assert outcome == "scored" and scored

    def test_refusal_is_its_own_outcome(self):
        outcome, scored = classify("I'm sorry, but I can't help with that.", "")
        assert outcome == "refused" and not scored

    def test_declining_to_edit_is_distinct_from_failing(self):
        """Both are zero, but averaging them hides a model that quietly refuses hard
        tasks -- the survivorship bias that makes refusing look like success."""
        outcome, scored = classify("NO_CHANGES", "")
        assert outcome == "declined_work" and not scored

    def test_prose_with_no_diff_is_invalid(self):
        outcome, scored = classify("I think the bug is on line 3.", "")
        assert outcome == "invalid_response" and not scored

    def test_empty_reply_is_invalid(self):
        assert classify("", "")[0] == "invalid_response"


class TestPrompt:
    def test_prompt_carries_the_source_file(self):
        msgs = build_prompt({"mod.py": "def f():\n    pass\n"})
        assert "def f():" in msgs[1]["content"]

    def test_prompt_never_mentions_a_test(self):
        """The anti-cheating guarantee, asserted on the prompt itself."""
        msgs = build_prompt({"mod.py": "x = 1\n"}, hint="")
        blob = json.dumps(msgs).lower()
        for word in ("test_", "pytest", "assert ", "test file"):
            assert word not in blob, f"prompt leaks {word!r}"


class TestApplyPatch:
    def test_applies_a_simple_diff(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("def f(x):\n    return x * 0.6\n")
        assert apply_patch_to_tree(tmp_path, DIFF)
        assert "return x\n" in f.read_text()
        assert "0.6" not in f.read_text()

    def test_no_change_when_nothing_matches(self, tmp_path):
        f = tmp_path / "mod.py"
        original = "def g():\n    return 1\n"
        f.write_text(original)
        assert apply_patch_to_tree(tmp_path, DIFF) is False
        assert f.read_text() == original

    def test_refuses_to_escape_the_tree(self, tmp_path):
        """A diff header naming ../.. must not be able to write outside the sandbox."""
        evil = "--- a/../../etc/pwn\n+++ b/../../etc/pwn\n@@ -1 +1 @@\n-a\n+b\n"
        (tmp_path / "mod.py").write_text("x=1\n")
        apply_patch_to_tree(tmp_path, evil)
        assert not (tmp_path.parent / "etc" / "pwn").exists()

    def test_unknown_target_is_ignored(self, tmp_path):
        (tmp_path / "mod.py").write_text("x=1\n")
        patch = "--- a/ghost.py\n+++ b/ghost.py\n@@ -1 +1 @@\n-a\n+b\n"
        apply_patch_to_tree(tmp_path, patch)
        assert not (tmp_path / "ghost.py").exists()


class TestTransport:
    def test_http_error_is_reported_not_raised(self, monkeypatch):
        import urllib.error
        import urllib.request

        def boom(*a, **k):
            raise urllib.error.HTTPError("u", 429, "slow down", {}, None)
        monkeypatch.setattr(urllib.request, "urlopen", boom)

        reply, _lat, err = call_openai("http://x/v1", "m", [])
        assert reply == "" and "429" in err

    def test_connection_failure_is_reported(self, monkeypatch):
        import urllib.request

        def boom(*a, **k):
            raise urllib.error.URLError("refused")
        monkeypatch.setattr(urllib.request, "urlopen", boom)

        reply, _lat, err = call_openai("http://x/v1", "m", [])
        assert reply == "" and "transport" in err

    def test_malformed_body_is_reported(self, monkeypatch):
        import io
        import urllib.request

        class R:
            def read(self): return b'{"choices": []}'
            def __enter__(self): return self
            def __exit__(self, *a): return False
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: R())

        reply, _lat, err = call_openai("http://x/v1", "m", [])
        assert reply == "" and err

    def test_temperature_is_pinned(self):
        """The noise-floor gate cannot attribute variance to the harness if the sampler
        wanders, so this is part of the frozen conditions, not a default."""
        captured = {}
        import urllib.request

        class R:
            def read(self):
                return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake(req, **k):
            captured["body"] = json.loads(req.data)
            return R()

        # Captured BEFORE patching and restored from that binding. Restoring from
        # `urllib.request.urlopen` inside the finally block reads the *already patched*
        # attribute -- ur is urllib.request -- so the fake leaked and broke every later
        # test in the suite that makes a real HTTP call.
        import urllib.request as ur
        original = ur.urlopen
        ur.urlopen = fake
        try:
            call_openai("http://x/v1", "m", [{"role": "user", "content": "hi"}])
        finally:
            ur.urlopen = original
        assert captured["body"]["temperature"] == 0.0


class TestAttemptFlow:
    def test_transport_failure_is_not_scored(self, monkeypatch):
        import bugbench.attempt as r
        monkeypatch.setattr(r, "call_openai", lambda *a, **k: ("", 0.1, "transport: down"))

        att = r.attempt_task("http://x/v1", "m", {"task_id": "t"},
                             {"mod.py": "x=1\n"}, None)
        assert att.outcome == "transport" and not att.ok

    def test_refusal_carries_no_score(self, monkeypatch):
        import bugbench.attempt as r
        monkeypatch.setattr(r, "call_openai",
                            lambda *a, **k: ("I can't help with that.", 0.1, ""))

        att = r.attempt_task("http://x/v1", "m", {"task_id": "t"},
                             {"mod.py": "x=1\n"}, None)
        assert att.outcome == "refused" and not att.ok

    def test_row_carries_the_manifest_hash(self):
        """Every scored row must be traceable to the frozen panel, or the analysis can
        silently mix conditions."""
        att = Attempt("m", "t", True, "scored", detail={"manifest_hash": "abc"})
        assert att.as_row()["manifest_hash"] == "abc"


class TestUserAgent:
    def test_default_ua_is_sent(self, monkeypatch):
        """Cloudflare 403s Python's default UA, which turns a working model into an
        apparent transport failure."""
        import json as _json
        import urllib.request as ur

        captured = {}

        class R:
            def read(self):
                return _json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake(req, **k):
            captured["headers"] = dict(req.headers)
            return R()

        real = ur.urlopen
        ur.urlopen = fake
        try:
            call_openai("http://x/v1", "m", [{"role": "user", "content": "hi"}])
        finally:
            ur.urlopen = real
        ua = captured["headers"].get("User-agent") or captured["headers"].get("User-Agent")
        assert ua and "Python-urllib" not in ua


class TestScopeSource:
    def test_small_file_is_untouched(self):
        from bugbench.attempt import scope_source
        text = "x = 1\n" * 10
        assert scope_source(text) == (text, False)

    def test_large_file_is_truncated(self):
        from bugbench.attempt import scope_source
        text = "x = 1\n" * 20_000
        out, trunc = scope_source(text, budget=5_000)
        assert trunc and len(out) < 6_000

    def test_focus_line_is_centred(self):
        """For a mutant task the defect is at a known line, so the window must include it."""
        from bugbench.attempt import scope_source
        lines = [f"line{i} = {i}" for i in range(5_000)]
        lines[3_000] = "DEFECT_HERE = True"
        out, _ = scope_source("\n".join(lines), focus_line=3_001, budget=4_000)
        assert "DEFECT_HERE" in out

    def test_truncation_is_marked_not_silent(self):
        """A model must be able to tell the file was cut, or it will reason about code it
        cannot see and invent context."""
        from bugbench.attempt import scope_source
        out, _ = scope_source("x = 1\n" * 20_000, budget=3_000)
        assert "elided" in out or "truncated" in out

    def test_prompt_stays_within_budget(self):
        from bugbench.attempt import MAX_PROMPT_CHARS, build_prompt
        msgs = build_prompt({"big.py": "x = 1\n" * 40_000})
        assert len(msgs[1]["content"]) <= MAX_PROMPT_CHARS + 2_000


class TestRetryableTransport:
    def test_provider_timeout_is_retryable_not_a_model_failure(self):
        """Scoring a Cloudflare 524 as zero would penalise whichever model is slowest --
        the survivorship bias this benchmark exists to avoid."""
        from bugbench.attempt import is_retryable
        for e in ("http_524: origin timeout", "http_429", "transport: refused"):
            assert is_retryable(e), e

    def test_a_real_model_error_is_not_retried(self):
        from bugbench.attempt import is_retryable
        for e in ("http_400: bad request", "http_401: unauthorized", "malformed_response"):
            assert not is_retryable(e), e

    def test_transport_failure_is_retried_then_reported(self, monkeypatch, tmp_path):
        import bugbench.attempt as r
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            return ("", 0.1, "http_524: origin timeout") if calls["n"] < 3 else ("ok", 0.1, "")
        monkeypatch.setattr(r, "call_openai", flaky)
        monkeypatch.setattr(r.time, "sleep", lambda *_: None, raising=False)

        att = r.attempt_task("http://x/v1", "m", {"task_id": "t", "module": "m.py"},
                             {"m.py": "x=1\n"}, tmp_path, retries=2)
        assert calls["n"] == 3, "should retry a retryable failure"
        assert att.detail["transport_attempts"] == 3


class TestPatchExtractionRealShapes:
    """Shapes actually produced by the cohort, not shapes imagined for it."""

    FENCED = (
        " --- a/check_model_menu.py\n+++ b/check_model_menu.py\n"
        "@@ -123,7 +123,7 @@\n-        return 0\n+        return None\n***\n"
    )

    def test_bare_fence_block_is_extracted(self):
        """Observed: the local model wraps its diff in ``` and appends ***."""
        from bugbench.attempt import extract_patch
        reply = "```diff\n" + self.FENCED + "```\n"
        assert "return None" in extract_patch(reply)

    def test_unterminated_fence_is_still_extracted(self):
        from bugbench.attempt import extract_patch
        reply = "```\n" + self.FENCED
        assert "return None" in extract_patch(reply)

    def test_trailing_marker_is_stripped(self):
        """A stray *** would be read as a context line and misalign later hunks."""
        from bugbench.attempt import extract_patch
        out = extract_patch(self.FENCED)
        assert "***" not in out.splitlines()

    def test_multiple_hunks_survive_extraction(self):
        from bugbench.attempt import extract_patch
        two = self.FENCED + "@@ -152,7 +152,7 @@\n-        a = 1\n+        a = 2\n"
        out = extract_patch(two)
        # Count hunk-header LINES, not the "@@" substring: "@@ -1,2 +1,2 @@" contains
        # two occurrences, so a substring count reports 4 for two hunks.
        assert len([ln for ln in out.splitlines() if ln.startswith("@@")]) == 2, out

    def test_hunks_without_a_file_header_are_rejected(self):
        """Regression: hunks alone were once accepted and classified `scored`, then
        silently applied to nothing -- a model credited with an edit it never made."""
        from bugbench.attempt import extract_patch
        assert extract_patch("@@ -1,2 +1,2 @@\n-a\n+b\n") == ""

    def test_mismatched_header_paths_are_rejected(self):
        from bugbench.attempt import extract_patch
        assert extract_patch("--- a/one.py\n+++ b/two.py\n@@ -1 +1 @@\n-a\n+b\n") == ""


class TestToolCallReplies:
    """Observed from the local 9B: it replied with tool-call XML instead of a diff."""

    REPLY = ("<tool_call><function=bash><parameter=command>sed -n 75,100p bench_stt.py"
             " 2>/dev/null || echo missing</parameter></function></tool_call>")

    def test_tool_call_reply_is_recognised(self):
        from bugbench.attempt import wants_tools
        assert wants_tools(self.REPLY)

    def test_a_plain_diff_is_not_mistaken_for_a_tool_call(self):
        from bugbench.attempt import wants_tools
        assert not wants_tools("--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-a\n+b\n")

    def test_tool_call_gets_its_own_outcome(self):
        """Its own outcome, not a generic non-answer: it tells us the model needs a tool
        interface, which is actionable about the model rather than about the harness."""
        from bugbench.attempt import classify, extract_patch
        outcome, scored = classify(self.REPLY, extract_patch(self.REPLY))
        assert outcome == "needs_tools" and not scored

    def test_no_patch_is_scraped_out_of_a_tool_call(self):
        """Scraping diff-like lines from tool XML yields a headerless patch that applies to
        nothing and would be recorded as an attempt."""
        from bugbench.attempt import extract_patch
        assert extract_patch(self.REPLY) == ""
