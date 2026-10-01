"""Model runners: one uniform interface over every endpoint this machine has.

`kind`:
  openai   -- any OpenAI-compatible /chat/completions endpoint
  opencode -- the opencode CLI (its own agent harness, tool-capable)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_TIMEOUT = 180


class ModelError(RuntimeError):
    pass


@dataclass
class ModelSpec:
    name: str
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    kind: str = "openai"
    temperature: float = 0.0
    max_tokens: int = 1400
    timeout: int = DEFAULT_TIMEOUT
    extra_headers: dict = field(default_factory=dict)
    effort: str = ""
    notes: str = ""


@dataclass
class Result:
    text: str
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""
    text_source: str = "content"
    reasoning_chars: int = 0

    @property
    def ok(self) -> bool:
        return not self.error


class OpenAIChatRunner:
    def __init__(self, spec: ModelSpec):
        self.spec = spec

    def complete(self, prompt: str, system: str = "") -> Result:
        return self.complete_messages(
            [{"role": "system", "content": system},
             {"role": "user", "content": prompt}] if system
            else [{"role": "user", "content": prompt}])

    def complete_messages(self, messages: list[dict]) -> Result:
        url = self.spec.base_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url = url + "/chat/completions"
        body = {
            "model": self.spec.model or self.spec.name,
            "messages": messages,
            "temperature": self.spec.temperature,
            "max_tokens": self.spec.max_tokens,
            "stream": False,
        }
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", DEFAULT_UA)
        req.add_header("Accept", "application/json")
        if self.spec.api_key:
            req.add_header("Authorization", f"Bearer {self.spec.api_key}")
        for k, v in self.spec.extra_headers.items():
            req.add_header(k, v)
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.spec.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            raise ModelError(f"HTTP {e.code}: {body}") from e
        except Exception as e:
            raise ModelError(f"{type(e).__name__}: {e}") from e
        dt = int((time.time() - t0) * 1000)
        if status != 200:
            raise ModelError(f"HTTP {status}")
        try:
            d = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ModelError(f"non-JSON HTTP {status}: {e}") from e
        choices = d.get("choices") or []
        if not choices:
            raise ModelError(f"no choices in response: {raw[:200]}")
        msg = choices[0].get("message") or {}
        text = msg.get("content")
        if text is None:
            text = choices[0].get("text") or ""
        # reasoning models (gpt-oss, space-bunny-alpha, ...) can put their whole answer
        # in `reasoning_content` and return an EMPTY `content`. Dropping it made those
        # models look like they produced nothing at all.
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        if not (text or "").strip() and (reasoning or "").strip():
            text = reasoning
            out_of = "reasoning_content"
        else:
            out_of = "content"
        u = d.get("usage") or {}
        return Result(text=text, latency_ms=dt, text_source=out_of,
                      reasoning_chars=len(reasoning or ""),
                      prompt_tokens=int(u.get("prompt_tokens", 0) or 0),
                      completion_tokens=int(u.get("completion_tokens", 0) or 0))


CANARY_PATH = os.environ.get(
    "BUGBENCH_AGY_CANARY",
    str(Path.home() / "zen-proxy/zen_proxy.mjs"))


def canary_leaked(out: str) -> bool:
    """Did this reply reproduce the canary file's real CONTENT?

    The marker is the file's content, never its path. Matching the path string produced
    a false positive that blocked the whole agentic lane: a correctly isolated model
    answers "I cannot read <canary path>", and merely SAYING the path was scored as a
    successful leak.
    """
    low = (out or "").lower()
    return ("#!" in (out or "")
            or "zen-proxy v2" in low        # line 2 of the real file
            or "anthropic-messages in" in low
            or "hy4 preview" in low)


class AgyRunner:
    """Antigravity CLI (`agy`) runner -- an agentic lane with tool use.

    Distinct from the plain chat lanes: the model can read files and run commands, so
    prompts that hand it a repo would behave very differently from the snippet-only
    benchmark. Registered as kind="agy".
    """

    def __init__(self, spec: ModelSpec):
        self.spec = spec
        self.bin = shutil.which("agy") or str(Path.home() / ".local/bin/agy")

    _isolation_lock = threading.Lock()

    @classmethod
    def _isolation_path(cls) -> Path:
        return Path(os.environ.get(
            "BUGBENCH_AGY_ISOLATION_CACHE",
            Path.home() / ".cache/bugbench-agy-isolation.json"))

    @classmethod
    def _isolation_store(cls) -> dict:
        """Disk-backed canary verdicts, keyed by wrapper prefix.

        Filesystem isolation is a property of the WRAPPER, not of the model, so the
        canary is paid once and reused by every model in every later process. Before
        this it was per-process and per-model: 14 lanes each paid minutes for a slow
        model, and any timeout was silently read as "not isolated" -- which is exactly
        how the whole agentic fleet ended up blocked.
        """
        try:
            return json.loads(cls._isolation_path().read_text())
        except Exception:
            return {}

    @classmethod
    def _isolation_save(cls, store: dict) -> None:
        p = cls._isolation_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(store))
        except Exception:
            pass

    def _isolation_holds(self, prefix: str) -> bool:
        """Canary read: ask the agent for a real source file's first line.

        If it can read the file, the lane is NOT isolated and must not be scored.
        """
        with AgyRunner._isolation_lock:
            store = self._isolation_store()
            if prefix in store:
                return bool(store[prefix])
            verdict = self._probe_isolation(prefix)
            store[prefix] = verdict
            self._isolation_save(store)
            return verdict

    def _probe_isolation(self, prefix: str) -> bool:
        import shlex
        import tempfile as _tf
        with _tf.TemporaryDirectory(prefix="bugbench-agy-probe-") as td:
            cmd = shlex.split(prefix) + [self.bin, "-p",
                                          f"Quote line 1 of {CANARY_PATH} exactly.",
                                          "--model", self.spec.model,
                                          "--dangerously-skip-permissions", "--sandbox",
                                          "--add-dir", td]
            if self.spec.effort:
                cmd += ["--effort", self.spec.effort]
            try:
                # Generous on purpose: a high-effort turn legitimately takes minutes.
                # The old hardcoded 180s read every slow model as a successful LEAK.
                budget = int(os.environ.get("BUGBENCH_AGY_CANARY_TIMEOUT",
                                             str(max(600, self.spec.timeout * 2))))
                p = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=budget, cwd=td)
            except Exception:
                return False
            out = (p.stdout or "")
            # FAIL CLOSED. Isolation counts as proven only if the wrapper actually ran a
            # model turn AND that turn did not reproduce the canary file. An empty reply,
            # a crash, or a wrapper that is really a no-op all count as NOT isolated.
            ran_a_turn = p.returncode == 0 and len(out.strip()) > 0
            return bool(ran_a_turn and not canary_leaked(out))

    def complete(self, prompt: str, system: str = "") -> Result:
        full = (system + "\n\n" + prompt) if system else prompt
        # --sandbox IS NOT ENOUGH. Measured, not assumed:
        #   agy -p "Read ~/zen-proxy/zen_proxy.mjs ..." --sandbox
        # still returns the file contents. --sandbox restricts TERMINAL commands only,
        # and --add-dir does not restrict reads either. There is no CLI flag to remove the
        # file tool. So on a normal filesystem this lane can read the answer straight out of
        # the repo, which it did: 9 of 12 answers were byte-identical to the reference fix.
        #
        # Therefore this lane REFUSES to run unless a real filesystem-isolating wrapper is
        # configured via BUGBENCH_AGY_ISOLATE (bwrap / docker / firejail / systemd-run
        # with a private root). A pre-flight canary read verifies the isolation actually
        # holds before any scored task is sent.
        prefix = os.environ.get("BUGBENCH_AGY_ISOLATE", "").strip()
        if not prefix:
            return Result(text="", error=(
                "agy lane disabled: no filesystem isolation. The Antigravity CLI can read "
                "the task's real source file even with --sandbox, so it would copy the "
                "reference fix instead of writing one. Set BUGBENCH_AGY_ISOLATE to a "
                "wrapper command (bwrap/docker/firejail) to enable this lane."))
        if not self._isolation_holds(prefix):
            return Result(text="", error=(
                "agy lane disabled: the configured isolation did NOT hold a canary read, "
                "so the lane can still see the real source files"))

        # SANDBOX ON TOP OF ISOLATION.
        # The benchmark hands the agent a snippet and asks it to fix it. Run with normal
        # permissions from the project directory, `agy` could simply grep the machine for
        # the real file and paste the fixed version. Measured on the first agy run:
        # 8 of 11 answers were BYTE-IDENTICAL to the historical fix, because the agent
        # read the real source file for several tasks straight off disk (e.g. the user's
        # own zen-proxy/ and llm-scout/ projects) and copied the answer out of the repo.
        # That is not a capability score.
        # So: a throwaway empty working directory, plus --sandbox.
        import shlex
        import tempfile
        with tempfile.TemporaryDirectory(prefix="bugbench-agy-") as td:
            # shlex, not str.split: a wrapper path containing spaces was previously
            # shredded into separate argv entries and the lane failed to start.
            base = shlex.split(prefix) + [self.bin, "-p", full, "--model", self.spec.model,
                                          "--dangerously-skip-permissions", "--sandbox",
                                          "--add-dir", td]
            t0 = time.time()

            def _run(extra):
                return subprocess.run(base + extra, capture_output=True, text=True,
                                      timeout=self.spec.timeout, cwd=td)

            try:
                if self.spec.effort:
                    p = _run(["--effort", self.spec.effort])
                    # A model can be renamed/reconfigured upstream and start rejecting an
                    # effort level it used to accept, which silently costs a whole lane
                    # (`invalid model selection`). Drop the flag and retry once rather
                    # than writing off every task.
                    if p.returncode != 0 and "effort is not supported" in (p.stderr or ""):
                        p = _run([])
                else:
                    p = _run([])
            except subprocess.TimeoutExpired:
                return Result(text="", latency_ms=int((time.time() - t0) * 1000),
                              error=f"timeout after {self.spec.timeout}s")
        dt = int((time.time() - t0) * 1000)
        if p.returncode != 0:
            return Result(text="", latency_ms=dt,
                          error=f"exit {p.returncode}: {(p.stderr or '')[:200]}")
        return Result(text=(p.stdout or "").strip(), latency_ms=dt)

    def complete_messages(self, messages: list[dict]) -> Result:
        system = ""
        user = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            else:
                user.append(m["content"])
        return self.complete("\n\n".join(user), system)


class OpenCodeRunner:
    """Runs `opencode run` as a subprocess (agentic, tool-capable model lane)."""

    def __init__(self, spec: ModelSpec):
        self.spec = spec
        self.bin = shutil.which("opencode") or "opencode"

    _isolation_cache = None

    def _isolation_holds(self, prefix: str) -> bool:
        """Canary read: ask the agent for a real source file's first line.

        If it can read the file, the lane is NOT isolated and must not be scored. Cached
        per prefix so this costs one call, not one per task.
        """
        if AgyRunner._isolation_cache is None:
            AgyRunner._isolation_cache = {}
        if prefix in AgyRunner._isolation_cache:
            return AgyRunner._isolation_cache[prefix]
        import shlex
        import tempfile as _tf
        with _tf.TemporaryDirectory(prefix="bugbench-agy-probe-") as td:
            cmd = shlex.split(prefix) + [self.bin, "-p",
                                    f"Quote line 1 of {CANARY_PATH} exactly.",
                                    "--model", self.spec.model,
                                    "--dangerously-skip-permissions", "--sandbox",
                                    "--add-dir", td]
            try:
                p = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=180, cwd=td)
            except Exception:
                AgyRunner._isolation_cache[prefix] = False
                return False
            out = (p.stdout or "")
            # FAIL CLOSED. Isolation counts as proven only if the wrapper actually ran a
            # model turn AND that turn did not reproduce the canary file. An empty reply,
            # a crash, or a wrapper that is really a no-op all count as NOT isolated.
            ran_a_turn = p.returncode == 0 and len(out.strip()) > 0
            leaked = canary_leaked(out)
            AgyRunner._isolation_cache[prefix] = bool(ran_a_turn and not leaked)
        return AgyRunner._isolation_cache[prefix]

    def complete(self, prompt: str, system: str = "") -> Result:
        full = (system + "\n\n" + prompt) if system else prompt
        cmd = [self.bin, "run", "--dir", os.environ.get("BUGBENCH_DIR", "/tmp"),
               "--model", self.spec.model, full]
        t0 = time.time()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=self.spec.timeout)
        except subprocess.TimeoutExpired:
            return Result(text="", latency_ms=int((time.time() - t0) * 1000),
                          error=f"timeout after {self.spec.timeout}s")
        dt = int((time.time() - t0) * 1000)
        if p.returncode != 0:
            return Result(text="", latency_ms=dt,
                          error=f"exit {p.returncode}: {(p.stderr or '')[:200]}")
        return Result(text=p.stdout or "", latency_ms=dt)


#: lane kinds that can touch the host filesystem. A plain HTTP chat lane cannot, so a
#: byte-identical answer from one is capability (or memorisation), never a file leak.
FS_CAPABLE_KINDS = ("agy", "opencode", "cli")


def lane_has_filesystem(model_name: str) -> bool:
    """Can this model's lane read files off this host?

    Used to gate the file-read-leak detector. Without this check the detector fired on
    plain API models that had no filesystem access at all, and quarantined a healthy
    model over a single byte-identical answer.

    An UNREGISTERED lane name conservatively returns True: we cannot prove such a lane
    lacks filesystem access, and absence of proof is not proof of safety.
    """
    try:
        return get(model_name).kind in FS_CAPABLE_KINDS
    except Exception:
        return True


def runner_for(spec: ModelSpec):

    if spec.kind == "agy":
        return AgyRunner(spec)
    if spec.kind == "opencode":
        return OpenCodeRunner(spec)
    return OpenAIChatRunner(spec)


# ---------------------------------------------------------------- registry
DEFAULT_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

LITELLM_BASE = "https://aitshirts.in/litellm/v1"
LITELLM_KEY = os.environ.get("LITELLM_KEY", "sk-litellm-vps-2026")

# Only models verified LIVE against /v1/models + a real completion are registered
# (2026-09-29). The pool changes daily: groq-llama-3.3-70b-verse 404s, nvidia-llama-3.1
# 410s, hf-llama-3.1-8b 402s, the Cloudflare-hosted models exhaust a 10k-neuron/day quota.
FREE = [
    ("kilo-nemotron-3-ultra", "nvidia/nemotron-3-ultra-550b-a55b:free"),
    ("kilo-nemotron-3-super", "nvidia/nemotron-3-super-120b-a12b:free"),
    ("kilo-inkling", "thinkingmachines/inkling:free"),
    ("kilo-step-3.7-flash", "stepfun/step-3.7-flash:free"),
    ("openrouter-nex-n2-5-pro", "nex-agi/nex-n2.5-pro:free"),
    ("openrouter-gemma-4-31b-it", "google/gemma-4-31b-it:free"),
    ("openrouter-laguna-xs-2-1", "poolside/laguna-xs-2.1:free"),
    ("openrouter-north-mini-code", "cohere/north-mini-code:free"),
    ("openrouter-qwen3-8-27b", "qwen/qwen3.8-27b:free"),
    ("openrouter-ling-3-0-flash-vl", "inclusionai/ling-3.0-flash-vl:free"),
    ("openrouter-nex-n2-5-mini", "nex-agi/nex-n2.5-mini:free"),
    ("openrouter-nemotron-3-5-lightning", "nvidia/nemotron-3.5-lightning:free"),
    ("openrouter-nemotron-3-nano-omni", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"),
    ("openrouter-gemma-4-26b", "google/gemma-4-26b-a4b-it:free"),
    ("openrouter-lfm-2-5-2-6b", "liquid/lfm-2.5-2.6b:free"),
    # alpha / experimental
    ("openrouter-space-bunny-alpha", "stealth/space-bunny-alpha"),
]

# Antigravity CLI lanes (`agy`), verified live 2026-09-29 via `agy models` (14 available).
# `effort` is EMPTY for the Claude lanes on purpose: they are registered as
# "(Thinking)" variants and reject `--effort` outright
# (`--effort is not supported for model "claude-opus-4-6-thinking"`), which was the
# cause of 40 `invalid model selection` failures. The Gemini/gpt-oss names already
# encode their own effort level.
AGY = [
    ("agy-gemini-3.8-flash-high", "gemini-3.8-flash-high", "high"),
    ("agy-gemini-3.8-flash-medium", "gemini-3.8-flash-medium", "medium"),
    ("agy-gemini-3.8-flash-low", "gemini-3.8-flash-low", "low"),
    ("agy-gemini-3.7-flash-high", "gemini-3.7-flash-high", "high"),
    ("agy-gemini-3.7-flash-medium", "gemini-3.7-flash-medium", "medium"),
    ("agy-gemini-3.7-flash-low", "gemini-3.7-flash-low", "low"),
    ("agy-gemini-3.6-flash-high", "gemini-3.6-flash-high", "high"),
    ("agy-gemini-3.6-flash-medium", "gemini-3.6-flash-medium", "medium"),
    ("agy-gemini-3.6-flash-low", "gemini-3.6-flash-low", "low"),
    ("agy-gemini-3.1-pro-high", "gemini-3.1-pro-high", "high"),
    ("agy-gemini-3.1-pro-low", "gemini-3.1-pro-low", "low"),
    ("agy-claude-opus-4.6-thinking", "claude-opus-4-6-thinking", ""),
    ("agy-claude-sonnet-4.6", "claude-sonnet-4-6", ""),
    ("agy-gpt-oss-120b-medium", "gpt-oss-120b-medium", "medium"),
]

# GLM family (verified live 2026-09-29; the nvidia-* GLM 5.x entries are retired/410)
GLM = [
    ("glm-5.2", "openrouter-glm-5.2"),
    ("glm-5.3-flash", "openrouter-glm-5.3-flash"),
]

REGISTRY: list[ModelSpec] = [
    ModelSpec(name="local-mimo-9b", base_url="http://127.0.0.1:8083/v1",
              api_key="local", model="mimo-v26-9b-mtp", max_tokens=2400,
              timeout=600, notes="local llama-server; run with --workers 1"),
    ModelSpec(name="qwen-3.8-27b", base_url=LITELLM_BASE, api_key=LITELLM_KEY,
              model="openrouter-qwen-3.8-27b", max_tokens=2400,
              notes="OpenRouter Qwen3.8 27B"),
    ModelSpec(name="litellm-auto", base_url=LITELLM_BASE, api_key=LITELLM_KEY,
              model="auto", max_tokens=2400, notes="smart failover collection"),
    ModelSpec(name="codestral", base_url=LITELLM_BASE, api_key=LITELLM_KEY,
              model="mistral-codestral-latest", max_tokens=2400,
              notes="Mistral Codestral"),
    ModelSpec(name="gpt-oss-20b-ollama", base_url=LITELLM_BASE, api_key=LITELLM_KEY,
              model="ollama-gpt-oss-20b", max_tokens=2400,
              notes="gpt-oss-20b via ollama upstream"),
    ModelSpec(name="agnes-2.0-flash", base_url=LITELLM_BASE, api_key=LITELLM_KEY,
              model="agnes-2.0-flash", max_tokens=2400, notes="fast small model"),
] + [
    ModelSpec(name=n, base_url=LITELLM_BASE, api_key=LITELLM_KEY, model=m,
              max_tokens=2400, timeout=180, notes=f"GLM -- {m}")
    for n, m in GLM
] + [
] + [
    ModelSpec(name=n, kind="agy", model=m, effort=e, max_tokens=2400, timeout=300,
              notes=f"Antigravity CLI -- {m}")
    for n, m, e in AGY
] + [
    ModelSpec(name="copilot-gpt56", base_url="http://127.0.0.1:8789/v1",
              api_key="sk-copilot-tool-layer", model="copilot-gpt", max_tokens=2400,
              notes="M365 Copilot via local tool layer"),
] + [
    # OpenRouter free tier (:free). These are rate-limited, so run them with the
    # throttle active and read the `errors` column -- a 429 is availability, not skill.
    ModelSpec(name=n, base_url=LITELLM_BASE, api_key=LITELLM_KEY, model=n,
              max_tokens=2400, timeout=120, notes=f"OpenRouter free -- {d}")
    for n, d in FREE
]


def registry() -> list[dict]:
    return [{"name": s.name, "kind": s.kind, "model": s.model,
             "base_url": s.base_url, "notes": s.notes} for s in REGISTRY]


def get(name: str) -> ModelSpec:
    for s in REGISTRY:
        if s.name == name:
            return s
    raise ModelError(f"unknown model {name!r}; known: {[s.name for s in REGISTRY]}")


class Throttle:
    """Process-wide rate gate + exponential backoff.

    The LiteLLM pool sits behind Cloudflare, which answers HTTP 500 with error code 971
    ("throttling your request speed") when concurrency is too high. Adding a judge doubles
    the request count, so the runner must pace itself rather than record the throttle as a
    model failure.
    """

    def __init__(self, min_interval: float = 0.35, max_backoff: float = 20.0):
        self.min_interval = min_interval
        self.max_backoff = max_backoff
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                delay = self._next - now
            else:
                delay = 0.0
            self._next = max(now, self._next) + self.min_interval
        if delay:
            time.sleep(delay)

    def penalise(self) -> float:
        with self._lock:
            self.min_interval = min(self.max_backoff, self.min_interval * 2)
            return self.min_interval

    def reward(self) -> None:
        with self._lock:
            self.min_interval = max(0.1, self.min_interval / 1.5)


GLOBAL_THROTTLE = Throttle()
THROTTLED_MARKERS = ("throttling your request speed", "rate limit", "429",
                     "too many requests", "slow down")


class FallbackRunner:
    """Tries several endpoints until one returns usable content.

    Needed because the pool is a live failover mix: some upstreams return HTTP 402/410/404
    or an EMPTY completion (reasoning models that burn the whole budget). A single-shot
    runner would silently record "no answer" as a model failure.
    """

    def __init__(self, specs: list[ModelSpec], min_chars: int = 1,
                 throttle: Throttle | None = None):
        self.specs = specs
        self.min_chars = min_chars
        self.throttle = throttle if throttle is not None else GLOBAL_THROTTLE
        self.tried: list[str] = []

    @staticmethod
    def _label(spec) -> str:
        if isinstance(spec, ModelSpec):
            return spec.model or spec.name
        inner = getattr(spec, "spec", None)
        if isinstance(inner, ModelSpec):
            return inner.model or inner.name
        return getattr(spec, "name", None) or type(spec).__name__

    def complete_messages(self, messages: list[dict]) -> Result:
        self.tried = []
        last: Result | None = None
        for attempt in range(3):
            for spec in self.specs:
                self.tried.append(self._label(spec))
                engine = (spec if hasattr(spec, "complete_messages")
                          else OpenAIChatRunner(spec))
                self.throttle.wait()
                try:
                    r = engine.complete_messages(messages)
                except Exception as e:
                    last = Result(text="", error=f"{type(e).__name__}: {e}")
                    self.throttle.penalise()
                    continue
                if self._throttled(r):
                    last = r
                    self.throttle.penalise()
                    continue
                if r.ok and len((r.text or "").strip()) >= self.min_chars:
                    self.throttle.reward()
                    return r
                last = r
            if last is not None and self._throttled(last):
                time.sleep(min(self.throttle.max_backoff,
                               max(1.0, self.throttle.min_interval * 4)))
        return last or Result(text="", error="no endpoints configured")

    @staticmethod
    def _throttled(r: Result) -> bool:
        blob = f"{r.error or ''} {r.text or ''}".lower()
        return any(m in blob for m in THROTTLED_MARKERS)


def json_runner(models: list[str] | None = None,
                min_chars: int = 2) -> FallbackRunner:
    """A JSON-capable fallback chain, ordered by observed reliability on this box."""
    order = models or ["openrouter-qwen-3.8-27b", "mistral-codestral-latest", "auto"]
    specs = []
    for m in order:
        try:
            s = get(m)
        except ModelError:
            s = ModelSpec(name="__lb__", base_url=LITELLM_BASE, api_key=LITELLM_KEY,
                          model=m, max_tokens=600, timeout=120)
        specs.append(s)
    return FallbackRunner(specs, min_chars=min_chars)
