#!/usr/bin/env python
"""Train & validate the IntradayReturnForecaster on REAL 5-minute candles.

This is the earn-deployment gate for the intraday/microstructure avenue flagged
in the project's honesty log (daily-bar TA already showed ZERO out-of-sample edge;
see CLAUDE.md and the memory notes). It pulls real 5-min history from Angel One,
caches it, trains ONE pooled model across a liquid basket, runs walk-forward
validation, and ONLY saves the model if it clears the noise threshold
out-of-sample. A REJECTED verdict is a valid, honest outcome — it means intraday
TA has no edge on this data and the system correctly refuses to trade it.

Usage:

    # Sweep horizons (3/6/12 bars = 15/30/60 min) on real candles, keep+save the
    # best VALIDATED one. Needs Angel One creds in .env:
    python -m scripts.train_intraday_forecaster --sweep --save --days 60

    # Explicit single horizon on a custom basket:
    python -m scripts.train_intraday_forecaster --horizon 3 --days 45 --save \
        --symbols NIFTY,BANKNIFTY,RELIANCE,TCS,HDFCBANK

    # Honesty check on synthetic random walk (expected: REJECTED — no edge):
    python -m scripts.train_intraday_forecaster --source synthetic

Only when this script prints ACCEPTED and writes models/intraday_forecaster does
the live NeuralPredictionService load and serve it — and even then, directional
orders require AGENT_DIRECTIONAL_ENABLED=true. Both gates must pass.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Force single-threaded BLAS/OpenMP BEFORE numpy/sklearn import (macOS deadlock).
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_v, "1")

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("train_intraday_forecaster")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

CACHE = REPO_ROOT / "data" / "intraday_research"
CACHE.mkdir(parents=True, exist_ok=True)
MODEL_PATH = REPO_ROOT / "models" / "intraday_forecaster"

# Liquid names + indices — where any microstructure edge is most likely tradeable.
DEFAULT_SYMBOLS = [
    "NIFTY", "BANKNIFTY", "RELIANCE", "TCS", "INFY", "HDFCBANK",
    "ICICIBANK", "SBIN", "TATAMOTORS", "AXISBANK", "MARUTI", "ITC",
]
# AMXIDX spot tokens for indices (the WebSocket index tokens have no candles).
INDEX_TOKENS = {
    "NIFTY": ("99926000", "NSE"), "BANKNIFTY": ("99926009", "NSE"),
    "FINNIFTY": ("99926037", "NSE"), "MIDCPNIFTY": ("99926074", "NSE"),
}


# ── Data sources ────────────────────────────────────────────────────────────────

def fetch_angel(symbol: str, days: int, save_cache: bool) -> list[dict]:
    """5-min bars for one symbol as dicts with a datetime 'ts'. Cached per day-count."""
    cache_file = CACHE / f"{symbol}__FIVE_MINUTE_{days}d.npz"
    if cache_file.exists():
        z = np.load(cache_file, allow_pickle=True)
        return list(z["bars"])

    import dataclasses
    from trading_platform.config import load_settings
    from trading_platform.data.angel_one_history import AngelOneHistoricalDataProvider
    from trading_platform.data.angel_one_instruments import AngelOneInstrumentMasterProvider
    from trading_platform.domain.enums import Exchange

    settings = load_settings()
    if not settings.angel_one_configured:
        raise SystemExit("Angel One credentials not configured in .env — cannot fetch real candles.")
    master = AngelOneInstrumentMasterProvider(settings).load_cached()
    hist = AngelOneHistoricalDataProvider(settings)
    inst = master.get(symbol)
    if symbol in INDEX_TOKENS:
        tok, exch = INDEX_TOKENS[symbol]
        inst = dataclasses.replace(inst, token=tok, exchange=Exchange(exch))
    bars_raw = hist.get_candles(
        inst, datetime.now() - timedelta(days=days), datetime.now(), interval="FIVE_MINUTE"
    )
    bars = [
        {"ts": b.timestamp, "open": b.open, "high": b.high,
         "low": b.low, "close": b.close, "volume": b.volume}
        for b in bars_raw
    ]
    if save_cache and bars:
        np.savez_compressed(cache_file, bars=np.array(bars, dtype=object))
    return bars


def fetch_synthetic(symbol: str, days: int) -> list[dict]:
    """Synthetic intraday random walk — the honesty check (must be REJECTED)."""
    rng = np.random.default_rng(abs(hash(symbol)) % (2**32))
    bars_per_day = 75              # ~09:15–15:30 in 5-min bars
    price = 100.0 + (abs(hash(symbol)) % 100)
    out: list[dict] = []
    start = datetime.now() - timedelta(days=int(days * 1.5))
    d = 0
    made_days = 0
    while made_days < days:
        day = start + timedelta(days=d)
        d += 1
        if day.weekday() >= 5:
            continue
        made_days += 1
        t = day.replace(hour=9, minute=15, second=0, microsecond=0)
        for _ in range(bars_per_day):
            ret = rng.normal(0, 0.001)          # zero-drift → no edge
            o = price
            price = max(price * (1 + ret), 1e-3)
            hi = max(o, price) * (1 + abs(rng.normal(0, 0.0005)))
            lo = min(o, price) * (1 - abs(rng.normal(0, 0.0005)))
            out.append({"ts": t, "open": o, "high": hi, "low": lo,
                        "close": price, "volume": float(rng.integers(1000, 5000))})
            t += timedelta(minutes=5)
    return out


# ── Main ────────────────────────────────────────────────────────────────────────

def _resolve_source(requested: str) -> str:
    if requested != "auto":
        return requested
    try:
        from trading_platform.config import load_settings
        if load_settings().angel_one_configured:
            logger.info("auto source: Angel One credentials found -> using REAL 5-min candles")
            return "angel"
    except Exception:
        pass
    logger.warning("auto source: no creds -> SYNTHETIC random walk (expected verdict: REJECTED).")
    return "synthetic"


def _load_basket(symbols: list[str], source: str, days: int, save_cache: bool) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for sym in symbols:
        try:
            bars = fetch_angel(sym, days, save_cache) if source == "angel" else fetch_synthetic(sym, days)
        except SystemExit:
            raise
        except Exception as exc:
            logger.warning("skip %s: %s", sym, str(exc)[:100])
            continue
        if len(bars) < 200:
            logger.warning("skip %s: only %d bars", sym, len(bars))
            continue
        out[sym] = bars
        logger.info("loaded %-11s %5d 5-min bars", sym, len(bars))
    return out


def _train_and_report(bars_by_symbol: dict, horizon: int, save: bool) -> tuple[bool, dict]:
    from trading_platform.neural.intraday_forecaster import IntradayReturnForecaster, FEATURE_NAMES
    total = sum(len(v) for v in bars_by_symbol.values())
    logger.info("Training intraday model on %d symbols, %d total bars (horizon=%d bars / %dmin) ...",
                len(bars_by_symbol), total, horizon, horizon * 5)
    f = IntradayReturnForecaster(horizon=horizon)
    accepted = f.train_pooled(bars_by_symbol)
    m = f.last_train_metrics or {}

    print("\n" + "=" * 62)
    print(f"INTRADAY WALK-FORWARD VALIDATION  (horizon={horizon} bars / {horizon*5}min)")
    print("=" * 62)
    if "oos_auc" in m:
        print(f"  out-of-sample AUC      : {m['oos_auc']:.4f}   (need > {m.get('auc_threshold', 0.52):.4f})")
        print(f"  out-of-sample accuracy : {m['oos_accuracy']:.4f}")
        print(f"  majority baseline      : {m['majority_baseline']:.4f}")
        print(f"  OOS samples            : {m['oos_samples']}")
        print(f"  symbols pooled         : {m.get('n_symbols', '?')}  |  features: {len(FEATURE_NAMES)}")
    print(f"  VERDICT                : {'ACCEPTED — real intraday edge' if accepted else 'REJECTED — no validated edge'}")
    if m.get("reason"):
        print(f"  reason                 : {m['reason']}")
    print("=" * 62)

    if accepted and save:
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        f.save(str(MODEL_PATH))
        print(f"\n  Saved validated model -> {MODEL_PATH}_sklearn.pkl")
        print("  Restart the backend to load it. It then drives neural_direction_prob")
        print("  ONLY when fed intraday bars AND AGENT_DIRECTIONAL_ENABLED=true.")
    return accepted, m


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", choices=["auto", "angel", "synthetic"], default="auto")
    ap.add_argument("--symbols", default="", help="comma-separated; default = liquid basket")
    ap.add_argument("--days", type=int, default=45, help="calendar days of 5-min history")
    ap.add_argument("--horizon", type=int, default=3, help="forward bars for the label (3=15min)")
    ap.add_argument("--sweep", action="store_true", help="try horizons 3/6/12 and keep the best VALIDATED one")
    ap.add_argument("--save", action="store_true", help="persist the model if validation accepts it")
    ap.add_argument("--save-cache", action="store_true", help="cache fetched candles to disk")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or DEFAULT_SYMBOLS
    source = _resolve_source(args.source)
    bars_by_symbol = _load_basket(symbols, source, args.days, args.save_cache)
    if not bars_by_symbol:
        logger.error("No data loaded. For real data run with --source angel (needs creds).")
        return 2

    horizons = [3, 6, 12] if args.sweep else [args.horizon]
    results: list[tuple[int, bool, dict]] = []
    for h in horizons:
        accepted, m = _train_and_report(bars_by_symbol, h, save=args.save and not args.sweep)
        results.append((h, accepted, m))

    validated = [(h, m) for h, acc, m in results if acc]
    any_edge = bool(validated)

    if args.sweep:
        print("\n" + "#" * 62)
        print("SWEEP SUMMARY")
        for h, acc, m in results:
            auc = m.get("oos_auc")
            print(f"  horizon={h:<3} {'ACCEPTED' if acc else 'rejected'}"
                  + (f"  (AUC={auc:.4f})" if auc is not None else ""))
        if validated and args.save:
            best_h, _ = max(validated, key=lambda hm: hm[1].get("oos_auc", 0.0))
            print(f"\n  Best validated horizon = {best_h}. Saving it ...")
            _train_and_report(bars_by_symbol, best_h, save=True)
        print("#" * 62)

    if not any_edge:
        print("\nNo validated intraday edge found. This is the system telling the truth:")
        print("these 5-min microstructure features have no OOS predictive power here.")
        print("Next: try more --days, a --sweep over horizons, or richer features —")
        print("do NOT deploy a model that can't beat baseline.")
    return 0 if any_edge else 1


if __name__ == "__main__":
    raise SystemExit(main())
