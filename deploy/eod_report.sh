#!/usr/bin/env bash
# End-of-day performance snapshot for fine-tuning, written to
# data/eod_reports/eod_<date>.txt after market close. Captures trades, daily
# P&L, and — most useful for tuning — WHY signals were rejected (by reason).
# The auth token (if any) is read from deploy/trading.env, never hard-coded.
#
# Cron (server): 0 10 * * 1-5 root bash /opt/trading/deploy/eod_report.sh
set -uo pipefail
ENV=/opt/trading/deploy/trading.env
B=http://127.0.0.1:8100
OUT=/opt/trading/data/eod_reports
sudo mkdir -p "$OUT"

TOKEN=$(grep -E '^API_AUTH_TOKEN=' "$ENV" 2>/dev/null | cut -d= -f2- || true)
AUTH=(); [ -n "${TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer $TOKEN")
DAY=$(TZ=Asia/Kolkata date '+%Y-%m-%d')

{
  echo "==== EOD REPORT $DAY ===="
  echo "-- agent --"
  curl -s "${AUTH[@]}" "$B/agent/status" | python3 -c "import sys,json;d=json.load(sys.stdin);print('scans',d.get('scan_count'),'enqueued',d.get('enqueued_total'))" 2>/dev/null
  echo "-- restarts (0 = crash fix holding) --"
  sudo docker inspect -f '{{.RestartCount}}' trading-api 2>/dev/null
  echo "-- trades today --"
  curl -s "${AUTH[@]}" "$B/db/trades?limit=200" | python3 -c "import sys,json;print('count',json.load(sys.stdin).get('count',0))" 2>/dev/null
  echo "-- daily P&L --"
  curl -s "${AUTH[@]}" "$B/db/daily-pnl?limit=5" 2>/dev/null | head -c 400; echo
  echo "-- rejections by reason (WHERE TO TUNE) --"
  curl -s "${AUTH[@]}" "$B/risk/rejections?limit=200" | python3 -c "import sys,json,collections;d=json.load(sys.stdin);r=d.get('rejections',d.get('events',[]));c=collections.Counter(x.get('reason',x.get('rejection_reason','?')) for x in r) if isinstance(r,list) else {};[print(' ',k,v) for k,v in c.most_common()]" 2>/dev/null
} > "$OUT/eod_$DAY.txt" 2>&1
echo "wrote $OUT/eod_$DAY.txt"
