"""
Order-flow AI.

Turns an OrderFlowSnapshot into a single buy/sell-conviction score.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from market.orderflow import OrderFlowSnapshot
from utils.helpers import clamp


@dataclass
class OrderFlowSignal:
    conviction: float    # -1..1 - negative = sellers in control
    aggression: float    # 0..1 - how lopsided the tape is
    whale_bias: float    # -1..1
    notes: List[str]


def evaluate(snap: OrderFlowSnapshot) -> OrderFlowSignal:
    notes: List[str] = []
    if snap.total_volume_usd <= 0:
        return OrderFlowSignal(conviction=0.0, aggression=0.0, whale_bias=0.0, notes=["empty_tape"])

    aggression = abs(snap.aggressive_buy_ratio)
    conviction = clamp(snap.aggressive_buy_ratio * 1.2, -1.0, 1.0)

    whale_total = snap.whale_buys + snap.whale_sells
    whale_bias = 0.0
    if whale_total > 0:
        whale_bias = clamp((snap.whale_buys - snap.whale_sells) / whale_total, -1.0, 1.0)
        if snap.whale_buys >= 3 and snap.whale_buys > snap.whale_sells * 2:
            notes.append("whales_accumulating")
            conviction = clamp(conviction + 0.15, -1.0, 1.0)
        if snap.whale_sells >= 3 and snap.whale_sells > snap.whale_buys * 2:
            notes.append("whales_distributing")
            conviction = clamp(conviction - 0.15, -1.0, 1.0)

    if snap.exhaustion_score > 0.5 and snap.aggressive_buy_ratio > 0:
        notes.append("seller_exhaustion")
        conviction = clamp(conviction + 0.10, -1.0, 1.0)

    return OrderFlowSignal(
        conviction=conviction,
        aggression=aggression,
        whale_bias=whale_bias,
        notes=notes,
    )
