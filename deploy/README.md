# AWS Deployment Runbook (free-tier credits, ~$15/month)

One EC2 instance in **Mumbai (ap-south-1)** runs the whole docker-compose stack
(TimescaleDB + API + scheduler + frontend). The dashboard is reachable **only
through Tailscale** — zero ports open to the internet except SSH from your IP.

```
Phone/Laptop ──Tailscale (HTTPS)──▶ EC2 t4g.small (Mumbai)
                                     ├─ trading-frontend  :3100 (localhost)
                                     ├─ trading-api       :8100 (localhost)
                                     ├─ trading-scheduler
                                     └─ timescaledb       :5432 (localhost)
                                     nightly backup ──▶ S3 bucket
```

## 1. Launch the instance (console, ~5 minutes)

EC2 → Launch instance:

| Setting | Value |
|---|---|
| Region | **ap-south-1 (Mumbai)** |
| Name | `trading-server` |
| AMI | **Ubuntu Server 24.04 LTS (64-bit ARM)** |
| Instance type | **t4g.small** (2 vCPU, 2 GB — ~$12/mo) |
| Key pair | Create new → download the `.pem` |
| Security group | Inbound: **SSH (22) from "My IP" only**. Nothing else. |
| Storage | **30 GB gp3** |

Optional but recommended: attach an IAM role with S3 access limited to your
backup bucket (IAM → Role → EC2 → policy `AmazonS3FullAccess` scoped down later),
so backups need no stored keys.

Also create the backup bucket once: S3 → Create bucket → e.g.
`trading-backups-<yourname>` (region ap-south-1, all public access blocked).

## 2. Get the code and data onto the server

From your laptop:

```bash
chmod 400 ~/Downloads/trading-server.pem
SERVER=ubuntu@<EC2-public-IP>
KEY="-i ~/Downloads/trading-server.pem"

# code (rsync respects nothing — exclude junk explicitly)
rsync -az -e "ssh $KEY" \
  --exclude node_modules --exclude .venv --exclude .git \
  --exclude logs --exclude backtest_results \
  "/Users/saikumaryaramasu/Documents/TRADING PROJECT/Automated-trading-ml-project/" \
  "$SERVER:/tmp/trading/"

ssh $KEY $SERVER "sudo mkdir -p /opt && sudo mv /tmp/trading /opt/trading && sudo chown -R root:root /opt/trading"
```

(`data/` and `models/` ride along — that's your instrument cache, SQLite state,
and trained artifacts.)

## 3. Configure and bootstrap

```bash
ssh $KEY $SERVER
sudo cp /opt/trading/deploy/trading.env.example /opt/trading/deploy/trading.env
sudo nano /opt/trading/deploy/trading.env    # fill EVERY <placeholder>
sudo bash /opt/trading/deploy/bootstrap.sh   # installs docker+tailscale, may ask you to 'tailscale up'
sudo tailscale up                             # authenticate in browser (once)
sudo bash /opt/trading/deploy/bootstrap.sh   # re-run → builds and starts everything
```

The script: adds 2 GB swap → installs Docker + Tailscale → `docker compose up
-d --build` → `tailscale serve 3100` (dashboard on your tailnet over HTTPS) →
installs the nightly backup cron (17:30 IST, Mon–Fri).

## 4. Verify

```bash
docker compose ps                                   # 4 containers healthy
curl -s http://127.0.0.1:8100/health | head -c 300  # API answers
tailscale serve status                              # your dashboard URL
```

Open the tailnet URL on your phone (Tailscale app installed, same account) →
sidebar → **API Token** → paste the token from `trading.env`.

## 5. Day-2 operations

| Task | Command (on the server) |
|---|---|
| Logs | `docker logs -f trading-api` |
| Restart stack | `cd /opt/trading && docker compose restart` |
| Deploy new code | rsync again, then `docker compose up -d --build` |
| Manual backup | `sudo bash /opt/trading/deploy/backup.sh` |
| Restore DB | `docker exec -i trading-timescaledb pg_restore -U trading -d trading --clean < pg_<stamp>.dump` |

Everything has `restart: unless-stopped`, so reboots and crashes self-heal.
Add a free UptimeRobot monitor later if you want down-alerts to your phone.

## Cost inside the 6-month credit

| Item | Monthly | 6 months |
|---|---|---|
| t4g.small (on-demand, Mumbai) | ~$12.3 | ~$74 |
| 30 GB gp3 EBS | ~$2.6 | ~$16 |
| S3 backups (few GB) | <$0.5 | ~$2 |
| **Total** | **~$15** | **~$92** |

Fits a $100 credit. When credits end: keep the instance (~₹1,300/mo), move to
Oracle Cloud free tier, or buy a 1-yr t4g.small savings plan (~40% off).

## Security notes

- Only port 22 (from your IP) is open; dashboard/API are localhost + Tailscale.
- `API_AUTH_REQUIRED=true` — generate a **fresh** token; the pre-July-2026
  token shipped inside old frontend bundles and must not be reused.
- Angel One secrets exist only in `/opt/trading/deploy/trading.env` (chmod 600).
- This deployment stays in PAPER mode; going LIVE still requires every gate
  (arming, confirmation phrase, readiness) — nothing here weakens that.

## Frontend dependency advisories (Dependabot)

GitHub reports advisories on the frontend **dev/build** dependencies
(`@babel/core`, `brace-expansion`, `esbuild`, `vite`, `vitest`, `vite-node`).
None are exploitable in the deployed app: production serves the pre-built
static bundle via nginx and never runs the Vite dev server, Vitest UI, or Babel
at runtime. The critical/high items specifically require running the Vitest UI
server or Vite dev server, which this deployment does not do.

Remediation:
- Safe, non-breaking (clears the two low-risk transitive advisories):
  `cd hft_frontend && npm audit fix`
- Full clear needs a **planned** major upgrade (Vite 5 → 8, Vitest 2 → 3). It is
  NOT a drop-in: `vite-plugin-pwa` and `@vitejs/plugin-react` must move to
  Vite-8-compatible versions first, or the production build hangs. Do this as a
  deliberate migration with a build + test pass, not `npm audit fix --force`.
