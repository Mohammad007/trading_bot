"""
Position manager.

Holds in-memory mirror of the `positions` table and provides atomic
open/update/close operations. Used by both the buy and sell modules.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from config import settings
from database.db import db
from utils.helpers import now_ms
from utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class Position:
    token_mint: str
    token_symbol: str
    dex: str
    entry_ts: int
    entry_price: float        # SOL per token
    amount_token: float
    amount_sol: float
    high_water: float
    take_profit: float
    stop_loss: float
    trailing_stop: float
    ai_score: float
    mode: str

    def unrealized_pct(self, current_price_sol: float) -> float:
        if self.entry_price == 0:
            return 0.0
        return (current_price_sol - self.entry_price) / self.entry_price


@dataclass
class PositionManager:
    positions: Dict[str, Position] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def load(self) -> None:
        rows = db.get_positions()
        for r in rows:
            p = Position(
                token_mint=r["token_mint"],
                token_symbol=r["token_symbol"] or "",
                dex=r["dex"] or "",
                entry_ts=int(r["entry_ts"]),
                entry_price=float(r["entry_price"]),
                amount_token=float(r["amount_token"]),
                amount_sol=float(r["amount_sol"]),
                high_water=float(r["high_water"]),
                take_profit=float(r["take_profit"] or settings.take_profit),
                stop_loss=float(r["stop_loss"] or settings.stop_loss),
                trailing_stop=float(r["trailing_stop"] or settings.trailing_stop),
                ai_score=float(r["ai_score"] or 0),
                mode=r["mode"] or settings.mode,
            )
            self.positions[p.token_mint] = p
        log.info("Loaded %d open positions.", len(self.positions))

    # ------------------------------------------------------------------

    async def open(
        self,
        token_mint: str,
        token_symbol: str,
        dex: str,
        entry_price_sol: float,
        amount_token: float,
        amount_sol: float,
        ai_score: float,
    ) -> Position:
        async with self._lock:
            pos = Position(
                token_mint=token_mint,
                token_symbol=token_symbol,
                dex=dex,
                entry_ts=now_ms(),
                entry_price=entry_price_sol,
                amount_token=amount_token,
                amount_sol=amount_sol,
                high_water=entry_price_sol,
                take_profit=settings.take_profit,
                stop_loss=settings.stop_loss,
                trailing_stop=settings.trailing_stop,
                ai_score=ai_score,
                mode=settings.mode,
            )
            self.positions[token_mint] = pos
            db.upsert_position(
                token_mint=pos.token_mint,
                token_symbol=pos.token_symbol,
                dex=pos.dex,
                entry_ts=pos.entry_ts,
                entry_price=pos.entry_price,
                amount_token=pos.amount_token,
                amount_sol=pos.amount_sol,
                high_water=pos.high_water,
                take_profit=pos.take_profit,
                stop_loss=pos.stop_loss,
                trailing_stop=pos.trailing_stop,
                ai_score=pos.ai_score,
                mode=pos.mode,
            )
            return pos

    async def update_price(self, token_mint: str, current_price: float) -> Optional[Position]:
        async with self._lock:
            pos = self.positions.get(token_mint)
            if pos is None:
                return None
            if current_price > pos.high_water:
                pos.high_water = current_price
                db.update_position(pos.token_mint, high_water=pos.high_water)
            return pos

    async def close(self, token_mint: str) -> Optional[Position]:
        async with self._lock:
            pos = self.positions.pop(token_mint, None)
            if pos:
                db.delete_position(token_mint)
            return pos

    def list(self) -> List[Position]:
        return list(self.positions.values())

    def has(self, token_mint: str) -> bool:
        return token_mint in self.positions

    def count(self) -> int:
        return len(self.positions)


position_manager = PositionManager()
position_manager.load()
