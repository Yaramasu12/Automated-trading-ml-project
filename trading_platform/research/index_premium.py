"""Per-index implied volatility from NSE's free daily F&O bhavcopy.

Shared by scripts/build_index_iv_history.py (bulk historical backfill) and
the daily_scheduler.py "premium_monitor_update" job (today's reading only) —
one implementation, so the live monitor's numbers are built the same way as
the historical series they get compared against.

Method: for each (day, index, expiry), find the strike where |call - put| is
smallest, imply the forward from put-call parity (F = K + (C-P)*e^{rT}), then
invert Black-76 for the ATM implied vol. NO realized-vol proxying — that
would manufacture the very premium being measured. A 30-day constant-
maturity IV is built by total-variance interpolation between the bracketing
expiries, the like-for-like analogue of India VIX for non-NIFTY indices.

REPORTING ONLY: nothing in this module is wired into any order-submission or
entry-decision path. See runtime.py::premium_monitor_status() and daily_
scheduler.py::run_premium_monitor_update() for the two real callers.
"""
from __future__ import annotations

import csv
import io
import math
import statistics
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime

INDICES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")
R = 0.065                      # matches short_vol_backtest.RISK_FREE_RATE
UDIFF_CUTOVER = date(2024, 7, 8)
_MON = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ─── Black-76 ────────────────────────────────────────────────────────────────

