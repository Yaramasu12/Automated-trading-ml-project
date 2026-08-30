"""CLI wrapper for trading_platform.governance.eval_harness.evaluate_council_skill
— fetches over HTTP (for running against a remote deployment, or from outside
the API process) and pretty-prints the result. The join/scoring logic itself
lives in governance/eval_harness.py, shared with GET /ai-council/skill-eval
(the in-process version, served by the running API) so the two never drift.
"""
from __future__ import annotations

import sys
import urllib.request
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_platform.governance.eval_harness import evaluate_council_skill


def _get(base_url: str, path: str) -> dict:
    with urllib.request.urlopen(f"{base_url}{path}", timeout=15) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8100"

    decisions_raw = _get(base_url, "/ai-council/decisions?limit=500")["decisions"]
    reflections = _get(base_url, "/db/reflections-history?limit=500")["reflections"]
    result = evaluate_council_skill(decisions_raw, reflections)

    print(f"Council decisions with a traced outcome: {result['joined_count']} "
          f"(of {result['total_decisions_traced']} total traced decisions, "
          f"{result['total_outcomes_traced']} total outcomes)")

    if result["structural_note"]:
        print(f"\n{result['structural_note']}")

    if result["joined_count"] and not result["sample_size_sufficient"]:
        print(f"\nReal (non-stub) decisions with an outcome: {result['real_decision_count']} "
              f"({result['stub_only_count']} were 100% stub fallback — excluded)")
        for j in result["joined"]:
            if j["n_real_votes"] > 0:
                print(f"  {j['trace_id']}: mean_confidence={j['mean_confidence']:.2f} "
                      f"won={j['won']} pnl_pct={j['pnl_pct']:.3f} quality={j.get('quality')}")

    if result["buckets"]:
        for label, b in result["buckets"].items():
            print(f"{label}: n={b['n']} win_rate={b['win_rate']:.1%} mean_quality={b['mean_quality']:.2f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
