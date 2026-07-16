"""Backtest the REAL defined-risk short-vol strategy (weekly NIFTY iron condor).

The vol-premium research showed IV > RV ~76% of the time (a real edge). But that
was an idealized IV-RV proxy. This backtests the ACTUAL tradeable implementation
— a defined-risk iron condor with discrete strikes and a hard loss cap — because
that's what would really trade, and its P&L can differ from the proxy.

Each week:
  * spot = NIFTY, IV = India VIX. Sell a ~1-SD strangle, buy wings `width` beyond
    for defined risk (iron condor). Legs priced by Black-Scholes with VIX as IV.
  * Net credit = short premiums - long premiums. Max loss = width - net credit.
  * Hold to expiry; realized P&L from where NIFTY actually settled, minus costs.
Risk: position sized so max loss per trade <= risk_budget of capital. This is the
honest, capped version — one crash can't wipe the account (unlike naked short vol).

Reports total/annualized return, Sharpe, win rate, worst trade, max drawdown.

Run:  python scripts/backtest_short_vol.py --sd 1.0 --width 300 --risk 0.02
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("bt_short_vol")

_SQRT2 = math.sqrt(2.0)


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def bs_call(S, K, T, sig, r=0.065):
    if T <= 0 or sig <= 0:
        return max(0.0, S - K)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)


def bs_put(S, K, T, sig, r=0.065):
    if T <= 0 or sig <= 0:
        return max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def fetch(days=900):
    from trading_platform.config import load_settings
    from trading_platform.data.angel_one_history import AngelOneHistoricalDataProvider
    from trading_platform.data.angel_one_instruments import AngelOneInstrumentMasterProvider
    from trading_platform.domain.enums import Exchange
    import dataclasses
    s = load_settings()
    master = AngelOneInstrumentMasterProvider(s).load_cached()
    h = AngelOneHistoricalDataProvider(s)
    to_dt = datetime.now(); from_dt = to_dt - timedelta(days=days)
    nifty = master.get("NIFTY")

    def series(tok):
        inst = dataclasses.replace(nifty, token=tok, exchange=Exchange("NSE"))
        return {b.timestamp.date(): b.close for b in h.get_candles(inst, from_dt, to_dt, "ONE_DAY")}
    vix = series("99926017"); spot = series("99926000")
    dates = sorted(set(vix) & set(spot))
    return np.array(dates), np.array([spot[d] for d in dates]), np.array([vix[d] for d in dates])


def run_config(dates, spot, vix, logret, sd, width, hold, risk, min_vrp, cost_pts, verbose=True):
    capital = 1_000_000.0
    equity = [capital]; trades = []; H = hold; i = 60
    while i < len(spot) - H:
        S = spot[i]; iv = vix[i] / 100.0
        rv20 = logret[i - 19:i + 1].std() * math.sqrt(252)
        vrp = vix[i] - rv20 * 100.0
        if vrp < min_vrp:
            i += 1; continue
        T = H / 252.0
        move = S * iv * math.sqrt(T)
        call_k = round((S + sd * move) / 50) * 50
        put_k = round((S - sd * move) / 50) * 50
        credit = (bs_call(S, call_k, T, iv) - bs_call(S, call_k + width, T, iv)
                  + bs_put(S, put_k, T, iv) - bs_put(S, put_k - width, T, iv))
        max_loss = width - credit
        if max_loss <= 0 or credit <= 0:
            i += 1; continue
        lot = 50; lots = max(0, int(capital * risk / (max_loss * lot)))
        if lots < 1:
            i += 1; continue
        Se = spot[i + H]
        payoff = (max(Se - call_k, 0) - max(Se - (call_k + width), 0)
                  + max(put_k - Se, 0) - max((put_k - width) - Se, 0))
        pnl = (credit - payoff - cost_pts) * lot * lots
        capital += pnl; equity.append(capital); trades.append(credit - payoff - cost_pts)
        i += H
    if len(trades) < 20:
        return None
    tr = np.array(trades); eq = np.array(equity)
    ppy = 252 / H; ret = tr / width
    years = len(dates) / 252
    cagr = ((eq[-1] / eq[0]) ** (1 / max(years, 0.1)) - 1) * 100
    peak = np.maximum.accumulate(eq); dd = ((eq - peak) / peak).min() * 100
    return {"trades": len(trades), "win": float((tr > 0).mean()) * 100,
            "cagr": cagr, "sharpe": float(ret.mean() / (ret.std() + 1e-12) * math.sqrt(ppy)),
            "dd": dd, "worst": float(tr.min()), "final": eq[-1]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sd", type=float, default=1.0)
    ap.add_argument("--width", type=float, default=300.0)
    ap.add_argument("--hold", type=int, default=5)
    ap.add_argument("--risk", type=float, default=0.02)
    ap.add_argument("--min-vrp", type=float, default=0.0)
    ap.add_argument("--cost-pts", type=float, default=4.0)
    ap.add_argument("--sweep", action="store_true", help="sweep the risk/return frontier")
    args = ap.parse_args()

    dates, spot, vix = fetch()
    if len(dates) < 200:
        logger.error("insufficient data"); return 2
    logret = np.zeros(len(spot)); logret[1:] = np.log(spot[1:] / spot[:-1])

    if args.sweep:
        logger.info("SWEEP | %d days (%s..%s) | width=%d hold=%dd", len(dates), dates[0], dates[-1],
                    int(args.width), args.hold)
        logger.info("  %-6s %-6s %-8s %6s %6s %6s %6s %7s", "sd", "risk", "minVRP", "trades", "win%", "CAGR%", "Shrp", "maxDD%")
        for sd in (0.75, 1.0, 1.25):
            for risk in (0.02, 0.05, 0.08):
                for mv in (0.0, 2.0):
                    r = run_config(dates, spot, vix, logret, sd, args.width, args.hold, risk, mv, args.cost_pts, False)
                    if r:
                        logger.info("  %-6.2f %-6.0f%% %-8.1f %6d %6.0f %6.1f %6.2f %7.1f",
                                    sd, risk * 100, mv, r["trades"], r["win"], r["cagr"], r["sharpe"], r["dd"])
        return 0

    logger.info("Short-vol iron-condor backtest | %d days (%s..%s) | SD=%.1f width=%d hold=%dd risk=%.0f%%",
                len(dates), dates[0], dates[-1], args.sd, args.width, args.hold, args.risk * 100)

    capital = 1_000_000.0
    equity = [capital]
    trades = []
    H = args.hold
    i = 60
    while i < len(spot) - H:
        S = spot[i]; iv = vix[i] / 100.0
        rv20 = logret[i - 19:i + 1].std() * math.sqrt(252)
        vrp = vix[i] - rv20 * 100.0    # vol-risk-premium in points
        if vrp < args.min_vrp:
            i += 1; continue
        T = H / 252.0
        move = S * iv * math.sqrt(T)                     # 1-SD expected move
        call_k = round((S + args.sd * move) / 50) * 50
        put_k = round((S - args.sd * move) / 50) * 50
        # iron condor: short strangle + long wings
        credit = (bs_call(S, call_k, T, iv) - bs_call(S, call_k + args.width, T, iv)
                  + bs_put(S, put_k, T, iv) - bs_put(S, put_k - args.width, T, iv))
        max_loss = args.width - credit
        if max_loss <= 0 or credit <= 0:
            i += 1; continue
        # size: lots so worst-case loss <= risk budget (lot size 50 for NIFTY)
        lot = 50
        risk_budget = capital * args.risk
        lots = max(0, int(risk_budget / (max_loss * lot)))
        if lots < 1:
            i += 1; continue
        # settle at expiry
        Se = spot[i + H]
        payoff = (max(Se - call_k, 0) - max(Se - (call_k + args.width), 0)
                  + max(put_k - Se, 0) - max((put_k - args.width) - Se, 0))
        pnl_pts = credit - payoff - args.cost_pts
        pnl = pnl_pts * lot * lots
        capital += pnl
        equity.append(capital)
        trades.append(pnl_pts)
        i += H
    if len(trades) < 20:
        logger.error("too few trades (%d)", len(trades)); return 2

    tr = np.array(trades)
    eq = np.array(equity)
    win = float((tr > 0).mean())
    ppy = 252 / H
    ret = tr / args.width                        # normalize to per-unit-risk return
    sharpe = float(ret.mean() / (ret.std() + 1e-12) * math.sqrt(ppy))
    total_ret = (eq[-1] / eq[0] - 1) * 100
    years = len(dates) / 252
    cagr = ((eq[-1] / eq[0]) ** (1 / max(years, 0.1)) - 1) * 100
    peak = np.maximum.accumulate(eq); dd = ((eq - peak) / peak).min() * 100

    logger.info("\n" + "=" * 64)
    logger.info("DEFINED-RISK SHORT-VOL (weekly NIFTY iron condor)")
    logger.info("  trades: %d | win rate: %.0f%%", len(trades), win * 100)
    logger.info("  total return: %+.1f%%  | CAGR: %+.1f%%  | Sharpe: %.2f", total_ret, cagr, sharpe)
    logger.info("  worst trade: %.1f pts | max drawdown: %.1f%%", tr.min(), dd)
    logger.info("  avg credit/trade: %.1f pts | avg P&L/trade: %+.1f pts", 0.0, tr.mean())
    logger.info("  final equity: %s (from 1,000,000)", f"{eq[-1]:,.0f}")
    logger.info("=" * 64)
    good = cagr > 8 and sharpe > 0.7 and dd > -25
    logger.info("VERDICT: %s", (
        "VIABLE defined-risk short-vol — proceed to build the strategy"
        if good else "marginal — tune SD/width/risk or reconsider"))
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
