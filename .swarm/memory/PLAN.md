# PLAN — execution-backed benchmark (replaces the judge/snippet plan)

**Goal (the user's actual need):** "benchmark new models in my VPS, on my personal
projects, so I can know which works best for my needs." This is a *personal selection
instrument*, not a general model leaderboard. Every design choice below follows from
that sentence.

**Supersedes:** the snippet-level judge plan (same file, backed up alongside). That plan
is preserved for the historical 552 rows, which remain valid *as archive data only*.

---

## Council record (required gate)

| Attempt | Scope | Terminal state |
|---|---|---|
| 1 | 3 seats, collaborative | DEADLOCKED — quorum 3 families **none**; 3 analyses (.84 copilot / .79 qwen / .72 glm); **no synthesis** |
| 2 | 2 seats, narrowed | **MCP error -32001** (timeout) → recorded *skipped* per gate |
| 3 | 2 seats, narrowed | DEADLOCKED — quorum 2, only `gpt` answered; plan produced; dissent preserved |

**No attempt ever reached quorum.** Findings below are therefore *advisory*, not
consensus. Each is either user-decided or empirically verified on this machine.
Preserved dissent is carried in ATTEMPTS.md, not averaged away.

---

## Verified empirically (not assumptions)

Each was tested by running it, not by reading docs.

1. **No `--tb` value is leak-free.** pytest 9.1.1: `long`/`short`/`line` all print the
   hidden test's source; `--tb=no` still leaks the assertion message through the
   short-test-summary line. `--show-capture=no` does not help.
2. **Feedback from `--junit-xml` node IDs only IS leak-free.** Build feedback from
   `classname::name`, never expose pytest's stdout. Probes for test source, message,
   assertion expression and path all came back clean while the raw stdout stayed
   server-side.
3. **Python `sys.addaudithook` denies model-code reads** of the test path and is
   non-removable: grader reads/parses tests *first*, installs the hook, then model code
   gets `PermissionError`. VERIFIED BLOCKED.
4. **Node v22.22.1 `--permission` + `--allow-fs-read` denies the same read.**
   VERIFIED `ERR_ACCESS_DENIED`.
5. **`__pycache__` silently leaks the entire hidden test source.** Default umask writes
   `test/__pycache__/*.pyc` as `-rw-rw-r--` and the hidden constant is recoverable as
   plain text from the `.pyc`. This bypasses *all three* isolation layers on every run.
   Fixed by `PYTHONDONTWRITEBYTECODE=1` (verified: no file written) or `umask 077`
   (verified: `-rw-------`).
6. **Contamination is measured, not assumed:** `marketplace-monitor` is **public**
   (pushed 2026-09-27); `AutoPilot-Jobs` is **private** (404 unauthenticated).
7. **Git history is nearly absent:** only `AutoPilot-Jobs` (88 commits, fix-shaped) and
   `marketplace-monitor` (1 commit) have any. The other 8 codebases have **no git at
   all**, so DB replay remains the primary source for them.
8. **AutoPilot-Jobs already ships graded executable oracles**
   (`b9b1b62 test(eval): score the years path against 1,152 real questions`) and
   multi-bug fix commits (`f9ddd74 test: three defects the evidence-grounded merge
   exposed`).
9. GLM resolves without guessing: `openai/z-ai/glm-5.3` via
   `https://integrate.api.nvidia.com/v1` (alias `nvidia-glm-5-3`), and `z-ai/glm-5.2`
   via OpenRouter (`openrouter-glm-5.2`).
10. Environment: pytest 9.1.1, Node v22.22.1, Docker 29.1.3 / Compose 2.40.3.

---

## Frozen design (user-approved)

- **25 hidden test files**: 20 Python, 5 JavaScript. 1–5 independently-scored bugs each
  => **25–125 scored items**, partial credit.
- **Per-bug primary scoring**; file-clear rate as a pre-registered concordance check. If
  the ranking flips between the two rules, report both and declare the decision
  **scoring-sensitive**.
- **All failures for a file shown at once**, node-ID only.
- **Pass A** = blind one-shot, all 25. **Pass B** = ≤3 retries, only on Pass-A failures,
  with the same frozen feedback. `iterations_to_green` comparable only within the
  Pass-A failure subset.
- **150 reliability runs** = 3 buggy-baseline + 3 fixed-oracle per file. Determinism
  validation, not model scoring.
- **No perturbed/torture variants** (explicitly rejected). Mutation testing is used
  **only as a grader-calibration instrument**, never as panel content.
- Two containers: `bench` (model; sees only code) / `services` (grader + copilot-dummy +
  litellm-dummy + queue-dummy). `test/` mounted **only** in services.
- Node pinned, `--test-concurrency=1`, fixed test order.
- No infra/policy/refusal failure carries a numeric score.

### Estimand — fixed panel

The 10 codebases **are** the population, not a sample. This dissolves the clustering
critique for the stated purpose, at the cost of never being publishable as a general
model ranking. Stated plainly in every report.

---

## Statistics (pre-registered before any model runs)

- **Primary:** paired permutation test on per-bug score differences, permuting **within
  bug** (not Wilcoxon — partial credit produces heavy ties that signed-rank handles
  badly). Holm correction over the 10 pairwise comparisons. Cliff's δ alongside.
- **Sensitivity:** cluster bootstrap over codebases, reported side by side. Pre-registered
  disagreement rule: trust the paired result, report the discrepancy.
- **TOST** against a pre-registered smallest-effect-of-interest δ₀, so that
  **TIED** (gap bounded below δ₀) is distinct from **INCONCLUSIVE** (could not bound it).
  Calling an underpowered result "TIED" would manufacture confidence.
- **Weighting:** equal per codebase, files-per-codebase capped, per-codebase breakdown
  always labelled — so "what outperformed in which case" is answerable.
- **Survivorship:** report completion rate per model **and** an intention-to-treat
  sensitivity with refusal scored 0, so declining work cannot look like success.

---

## Pre-flight gates — all hard, none skippable

1. **Contamination / memorization.** Audit public exposure of every task repo, probe
   models for verbatim recall of the fixed code, and keep memorization canaries
   **distinct** from the cheat-detection honey tokens. Public-exposure tasks are
   memorization probes only, never headline scoring. *(Campaign-sinker: it corrupts
   every score silently and no downstream gate detects it.)*
2. **Model-version pinning.** Record exact provider model ID per run. Interleaved order
   handles thermal drift; it does nothing about a provider silently updating a model
   mid-campaign.
3. **Noise floor.** Identical (model, file) repeats must produce identical scores.
   Pooled across a stratified duplicate subset (~25 spare runs) — **not** per-cell,
   because 150 − 125 unique cells leaves only ~20% repeats.
4. **Oracle validation.** Known-good patches earn full credit per bug; garbage patches
   score ≈0. Proves partial credit discriminates and is monotonic. Calibration
   instrument: mutation-testing mutants, whose ground truth is known by construction.
5. **Task admission (per bug).** Buggy fails → fixed passes → reverted fails, with an
   explicit `bug_id → test_node` map. Plus **no NEW unexpected non-target failures**
   against a pre-registered allowlist — *not* strict behavioural identity, since a
   correct fix may legitimately change other behaviour.
6. **Reconstruction admission (fail-closed).** Chronological replay; each `oldString`
   must match **exactly once**; record hunk-order hash + per-file sha256. Ambiguous or
   underdetermined ⇒ **quarantine, never hand-patch**.
7. **Leakage.** Audit hook / `--permission` + guest uid, no sudo, no egress, no env/secret
   exposure, tmpfs workdirs, `PYTHONDONTWRITEBYTECODE=1`, artifacts isolated,
   honey-token canaries. Per-run CPU/time/memory caps so one model's infinite loop cannot
   eat the shared budget.
8. **Randomized/interleaved run order** so machine load and thermal drift cannot
   masquerade as model identity.

---

## MEASURED: DB replay cannot supply the panel (2026-10-01)

The pilot kill-switch fired. Numbers, not opinion:

| Stage | Count |
|---|---|
| Distinct `filePath`s in the corpus | 4,234 |
| Inside the 10 target repos | 428 (AutoPilot-Jobs 264, marketplace-monitor 39, llm-bugbench 42, …) |
| Target-repo files with ≥2 recorded hunks | 163 (all still on disk; **0 diskless**) |
| Replay clean under strict fail-closed rules | **1** (99.4% quarantined) |
| Replay clean under a *permissive prefix replay* | **24 files / 45 states**, median depth **1 state** |

Why strict replay fails, measured on the newest hunk of each of the 163 files:

| Cause | Count | Meaning |
|---|---|---|
| Edit later overwritten (newString present, oldString gone) | 116 (71%) | chain intact but superseded — **not** corruption |
| Newest hunk matches, chain breaks further back | 24 | recoverable only one link deep |
| oldString and newString both absent | 13 (8%) | genuine drift |
| Newest hunk is a whole-file `write` (no oldString) | 10 | unverifiable by construction |

**Conclusion: the opencode DB cannot reconstruct 25 three-state tasks.** Median yield is
one state per file, so almost nothing yields both a "before" and an "after". The earlier
"57 diskless tasks" figure was wrong in the useful direction: **zero** of the 163
multi-hunk target files are diskless — they are all still on disk, and the failure is
superseded-edit churn, not missing files. A prefix replay recovers 24 files but cannot
produce buggy/fixed/reverted triples.

This does **not** invalidate the benchmark design; it removes one task source. The
surviving sources, in order of measured quality:

1. **AutoPilot-Jobs git history** — 88 commits, private, fix-shaped messages, and it
   already ships graded executable oracles (`b9b1b62 test(eval): 1,152 real questions`).
   A fix commit *is* a (buggy, fixed, tests) triple with no replay risk.
2. **Mutation testing** (`mutmut`/Stryker) — mutants have a ground-truth oracle by
   construction. Shallower than real bugs, so used as a **calibration instrument** for
   the oracle-validation gate, and as panel *supplement*, never as the headline measure.
3. **Fresh authored tasks** against these codebases with tests written to the current
   state — unlimited supply, zero reconstruction risk, but authored rather than organic.

## MEASURED: task supply is the binding constraint (2026-10-01)

Both candidate sources were measured, not assumed. **Neither can supply 25 tasks.**

### Source 1 — opencode DB replay: 1 of 163

| Stage | Count |
|---|---|
| Distinct `filePath`s in corpus | 4,234 (428 inside the 10 target repos) |
| Target-repo files with ≥2 recorded hunks | 163 (**0 diskless** — all still on disk) |
| Clean under strict fail-closed replay | **1** (99.4% quarantined) |
| Clean under permissive prefix replay | 24 files / 45 states, **median depth 1** |

Cause on the newest hunk of each file: 116 (71%) *edit later overwritten* (newString
present, oldString gone — chain intact but superseded, not corruption), 24 match at the
head but break further back, 13 (8%) genuine drift, 10 whole-file writes.

Median yield of **one state per file** is fatal: almost nothing yields both a "before" and
an "after", so buggy/fixed/reverted triples do not exist in the corpus.

### Source 2 — AutoPilot-Jobs git: 1 of 42

The miner works correctly (`gitmine.py`, 18 tests). Mining is sound; the **tasks are not
hermetic**:

| Outcome | Count |
|---|---|
| ADMISSIBLE (oracle fails buggy, passes fixed) | **1** |
| Blocked: oracle imports an undeclared/external dep | 34 |
| Rejected: no test shipped with the fix (no oracle) | 5 |
| Rejected: test-only commit (no source change) | 2 |

Blocking dependencies, by frequency: `playwright` (14), `field_extractor` (5),
`indeed_applier_playwright` (2), `shared` (2), `resume_forge` (2), `rag_resume_extractor`,
`capability_evidence`, `llm_router`. These tests drive real browsers and LLM routers, so
they cannot run in a hermetic two-container benchmark — which is the correct design, not a
workaround to route around.

Four real bugs were found and fixed while measuring this (each would have silently cost
tasks):
1. `_git(..., text=True)` crashed on binary assets → 35/42 commits lost to a blanket
   "export error". Now reads bytes and skips binary blobs.
2. Exporting only the commit's *touched* files → oracles could not import sibling
   packages. Now exports the whole non-test tree.
3. pytest writes a well-formed **empty** junit report when it collects nothing, which is
   indistinguishable from "all passed". Now an explicit `error` row — a vanished test
   suite can never score as a clean fix.
4. The export root `code/` shadowed the stdlib `code` module (imported by `pdb` during
   pytest start-up) → `INTERNALERROR`. Root is no longer a package; the `sys.path` shim
   moved to the pytest rootdir, where pytest actually loads it.

### Consequence for the panel

The blocker is **not** engineering effort; it is that this corpus is browser/LLM-driven
production code whose oracles are integration tests. Panel supply must therefore come from
**hermetic** material:

1. **Mutation testing** (`mutmut`) over the pure-Python modules — ground truth by
   construction, no external deps. This is now a *panel source*, not just a calibration
   instrument, because the organic sources are exhausted.
2. **Curated hermetic subset** of the git history — the 1 admissible triple plus any
   commit whose oracle imports only stdlib + already-available deps.
3. **Authored tasks** against hermetic modules, with tests written to the current tree.

`marketplace-monitor` (39 candidate files, public) stays a memorization probe only.
Equal per-codebase weighting is retained and now essential, since supply is uneven.

## Panel composition (user chose Option A)

`AutoPilot-Jobs` is the **backbone** — private, 88 fix commits, and it already ships
graded executable oracles — with per-codebase quotas filling the other 9 so that
"what outperformed in which case" is answerable. Contamination risk lowest this way.
Rejected: excluding AutoPilot-Jobs (8/10 repos have no git, so most tasks would depend on
unproven replay), and scoring `marketplace-monitor` despite its public exposure.

Reconstruction and feedback-sufficiency are **piloted on ~5 tasks first** — the two
cheapest kill-switches on the two largest construction risks — before `panel25.json` is
frozen. Any change after freeze = benchmark v2 + full re-run.

---

## Cohort (today's snapshot)

`openrouter-space-bunny-alpha` · `agy-gemini-3.8-flash-high` · `agy-claude-sonnet-4.6` ·
`agy-claude-opus-4.6-thinking` · GLM via **nvidia `openai/z-ai/glm-5.3`**
(`openrouter-glm-5.2` as cross-check). AGY is present deliberately as the user's base
reference point. Budget is not a constraint. A cron job the user already built handles
future model churn, so version pinning here is record-only.

---

## Known, accepted, and not acted on

- **A live GitHub PAT is embedded in plaintext** in
  `Desktop/AutoPilot-Jobs/.git/config`'s remote URL. Verified live (HTTP 200). The user
  was shown the finding and **chose not to rotate it**. Recorded here so it is not lost;
  no further action.
- 57 candidate tasks have no surviving file on disk and depend entirely on DB replay.
- JS test titles (`returns 401 when token expired`) leak more semantics than pytest
  `classname::name`, so the two languages are **not** on an identical feedback regime.
  Minimise JS titles and document the asymmetry rather than pretend it is equal.

---

## Execution order

1. Write the design in (this file) — done.
2. Pilot reconstruction on ~5 diskless tasks + feedback sufficiency with one reference
   model. Report back before committing.
3. Freeze `tasks/panel25.json`; run all eight pre-flight gates.
4. Score the cohort; report per-codebase, per-language, and pass/fail-both rules.
