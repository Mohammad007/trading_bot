# AI Multi-Chain Sniper v2

A modular, terminal-based AI trading platform with multi-chain support,
human-trader-style chart reading, order-flow analysis, smart-money
tracking, ATR-based dynamic risk management, and dual paper/real mode.

> **Default mode is PAPER.** Real trading requires explicit env flags
> + interactive confirmation at startup. Never run REAL mode with a
> wallet you can't afford to lose.

## What's new in v2

- **Multi-chain**: Solana + Ethereum + BSC + Polygon + Base + Arbitrum +
  Optimism + Avalanche + Tron. One module (`chains/evm`) covers all 7 EVM
  chains via Uniswap-V2-compatible routers.
- **Market data engine** (`market/`): in-memory OHLCV candle cache,
  numpy-only indicators (RSI, MACD, EMA, ATR, VWAP, Bollinger), pattern
  detector (S/R, BOS, CHOCH, FVG, liquidity sweeps, exhaustion, absorption).
- **Chart AI** (`ai/chart_ai.py`): human-trader read of trend + momentum +
  market structure + S/R proximity.
- **Order-flow AI** (`ai/orderflow_ai.py`): aggressive buyer/seller
  imbalance, whale bias, seller exhaustion.
- **Smart-money AI** (`ai/smart_money.py`): per-wallet PnL tracking,
  confidence score, persisted to SQLite.
- **Position-sizing AI** (`ai/position_sizing.py`): Kelly-lite sizing
  with ATR-based stops and scale-in / scale-out levels.
- **RPC failover** (`sniper/rpc_failover.py`): multi-endpoint pool with
  background latency monitoring and auto-quarantine of failing nodes.
- **Smart exits**: auto_sell now exits on chart collapse, flow flip,
  exhaustion, absorption, or liquidity-sweep-high - on top of legacy
  TP/SL/trailing.

## Project layout

```
bot/
  main.py                              # entry + dashboard
  config.py                            # pydantic settings
  requirements.txt
  requirements-ml.txt                  # optional TF for Python 3.10-3.12
  .env.example
  ai/
    xgb_model.py                       # XGB pump probability
    lstm_model.py                      # LSTM trend (+ statistical fallback)
    reinforcement.py                   # Q-learning agent
    correlation_ai.py                  # ecosystem heat
    chart_ai.py                        # human-trader chart read
    orderflow_ai.py                    # buy/sell aggression + whales
    smart_money.py                     # wallet PnL scorer
    position_sizing.py                 # ATR-based Kelly-lite sizing
    smart_entry.py                     # blended buy decision
  market/
    candles.py                         # OHLCV in-memory cache
    indicators.py                      # RSI/MACD/EMA/ATR/VWAP/Bollinger
    patterns.py                        # S/R, BOS, CHOCH, FVG, sweeps
    orderflow.py                       # tape replay + buy/sell pressure
  chains/
    __init__.py                        # Chain enum + EVM specs
    evm/
      rpc.py                           # web3.py with auto-failover
      wallet.py                        # EVM account + ERC-20 balances
      uniswap_v2.py                    # V2 router swap (covers 7 chains)
      sniper.py                        # PairCreated WS subscription
    tron/
      __init__.py                      # tronpy wallet (basic)
  dex/
    dexscreener.py
    raydium.py
    meteora.py
    pumpfun.py
    launchlab.py
  sniper/
    sniper_engine.py                   # Solana feed orchestrator
    rpc_failover.py                    # multi-RPC + latency monitor
    rugcheck.py                        # mint/freeze authority + heuristics
    mev_protection.py                  # dynamic priority fee + slippage
    wallet_rotation.py
  trading/
    paper_wallet.py
    real_wallet.py                     # Solana wallet (solders)
    jupiter_swap.py
    position_manager.py
    auto_buy.py                        # router + RiskGate
    auto_sell.py                       # smart exits (chart + flow)
    trailing_stop.py
    copy_trading.py
  analytics/
    pnl.py
    winrate.py
    performance.py
  alerts/
    telegram.py
  database/
    db.py
  utils/
    logger.py
    helpers.py
```

## Quick start (Windows / Python 3.13)

```powershell
cd d:\trading_bot
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned

python -m venv .venv
. .\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements.txt

copy .env.example .env
# edit .env: at minimum set ENABLED_CHAINS=solana (default) and HELIUS_API_KEY

python main.py
```

