"""
Auto-sell monitor v2.

Now exits on any of:
  - take profit / stop loss / trailing stop (legacy)
  - chart_ai bullish < SELL_THR while in profit
  - orderflow conviction flips strongly negative
  - exhaustion candle on top of profit
  - volume absorption (institutional supply) on top of profit

Also performs ATR-based trailing once price is +1R from entry.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from ai.chart_ai import evaluate as chart_evaluate
from ai.orderflow_ai import evaluate as orderflow_evaluate
from ai.reinforcement import agent, discretize
from ai.smart_entry import _features
from ai.xgb_model import xgb_model
from config import settings
from database.db import db
from dex.dexscreener import dexscreener
from market.candles import candles
from market.orderflow import orderflow
from sniper.wallet_rotation import rotator
from trading.auto_buy import risk_gate
from trading.jupiter_swap import jupiter
from trading.paper_wallet import paper_wallet
from trading.position_manager import Position, position_manager
from trading.real_wallet import get_real_wallet
from trading.trailing_stop import ExitDecision, evaluate
from utils.helpers import now_ms
from utils.logger import get_logger

log = get_logger(__name__)


class AutoSeller:
    def __init__(self, poll_interval: float = 4.0) -> None:
        # Scalp mode polls much faster - profit windows are seconds long.
        self.poll_interval = 1.5 if settings.scalp_mode else poll_interval
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        log.info("Auto-sell monitor v2 started.")
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:
                log.exception("auto-sell tick error: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass
        log.info("Auto-sell monitor stopped.")

    async def _tick(self) -> None:
        positions = position_manager.list()
        if not positions:
            return

        # Fetch all live prices in parallel.
        price_tasks = {p.token_mint: asyncio.create_task(dexscreener.get_token(p.token_mint))
                       for p in positions}

        async def _process(pos: Position) -> None:
            try:
                snap = await price_tasks[pos.token_mint]
            except Exception:
                return
            if snap is None:
                return
            price_sol = snap.price_sol or 0.0
            if price_sol <= 0:
                return
            candles.add_tick(pos.token_mint, price_sol, snap.volume_5m_usd, now_ms())
            await position_manager.update_price(pos.token_mint, price_sol)
            decision = self._exit_decision(pos, snap, price_sol)
            if decision.sell:
                await self._exit(pos, price_sol, decision.reason or "exit")

        # Process each position concurrently - one slow sell can't delay TP
        # firing on another.
        await asyncio.gather(
            *(_process(p) for p in positions),
            return_exceptions=True,
        )

    # ------------------------------------------------------------------
    # Exit logic
    # ------------------------------------------------------------------

    def _exit_decision(self, pos: Position, snap, price_sol: float) -> ExitDecision:
        # 1) Hard rules (TP / SL / trailing  OR  scalp-mode profit/loss)
        legacy = evaluate(pos, price_sol)
        if legacy.sell:
            return legacy

        # In scalp mode, the only allowed exits are the scalp rules themselves
        # (handled inside `evaluate`). Skip the chart/flow exits below so the
        # position cannot be closed by anything except scalp_pct / scalp_usd /
        # scalp_max_loss.
        if settings.scalp_mode:
            return ExitDecision(sell=False)

        pnl_pct = pos.unrealized_pct(price_sol)

        # 2) Chart signal collapse while in profit
        chart_candles = candles.candles(pos.token_mint, tf="1m", n=64)
        if len(chart_candles) >= 12:
            chart = chart_evaluate(chart_candles)
            if pnl_pct > 0.05 and chart.bullish < 0.35:
                return ExitDecision(sell=True, reason=f"chart_collapse ({chart.bullish:.2f})")
            if pnl_pct > 0.10 and chart.patterns and chart.patterns.exhaustion_candle:
                return ExitDecision(sell=True, reason="exhaustion_candle_in_profit")
            if pnl_pct > 0.10 and chart.patterns and chart.patterns.volume_absorption:
                return ExitDecision(sell=True, reason="absorption_at_top")
            if pnl_pct > 0.05 and chart.patterns and chart.patterns.liquidity_sweep_up:
                return ExitDecision(sell=True, reason="liquidity_sweep_high")

        # 3) Orderflow conviction flip
        of_snap = orderflow.snapshot(pos.token_mint, now_ms())
        if of_snap.total_volume_usd > 0:
            of_sig = orderflow_evaluate(of_snap)
            if pnl_pct > 0.05 and of_sig.conviction < -0.5:
                return ExitDecision(sell=True, reason=f"flow_flip ({of_sig.conviction:+.2f})")
            if of_sig.whale_bias < -0.6 and pnl_pct > 0.0:
                return ExitDecision(sell=True, reason="whales_dumping")

        # 4) AI-score collapse (legacy XGB), only when already in profit
        if pnl_pct > 0.05:
            f = _features(snap, 0.0, 0)
            score = xgb_model.predict(f)
            if score < settings.ai_sell_threshold:
                return ExitDecision(sell=True, reason=f"AI score collapse ({score:.2f})")

        return ExitDecision(sell=False)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def _exit(self, pos: Position, price_sol: float, reason: str) -> None:
        log.info("[EXIT] %s reason=%s pnl=%+.1f%%",
                 pos.token_symbol or pos.token_mint[:8],
                 reason, pos.unrealized_pct(price_sol) * 100)

        realized_sol = pos.amount_token * price_sol
        pnl_sol = realized_sol - pos.amount_sol
        pnl_usd = pnl_sol * 150.0

        # Wallet leg may throw (network, serialization, RPC). The position MUST
        # still be closed in position_manager, otherwise the next tick re-fires
        # the same exit and we double-count the trade.
        try:
            if settings.is_real:
                await self._real_exit(pos, price_sol)
            else:
                # Look up latest liquidity from the most recent snap for slippage sim.
                latest_snap = await dexscreener.get_token(pos.token_mint)
                liq = float(latest_snap.liquidity_usd) if latest_snap else 0.0
                await paper_wallet.sell(
                    token_mint=pos.token_mint,
                    amount_token=pos.amount_token,
                    price_sol_per_token=price_sol,
                    symbol=pos.token_symbol,
                    dex=pos.dex,
                    liquidity_usd=liq,
                )
        except Exception as exc:
            log.error("wallet sell failed for %s (%s); closing position anyway", pos.token_mint[:8], exc)

        closed = await position_manager.close(pos.token_mint)
        risk_gate.record_pnl(pnl_usd)

        day = time.strftime("%Y-%m-%d", time.gmtime())
        is_win = 1 if pnl_sol > 0 else 0
        is_loss = 0 if pnl_sol > 0 else 1
        db.execute(
            """
            INSERT INTO pnl_daily (day, realized_usd, trades_count, wins, losses)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(day) DO UPDATE SET
                realized_usd = realized_usd + excluded.realized_usd,
                trades_count = trades_count + 1,
                wins = wins + excluded.wins,
                losses = losses + excluded.losses
            """,
            (day, pnl_usd, is_win, is_loss),
        )

        if closed:
            try:
                state = discretize(closed.ai_score, 0.0, 0.0, 0.0)
                next_state = discretize(0.5, 0.0, 0.0, 0.0)
                reward = max(-1.0, min(1.0, pos.unrealized_pct(price_sol)))
                action = "BUY_BIG" if closed.amount_sol > settings.default_buy_amount_sol else "BUY_SMALL"
                agent.update(state, action, reward, next_state)
                db.execute(
                    "INSERT INTO rl_history (ts, state_hash, action, reward, next_state) VALUES (?,?,?,?,?)",
                    (now_ms(), state, action, reward, next_state),
                )
            except Exception as exc:
                log.debug("RL update failed: %s", exc)

        await rotator.on_trade()

    async def _real_exit(self, pos: Position, price_sol: float) -> Optional[str]:
        wallet = await get_real_wallet()
        if wallet is None:
            log.error("real exit blocked: wallet missing")
            return None
        try:
            decimals = 6
            raw = int(pos.amount_token * (10 ** decimals))
            if raw <= 0:
                return None
            sig = await jupiter.swap_token_for_sol(
                token_mint=pos.token_mint,
                amount_token_units=raw,
            )
            if sig:
                db.log_trade(
                    ts=now_ms(),
                    mode="REAL",
                    side="SELL",
                    token_mint=pos.token_mint,
                    token_symbol=pos.token_symbol,
                    dex=pos.dex,
                    amount_sol=pos.amount_token * price_sol,
                    amount_token=pos.amount_token,
                    price_sol=price_sol,
                    tx_sig=sig,
                    notes="jupiter_exit",
                )
            return sig
        except Exception as exc:
            log.error("real exit failed: %s", exc)
            return None


auto_seller = AutoSeller()