def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def b76(f: float, k: float, t: float, sig: float, is_call: bool) -> float:
    if sig <= 0 or t <= 0:
        return max(0.0, (f - k) if is_call else (k - f)) * math.exp(-R * t)
    sd = sig * math.sqrt(t)
    d1 = (math.log(f / k) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    df = math.exp(-R * t)
    if is_call:
        return df * (f * _phi(d1) - k * _phi(d2))
    return df * (k * _phi(-d2) - f * _phi(-d1))


def implied_vol(price: float, f: float, k: float, t: float, is_call: bool) -> float | None:
    """Bisection Black-76 inversion. None when the price is outside no-arbitrage
    bounds (settlement prices of illiquid strikes sometimes are)."""
    df = math.exp(-R * t)
    intrinsic = df * max(0.0, (f - k) if is_call else (k - f))
    if price <= intrinsic + 1e-6 or price >= df * (f if is_call else k):
        return None
    lo, hi = 0.005, 4.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if b76(f, k, t, mid, is_call) > price:
            hi = mid
        else:
            lo = mid
    v = 0.5 * (lo + hi)
    return v if 0.01 < v < 3.9 else None


# ─── Fetch + parse one day ───────────────────────────────────────────────────

def urls_for(d: date) -> list[str]:
    new = (f"https://nsearchives.nseindia.com/content/fo/"
           f"BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")
    old = (f"https://nsearchives.nseindia.com/content/historical/DERIVATIVES/"
           f"{d.year}/{_MON[d.month - 1]}/fo{d.day:02d}{_MON[d.month - 1]}{d.year}bhav.csv.zip")
    return [new, old] if d >= UDIFF_CUTOVER else [old, new]


def fetch_zip(d: date, retries: int = 3) -> bytes | None:
    """Bytes of the day's bhavcopy zip, or None when NSE has no file (holiday)."""
    for url in urls_for(d):
        for attempt in range(retries):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": _UA, "Referer": "https://www.nseindia.com/"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    break
                if exc.code in (403, 429):
                    time.sleep(5 * (attempt + 1))
                    continue
                time.sleep(2 * (attempt + 1))
            except Exception:
                time.sleep(2 * (attempt + 1))
        else:
            raise RuntimeError(f"NSE fetch failed repeatedly for {d} ({url})")
    return None


def parse_index_options(raw_zip: bytes) -> list[dict]:
    """Index-option rows from either bhavcopy format, normalised."""
    zf = zipfile.ZipFile(io.BytesIO(raw_zip))
    lines = zf.read(zf.namelist()[0]).decode("utf-8", "replace").splitlines()
    if not lines:
        return []
    header = lines[0]
    new_fmt = "TckrSymb" in header
    keep = [ln for ln in lines[1:] if (",IDO," in ln if new_fmt else "OPTIDX" in ln)]
    rd = csv.DictReader(io.StringIO(header + "\n" + "\n".join(keep)))
    out: list[dict] = []
    for r in rd:
        try:
            if new_fmt:
                sym, exp = r["TckrSymb"], datetime.strptime(r["XpryDt"], "%Y-%m-%d").date()
                strike, ot = float(r["StrkPric"]), r["OptnTp"]
                px, vol, oi = float(r["SttlmPric"]), float(r["TtlTradgVol"] or 0), float(r["OpnIntrst"] or 0)
                lot, und = int(float(r.get("NewBrdLotQty") or 0)), float(r.get("UndrlygPric") or 0)
            else:
                sym, exp = r["SYMBOL"], datetime.strptime(r["EXPIRY_DT"], "%d-%b-%Y").date()
                strike, ot = float(r["STRIKE_PR"]), r["OPTION_TYP"]
                px, vol, oi = float(r["SETTLE_PR"]), float(r["CONTRACTS"] or 0), float(r["OPEN_INT"] or 0)
                lot, und = 0, 0.0
        except (KeyError, ValueError):
            continue
        if sym in INDICES and ot in ("CE", "PE") and px > 0:
            out.append(dict(sym=sym, exp=exp, k=strike, ot=ot, px=px, vol=vol, oi=oi, lot=lot, und=und))
    return out


def atm_rows(d: date, opts: list[dict]) -> list[dict]:
    """One ATM-implied-vol row per (index, expiry) for the day, DTE 1-75,
    at most 4 nearest expiries per index."""
    by: dict[tuple[str, date], dict[float, dict]] = {}
    for o in opts:
        by.setdefault((o["sym"], o["exp"]), {}).setdefault(o["k"], {})[o["ot"]] = o
    rows: list[dict] = []
    per_index: dict[str, int] = {}
    for (sym, exp) in sorted(by, key=lambda x: (x[0], x[1])):
        dte = (exp - d).days
        if dte < 1 or dte > 75 or per_index.get(sym, 0) >= 4:
            continue
        strikes = by[(sym, exp)]
        both = {k: v for k, v in strikes.items() if "CE" in v and "PE" in v}
        if len(both) < 3:
            continue
        t = dte / 365.0
        k_atm = min(both, key=lambda k: abs(both[k]["CE"]["px"] - both[k]["PE"]["px"]))
        c, p = both[k_atm]["CE"], both[k_atm]["PE"]
        fwd = k_atm + (c["px"] - p["px"]) * math.exp(R * t)
        if fwd <= 0:
            continue
        ivs = [v for v in (implied_vol(c["px"], fwd, k_atm, t, True),
                           implied_vol(p["px"], fwd, k_atm, t, False)) if v is not None]
        if not ivs:
            continue
        iv = sum(ivs) / len(ivs)

        def otm_iv(target_m: float, is_call: bool) -> float | None:
            ks = [k for k, v in strikes.items() if ("CE" if is_call else "PE") in v]
            if not ks:
                return None
            k = min(ks, key=lambda x: abs(x - fwd * target_m))
            if abs(k / fwd - target_m) > 0.03:
                return None
            o = strikes[k]["CE" if is_call else "PE"]
            return implied_vol(o["px"], fwd, k, t, is_call)

        rows.append({
            "date": d.isoformat(), "index": sym, "expiry": exp.isoformat(), "dte": dte,
            "strike": k_atm, "forward": round(fwd, 2),
            "spot_parity": round(fwd * math.exp(-R * t), 2),
            "call": c["px"], "put": p["px"], "iv": round(iv, 5),
            "iv_put95": (lambda v: "" if v is None else round(v, 5))(otm_iv(0.95, False)),
            "iv_call105": (lambda v: "" if v is None else round(v, 5))(otm_iv(1.05, True)),
            "vol_ce": c["vol"], "vol_pe": p["vol"], "oi_ce": c["oi"], "oi_pe": p["oi"],
            "lot": c["lot"], "undrlyg": c["und"],
        })
        per_index[sym] = per_index.get(sym, 0) + 1
    return rows


# ─── 30-day constant-maturity interpolation ─────────────────────────────────

def iv30_for_day(rows: list[dict]) -> dict[str, tuple[float, float]]:
    """rows: this ONE day's atm_rows() output (any indices mixed).
    Returns index -> (iv30_pct, spot_parity), total-variance-interpolated
    to a 30-day constant maturity from the bracketing expiries."""
    by_index: dict[str, list[dict]] = {}
    for r in rows:
        by_index.setdefault(r["index"], []).append(r)
    out: dict[str, tuple[float, float]] = {}
    for idx, idx_rows in by_index.items():
        usable = sorted((r for r in idx_rows if int(r["dte"]) >= 2), key=lambda r: int(r["dte"]))
        if not usable:
            continue
        below = [r for r in usable if int(r["dte"]) <= 30]
        above = [r for r in usable if int(r["dte"]) > 30]
        if below and above:
            a, b = below[-1], above[0]
            t1, t2 = int(a["dte"]) / 365, int(b["dte"]) / 365
            v1, v2 = float(a["iv"]), float(b["iv"])
            t = 30 / 365
            w = (t2 - t) / (t2 - t1)
            var = (w * t1 * v1 * v1 + (1 - w) * t2 * v2 * v2) / t
            iv30 = math.sqrt(max(var, 1e-12))
        else:
            iv30 = float((below[-1] if below else above[0])["iv"])
        out[idx] = (iv30 * 100.0, float(usable[0]["spot_parity"]))
    return out


# ─── Realized vol / premium (shared by the daily job and DB backfill) ───────

def compute_reading(
    trade_date: date, iv30: float, spot: float,
    prior_spots: list[tuple[date, float]], prior_premiums: list[float],
) -> tuple[float | None, float | None, float | None]:
    """(realized_vol20, premium, trailing_median_60d) for one (index, day).

    prior_spots / prior_premiums: that index's own history strictly BEFORE
    trade_date, ascending by date -- the spot series is the one this monitor
    itself recovers from the option chain (put-call parity), so there is no
    separate price-feed dependency. Needs >=15 realized-vol observations (of
    a 20-day window) and >=15 premium observations (of a 60-day window)
    before reporting a number, rather than a noisy read on day 2.
    """
    spots = prior_spots[-20:] + [(trade_date, spot)]
    realized = None
    if len(spots) >= 15:
        rets = [math.log(spots[i][1] / spots[i - 1][1]) for i in range(1, len(spots))]
        realized = statistics.pstdev(rets) * math.sqrt(252) * 100
    premium = (iv30 - realized) if realized is not None else None
    hist = prior_premiums[-59:] + ([premium] if premium is not None else [])
    trailing_median = statistics.median(hist) if len(hist) >= 15 else None
    return realized, premium, trailing_median
