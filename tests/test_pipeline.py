"""Block 3/5/6: judge validity controls, end-to-end run, leaderboard, app."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from bugbench.models import load_tasks
from bugbench.grade import score_repair
from bugbench.oracle import build_oracle
from bugbench.report import bootstrap_ci, leaderboard, report, sub_signals
from bugbench.runner import Judge, append_results, load_results, run_model
from bugbench.runners import ModelSpec, OpenAIChatRunner, registry

from conftest import (
    BUGGY_ARITH, BUGGY_REGEX, BUGGY_RENAME, FIXED_ARITH, FIXED_REGEX,
    FIXED_RENAME, make_task, write_task_file,
)

REGISTRY_KEY = "sk-test-key"


# ---------------------------------------------------------------- judge
class _StubJudgeRunner:
    """Deterministic stand-in for the judge: fixed if the candidate text differs from
    the buggy snippet and contains the reference's distinctive line."""

    def __init__(self):
        self.calls = []

    def complete_messages(self, messages):
        user = messages[-1]["content"]
        self.calls.append(user)
        if "SECRET" in user:
            pytest.fail("judge prompt leaked the reference fix as SECRET")
        cand = user.split("CANDIDATE FIX:")[1] if "CANDIDATE FIX:" in user else user
        fixed = ("_negated_before" in cand or "if not values" in cand
                 or "for m in BANNED.finditer" in cand)
        return type("R", (), {"text": json.dumps({"fixed": bool(fixed),
                                                  "reason": "stub", "confidence": 1.0}),
                              "ok": True, "error": "", "latency_ms": 1,
                              "prompt_tokens": 1, "completion_tokens": 1})()


def test_judge_accepts_the_reference_fix_and_rejects_the_buggy_snippet():
    """Gold/negative control: a vacuous judge would fail this test."""
    task = make_task(None)
    j = Judge(runner=_StubJudgeRunner())
    assert j.score(task, task.reference_fix) >= 0.99
    assert j.score(task, task.buggy) == 0.0


def test_judge_is_blind_its_prompt_has_no_model_identity():
    task = make_task(None)
    from bugbench.prompts import build_judge_prompt
    p = build_judge_prompt(task, task.reference_fix)
    for token in ("litellm", "codestral", "llama", "mimo", "copilot", "qwen"):
        assert token not in p.lower()


def test_judge_reference_aware_prompt_is_a_separate_opt_in():
    task = make_task(None)
    from bugbench.prompts import build_judge_prompt
    blind = build_judge_prompt(task, "x", task.reference_fix, reference_aware=False)
    aware = build_judge_prompt(task, "x", task.reference_fix, reference_aware=True)
    assert "HISTORICAL FIX" not in blind
    assert "HISTORICAL FIX" in aware


def test_judge_caches_by_content():
    task = make_task(None, task_id="B-CACHE-UNIQUE")
    stub = _StubJudgeRunner()
    j = Judge(runner=stub)
    j.score(task, task.reference_fix)
    j.score(task, task.reference_fix)
    assert len(stub.calls) == 1


# ---------------------------------------------------------------- oracle on real fixtures
def test_oracle_distinguishes_regex_first_match_bug():
    o = build_oracle({"language": "python", "buggy": BUGGY_REGEX,
                      "reference_fix": FIXED_REGEX, "goal": "is_safe misses later claims",
                      "file_name": "safety.py"})
    if o.kind == "exec_diff":
        assert o.score(FIXED_REGEX) == 1.0
        assert o.score(BUGGY_REGEX) == 0.0
    else:                      # probes could not discriminate -> correctly rejected
        assert o.reason


def test_oracle_gate_is_a_no_op_for_rename_only_pair():
    o = build_oracle({"language": "python", "buggy": BUGGY_RENAME,
                      "reference_fix": FIXED_RENAME, "goal": "rename",
                      "file_name": "p.py"})
    assert o.kind == "none"


