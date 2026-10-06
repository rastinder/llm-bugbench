#!/usr/bin/env python3
"""Run benchmark tasks inside ephemeral Docker containers.

Architecture:
- `bugbench-services`: Container with hidden tests and LLM proxy.
- `bugbench-bench`: Ephemeral container per task, strictly internal network (no internet egress).
- Clean workspace per run: /work contains only the task source code.
- Post-hoc grading: modified file is sent to services:5000/grade.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
from bugbench.sandbox import build_sandbox

HOSTS = REPO_ROOT / "data" / "host_tasks.json"
SOURCES = REPO_ROOT / "data" / "host_tasks_sources.json"
TEMP_RUN_ROOT = Path("/tmp/container_run")

PROMPT = """You are fixing a real bug in a real codebase.

The file `{module}` has a defect. Find and fix it.

Rules:
- Work only inside /work.
- Make the minimal change that fixes the defect. Do not refactor or reformat.
- Do not create test files.

When done, save your edit to `{module}`.

Start by reading `{module}`."""


def wait_for_services(url: str = "http://127.0.0.1:5000/health", timeout: int = 30) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(1)
    return False


def start_services() -> None:
    print("Ensuring services container is running...", flush=True)
    subprocess.run(["docker", "compose", "up", "-d", "services"], cwd=REPO_ROOT, check=True)
    if not wait_for_services():
        raise RuntimeError("bugbench-services failed to become healthy on port 5000")
    print("Services container is healthy and ready.", flush=True)


def grade_via_service(task_id: str, source: str) -> dict:
    url = "http://127.0.0.1:5000/grade"
    payload = json.dumps({"task_id": task_id, "source": source}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"fixed": False, "error": str(e)}


def run_container_task(task: dict, model: str, timeout: int, agent_type: str = "agent", skill: str = "") -> dict:
    task_id = task["task_id"]
    workdir = TEMP_RUN_ROOT / task_id[-24:]
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)

    repo = Path(task.get("repo", REPO_ROOT))
    if not (repo / task["module"]).exists():
        repo = REPO_ROOT

    # Build clean sandbox with no tests and no agent memory
    code_dir = build_sandbox(repo, workdir / "code", drop_tests=True)
    target_file = code_dir / task["module"]
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text(task["before_source"])

    # Remove any leftover .agent.md, .swarm, or hidden files
    for bad in code_dir.rglob("*.md"):
        if "agent" in bad.name.lower() or "plan" in bad.name.lower():
            bad.unlink()
    shutil.rmtree(code_dir / ".swarm", ignore_errors=True)

    desc = task.get("description", "")
    if desc:
        prompt = (
            f"You are fixing a real bug in a real codebase.\n\n"
            f"The file `{task['module']}` has a defect:\n{desc}\n\n"
            f"Rules:\n"
            f"- Work only inside /work.\n"
            f"- Make the minimal change that fixes the defect. Do not refactor or reformat.\n"
            f"- Do not create test files.\n\n"
            f"When done, save your edit to `{task['module']}`.\n\n"
            f"Start by reading `{task['module']}`."
        )
    else:
        prompt = PROMPT.format(module=task["module"])

    net = "bugbench-exec_bench_internal"
    if skill == "deepcraft":
        prompt = (
            "You have the `deepcraft` skill loaded at /root/.config/opencode/skills/deepcraft/SKILL.md.\n"
            "Apply the DeepCraft methodology (TDDAB planning, Protocol D systematic root-cause debugging).\n\n"
            f"{prompt}"
        )

    if agent_type == "opencode":
        opencode_model = model if model.startswith("opencode/") else f"services-gateway/{model}"
        skill_mounts = []
        opencode_flags = ["--auto"]
        if skill == "deepcraft":
            dc_path = Path("/home/ras/.config/opencode/skills/deepcraft")
            if dc_path.exists():
                skill_mounts = [
                    "-v", f"{dc_path}:/root/.config/opencode/skills/deepcraft:ro",
                    "-v", f"{dc_path}:/root/.opencode/skills/deepcraft:ro",
                ]
        else:
            opencode_flags.append("--pure")

        cmd = [
            "docker", "run", "--rm",
            "--network", net,
            "-e", "https_proxy=http://services:8888",
            "-e", "http_proxy=http://services:8888",
            *skill_mounts,
            "-v", f"{code_dir.resolve()}:/work",
            "-w", "/work",
            "bugbench-bench:latest",
            "opencode", "run", *opencode_flags,
            "-m", opencode_model,
            prompt
        ]
    else:
        cmd = [
            "docker", "run", "--rm",
            "--network", net,
            "-v", f"{code_dir.resolve()}:/work",
            "-v", f"{REPO_ROOT / 'docker/bench/agent.py'}:/srv/agent.py:ro",
            "-w", "/work",
            "bugbench-bench:latest",
            "python3", "/srv/agent.py",
            "--model", model,
            "--gateway", "http://services:8000/v1",
            "--prompt", prompt,
            "--max-turns", "10"
        ]

    start_time = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        rc = proc.returncode
        stdout_tail = proc.stdout[-1500:]
        stderr_tail = proc.stderr[-1500:]
    except subprocess.TimeoutExpired:
        rc = -1
        stdout_tail = ""
        stderr_tail = "container timed out"

    latency = round(time.monotonic() - start_time, 1)
    after_source = target_file.read_text(encoding="utf-8", errors="replace") if target_file.exists() else ""
    changed = (after_source != task["before_source"])

    # Grade out-of-band via services container
    grade_res = grade_via_service(task_id, after_source) if changed else {"fixed": False}
    fixed = grade_res.get("fixed", False)

    # Clean up workspace
    shutil.rmtree(workdir, ignore_errors=True)

    return {
        "task_id": task_id,
        "changed": changed,
        "fixed": fixed,
        "latency_s": latency,
        "rc": rc,
        "grade_details": grade_res,
        "stdout": stdout_tail,
        "stderr": stderr_tail,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="space-bunny-alpha")
    ap.add_argument("--agent", choices=["agent", "opencode"], default="agent")
    ap.add_argument("--skill", default="", choices=["", "deepcraft"], help="Skill to invoke in agent/OpenCode")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--task-id", default="")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    tasks = json.loads(HOSTS.read_text())
    sources = {t["task_id"]: t for t in json.loads(SOURCES.read_text())}
    if args.task_id:
        tasks = [t for t in tasks if t["task_id"] == args.task_id]
    elif args.limit > 0:
        tasks = tasks[:args.limit]

    start_services()

    model_clean_name = args.model.replace("/", "_")
    out_file = Path(args.out) if args.out else REPO_ROOT / f"data/results/container-{model_clean_name}.jsonl"
    out_file.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n=== Running Container Campaign: model={args.model}, agent={args.agent}, tasks={len(tasks)} ===")
    rows = []
    fixed_count = 0

    for i, t in enumerate(tasks, 1):
        full_task = {**t, **sources.get(t["task_id"], {})}
        print(f"[{i:2d}/{len(tasks)}] Starting {t['task_id']} ({t['module']})...", flush=True)
        res = run_container_task(full_task, args.model, args.timeout, args.agent, skill=args.skill)
        if res["fixed"]:
            fixed_count += 1

        row = {
            "model": f"container/{args.model}",
            "task_id": t["task_id"],
            "agent": args.agent,
            "changed": res["changed"],
            "fixed": res["fixed"],
            "latency_s": res["latency_s"],
            "returncode": res["rc"],
            "codebase": t.get("codebase", ""),
        }
        rows.append(row)
        print(f"  Result: changed={res['changed']} fixed={res['fixed']} latency={res['latency_s']}s", flush=True)

    # If running a single task, append or merge, otherwise overwrite
    if args.task_id and out_file.exists():
        existing = [json.loads(l) for l in out_file.read_text().splitlines() if l]
        existing = [e for e in existing if e["task_id"] != args.task_id] + rows
        out_file.write_text("\n".join(json.dumps(r) for r in existing) + "\n")
    else:
        out_file.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    print(f"\nCampaign Complete: {fixed_count}/{len(tasks)} fixed ({fixed_count/max(len(tasks),1):.1%}) -> {out_file}")


if __name__ == "__main__":
    main()
