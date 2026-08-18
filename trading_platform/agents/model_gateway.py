from __future__ import annotations

"""Local Gemma-compatible model gateway.

Supports runtimes: stub (tests), ollama, llama_cpp, vllm.
Never receives broker credentials. Outputs structured JSON only.

RAG integration
---------------
An optional RAGRetriever is injected at construction time.  On every
generate() call the retriever searches the VectorMemoryStore for
documents relevant to the user_prompt and:
  1. Prepends a compact evidence block to the user prompt.
  2. Returns the evidence doc_ids in the response under "evidence_ids"
     so the caller can attach them to the AgentVote / trace.
"""

import json
import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trading_platform.agents.vector_memory import RAGRetriever

logger = logging.getLogger(__name__)

# Deterministic stub responses keyed by model name + prompt hash hint
def _concurrency_wait_cap(default: float = 45.0) -> float:
    """Upper bound on the queue wait for an LLM concurrency slot (seconds)."""
    try:
        return max(0.25, float(os.getenv("LOCAL_LLM_CONCURRENCY_WAIT_MAX_S", default)))
    except (TypeError, ValueError):
        return default


# Schema every specialist verdict must satisfy. Kept permissive on `reasoning`
# length so the model is constrained in SHAPE without being pushed into
# truncation; brevity is requested in the prompt text instead.
_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
    },
    "required": ["action", "confidence", "reasoning"],
}


# Batched multi-instrument replies use a different SHAPE ({"results":[...]}),
# so they must not be validated against _VERDICT_SCHEMA — doing so would reject
# every batch. Callers pass this via generate(response_schema=...).
_BATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
                    "confidence": {"type": "number"},
                    "reasoning": {"type": "string"},
                },
                "required": ["symbol", "action", "confidence", "reasoning"],
            },
        }
    },
    "required": ["results"],
}


_STUB_RESPONSES: dict[str, dict] = {
    "gemma4-31b": {
        "action": "HOLD",
        "confidence": 0.55,
        "reasoning": "Stub: insufficient live data for analysis.",
        "evidence_ids": [],
        "failure_mode": None,
    },
    "gemma4-26b-moe": {
        "action": "HOLD",
        "confidence": 0.50,
        "reasoning": "Stub: coordinator model unavailable.",
        "evidence_ids": [],
        "failure_mode": None,
    },
    "gemma4-e4b": {
        "action": "HOLD",
        "confidence": 0.45,
        "reasoning": "Stub: fast micro-agent default.",
        "evidence_ids": [],
        "failure_mode": None,
    },
    "gemma4-e2b": {
        "action": "HOLD",
        "confidence": 0.45,
        "reasoning": "Stub: fast micro-agent default.",
        "evidence_ids": [],
        "failure_mode": None,
    },
}


