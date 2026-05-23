"""
Chart-reading AI.

Combines technical indicators + pattern detector into a single bullish
probability in [0, 1]. This is the "human trader" reading the chart:
trend + momentum + structure + S/R proximity + traps.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from market.candles import Candle
from market.indicators import atr, bollinger, ema, macd, momentum, rsi, slope, vwap
from market.patterns import PatternReport, analyze
from utils.helpers import clamp


@dataclass
class ChartSignal:
    bullish: float           # 0..1
    trend: float             # -1..1
    momentum: float          # -1..1
    structure_bias: float    # -1..1
    sr_room_up: float        # fraction of distance to next resistance
    near_support: bool
    near_resistance: bool
    atr_value: float
    notes: List[str] = field(default_factory=list)
    patterns: Optional[PatternReport] = None


def evaluate(candles: Sequence[Candle]) -> ChartSignal:
    if len(candles) < 8:
        return ChartSignal(
            bullish=0.5, trend=0.0, momentum=0.0, structure_bias=0.0,
            sr_room_up=0.0, near_support=False, near_resistance=False,
            atr_value=0.0, notes=["insufficient_history"],
        )

    closes = [c.c for c in candles]
    last = closes[-1]

    # --- trend (EMA crossover slope normalized by price) ---------------------
    ema_fast = ema(closes, 8)
    ema_slow = ema(closes, 21)
    trend_raw = (ema_fast - ema_slow) / max(abs(ema_slow), 1e-12)
    trend = clamp(trend_raw * 30.0, -1.0, 1.0)

    # --- momentum ------------------------------------------------------------
    rsi_v = rsi(closes, 14)
    _, _, hist = macd(closes)
    mom8 = momentum(closes, 8)
    mom_score = clamp(
        ((rsi_v - 50.0) / 50.0) * 0.5
        + clamp(hist / max(abs(ema_slow) * 0.005, 1e-9), -1, 1) * 0.3
        + clamp(mom8 * 5.0, -1, 1) * 0.2,
        -1.0, 1.0,
    )

    # --- patterns / structure ----------------------------------------------
    patterns = analyze(candles)
    s = patterns.structure
    structure_bias = 0.0
    if s.bos_up:
        structure_bias += 0.5
    if s.choch_up:
        structure_bias += 0.3
    if s.bos_down:
        structure_bias -= 0.5
    if s.choch_down:
        structure_bias -= 0.3
    if patterns.liquidity_sweep_down:
        structure_bias += 0.2          # sweep-low + reversal = bullish
    if patterns.liquidity_sweep_up:
        structure_bias -= 0.2          # bull trap
    if patterns.exhaustion_candle:
        structure_bias *= -0.5         # flip the read
    structure_bias = clamp(structure_bias, -1.0, 1.0)

    # --- support / resistance proximity -------------------------------------
    sr = patterns.sr
    res = sr.nearest_resistance(last)
    sup = sr.nearest_support(last)
    sr_room_up = 0.0
    near_resistance = False
    near_support = False
    if res:
        sr_room_up = (res - last) / max(last, 1e-12)
        near_resistance = sr_room_up < 0.01
    if sup:
        near_support = (last - sup) / max(last, 1e-12) < 0.01

    # --- bullish blend ------------------------------------------------------
    notes: List[str] = []
    bullish = 0.5
    bullish += 0.20 * trend
    bullish += 0.20 * mom_score
    bullish += 0.20 * structure_bias
    if near_support and not near_resistance:
        bullish += 0.10
        notes.append("near_support")
    if near_resistance:
        bullish -= 0.10
        notes.append("near_resistance")
    if patterns.fvgs and patterns.fvgs[-1].bullish:
        bullish += 0.05
        notes.append("bullish_fvg")
    if patterns.volume_absorption and trend_raw > 0:
        bullish -= 0.10
        notes.append("absorption_top")

    return ChartSignal(
        bullish=clamp(bullish, 0.0, 1.0),
        trend=trend,
        momentum=mom_score,
        structure_bias=structure_bias,
        sr_room_up=sr_room_up,
        near_support=near_support,
        near_resistance=near_resistance,
        atr_value=atr(candles, 14),
        notes=notes,
        patterns=patterns,
    )
