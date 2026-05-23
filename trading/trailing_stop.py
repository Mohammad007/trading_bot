"""
Exit decision evaluator.

Two regimes:

  1) SCALP MODE   (settings.scalp_mode = True)
     - exit on profit_pct >= scalp_profit_pct
     - exit on profit_usd >= scalp_profit_usd
     - safety floor: exit on loss_pct >= scalp_max_loss_pct
     - all other exits (TP/SL/trailing/chart) are bypassed

  2) NORMAL MODE  (default)
     - take profit (absolute %)
     - stop loss   (absolute %)
     - trailing stop after activation

Pure function: given a position + current price, return ExitDecision.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config import settings
from trading.position_manager import Position

# Rough SOL/USD conversion for scalp-USD comparisons. Updated dynamically by
# the seller before calling evaluate() when in scalp mode and we have a fresh
# DexScreener snap, but a sensible default keeps the function pure.
_SOL_USD_ASSUMPTION = 150.0


@dataclass
class ExitDecision:
    sell: bool
    reason: Optional[str] = None


def evaluate(pos: Position, current_price_sol: float, sol_usd: float = _SOL_USD_ASSUMPTION) -> ExitDecision:
    if current_price_sol <= 0 or pos.entry_price <= 0:
        return ExitDecision(sell=False)

    pct = (current_price_sol - pos.entry_price) / pos.entry_price

    # --- SCALP MODE ----------------------------------------------------------
    if settings.scalp_mode:
        # Safety net first - even with SL off, do not let an account die.
        if settings.scalp_max_loss_pct > 0 and pct <= -abs(settings.scalp_max_loss_pct):
            return ExitDecision(sell=True, reason=f"scalp_max_loss ({pct:+.1%})")

        # ---- SMART PROFIT LOCK ("human scalper" trailing) ----------------
        # Once we've been in profit by at least `arm_pct`, watch the peak.
        # The moment price retraces `retrace_pct` from that peak, exit -
        # we keep whatever profit is left rather than waiting for the full
        # scalp_profit_pct target.
        if settings.smart_profit_lock and pos.high_water > pos.entry_price:
            peak_pct = (pos.high_water - pos.entry_price) / pos.entry_price
            if peak_pct >= settings.smart_lock_arm_pct:
                retrace = (pos.high_water - current_price_sol) / pos.high_water
                if retrace >= settings.smart_lock_retrace_pct:
                    return ExitDecision(
                        sell=True,
                        reason=f"smart_lock peak={peak_pct:+.2%} now={pct:+.2%} (retrace={retrace:.2%})",
                    )

        # Profit triggers (whichever hits first)
        if settings.scalp_profit_pct > 0 and pct >= settings.scalp_profit_pct:
            return ExitDecision(sell=True, reason=f"scalp_pct {pct:+.2%}")

        if settings.scalp_profit_usd > 0:
            pnl_sol = (current_price_sol - pos.entry_price) * pos.amount_token
            pnl_usd = pnl_sol * sol_usd
            if pnl_usd >= settings.scalp_profit_usd:
                return ExitDecision(sell=True, reason=f"scalp_usd +${pnl_usd:.2f}")

        return ExitDecision(sell=False)

    # --- NORMAL MODE ---------------------------------------------------------
    if pos.take_profit > 0 and pct >= pos.take_profit:
        return ExitDecision(sell=True, reason=f"take_profit hit ({pct:+.1%})")

    if pos.stop_loss > 0 and pct <= -pos.stop_loss:
        return ExitDecision(sell=True, reason=f"stop_loss hit ({pct:+.1%})")

    # Trailing stop activates once we've been in profit by trailing_stop + 5%.
    activation = pos.trailing_stop + 0.05
    in_profit_high = (pos.high_water - pos.entry_price) / pos.entry_price
    if pos.trailing_stop > 0 and in_profit_high >= activation:
        retrace = (pos.high_water - current_price_sol) / pos.high_water
        if retrace >= pos.trailing_stop:
            return ExitDecision(
                sell=True,
                reason=f"trailing_stop ({retrace:+.1%} retrace from peak)",
            )

    return ExitDecision(sell=False)
