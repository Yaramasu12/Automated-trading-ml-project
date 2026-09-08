"""Phase 1 of the strategy-catalog-reconnection plan (2026-09-08): run the
strategies in strategies/factory.py through HypothesisHarness's DSR/PBO/
Monte-Carlo gates — the same bar short_vol cleared and futures_trend
failed — before any of them is allowed near live capital.

Covers two families (see strategy_validator.py's module docstring for the
full breakdown): single-instrument directional strategies
(SINGLE_INSTRUMENT_STRATEGIES) and single-leg option-buying strategies
(OPTION_STRATEGIES, P&L simulated against a synthetic rolling ATM-premium
series). Also records, informationally, the strategies that don't need or
can't get a fresh backtest: three bare FuturesTrendStrategy subclasses that
inherit futures_trend's own already-REJECTED verdict byte-for-byte
(INHERITS_FUTURES_TREND_VERDICT), two unmodified DefinedRiskOptionSpread
subclasses that inherit that strategy's verdict
(INHERITS_DEFINED_RISK_SPREAD_VERDICT), and six strategies whose
generate_signal() unconditionally returns None pending a MultiLegOrderManager
that doesn't exist yet (STRUCTURALLY_DISABLED_STRATEGIES) — not rejected by
backtesting, just not testable today.

Merges into the existing registry rather than overwriting it: a partial
re-run (e.g. just one strategy after a code change) updates only the rows
it touched, so it can't silently erase everything else that was already
validated.

Usage:
    python -m scripts.validate_dormant_strategies
    python -m scripts.validate_dormant_strategies --symbols RELIANCE,TCS
    python -m scripts.validate_dormant_strategies --strategies mean_reversion
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_platform.research.strategy_validator import (
    INHERITS_DEFINED_RISK_SPREAD_VERDICT,
    INHERITS_FUTURES_TREND_VERDICT,
    OPTION_STRATEGIES,
    SINGLE_INSTRUMENT_STRATEGIES,
    STRUCTURALLY_DISABLED_STRATEGIES,
    REGISTRY_PATH,
    validate_option_strategy,
    validate_strategy,
)

logging.basicConfig(level=logging.WARNING, format="%(message)s")

DEFAULT_SYMBOLS = [
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK",
    "NIFTY", "BANKNIFTY", "SBIN", "ITC", "AXISBANK",
]

DEFAULT_STRATEGIES = list(SINGLE_INSTRUMENT_STRATEGIES) + list(OPTION_STRATEGIES)


def _csv_path(symbol: str) -> Path | None:
    deep = Path(f"data/historical/{symbol}__ONE_DAY_deep.csv")
    if deep.exists():
        return deep
    plain = Path(f"data/historical/{symbol}__ONE_DAY.csv")
    return plain if plain.exists() else None


def _load_existing_results() -> list[dict]:
    if not REGISTRY_PATH.exists():
        return []
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8")).get("results", [])
    except Exception:
        return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--strategies", default=",".join(DEFAULT_STRATEGIES))
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]

    # Start from whatever's already recorded for OTHER strategies; this
    # run's own strategies are recomputed fresh below, not merged row-by-row
    # (a strategy's whole symbol set is one coherent experiment).
    existing = _load_existing_results()
    kept = [r for r in existing if r.get("strategy_name") not in strategies]

    fresh: list[dict] = []
    for strategy_name in strategies:
        validator = validate_option_strategy if strategy_name in OPTION_STRATEGIES else validate_strategy
        for symbol in symbols:
            path = _csv_path(symbol)
            if path is None:
                print(f"SKIP  {strategy_name:28} {symbol:10} no historical CSV found")
                continue
            try:
                rec = validator(strategy_name, symbol, path)
            except Exception as exc:
                print(f"ERROR {strategy_name:28} {symbol:10} {exc}")
                continue
            verdict = "PASS" if rec.passed else "FAIL"
            print(
                f"{verdict:4}  {strategy_name:28} {symbol:10} "
                f"CAGR={rec.cagr*100:6.2f}% Sharpe={rec.sharpe:6.3f} "
                f"maxDD={rec.max_drawdown*100:5.1f}%  {rec.dsr_message}"
            )
            fresh.append({
                "strategy_name": rec.strategy_name,
                "symbol": rec.symbol,
                "passed": rec.passed,
                "cagr": rec.cagr,
                "sharpe": rec.sharpe,
                "max_drawdown": rec.max_drawdown,
                "dsr_message": rec.dsr_message,
                "pbo_message": rec.pbo_message,
                "evaluated_at": rec.evaluated_at,
            })

    results = kept + fresh

    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "results": results,
        # Informational only -- load_accepted_strategies() only reads
        # "results" above, so these can never be silently treated as passed.
        "inherits_futures_trend_verdict_rejected": list(INHERITS_FUTURES_TREND_VERDICT),
        "inherits_defined_risk_spread_verdict": list(INHERITS_DEFINED_RISK_SPREAD_VERDICT),
        "structurally_disabled_pending_multileg_order_manager": list(STRUCTURALLY_DISABLED_STRATEGIES),
    }, indent=2), encoding="utf-8")

    print()
    print(f"wrote {len(results)} records to {REGISTRY_PATH} ({len(fresh)} fresh, {len(kept)} kept from prior runs)")

    # Acceptance bar: a strategy passing on only one of several independent
    # symbols is exactly the kind of single-trial luck DSR/PBO exist to
    # catch across a param grid, not across symbols — testing N symbols and
    # keeping whichever one passed is its own multiple-comparisons problem
    # (see evaluate_all()'s own warning about this trap). Require a real
    # majority of tested symbols to pass before calling a strategy accepted.
    by_strategy: dict[str, list[dict]] = {}
    for r in results:
        by_strategy.setdefault(r["strategy_name"], []).append(r)

    print("Per-strategy pass rate (need > 50% of tested symbols to be ACCEPTED):")
    accepted: list[str] = []
    for name, recs in sorted(by_strategy.items()):
        n_pass = sum(1 for r in recs if r["passed"])
        rate = n_pass / len(recs) if recs else 0.0
        verdict = "ACCEPTED" if rate > 0.5 else "rejected"
        if verdict == "ACCEPTED":
            accepted.append(name)
        print(f"  {name:28} {n_pass}/{len(recs)} symbols passed ({rate*100:.0f}%) -> {verdict}")

    if INHERITS_FUTURES_TREND_VERDICT:
        print(f"\n  (inherit futures_trend's REJECTED verdict, no separate backtest possible: "
              f"{list(INHERITS_FUTURES_TREND_VERDICT)})")
    if INHERITS_DEFINED_RISK_SPREAD_VERDICT:
        print(f"  (inherit defined_risk_option_spread's verdict above by identical code path: "
              f"{list(INHERITS_DEFINED_RISK_SPREAD_VERDICT)})")
    if STRUCTURALLY_DISABLED_STRATEGIES:
        print(f"  (structurally disabled pending MultiLegOrderManager, not rejected by backtesting: "
              f"{list(STRUCTURALLY_DISABLED_STRATEGIES)})")

    print(f"\nACCEPTED strategies: {accepted or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
