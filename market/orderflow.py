"""
Order-flow analyzer for AMM markets.

Solana / EVM AMMs don't expose a traditional CLOB, so we synthesize
order-flow from per-trade buy/sell prints (DexScreener m5 txns and, in
EVM, decoded Swap events from the pool). We compute:

  - aggressive buy / sell imbalance
  - whale detection (size > Nx median)
  - rolling exhaustion (sellers giving up)
  - "spoofing" proxy: large liquidity adds that disappear before fills
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from threading import RLock
from typing import Deque, Dict, List, Optional

import numpy as np


@dataclass
class Print:
    ts_ms: int
    price: float
    size_usd: float
    is_buy: bool
    wallet: str = ""


@dataclass
class OrderFlowSnapshot:
    buys: int = 0
    sells: int = 0
    buy_volume_usd: float = 0.0
    sell_volume_usd: float = 0.0
    aggressive_buy_ratio: float = 0.0   # (buyVol - sellVol) / total
    whale_buys: int = 0
    whale_sells: int = 0
    avg_size_usd: float = 0.0
    exhaustion_score: float = 0.0        # 0..1, sellers fading

    @property
    def total_volume_usd(self) -> float:
        return self.buy_volume_usd + self.sell_volume_usd


class OrderFlowBook:
    """Per-token rolling tape of prints (default last 5 minutes)."""

    def __init__(self, max_age_ms: int = 5 * 60 * 1000, max_prints: int = 2000) -> None:
        self.max_age_ms = max_age_ms
        self.max_prints = max_prints
        self._prints: Deque[Print] = deque(maxlen=max_prints)
        self._lock = RLock()

    def add(self, p: Print) -> None:
        with self._lock:
            self._prints.append(p)

    def _live(self, now_ms: int) -> List[Print]:
        cutoff = now_ms - self.max_age_ms
        return [p for p in self._prints if p.ts_ms >= cutoff]

    def snapshot(self, now_ms: int) -> OrderFlowSnapshot:
        with self._lock:
            prints = self._live(now_ms)
        if not prints:
            return OrderFlowSnapshot()

        snap = OrderFlowSnapshot()
        sizes = np.array([p.size_usd for p in prints], dtype=np.float64)
        median = float(np.median(sizes)) if sizes.size else 0.0
        whale_threshold = max(median * 5.0, 250.0)

        for p in prints:
            if p.is_buy:
                snap.buys += 1
                snap.buy_volume_usd += p.size_usd
                if p.size_usd >= whale_threshold:
                    snap.whale_buys += 1
            else:
                snap.sells += 1
                snap.sell_volume_usd += p.size_usd
                if p.size_usd >= whale_threshold:
                    snap.whale_sells += 1

        total = snap.total_volume_usd
        snap.aggressive_buy_ratio = (
            (snap.buy_volume_usd - snap.sell_volume_usd) / total if total > 0 else 0.0
        )
        snap.avg_size_usd = float(sizes.mean())

        # Exhaustion: in the last third of the window, sell volume drops vs prior thirds.
        if len(prints) >= 30:
            third = max(len(prints) // 3, 1)
            early_sell = sum(p.size_usd for p in prints[:third] if not p.is_buy)
            late_sell = sum(p.size_usd for p in prints[-third:] if not p.is_buy)
            if early_sell > 0:
                drop = (early_sell - late_sell) / early_sell
                snap.exhaustion_score = max(0.0, min(1.0, drop))
        return snap


class OrderFlowCache:
    """Global token -> OrderFlowBook."""

    def __init__(self) -> None:
        self._store: Dict[str, OrderFlowBook] = {}
        self._lock = RLock()

    def book(self, token: str) -> OrderFlowBook:
        with self._lock:
            b = self._store.get(token)
            if b is None:
                b = OrderFlowBook()
                self._store[token] = b
            return b

    def add_print(self, token: str, **kw) -> None:
        self.book(token).add(Print(**kw))

    def snapshot(self, token: str, now_ms: int) -> OrderFlowSnapshot:
        return self.book(token).snapshot(now_ms)


orderflow = OrderFlowCache()
