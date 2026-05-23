"""
PnL helpers - realized and unrealized.

Realized PnL is reconstructed from the `trades` table using FIFO cost basis
within each token mint. Unrealized PnL is computed from open positions
+ current prices passed in.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional

from database.db import db
from trading.position_manager import position_manager


@dataclass
class RealizedSummary:
    total_sol: float
    total_usd_approx: float
    trades: int
    wins: int
    losses: int

    @property
    def winrate(self) -> float:
        n = self.wins + self.losses
        return self.wins / n if n else 0.0


def compute_realized_pnl(sol_usd_price: float = 150.0) -> RealizedSummary:
    rows = db.fetchall(
        "SELECT side, token_mint, amount_token, price_sol, amount_sol FROM trades ORDER BY ts ASC"
    )
    fifo: Dict[str, deque] = {}
    realized_sol = 0.0
    wins = 0
    losses = 0
    closed_count = 0

    for r in rows:
        mint = r["token_mint"]
        side = r["side"]
        amt = float(r["amount_token"] or 0)
        price = float(r["price_sol"] or 0)
        sol_amount = float(r["amount_sol"] or 0)
        if mint not in fifo:
            fifo[mint] = deque()
        if side == "BUY":
            fifo[mint].append([amt, price])  # remaining, cost basis
        else:  # SELL
            remaining_to_sell = amt
            while remaining_to_sell > 0 and fifo[mint]:
                lot_amt, lot_price = fifo[mint][0]
                take = min(lot_amt, remaining_to_sell)
                pnl = take * (price - lot_price)
                realized_sol += pnl
                if pnl > 0:
                    wins += 1
                else:
                    losses += 1
                closed_count += 1
                lot_amt -= take
                remaining_to_sell -= take
                if lot_amt <= 1e-12:
                    fifo[mint].popleft()
                else:
                    fifo[mint][0][0] = lot_amt
    return RealizedSummary(
        total_sol=realized_sol,
        total_usd_approx=realized_sol * sol_usd_price,
        trades=closed_count,
        wins=wins,
        losses=losses,
    )


def compute_unrealized_pnl(current_prices_sol: Dict[str, float]) -> float:
    """Returns total unrealized PnL in SOL across all open positions."""
    total = 0.0
    for p in position_manager.list():
        cur = current_prices_sol.get(p.token_mint, p.entry_price)
        total += (cur - p.entry_price) * p.amount_token
    return total
