"""
Technical indicators (numpy only - no pandas, no TA-Lib).

Designed to be cheap to recompute on a small rolling buffer. Every
function returns a *single number for the most recent bar* unless
otherwise noted - that's what AI scoring needs. If you want a series,
each function has an `_series` variant.
"""
from __future__ import annotations

from typing import List, Sequence, Tuple

import numpy as np

from market.candles import Candle


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _arr(x: Sequence[float]) -> np.ndarray:
    return np.asarray(x, dtype=np.float64)


def _ema_series(x: np.ndarray, span: int) -> np.ndarray:
    if x.size == 0:
        return x
    alpha = 2.0 / (span + 1)
    out = np.empty_like(x)
    out[0] = x[0]
    for i in range(1, x.size):
        out[i] = alpha * x[i] + (1 - alpha) * out[i - 1]
    return out


# ---------------------------------------------------------------------------
# Trend / momentum
# ---------------------------------------------------------------------------

def ema(values: Sequence[float], span: int) -> float:
    a = _arr(values)
    if a.size == 0:
        return 0.0
    return float(_ema_series(a, span)[-1])


def sma(values: Sequence[float], window: int) -> float:
    a = _arr(values)
    if a.size == 0:
        return 0.0
    window = min(window, a.size)
    return float(a[-window:].mean())


def rsi(values: Sequence[float], period: int = 14) -> float:
    """Wilder's RSI. Returns 50 if not enough data."""
    a = _arr(values)
    if a.size < period + 1:
        return 50.0
    delta = np.diff(a)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    for i in range(period, gains.size):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))


def macd(values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[float, float, float]:
    """Returns (macd, signal, histogram) for the most recent bar."""
    a = _arr(values)
    if a.size < slow:
        return 0.0, 0.0, 0.0
    ema_f = _ema_series(a, fast)
    ema_s = _ema_series(a, slow)
    macd_line = ema_f - ema_s
    sig_line = _ema_series(macd_line, signal)
    hist = macd_line - sig_line
    return float(macd_line[-1]), float(sig_line[-1]), float(hist[-1])


# ---------------------------------------------------------------------------
# Volatility
# ---------------------------------------------------------------------------

def atr(candles: Sequence[Candle], period: int = 14) -> float:
    """Average True Range. Returns 0 if too few candles."""
    if len(candles) < 2:
        return 0.0
    highs = _arr([c.h for c in candles])
    lows = _arr([c.l for c in candles])
    closes = _arr([c.c for c in candles])
    prev_c = closes[:-1]
    tr = np.maximum.reduce([
        highs[1:] - lows[1:],
        np.abs(highs[1:] - prev_c),
        np.abs(lows[1:] - prev_c),
    ])
    if tr.size == 0:
        return 0.0
    window = min(period, tr.size)
    return float(tr[-window:].mean())


def bollinger(values: Sequence[float], window: int = 20, n_std: float = 2.0) -> Tuple[float, float, float]:
    """Returns (lower, middle, upper)."""
    a = _arr(values)
    if a.size == 0:
        return 0.0, 0.0, 0.0
    window = min(window, a.size)
    slice_ = a[-window:]
    mid = float(slice_.mean())
    std = float(slice_.std()) if slice_.size > 1 else 0.0
    return mid - n_std * std, mid, mid + n_std * std


# ---------------------------------------------------------------------------
# Volume / flow
# ---------------------------------------------------------------------------

def vwap(candles: Sequence[Candle]) -> float:
    """Volume-weighted average price using (h+l+c)/3 as the typical price."""
    if not candles:
        return 0.0
    typ = _arr([(c.h + c.l + c.c) / 3.0 for c in candles])
    vol = _arr([c.v for c in candles])
    if vol.sum() == 0:
        return float(typ.mean())
    return float((typ * vol).sum() / vol.sum())


def volume_spike_z(candles: Sequence[Candle], lookback: int = 20) -> float:
    """Z-score of the latest bar's volume vs lookback mean. Positive = spike."""
    if len(candles) < 3:
        return 0.0
    vols = _arr([c.v for c in candles[-(lookback + 1):]])
    if vols.size < 2:
        return 0.0
    base = vols[:-1]
    mu = base.mean()
    sigma = base.std()
    if sigma <= 0:
        return 0.0
    return float((vols[-1] - mu) / sigma)


# ---------------------------------------------------------------------------
# Composite helpers used by the AI
# ---------------------------------------------------------------------------

def momentum(values: Sequence[float], lookback: int = 8) -> float:
    """% change over `lookback` bars."""
    a = _arr(values)
    if a.size < 2:
        return 0.0
    lookback = min(lookback, a.size - 1)
    base = a[-(lookback + 1)]
    if base == 0:
        return 0.0
    return float((a[-1] - base) / abs(base))


def slope(values: Sequence[float], window: int = 10) -> float:
    """Linear regression slope (price-units per bar) over the last `window`."""
    a = _arr(values)
    if a.size < 3:
        return 0.0
    window = min(window, a.size)
    y = a[-window:]
    x = np.arange(window, dtype=np.float64)
    x_mean = x.mean()
    y_mean = y.mean()
    denom = ((x - x_mean) ** 2).sum()
    if denom == 0:
        return 0.0
    return float(((x - x_mean) * (y - y_mean)).sum() / denom)
