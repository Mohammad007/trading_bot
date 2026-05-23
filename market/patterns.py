"""
Chart pattern + market structure detector.

Implements the price-action concepts the human-trader prompt asked for:

- support / resistance (swing-pivot clustering)
- break of structure (BOS)
- change of character (CHOCH)
- fair value gap (FVG)
- liquidity sweep (stop-hunt)
- exhaustion candle
- volume absorption

All work on a small candle window (default last 60). Pure numpy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

import numpy as np

from market.candles import Candle


# ---------------------------------------------------------------------------
# Swing pivots
# ---------------------------------------------------------------------------

def _swing_highs(highs: np.ndarray, window: int = 3) -> List[int]:
    """Indices where bar is the local max over (window) bars on each side."""
    idx: List[int] = []
    n = highs.size
    for i in range(window, n - window):
        seg = highs[i - window : i + window + 1]
        if highs[i] == seg.max() and (seg == highs[i]).sum() == 1:
            idx.append(i)
    return idx


def _swing_lows(lows: np.ndarray, window: int = 3) -> List[int]:
    idx: List[int] = []
    n = lows.size
    for i in range(window, n - window):
        seg = lows[i - window : i + window + 1]
        if lows[i] == seg.min() and (seg == lows[i]).sum() == 1:
            idx.append(i)
    return idx


# ---------------------------------------------------------------------------
# Public structures
# ---------------------------------------------------------------------------

@dataclass
class SupportResistance:
    supports: List[float] = field(default_factory=list)
    resistances: List[float] = field(default_factory=list)

    def nearest_support(self, price: float) -> Optional[float]:
        below = [s for s in self.supports if s < price]
        return max(below) if below else None

    def nearest_resistance(self, price: float) -> Optional[float]:
        above = [r for r in self.resistances if r > price]
        return min(above) if above else None


@dataclass
class StructureSignal:
    bos_up: bool = False       # Break of structure to the upside
    bos_down: bool = False     # Break of structure to the downside
    choch_up: bool = False     # Change of character bullish (LH -> HH)
    choch_down: bool = False   # Change of character bearish (HL -> LL)


@dataclass
class FairValueGap:
    start_idx: int
    low: float
    high: float
    bullish: bool

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0


@dataclass
class PatternReport:
    sr: SupportResistance
    structure: StructureSignal
    fvgs: List[FairValueGap]
    liquidity_sweep_up: bool = False
    liquidity_sweep_down: bool = False
    exhaustion_candle: bool = False
    volume_absorption: bool = False


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

def support_resistance(candles: Sequence[Candle], cluster_pct: float = 0.005,
                       max_levels: int = 4) -> SupportResistance:
    """
    Cluster swing pivots into S/R levels.
    `cluster_pct` is the % distance below which two pivots collapse to one.
    """
    if len(candles) < 10:
        return SupportResistance()
    highs = np.array([c.h for c in candles])
    lows = np.array([c.l for c in candles])
    sh_idx = _swing_highs(highs)
    sl_idx = _swing_lows(lows)

    def _cluster(prices: List[float]) -> List[float]:
        prices = sorted(prices)
        clusters: List[List[float]] = []
        for p in prices:
            if clusters and abs(p - clusters[-1][-1]) / max(clusters[-1][-1], 1e-12) <= cluster_pct:
                clusters[-1].append(p)
            else:
                clusters.append([p])
        # Score by occurrence count then pick top.
        clusters.sort(key=lambda c: len(c), reverse=True)
        return [float(np.mean(c)) for c in clusters[:max_levels]]

    res = _cluster([float(highs[i]) for i in sh_idx])
    sup = _cluster([float(lows[i]) for i in sl_idx])
    return SupportResistance(supports=sup, resistances=res)


def structure(candles: Sequence[Candle]) -> StructureSignal:
    """
    Detect BOS and CHOCH on the latest swing pair.

    BOS up   = new HH after a HL
    BOS down = new LL after a LH
    CHOCH up = first HH after a series of LH/LL
    CHOCH down = first LL after a series of HH/HL
    """
    sig = StructureSignal()
    if len(candles) < 10:
        return sig
    highs = np.array([c.h for c in candles])
    lows = np.array([c.l for c in candles])
    sh = _swing_highs(highs)
    sl = _swing_lows(lows)
    if len(sh) < 2 or len(sl) < 2:
        return sig

    # Most recent two swing highs and lows
    h1, h2 = highs[sh[-2]], highs[sh[-1]]
    l1, l2 = lows[sl[-2]], lows[sl[-1]]

    last_close = candles[-1].c

    # BOS
    if last_close > h2 and h2 > h1:
        sig.bos_up = True
    if last_close < l2 and l2 < l1:
        sig.bos_down = True

    # CHOCH (regime flip)
    if h2 > h1 and l2 > l1:
        # Now in uptrend
        if any(highs[i] < highs[sh[-3]] for i in sh[-4:-2]) if len(sh) >= 4 else False:
            sig.choch_up = True
    if h2 < h1 and l2 < l1:
        if any(highs[i] > highs[sh[-3]] for i in sh[-4:-2]) if len(sh) >= 4 else False:
            sig.choch_down = True

    return sig


def fair_value_gaps(candles: Sequence[Candle]) -> List[FairValueGap]:
    """
    Classic 3-candle FVG: gap between candle[i-2].high and candle[i].low
    (bullish) or between candle[i-2].low and candle[i].high (bearish).
    Returns gaps still unfilled by the latest close.
    """
    out: List[FairValueGap] = []
    if len(candles) < 3:
        return out
    last_close = candles[-1].c
    for i in range(2, len(candles)):
        c0, _, c2 = candles[i - 2], candles[i - 1], candles[i]
        # Bullish FVG: c2.low > c0.high
        if c2.l > c0.h:
            fvg = FairValueGap(start_idx=i, low=c0.h, high=c2.l, bullish=True)
            if last_close > fvg.low:
                out.append(fvg)
        # Bearish FVG: c2.high < c0.low
        if c2.h < c0.l:
            fvg = FairValueGap(start_idx=i, low=c2.h, high=c0.l, bullish=False)
            if last_close < fvg.high:
                out.append(fvg)
    # Keep most-recent 3
    return out[-3:]


def liquidity_sweep(candles: Sequence[Candle], lookback: int = 20) -> tuple[bool, bool]:
    """
    Detect a sweep of recent swing highs/lows that immediately reverses.

    Returns (sweep_up, sweep_down):
      sweep_up  = wick took out prior high then closed back below (bull trap)
      sweep_down = wick took out prior low then closed back above (bear trap)
    """
    if len(candles) < lookback + 2:
        return False, False
    window = candles[-(lookback + 1) : -1]
    last = candles[-1]
    prior_high = max(c.h for c in window)
    prior_low = min(c.l for c in window)

    sweep_up = last.h > prior_high and last.c < prior_high
    sweep_down = last.l < prior_low and last.c > prior_low
    return sweep_up, sweep_down


def exhaustion_candle(candles: Sequence[Candle]) -> bool:
    """
    Large range vs recent ATR + tiny body = climax bar (often reversal).
    """
    if len(candles) < 10:
        return False
    ranges = np.array([c.h - c.l for c in candles[-10:]])
    if ranges.size == 0:
        return False
    avg_range = float(ranges[:-1].mean())
    last = candles[-1]
    last_range = last.h - last.l
    if avg_range <= 0:
        return False
    if last_range < 2.5 * avg_range:
        return False
    body = abs(last.c - last.o)
    return body < 0.25 * last_range


def volume_absorption(candles: Sequence[Candle]) -> bool:
    """
    Big volume bar with small body and rejection wick = institutions
    absorbing on the way down (or supplying on the way up). Useful as
    a "stop" signal for a runaway position.
    """
    if len(candles) < 10:
        return False
    vols = np.array([c.v for c in candles[-10:]])
    if vols.size == 0:
        return False
    avg_v = float(vols[:-1].mean())
    last = candles[-1]
    if avg_v <= 0 or last.v < 2.0 * avg_v:
        return False
    rng = last.h - last.l
    if rng <= 0:
        return False
    body = abs(last.c - last.o)
    return body / rng < 0.30


# ---------------------------------------------------------------------------

def analyze(candles: Sequence[Candle]) -> PatternReport:
    """One-shot full report. Cheap enough to call every tick."""
    sr = support_resistance(candles)
    st = structure(candles)
    fvgs = fair_value_gaps(candles)
    sw_up, sw_dn = liquidity_sweep(candles)
    return PatternReport(
        sr=sr,
        structure=st,
        fvgs=fvgs,
        liquidity_sweep_up=sw_up,
        liquidity_sweep_down=sw_dn,
        exhaustion_candle=exhaustion_candle(candles),
        volume_absorption=volume_absorption(candles),
    )
