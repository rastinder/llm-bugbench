#!/usr/bin/env python3
"""Execute benchmark on requested cohort inside isolated Docker containers.

Cohort:
1. glm-5.3 (tries glm-5.3, falls back to glm-5.2 on gateway)
2. qwen-3.8
3. northmini-code
4. space-bunny-alpha
5. OpenCode: opencode/mimo-v2.6-flash-free
6. OpenCode: opencode/big-pickle
7. auto
8. gemini-3.8-high
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

RUNS = [
    {"name": "glm-5.3", "model": "glm-5.3", "agent": "agent"},
    {"name": "qwen-3.8", "model": "qwen-3.8", "agent": "agent"},
    {"name": "northmini-code", "model": "northmini-code", "agent": "agent"},
    {"name": "space-bunny-alpha", "model": "space-bunny-alpha", "agent": "agent"},
    {"name": "opencode-mimo-2.6-flash", "model": "opencode/mimo-v2.6-flash-free", "agent": "opencode"},
    {"name": "opencode-big-pickle", "model": "opencode/big-pickle", "agent": "opencode"},
    {"name": "auto", "model": "auto", "agent": "agent"},
    {"name": "gemini-3.8-high", "model": "gemini-3.8-high", "agent": "agent"},
]

def main():
    print("=== Starting Full Benchmark Campaign Suite ===", flush=True)
    for r in RUNS:
        print(f"\n==================================================", flush=True)
        print(f"--> Starting Run: {r['name']} (model={r['model']}, agent={r['agent']})", flush=True)
        print(f"==================================================", flush=True)
        cmd = [
            sys.executable,
            str(REPO_ROOT / "scripts/run_container_campaign.py"),
            "--model", r["model"],
            "--agent", r["agent"],
            "--timeout", "180"
        ]
        t0 = time.time()
        res = subprocess.run(cmd, cwd=REPO_ROOT)
        dur = round(time.time() - t0, 1)
        print(f"--> Completed Run {r['name']} in {dur}s (rc={res.returncode})", flush=True)

if __name__ == "__main__":
    main()
