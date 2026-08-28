"""One-off runner for the LLM hypothesis research loop (research/llm_researcher.py)
against real NIFTY daily bars, using the 3 models actually verified working in
this LM Studio instance today (2026-08-27) — NOT llm_researcher.py's own
DEFAULT_MODEL ("meta/llama-3.3-70b"), which isn't loaded here at all.

Falls back across models if one is busy (e.g. the live AI council is mid-call
on the primary) rather than hardcoding a single model and failing the whole
round on contention.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_platform.backtesting.short_vol_backtest import load_daily_closes
from trading_platform.research.llm_researcher import LLMResearcher, LocalLLMClient

logging.basicConfig(level=logging.INFO, format="%(message)s")

# Checked live against GET /v1/models immediately before this run
# (2026-08-27) rather than trusting a stale snapshot — LM Studio's loaded set
# has been shifting during this session (glm-4.5-air and a duplicate
# gemma-4-31b-qat entry both came and went). All 4 chat-capable models
# currently listed are verified to actually respond when called ALONE;
# qwen3.8-27b previously crashed the GPU driver only when called
# concurrently with another model — safe here since this script calls one
# model at a time, in sequence, never in parallel.
_MODELS = [
    "qwen/qwen3.6-35b-a3b",
    "google/gemma-4-31b-qat",
    "qwen/qwen3.8-27b",
    "google/gemma-4-e4b",
]


class FallbackLLMClient:
    """Tries each model in order; only raises if all of them fail."""

    def __init__(self, models: list[str], base_url: str = "http://localhost:1234/v1") -> None:
        # All 4 currently-loaded models are "thinking" hybrids that burn their
        # ENTIRE token budget on hidden reasoning even for a 1-word prompt —
        # confirmed empirically, and chat_template_kwargs.enable_thinking:
        # false is a no-op on this LM Studio build. LocalLLMClient.complete()
        # now falls back to reasoning_content when content is empty (fixed
        # 2026-08-27), so what matters here is giving the model enough budget
        # to actually reach the NAME:/CODE: answer inside that stream — 16000
        # tokens produced a full, parseable, structurally novel proposal in
        # testing; 6000 was not enough. This is slow (multi-minute per call
        # on local hardware) but correctness beats speed for an overnight run.
        self._clients = [
            LocalLLMClient(base_url=base_url, model=m, max_tokens=16000, timeout=900) for m in models
        ]

    def complete(self, system: str, user: str, temperature: float = 0.7) -> str:
        last_exc: Exception | None = None
        for client in self._clients:
            try:
                return client.complete(system, user, temperature=temperature)
            except Exception as exc:  # noqa: BLE001 - deliberately broad, this IS the fallback
                logging.warning("model %s unavailable/busy (%s) — trying next", client.model, exc)
                last_exc = exc
        raise RuntimeError(f"all {len(self._clients)} local models failed") from last_exc


def main() -> int:
    instrument = sys.argv[1] if len(sys.argv) > 1 else "NIFTY"
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    path = f"data/historical/{instrument}__ONE_DAY_deep.csv"
    bars = load_daily_closes(path)
    print(f"{instrument}: {len(bars)} daily bars, {bars[0].day} .. {bars[-1].day}")

    researcher = LLMResearcher(client=FallbackLLMClient(_MODELS))
    session = researcher.research(instrument, bars, rounds=rounds)
    print()
    print(session.report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
