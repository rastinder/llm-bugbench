"""Orchestration: run models over tasks, judge, grade, persist results."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .grade import (extract_code, parse_diagnosis, parse_json_block,
                    score_diagnose, score_repair)
from .models import RESULTS, Task
from .oracle import build_oracle
from .prompts import (build_diagnose_judge_messages, build_diagnose_messages,
                      build_judge_messages, build_repair_given_messages,
                      build_repair_messages)
from .runners import Result, runner_for

_JUDGE_CACHE: dict[str, float] = {}
_JUDGE_LOCK = threading.Lock()
JUDGE_SPEC_NAME = "litellm-auto"


def _cache_key(task: Task, candidate: str, ref: str | None, aware: bool) -> str:
    h = hashlib.sha256()
    h.update(task.task_id.encode())
    h.update(b"\x00CAND\x00")
    h.update(candidate.encode())
    h.update(b"\x00REF\x00" if aware else b"\x00NOREF\x00")
    if aware:
        h.update((ref or "").encode())
    return h.hexdigest()


class Judge:
    """Blind, frozen-rubric LLM judge with content-addressed caching.

    Two distinct rubrics: `score_repair` judges a candidate PATCH, `score_diagnosis` judges
    a candidate DIAGNOSIS. They must not share a rubric -- asking a patch rubric to grade a
    JSON diagnosis made every model score zero.
    """

    def __init__(self, spec=None, blind: bool = True, runner=None):
        self.spec = spec
        self.blind = blind
        self._runner = runner if runner is not None else (
            runner_for(spec) if spec else None)

    def score_diagnosis(self, task: Task, diagnosis: dict) -> dict:
        """Return {'judge_agreement': float, 'mechanism': float, 'location_ok': bool}."""
        empty = {"judge_agreement": 0.0, "mechanism": 0.0, "location_ok": False}
        if self._runner is None or not diagnosis:
            return empty
        key = hashlib.sha256(
            (task.task_id + "|DIAG|" +
             json.dumps(diagnosis, sort_keys=True)).encode()).hexdigest()
        with _JUDGE_LOCK:
            if key in _JUDGE_CACHE:
                return _JUDGE_CACHE[key]
        try:
            r = self._runner.complete_messages(
                build_diagnose_judge_messages(task, diagnosis))
        except Exception:
            r = Result(text="", error="diagnose judge call failed")
        out = empty
        if r.ok:
            d = parse_json_block(r.text) or {}
            try:
                mech = max(0.0, min(1.0, float(d.get("mechanism", 0.0))))
            except (TypeError, ValueError):
                mech = 0.0
            loc = bool(d.get("location_ok"))
            out = {"judge_agreement": round(mech, 4), "mechanism": round(mech, 4),
                   "location_ok": loc}
        with _JUDGE_LOCK:
            _JUDGE_CACHE[key] = out
        return out

    def score(self, task: Task, candidate: str, reference_aware: bool = False) -> float:
        if self._runner is None:
            return 0.0
        key = _cache_key(task, candidate,
                         task.reference_fix if reference_aware else None,
                         reference_aware)
        with _JUDGE_LOCK:
            if key in _JUDGE_CACHE:
                return _JUDGE_CACHE[key]
        ref = task.reference_fix if reference_aware else None
        msgs = build_judge_messages(task, candidate, ref, reference_aware)
        val = 0.0
        try:
            r = self._runner.complete_messages(msgs)
        except Exception:
            r = Result(text="", error="judge call failed")
        if r.ok:
            d = parse_json_block(r.text) or {}
            if reference_aware:
                val = float(d.get("agreement", 0.0) or 0.0)
            else:
                val = 1.0 if d.get("fixed") is True else 0.0
            val = max(0.0, min(1.0, val))
        with _JUDGE_LOCK:
            _JUDGE_CACHE[key] = val
        return val


def _diagnose_one(task: Task, runner, judge: Judge) -> dict:
    from .runners import GLOBAL_THROTTLE
    GLOBAL_THROTTLE.wait()
    try:
        r = runner.complete_messages(build_diagnose_messages(task))
    except Exception as e:
        r = Result(text="", error=f"{type(e).__name__}: {e}")
    pred = parse_diagnosis(r.text) if r.ok else None
    dj = judge.score_diagnosis(task, pred) if pred else {
        "judge_agreement": 0.0, "mechanism": 0.0, "location_ok": False}
    graded_pred = dict(pred or {})
    graded_pred["judge_agreement"] = dj["judge_agreement"]
    out = score_diagnose(task, graded_pred)
    out["mechanism"] = dj["mechanism"]
    out["judge_location_ok"] = dj["location_ok"]
    out["raw"] = r.text[:2000]
    out["error"] = r.error
    out["latency_ms"] = r.latency_ms
    out["parsed"] = pred
    return out


def _repair_given_one(task: Task, runner, judge: Judge, oracle) -> dict:
    """Layer 2: the bug is disclosed, only the repair is scored."""
    from .runners import GLOBAL_THROTTLE
    GLOBAL_THROTTLE.wait()
    try:
        r = runner.complete_messages(build_repair_given_messages(task))
    except Exception as e:
        r = Result(text="", error=f"{type(e).__name__}: {e}")
    cand = extract_code(r.text) if r.ok else ""
    ag = judge.score(task, cand) if cand else 0.0
    out = score_repair(task, cand, ag, oracle=oracle)
    out["raw"] = r.text[:4000]
    out["candidate"] = cand
    out["error"] = r.error
    out["latency_ms"] = r.latency_ms
    out["stage"] = "repair_given_bug"
    return out


def _repair_one(task: Task, runner, judge: Judge, oracle) -> dict:
    from .runners import GLOBAL_THROTTLE
    GLOBAL_THROTTLE.wait()
    try:
        r = runner.complete_messages(build_repair_messages(task))
    except Exception as e:
        r = Result(text="", error=f"{type(e).__name__}: {e}")
    cand = extract_code(r.text) if r.ok else ""
    ag = judge.score(task, cand) if cand else 0.0
    out = score_repair(task, cand, ag, oracle=oracle)
    out["raw"] = r.text[:4000]
    out["candidate"] = cand
    out["error"] = r.error
    out["latency_ms"] = r.latency_ms
    return out


def run_model(model_name: str, tasks: list[Task],
              stages=("diagnose", "repair", "repair_given"),
              judge: Judge | None = None, workers: int = 3,
              judge_ref_aware: bool = False, on_row=None) -> list[dict]:
    from .runners import get
    spec = get(model_name)
    runner = runner_for(spec)
    rows = []

    from .runners import GLOBAL_THROTTLE

    def work(t: Task):
        oracle = build_oracle(t) if "repair" in stages else None
        row = {"model": model_name, "task_id": t.task_id,
               "language": t.language, "category": t.category,
               "project": t.project, "file_name": t.file_name,
               "goal_had_alternatives": t.goal_had_alternatives,
               "ts": int(time.time())}
        if "diagnose" in stages:
            d = _diagnose_one(t, runner, judge)
            row["diagnose"] = d
            row["diagnose_error"] = d.get("error", "")
        if "repair" in stages:
            rp = _repair_one(t, runner, judge, oracle)
            rp["reference_aware"] = judge_ref_aware
            row["repair"] = rp
            row["repair_error"] = rp.get("error", "")
        if "repair_given" in stages:
            rg = _repair_given_one(t, runner, judge, oracle)
            row["repair_given"] = rg
            row["repair_given_error"] = rg.get("error", "")
        if "repair_given" in row and "diagnose" in row:
            # how much of the model's success depended on its own diagnosis
            row["diagnosis_uplift"] = round(
                row["repair_given"]["total"] - row["diagnose"]["total"], 4)
        if "diagnose" in row and "repair" in row:
            row["combined"] = round(
                0.5 * row["diagnose"]["total"] + 0.5 * row["repair"]["total"], 4)
        return row

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        # as_completed, not map: a timeout or a crash must not discard the tasks that
        # already finished. on_row lets the caller persist each row as it lands.
        for row in ex.map(work, tasks):
            rows.append(row)
            if on_row is not None:
                try:
                    on_row(row)
                except Exception:
                    pass
    return rows


def append_results(rows: list[dict], path: str = RESULTS) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


def load_results(path: str = RESULTS) -> list[dict]:
    if not os.path.exists(path):
        return []
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows
