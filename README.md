# bugbench — LLM bug-fixing benchmark

Measures how well LLMs **(1) identify** and **(2) fix** real bugs, using bug-fix
episodes mined from this machine's own `opencode` history.

## Where the tasks come from

`~/.local/share/opencode/opencode.db` holds 4,764 `edit` tool calls. An `edit` call's
`oldString` → `newString` is an **exact contiguous-region replacement**, so each pair is a
guaranteed before/after of the *same* code region. The task goal is the nearest preceding
user message in that session — the developer's own words, including the "try X or Y"
alternatives (`goal_had_alternatives` flags those).

Pipeline: `data/raw/scripts/`

| step | script | result |
|---|---|---|
| dump user text + sessions | `dump_all.py` | 4,080 user messages, 849 sessions |
| mine edit pairs | `extract_pairs.py` | 1,947 (buggy, fixed) pairs |
| quality gates | `build_tasks.py` | 501 candidates |
| curate + dedupe + spread | `bugbench build` | task set with `category` labels |

## How it is scored

Two stages, scored **separately** (no single weighted number can hide a regression):

1. **diagnose** — model sees goal + buggy code, returns JSON
   `{root_cause, fault_location, bug_category}`. Score = `0.5*location_match`
   (deterministic: the quoted line must be one the real fix changed) + `0.3*judge_agreement`
   + `0.2*category_match`.
2. **repair** — model returns the corrected snippet. Score = `0.5*judge_agreement` +
   `0.3*patch_reproduction` + `0.2*oracle_score` (oracle only where validated).

`combined = 0.5*diagnose + 0.5*repair` (frozen; no calibration data yet).

### Guard rails

- **Curation is mandatory.** Every task is labelled `bug_fix | feature_addition | refactor |
  documentation | configuration | ambiguous` by a deterministic heuristic, optionally refined
  by an LLM. Only `bug_fix` reaches the primary leaderboard.
- **The reference fix is never shown to any model** — asserted by a test that inspects
  every prompt.
- **Oracle validity gate.** An execution oracle is admitted only if its test *fails on the
  buggy snippet and passes on the reference fix*. A check that cannot tell them apart is
  discarded. All execution happens in an isolated subprocess (fresh temp cwd, no proxy,
  10 s timeout, memory cap) — nothing is `eval`'d in-process.
- **Blind judge.** The judge prompt never names the candidate model, uses a frozen rubric,
  is told to ignore formatting/renames/comments, and is cached by content hash. A
  reference-aware pass is a separate opt-in column.
- **Uncertainty.** Leaderboards carry seeded bootstrap 95% CIs and per-category
  breakdowns. `patch_reproduction` is labelled as *patch resemblance*, not correctness.

## Use

```bash
export PYTHONPATH=$PWD/src

python3 -m bugbench.cli build --llm litellm-auto   # curate the dataset
python3 -m bugbench.cli list --category bug_fix    # inspect tasks
python3 -m bugbench.cli models                     # available models

python3 -m bugbench.cli run litellm-auto --limit 20 --category bug_fix --append
python3 -m bugbench.cli run groq-llama-3.3-70b --limit 20 --append
python3 -m bugbench.cli report --html /tmp/bugbench.html

./run-server.sh          # http://127.0.0.1:8099
```

API: `GET /health`, `GET /api/tasks`, `GET /api/models`, `GET /api/leaderboard`,
`POST /api/run {"model","limit","category","stages"}`, `GET /` (HTML leaderboard).

## Tests

```bash
python3 -m pytest tests/ -q
```

Includes negative controls that keep the grader honest: the judge must accept the
reference fix and reject the unmodified snippet; the oracle must reject a rename-only
pair; the report's CI must bracket the point estimate.

## Current leaderboard (25 tasks, 2026-09-29)

| model | combined | 95% CI | diagnose | repair | errors |
|---|---|---|---|---|---|
| qwen-3.8-27b (OpenRouter) | **0.732** | 0.680 – 0.781 | 0.732 | 0.731 | 0/25 |
| codestral (Mistral) | 0.726 | 0.676 – 0.778 | 0.718 | 0.734 | 0/25 |
| gpt-oss-20b (ollama upstream) | 0.638 | 0.529 – 0.753 | 0.552 | 0.725 | 0/25 |
| copilot-gpt56 (M365) | 0.565 | 0.529 – 0.604 | 0.294 | 0.837 | 0/25 |
| agnes-2.0-flash | 0.008 | 0.000 – 0.024 | 0.016 | 0.000 | **25/25** |

Read the `errors` column: agnes-2.0-flash scored ~0 because every one of its requests hit a
120 s upstream cooldown, not because it cannot fix bugs. Its row measures availability.

Note the two stages disagree about the winner — copilot-gpt56 is the **best repairer**
(0.837) but among the **weakest diagnosticians** (0.294), which is exactly the kind of split
a single blended score would have hidden.

## Known limitations (measured, not assumed)

1. **Execution-oracle coverage is 0 on the real task set.** Probing 60 real tasks admitted an
   oracle on 1: 26 snippets do not compile standalone (they are method fragments using
   `self`/module symbols), 7 expose no public function, 15 are JS/TS/shell. Repair scores are
   therefore judge + patch-reproduction only. `Oracle.reason` records why for every task, and
   `oracle_coverage` is on the report so the two subsets can never be silently averaged.
2. **No task is "solved" end-to-end yet** (`solved = 0` for every model). These are hard,
   real production bugs; partial credit is doing all the discriminating work.
3. **The pool is volatile.** The LiteLLM catalog changes daily; the registry only lists
   endpoints verified live with a real completion.
4. **Contamination is unmeasured.** These snippets come from one developer's private history
   and could in principle appear in a model's training data. There is no private holdout yet.
