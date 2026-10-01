#!/usr/bin/env bash
# Bring every model to exactly 20 tasks (TARGET below), running ONLY the tasks each model
# has not attempted yet, so no tokens are re-spent. Models that are rate-limited to death
# still get their attempt and are marked as such by the existing errors column.
set -u
cd /home/ras/llm-bugbench
export PYTHONPATH=/home/ras/llm-bugbench/src
TARGET=${TARGET:-20}
RES=data/results.jsonl

needs() {
  python3 - "$1" "$TARGET" <<'PY'
import json, sys, collections
model, target = sys.argv[1], int(sys.argv[2])
seen = set()
for line in open("data/results.jsonl"):
    line = line.strip()
    if not line:
        continue
    try:
        r = json.loads(line)
    except json.JSONDecodeError:
        continue
    if r.get("model") == model:
        seen.add(r.get("task_id"))
print(max(0, target - len(seen)))
PY
}

# The Antigravity CLI lanes are DISABLED here on purpose. Measured: `agy --sandbox` does
# not stop file reads (there is no flag to remove the file tool), so the agent could copy
# the reference fix out of the operator's real repos -- and did, 9 of 12 answers
# byte-identical. The runner now refuses the lane unless BUGBENCH_AGY_ISOLATE is set AND a
# canary read proves the isolation holds. agy auth cannot currently be bound without
# exposing the filesystem, so there is no working isolation to point it at.
# To enable it:
#   export BUGBENCH_AGY_ISOLATE=/home/ras/llm-bugbench/scripts/agy-isolate.sh
# then uncomment the block below.
for m in agy-claude-opus-4.6-thinking agy-claude-sonnet-4.6 agy-gpt-oss-120b-medium \
         agy-gemini-3.8-flash-high; do
  n=$(needs "$m"); [ "$n" = "0" ] && { echo "=== $m SKIP ==="; continue; }
  echo "=== $m (needs $n) -- expect DISABLED without isolation ==="
  timeout 300 python3 -m bugbench.cli run "$m" --limit "$TARGET" --category bug_fix \
      --workers 1 --shuffle --seed 7 --topup --append 2>&1 | tail -1
done

# the free tier that actually produced results
for m in kilo-nemotron-3-super kilo-nemotron-3-ultra kilo-step-3.7-flash \
         openrouter-space-bunny-alpha; do
  n=$(needs "$m"); [ "$n" = "0" ] && { echo "=== $m SKIP ==="; continue; }
  echo "=== $m (needs $n) ==="
  timeout 3000 python3 -m bugbench.cli run "$m" --limit "$TARGET" --category bug_fix \
      --workers 1 --shuffle --seed 7 --topup --append 2>&1 | tail -1
done

# the rest of the free tier: mostly rate-limited, but they get a fair attempt
for m in openrouter-north-mini-code openrouter-lfm-2-5-2-6b kilo-inkling \
         openrouter-gemma-4-31b-it openrouter-laguna-xs-2-1 openrouter-qwen3-8-27b \
         openrouter-nex-n2-5-pro openrouter-nex-n2-5-mini openrouter-gemma-4-26b \
         openrouter-ling-3-0-flash-vl openrouter-nemotron-3-nano-omni \
         openrouter-nemotron-3-5-lightning; do
  n=$(needs "$m"); [ "$n" = "0" ] && { echo "=== $m SKIP ==="; continue; }
  echo "=== $m (needs $n) ==="
  timeout 1800 python3 -m bugbench.cli run "$m" --limit "$TARGET" --category bug_fix \
      --workers 1 --shuffle --seed 7 --topup --append 2>&1 | tail -1
done
echo TOPUPDONE
