#!/usr/bin/env python
"""Train & validate the GradientBoostedReturnForecaster on REAL market data.

This is the data foundation for "real edge": it pulls real historical candles
from Angel One, persists them, trains ONE pooled model across the universe, runs
walk-forward validation, and ONLY saves the model if it shows genuine
out-of-sample edge (beats the naive baseline after the validation gate).

Turnkey usage (no env-var prefix needed — single-threaded BLAS is set in-script):

    # One command. 'auto' uses real Angel One candles if creds are in .env,
    # else cached CSVs, else synthetic. Sweeps horizons and saves the best one
    # that actually beats baseline out-of-sample:
    python -m scripts.train_return_forecaster --sweep --save

    # Explicit real-data run on a custom basket:
    python -m scripts.train_return_forecaster --source angel --days 750 --save \
        --symbols RELIANCE,TCS,INFY,HDFCBANK,ICICIBANK,SBIN,ITC,LT

    # Re-train from previously downloaded CSVs (no network):
    python -m scripts.train_return_forecaster --source csv --save

    # Honesty check on synthetic random-walk data (expected: REJECTED — no edge):
    python -m scripts.train_return_forecaster --source synthetic

The synthetic run is the honesty check: on a random walk the gate MUST reject the
model. If it ever "accepts" synthetic data, the validation is broken. A REJECTED
verdict on real data is also valuable — it means those features have no edge and
the system is correctly refusing to trade noise.
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Force single-threaded BLAS/OpenMP BEFORE numpy/sklearn are ever imported.
# sklearn's GradientBoosting + numpy can deadlock on import on some macOS Python
# builds; this makes the trainer "just run" without an env-var prefix.
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_v, "1")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("train_return_forecaster")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

HIST_DIR = REPO_ROOT / "data" / "historical"
MODEL_PATH = REPO_ROOT / "models" / "return_forecaster"

DEFAULT_SYMBOLS = [
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN",
    "ITC", "LT", "AXISBANK", "KOTAKBANK", "HINDUNILVR", "BHARTIARTL",
]


# ── Persistence: a simple CSV bar store (the DB has no bars table) ──────────────

def _csv_path(symbol: str, interval: str) -> Path:
    return HIST_DIR / f"{symbol}__{interval}.csv"


def save_bars_csv(symbol: str, interval: str, bars: list[dict]) -> None:
    HIST_DIR.mkdir(parents=True, exist_ok=True)
    with _csv_path(symbol, interval).open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([b["ts"], b["open"], b["high"], b["low"], b["close"], b["volume"]])


def load_bars_csv(symbol: str, interval: str) -> list[dict]:
    path = _csv_path(symbol, interval)
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            out.append({
                "ts": row["timestamp"],
                "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
                "volume": float(row.get("volume", 0) or 0),
            })
    return out


def _marketbar_to_dict(bar) -> dict:
    return {
        "ts": bar.timestamp.isoformat(),
        "open": bar.open, "high": bar.high, "low": bar.low,
        "close": bar.close, "volume": bar.volume,
    }


# ── Data sources ────────────────────────────────────────────────────────────────

def fetch_angel(symbols: list[str], days: int, interval: str) -> dict[str, list[dict]]:
    from trading_platform.config import load_settings
    from trading_platform.data.angel_one_instruments import AngelOneInstrumentMasterProvider
    from trading_platform.data.angel_one_history import AngelOneHistoricalDataProvider

    settings = load_settings()
    if not settings.angel_one_configured:
        raise SystemExit("Angel One credentials are not configured in .env — cannot fetch real data.")

    provider = AngelOneInstrumentMasterProvider(settings)
    try:
        master = provider.load_cached()
    except FileNotFoundError:
        logger.info("Instrument cache missing — downloading instrument master ...")
        provider.refresh()
        master = provider.load_cached()

    history = AngelOneHistoricalDataProvider(settings)
    to_dt = datetime.now()
    from_dt = to_dt - timedelta(days=days)

    out: dict[str, list[dict]] = {}
    for sym in symbols:
        try:
            inst = master.get(sym)
        except KeyError:
            logger.warning("skip %s: not in instrument master", sym)
            continue
        try:
            bars = history.get_candles(inst, from_dt, to_dt, interval=interval)
        except Exception as exc:
            logger.warning("skip %s: candle fetch failed: %s", sym, exc)
            continue
        if len(bars) < 60:
            logger.warning("skip %s: only %d bars", sym, len(bars))
            continue
        dicts = [_marketbar_to_dict(b) for b in bars]
        save_bars_csv(sym, interval, dicts)
        out[sym] = dicts
        logger.info("fetched %-12s %4d bars", sym, len(dicts))
    return out


def fetch_csv(symbols: list[str] | None, interval: str) -> dict[str, list[dict]]:
    HIST_DIR.mkdir(parents=True, exist_ok=True)
    if not symbols:
        symbols = sorted({p.name.split("__")[0] for p in HIST_DIR.glob(f"*__{interval}.csv")})
    out: dict[str, list[dict]] = {}
    for sym in symbols:
        bars = load_bars_csv(sym, interval)
        if len(bars) >= 60:
            out[sym] = bars
            logger.info("loaded %-12s %4d bars", sym, len(bars))
    return out


def fetch_synthetic(symbols: list[str], days: int) -> dict[str, list[dict]]:
    from trading_platform.data.market_data import SyntheticDataProvider
    prov = SyntheticDataProvider()
    start = date.today() - timedelta(days=int(days * 1.5))
    out: dict[str, list[dict]] = {}
    for sym in symbols:
        base = SyntheticDataProvider._BASE_PRICES.get(sym, 1500.0)
        bars = prov.generate_daily_bars(sym, start, days=days, base_price=base)
        out[sym] = [_marketbar_to_dict(b) for b in bars]
    return out


# ── Main ────────────────────────────────────────────────────────────────────────

def _resolve_source(requested: str) -> str:
    """'auto' -> angel if creds configured, else cached CSVs, else synthetic."""
    if requested != "auto":
        return requested
    try:
        from trading_platform.config import load_settings
        if load_settings().angel_one_configured:
            logger.info("auto source: Angel One credentials found -> using REAL candles")
            return "angel"
    except Exception:
        pass
    if any(HIST_DIR.glob("*.csv")):
        logger.info("auto source: no creds, but cached candles exist -> using CSV cache")
        return "csv"
    logger.warning("auto source: no creds and no cache -> SYNTHETIC random walk "
                   "(expected verdict: REJECTED — there is no edge in noise)")
    return "synthetic"


def _train_and_report(bars_by_symbol: dict, horizon: int, save: bool) -> tuple[bool, dict]:
    """Train one pooled forecaster at `horizon`, print the verdict, optionally save."""
    from trading_platform.neural.return_forecaster import GradientBoostedReturnForecaster
    total = sum(len(v) for v in bars_by_symbol.values())
    logger.info("Training on %d symbols, %d total bars (horizon=%d) ...",
                len(bars_by_symbol), total, horizon)
    f = GradientBoostedReturnForecaster(horizon=horizon)
    accepted = f.train_pooled(bars_by_symbol)
    m = f.last_train_metrics or {}

    print("\n" + "=" * 60)
    print(f"WALK-FORWARD VALIDATION  (horizon={horizon})")
    print("=" * 60)
    if "oos_auc" in m:
        threshold = m.get("auc_threshold", 0.52)
        print(f"  out-of-sample AUC      : {m['oos_auc']:.4f}   (need > {threshold:.4f})")
        print(f"  out-of-sample accuracy : {m['oos_accuracy']:.4f}")
        print(f"  majority baseline      : {m['majority_baseline']:.4f}")
        print(f"  OOS samples            : {m['oos_samples']}")
        print(f"  symbols pooled         : {m.get('n_symbols', '?')}")
    print(f"  VERDICT                : {'ACCEPTED — real edge' if accepted else 'REJECTED — no validated edge'}")
    if m.get("reason"):
        print(f"  reason                 : {m['reason']}")
    print("=" * 60)

    if accepted and save:
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        f.save(str(MODEL_PATH))
        print(f"\n  Saved validated model -> {MODEL_PATH}_sklearn.pkl")
        print("  Restart the backend to activate it (it drives neural_direction_prob).")
    return accepted, m


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", choices=["auto", "angel", "csv", "synthetic"], default="auto",
                    help="auto = Angel One if creds present, else CSV cache, else synthetic")
    ap.add_argument("--symbols", default="", help="comma-separated; default = a large-cap basket")
    ap.add_argument("--days", type=int, default=750)
    ap.add_argument("--interval", default="ONE_DAY")
    ap.add_argument("--horizon", type=int, default=1, help="forward bars for the direction label")
    ap.add_argument("--sweep", action="store_true",
                    help="try horizons 1/3/5/10 and keep the best VALIDATED one")
    ap.add_argument("--save", action="store_true", help="persist the model if validation accepts it")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or DEFAULT_SYMBOLS
    source = _resolve_source(args.source)

    if source == "angel":
        bars_by_symbol = fetch_angel(symbols, args.days, args.interval)
    elif source == "csv":
        bars_by_symbol = fetch_csv([s for s in symbols] if args.symbols else None, args.interval)
    else:
        bars_by_symbol = fetch_synthetic(symbols, args.days)

    if not bars_by_symbol:
        logger.error("No data loaded. For real data run with --source angel (needs creds).")
        return 2

    horizons = [1, 3, 5, 10] if args.sweep else [args.horizon]
    results: list[tuple[int, bool, dict]] = []
    # When sweeping, only --save the single best validated horizon (not each).
    for h in horizons:
        accepted, m = _train_and_report(bars_by_symbol, h, save=args.save and not args.sweep)
        results.append((h, accepted, m))

    validated = [(h, m) for h, acc, m in results if acc]
    any_edge = bool(validated)

    if args.sweep:
        print("\n" + "#" * 60)
        print("SWEEP SUMMARY")
        for h, acc, m in results:
            auc = m.get("oos_auc")
            print(f"  horizon={h:<3} {'ACCEPTED' if acc else 'rejected'}"
                  + (f"  (AUC={auc:.4f})" if auc is not None else ""))
        if validated and args.save:
            best_h, _ = max(validated, key=lambda hm: hm[1].get("oos_auc", 0.0))
            print(f"\n  Best validated horizon = {best_h}. Saving it ...")
            _train_and_report(bars_by_symbol, best_h, save=True)
        print("#" * 60)

    if not any_edge:
        print("\nNo validated edge found. This is the system telling you the truth:")
        print("these features have no out-of-sample predictive power on this data.")
        print("Next: try --source angel with more --days, a --sweep over horizons,")
        print("or richer features — do NOT deploy a model that can't beat baseline.")
    return 0 if any_edge else 1


if __name__ == "__main__":
    raise SystemExit(main())
