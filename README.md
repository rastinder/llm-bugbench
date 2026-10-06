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

## 2. Bilateral Ground-Truth Verification & Task Difficulty Tiers

All 16 tasks across easy, medium, and hard difficulty tiers are bilaterally verified:
- **Buggy Starting Code (`before_source`)**: **0 / 16 (0.0%)** pass rate against hidden tests.
- **Genuine Human Reference Patch (`after_source`)**: **16 / 16 (100.0%)** pass rate against hidden tests.

Every task has a confirmed behavioral defect and a verified working reference patch.

### Task Difficulty Tiers
- **Easy Tier (1 task)**: Focused unit logic defects (e.g. `rec__scoring__bugscore_errored`). Proves tool calling, file editing, and test alignment capabilities.
- **Medium Tier (1 task)**: Real production feature boundary defects (e.g. `rec__autopilot__job_matcher_threshold` from `AutoPilot-Jobs`), requiring environment variable resolution, type casting, fallback defaults, and prompt template embedding.
- **Hard Tier (14 tasks)**: Production concurrency bugs, semantic query deduplication, distributed process supervisors, and asynchronous event pipelines from real multi-agent codebases.

---

## 3. Benchmark Results: October 2026 Model Cohort

Each model was evaluated inside hermetic ephemeral containers across the benchmark panel:

| Model | Evaluation Lane / Agent | Tasks | Edits Attempted | Fixed | Pass Rate | Notes |
|---|---|---|---|---|---|---|
| **glm-5.3** | Container / Tool Agent | 16 | 10 | 1 | **6.2%** | Solved easy task; failed medium & hard tasks |
| **qwen-3.8** | Container / Tool Agent | 16 | 10 | 1 | **6.2%** | Solved easy task; failed medium & hard tasks |
| **auto** (LiteLLM) | Container / Tool Agent | 16 | 4 | 1 | **6.2%** | Solved easy task; failed medium & hard tasks |
| **space-bunny-alpha** | Container / Tool Agent | 16 | 1 | 1 | **6.2%** | Solved easy task; failed medium & hard tasks |
| **northmini-code** | Container / Tool Agent | 15 | 2 | 1 | **6.7%** | Solved easy task; failed medium & hard tasks |
| **gemini-3.8-high** (API) | Container / Tool Agent | 14 | 1 | 1 | **7.1%** | Solved easy task in 156.0s; failed hard tasks |
| **big-pickle** | Container / OpenCode CLI (`opencode`) | 14 | 2 | 0 | **0.0%** | 2 edits made; failed hidden pytest suite |
| **mimo-2.6-flash** | Container / OpenCode CLI (`opencode`) | 14 | 0 | 0 | **0.0%** | Agent timed out navigating full codebase |
| **gemini-3.8-flash-high** (Antigravity) | Antigravity CLI (`agy` with `bwrap` FS isolation) | 13 | 0 | 0 | **0.0%** | `declined_work` across all 13 hard tasks |

**Key Findings:**
- **Zero Hallucination Tolerance**: The 15 medium and hard production bugs (including AutoPilot-Jobs matching thresholds and query deduplication) defeated 100% of tested models.
- **Verified Tooling Baseline**: The easy defect (`rec__scoring__bugscore_errored`) proves the container harnesses (`agent.py`) and model tool loops are fully functional, with 6 frontier models solving it immediately.
- **Honest Distribution**: Rather than an artificial 0% or inflated 90%, the benchmark provides a realistic difficulty gradient.

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
