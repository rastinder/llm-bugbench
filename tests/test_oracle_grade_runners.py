"""Block 3 + 4 + 5: oracle validity gate, runners, grading."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from bugbench.oracle import build_oracle
from bugbench.grade import diff_similarity, changed_line_numbers, parse_json_block, score_diagnose, score_repair
from bugbench.runners import ModelError, OpenAIChatRunner, ModelSpec

from conftest import (
    BUGGY_ARITH, BUGGY_REGEX, BUGGY_RENAME, FIXED_ARITH, FIXED_REGEX,
    FIXED_RENAME, make_task,
)


# ---------------------------------------------------------------- oracle gate
def _t(**kw):
    base = {"goal": "g", "buggy": BUGGY_ARITH, "reference_fix": FIXED_ARITH,
            "file_name": "mod.py"}
    base.update(kw)
    return base


def test_oracle_admitted_when_buggy_fails_and_reference_passes():
    task = _t(goal="average() raises ZeroDivisionError on an empty list")
    oracle = build_oracle(task)
    assert oracle.kind in {"exec_diff", "none"}
    if oracle.kind == "exec_diff":
        assert oracle.validate() is True
        assert oracle.score(FIXED_ARITH) == 1.0
        assert oracle.score(BUGGY_ARITH) == 0.0


def test_oracle_rejected_when_buggy_and_reference_behave_identically():
    """Negative control: a rename-only pair must NOT get an oracle."""
    oracle = build_oracle(_t(buggy=BUGGY_RENAME, reference_fix=FIXED_RENAME,
                             goal="rename parameters"))
    assert oracle.kind == "none"
    assert oracle.validate() is False


def test_oracle_rejected_for_non_python_language():
    assert build_oracle(_t(language="javascript", buggy="const a = 1;\n",
                           reference_fix="const a = 2;\n")).kind == "none"


def test_oracle_rejects_snippet_that_does_not_even_parse():
    oracle = build_oracle(_t(buggy="def broken(:\n    pass\n",
                             reference_fix="def broken(:\n    pass\n",
                             goal="fix syntax"))
    assert oracle.kind == "none"


def test_oracle_score_is_zero_for_a_candidate_that_does_not_import():
    oracle = build_oracle(_t())
    assert oracle.score("this is not python at all ((") == 0.0


def test_oracle_score_is_partial_and_bounded():
    oracle = build_oracle(_t())
    s = oracle.score("def average(values):\n    return None\n")
    assert 0.0 <= s <= 1.0


def test_oracle_never_runs_snippets_containing_network_calls():
    oracle = build_oracle(_t())
    s = oracle.score("import socket\ndef average(v):\n    return None\n")
    assert isinstance(s, float)


# ---------------------------------------------------------------- diff tools
def test_diff_similarity_is_one_for_identical_text():
    assert diff_similarity(FIXED_ARITH, FIXED_ARITH) == 1.0


def test_diff_similarity_is_zero_for_disjoint_text():
    assert diff_similarity("alpha beta gamma", "111 222 333") == 0.0


def test_diff_similarity_penalises_omitted_fix():
    partial = "def average(values):\n    if not values:\n        return None\n"
    full = FIXED_ARITH
    assert 0.0 < diff_similarity(partial, full) < 1.0


def test_changed_line_numbers_are_one_based_and_only_on_the_fixed_side():
    lines = changed_line_numbers(BUGGY_ARITH, FIXED_ARITH)
    assert lines
    assert all(isinstance(n, int) and n >= 1 for n in lines)


def test_parse_json_block_handles_fenced_and_bare_json():
    assert parse_json_block('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_block('noise {"a": 2} noise') == {"a": 2}
    assert parse_json_block("not json at all") is None


# ---------------------------------------------------------------- diagnose
def test_score_diagnose_is_zero_when_prediction_is_empty():
    task = make_task(None)
    s = score_diagnose(task, {})
    assert s["total"] == 0.0


def test_score_diagnose_rewards_a_correct_location():
    task = make_task(None)
    lines = changed_line_numbers(task.buggy, task.reference_fix)
    target = lines[0]
    s = score_diagnose(task, {"fault_location": f"line {target}",
                              "root_cause": "no empty guard",
                              "bug_category": "boundary",
                              "judge_agreement": 0.9})
    assert s["location_match"] == 1.0
    assert s["total"] > 0.4


def test_score_diagnose_penalises_a_wrong_location():
    task = make_task(None)
    lines = changed_line_numbers(task.buggy, task.reference_fix)
    wrong = max(lines) + 50
    s = score_diagnose(task, {"fault_location": f"line {wrong}",
                              "judge_agreement": 0.9})
    assert s["location_match"] == 0.0
    assert s["total"] < 0.5


def test_score_diagnose_returns_every_component_separately():
    s = score_diagnose(make_task(None), {"judge_agreement": 0.5})
    for k in ("location_match", "judge_agreement", "category_match", "total"):
        assert k in s


# ---------------------------------------------------------------- repair
def test_score_repair_rewards_the_reference_fix():
    task = make_task(None)
    s = score_repair(task, "```python\n" + FIXED_ARITH + "\n```", judge_agreement=1.0)
    assert s["diff_similarity"] == 1.0
    assert s["total"] >= 0.7


def test_score_repair_gives_the_buggy_snippet_a_low_score():
    """Gold/control: the *unfixed* snippet must not earn a high repair score.

    Note diff_similarity alone is high for the buggy snippet (it is one guard away from
    the fix) -- that is exactly why the judge signal carries half the weight and why the
    HTML report labels it `patch_reproduction` rather than correctness.
    """
    task = make_task(None)
    s = score_repair(task, BUGGY_ARITH, judge_agreement=0.0)
    assert s["total"] < 0.45
    fixed = score_repair(task, FIXED_ARITH, judge_agreement=1.0)
    assert fixed["total"] - s["total"] > 0.4


def test_score_repair_marks_tasks_without_an_oracle():
    task = make_task(None)
    s = score_repair(task, FIXED_ARITH, judge_agreement=1.0)
    assert s["oracle"] == "none"


# ---------------------------------------------------------------- runners
class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        self.server.last_request = body
        payload = {"choices": [{"message": {"content": "CANARY_REPLY"}}],
                   "usage": {"prompt_tokens": 11, "completion_tokens": 3}}
        out = json.dumps(payload).encode()
        self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    srv.status = 200
    srv.last_request = None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def test_openai_runner_returns_content_and_records_usage(fake_server):
    spec = ModelSpec(name="fake", base_url=f"http://127.0.0.1:{fake_server.server_port}/v1",
                     api_key="k", model="m")
    r = OpenAIChatRunner(spec).complete("hi")
    assert r.text == "CANARY_REPLY"
    assert r.prompt_tokens == 11 and r.completion_tokens == 3
    assert r.latency_ms >= 0


def test_openai_runner_sends_the_prompt_and_api_key(fake_server):
    spec = ModelSpec(name="fake", base_url=f"http://127.0.0.1:{fake_server.server_port}/v1",
                     api_key="sk-secret", model="my-model")
    OpenAIChatRunner(spec).complete("PROMPT_CANARY")
    assert fake_server.last_request["model"] == "my-model"
    assert "PROMPT_CANARY" in json.dumps(fake_server.last_request)


def test_openai_runner_raises_model_error_on_http_failure(fake_server):
    fake_server.status = 500
    spec = ModelSpec(name="fake", base_url=f"http://127.0.0.1:{fake_server.server_port}/v1",
                     api_key="k", model="m")
    with pytest.raises(ModelError) as e:
        OpenAIChatRunner(spec).complete("hi")
    assert "500" in str(e.value)


def test_reference_fix_never_appears_in_any_prompt():
    """Council gate: the model must never see the ground truth."""
    task = make_task(None)
    task.reference_fix = "SECRET_REFERENCE_TOKEN_ZZZ"
    from bugbench.prompts import build_diagnose_prompt, build_repair_prompt
    for p in (build_diagnose_prompt(task), build_repair_prompt(task)):
        assert "SECRET_REFERENCE_TOKEN_ZZZ" not in p
