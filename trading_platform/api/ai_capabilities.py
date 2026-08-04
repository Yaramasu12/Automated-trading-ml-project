"""Honest reporting of which "advanced" AI layers are real vs stub/heuristic.

Review finding #4: quantum/RL/parts-of-neural are placeholders, and the
orchestrator wires absent services to neutral defaults that never veto — so the
pipeline *markets* them as safety/edge layers while they're inert.

This module makes that degradation **visible**: it introspects the configured
backends and returns a per-layer status plus an explicit statement that these
layers are ADVISORY and do not hard-block trades (the real gates are RiskCritic,
ProfitGuard, and EventRiskGuard). The runtime surfaces this in /health and logs
it at startup, so an operator can never mistake an inert stub for a live safety
control.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Statuses that mean "do not trust this as an advanced edge/safety layer".
_WEAK_STATUSES = {"disabled", "stub", "classical_fallback", "heuristic_baseline", "mock_only", "no_data_source"}


def ai_capabilities(runtime: Any) -> dict:
    """Return an honest capability report for the AI layers."""
    s = runtime.settings
    layers: dict[str, dict] = {}

    # ── LLM / AI Council ──────────────────────────────────────────────────────
    gateway = str(getattr(s, "local_llm_gateway", "disabled") or "disabled").lower()
    llm_runtime = str(getattr(s, "local_llm_runtime", "stub") or "stub").lower()
    if gateway in ("", "disabled"):
        llm_status = "disabled"
    elif gateway == "stub" or llm_runtime == "stub":
        llm_status = "stub"
    else:
        llm_status = "real"
    layers["llm_council"] = {
        "status": llm_status,
        "detail": f"gateway={gateway} runtime={llm_runtime}",
        "role": "advisory",
    }

    # ── Neural forecaster ─────────────────────────────────────────────────────
    neural = None
    try:
        neural = runtime.neural_service
    except Exception:
        pass
    if neural is None:
        neural_status = "disabled"
    else:
        gbm = getattr(neural, "_gbm_forecaster", None)
        neural_status = "validated_model" if (gbm is not None and gbm.is_available()) else "heuristic_baseline"
    layers["neural_forecast"] = {
        "status": neural_status,
        "detail": "GBM validated model active" if neural_status == "validated_model"
                  else "moving-average baseline (no validated edge model loaded)",
        "role": "advisory",
    }

    # ── News sentiment ────────────────────────────────────────────────────────
    # Real as of 2026-08-04: TradingRuntime._periodic_news_fetch_loop ingests
    # real RSS headlines 24/7 (see news/feed.py) and master_orchestrator.py's
    # _node_market_intelligence reads them back via
    # NewsIntelligence.sentiment_for(). Status reflects whether that's
    # actually producing data right now, not just whether the code exists.
    if not getattr(s, "enable_news_feed", False):
        layers["news_sentiment"] = {
            "status": "disabled",
            "detail": "news feed loop disabled (ENABLE_NEWS_FEED=false)",
            "role": "advisory",
        }
    else:
        active = 0
        try:
            active = int(runtime.news_intelligence.feature_snapshot().get("active_event_count", 0))
        except Exception:
            pass
        if active > 0:
            layers["news_sentiment"] = {
                "status": "real",
                "detail": f"{active} active real news event(s) ingested",
                "role": "advisory",
            }
        else:
            layers["news_sentiment"] = {
                "status": "no_data_source",
                "detail": "feed loop enabled but no events ingested yet (just started, or all feeds unreachable)",
                "role": "advisory",
            }

    # ── RL / MARL policies ────────────────────────────────────────────────────
    rl_status = "disabled"
    try:
        registry = runtime.policy_registry
        records = registry.list_all() or []
        real = 0
        mock = 0
        for rec in records:
            pid = rec.get("policy_id") if isinstance(rec, dict) else None
            policy = registry.get(pid) if pid else None
            name = type(policy).__name__ if policy is not None else ""
            if "Mock" in name:
                mock += 1
            elif policy is not None:
                real += 1
        if real > 0:
            rl_status = "trained_policies"
        elif mock > 0:
            rl_status = "mock_only"
    except Exception:
        rl_status = "disabled"
    layers["rl_marl"] = {"status": rl_status, "role": "advisory"}

    degraded = sorted(k for k, v in layers.items() if v["status"] in _WEAK_STATUSES)
    return {
        "layers": layers,
        "degraded": bool(degraded),
        "degraded_layers": degraded,
        "note": (
            "These AI layers are ADVISORY — they inform sizing/conviction but do NOT "
            "hard-block trades. Hard safety gates are RiskCritic, ProfitGuard, and "
            "EventRiskGuard. Any layer marked stub/heuristic_baseline/classical_fallback/"
            "mock_only is NOT an advanced edge or safety control; treat its signal as weak."
        ),
    }


def log_capabilities_at_startup(runtime: Any) -> None:
    """Emit a clear WARNING listing inert/degraded AI layers at boot."""
    try:
        cap = ai_capabilities(runtime)
    except Exception as exc:
        logger.debug("ai_capabilities report failed: %s", exc)
        return
    if cap["degraded"]:
        logger.warning(
            "AI layers running DEGRADED/ADVISORY-ONLY: %s. These do NOT block trades; "
            "real safety = RiskCritic/ProfitGuard/EventRiskGuard.",
            ", ".join(f"{k}={cap['layers'][k]['status']}" for k in cap["degraded_layers"]),
        )
    else:
        logger.info("AI layers: all configured backends are real.")
