"""
In-memory OHLCV candle cache.

For meme-coin tokens we usually don't have a clean exchange-quality 1m
candle feed. So we *synthesize* candles from the tick stream of price
updates the bot already receives (DexScreener snapshots, RPC pool reads,
EVM Sync events). Each token gets a small rolling buffer per timeframe.

Why we cache:
- AI inference needs 8-32 candles; recomputing from REST every tick wastes
  10s of ms and quota.
- Trailing stop and ATR-based sizing need O(N) lookback constantly.
- Pattern detector (S/R, BOS, FVG) is a windowed pass over the buffer.

Memory budget: ~5kb per token per timeframe @ 100 candles. With 5000
tracked tokens that's 25MB total - comfortable on a 4GB VPS.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from threading import RLock
from typing import Deque, Dict, List, Optional, Tuple


@dataclass
class Candle:
    ts: int           # bucket start (ms, UTC)
    o: float
    h: float
    l: float
    c: float
    v: float          # quote-volume in USD or SOL depending on source
    buys: int = 0
    sells: int = 0

    @property
    def body_pct(self) -> float:
        if self.o <= 0:
            return 0.0
        return (self.c - self.o) / self.o

    @property
    def range_pct(self) -> float:
        if self.l <= 0:
            return 0.0
        return (self.h - self.l) / self.l


# Common timeframes in seconds.
TIMEFRAMES: Dict[str, int] = {
    "5s": 5,
    "30s": 30,
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
}


def bucket_start(ts_ms: int, tf_seconds: int) -> int:
    """Floor `ts_ms` to the timeframe bucket."""
    tf_ms = tf_seconds * 1000
    return (ts_ms // tf_ms) * tf_ms


# ---------------------------------------------------------------------------

class CandleBuffer:
    """Per-token, per-timeframe rolling OHLCV buffer."""

    __slots__ = ("tf_sec", "capacity", "_buf", "_lock")

    def __init__(self, tf_sec: int, capacity: int = 256) -> None:
        self.tf_sec = tf_sec
        self.capacity = capacity
        self._buf: Deque[Candle] = deque(maxlen=capacity)
        self._lock = RLock()

    def add_tick(self, price: float, vol_quote: float, ts_ms: Optional[int] = None,
                 is_buy: Optional[bool] = None) -> Candle:
        """Fold a tick into the current bucket (creates if new)."""
        if price <= 0:
            return self._buf[-1] if self._buf else Candle(0, 0, 0, 0, 0, 0)
        ts = ts_ms if ts_ms is not None else int(time.time() * 1000)
        bstart = bucket_start(ts, self.tf_sec)
        with self._lock:
            if not self._buf or self._buf[-1].ts != bstart:
                c = Candle(ts=bstart, o=price, h=price, l=price, c=price, v=vol_quote)
                if is_buy is True:
                    c.buys = 1
                elif is_buy is False:
                    c.sells = 1
                self._buf.append(c)
                return c
            c = self._buf[-1]
            if price > c.h:
                c.h = price
            if price < c.l:
                c.l = price
            c.c = price
            c.v += max(0.0, vol_quote)
            if is_buy is True:
                c.buys += 1
            elif is_buy is False:
                c.sells += 1
            return c

    def snapshot(self, n: Optional[int] = None) -> List[Candle]:
        with self._lock:
            if n is None or n >= len(self._buf):
                return list(self._buf)
            return list(self._buf)[-n:]

    def last(self) -> Optional[Candle]:
        with self._lock:
            return self._buf[-1] if self._buf else None

    def __len__(self) -> int:
        return len(self._buf)


# ---------------------------------------------------------------------------

class CandleCache:
    """Global cache: token_id -> {timeframe -> CandleBuffer}."""

    def __init__(self, default_timeframes: Optional[List[str]] = None) -> None:
        self.timeframes: List[str] = default_timeframes or ["30s", "1m", "5m", "15m"]
        self._store: Dict[str, Dict[str, CandleBuffer]] = {}
        self._lock = RLock()

    def _ensure(self, token: str) -> Dict[str, CandleBuffer]:
        with self._lock:
            tfs = self._store.get(token)
            if tfs is None:
                tfs = {tf: CandleBuffer(TIMEFRAMES[tf]) for tf in self.timeframes}
                self._store[token] = tfs
            return tfs

    def add_tick(
        self,
        token: str,
        price: float,
        vol_quote: float = 0.0,
        ts_ms: Optional[int] = None,
        is_buy: Optional[bool] = None,
    ) -> None:
        tfs = self._ensure(token)
        for buf in tfs.values():
            buf.add_tick(price=price, vol_quote=vol_quote, ts_ms=ts_ms, is_buy=is_buy)

    def candles(self, token: str, tf: str = "1m", n: Optional[int] = None) -> List[Candle]:
        tfs = self._store.get(token)
        if tfs is None or tf not in tfs:
            return []
        return tfs[tf].snapshot(n)

    def closes(self, token: str, tf: str = "1m", n: Optional[int] = None) -> List[float]:
        return [c.c for c in self.candles(token, tf, n)]

    def highs(self, token: str, tf: str = "1m", n: Optional[int] = None) -> List[float]:
        return [c.h for c in self.candles(token, tf, n)]

    def lows(self, token: str, tf: str = "1m", n: Optional[int] = None) -> List[float]:
        return [c.l for c in self.candles(token, tf, n)]

    def volumes(self, token: str, tf: str = "1m", n: Optional[int] = None) -> List[float]:
        return [c.v for c in self.candles(token, tf, n)]

    def prune(self, keep_tokens: int = 5000) -> None:
        """Drop oldest tokens if cache grows too big."""
        with self._lock:
            if len(self._store) <= keep_tokens:
                return
            # Drop tokens whose newest candle is oldest.
            ranked: List[Tuple[str, int]] = []
            for tok, tfs in self._store.items():
                last_ts = 0
                for buf in tfs.values():
                    c = buf.last()
                    if c and c.ts > last_ts:
                        last_ts = c.ts
                ranked.append((tok, last_ts))
            ranked.sort(key=lambda x: x[1])
            for tok, _ in ranked[: len(self._store) - keep_tokens]:
                self._store.pop(tok, None)


candles = CandleCache()