# ---------------------------------------------------------------- end-to-end
class _FakeModelHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        text = json.dumps(body)
        if "Return the corrected snippet" in text or "CURRENT CODE" in text:
            content = ("```python\n" + FIXED_ARITH + "\n```\nRoot cause: empty guard")
        else:
            content = json.dumps({
                "root_cause": "division by zero on empty input",
                "fault_location": "    return total / len(values)",
                "bug_category": "boundary", "confidence": 0.9})
        out = json.dumps({"choices": [{"message": {"content": content}}],
                          "usage": {"prompt_tokens": 5, "completion_tokens": 5}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture
def two_tasks(tmp_path):
    a = make_task(tmp_path, "B0001")
    b = make_task(tmp_path, "B0002", buggy=BUGGY_REGEX, fixed=FIXED_REGEX,
                  goal="is_safe returns True for a banned claim after a negated one")
    b.category = "bug_fix"
    p = write_task_file(tmp_path, [a, b])
    return load_tasks(str(p)), tmp_path


@pytest.fixture
def fake_model_server():
    srv = HTTPServer(("127.0.0.1", 0), _FakeModelHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def test_run_model_end_to_end_scores_both_stages(two_tasks, fake_model_server, monkeypatch):
    tasks, tmp_path = two_tasks
    spec = ModelSpec(name="fake", base_url=f"http://127.0.0.1:{fake_model_server.server_port}/v1",
                     api_key=REGISTRY_KEY, model="m")
    monkeypatch.setattr("bugbench.runner.get", lambda name: spec, raising=False)
    monkeypatch.setattr("bugbench.runners.REGISTRY", [spec])
    judge = Judge(runner=_StubJudgeRunner())
    rows = run_model("fake", tasks, stages=("diagnose", "repair"), judge=judge, workers=2)
    assert len(rows) == 2
    for r in rows:
        assert "diagnose" in r and "repair" in r
        assert 0.0 <= r["combined"] <= 1.0
        assert r["diagnose"]["total"] > 0.0
        assert r["repair"]["total"] > 0.0
    out = append_results(rows, str(tmp_path / "results.jsonl"))
    assert len(load_results(out)) == 2


def test_run_model_records_errors_without_raising(two_tasks, tmp_path, monkeypatch):
    tasks, _ = two_tasks

    class Boom:
        def complete_messages(self, messages):
            raise RuntimeError("endpoint down")

    rows = run_model("x", tasks, stages=("repair",), judge=Judge(spec=None),
                     workers=1) if False else None
    # direct exercise of the error path
    from bugbench.runner import _repair_one
    r = _repair_one(tasks[0], Boom(), Judge(spec=None), None)
    assert r["error"].startswith("RuntimeError")
    assert r["total"] == 0.0


# ---------------------------------------------------------------- report
def test_bootstrap_ci_brackets_the_point_estimate():
    vals = [0.0, 0.5, 1.0, 0.25, 0.75, 0.5]
    p, lo, hi = bootstrap_ci(vals, n=400)
    assert lo <= p <= hi
    assert 0.0 <= lo and hi <= 1.0


def test_bootstrap_ci_is_deterministic():
    vals = [0.1, 0.9, 0.4, 0.6, 0.2]
    assert bootstrap_ci(vals, n=200) == bootstrap_ci(vals, n=200)


def test_leaderboard_orders_by_score_and_keeps_ci():
    rows = [{"model": "a", "task_id": f"T{i}", "combined": v}
            for i, v in enumerate([0.1, 0.9, 0.5])] + \
           [{"model": "b", "task_id": f"T{i}", "combined": v}
            for i, v in enumerate([0.4, 0.4])]
    lb = leaderboard(rows)
    assert lb[0]["model"] == "a" and lb[0]["score"] == pytest.approx(0.5)
    assert lb[0]["ci95_lo"] <= lb[0]["score"] <= lb[0]["ci95_hi"]


def test_sub_signals_expose_every_component_separately():
    rows = [{"model": "a", "task_id": "T1",
             "diagnose": {"total": 1.0, "location_match": 1.0,
                          "judge_agreement": 1.0, "category_match": 1.0},
             "repair": {"total": 1.0, "patch_reproduction": 1.0,
                        "judge_agreement": 1.0, "oracle_score": 0.0},
             "combined": 1.0}]
    sig = sub_signals(rows)
    assert sig[0]["diagnose.location_match"] == 1.0
    assert sig[0]["repair.oracle_score"] == 0.0


def test_report_shape():
    rep = report([{"model": "a", "task_id": "T1", "combined": 1.0,
                   "diagnose": {"total": 1.0}, "repair": {"total": 1.0},
                   "category": "bug_fix"}])
    for k in ("leaderboard_combined", "leaderboard_diagnose", "leaderboard_repair",
              "category_breakdown", "sub_signals"):
        assert k in rep


# ---------------------------------------------------------------- app
def test_app_endpoints(two_tasks, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import bugbench.models as M
    import bugbench.app as A
    tasks_file = write_task_file(tmp_path, [t.to_dict() for t in two_tasks[0]])
    monkeypatch.setattr(A, "DATA", str(tasks_file))
    monkeypatch.setattr(A, "RESULTS", str(tmp_path / "r.jsonl"))
    c = TestClient(A.app)
    h = c.get("/health").json()
    assert h["ok"] and h["tasks"] == 2
    t = c.get("/api/tasks").json()
    assert t["total"] == 2
    assert "buggy" not in t["tasks"][0]          # ground truth is not served
    assert c.get("/api/leaderboard").json()["n_rows"] == 0
    m = c.get("/api/models").json()["models"]
    assert any(x["name"] == "local-mimo-9b" for x in m)
    assert c.get("/").status_code == 200
    r = c.post("/api/run", json={"model": "does-not-exist", "limit": 1})
    assert r.status_code == 404


def test_registry_has_local_and_cloud_models():
    names = {m["name"] for m in registry()}
    assert "local-mimo-9b" in names
    assert "litellm-auto" in names


# ---------------------------------------------------------------- fallback chain
def test_fallback_runner_skips_an_empty_completion_and_uses_the_next():
    from bugbench.runners import FallbackRunner, ModelSpec
    calls = []

    class R:
        def __init__(self, text, ok=True):
            self.text, self.ok, self.error, self.latency_ms = text, ok, "", 1
            self.prompt_tokens = self.completion_tokens = 0

    class Fake:
        def __init__(self, results):
            self.results = list(results)
            self.name = self.results[0][0]
            self.spec = ModelSpec(name=self.name, base_url="x", api_key="k",
                                  model=self.name)

        def complete_messages(self, messages):
            model, text, ok = self.results.pop(0)
            calls.append(model)
            return R(text, ok)

    fr = FallbackRunner([Fake([("a", "", True)]), Fake([("b", '{"ok":1}', True)])])
    out = fr.complete_messages([{"role": "user", "content": "x"}])
    assert out.text == '{"ok":1}'
    assert calls == ["a", "b"]


def test_fallback_runner_records_the_last_error_when_all_fail():
    from bugbench.runners import FallbackRunner, ModelSpec

    class R:
        text, ok, error, latency_ms = "", False, "boom", 1
        prompt_tokens = completion_tokens = 0

    class Fake:
        def __init__(self, name):
            self.spec = ModelSpec(name=name, base_url="x", api_key="k", model=name)

        def complete_messages(self, messages):
            return R()

    fr = FallbackRunner([Fake("a"), Fake("b")])
    out = fr.complete_messages([{"role": "user", "content": "x"}])
    assert not out.ok and out.error == "boom"
    # retried across the retry budget rather than giving up on the first pass
    assert fr.tried[:2] == ["a", "b"] and len(fr.tried) == 6


def test_json_runner_prefers_a_json_capable_model():
    from bugbench.runners import json_runner
    assert json_runner().specs[0].model == "openrouter-qwen-3.8-27b"
    # every registered model must name a model; API lanes must also have an endpoint
    for m in registry():
        assert m["model"]
        if m["kind"] == "openai":
            assert m["base_url"], m


def test_oracle_coverage_is_reported_and_distinguishes_subsets():
    from bugbench.report import oracle_coverage
    rows = [{"model": "a", "task_id": "T1", "repair": {"oracle": "exec_diff",
                                                       "judge_agreement": 1.0}},
            {"model": "a", "task_id": "T2", "repair": {"oracle": "none",
                                                       "judge_agreement": 1.0}}]
    cov = oracle_coverage(rows)
    assert cov["with_oracle"] == 1 and cov["rows"] == 2
    assert cov["oracle_rate"] == 0.5
    assert "separately" in cov["note"]


def test_diagnose_judge_score_reaches_the_aggregate():
    """Regression: the judge's mechanism score must be the diagnose judge_agreement.

    It was computed and then dropped on the floor, so every model scored exactly 0.0 on
    the judge component of stage 1.
    """
    from bugbench.runner import _diagnose_one
    seen = {}

    class Runner:
        def complete_messages(self, messages):
            if "CANDIDATE DIAGNOSIS" in messages[-1]["content"]:
                return type("R", (), {"text": json.dumps(
                    {"mechanism": 0.75, "location_ok": True, "reason": "ok"}),
                    "ok": True, "error": "", "latency_ms": 1,
                    "prompt_tokens": 1, "completion_tokens": 1})()
            return type("R", (), {"text": json.dumps(
                {"root_cause": "empty input divides by zero",
                 "fault_location": "    return total / len(values)",
                 "bug_category": "boundary"}),
                "ok": True, "error": "", "latency_ms": 1,
                "prompt_tokens": 1, "completion_tokens": 1})()

    judge = Judge(runner=Runner())
    out = _diagnose_one(make_task(None), Runner(), judge)
    assert out["mechanism"] == 0.75
    assert out["judge_agreement"] == 0.75
    assert out["total"] > 0.5
    seen["ok"] = True
    assert seen["ok"]


def test_diagnose_judge_uses_a_diagnosis_rubric_not_the_patch_rubric():
    from bugbench.prompts import build_diagnose_judge_messages
    msgs = build_diagnose_judge_messages(make_task(None), {"root_cause": "x"})
    assert "MECHANISM" in msgs[0]["content"].upper()
    assert "CANDIDATE DIAGNOSIS" in msgs[1]["content"]
    assert "is the bug fixed" not in msgs[0]["content"]


def test_throttle_recovers_after_a_success_and_backs_off_on_throttling():
    from bugbench.runners import Throttle
    t = Throttle(min_interval=1.0)
    assert t.penalise() == 2.0
    assert t.penalise() == 4.0
    t.reward()
    assert t.min_interval < 4.0


def test_fallback_runner_retries_when_the_pool_is_throttling():
    from bugbench.runners import FallbackRunner, ModelSpec, Throttle

    class R:
        def __init__(self, text, ok, error=""):
            self.text, self.ok, self.error = text, ok, error
            self.latency_ms = 1
            self.prompt_tokens = self.completion_tokens = 0

    class Flaky:
        def __init__(self):
            self.name = "flaky"
            self.spec = ModelSpec(name="flaky", base_url="x", api_key="k", model="flaky")
            self.n = 0

        def complete_messages(self, messages):
            self.n += 1
            if self.n < 3:
                return R("", False, "HTTP 500 throttling your request speed")
            return R('{"ok":1}', True)

    f = Flaky()
    fr = FallbackRunner([f], min_chars=2, throttle=Throttle(min_interval=0.01))
    out = fr.complete_messages([{"role": "user", "content": "x"}])
    assert out.ok and out.text == '{"ok":1}'
    assert f.n == 3


def test_leaderboard_reports_infrastructure_errors_separately():
    rows = [{"model": "a", "task_id": "T1", "combined": 0.9,
             "repair": {"total": 0.9, "error": ""}},
            {"model": "a", "task_id": "T2", "combined": 0.0,
             "repair": {"total": 0.0, "error": "HTTP 429 cooldown"}},
            {"model": "b", "task_id": "T1", "combined": 0.9,
             "repair": {"total": 0.9, "error": ""}}]
    lb = {e["model"]: e for e in leaderboard(rows)}
    assert lb["a"]["errors"] == 1 and lb["a"]["error_rate"] == 0.5
    assert lb["b"]["errors"] == 0


# ---------------------------------------------------------------- layer 2
def test_repair_given_stage_uses_a_prompt_that_discloses_the_bug():
    from bugbench.prompts import build_repair_given_messages
    t = make_task(None, goal="average() throws ZeroDivisionError on an empty list")
    msgs = build_repair_given_messages(t)
    assert "BUG REPORT" in msgs[1]["content"]
    assert "average() throws ZeroDivisionError" in msgs[1]["content"]
    # the reference fix must still never appear
    msgs[0]["content"] = msgs[0]["content"].replace("{lang}", "python")
    assert "reference" not in msgs[0]["content"].lower()


def test_run_model_records_repair_given_and_the_diagnosis_uplift(two_tasks, tmp_path,
                                                                  fake_model_server,
                                                                  monkeypatch):
    tasks, _ = two_tasks
    spec = ModelSpec(name="fake",
                     base_url=f"http://127.0.0.1:{fake_model_server.server_port}/v1",
                     api_key=REGISTRY_KEY, model="m")
    monkeypatch.setattr("bugbench.runners.REGISTRY", [spec])
    rows = run_model("fake", tasks, stages=("diagnose", "repair", "repair_given"),
                     judge=Judge(runner=_StubJudgeRunner()), workers=1)
    for r in rows:
        assert "repair_given" in r
        assert r["repair_given"]["stage"] == "repair_given_bug"
        assert "diagnosis_uplift" in r
    # the fake model returns the reference snippet, so layer 2 must not be worse
    assert all(r["repair_given"]["total"] >= r["diagnose"]["total"] for r in rows)


def test_report_exposes_a_separate_layer2_leaderboard():
    rows = [{"model": "a", "task_id": "T1", "combined": 0.5,
             "diagnose": {"total": 0.2}, "repair": {"total": 0.8, "error": ""},
             "repair_given": {"total": 0.9}, "diagnosis_uplift": 0.7}]
    rep = report(rows)
    assert rep["leaderboard_repair_given"][0]["score"] == 0.9
    assert rep["diagnosis_uplift"][0]["score"] == 0.7


# ---------------------------------------------------------------- cheat detection
def test_flags_a_verbatim_reproduction_of_the_historical_fix():
    from bugbench.cheat import check_row
    t = make_task(None)
    row = {"model": "m", "task_id": t.task_id,
           "repair": {"candidate": t.reference_fix, "raw": "ok",
                      "patch_reproduction": 1.0, "judge_agreement": 1.0}}
    v = check_row(row, t)
    assert v["cheating"] is True
    assert "verbatim_reproduction" in v["fired"]
    assert v["severity"] == "high"


def test_flags_echoing_the_buggy_snippet_back():
    from bugbench.cheat import check_row
    t = make_task(None)
    row = {"model": "m", "task_id": t.task_id,
           "repair": {"candidate": t.buggy, "raw": "here is the code",
                      "judge_agreement": 1.0, "patch_reproduction": 0.9}}
    v = check_row(row, t)
    assert "echo_input" in v["fired"]
    # a judge that says "fixed" for an unmodified snippet is itself a finding
    assert "judge_gaming" in v["fired"]


def test_flags_a_refusal_or_a_reply_with_no_code():
    from bugbench.cheat import check_row
    t = make_task(None)
    row = {"model": "m", "task_id": t.task_id,
           "repair": {"candidate": "I cannot help with that.",
                      "raw": "I'm sorry, I can't do that.",
                      "judge_agreement": 0.0, "patch_reproduction": 0.1}}
    v = check_row(row, t)
    assert "degenerate_output" in v["fired"]


def test_a_genuine_fix_is_not_flagged():
    from bugbench.cheat import check_row
    t = make_task(None)
    # correct fix that is written differently from the reference
    alt = ("def average(values):\n"
           "    if len(values) == 0:\n"
           "        return None\n"
           "    return sum(values) / float(len(values))\n")
    row = {"model": "m", "task_id": t.task_id,
           "repair": {"candidate": alt, "raw": "```python\n%s\n```" % alt,
                      "judge_agreement": 1.0, "patch_reproduction": 0.6}}
    v = check_row(row, t)
    assert v["cheating"] is False, v["fired"]


def test_screen_summarises_per_model():
    from bugbench.cheat import screen
    from bugbench.models import Task
    t = make_task(None, task_id="X1")
    tasks = {"X1": t}
    rows = [
        {"model": "a", "task_id": "X1", "repair": {"candidate": t.reference_fix,
                                                    "raw": "", "judge_agreement": 1.0}},
        {"model": "b", "task_id": "X1", "repair": {"candidate": "x = 1\n",
                                                    "raw": "", "judge_agreement": 1.0}},
    ]
    s = screen(rows, tasks)
    assert s["total_flagged"] == 1
    per = {m["model"]: m for m in s["per_model"]}
    assert per["a"]["flagged"] == 1 and per["b"]["flagged"] == 0


# ---------------------------------------------------------------- markers
def test_marks_give_integer_counts_out_of_a_total():
    from bugbench.marks import mark_all
    t = make_task(None, task_id="M1")
    rows = [{"model": "a", "task_id": "M1", "combined": 1.0,
             "diagnose": {"total": 1.0, "location_match": 1.0, "judge_agreement": 0.9},
             "repair": {"total": 1.0, "judge_agreement": 1.0, "patch_reproduction": 1.0,
                        "candidate": t.reference_fix}},
            {"model": "a", "task_id": "M1", "combined": 0.0,
             "diagnose": {"total": 0.0, "location_match": 0.0, "judge_agreement": 0.0},
             "repair": {"total": 0.0, "judge_agreement": 0.0, "patch_reproduction": 0.0,
                        "candidate": t.buggy}}]
    m = mark_all(rows, {"M1": t})
    assert m[0]["bug_found"] and m[0]["bug_fixed"] and m[0]["same_fix"]
    assert m[0]["score_pct"] == 100.0
    assert not m[1]["bug_found"] and m[1]["untouched"]
    assert m[1]["score_pct"] == 0.0


def test_marker_leaderboard_reports_n_out_of_m_as_percentages():
    from bugbench.marks import leaderboard_marks
    t = make_task(None, task_id="M1")
    rows = [{"model": "a", "task_id": "M1", "combined": 0.5,
             "diagnose": {"total": 0.5, "location_match": 1.0, "judge_agreement": 0.5},
             "repair": {"total": 0.5, "judge_agreement": 1.0,
                        "patch_reproduction": 0.5, "candidate": "def f():\n    return 2\n"}}]
    lb = leaderboard_marks(rows, {"M1": t})
    r = lb[0]
    import re as _re
    assert _re.fullmatch(r"\d+/\d+ \(\d+(\.\d+)?%\)", r["found_str"]), r["found_str"]
    assert 0.0 <= r["score_pct"] <= 100.0
    assert r["tasks"] == 1


def test_marks_never_exceed_the_task_count():
    from bugbench.marks import leaderboard_marks
    t = make_task(None, task_id="M1")
    rows = [{"model": "a", "task_id": "M1", "combined": 1.0,
             "diagnose": {"total": 1.0, "location_match": 1.0, "judge_agreement": 1.0},
             "repair": {"total": 1.0, "judge_agreement": 1.0, "patch_reproduction": 1.0,
                        "candidate": t.reference_fix}}]
    r = leaderboard_marks(rows, {"M1": t})[0]
    assert r["bugs_found"] <= r["tasks"] and r["bugs_fixed"] <= r["tasks"]


def test_returning_the_buggy_snippet_unchanged_scores_zero():
    """The judge must not be able to award credit for an unmodified input.

    Measured on the first run: 22 rows echoed the input, the judge said 'fixed' on 15.
    """
    task = make_task(None)
    s = score_repair(task, BUGGY_ARITH, judge_agreement=1.0)
    assert s["total"] == 0.0
    assert s["penalty"] == "unchanged_input"
    assert s["judge_agreement"] == 0.0


def test_unchanged_detection_ignores_trivial_snippets():
    from bugbench.grade import is_unchanged
    assert is_unchanged("x = 1\n", "x = 1\n") is False      # too short to judge
    assert is_unchanged("", BUGGY_ARITH) is False
    assert is_unchanged(BUGGY_ARITH, BUGGY_ARITH) is True
    assert is_unchanged(FIXED_ARITH, BUGGY_ARITH) is False


def test_a_real_fix_still_scores_normally_after_the_unchanged_guard():
    task = make_task(None)
    s = score_repair(task, FIXED_ARITH, judge_agreement=1.0)
    assert s["total"] >= 0.7
    assert "penalty" not in s


def test_runner_falls_back_to_reasoning_content_when_content_is_empty():
    """Reasoning models (gpt-oss, space-bunny-alpha) return everything in
    `reasoning_content` with an empty `content`. Dropping it made them look silent."""
    import json as _json
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            out = _json.dumps({"choices": [{"message": {
                "content": "", "reasoning_content": '{"root_cause":"empty input"}'}}],
                "usage": {}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    spec = ModelSpec(name="r", base_url=f"http://127.0.0.1:{srv.server_port}/v1",
                     api_key="k", model="reasoner")
    r = OpenAIChatRunner(spec).complete("hi")
    srv.shutdown()
    assert r.ok
    assert r.text == '{"root_cause":"empty input"}'
    assert r.text_source == "reasoning_content"


def test_run_model_streams_rows_so_a_crash_keeps_finished_work(two_tasks, tmp_path,
                                                               fake_model_server,
                                                               monkeypatch):
    """Regression: rows used to be appended only when the whole run finished, so a
    timeout on a slow model silently discarded every task that had already scored."""
    tasks, _ = two_tasks
    spec = ModelSpec(name="fake",
                     base_url=f"http://127.0.0.1:{fake_model_server.server_port}/v1",
                     api_key=REGISTRY_KEY, model="m")
    monkeypatch.setattr("bugbench.runners.REGISTRY", [spec])
    seen = []
    run_model("fake", tasks, stages=("repair",), judge=Judge(runner=_StubJudgeRunner()),
              workers=1, on_row=seen.append)
    assert len(seen) == len(tasks)


def test_agy_runner_is_selected_for_agy_models():
    from bugbench.runners import AgyRunner, runner_for
    spec = ModelSpec(name="a", kind="agy", model="gemini-3.8-flash-high", effort="high")
    assert isinstance(runner_for(spec), AgyRunner)


def test_agy_models_are_registered_with_high_effort():
    from bugbench.runners import AGY
    names = {n for n, _, _ in AGY}
    assert "agy-gemini-3.8-flash-high" in names
    for n, m, e in AGY:
        if "high" in n:
            assert e == "high", f"{n} should run at high effort"


def test_byte_identical_plus_readable_source_is_a_critical_leak(tmp_path):
    """The agy run reproduced 8/11 fixes byte-for-byte because the agent read the real
    repo. Byte-identity ALONE is memorisation; byte-identity when the source file is on
    disk is a file-read leak and must outrank it."""
    from bugbench.cheat import check_row
    d = tmp_path / "widget.py"
    d.write_text("x = 1\n")
    t = make_task(None, file_name="widget.py")
    row = {"model": "agy", "task_id": t.task_id,
           "repair": {"candidate": t.reference_fix, "raw": "", "judge_agreement": 1.0}}
    v = check_row(row, t, roots=(str(tmp_path),))
    assert v["signals"]["file_read_leak"]["fired"] is True
    assert v["severity"] == "critical"


def test_no_file_read_leak_when_the_source_is_not_on_disk():
    from bugbench.cheat import check_row
    t = make_task(None, file_name="not_on_disk_anywhere_xyz.py")
    row = {"model": "api", "task_id": t.task_id,
           "repair": {"candidate": t.reference_fix, "raw": "", "judge_agreement": 1.0}}
    v = check_row(row, t, roots=("/nonexistent",))
    assert v["signals"]["file_read_leak"]["fired"] is False


def test_file_read_leak_rows_are_quarantined_from_the_headline(tmp_path):
    from bugbench.marks import clean_rows, leaderboard_marks
    d = tmp_path / "widget.py"
    d.write_text("x = 1\n")
    t = make_task(None, task_id="L1", file_name="widget.py")
    leak = {"model": "agy", "task_id": "L1", "combined": 1.0,
            "diagnose": {"total": 1.0, "location_match": 1.0, "judge_agreement": 1.0},
            "repair": {"total": 1.0, "judge_agreement": 1.0,
                       "patch_reproduction": 1.0, "candidate": t.reference_fix}}
    ok = {"model": "agy", "task_id": "L1", "combined": 0.4,
          "diagnose": {"total": 0.4, "location_match": 1.0, "judge_agreement": 0.4},
          "repair": {"total": 0.4, "judge_agreement": 0.5,
                     "patch_reproduction": 0.3, "candidate": "def f():\n    return 1\n"}}
    trusted, quar = clean_rows([leak, ok], {"L1": t}, roots=(str(tmp_path),))
    assert leak in quar and ok in trusted


def test_common_task_set_only_keeps_tasks_seen_by_several_models():
    from bugbench.marks import common_task_set
    rows = [{"model": "a", "task_id": "T1"}, {"model": "a", "task_id": "T2"},
            {"model": "b", "task_id": "T1"}, {"model": "b", "task_id": "T3"}]
    assert common_task_set(rows, min_models=2) == {"T1"}


def test_restricting_to_a_common_set_makes_denominators_equal():
    from bugbench.marks import leaderboard_marks
    t1, t2 = make_task(None, task_id="T1"), make_task(None, task_id="T2")
    rows = [
        {"model": "a", "task_id": "T1", "combined": 1.0,
         "diagnose": {"total": 1.0, "location_match": 1.0, "judge_agreement": 1.0},
         "repair": {"total": 1.0, "judge_agreement": 1.0, "patch_reproduction": 1.0,
                    "candidate": t1.reference_fix}},
        {"model": "a", "task_id": "T2", "combined": 0.0,
         "diagnose": {"total": 0.0, "location_match": 0.0, "judge_agreement": 0.0},
         "repair": {"total": 0.0, "judge_agreement": 0.0, "patch_reproduction": 0.0,
                    "candidate": t2.buggy}},
        {"model": "b", "task_id": "T1", "combined": 0.5,
         "diagnose": {"total": 0.5, "location_match": 1.0, "judge_agreement": 0.5},
         "repair": {"total": 0.5, "judge_agreement": 1.0, "patch_reproduction": 0.4,
                    "candidate": "def f():\n    return 1\n"}},
    ]
    tasks = {"T1": t1, "T2": t2}
    assert {m["tasks"] for m in leaderboard_marks(rows, tasks)} == {2, 1}
    same = leaderboard_marks(rows, tasks, restrict_to={"T1"})
    assert {m["tasks"] for m in same} == {1}
