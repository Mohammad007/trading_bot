"""
Position sizing AI.

Replaces the naive "always default_buy_amount_sol" with a Kelly-lite
sizing function:

    size = bankroll * risk_per_trade / (stop_distance_pct)

- `risk_per_trade` is scaled by AI confidence (Kelly bet).
- `stop_distance_pct` comes from ATR (volatility-adjusted).
- Result is clamped between min/max so a 99% confidence trade can't blow
  the bankroll.

Also exposes scale-in / scale-out helpers used by auto_sell.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config import settings
from utils.helpers import clamp


@dataclass
class SizingPlan:
    sol_amount: float       # how much SOL to spend now
    stop_distance: float    # price distance to stop (in price units)
    take_profit_distance: float
    scale_in_levels: list[float]
    scale_out_levels: list[float]
    notes: str = ""


# Defaults; tune via env if you want.
RISK_PER_TRADE = 0.02    # 2% of bankroll per trade at max confidence
MIN_SIZE_FRACTION = 1.0
MAX_SIZE_FRACTION = 2.0
ATR_STOP_MULT = 2.0
ATR_TP_MULT = 4.0


def size(
    bankroll_sol: float,
    confidence: float,
    price: float,
    atr_value: float,
    base_sol: Optional[float] = None,
) -> SizingPlan:
    """
    Returns the SOL amount + stop / TP distances for a single new entry.

    `confidence` should be in [0, 1] (e.g. blended AI buy probability).
    `atr_value` is the absolute ATR in price units (close-currency).
    """
    base = base_sol if base_sol is not None else settings.default_buy_amount_sol
    if price <= 0:
        return SizingPlan(
            sol_amount=base, stop_distance=0.0,
            take_profit_distance=0.0, scale_in_levels=[],
            scale_out_levels=[], notes="invalid_price",
        )

    # Stop distance from ATR; fallback to settings.stop_loss as fraction.
    if atr_value > 0:
        stop_dist_pct = clamp(ATR_STOP_MULT * (atr_value / price), 0.05, 0.40)
    else:
        stop_dist_pct = settings.stop_loss

    # Kelly-lite: edge = confidence - 0.5, bet a fraction of bankroll
    # equal to edge * RISK_PER_TRADE / stop_distance.
    edge = clamp(confidence - 0.5, 0.0, 0.5) * 2.0   # 0..1
    raw_size_frac = (RISK_PER_TRADE * edge) / max(stop_dist_pct, 0.01)
    size_frac = clamp(raw_size_frac, 0.0, RISK_PER_TRADE * 5)
    sol_from_kelly = bankroll_sol * size_frac

    # Floor / ceiling around the default base size so we don't go crazy.
    final = clamp(
        max(sol_from_kelly, base * MIN_SIZE_FRACTION),
        base * MIN_SIZE_FRACTION,
        base * MAX_SIZE_FRACTION,
    )

    stop_distance = stop_dist_pct * price
    tp_distance = (ATR_TP_MULT * atr_value if atr_value > 0
                   else settings.take_profit * price)

    # Scale-in: optional second leg if price retraces 50% of stop without trigger.
    scale_in = [price - stop_distance * 0.5]
    # Scale-out: take 30% at +1R, 30% at +2R, runner left to trail.
    r = stop_distance
    scale_out = [price + r, price + 2 * r]

    return SizingPlan(
        sol_amount=round(final, 6),
        stop_distance=stop_distance,
        take_profit_distance=tp_distance,
        scale_in_levels=scale_in,
        scale_out_levels=scale_out,
        notes=f"frac={size_frac:.3f} stop%={stop_dist_pct:.3f}",
    )
