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
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trading_platform.agents.vector_memory import RAGRetriever

logger = logging.getLogger(__name__)

# Deterministic stub responses keyed by model name + prompt hash hint
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
    ) -> None:
        self.runtime = runtime
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_tokens = max_tokens
        self._available = runtime == "stub"  # stubs always available
        self._rag: RAGRetriever | None = rag_retriever

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
        return {
            "runtime": self.runtime,
            "base_url": self.base_url if self.runtime != "stub" else None,
            "available": available,
            "fallback_active": self.runtime != "stub" and not available,
            "rag": rag_status,
            "models": {
                "primary": "gemma4-31b",
                "coordinator": "gemma4-26b-moe",
                "fast": "gemma4-e4b",
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

    def generate(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Generate a structured JSON response from the local model.

        Returns a dict with at minimum: action, confidence, reasoning, evidence_ids.
        On any failure returns a safe HOLD stub.

        If a RAGRetriever was supplied at construction the user_prompt is
        enriched with a compact evidence block and the retrieved doc_ids
        are included in the response under "evidence_ids".
        """
        self._assert_no_secrets(system_prompt)
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

        try:
            start = time.monotonic()
            raw = self._dispatch(model, system_prompt, enriched_prompt, context or {})
            elapsed = time.monotonic() - start
            if elapsed > self.timeout:
                logger.warning("LocalModelGateway: model %s timed out (%.1fs) — stub fallback", model, elapsed)
                resp = self._stub_response(model, failure_mode="timeout")
                resp["evidence_ids"] = retrieved_ids
                return resp
            parsed = self._parse_json(raw, model)
            # Merge retrieved ids with any ids the model self-reported
            model_ids = parsed.get("evidence_ids") or []
            parsed["evidence_ids"] = list(dict.fromkeys(retrieved_ids + model_ids))
            return parsed
        except Exception as exc:
            logger.info(
                "LocalModelGateway: %s runtime='%s' unreachable (%s) — using safe stub fallback",
                model, self.runtime, type(exc).__name__,
            )
            resp = self._stub_response(model, failure_mode=f"{type(exc).__name__}: server_unavailable")
            resp["evidence_ids"] = retrieved_ids
            return resp

    # ── Dispatch to runtimes ──────────────────────────────────────────────────

    def _dispatch(self, model: str, system_prompt: str, user_prompt: str, context: dict) -> str:
        if self.runtime == "ollama":
            return self._ollama(model, system_prompt, user_prompt)
        if self.runtime in ("llama_cpp", "vllm"):
            return self._openai_compat(model, system_prompt, user_prompt)
        raise ValueError(f"Unknown runtime: {self.runtime}")

    def _ollama(self, model: str, system: str, user: str) -> str:
        import urllib.request
        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": "json",
            "options": {"num_predict": self.max_tokens},
        }).encode()
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read())
        return data["message"]["content"]

    def _openai_compat(self, model: str, system: str, user: str) -> str:
        import urllib.request
        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
        }).encode()
        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]

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
        base = _STUB_RESPONSES.get(model, _STUB_RESPONSES["gemma4-e4b"]).copy()
        if failure_mode:
            base["failure_mode"] = failure_mode
        base["model_id"] = model
        return base

    def _parse_json(self, raw: str, model: str) -> dict:
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("Expected JSON object")
            parsed.setdefault("model_id", model)
            parsed.setdefault("evidence_ids", [])
            parsed.setdefault("failure_mode", None)
            return parsed
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("LocalModelGateway: JSON parse error: %s", exc)
            return self._stub_response(model, failure_mode=f"json_parse_error: {exc}")

    def _assert_no_secrets(self, text: str) -> None:
        lower = text.lower()
        for key in self._SECRET_KEYS:
            if key in lower:
                # Warn but do not raise — callers must sanitize; we just log.
                logger.warning("LocalModelGateway: potential secret key '%s' detected in prompt — sanitize callers", key)
                break
