# Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         main.py                                 │
│         (REAL/PAPER safety flow + rich dashboard)               │
└───────────────────────────┬─────────────────────────────────────┘
                            │
            ┌───────────────┼─────────────────┐
            ▼               ▼                 ▼
   ┌──────────────┐  ┌──────────────┐  ┌───────────────┐
   │ SniperEngine │  │  AutoSeller  │  │  CopyTrader   │
   │   (feeds)    │  │  (monitors)  │  │  (logsSub)    │
   └──────┬───────┘  └──────┬───────┘  └───────┬───────┘
          │                 │                  │
          │                 │                  │ whale → mint
          │                 ▼                  ▼
          │       ┌──────────────────────────────────┐
          │       │   PositionManager (in-memory     │
          │       │   mirror of `positions` table)   │
          │       └──────────────────────────────────┘
          │
          ▼
   ┌──────────────┐
   │ smart_entry  │ ──── xgb_model  ┐
   │   .decide()  │ ──── lstm_model ├──→ SmartEntryDecision
   │              │ ──── q-agent    ┘
   └──────┬───────┘
          │ should_buy
          ▼
   ┌──────────────┐
   │ auto_buy     │ ── RiskGate (daily loss / cooldown / max-open)
   │   .on_buy()  │
   └──────┬───────┘
          │
   ┌──────┴───────────────────────────┐
   │                                  │
   ▼ PAPER                            ▼ REAL
┌───────────┐                  ┌──────────────────┐
│paper_walle│                  │  jupiter_swap    │
│ buy/sell  │                  │  + real_wallet   │
└─────┬─────┘                  └────────┬─────────┘
      │                                 │
      └──────────── trades / pnl ───────┘
                       │
                       ▼
              ┌─────────────────┐
              │   SQLite (db)   │
              └─────────────────┘
```

## Mode separation

Everything above `auto_buy` is mode-agnostic. The router checks
`settings.is_real` (which requires both `MODE=REAL` *and*
`ENABLE_REAL_TRADING=true`) and dispatches accordingly. This is the
*only* place where real-money execution can be triggered, which makes
the safety boundary auditable in one file.

## Data flow

1. **Discovery** — `SniperEngine` runs concurrent loops:
   - Pump.fun WebSocket (`subscribeNewToken`)
   - Pump.fun REST poll (every 8s)
   - DexScreener trending (every 20s)
   - Raydium pools (every 30s)
   - Meteora pairs (every 60s)
   - LaunchLab (every 45s)
2. **Normalization** — each source produces a `TokenSnapshot`.
3. **Filter** — `rugcheck.check()` runs heuristic + on-chain authority
   checks; failures get blacklisted (persisted to DB).
4. **Score** — `smart_entry.decide()` blends XGBoost, LSTM, and the
   correlation/ecosystem heat. The RL agent picks an action conditioned
   on the discretized state.
5. **Buy** — `auto_buy.on_buy_signal()` consults `RiskGate` and routes
   to paper or real wallet.
6. **Monitor** — `AutoSeller` polls open positions every 6s, updates
   trailing-stop high-water marks, and exits on TP/SL/trailing or
   AI-score collapse.
7. **Learn** — every closed position posts a (state, action, reward)
   tuple to the Q-table so the agent gradually skews position sizing
   toward setups that worked.

## Persistence

SQLite at `database.db` with WAL mode. Tables:

| Table        | Purpose                                       |
|--------------|-----------------------------------------------|
| trades       | Every buy / sell (paper + real)               |
| positions    | Open positions (mirrored in-memory)           |
| pnl_daily    | Day-bucketed realized PnL + W/L counts        |
| ai_scores    | Predictions per (token, model)                |
| tokens       | Token catalog cache                           |
| wallets      | Smart-money tracking                          |
| rl_history   | RL transitions (state, action, reward)        |
| blacklist    | Rug-flagged mints                             |

`paper_wallet.json` stores virtual SOL/USDT balances + holdings.

## CPU budget

The system is intentionally async + CPU-friendly:

- All network IO is `aiohttp` / `websockets`.
- AI inference is per-candidate, batched implicitly by the loop.
- Heuristic fallbacks let you skip TensorFlow on small VPSes.
- No threads beyond the SQLite worker pool (`asyncio.to_thread`).

Typical resident set: ~120 MB (no TF), ~400 MB (with TF).

## Safety boundaries

| Boundary                                | Enforced by                          |
|-----------------------------------------|--------------------------------------|
| Real-money execution                    | `Settings.is_real` (both flags req)  |
| Interactive confirmation                | `main.confirm_real_mode()`           |
| Risk limits                             | `RiskGate.allow_buy()` in auto_buy   |
| Rug filter                              | `sniper.rugcheck.check()`            |
| Slippage cap                            | `sniper.mev_protection.adjust_slippage()` |
| Wallet rotation                         | `sniper.wallet_rotation.rotator`     |
| Telegram authorization                  | chat-id allow-list                   |
