# Railway Deployment Guide

Deploy the AI sniper to Railway (railway.app) in ~10 minutes.

## ⚠️ Pehle padho

| Reality | Detail |
|---|---|
| Cost | Railway Hobby plan: **$5/mo** + usage. 24/7 bot ≈ **$8–15/mo** total. |
| Mode recommendation | **PAPER only.** REAL mode on a cloud server has extra risks (server crash → stuck positions, no manual intervention). |
| What persists | Volume-mounted: SQLite DB, paper_wallet.json, AI models, logs. |
| What doesn't | The interactive TTY dashboard. Headless mode logs status every 60s instead. |
| Real-mode confirmation | The interactive "I UNDERSTAND THE RISKS" prompt doesn't work in headless. You set `REAL_CONFIRMED='I UNDERSTAND THE RISKS'` as an env var instead. |

## What ships in this repo for Railway

- **Dockerfile** — Python 3.11 slim, headless mode, runs as non-root user.
- **.dockerignore** — keeps `.env`, `.git`, venv out of the image.
- **railway.json** — tells Railway to use Dockerfile + restart on failure.
- `main.py` auto-detects no-TTY and switches from Rich dashboard to log lines.
- `config.py` reads `DATA_DIR` env var so all state goes to your mounted volume.

## Step-by-step

### 1. Push code to GitHub

```powershell
cd d:\trading_bot
git init
git add .
git commit -m "Initial AI sniper"
# Create a private repo on github.com, then:
git remote add origin https://github.com/<your-username>/<repo>.git
git branch -M main
git push -u origin main
```

> ⚠️ `.env` is in `.gitignore`. Verify with `git ls-files | findstr env` — it should ONLY show `.env.example`, never `.env`.

### 2. Create Railway project

1. Go to https://railway.app, sign in with GitHub.
2. **New Project** → **Deploy from GitHub repo**.
3. Authorize Railway, pick your sniper repo.
4. Railway detects the Dockerfile and builds. First build takes ~3-5 min.

### 3. Attach a persistent volume

Without this, every restart wipes `paper_wallet.json`, `database.db`, the Q-table — you lose all paper PnL history and AI learning.

1. In your Railway service → **Settings** → **Volumes** → **+ New Volume**.
2. Mount path: `/data`
3. Size: `1 GB` is plenty.
4. **Add Volume**.

Railway redeploys; from now on `/data` survives restarts.

### 4. Set environment variables

In Railway service → **Variables** → **+ New Variable**. Add these (minimum for PAPER mode):

```
MODE=PAPER
ENABLE_REAL_TRADING=false
ENABLED_CHAINS=solana

DATA_DIR=/data

HELIUS_API_KEY=99253c5e-bf8f-4d0a-8e5f-296fcfc6161a
SOLANA_RPC_URL=https://api.mainnet-beta.solana.com
SOLANA_WS_URL=wss://api.mainnet-beta.solana.com

PAPER_STARTING_BALANCE_USDT=100
PAPER_SIMULATION_REALISM=full

DEFAULT_BUY_AMOUNT_SOL=0.02
MAX_OPEN_POSITIONS=5
MAX_DAILY_LOSS_USDT=20

AI_BUY_THRESHOLD=0.55
AI_SELL_THRESHOLD=0.30

SCALP_MODE=true
SCALP_PROFIT_PCT=0.03
SCALP_MAX_LOSS_PCT=0.08

TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=<your token>
TELEGRAM_CHAT_ID=<your chat id>
```

> 💡 Click **Raw editor** in Railway to paste these in one shot.

### 5. Redeploy

Click **Deploy** in Railway. Watch the **Deploy Logs**:

```
INFO Booting AI Multi-Chain Sniper... mode=PAPER chains=['solana']
INFO Headless mode detected - dashboard disabled; periodic status logs.
INFO Sniper engine starting (mode=PAPER).
INFO pump.fun WS connected.
INFO STATUS mode=PAPER positions=0 candidates=12 buys=0 sol=0.3333
```

If you see this, **bot is live**. 🎉

### 6. Verify Telegram works

In Telegram, send `/mode` to your bot. You should get back:
```
Mode: PAPER  (real_enabled=False)
```

