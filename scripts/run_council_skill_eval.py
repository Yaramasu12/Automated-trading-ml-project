"""EVALS for the AI council's actual advisory skill — not RAG retrieval
quality (that's ai/rag/eval.py, wired by run_rag_eval.py), not strategy
backtest performance (that's research/hypothesis_harness.py) — whether the
council's own PROCEED/REDUCE/HALT/NO_TRADE calls and per-specialist votes
correlate with what actually happened to the trade.

Mirrors algo-trading-system's agent_evals.py (memory:
algo-trading-system-ai-layer): score historical agent output against real
subsequent outcomes, so this component's actual skill is measured, not
assumed — same "honesty discipline" this project already applies to trading
strategies, extended to the advisory layer itself.

The join key already exists in the data model: GET /ai-council/decisions
returns per-trace council votes (trace_id, action, confidence, per-agent
votes); GET /db/reflections-history returns per-trace REAL outcomes (won,
pnl_pct, quality — a meta-label score already computed by
trace/label_factory.py's triple-barrier logic). This script is the missing
piece that actually joins them and reports whether they correlate — nothing
here computes a NEW label, it only asks a question of labels that already
exist.
"""
from __future__ import annotations

import sys
import urllib.request
import json
from collections import defaultdict


def _get(base_url: str, path: str) -> dict:
    with urllib.request.urlopen(f"{base_url}{path}", timeout=15) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8100"

    decisions_raw = _get(base_url, "/ai-council/decisions?limit=500")["decisions"]
    reflections = _get(base_url, "/db/reflections-history?limit=500")["reflections"]
    outcome_by_trace = {r["trace_id"]: r for r in reflections}

    # Each "decision" in the API response is itself a list of per-agent votes
    # for one trace (see /ai-council/decisions' implementation: it returns
    # t["agent_outputs"] per trace, not a single flattened record) — pull the
    # trace-level action/confidence back out of the first vote's shared context
    # where present, otherwise summarize from the votes themselves.
    joined = []
    for votes in decisions_raw:
        if not votes:
            continue
        trace_id = votes[0].get("trace_id")
        if not trace_id or trace_id not in outcome_by_trace:
            continue
        real_votes = [v for v in votes if v.get("failure_mode") is None]
        stub_votes = [v for v in votes if v.get("failure_mode") is not None]
        outcome = outcome_by_trace[trace_id]
        joined.append({
            "trace_id": trace_id,
            "n_votes": len(votes),
            "n_real_votes": len(real_votes),
            "n_stub_votes": len(stub_votes),
            "mean_confidence": sum(v["confidence"] for v in votes) / len(votes),
            "won": outcome["won"],
            "pnl_pct": outcome["pnl_pct"],
            "quality": outcome.get("quality"),
        })

    print(f"Council decisions with a traced outcome: {len(joined)} "
          f"(of {len(decisions_raw)} total traced decisions, {len(reflections)} total outcomes)")

    if not joined:
        print("\nZero overlap by trace_id — and this WON'T change no matter how much "
              "data accumulates, which is the real, structural finding here (not just "
              "'not enough data yet'): council decisions are traced under scan-* IDs "
              "(one DecisionTrace per scan cycle) and trade outcomes are traced under "
              "order-* IDs (one per executed order) — two disjoint ID namespaces, "
              "confirmed by inspecting live samples of both.")
        print("\nA real bridge DOES exist and isn't yet exploited: DecisionTrace "
              "(trace/models.py) has an order_intent_ids: list[str] field — the scan "
              "trace records which order intents it produced. Resolving "
              "scan_trace.order_intent_ids -> the order/trade that intent became -> "
              "that order's outcome would make this join possible, but needs someone "
              "to confirm what order_intent_ids actually holds at runtime (an intent "
              "UUID? the broker order_id format seen in /db/trades, e.g. 'SIM-000012'?) "
              "and trace that all the way to /db/reflections-history's order-* trace_id. "
              "Not resolved here — flagging precisely rather than guessing at IDs that "
              "might not even be the same identifier space.")
        return 0

    n_stub_only = sum(1 for j in joined if j["n_real_votes"] == 0)
    real = [j for j in joined if j["n_real_votes"] > 0]
    print(f"Real (non-stub) council decisions with an outcome: {len(real)} "
          f"({n_stub_only} were 100% stub fallback — excluded from skill scoring)")

    if len(real) < 5:
        print(f"\nOnly {len(real)} real decisions with outcomes — too few to draw any "
              f"conclusion about whether council confidence predicts trade quality. "
              f"Re-run this script as more accumulate; do not trust a correlation "
              f"computed on this few points.")
        for j in real:
            print(f"  {j['trace_id']}: mean_confidence={j['mean_confidence']:.2f} "
                  f"won={j['won']} pnl_pct={j['pnl_pct']:.3f} quality={j.get('quality')}")
        return 0

    # Only compute this once there's a defensible sample size.
    high_conf = [j for j in real if j["mean_confidence"] >= 0.6]
    low_conf = [j for j in real if j["mean_confidence"] < 0.6]
    for label, bucket in (("high-confidence (>=0.6)", high_conf), ("low-confidence (<0.6)", low_conf)):
        if bucket:
            win_rate = sum(1 for j in bucket if j["won"]) / len(bucket)
            mean_quality = sum(j["quality"] or 0 for j in bucket) / len(bucket)
            print(f"{label}: n={len(bucket)} win_rate={win_rate:.1%} mean_quality={mean_quality:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
