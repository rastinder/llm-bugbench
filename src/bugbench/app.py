"""FastAPI app: serve the leaderboard, the task list, and trigger runs."""
from __future__ import annotations

import json
import threading

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import __version__
from .html_report import render_html
from .models import DATA, RESULTS, counts_by_category, load_tasks, primary_tasks
from .report import report as build_report
from .runner import Judge, append_results, load_results, run_model
from .runners import ModelError, get as get_model, registry

app = FastAPI(title="bugbench", version=__version__)
_lock = threading.Lock()


@app.get("/health")
def health():
    try:
        tasks = load_tasks(DATA)
        return {"ok": True, "version": __version__, "tasks": len(tasks),
                "bug_fix_tasks": len(primary_tasks(tasks)),
                "results": len(load_results(RESULTS))}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/tasks")
def api_tasks(category: str | None = None, limit: int = 50, include_code: bool = False):
    tasks = load_tasks(DATA)
    if category:
        tasks = [t for t in tasks if t.category == category]
    total = len(tasks)
    out = []
    for t in tasks[:limit]:
        d = {"task_id": t.task_id, "language": t.language,
             "file_name": t.file_name, "project": t.project,
             "category": t.category, "goal": t.goal,
             "goal_had_alternatives": t.goal_had_alternatives,
             "is_test_file": t.is_test_file}
        if include_code:
            d["buggy"] = t.buggy
        out.append(d)
    return {"total": total, "returned": len(out), "tasks": out}


@app.get("/api/models")
def api_models():
    return {"models": registry()}


@app.get("/api/leaderboard")
def api_leaderboard():
    rows = load_results(RESULTS)
    return build_report(rows)


class RunRequest(BaseModel):
    model: str
    limit: int = 10
    category: str = "bug_fix"
    stages: list[str] = ["diagnose", "repair", "repair_given"]
    workers: int = 3
    judge_ref_aware: bool = False
    persist: bool = True


@app.post("/api/run")
def api_run(req: RunRequest):
    try:
        spec = get_model(req.model)
    except ModelError as e:
        raise HTTPException(status_code=404, detail=str(e))
    tasks = load_tasks(DATA)
    if req.category and req.category != "all":
        tasks = [t for t in tasks if t.category == req.category]
    if not tasks:
        raise HTTPException(status_code=400,
                            detail=f"no tasks in category {req.category!r}")
    tasks = tasks[:req.limit]
    from .runners import json_runner
    judge = Judge(runner=json_runner(min_chars=10))
    with _lock:
        rows = run_model(req.model, tasks, stages=tuple(req.stages), judge=judge,
                         workers=req.workers, judge_ref_aware=req.judge_ref_aware)
    if req.persist:
        append_results(rows, RESULTS)
    return {"model": req.model, "n": len(rows), "rows": rows}


@app.get("/", response_class=HTMLResponse)
def index():
    rows = load_results(RESULTS)
    try:
        tasks = {t.task_id: t for t in load_tasks(DATA)}
    except Exception:
        tasks = {}
    cheat = None
    if tasks:
        try:
            from .cheat import screen
            cheat = screen(rows, tasks)
        except Exception:
            cheat = None
    return HTMLResponse(render_html(rows, tasks, cheat))
