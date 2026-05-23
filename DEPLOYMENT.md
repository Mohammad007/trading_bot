# Deployment Guide

The bot is a single Python process. It scales fine on a 1 vCPU / 1 GB VPS
in PAPER mode, and 2 vCPU / 2 GB is comfortable in REAL mode (because of
TLS + signing overhead).

---

## Ubuntu / Debian VPS

```bash
# system deps
sudo apt update
sudo apt install -y python3.11 python3.11-venv git build-essential

# create user and clone
sudo adduser --disabled-password sniper
sudo -iu sniper
git clone https://your.git/trading_bot.git
cd trading_bot

python3.11 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install -r requirements.txt

cp .env.example .env
nano .env   # leave MODE=PAPER for first run

python main.py
```

### Run under tmux

```bash
tmux new -s sniper
. .venv/bin/activate
python main.py
# detach: Ctrl-B then D
# reattach: tmux attach -t sniper
```

### systemd unit (auto-restart on crash)

`/etc/systemd/system/solana-sniper.service`:

```ini
[Unit]
Description=Solana AI Sniper
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=sniper
WorkingDirectory=/home/sniper/trading_bot
EnvironmentFile=/home/sniper/trading_bot/.env
ExecStart=/home/sniper/trading_bot/.venv/bin/python main.py
Restart=on-failure
RestartSec=10
StandardOutput=append:/home/sniper/trading_bot/logs/stdout.log
StandardError=append:/home/sniper/trading_bot/logs/stderr.log

# Hardening
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/home/sniper/trading_bot
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now solana-sniper
journalctl -u solana-sniper -f
```

---

## Linux (any distro) without systemd

```bash
nohup ./.venv/bin/python main.py >> logs/stdout.log 2>&1 &
disown
```

Combine with a cron `@reboot` entry for auto-start:

```cron
@reboot cd /home/sniper/trading_bot && ./.venv/bin/python main.py >> logs/stdout.log 2>&1
```

---

## Windows (your dev machine, `d:\trading_bot`)

```powershell
cd d:\trading_bot
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -U pip
pip install -r requirements.txt
copy .env.example .env
notepad .env
python main.py
```

### Auto-start with Task Scheduler

1. Open Task Scheduler -> *Create Basic Task*.
2. Trigger: *When the computer starts*.
3. Action: *Start a program*.
4. Program: `d:\trading_bot\.venv\Scripts\python.exe`
5. Arguments: `main.py`
6. Start in: `d:\trading_bot`
7. After creating, open *Properties* and tick:
   - "Run whether user is logged on or not"
   - "Configure for: Windows 10/11"
8. Settings tab: enable "Restart the task if it fails".

---

## Docker (optional)

`Dockerfile`:

```dockerfile
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential git curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .
ENV TZ=UTC
CMD ["python", "main.py"]
```

`docker-compose.yml`:

```yaml
services:
  sniper:
    build: .
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./database.db:/app/database.db
      - ./logs:/app/logs
      - ./paper_wallet.json:/app/paper_wallet.json
      - ./ai/saved_models:/app/ai/saved_models
```

```bash
docker compose up -d
docker compose logs -f
```

---

## Monitoring

- The bot writes a rotating log file at `logs/bot.log` (5 MB x 5).
- Enable Telegram alerts for live trade notifications.
- Tail SQLite:
  ```bash
  sqlite3 database.db "SELECT * FROM trades ORDER BY ts DESC LIMIT 20;"
  ```

---

## Backups

Back up these files daily:

```
database.db
paper_wallet.json
ai/saved_models/*
.env       (encrypted, never plain)
```

A trivial cron job:

```cron
0 3 * * * tar czf /backup/sniper-$(date +\%F).tgz /home/sniper/trading_bot/database.db /home/sniper/trading_bot/paper_wallet.json /home/sniper/trading_bot/ai/saved_models
```

---

## Sanity-check commands

```bash
# Confirm paper wallet survives restart
python -c "from trading.paper_wallet import paper_wallet; print(paper_wallet.balance_sol(), paper_wallet.balance_usdt())"

# Confirm AI model heuristic works
python -c "from ai.smart_entry import decide; from dex import TokenSnapshot; print(decide(TokenSnapshot(mint='x', liquidity_usd=20000, volume_5m_usd=5000, buys_5m=20, sells_5m=5, price_change_5m=10)))"

# Run a one-shot scan
python -c "import asyncio; from dex.dexscreener import dexscreener; print(asyncio.run(dexscreener.trending())[:3])"
```
