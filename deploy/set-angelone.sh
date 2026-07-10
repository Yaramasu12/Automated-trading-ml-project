#!/usr/bin/env bash
# Interactively add Angel One credentials to deploy/trading.env, then enable the
# live market-data feed + autonomous agent and restart. Values are read locally
# on the server and never printed — nothing is echoed or logged.
#
#   sudo bash /opt/trading/deploy/set-angelone.sh
set -euo pipefail
ENV=/opt/trading/deploy/trading.env

read -rp  "Angel One API Key:        " AO_KEY
read -rp  "Angel One API Secret:     " AO_SECRET
read -rp  "Angel One Client Code:    " AO_CLIENT
read -rsp "Angel One PIN:            " AO_PIN;  echo
read -rsp "Angel One TOTP Secret:    " AO_TOTP; echo

set_kv() { sudo sed -i "s|^$1=.*|$1=$2|" "$ENV"; }
set_kv ANGEL_ONE_API_KEY              "$AO_KEY"
set_kv ANGEL_ONE_API_SECRET           "$AO_SECRET"
set_kv ANGEL_ONE_CLIENT_CODE          "$AO_CLIENT"
set_kv ANGEL_ONE_PIN                  "$AO_PIN"
set_kv ANGEL_ONE_TOTP_SECRET          "$AO_TOTP"
# Turn on live market data + autonomous agent now that credentials exist.
set_kv AUTO_START_LIVE_FEED           true
set_kv AUTO_START_AGENT               true
set_kv PREMARKET_REFRESH_INSTRUMENTS  true

cd /opt/trading
set -a; source "$ENV"; set +a
docker compose up -d
echo "Angel One credentials saved; live feed + agent enabled; containers restarting."
