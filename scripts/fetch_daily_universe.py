"""Fetch daily candles for a broad NSE F&O universe → data/historical cache.

Populates the cross-sectional research universe (research_cross_sectional.py reads
every *__ONE_DAY.csv here). Rate-limited and resumable: already-cached symbols are
skipped, and a rate-limit response triggers a cooldown rather than a login storm.
"""
from __future__ import annotations

import csv
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("fetch_universe")
HIST = Path(__file__).resolve().parent.parent / "data" / "historical"
HIST.mkdir(parents=True, exist_ok=True)

# ~100 liquid NSE F&O single stocks (broad sector spread for cross-sectional breadth).
UNIVERSE = [
    "RELIANCE","TCS","INFY","HDFCBANK","ICICIBANK","SBIN","AXISBANK","KOTAKBANK",
    "BHARTIARTL","ITC","LT","HINDUNILVR","BAJFINANCE","BAJAJFINSV","MARUTI","M&M",
    "TATAMOTORS","SUNPHARMA","WIPRO","HCLTECH","TECHM","LTIM","ASIANPAINT","TITAN",
    "NTPC","POWERGRID","ONGC","COALINDIA","BPCL","IOC","GAIL","TATAPOWER",
    "TATASTEEL","JSWSTEEL","HINDALCO","VEDL","JINDALSTEL","NMDC","SAIL","NATIONALUM",
    "ULTRACEMCO","GRASIM","SHREECEM","AMBUJACEM","ACC","DALBHARAT","RAMCOCEM",
    "CIPLA","DRREDDY","DIVISLAB","APOLLOHOSP","LUPIN","AUROPHARMA","BIOCON","ALKEM",
    "HEROMOTOCO","BAJAJ-AUTO","EICHERMOT","TVSMOTOR","BOSCHLTD","MOTHERSON","BALKRISIND",
    "NESTLEIND","BRITANNIA","TATACONSUM","DABUR","MARICO","GODREJCP","COLPAL","UBL",
    "HDFCLIFE","SBILIFE","ICICIPRULI","ICICIGI","BAJAJHLDNG","MUTHOOTFIN","CHOLAFIN",
    "PNB","BANKBARODA","CANBK","FEDERALBNK","IDFCFIRSTB","AUBANK","BANDHANBNK",
    "ADANIENT","ADANIPORTS","ADANIGREEN","ADANIPOWER","DLF","GODREJPROP","OBEROIRLTY",
    "TRENT","DMART","PIDILITIND","BERGEPAINT","SRF","PIIND","UPL","AARTIIND",
    "INDUSINDBK","SHRIRAMFIN","IRCTC","INDIGO","NAUKRI","PERSISTENT","COFORGE","MPHASIS",
]


def main() -> int:
    from trading_platform.config import load_settings
    from trading_platform.data.angel_one_history import AngelOneHistoricalDataProvider
    from trading_platform.data.angel_one_instruments import AngelOneInstrumentMasterProvider

    settings = load_settings()
    if not settings.angel_one_configured:
        logger.error("Angel One creds not configured."); return 2
    master = AngelOneInstrumentMasterProvider(settings).load_cached()
    hist = AngelOneHistoricalDataProvider(settings)
    to_dt = datetime.now(); from_dt = to_dt - timedelta(days=900)   # ~3.5y

    ok = skip = fail = 0
    cooldown_until = 0.0
    for i, sym in enumerate(UNIVERSE, 1):
        path = HIST / f"{sym}__ONE_DAY.csv"
        if path.exists() and path.stat().st_size > 5000:
            skip += 1; continue
        # resolve a real cash instrument (equity token needed for candles)
        inst = None
        for cand in (sym, f"{sym}-EQ"):
            try:
                inst = master.get(cand); break
            except Exception:
                continue
        if inst is None:
            logger.info("  [%d/%d] %s: not in master", i, len(UNIVERSE), sym); fail += 1; continue
        now = time.monotonic()
        if now < cooldown_until:
            time.sleep(cooldown_until - now)
        try:
            bars = hist.get_candles(inst, from_dt, to_dt, interval="ONE_DAY")
        except Exception as e:
            msg = str(e)
            if "rate" in msg.lower() or "exceeding" in msg.lower():
                cooldown_until = time.monotonic() + 30.0
                logger.info("  [%d/%d] %s: rate-limited, cooling 30s", i, len(UNIVERSE), sym)
            else:
                logger.info("  [%d/%d] %s: %s", i, len(UNIVERSE), sym, msg[:50])
            fail += 1
            time.sleep(0.5)
            continue
        if len(bars) < 200:
            logger.info("  [%d/%d] %s: only %d bars", i, len(UNIVERSE), sym, len(bars)); fail += 1; continue
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh); w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
            for b in bars:
                w.writerow([b.timestamp.isoformat(), b.open, b.high, b.low, b.close, b.volume])
        ok += 1
        if ok % 10 == 0:
            logger.info("  [%d/%d] fetched %s (%d bars) | ok=%d skip=%d fail=%d",
                        i, len(UNIVERSE), sym, len(bars), ok, skip, fail)
        time.sleep(0.4)   # pace under the historical-API rate limit
    logger.info("DONE: %d fetched, %d already cached, %d failed. Total universe files: %d",
                ok, skip, fail, len(list(HIST.glob("*__ONE_DAY.csv"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
