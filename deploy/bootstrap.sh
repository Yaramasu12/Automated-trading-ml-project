#!/usr/bin/env bash
# One-shot server bootstrap for the trading stack on a fresh Ubuntu 24.04 EC2.
# Run as root (or with sudo) AFTER the repo is at /opt/trading and
# /opt/trading/deploy/trading.env is filled in:
#
#   sudo bash /opt/trading/deploy/bootstrap.sh
#
# Idempotent: safe to re-run.
set -euo pipefail

REPO_DIR=/opt/trading
ENV_FILE="$REPO_DIR/deploy/trading.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE not found. Copy deploy/trading.env.example there and fill it in." >&2
  exit 1
fi
if grep -q "<your-" "$ENV_FILE" || grep -q "<paste-" "$ENV_FILE" || grep -q "<pick-" "$ENV_FILE"; then
  echo "ERROR: $ENV_FILE still contains <placeholder> values. Fill every one first." >&2
  exit 1
fi
chmod 600 "$ENV_FILE"

# ── 1. Swap: 2G cushion so TimescaleDB + API fit on a 2GB instance ───────────
if ! swapon --show | grep -q /swapfile; then
  echo "── Creating 2G swapfile"
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# ── 2. Docker ─────────────────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
  echo "── Installing Docker"
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

# ── 3. Tailscale (private access to the dashboard — no public ports) ────────
if ! command -v tailscale >/dev/null 2>&1; then
  echo "── Installing Tailscale"
  curl -fsSL https://tailscale.com/install.sh | sh
fi
if ! tailscale status >/dev/null 2>&1; then
  echo ""
  echo ">>> Run 'sudo tailscale up' and authenticate in the browser, then re-run this script."
  echo ">>> (Skipping the rest until Tailscale is connected.)"
  exit 0
fi

# ── 4. Data ownership: the API container runs as uid 1000 (trader). Root-owned
# SQLite files make every DB write fail ("attempt to write a readonly database")
# and crash the trading agent — seen in production 2026-07-13. Enforce on every run.
mkdir -p "$REPO_DIR/data" "$REPO_DIR/models" "$REPO_DIR/backtest_results"
chown -R 1000:1000 "$REPO_DIR/data" "$REPO_DIR/models" "$REPO_DIR/backtest_results"

# ── 5. Build & start the stack ────────────────────────────────────────────────
echo "── Starting stack"
cd "$REPO_DIR"
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
docker compose up -d --build

# ── 5. Expose the dashboard on the tailnet only (HTTPS, no public exposure) ──
tailscale serve --bg 3100 || true

# ── 6. Nightly backup at 17:30 IST (after market close + EOD jobs) ──────────
CRON_LINE="30 17 * * 1-5 root bash $REPO_DIR/deploy/backup.sh >> /var/log/trading-backup.log 2>&1"
if ! grep -qF "deploy/backup.sh" /etc/crontab; then
  echo "$CRON_LINE" >> /etc/crontab
  echo "── Nightly backup cron installed (17:30 IST, Mon-Fri)"
fi

echo ""
echo "════════════════════════════════════════════════════════════════"
echo " Stack is up. Verify:"
echo "   docker compose ps"
echo "   curl -s http://127.0.0.1:8100/health | head -c 300"
echo ""
echo " Dashboard (from any of YOUR Tailscale devices):"
tailscale serve status 2>/dev/null | sed 's/^/   /' || echo "   https://<this-machine>.<tailnet>.ts.net"
echo "════════════════════════════════════════════════════════════════"