This confirms the deployment is healthy and reachable.

## Common questions

### Logs kahan dikhenge?

Railway service → **Deployments** → click the running deployment → **View Logs**. Live tail with search.

### Bot status check kaise karu?

Three ways:
- **Telegram**: `/balance`, `/positions`, `/winrate` (best)
- **Railway logs**: STATUS line logged every 60s
- **Railway metrics tab**: CPU / RAM / network graphs

### Restart kaise karu?

Railway service → **Settings** → top right `⋮` → **Redeploy**. Or push a new commit to GitHub — auto-redeploys.

### Cost kam kaise karu?

- **Sleep when not needed**: Railway services can be paused manually (Settings → Suspend). Bot off = no charges.
- **Reduce chains**: only Solana = lowest CPU/network use.
- **Tighten polls**: edit `sniper_engine.py` to poll DexScreener every 30s instead of 20s.
- **Best alternative**: a $4/mo VPS (Contabo / Hetzner ARM) runs this bot 24/7 with way more room. See [DEPLOYMENT.md](DEPLOYMENT.md).

### REAL mode pe Railway pe deploy karna safe hai?

**Recommended: no.** Reasons:
1. No manual intervention if Railway has an outage during an open position.
2. Your private key sits on a shared cloud. Even encrypted, the passphrase is in the same env vars.
3. Bot will keep buying while you sleep. Need very tight `MAX_DAILY_LOSS_USDT`.

If you still want to:
1. Encrypt key with `utils.helpers.encrypt_secret(...)`, set `WALLET_PRIVATE_KEY_ENC` + `WALLET_ENC_PASSPHRASE`.
2. Set `MODE=REAL`, `ENABLE_REAL_TRADING=true`.
3. Set `REAL_CONFIRMED=I UNDERSTAND THE RISKS` (this replaces the interactive prompt).
4. Use a **dedicated low-balance wallet** (max 0.1 SOL). Treat it as already-lost money.
5. Tighten everything: `MAX_OPEN_POSITIONS=2`, `DEFAULT_BUY_AMOUNT_SOL=0.005`, `MAX_DAILY_LOSS_USDT=2`.

### Bot crash ho gaya, positions stuck hain — kya karu?

`restartPolicyType` is `ON_FAILURE` with 10 retries. After 10 quick restarts Railway gives up. Then:
1. Check **Deploy Logs** for the error.
2. Open positions are safe in your wallet (in REAL mode); the DB still tracks them.
3. After fixing the bug and redeploying, the bot reloads positions from DB and resumes monitoring.

### Volume backup?

Railway doesn't auto-backup volumes. Manual backup:
```bash
# locally:
railway link
railway run cat /data/database.db > backup-$(date +%F).db
railway run cat /data/paper_wallet.json > paper-$(date +%F).json
```

## Cheat-sheet — Railway CLI

```powershell
npm i -g @railway/cli
railway login
railway link              # connects current dir to project
railway logs              # tail live logs
railway run python -c "from database.db import db; print(len(db.get_positions()))"
railway variables         # list env vars
railway redeploy          # force redeploy
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Build fails on `cryptography` | musl base + old pip | Already on `python:3.11-slim` (glibc); upgrade pip — already in Dockerfile |
| `paper_wallet.json` resets every deploy | Volume not mounted at `/data` | Settings → Volumes → confirm `/data` is mounted |
| No DEX activity in logs | `HELIUS_API_KEY` not set | Verify env var; bot is rate-limited on public RPC |
| Telegram unreachable | Wrong chat_id | `https://api.telegram.org/bot<TOKEN>/getUpdates` to get the right ID |
| `Telegram Conflict: terminated by other getUpdates request` | Two instances polling same token | Suspend any old deployment / local copy |
| Memory OOM | Too many chains enabled | Reduce `ENABLED_CHAINS` to just `solana` |

## File reference

- [Dockerfile](Dockerfile) — production image build
- [.dockerignore](.dockerignore) — keeps secrets out
- [railway.json](railway.json) — Railway-specific build + deploy config
- [main.py](main.py) — auto-detects headless mode
- [config.py](config.py) — reads `DATA_DIR` for volume mount
