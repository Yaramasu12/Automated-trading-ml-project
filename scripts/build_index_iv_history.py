#!/usr/bin/env python3
"""Build per-index implied-volatility history from NSE's FREE daily F&O bhavcopy.

Why this exists: the short-vol backtester prices every index's options off ONE
implied-vol series. India VIX is NIFTY's IV, so feeding it to BANKNIFTY /
FINNIFTY (which realize ~24% vs NIFTY's ~17%) sells strikes far too tight and
manufactures a loser by construction -- the earlier non-NIFTY backtests were an
artifact. NSE publishes the settlement price of EVERY option contract each
day, from which each index's own implied vol can be recovered exactly.

The fetch/parse/ATM-extraction logic lives in trading_platform/research/
index_premium.py, shared with the daily_scheduler.py "premium_monitor_update"
job (today's reading only) so both build the number the same way.

    python scripts/build_index_iv_history.py --start 2019-03-11
    python scripts/build_index_iv_history.py --build-series     # after download

Resumable: processed dates are recorded, so re-running only fetches what is
missing. Raw zips are not kept -- only the extracted per-expiry ATM rows.
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from trading_platform.research.index_premium import (  # noqa: E402
    INDICES,
    atm_rows,
    fetch_zip,
    iv30_for_day,
    parse_index_options,
)

OUT = REPO / "data" / "historical" / "iv"
ROWS_CSV = OUT / "atm_by_expiry.csv"
DONE_TXT = OUT / "_done_dates.txt"
FIELDS = ["date", "index", "expiry", "dte", "strike", "forward", "spot_parity",
          "call", "put", "iv", "iv_put95", "iv_call105", "vol_ce", "vol_pe",
          "oi_ce", "oi_pe", "lot", "undrlyg"]

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
    by_day: dict[str, list[dict]] = {}
    with open(ROWS_CSV, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            by_day.setdefault(r["date"], []).append(r)
    series: dict[str, dict[str, tuple[float, float]]] = {i: {} for i in INDICES}
    for day, rows in by_day.items():
        for idx, (iv30, spot) in iv30_for_day(rows).items():
            series.setdefault(idx, {})[day] = (iv30, spot)
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
            for day in sorted(series[idx]):
                v = series[idx][day][1]
                w.writerow([f"{day}T00:00:00+05:30", v, v, v, v, 0])
        days = sorted(series[idx])
        print(f"{idx:11} {len(days):5d} days  {days[0]} .. {days[-1]}  "
              f"mean IV30 {sum(series[idx][d][0] for d in days) / len(days):5.2f}%")
    # sanity check the method on NIFTY against India VIX
    vix_path = hist / "INDIAVIX__ONE_DAY_deep.csv"
    if vix_path.exists() and series["NIFTY"]:
        import csv as _csv
        vix = {}
        with open(vix_path, newline="", encoding="utf-8") as fh:
            for r in _csv.DictReader(fh):
                vix[r["timestamp"][:10]] = float(r["close"])
        pairs = [(series["NIFTY"][d][0], vix[d]) for d in series["NIFTY"] if d in vix]
        if len(pairs) > 30:
            xs, ys = zip(*pairs)
            mx, my = statistics.mean(xs), statistics.mean(ys)
            cov = sum((x - mx) * (y - my) for x, y in pairs)
            corr = cov / ((sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)) ** 0.5)
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
