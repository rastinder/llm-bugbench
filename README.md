# bugbench — LLM Bug-Fixing Benchmark

An honest, hermetically isolated benchmark evaluating how frontier LLMs and coding agents fix genuine, hard-won software defects mined from real development history.

---

## 1. Hermetic Container Architecture

The benchmark evaluates models inside strictly isolated, ephemeral Docker containers designed to prevent prompt leakage, repository grepping, and benchmark contamination.

```
┌─────────────────────────────────────────────────────────────┐
│ bugbench-bench (Ephemeral Container per Task)               │
│ - Strict internal network (internal: true, no internet)     │
│ - Fresh /work workspace containing ONLY buggy source code   │
│ - All test suites, solutions, and agent memory stripped     │
│ - Tool operations strictly confined to /work                │
│                                                             │
│   Agent (Python Agent / OpenCode CLI)                       │
│     │                           │                           │
│     ▼                           ▼                           │
│  [Port 8000]                 [Port 8888]                    │
└─────┬───────────────────────────┬───────────────────────────┘
      │                           │
┌─────▼───────────────────────────▼───────────────────────────┐
│ bugbench-services (Evaluation & Gateway Container)           │
│ - Port 8000: OpenAI-compatible LLM Gateway                  │
│ - Port 8888: Forwarding HTTP/CONNECT Proxy (OpenCode Cloud) │
│ - Port 5000: Hidden Test Grader & Mock Upstream Services    │
│              (grades modified file against private pytest)   │
└─────────────────────────────────────────────────────────────┘
```

### Isolation & Integrity Guarantees
1. **Zero Internet Egress**: The bench container runs on `bench_internal` (`internal: true`).
2. **Zero Reference Leaks**: Neither reference solutions (`after_source`) nor test suites (`test_source`) are present in the bench container or git repository.
3. **Sterile Workspaces**: `/work` is generated fresh per task and wiped immediately upon completion. Any extraneous `.swarm/`, `.agent.md`, or previous model memory files are scrubbed.
4. **Out-of-Band Grading**: Once the container finishes, the candidate file is sent out-of-band to `http://services:5000/grade` for verification against private tests in `services`.

---

## 2. Bilateral Ground-Truth Verification

All 14 tasks in the benchmark suite are bilaterally verified:
- **Buggy Starting Code (`before_source`)**: **0 / 14 (0.0%)** pass rate against hidden tests.
- **Genuine Human Reference Patch (`after_source`)**: **14 / 14 (100.0%)** pass rate against hidden tests.

Every task has a confirmed behavioral defect and a verified working solution.

---

## 3. Benchmark Results: October 2026 Model Cohort

Each model was evaluated across all 14 verified tasks inside hermetic ephemeral containers:

| Model | Evaluation Lane / Agent | Tasks | Edits Attempted | Fixed | Pass Rate | Avg Latency | Notes |
|---|---|---|---|---|---|---|---|
| **glm-5.3** | Container / Tool Agent | 14 | 9 | 1 | **7.1%** | 9.0s | Solved easy task in 7.8s; failed 13 hard tasks |
| **qwen-3.8** | Container / Tool Agent | 14 | 9 | 1 | **7.1%** | 8.4s | Solved easy task in 7.0s; failed 13 hard tasks |
| **northmini-code** | Container / Tool Agent | 14 | 1 | 1 | **7.1%** | 27.1s | Solved easy task in 17.1s; failed 13 hard tasks |
| **space-bunny-alpha** | Container / Tool Agent | 14 | 1 | 1 | **7.1%** | 57.4s | Solved easy task in 38.5s; failed 13 hard tasks |
| **auto** (LiteLLM) | Container / Tool Agent | 14 | 2 | 1 | **7.1%** | 61.3s | Solved easy task in 28.1s; failed 13 hard tasks |
| **gemini-3.8-high** (API) | Container / Tool Agent | 14 | 1 | 1 | **7.1%** | 13.8s | Solved easy task in 156.0s; failed 13 hard tasks |
| **big-pickle** | Container / OpenCode CLI (`opencode`) | 14 | 2 | 0 | **0.0%** | 177.4s | 2 edits made; failed hidden pytest suite |
| **mimo-2.6-flash** | Container / OpenCode CLI (`opencode`) | 14 | 0 | 0 | **0.0%** | 184.3s | Agent timed out navigating full codebase |
| **gemini-3.8-flash-high** (Antigravity) | Antigravity CLI (`agy` with `bwrap` FS isolation) | 13 | 0 | 0 | **0.0%** | 153.3s | `declined_work` across all 13 hard tasks |

**Key Findings:**
- The 13 production bugs (in asynchronous distributed supervisory processes, ADB event listeners, feedback token validators) defeated 100% of frontier models.
- Adding a localized unit-level defect (`rec__scoring__bugscore_errored`) confirmed that the agent harnesses (`agent.py`) and models have working tool-use and code editing capabilities, with 6 different models successfully solving the easy task.

---

## 4. Running the Benchmark

### Prerequisites
- Docker & Docker Compose
- Python 3.10+

### Start the Services Daemon
```bash
docker compose up -d services
curl http://127.0.0.1:5000/health
```

### Run a Model Campaign
To evaluate a model across the benchmark suite:
```bash
# Minimal Tool Agent (LiteLLM Gateway)
python3 scripts/run_container_campaign.py --model space-bunny-alpha --agent agent

# OpenCode Agent
python3 scripts/run_container_campaign.py --model opencode/big-pickle --agent opencode

# Full Suite Runner
python3 scripts/run_full_benchmark_suite.py
```

Results are streamed and recorded as JSONL files in `data/results/`.

---

## 5. Repository Cleanliness & Anti-Cheating

This repository is maintained with strict anti-cheating hygiene:
- `data/host_tasks.json` contains only task metadata and buggy initial source code.
- Ground-truth fixes and test suites (`host_tasks_sources.json`) and private container data (`docker/services/data/`) are excluded via `.gitignore`.
- All past agent traces, memory logs (`.swarm/`, `PLAN.md`, `ATTEMPTS.md`), and compiled caches are removed so subsequent runs cannot leverage prior agent context.
