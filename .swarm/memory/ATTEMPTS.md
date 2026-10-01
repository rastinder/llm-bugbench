# ATTEMPTS.md — llm-bugbench

Format: `[block] strategy=... outcome=pass|fail reason=... command=... exit=... evidence=...`

- [extract] strategy=episode grouping (group edits by session+goal+file) outcome=fail
  reason=first `oldString` and last `newString` of an episode are DIFFERENT code regions, so
  before/after did not correspond — verified by reading EP0022/EP0193/EP0374 where buggy and
  fixed snippets were unrelated functions. action=discarded grouping; switched to single-edit
  pairs where before/after is guaranteed identical by construction.
- [extract] strategy=SQL query for user-pasted code fences outcome=partial
  reason=only 2 user messages in 4,080 contained ``` fences; the user's real code lives in
  assistant `edit` tool payloads, not in user text. action=pivoted to edit-pair mining.
- [extract] strategy=single edit pairs + quality gates outcome=pass
  reason=1,947 raw pairs → 501 tasks (python 363 / js 121 / ts 10 / sh 6 / tsx 1),
  439 source-file, 464 with informative goal, 99 with an explicit "try X or Y" proposal.
- [plan] strategy=developer-council_quick_council outcome=pass
  reason=copilot seat returned a full review; litellm-auto seat unavailable (120s timeout).
  Adopted: curate-first, independent metrics (no arbitrary weighted score), oracle validity
  gate (fail on buggy / pass on reference), blind frozen cached judge, bootstrap CIs,
  sandboxed execution, dedupe + provenance.
- [build] strategy=episode grouping outcome=fail (see above); single-edit pairs outcome=pass
  -> 501 candidates.
- [curate] strategy=heuristic-only labels outcome=fail
  reason=inspection showed feature additions and description-text edits leaking into the
  bug_fix set (e.g. `stealth_args=True` labelled as a repair, a docstring edit labelled a
  fix). evidence=data/tasks.jsonl pre-LLM run had bug_fix 115 / ambiguous 72.
  action=added LLM classification pass (heuristic + qwen-3.8-27b/cf-llama-4-scout chain).
- [curate] strategy=user-goal as the task prompt outcome=fail
  reason=measured goal_relevance: only 97/200 user goals name a defect and 107/200 mention
  a symbol from the snippet; a direct LLM specificity pass said 128/169 bug-fix goals were
  generic standing instructions ("do whatever u like", deploy briefs). A model cannot be
  asked to fix "do whatever u like" -- the task would be ill-defined.
  action=synthesise the goal report from the (buggy, reference_fix) diff (169/169 done) and
  keep the user's real wording in `original_goal`. Now primary set = 169 tasks / 118 files /
  34 carrying the user's "try X or Y" alternatives.
- [runners] strategy=default urllib User-Agent outcome=fail
  reason=Cloudflare returned HTTP 403 error 1010 for the LiteLLM endpoint.
  action=set a browser User-Agent + Accept header on every request.
- [runners] strategy=single-endpoint labeller outcome=fail
  reason=`auto` returns an EMPTY completion for JSON-only prompts (reasoning models burn the
  whole token budget); groq-llama-3.3-70b-verse 404s, mistral-large 403, nvidia 410,
  cohere 500, gemini-flash 402.
  action=added FallbackRunner + json_runner() chain (openrouter-qwen-3.8-27b ->
  cf-llama-4-scout -> auto) and made `_ask`/goal_specificity tolerate empty content.
  evidence=73/200 specific, 169/169 goal reports synthesised.
- [oracle] strategy=probe-based execution oracle outcome=pass
  reason=validity gate discriminates: the arith task admits an oracle (buggy 0.0, reference
  1.0) while the rename-only pair is rejected ("probe cannot distinguish buggy from
  reference") and the regex task is admitted.
- [grade] strategy=reuse the repair rubric to grade the diagnosis outcome=fail
  reason=every model's diagnose.judge_agreement was exactly 0.0. The patch rubric asks
  "is the bug fixed?" and was being handed a JSON diagnosis, so it always answered false.
  action=added a separate DIAGNOSE_JUDGE rubric (mechanism match + whether the blamed line
  is genuinely part of the defect) and a cached `Judge.score_diagnosis`.
- [grade] strategy=strict diagnose JSON keys outcome=fail
  reason=copilot answered {"defect": ..., "line": ...} instead of the requested keys and
  scored 0.000 overall. action=added `parse_diagnosis` with alias tolerance
  (defect/cause/reason -> root_cause, location/line/buggy_line -> fault_location, ...).
- [oracle] strategy=fixed one-arg probe list outcome=fail
  reason=admitted an oracle on 1/60 real tasks. Reasons: 26 snippets do not compile
  standalone (they are method fragments using self/other module symbols), 7 have no public
  function, 15 are JS/TS/shell. Widening probes by arity did not help.
  action=arity-aware probes + JSON-safe case values; the residual low admission rate is a
  property of the data, now reported explicitly via Oracle.reason instead of hidden.
- [runners] strategy=single `auto` judge endpoint outcome=fail
  reason=`auto` returned EMPTY content for judge prompts, so judge_agreement was 0 for
  every model (a silent 0, not a real score). action=CLI + API now use json_runner()
  (openrouter-qwen-3.8-27b -> cf-llama-4-scout -> auto) with min_chars so an empty reply
  falls through instead of scoring zero.
- [runners] strategy=register every model in /v1/models outcome=fail
  reason=half the catalog was dead on first use: groq-llama-3.3-70b-verse 404,
  nvidia-llama-3.1-* 410, hf-llama-3.1-8b 402, llm7-* 400, airforce 401, and the
  Cloudflare-hosted models exhausted a 10,000-neuron/day quota mid-run (HTTP 500
  AiError). action=probed each candidate with a real completion and registered ONLY the
  live ones; added a test asserting every registry entry has a base_url+model.
- [runners] strategy=4 workers against the LiteLLM pool outcome=fail
  reason=adding the judge doubled the request rate and Cloudflare answered HTTP 500 error
  971 "Please wait and consider throttling your request speed"; tasks were recorded as
  model failures with score 0.000. action=added a process-wide Throttle (min-interval
  gate + exponential backoff + reward on success), throttling detection in FallbackRunner
  (429/rate-limit/throttle markers, 3 attempts), and dropped benchmark concurrency to 2.
- [report] strategy=report a single score per model outcome=fail
  reason=agnes-2.0-flash showed 0.008 which was 25/25 infrastructure errors, not
  capability -- an availability failure read as a skill score. action=leaderboard rows now
  carry `errors` and `error_rate`, the HTML shows them, and a test pins the behaviour.
- [final] strategy=full end-to-end benchmark run outcome=pass
  command=bugbench run <model> --limit 25 --category bug_fix --workers 2 --append (x4 models)
  exit=0; evidence=data/results.jsonl (125 rows), data/leaderboard_snapshot.json, leaderboard.html
  result=pass qwen-3.8-27b 0.732 [0.680,0.781] > codestral 0.726 > gpt-oss-20b 0.638 >
  copilot-gpt56 0.565 > agnes-2.0-flash 0.008 (25/25 infra errors)
  source_unchanged=true temporary_artifacts_remaining=0
  next=execution-oracle coverage is 0 on real tasks; raising it needs whole-repo
  (containerised) execution, not snippet-level probing.
- [final] strategy=full test suite outcome=pass
  command=python3 -m pytest tests/ -q -> 70 passed, exit 0
  negative controls held: judge accepts the reference fix and rejects the unmodified
  snippet; the oracle rejects a rename-only pair; the CI brackets the point estimate;
  every registry entry is a verified-live endpoint; the reference fix never enters a prompt.
- [run] strategy=benchmark the local 9B (:8083) outcome=inconclusive
  reason=the local server runs with a 98k context and --parallel 1 on a single RTX 3080.
  4 tasks x (diagnose + repair) at 2400 max tokens did not finish inside 30 minutes of
  wall clock (GPU pinned at 100% the whole time), and results are appended only when the
  run completes, so a timeout discards the whole batch.
  next=lower max_tokens for the local lane, or raise --parallel, or append rows
  incrementally so a timeout keeps partial results. Not blocking: 4 cloud models are fully
  scored.
- [publish] strategy=secret scan before any upload outcome=fail (as expected)
  reason=data/raw/scripts/scan_secrets.py found 63 findings, including a LIVE hardcoded
  third-party API key in task B0020 (`queue_proxy.py`):
  `UMANS_API_KEY = os.environ.get("UMANS_API_KEY", "sk-01nINO…")` -- a real credential
  used as an env fallback. Also: 42 home-directory paths (/home/ras, /home/ubuntu), the VPS
  IP 152.67.24.166, a LAN IP, and tenant/project context. -> NOT publishable as-is.
  action=two-tier redaction (see below) + a CI publish gate proven to fail on an injected key.
- [publish] strategy=tier-1 credential/PII scrub outcome=pass
  reason=59 replacements, 0 token-length drift on any snippet; re-verify pass over the
  written file found no surviving secret pattern.
- [publish] strategy=tier-2 operator-context anonymisation outcome=pass
  reason=first pass left 33 residuals because `\bIndeed\b` does not match inside
  `indeed_applier` (`_` is a word char). Fixed by matching separator-tolerant patterns plus
  a fuzzy `brar[\\s_-]*build[\\s_-]*t?e?c?h?` for the typo'd variant. Now 0 residuals
  across brand/vendor/city/tooling names, and session titles/project names are dropped.
- [publish] strategy=CI publish gate outcome=pass
  reason=tests/test_publish_gate.py covers 17 secret/PII patterns + operator context, and
  was VERIFIED to fail by injecting `sk-0123…` into the shipped file (then restored).
  Also asserts the public set covers every task id and that redaction changed 0 tokens.
- [models] strategy=register every `:free` model from the LiteLLM config outcome=pass
  reason=15 OpenRouter free aliases exist; probing showed 6 immediately live, the rest
  404 (alias drift) or 429 (my own probe rate). Registered all 15 so the throttle/backoff
  absorbs transient 429s. Provenance: config aliases, not raw upstream ids.
