"""
Performance summaries: best/worst trades, AI accuracy.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from database.db import db


def best_trades(limit: int = 5) -> List[Dict[str, Any]]:
    """Top-PnL closed trades (by realized SOL on the SELL row)."""
    rows = db.fetchall(
        """
        SELECT
            s.token_symbol,
            s.token_mint,
            s.amount_sol - COALESCE(b.amount_sol, 0) AS pnl_sol,
            s.ts
        FROM trades s
        LEFT JOIN trades b ON b.token_mint = s.token_mint AND b.side = 'BUY'
        WHERE s.side = 'SELL'
        ORDER BY pnl_sol DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [dict(r) for r in rows]


def worst_trades(limit: int = 5) -> List[Dict[str, Any]]:
    rows = db.fetchall(
        """
        SELECT
            s.token_symbol,
            s.token_mint,
            s.amount_sol - COALESCE(b.amount_sol, 0) AS pnl_sol,
            s.ts
        FROM trades s
        LEFT JOIN trades b ON b.token_mint = s.token_mint AND b.side = 'BUY'
        WHERE s.side = 'SELL'
        ORDER BY pnl_sol ASC
        LIMIT ?
        """,
        (limit,),
    )
    return [dict(r) for r in rows]


def ai_accuracy(model: str = "xgb", threshold: float = 0.65) -> Tuple[int, int, float]:
    """
    Return (calls_above_threshold, calls_realized_positive, accuracy).

    `realized_pct` must have been backfilled by analytics jobs (best-effort:
    we infer realized return from the nearest SELL in the same mint).
    """
    rows = db.fetchall(
        """
        SELECT a.score, a.token_mint, s.amount_sol - COALESCE(b.amount_sol, 0) AS pnl
        FROM ai_scores a
        LEFT JOIN trades b ON b.token_mint = a.token_mint AND b.side = 'BUY'
        LEFT JOIN trades s ON s.token_mint = a.token_mint AND s.side = 'SELL'
        WHERE a.model = ?
        """,
        (model,),
    )
    above = 0
    correct = 0
    for r in rows:
        if r["score"] is None or r["score"] < threshold:
            continue
        above += 1
        if r["pnl"] is not None and r["pnl"] > 0:
            correct += 1
    acc = correct / above if above else 0.0
    return above, correct, acc
