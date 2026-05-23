"""
Winrate analytics over time windows.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List

from database.db import db


@dataclass
class WinrateWindow:
    days: int
    wins: int
    losses: int
    trades: int

    @property
    def winrate(self) -> float:
        return self.wins / max(self.trades, 1)


def winrate_last_n_days(n: int = 7) -> WinrateWindow:
    since = int((time.time() - n * 86400) * 1000)
    rows = db.fetchall(
        "SELECT day, wins, losses, trades_count FROM pnl_daily WHERE day >= date('now', ?)",
        (f"-{n} day",),
    )
    wins = sum(int(r["wins"]) for r in rows)
    losses = sum(int(r["losses"]) for r in rows)
    trades = sum(int(r["trades_count"]) for r in rows)
    return WinrateWindow(days=n, wins=wins, losses=losses, trades=trades)


def overall_winrate() -> WinrateWindow:
    row = db.fetchone(
        "SELECT COALESCE(SUM(wins),0) AS w, COALESCE(SUM(losses),0) AS l, "
        "COALESCE(SUM(trades_count),0) AS t FROM pnl_daily"
    )
    if not row:
        return WinrateWindow(days=0, wins=0, losses=0, trades=0)
    return WinrateWindow(days=0, wins=int(row["w"]), losses=int(row["l"]), trades=int(row["t"]))
