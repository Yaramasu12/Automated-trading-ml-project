#!/usr/bin/env bash
# Market-hours health snapshot -> /var/log/trading-verify.log
# Confirms the API is not crash-looping and the agent is scanning. The auth
# token (if any) is read from deploy/trading.env — never hard-coded here. When
# auth is disabled (API_AUTH_REQUIRED=false), no token is sent and that's fine.
#
# Cron (server): */15 3-10 * * 1-5 root bash /opt/trading/deploy/verify.sh
set -uo pipefail
ENV=/opt/trading/deploy/trading.env
B=http://127.0.0.1:8100

TOKEN=$(grep -E '^API_AUTH_TOKEN=' "$ENV" 2>/dev/null | cut -d= -f2- || true)
AUTH=(); [ -n "${TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer $TOKEN")

TS=$(TZ=Asia/Kolkata date '+%Y-%m-%d %H:%M IST')
R=$(sudo docker inspect -f '{{.RestartCount}}' trading-api 2>/dev/null || echo '?')
A=$(curl -s "${AUTH[@]}" "$B/agent/status" | python3 -c "import sys,json;d=json.load(sys.stdin);print('market=%s scans=%s enqueued=%s'%(d.get('market_status'),d.get('scan_count'),d.get('enqueued_total')))" 2>/dev/null || echo 'agent=unreachable')
T=$(curl -s "${AUTH[@]}" "$B/db/trades?limit=1" | python3 -c "import sys,json;print('trades=%d'%json.load(sys.stdin).get('count',0))" 2>/dev/null || echo 'trades=?')
M=$(sudo docker stats --no-stream --format '{{.MemPerc}}' trading-api 2>/dev/null || echo '?')

echo "$TS | restarts=$R | $A | $T | api_mem=$M" >> /var/log/trading-verify.log
