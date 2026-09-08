"""Phase 3 of the strategy-catalog-reconnection plan (2026-09-08): the
continuous version of scripts/run_llm_research_session.py (a one-off manual
runner until now) — meant to be registered as a recurring Windows Scheduled
Task so hypothesis search for genuinely new edge keeps happening on its own,
not only when someone remembers to run it by hand.

Same contract as the manual runner (research/llm_researcher.py's
propose -> validate -> holdout loop, unchanged) applied across several core
underlyings in one run, with everything written to a durable log instead of
just stdout (a scheduled task has no console to watch). Any hypothesis that
survives BOTH the research-period gates AND its own one-shot holdout re-run
gets written prominently to needs_review.log — a queue for a human (or
Claude, next session) to actually look at the code and decide whether to
build it into a real Strategy implementation. This script never promotes
anything into strategies/factory.py itself: the harness validates an
exposure function, not production order-construction/risk code, and turning
one into the other is deliberately a separate, reviewed step (see
strategy_validator.py's own module docstring on the same point for the
dormant-strategy side of this).

Usage:
    python -m scripts.scheduled_research_session
    python -m scripts.scheduled_research_session --instruments RELIANCE,TCS --rounds 3
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_platform.backtesting.short_vol_backtest import load_daily_closes
from trading_platform.research.llm_researcher import LLMResearcher, LocalLLMClient

LOG_DIR = Path("data/research_sessions")
NEEDS_REVIEW_PATH = LOG_DIR / "needs_review.log"

DEFAULT_INSTRUMENTS = ["NIFTY", "BANKNIFTY", "RELIANCE", "TCS", "HDFCBANK"]

# Checked live against GET /v1/models before relying on it (2026-08-27
# originally, reconfirmed 2026-09-08) rather than trusting a stale snapshot —
# LM Studio's loaded set shifts between sessions on this machine. Extra
# entries that happen not to be loaded right now are harmless: FallbackLLMClient
# just logs and moves to the next one.
_MODELS = [
    "qwen/qwen3.6-35b-a3b",
    "google/gemma-4-31b-qat",
    "qwen/qwen3.8-27b",
    "google/gemma-4-e4b",
]


class FallbackLLMClient:
    """Tries each model in order; only raises if all of them fail. Copied
    from run_llm_research_session.py's own (already-proven) version rather
    than imported, to keep this scheduled entry point self-contained and
    safe to edit independently."""

    def __init__(self, models: list[str], base_url: str = "http://localhost:1234/v1") -> None:
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


def _append_needs_review(instrument: str, name: str, rationale: str, code: str,
                          research_verdict, holdout_verdict) -> None:
    NEEDS_REVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    block = f"""
{'=' * 78}
{ts}  {instrument}  {name}  ** SURVIVED RESEARCH + HOLDOUT — NEEDS HUMAN REVIEW **
rationale: {rationale}
research : CAGR {research_verdict.best_cagr * 100:.2f}%  Sharpe {research_verdict.best_sharpe:.2f}
holdout  : CAGR {holdout_verdict.best_cagr * 100:.2f}%  Sharpe {holdout_verdict.best_sharpe:.2f}  \
verdict={'PASS' if holdout_verdict.passed else 'FAIL'}
code:
{code}
"""
    with NEEDS_REVIEW_PATH.open("a", encoding="utf-8") as fh:
        fh.write(block)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instruments", default=",".join(DEFAULT_INSTRUMENTS))
    parser.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args()

    instruments = [s.strip().upper() for s in args.instruments.split(",") if s.strip()]

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    run_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S+00-00")
    log_path = LOG_DIR / f"session_{run_ts}.log"
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()],
    )
    logger = logging.getLogger(__name__)

    researcher = LLMResearcher(client=FallbackLLMClient(_MODELS))
    total_survivors = 0

    for instrument in instruments:
        path = Path(f"data/historical/{instrument}__ONE_DAY_deep.csv")
        if not path.exists():
            path = Path(f"data/historical/{instrument}__ONE_DAY.csv")
        if not path.exists():
            logger.warning("SKIP %s — no historical CSV found", instrument)
            continue
        try:
            bars = load_daily_closes(path)
        except Exception as exc:
            logger.warning("SKIP %s — could not load history: %s", instrument, exc)
            continue
        if len(bars) < 200:
            logger.warning("SKIP %s — only %d bars, too little history", instrument, len(bars))
            continue

        logger.info("=== %s: %d daily bars, %s .. %s ===", instrument, len(bars), bars[0].day, bars[-1].day)
        try:
            session = researcher.research(instrument, bars, rounds=args.rounds, verbose=False)
        except Exception as exc:
            logger.error("%s: research session failed: %s", instrument, exc)
            continue

        logger.info(session.report())

        for name, holdout_verdict in session.holdout_results.items():
            if not holdout_verdict.passed:
                continue
            prop = next((p for p, _ in session.attempts if p.name == name), None)
            research_verdict = next((v for p, v in session.attempts if p.name == name), None)
            if prop is None or research_verdict is None:
                continue
            total_survivors += 1
            logger.info("SURVIVOR: %s / %s — writing to %s", instrument, name, NEEDS_REVIEW_PATH)
            _append_needs_review(instrument, name, prop.rationale, prop.code,
                                  research_verdict, holdout_verdict)

    logger.info("Session complete: %d instrument(s), %d survivor(s) needing review. Log: %s",
                len(instruments), total_survivors, log_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
