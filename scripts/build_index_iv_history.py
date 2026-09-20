#!/usr/bin/env python3
"""Build per-index implied-volatility history from NSE's FREE daily F&O bhavcopy.

Why this exists: the short-vol backtester prices every index's options off ONE
implied-vol series. India VIX is NIFTY's IV, so feeding it to BANKNIFTY /
FINNIFTY (which realize ~24% vs NIFTY's ~17%) sells strikes far too tight and
manufactures a loser by construction -- the earlier non-NIFTY backtests were an
artifact, and the project notes concluded per-index IV "cannot be back-filled
from any free source". That was wrong: NSE publishes the settlement price of
EVERY option contract each day, from which each index's own implied vol can be
recovered exactly.

Method (no realized-vol proxying -- that would manufacture the very edge under
test): for each (day, index, expiry) take the strike where |call - put| is
smallest, imply the forward from put-call parity (F = K + (C-P)*e^{rT}), then
invert Black-76 for the ATM implied vol. A 30-day constant-maturity IV series
is built by total-variance interpolation between bracketing expiries -- the
like-for-like analogue of India VIX for NIFTY.

    python scripts/build_index_iv_history.py --start 2019-03-11
    python scripts/build_index_iv_history.py --build-series     # after download

Resumable: processed dates are recorded, so re-running only fetches what is
missing. Raw zips are not kept -- only the extracted per-expiry ATM rows.
"""
from __future__ import annotations

import argparse
import csv
import io
import math
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "data" / "historical" / "iv"
ROWS_CSV = OUT / "atm_by_expiry.csv"
DONE_TXT = OUT / "_done_dates.txt"

INDICES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")
R = 0.065                      # matches short_vol_backtest.RISK_FREE_RATE
UDIFF_CUTOVER = date(2024, 7, 8)
MON = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
FIELDS = ["date", "index", "expiry", "dte", "strike", "forward", "spot_parity",
          "call", "put", "iv", "iv_put95", "iv_call105", "vol_ce", "vol_pe",
          "oi_ce", "oi_pe", "lot", "undrlyg"]


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
           f"{d.year}/{MON[d.month - 1]}/fo{d.day:02d}{MON[d.month - 1]}{d.year}bhav.csv.zip")
    return [new, old] if d >= UDIFF_CUTOVER else [old, new]


def fetch_zip(d: date, retries: int = 3) -> bytes | None:
    """Bytes of the day's bhavcopy zip, or None when NSE has no file (holiday)."""
    saw_404 = 0
    for url in urls_for(d):
        for attempt in range(retries):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": UA, "Referer": "https://www.nseindia.com/"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    saw_404 += 1
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
    """One ATM-implied-vol row per (index, expiry) for the day."""
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


# ─── Download driver ─────────────────────────────────────────────────────────

_lock = threading.Lock()


def process_day(d: date, delay: float) -> tuple[date, str, int]:
    time.sleep(delay)
    raw = fetch_zip(d)
    if raw is None:
        return d, "H", 0
    rows = atm_rows(d, parse_index_options(raw))
    with _lock:
        new_file = not ROWS_CSV.exists()
        with open(ROWS_CSV, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS)
            if new_file:
                w.writeheader()
            w.writerows(rows)
    return d, "D", len(rows)