Linux/macOS — same but:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
cp .env.example .env
python main.py
```

## Configuring chains

`.env`:
```
ENABLED_CHAINS=solana,ethereum,bsc,base
```

For each EVM chain, add an RPC URL:
```
ETHEREUM_RPC_URL=https://eth.llamarpc.com
ETHEREUM_RPC_FALLBACKS=https://rpc.ankr.com/eth
ETHEREUM_WS_URL=wss://eth-mainnet.g.alchemy.com/v2/<your-key>   # optional, for new-pair sniper
```

The RPC pool auto-monitors latency and falls back to healthy endpoints
if the primary degrades.

For REAL mode on EVM, set:
```
EVM_PRIVATE_KEY=0x<your-hex-key>
# or encrypted:
EVM_PRIVATE_KEY_ENC=<blob from utils.helpers.encrypt_secret>
WALLET_ENC_PASSPHRASE=<passphrase>
```

## How the new AI pipeline works

1. **Discovery** - `SniperEngine` polls Solana feeds; per-EVM-chain
   `EVMSniper` listens to `PairCreated` events.
2. **Cache update** - every snapshot pushes a tick into the
   `CandleCache` (per-token, per-timeframe ring buffers) and the
   `OrderFlowBook` (rolling tape of buy/sell prints).
3. **Rugcheck** - mint/freeze authority + liquidity heuristics.
4. **Smart entry** - `ai/smart_entry.py` blends 6 signals:
   - XGB pump probability (or heuristic fallback)
   - LSTM trend (or statistical fallback)
   - **Chart AI** (trend + momentum + market structure + S/R)
   - **Order-flow AI** (aggression + whales + exhaustion)
   - Smart-money score (known profitable wallets)
   - Ecosystem heat (correlation between majors)
5. **Size** - `ai/position_sizing.py` derives SOL amount using
   Kelly-lite formula bounded by ATR-derived stop distance.
6. **Risk gate** - daily-loss cap, cooldown, max-open-positions.
7. **Execute** - paper or real (Solana via Jupiter, EVM via Uniswap V2 router).
8. **Monitor** - `auto_sell` runs every 4 s; exits on TP/SL, trailing,
   chart collapse, flow flip, exhaustion, absorption, liquidity sweep.
9. **Learn** - every closed position updates the RL Q-table and the
   smart-money wallet stats.

## What was deliberately not built

- **Sui / Aptos**: their Python SDKs are not production-stable. Add by
  hand if you need them; the chain abstraction is ready.
- **Pump.fun / Pancake-V3 mempool sniping**: requires private relays
  (Bloxroute / Jito) that aren't free. The EVM sniper uses public WS
  block events, which is one block (12s on Ethereum, 3s on BSC) behind
  the optimal-latency private route.
- **Order-book reading**: Solana/EVM AMMs are not CLOBs. We synthesize
  flow from per-trade prints, which is the right paradigm for AMMs.

## Switching to REAL mode

> Real trading risks total loss.

```
MODE=REAL
ENABLE_REAL_TRADING=true
WALLET_PRIVATE_KEY=<base58 Solana key>            # if solana enabled
EVM_PRIVATE_KEY=0x<hex EVM key>                   # if any EVM enabled
```

Bot will:
1. Print a large red warning banner.
2. Ask you to type `I UNDERSTAND THE RISKS` (60s timeout).
3. Verify each chain's wallet loads and balances read.
4. If anything fails, downgrade back to PAPER.

## Telegram remote control

`.env`:
```
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=<botfather token>
TELEGRAM_CHAT_ID=<your numeric chat id>
```

Commands: `/start /stop /mode /positions /balance /winrate /buy <mint> /sell <mint>`.

## Performance characteristics

- **No TensorFlow required** — all "AI" defaults to deterministic
  numpy/heuristic implementations. TF is opt-in via `requirements-ml.txt`
  (Python 3.10-3.12 only).
- **CPU footprint**: ~120 MB resident in PAPER mode with all
  signals enabled, scanning 4-5 Solana feeds.
- **Latency**: chart-AI evaluation is <2 ms on a 64-candle buffer.
  Sniper-to-buy decision is bounded by DexScreener round-trip, which
  is the inherent floor.

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md). systemd, tmux, Docker, and Task
Scheduler examples included.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) (still describes v1 flow, which
is the basis; v2 adds the market+chains layers around it).

## Security

- Never commit `.env`. The provided `.gitignore` covers it.
- Use the encrypted-key path for both Solana and EVM.
- Run REAL mode in a dedicated, low-balance wallet.
- `MAX_DAILY_LOSS_USDT` is your circuit breaker; set it.
- The bot will NOT trade if `ENABLE_REAL_TRADING != "true"`, regardless
  of `MODE`.