class LocalModelGateway:
    """Routes inference requests to a local Gemma-compatible runtime.

    Safety rules enforced here:
    - No broker credentials may enter the prompt.
    - Only structured JSON responses are accepted.
    - Timeouts are enforced; failures return a safe stub.
    """

    _SECRET_KEYS = frozenset({"api_key", "secret", "password", "token", "pin", "totp", "credential"})

    def __init__(
        self,
        runtime: str = "stub",
        base_url: str = "http://localhost:11434",
        timeout: int = 15,
        max_tokens: int = 2048,
        rag_retriever: RAGRetriever | None = None,
        primary_model: str = "gemma4-31b",
        fast_model: str = "gemma4-e4b",
        coordinator_model: str = "gemma4-26b-moe",
        max_concurrent_calls: int = 2,
        embedding_model: str = "text-embedding-nomic-embed-text-v1.5",
        sentiment_model: str = "llama-3-8b-instruct-finance-rag",
    ) -> None:
        self.runtime = runtime
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_tokens = max_tokens
        self._available = runtime == "stub"  # stubs always available
        self._rag: RAGRetriever | None = rag_retriever
        # Real model IDs specialists.py dispatches to. Default to the placeholder
        # names so stub mode and existing tests are unaffected; a live runtime
        # gets these from Settings.local_llm_*_model instead.
        self.primary_model = primary_model
        self.fast_model = fast_model
        self.coordinator_model = coordinator_model
        self.embedding_model = embedding_model
        self.sentiment_model = sentiment_model
        # Global cap on concurrent in-flight HTTP calls to the local runtime —
        # independent of AGENT_SCAN_CONCURRENCY, which only bounds the outer
        # per-underlying pipeline count, not LLM-call concurrency. One local
        # inference server cannot usefully serve hundreds of simultaneous
        # generate() calls (2026-07-28 Ollama thundering-herd incident — see
        # .env "Local model gateway" comment). threading, not asyncio:
        # generate() runs synchronously inside OS threads spawned by
        # AgentCouncilSupervisor's ThreadPoolExecutor.
        self.max_concurrent_calls = max(1, int(max_concurrent_calls))
        self._concurrency_sem = threading.BoundedSemaphore(self.max_concurrent_calls)
        # Bounded wait for a slot, scaled to the per-call timeout so a queued
        # call never eats more than a small share of its own budget waiting —
        # callers that don't get a slot in this window fail fast to the same
        # stub path as any other failure, rather than queuing indefinitely.
        # How long an agent waits for a concurrency SLOT before giving up and
        # returning a canned stub vote.
        #
        # This was `min(3.0, timeout * 0.2)` — hard-capped at 3s regardless of
        # LOCAL_LLM_TIMEOUT_SECONDS. The council fans out ~10 specialist agents
        # against `max_concurrent_calls` slots, and a single local call takes
        # 3-5s, so agents beyond the first few need ~5s+ just to reach the front
        # of the queue. They gave up at 3s and silently degraded to stubs:
        # measured 7 of 10 votes stubbed, with the council still reporting
        # status "real". Raising LOCAL_LLM_TIMEOUT_SECONDS did NOT help because
        # the min(3.0, ...) cap bound first — a genuinely confusing failure,
        # since the knob that looked responsible wasn't.
        #
        # Scale the wait with the expected queue depth instead. Safe here because
        # the council runs on the 5-minute scan cycle, never in a tick-latency
        # path (REDESIGN §8.1); the per-call `timeout` still bounds a hung call.
        # Observed-behaviour counters. ai_capabilities previously derived the
        # council's status purely from CONFIG strings (gateway=lm_studio =>
        # "real"), so it reported a healthy council while 7 of 10 votes were
        # canned stubs — precisely the "advisory != safety / keep the DEGRADED
        # report truthful" rule CLAUDE.md sets. Status must reflect what the
        # gateway ACTUALLY did, not what it was configured to do.
        self._calls_total = 0
        self._calls_stubbed = 0
        self._failure_modes: dict[str, int] = {}
        self._counter_lock = threading.Lock()
        self._concurrency_wait_s = max(
            0.25,
            min(
                _concurrency_wait_cap(),
                self.timeout * 0.5,
            ),
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        if self.runtime == "stub":
            return True
        try:
            return self._health_check()
        except Exception:
            return False

    def status(self) -> dict:
        """Return gateway status dict for the health/status API."""
        available = self.is_available()
        rag_status: dict = {"enabled": False}
        if self._rag is not None:
            rag_status = {
                "enabled": True,
                "doc_count": self._rag.store.count(),
                "categories": self._rag.store.all_categories(),
            }
        with self._counter_lock:
            total, stubbed = self._calls_total, self._calls_stubbed
            modes = dict(self._failure_modes)
        stub_ratio = (stubbed / total) if total else 0.0
        return {
            "runtime": self.runtime,
            "base_url": self.base_url if self.runtime != "stub" else None,
            "available": available,
            "fallback_active": self.runtime != "stub" and not available,
            "max_concurrent_calls": self.max_concurrent_calls,
            # Measured, not configured — see the counters' comment in __init__.
            "calls_total": total,
            "calls_stubbed": stubbed,
            "stub_ratio": round(stub_ratio, 3),
            "failure_modes": modes,
            "rag": rag_status,
            "models": {
                "primary": self.primary_model,
                "coordinator": self.coordinator_model,
                "fast": self.fast_model,
            },
            "note": (
                "Stub mode — deterministic safe responses" if self.runtime == "stub"
                else "Live inference active" if available
                else f"Runtime '{self.runtime}' unreachable — using safe stub fallback"
            ),
        }

    @property
    def rag_retriever(self) -> RAGRetriever | None:
        return self._rag

    def embed(self, text: str) -> list[float] | None:
        """Return a real embedding vector for text via the local runtime's
        /v1/embeddings, or None if unavailable — stub runtime, saturated
        concurrency, unreachable server, or any parse failure. Callers (e.g.
        VectorMemoryStore.set_embedder) must treat None as "fall back to
        keyword matching", never as an error to propagate.

        Shares self._concurrency_sem with generate() — embeddings and
        generations compete for the same LM Studio capacity, so they must
        share one budget rather than each getting their own.
        """
        if self.runtime == "stub":
            return None
        if not self._concurrency_sem.acquire(timeout=self._concurrency_wait_s):
            return None
        try:
            import urllib.request
            payload = json.dumps({"model": self.embedding_model, "input": text}).encode()
            req = urllib.request.Request(
                f"{self.base_url}/v1/embeddings",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
            return data["data"][0]["embedding"]
        except Exception as exc:
            logger.debug("LocalModelGateway: embed() failed: %s", exc)
            return None
        finally:
            self._concurrency_sem.release()

    def score_sentiment(self, headline: str, summary: str) -> float | None:
        """Score financial-news sentiment in [-1, 1] via the local runtime, or
        None if unavailable — stub runtime, saturated concurrency, unreachable
        server, or any parse failure. Callers (NewsIntelligence) must treat
        None as "fall back to the lexicon scorer", never as an error to
        propagate — same contract as embed().

        Defaults to a plain instruct model (self.sentiment_model), not a
        "thinking"/reasoning one: a reasoning model spends its token budget on
        reasoning_content before the answer (confirmed this session with
        qwen3.6-35b-a3b — finish_reason="length" with empty content), a bad
        fit for a short, frequent, structured classification call.

        Shares self._concurrency_sem with generate()/embed() — one local
        runtime, one capacity budget.
        """
        if self.runtime == "stub":
            return None
        if not self._concurrency_sem.acquire(timeout=self._concurrency_wait_s):
            return None
        try:
            system = (
                "You are a financial news sentiment classifier. Respond with "
                "strict JSON only: {\"score\": <float from -1.0 (very negative "
                "for markets/the mentioned company) to 1.0 (very positive)>}. "
                "No other text."
            )
            user = f"Headline: {headline}\nSummary: {summary}"
            raw = self._dispatch(self.sentiment_model, system, user, {}, self.timeout, 128)
            parsed = json.loads(self._strip_code_fence(raw))
            return max(-1.0, min(1.0, float(parsed["score"])))
        except Exception as exc:
            logger.debug("LocalModelGateway: score_sentiment() failed: %s", exc)
            return None
        finally:
            self._concurrency_sem.release()

    def generate(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        context: dict[str, Any] | None = None,
        timeout: int | None = None,
        max_tokens: int | None = None,
        response_schema: dict | None = None,
    ) -> dict[str, Any]:
        """Generate a structured JSON response from the local model.

        Returns a dict with at minimum: action, confidence, reasoning, evidence_ids.
        On any failure returns a safe HOLD stub.

        If a RAGRetriever was supplied at construction the user_prompt is
        enriched with a compact evidence block and the retrieved doc_ids
        are included in the response under "evidence_ids".

        timeout/max_tokens override self.timeout/self.max_tokens for this call
        only. The per-cycle AI council calls never pass these (their budget is
        deliberately shared with supervisor.py's per-agent timeout — see
        runtime.py's comment on AgentCouncilSupervisor construction); they
        exist for occasional, human-triggered calls that need more than the
        per-cycle budget, e.g. generate_strategy_hypotheses()'s multi-hypothesis
        prompt: confirmed 2026-07-28 that qwen3.6-35b-a3b (a "thinking" model)
        spent its entire max_tokens budget on reasoning_content and returned
        finish_reason="length" with empty content — reasoning and the final
        answer compete for the same token budget, so a complex prompt needs
        both a longer timeout AND more max_tokens, not just one.
        """
        with self._counter_lock:
            self._calls_total += 1
        effective_timeout = timeout if timeout is not None else self.timeout
        effective_max_tokens = max_tokens if max_tokens is not None else self.max_tokens
        # system_prompt is always specialists._SYSTEM_BASE, a static constant that
        # itself instructs the model to never include credentials — checking it
        # produced a 100% false-positive rate (found via log monitoring
        # 2026-07-28: 152 warnings in 15 minutes, one per specialist call) and
        # drowned out any real signal from this check. Only scan the dynamic,
        # data-derived content that could actually carry an interpolated secret.
        self._assert_no_secrets(user_prompt)
        if context:
            self._assert_no_secrets(json.dumps(context))

        # ── RAG evidence retrieval ─────────────────────────────────────────
        retrieved_ids: list[str] = []
        enriched_prompt = user_prompt
        if self._rag is not None:
            try:
                evidence_snippet = self._rag.build_context_snippet(user_prompt, top_k=4)
                if evidence_snippet:
                    enriched_prompt = f"{user_prompt}\n\n{evidence_snippet}"
                retrieved_ids = self._rag.retrieve_ids(user_prompt, top_k=4)
            except Exception as rag_exc:
                logger.debug("LocalModelGateway: RAG retrieval error: %s", rag_exc)

        if self.runtime == "stub":
            resp = self._stub_response(model)
            resp["evidence_ids"] = retrieved_ids
            return resp

        if not self._concurrency_sem.acquire(timeout=self._concurrency_wait_s):
            logger.warning(
                "LocalModelGateway: %s runtime='%s' concurrency cap (%d) saturated after "
                "%.1fs wait — stub fallback",
                model, self.runtime, self.max_concurrent_calls, self._concurrency_wait_s,
            )
            resp = self._stub_response(model, failure_mode="concurrency_saturated")
            resp["evidence_ids"] = retrieved_ids
            return resp

        try:
            try:
                start = time.monotonic()
                raw = self._dispatch(
                    model, system_prompt, enriched_prompt, context or {},
                    effective_timeout, effective_max_tokens, response_schema,
                )
                elapsed = time.monotonic() - start
            except Exception as exc:
                logger.info(
                    "LocalModelGateway: %s runtime='%s' unreachable (%s) — using safe stub fallback",
                    model, self.runtime, type(exc).__name__,
                )
                resp = self._stub_response(model, failure_mode=f"{type(exc).__name__}: server_unavailable")
                resp["evidence_ids"] = retrieved_ids
                return resp
        finally:
            self._concurrency_sem.release()

        if elapsed > effective_timeout:
            logger.warning("LocalModelGateway: model %s timed out (%.1fs) — stub fallback", model, elapsed)
            resp = self._stub_response(model, failure_mode="timeout")
            resp["evidence_ids"] = retrieved_ids
            return resp
        parsed = self._parse_json(raw, model)
        # Merge retrieved ids with any ids the model self-reported
        model_ids = parsed.get("evidence_ids") or []
        parsed["evidence_ids"] = list(dict.fromkeys(retrieved_ids + model_ids))
        return parsed

    # ── Dispatch to runtimes ──────────────────────────────────────────────────

    def _dispatch(  # noqa: PLR0913
        self, model: str, system_prompt: str, user_prompt: str, context: dict,
        timeout: int, max_tokens: int, response_schema: dict | None = None,
    ) -> str:
        if self.runtime == "ollama":
            return self._ollama(model, system_prompt, user_prompt, timeout, max_tokens)
        if self.runtime in ("llama_cpp", "vllm", "lm_studio"):
            return self._openai_compat(model, system_prompt, user_prompt, timeout,
                                       max_tokens, response_schema)
        raise ValueError(f"Unknown runtime: {self.runtime}")

    def _ollama(self, model: str, system: str, user: str, timeout: int, max_tokens: int) -> str:
        import urllib.request
        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": "json",
            "options": {"num_predict": max_tokens},
            # Keep the model resident between scan cycles (default ~5 min matches
            # the agent's own scan interval, which would force a slow cold reload
            # — 14s+ measured for llama3.1:8b — on every single cycle otherwise.
            "keep_alive": "30m",
        }).encode()
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return data["message"]["content"]

    def _openai_compat(self, model: str, system: str, user: str, timeout: int,
                       max_tokens: int, response_schema: dict | None = None) -> str:
        import urllib.request
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
        }
        # STRUCTURED OUTPUT (REDESIGN §8.1: "use LM Studio's structured-output
        # (JSON schema) mode for every agent").
        #
        # This previously skipped response_format entirely for lm_studio,
        # because its server 400s on "json_object" (which llama.cpp/vLLM accept)
        # and the correct "json_schema" wire format had not been worked out.
        # Relying on _SYSTEM_BASE's textual "return JSON" instruction instead
        # had a measured cost: the model NEVER stopped on its own. At
        # max_tokens of 128 / 256 / 512 / 2048 it returned finish_reason
        # "length" every single time — burning the entire budget on prose and
        # making every call as slow as its cap allowed (~25s at 2048).
        #
        # With a real json_schema the same call returns finish_reason "stop"
        # after ~833 tokens. That is both faster and safer: the reply is
        # schema-valid by construction, and `length` becomes a TRUE truncation
        # signal we can act on (see _openai_compat's finish_reason check)
        # rather than the constant it used to be.
        if self.runtime == "lm_studio":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "agent_verdict",
                    "strict": True,
                    "schema": response_schema or _VERDICT_SCHEMA,
                },
            }
        else:
            body["response_format"] = {"type": "json_object"}
        payload = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        choice = data["choices"][0]
        # A truncated reply is NOT a verdict. Before structured output this
        # fired on every call and so had to be ignored; now that the model
        # stops naturally, `length` genuinely means the answer was cut off —
        # surface it as a failure so the caller stubs honestly instead of
        # parsing half a JSON object into a confident-looking vote.
        if choice.get("finish_reason") == "length":
            raise ValueError(
                f"{model}: response truncated at max_tokens={max_tokens} "
                "(finish_reason=length) — treating as failure, not a verdict"
            )
        return choice["message"]["content"]

    def _health_check(self) -> bool:
        import urllib.request
        url = f"{self.base_url}/api/tags" if self.runtime == "ollama" else f"{self.base_url}/v1/models"
        try:
            with urllib.request.urlopen(url, timeout=3):
                return True
        except Exception:
            return False

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _stub_response(self, model: str, failure_mode: str | None = None) -> dict:
        with self._counter_lock:
            self._calls_stubbed += 1
            key = failure_mode or "unspecified"
            self._failure_modes[key] = self._failure_modes.get(key, 0) + 1
        base = _STUB_RESPONSES.get(model, _STUB_RESPONSES["gemma4-e4b"]).copy()
        if failure_mode:
            base["failure_mode"] = failure_mode
        base["model_id"] = model
        return base

    def _parse_json(self, raw: str, model: str) -> dict:
        try:
            parsed = json.loads(self._strip_code_fence(raw))
            if not isinstance(parsed, dict):
                raise ValueError("Expected JSON object")
            parsed.setdefault("model_id", model)
            parsed.setdefault("evidence_ids", [])
            parsed.setdefault("failure_mode", None)
            return parsed
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("LocalModelGateway: JSON parse error: %s", exc)
            return self._stub_response(model, failure_mode=f"json_parse_error: {exc}")

    @staticmethod
    def _strip_code_fence(raw: str) -> str:
        """Strip a ```json ... ``` / ``` ... ``` wrapper some models emit around
        JSON output when nothing constrains them to raw JSON — confirmed
        2026-07-28 against google/gemma-4-e4b via LM Studio, whose server
        rejects response_format="json_object" (see _openai_compat), so the
        model falls back to its default habit of markdown-fencing code blocks
        and every reply otherwise failed json.loads at char 0."""
        text = raw.strip()
        if text.startswith("```"):
            text = text[3:]
            if text[:4].lower() == "json":
                text = text[4:]
            text = text.rsplit("```", 1)[0]
        return text.strip()

    def _assert_no_secrets(self, text: str) -> None:
        lower = text.lower()
        for key in self._SECRET_KEYS:
            if key in lower:
                # Warn but do not raise — callers must sanitize; we just log.
                logger.warning("LocalModelGateway: potential secret key '%s' detected in prompt — sanitize callers", key)
                break