def download(start: date, end: date, workers: int, delay: float) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    done = set()
    if DONE_TXT.exists():
        done = {ln.split()[0] for ln in DONE_TXT.read_text().splitlines() if ln.strip()}
    days = [start + timedelta(n) for n in range((end - start).days + 1)]
    days = [d for d in days if d.weekday() < 5 and d.isoformat() not in done]
    print(f"{len(days)} weekdays to fetch ({len(done)} already done)", flush=True)
    n_done = n_rows = n_hol = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool, open(DONE_TXT, "a") as done_fh:
        # submit in chunks so an abort on repeated failures is prompt
        for i in range(0, len(days), workers * 8):
            futs = [pool.submit(process_day, d, delay) for d in days[i:i + workers * 8]]
            for f in futs:
                d, kind, n = f.result()
                done_fh.write(f"{d.isoformat()} {kind}\n"); done_fh.flush()
                n_done += 1; n_rows += n; n_hol += kind == "H"
            if (i // (workers * 8)) % 5 == 0:
                rate = n_done / max(time.time() - t0, 1e-9)
                print(f"  {n_done}/{len(days)} days ({n_hol} holidays), {n_rows} rows, "
                      f"{rate:.1f} days/s, last {days[min(i + workers * 8, len(days)) - 1]}", flush=True)
    print(f"downloaded {n_done} days, {n_rows} rows", flush=True)
    return 0


# ─── 30-day constant-maturity series + sanity check ──────────────────────────

def build_series() -> int:
    if not ROWS_CSV.exists():
        print("no atm_by_expiry.csv yet — run the download first", file=sys.stderr)
        return 2
    by: dict[tuple[str, str], list[dict]] = {}
    with open(ROWS_CSV, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            by.setdefault((r["index"], r["date"]), []).append(r)
    series: dict[str, dict[str, tuple[float, float]]] = {i: {} for i in INDICES}
    spot: dict[str, dict[str, float]] = {i: {} for i in INDICES}
    for (idx, day), rows in by.items():
        usable = sorted((r for r in rows if int(r["dte"]) >= 2), key=lambda r: int(r["dte"]))
        if not usable:
            continue
        below = [r for r in usable if int(r["dte"]) <= 30]
        above = [r for r in usable if int(r["dte"]) > 30]
        if below and above:
            a, b = below[-1], above[0]
            t1, t2, v1, v2 = int(a["dte"]) / 365, int(b["dte"]) / 365, float(a["iv"]), float(b["iv"])
            t = 30 / 365
            w = (t2 - t) / (t2 - t1)
            var = (w * t1 * v1 * v1 + (1 - w) * t2 * v2 * v2) / t
            iv30 = math.sqrt(max(var, 1e-12))
        else:
            iv30 = float((below[-1] if below else above[0])["iv"])
        skews = [float(r["iv_put95"]) - float(r["iv_call105"]) for r in usable[:2]
                 if r["iv_put95"] != "" and r["iv_call105"] != ""]
        series[idx][day] = (iv30 * 100.0, (sum(skews) / len(skews) * 100.0) if skews else float("nan"))
        spot[idx][day] = float(usable[0]["spot_parity"])
    hist = REPO / "data" / "historical"
    for idx in INDICES:
        if not series[idx]:
            continue
        with open(hist / f"{idx}__IV30_deep.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh); w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
            for day in sorted(series[idx]):
                v = round(series[idx][day][0], 4)
                w.writerow([f"{day}T00:00:00+05:30", v, v, v, v, 0])
        with open(hist / f"{idx}__PARITY_SPOT_deep.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh); w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
            for day in sorted(spot[idx]):
                v = spot[idx][day]
                w.writerow([f"{day}T00:00:00+05:30", v, v, v, v, 0])
        days = sorted(series[idx])
        print(f"{idx:11} {len(days):5d} days  {days[0]} .. {days[-1]}  "
              f"mean IV30 {sum(series[idx][d][0] for d in days) / len(days):5.2f}%")
    # sanity check the method on NIFTY against India VIX
    vix_path = hist / "INDIAVIX__ONE_DAY_deep.csv"
    if vix_path.exists() and series["NIFTY"]:
        vix = {}
        with open(vix_path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                vix[r["timestamp"][:10]] = float(r["close"])
        pairs = [(series["NIFTY"][d][0], vix[d]) for d in series["NIFTY"] if d in vix]
        if len(pairs) > 30:
            xs, ys = zip(*pairs)
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            cov = sum((x - mx) * (y - my) for x, y in pairs)
            corr = cov / math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
            print(f"\nSANITY: NIFTY own-chain IV30 vs India VIX over {len(pairs)} days: "
                  f"corr {corr:.3f}, mean own {mx:.2f}% vs VIX {my:.2f}% (diff {mx - my:+.2f} pts)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2019-03-11")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--delay", type=float, default=0.15, help="per-request sleep, seconds")
    ap.add_argument("--build-series", action="store_true", help="only (re)build the IV30 CSVs")
    args = ap.parse_args()
    if args.build_series:
        return build_series()
    rc = download(date.fromisoformat(args.start), date.fromisoformat(args.end), args.workers, args.delay)
    return rc or build_series()


if __name__ == "__main__":
    raise SystemExit(main())
