"""EVALS for the AI council's actual advisory skill — not RAG retrieval
quality (that's ai/rag/eval.py), not strategy backtest performance (that's
research/hypothesis_harness.py) — whether the council's own
PROCEED/REDUCE/HALT/NO_TRADE calls and per-specialist confidence correlate
with what actually happened to the trade.

Pure, side-effect-free join/scoring logic shared by two callers:
  - scripts/run_council_skill_eval.py (CLI, fetches over HTTP — for running
    against a remote/external deployment, or from outside the process)
  - GET /ai-council/skill-eval (api/app.py — in-process, no self-HTTP-call,
    fast enough to serve synchronously since this is pure data correlation,
    no LLM calls involved)

Mirrors algo-trading-system's agent_evals.py (memory:
algo-trading-system-ai-layer): score historical agent output against real
subsequent outcomes, so this component's actual skill is measured, not
assumed — the same "honesty discipline" this project already applies to
trading strategies, extended to the advisory layer itself.
"""
from __future__ import annotations

from typing import Any

MIN_REAL_DECISIONS_FOR_CORRELATION = 5
HIGH_CONFIDENCE_THRESHOLD = 0.6


def evaluate_council_skill(decisions_raw: list[list[dict]], reflections: list[dict]) -> dict[str, Any]:
    """Join council decisions to real trade outcomes on trace_id and report
    whether confidence predicts quality. Never raises — always returns a
    well-formed dict, even with empty/malformed inputs.

    decisions_raw: as returned by GET /ai-council/decisions — each element
        is the list of per-agent AgentVote dicts for one traced scan cycle.
    reflections: as returned by GET /db/reflections-history — one dict per
        traced trade outcome (trace_id, won, pnl_pct, quality, ...).
    """
    outcome_by_trace = {r["trace_id"]: r for r in reflections if r.get("trace_id")}

    joined: list[dict[str, Any]] = []
    for votes in decisions_raw:
        if not votes:
            continue
        trace_id = votes[0].get("trace_id")
        if not trace_id or trace_id not in outcome_by_trace:
            continue
        real_votes = [v for v in votes if v.get("failure_mode") is None]
        stub_votes = [v for v in votes if v.get("failure_mode") is not None]
        outcome = outcome_by_trace[trace_id]
        confidences = [v.get("confidence", 0.0) for v in votes]
        joined.append({
            "trace_id": trace_id,
            "n_votes": len(votes),
            "n_real_votes": len(real_votes),
            "n_stub_votes": len(stub_votes),
            "mean_confidence": sum(confidences) / len(confidences) if confidences else 0.0,
            "won": outcome.get("won"),
            "pnl_pct": outcome.get("pnl_pct"),
            "quality": outcome.get("quality"),
        })

    result: dict[str, Any] = {
        "total_decisions_traced": len(decisions_raw),
        "total_outcomes_traced": len(reflections),
        "joined_count": len(joined),
        "structural_note": None,
        "real_decision_count": 0,
        "stub_only_count": 0,
        "sample_size_sufficient": False,
        "buckets": None,
        "joined": joined,
    }

    if not joined:
        result["structural_note"] = (
            "Zero overlap by trace_id. Traced 2026-09-13 (audit_results/2026-09-13/"
            "AUDIT_REPORT.md finding #11): this is NOT a fixable ID-propagation bug "
            "— DecisionTrace.order_intent_ids does get populated correctly when an "
            "intent already carries its originating trace_id (see runtime.py's "
            "_ensure_trace_for_intent). The real reason is structural: "
            "MasterOrchestrator.run() (the specialist-crew/council voting cycle) "
            "runs every scan regardless, but its resulting order_candidates are "
            "only ever enqueued when AGENT_DIRECTIONAL_ENABLED=true — off by "
            "default, since directional momentum has no proven edge (see "
            "AGENTS.md's honesty discipline). short_vol_executor.py, the strategy "
            "that generates this deployment's actual fills, never calls "
            "MasterOrchestrator or records any agent_outputs at all — its intents "
            "carry no pre-existing trace_id, so _ensure_trace_for_intent mints a "
            "fresh one at enqueue time, disconnected from any council vote by "
            "construction. The council's votes and the trades that actually "
            "execute are two different populations: no amount of trace-ID "
            "resolution can join votes that were never cast on the decisions that "
            "actually happened. Measuring the council's skill against real "
            "outcomes requires either enabling directional trading (not "
            "recommended without proven edge) or routing short_vol's own entries "
            "through an advisory (non-gating) council check purely to collect "
            "this evaluation data — both are product decisions, not bugs to fix "
            "here."
        )
        return result

    n_stub_only = sum(1 for j in joined if j["n_real_votes"] == 0)
    real = [j for j in joined if j["n_real_votes"] > 0]
    result["real_decision_count"] = len(real)
    result["stub_only_count"] = n_stub_only

    if len(real) < MIN_REAL_DECISIONS_FOR_CORRELATION:
        result["structural_note"] = (
            f"Only {len(real)} real (non-stub) decisions with a traced outcome — "
            f"below the minimum of {MIN_REAL_DECISIONS_FOR_CORRELATION} needed before "
            f"a confidence/quality correlation means anything. Do not trust a "
            f"correlation computed on this few points; this is reported as raw "
            f"per-decision rows instead (see 'joined')."
        )
        return result

    result["sample_size_sufficient"] = True
    high_conf = [j for j in real if j["mean_confidence"] >= HIGH_CONFIDENCE_THRESHOLD]
    low_conf = [j for j in real if j["mean_confidence"] < HIGH_CONFIDENCE_THRESHOLD]
    buckets = {}
    for label, bucket in (("high_confidence", high_conf), ("low_confidence", low_conf)):
        if bucket:
            buckets[label] = {
                "n": len(bucket),
                "win_rate": sum(1 for j in bucket if j["won"]) / len(bucket),
                "mean_quality": sum(j["quality"] or 0 for j in bucket) / len(bucket),
            }
    result["buckets"] = buckets
    return result
