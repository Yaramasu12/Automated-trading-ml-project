"""Phase 1 of the strategy-catalog-reconnection plan (2026-09-08): run the
17 dormant strategies in strategies/factory.py through HypothesisHarness's
DSR/PBO/Monte-Carlo gates — the same bar short_vol cleared and futures_trend
failed — before any of them is allowed near live capital.

This run covers the 5 strategies validatable with single-instrument OHLCV
alone (see strategy_validator.py's module docstring for why the other 12 —
options spreads needing a modeled IV surface, hedge_futures/pair_hedge/
expiry_rollover needing multi-instrument or expiry-aware data — need a
different approach and are out of scope here).

Usage:
    python -m scripts.validate_dormant_strategies
    python -m scripts.validate_dormant_strategies --symbols RELIANCE,TCS
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
    SINGLE_INSTRUMENT_STRATEGIES,
    validate_strategy,
)

logging.basicConfig(level=logging.WARNING, format="%(message)s")

DEFAULT_SYMBOLS = [
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK",
    "NIFTY", "BANKNIFTY", "SBIN", "ITC", "AXISBANK",
]

REGISTRY_PATH = Path("data/strategy_validation_registry.json")


def _csv_path(symbol: str) -> Path | None:
    deep = Path(f"data/historical/{symbol}__ONE_DAY_deep.csv")
    if deep.exists():
        return deep
    plain = Path(f"data/historical/{symbol}__ONE_DAY.csv")
    return plain if plain.exists() else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--strategies", default=",".join(SINGLE_INSTRUMENT_STRATEGIES))
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]

    results: list[dict] = []
    for strategy_name in strategies:
        for symbol in symbols:
            path = _csv_path(symbol)
            if path is None:
                print(f"SKIP  {strategy_name:16} {symbol:10} no historical CSV found")
                continue
            try:
                rec = validate_strategy(strategy_name, symbol, path)
            except Exception as exc:
                print(f"ERROR {strategy_name:16} {symbol:10} {exc}")
                continue
            verdict = "PASS" if rec.passed else "FAIL"
            print(
                f"{verdict:4}  {strategy_name:16} {symbol:10} "
                f"CAGR={rec.cagr*100:6.2f}% Sharpe={rec.sharpe:6.3f} "
                f"maxDD={rec.max_drawdown*100:5.1f}%  {rec.dsr_message}"
            )
            results.append({
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

    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "results": results,
    }, indent=2), encoding="utf-8")

    print()
    print(f"wrote {len(results)} records to {REGISTRY_PATH}")

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
        print(f"  {name:16} {n_pass}/{len(recs)} symbols passed ({rate*100:.0f}%) -> {verdict}")

    print(f"\nACCEPTED strategies: {accepted or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
