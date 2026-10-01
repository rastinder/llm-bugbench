#!/usr/bin/env bash
# Start the bugbench web service on :8099
set -euo pipefail
cd /home/ras/llm-bugbench
export PYTHONPATH="$PWD/src"
exec python3 -m uvicorn bugbench.app:app --host 127.0.0.1 --port 8099 --log-level warning
