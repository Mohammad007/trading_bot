"""
Auto-buy router.

Dispatches BUY signals from the sniper engine to either:
  - paper_wallet.buy(...) in PAPER mode
  - jupiter_swap.swap_sol_for_token(...) in REAL mode

In both cases we update the position_manager so the sell logic and the
dashboard see the new position uniformly.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from ai.smart_entry import SmartEntryDecision
from config import settings
from database.db import db
from dex import TokenSnapshot
from sniper.mev_protection import adjust_slippage, jitter, suggest_priority_fee
from sniper.wallet_rotation import rotator
from trading.jupiter_swap import jupiter
from trading.paper_wallet import paper_wallet
from trading.position_manager import position_manager
from trading.real_wallet import get_real_wallet
from utils.helpers import now_ms
from utils.logger import get_logger

log = get_logger(__name__)


class RiskGate:
    """
    Daily-loss / cooldown / max-open-positions gate.

    All buy attempts pass through `allow_buy()` first.
    """

    def __init__(self) -> None:
        self.cooldown_until_ts: int = 0
        self.daily_realized_usd: float = 0.0
        self.daily_key: str = ""

    def _roll_day(self) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if day != self.daily_key:
            self.daily_key = day
            self.daily_realized_usd = 0.0

    def record_pnl(self, pnl_usd: float) -> None:
        self._roll_day()
        self.daily_realized_usd += pnl_usd
        if pnl_usd < 0:
            self.cooldown_until_ts = now_ms() + settings.cooldown_after_loss_secs * 1000

    def allow_buy(self) -> Optional[str]:
        self._roll_day()
        if self.daily_realized_usd <= -abs(settings.max_daily_loss_usdt):
            return f"daily loss limit hit ({self.daily_realized_usd:.2f} USDT)"
        if now_ms() < self.cooldown_until_ts:
            wait = (self.cooldown_until_ts - now_ms()) // 1000
            return f"cooldown active ({wait}s left)"
        if position_manager.count() >= settings.max_open_positions:
            return f"max_open_positions={settings.max_open_positions} reached"
        return None


risk_gate = RiskGate()


# ---------------------------------------------------------------------------

async def on_buy_signal(snap: TokenSnapshot, decision: SmartEntryDecision) -> bool:
    """Entry point used by the sniper engine."""
    if position_manager.has(snap.mint):
        return False

    block_reason = risk_gate.allow_buy()
    if block_reason:
        log.info("buy blocked for %s: %s", snap.symbol or snap.mint[:8], block_reason)
        return False

    amount_sol = max(decision.suggested_sol, 0.001)

    if settings.is_real:
        return await _real_buy(snap, decision, amount_sol)
    return await _paper_buy(snap, decision, amount_sol)


async def _paper_buy(snap: TokenSnapshot, decision: SmartEntryDecision, amount_sol: float) -> bool:
    price_sol = snap.price_sol if snap.price_sol > 0 else snap.price_usd / 150.0
    if price_sol <= 0:
        log.warning("paper buy skipped (no price for %s)", snap.mint[:8])
        return False
    ok = await paper_wallet.buy(
        token_mint=snap.mint,
        amount_sol=amount_sol,
        price_sol_per_token=price_sol,
        symbol=snap.symbol,
        dex=snap.dex,
        ai_score=decision.confidence,
        liquidity_usd=snap.liquidity_usd,
    )
    if not ok:
        return False
    holding = paper_wallet.holding(snap.mint)
    if holding is None:
        return False
    await position_manager.open(
        token_mint=snap.mint,
        token_symbol=snap.symbol,
        dex=snap.dex,
        entry_price_sol=price_sol,
        amount_token=holding.amount,
        amount_sol=amount_sol,
        ai_score=decision.confidence,
    )
    return True


async def _real_buy(snap: TokenSnapshot, decision: SmartEntryDecision, amount_sol: float) -> bool:
    wallet = await get_real_wallet()
    if wallet is None:
        log.error("real buy aborted: wallet not loaded")
        return False

    slippage = adjust_slippage(settings.slippage_bps, snap.liquidity_usd)
    # priority fee is read inside jupiter via settings but we set it dynamically.
    settings.priority_fee_microlamports = await suggest_priority_fee()

    await jitter()
    sig = await jupiter.swap_sol_for_token(
        token_mint=snap.mint,
        amount_sol=amount_sol,
        slippage_bps=slippage,
    )
    if not sig:
        log.warning("real buy failed for %s", snap.symbol or snap.mint[:8])
        return False

    # Resolve actual filled amount on-chain.
    try:
        token_balance = await wallet.get_token_balance(snap.mint)
    except Exception:
        token_balance = 0.0

    price_sol = snap.price_sol if snap.price_sol > 0 else (amount_sol / max(token_balance, 1e-9))
    db.log_trade(
        ts=now_ms(),
        mode="REAL",
        side="BUY",
        token_mint=snap.mint,
        token_symbol=snap.symbol,
        dex=snap.dex,
        amount_sol=amount_sol,
        amount_token=token_balance,
        price_sol=price_sol,
        tx_sig=sig,
        ai_score=decision.confidence,
        notes="jupiter",
    )
    await position_manager.open(
        token_mint=snap.mint,
        token_symbol=snap.symbol,
        dex=snap.dex,
        entry_price_sol=price_sol,
        amount_token=token_balance,
        amount_sol=amount_sol,
        ai_score=decision.confidence,
    )
    await rotator.on_trade()
    return True
