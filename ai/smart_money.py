"""
Smart-money wallet scorer.

Maintains a rolling stats table for tracked wallets. Each wallet gets
a 0..1 'smart' score from realised winrate + average return + clustering
behavior. Score is consulted by smart_entry when one of a token's recent
buyers is in our database.

Wallet PnL inference is best-effort: when we see a wallet's buy and a
later sell of the same token, we credit the realized return. For the
copy-trading flow, we ask DexScreener for the recent buyers list (top-
holders endpoint is paid-tier on most providers) - in PAPER mode we
fall back to a heuristic based on whale-print observation in orderflow.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Dict, List, Optional

from database.db import db
from utils.helpers import clamp, now_ms


@dataclass
class WalletStats:
    address: str
    wins: int = 0
    losses: int = 0
    realized_pct_sum: float = 0.0
    realized_count: int = 0
    last_seen: int = 0

    @property
    def winrate(self) -> float:
        n = self.wins + self.losses
        return self.wins / n if n else 0.5

    @property
    def avg_return(self) -> float:
        return self.realized_pct_sum / self.realized_count if self.realized_count else 0.0

    @property
    def score(self) -> float:
        if self.wins + self.losses < 3:
            return 0.3
        base = self.winrate
        boost = clamp(self.avg_return * 0.5, -0.3, 0.4)
        return clamp(base + boost, 0.0, 1.0)


class SmartMoney:
    def __init__(self) -> None:
        self._wallets: Dict[str, WalletStats] = {}
        self._lock = RLock()
        self._load()

    # -- persistence (lightweight) ------------------------------------------

    def _load(self) -> None:
        try:
            rows = db.fetchall("SELECT address, win_count, loss_count, last_seen_ts FROM wallets")
        except Exception:
            return
        for r in rows:
            w = WalletStats(
                address=r["address"],
                wins=int(r["win_count"] or 0),
                losses=int(r["loss_count"] or 0),
                last_seen=int(r["last_seen_ts"] or 0),
            )
            self._wallets[w.address] = w

    def _persist(self, w: WalletStats) -> None:
        try:
            db.execute(
                """
                INSERT INTO wallets (address, label, is_smart, win_count, loss_count, last_seen_ts)
                VALUES (?, '', ?, ?, ?, ?)
                ON CONFLICT(address) DO UPDATE SET
                    is_smart=excluded.is_smart,
                    win_count=excluded.win_count,
                    loss_count=excluded.loss_count,
                    last_seen_ts=excluded.last_seen_ts
                """,
                (w.address, 1 if w.score >= 0.65 else 0, w.wins, w.losses, w.last_seen),
            )
        except Exception:
            pass

    # -- read API -----------------------------------------------------------

    def score(self, addresses: List[str]) -> float:
        """Aggregate score across a list of recent buyers."""
        if not addresses:
            return 0.0
        scores = []
        with self._lock:
            for a in addresses:
                w = self._wallets.get(a)
                if w:
                    scores.append(w.score)
        if not scores:
            return 0.0
        # Take the max - we only need ONE smart wallet to confirm.
        return max(scores)

    def is_smart(self, address: str, threshold: float = 0.65) -> bool:
        with self._lock:
            w = self._wallets.get(address)
            return bool(w and w.score >= threshold)

    # -- write API ----------------------------------------------------------

    def record_observation(self, address: str) -> None:
        if not address:
            return
        with self._lock:
            w = self._wallets.get(address)
            if w is None:
                w = WalletStats(address=address)
                self._wallets[address] = w
            w.last_seen = now_ms()

    def record_outcome(self, address: str, realized_pct: float) -> None:
        """Update a wallet's win/loss after a tracked entry resolves."""
        if not address:
            return
        with self._lock:
            w = self._wallets.get(address)
            if w is None:
                w = WalletStats(address=address)
                self._wallets[address] = w
            if realized_pct > 0:
                w.wins += 1
            else:
                w.losses += 1
            w.realized_pct_sum += realized_pct
            w.realized_count += 1
            w.last_seen = now_ms()
            self._persist(w)


smart_money = SmartMoney()
