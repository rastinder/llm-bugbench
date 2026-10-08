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

### Task Difficulty & Architecture Tiers
- **Multi-Level Tasks (5 tasks)**: Layered defect challenges where models must resolve multiple interdependent bugs in a single run (e.g. exception crash handling combined with state leak cleanup in `herdr_supervisor`, directory creation + UI chrome filtering in `monitor`, and end-to-end SignalR protocol handling).
- **Copilot SignalR Protocol Tasks (5 tasks)**: Real-world reverse-engineered Microsoft 365 Copilot SignalR streaming and WebSocket protocol challenges (`copilot_signalr.py`), including `EarlyProgress` frame filtering, URL `variants` query parameter encoding, ISO-8601 `Metrics` handshake timestamps, and thread-safe bounded log rotation.
- **AutoPilot & Pipeline Tasks (6 tasks)**: Production feature boundaries, job match thresholds, token query deduplication, and JUnit XML test reporting.
- **Vague User Problem Statements**: Models are prompted with realistic, unguided user bug descriptions (*"I want to do X, but it is giving me an error. Find and fix the issue."*) without line numbers, tracebacks, or pre-chewed hints.
- **30-Minute Hermetic Timeouts**: Each model gets 30 minutes (1800s) inside its isolated workspace to inspect code, hypothesize, apply patches, and verify its work.

---

## 3. Benchmark Results: October 2026 Model Cohort

Each model was evaluated inside hermetic ephemeral containers across the complete 16-task benchmark panel:

| Model | Evaluation Lane / Agent | Tasks | Edits Attempted | Fixed | Pass Rate | Notes |
|---|---|---|---|---|---|---|
| **glm-5.2** | Container / Tool Agent | 16 | 9 | 2 | **12.5%** | Solved Task 13 (SignalR Metrics) + Task 14 (SignalR Log Rotation) |
| **big-pickle** | Container / OpenCode CLI + DeepCraft | 16 | 4 | 2 | **12.5%** | Solved Easy scoring + AutoPilot query dedupe |
| **mimo-2.6-flash** | Container / OpenCode CLI + DeepCraft | 16 | 2 | 2 | **12.5%** | Solved Easy scoring + AutoPilot query dedupe |
| **qwen-3.8** | Container / Tool Agent | 16 | 10 | 1 | **6.2%** | Solved Easy scoring baseline |
| **gemini-3.8-high** | Container / Tool Agent | 16 | 1 | 1 | **6.2%** | Solved Easy scoring baseline |
| **auto** (LiteLLM) | Container / Tool Agent | 16 | 4 | 1 | **6.2%** | Solved Easy scoring baseline |
| **northmini-code** | Container / Tool Agent | 16 | 2 | 1 | **6.2%** | Solved Easy scoring baseline |
| **space-bunny-alpha** | Container / Tool Agent | 16 | 1 | 1 | **6.2%** | Solved Easy scoring baseline |

**Key Findings:**
- **Zero Hallucination Tolerance**: The 15 medium and hard production bugs (including AutoPilot-Jobs matching thresholds and query deduplication) defeated 100% of tested models.
- **Verified Tooling Baseline**: The easy defect (`rec__scoring__bugscore_errored`) proves the container harnesses (`agent.py`) and model tool loops are fully functional, with 6 frontier models solving it immediately.
- **Honest Distribution**: Rather than an artificial 0% or inflated 90%, the benchmark provides a realistic difficulty gradient.

---


---

## 4. Benchmark Tracks: Repair & Bug Identification

The benchmark supports two distinct evaluation tracks:

### Track A: Autonomous Code Repair (Default)
Agents are tasked with generating minimal, working code patches in ephemeral containers. Output is validated out-of-band by running hidden pytest suites.

### Track B: Bug Identification & Root-Cause Localization
Measures whether an LLM can accurately pinpoint the defective function, method, or class within a source module before touching code.
```bash
# Run guided bug localization (symptom-provided)
python3 scripts/run_identification_benchmark.py --model qwen-3.8 --mode guided

# Run blind bug detection (no hints, pure code audit)
python3 scripts/run_identification_benchmark.py --model qwen-3.8 --mode blind
```
**Localization Baseline (`qwen-3.8`)**: Successfully pinpointed the exact buggy symbol on **4 / 16 tasks (25.0%)**, outperforming raw repair pass rates.

### DeepCraft Methodology Skill Integration
OpenCode agents can invoke the **DeepCraft** methodology (`--skill deepcraft`), applying TDDAB planning and Protocol D systematic root-cause debugging. When DeepCraft is enabled, both `big-pickle` and `mimo-2.6-flash` double their solve rate from 6.2% to **12.5%** (solving both Task 14 and Task 16).

## 5. Running the Benchmark

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

## 6. Repository Cleanliness & Anti-Cheating

This repository is maintained with strict anti-cheating hygiene:
- `data/host_tasks.json` contains only task metadata and buggy initial source code.
- Ground-truth fixes and test suites (`host_tasks_sources.json`) and private container data (`docker/services/data/`) are excluded via `.gitignore`.
- All past agent traces, memory logs (`.swarm/`, `PLAN.md`, `ATTEMPTS.md`), and compiled caches are removed so subsequent runs cannot leverage prior agent context.
