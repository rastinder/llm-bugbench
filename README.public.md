# bugbench — a custom benchmark of LLM bug-fixing on real production defects

**169 real bugs**, taken from an actual engineer's `before → after` edit history. Not
synthetic exercises — the kind that survive code review, ship to production, and then get
silently patched at 2am.

Every task is a pair of the *same code region* before and after the engineer fixed it. The
model gets the goal and the broken code. The real fix is withheld and used only for grading.

```
python 134 · javascript 22 · typescript 7 · shell 6
118 distinct source files · median snippet 56 tokens
```

## Scores are 0–100%, reported as "N out of M"

```
model                                found        fixed    untouched    SCORE
glm-5.2                     11/20 (55.0%) 10/20 (50.0%)     9/20      56.8%
openrouter-space-bunny-alpha 5/10 (50.0%)  7/10 (70.0%)     2/10      52.6%
kilo-nemotron-3-super       4/10 (40.0%)  5/10 (50.0%)     3/10      48.0%
kilo-nemotron-3-ultra       3/7  (42.9%)  2/7  (28.6%)     2/7       45.0%
kilo-step-3.7-flash         1/10 (10.0%)  2/10 (20.0%)     1/10      15.3%
```

- **found** — named a line the real fix changed *and* the judge agreed on the mechanism
- **fixed** — the blind judge said the defect is actually repaired
- **untouched** — returned the input unchanged (a null answer)
- **SCORE** — 0–100% combined, with a seeded bootstrap 95% CI

`bugbench report` prints this table. `--html` renders it with the full sub-signal breakdown.

## The agentic-lane leak, and why rows get quarantined

The first Antigravity-CLI run scored **81.7%** — the highest number in this project.
It was fake.

`agy` is an agent with file tools. Run with normal permissions from the project directory,
it could `grep` the machine for the file the snippet came from and paste the fixed version.
It did:

```
<project>/zen-proxy/zen_proxy.mjs        task B0107
<project>/llm-scout/scout_server.py      task B0483
<user>/.local/bin/herdr_goals.py         task B0052
```

**9 of 12 answers were byte-identical to the historical fix.** It was reading the answer
out of the repo, not fixing anything.

Two fixes, both tested:

- `AgyRunner` runs in a throwaway empty directory with `--sandbox`. Mandatory, not a flag.
- A fifth screen signal, `file_read_leak` (**critical**): byte-identical answer *and* the
  real source file is readable on disk. Byte-identity alone is memorisation; byte-identity
  with the file on disk is a file read. Those rows are **quarantined** from the headline
  and reported separately — never silently averaged in.

## What the real bugs are

Full catalogue: [`data/bug_catalog.json`](data/bug_catalog.json).

| class | n |
|---|---|
| unhandled failure | 39 |
| other logic defect | 36 |
| missing / null value | 31 |
| validation gap | 21 |
| infinite loop / retry storm | 13 |
| concurrency / resource leak | 9 |
| duplicate / double-processing | 8 |
| wrong ordering / precedence | 5 |
| partial iteration | 5 |
| boundary / truncation | 2 |

A sample of real defects:

| id | file | defect |
|---|---|---|
| `B0404` | `bench_stt.py` | division by zero when `total_words` is zero, crashing `wer_pct` |
| `B0011` | `queue_proxy.py` | kill switch rejects **all** upstream calls, so health checks fail too |
| `B0395` | `opencode_telegram_bot.py` | process-group cleanup misses children, leaving zombies |
| `B0202` | `test_prompt_budget.py` | can't distinguish old-call/old-result from new-call/new-result |

**What solves them:** the smallest correct change — a guard clause, a loop that examines
every match instead of the first, an error branch that actually raises, a bound on a retry,
releasing a handle in a `finally`.

## Scoring

```
STAGE 1  diagnose                STAGE 2  repair
 location_match  .50  ← no LLM    judge_agreement  .50  ← LLM
 judge_mechanism .30  ← LLM       patch_reprod.    .30  ← no LLM
 category_match  .20  ← no LLM    oracle_score     .20  ← execution
             │                               │
           0–100%                          0–100%
             └────── combined = (d + r)/2 ───┘
```

`patch_reproduction` measures resemblance to the developer's historical patch, **not
correctness**, and is labelled that way everywhere. `oracle_score` is 0 on almost every
task: an execution oracle is admitted only if its test *fails on the buggy code and passes
on the reference fix*, and only 1 task in 60 passes that gate.

### Guard rails

| guard | prevents |
|---|---|
| mandatory curation | a feature addition scored as a bug repair |
| reference fix never in a prompt | leakage, enforced by a test that inspects every prompt |
| `is_unchanged()` before the judge | 22 rows returned the input unchanged and the judge still said "fixed" |
| separate judge rubrics per stage | one rubric for patches *and* diagnoses made every model score 0.000 |
| `errors` / `error_rate` column | a rate-limited model looking bad at code when it was unavailable |
| file-read quarantine | an agentic lane copying the answer out of the repo |
| seeded bootstrap CIs | ranking on a point estimate at n=8 |

## Run it

```bash
export PYTHONPATH=$PWD/src
python3 -m bugbench.cli report                      # 0-100% markers + integrity screen
python3 -m bugbench.cli models
python3 -m bugbench.cli run <model> --limit 20 --category bug_fix --append
python3 -m bugbench.cli report --html leaderboard.html
python3 scripts/prepush_sweep.py .                  # never publish a secret
```

Lanes: OpenAI-compatible endpoints, the Antigravity CLI (`agy`), and opencode. 100+ tests.

## Honest limitations

1. **Execution-oracle coverage is ~1 task in 60.** 26 snippets don't compile standalone, 7
   expose no public function, 15 are JS/TS/shell. Lifting this needs whole-repo
   containerised execution.
2. **Contamination is unmeasured.** The `file_read_leak` signal is a strong proxy for
   *active* file reading, not for training-set memorisation. No private holdout exists.
3. **Most OpenRouter free models are unusable** — 11 of 15 returned 10/10 rate-limit
   errors. Their 0.0% is availability, not skill.
