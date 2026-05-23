"""
SQLite persistence layer.

Synchronous SQLite is fine for our throughput (low write rate). We wrap
calls with an asyncio.to_thread() helper where async paths need it.

Schema:
    - trades       : every buy/sell (paper + real)
    - positions    : open trades
    - pnl_daily    : daily PnL roll-up
    - ai_scores    : log AI predictions for accuracy analysis
    - tokens       : known tokens cache
    - wallets      : wallet activity (smart-money tracking)
    - rl_history   : reinforcement-learning state/action/reward
    - blacklist    : token blacklist
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

from config import settings
from utils.logger import get_logger

log = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    mode          TEXT    NOT NULL,
    side          TEXT    NOT NULL,
    token_mint    TEXT    NOT NULL,
    token_symbol  TEXT,
    dex           TEXT,
    amount_sol    REAL    NOT NULL,
    amount_token  REAL    NOT NULL,
    price_usd     REAL,
    price_sol     REAL,
    tx_sig        TEXT,
    ai_score      REAL,
    notes         TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_ts    ON trades(ts);
CREATE INDEX IF NOT EXISTS idx_trades_mint  ON trades(token_mint);

CREATE TABLE IF NOT EXISTS positions (
    token_mint    TEXT PRIMARY KEY,
    token_symbol  TEXT,
    dex           TEXT,
    entry_ts      INTEGER NOT NULL,
    entry_price   REAL    NOT NULL,
    amount_token  REAL    NOT NULL,
    amount_sol    REAL    NOT NULL,
    high_water    REAL    NOT NULL,
    take_profit   REAL,
    stop_loss     REAL,
    trailing_stop REAL,
    ai_score      REAL,
    mode          TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS pnl_daily (
    day           TEXT PRIMARY KEY,
    realized_usd  REAL NOT NULL DEFAULT 0,
    trades_count  INTEGER NOT NULL DEFAULT 0,
    wins          INTEGER NOT NULL DEFAULT 0,
    losses        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ai_scores (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    token_mint    TEXT NOT NULL,
    model         TEXT NOT NULL,
    score         REAL NOT NULL,
    realized_pct  REAL
);

CREATE TABLE IF NOT EXISTS tokens (
    mint          TEXT PRIMARY KEY,
    symbol        TEXT,
    name          TEXT,
    dex           TEXT,
    first_seen_ts INTEGER NOT NULL,
    last_seen_ts  INTEGER NOT NULL,
    liquidity_usd REAL,
    volume_24h    REAL,
    market_cap    REAL,
    blacklisted   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS wallets (
    address       TEXT PRIMARY KEY,
    label         TEXT,
    is_smart      INTEGER NOT NULL DEFAULT 0,
    win_count     INTEGER NOT NULL DEFAULT 0,
    loss_count    INTEGER NOT NULL DEFAULT 0,
    last_seen_ts  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rl_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    state_hash    TEXT NOT NULL,
    action        TEXT NOT NULL,
    reward        REAL NOT NULL,
    next_state    TEXT
);

CREATE TABLE IF NOT EXISTS blacklist (
    token_mint    TEXT PRIMARY KEY,
    reason        TEXT,
    added_ts      INTEGER NOT NULL
);
"""

_lock = threading.RLock()


class Database:
    """Thin sqlite3 wrapper. Connections are per-thread, created lazily."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path: str = path or settings.db_path
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_schema()

    # -- connection management ------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            self._local.conn = conn
        return conn

    @contextmanager
    def cursor(self):
        with _lock:
            cur = self._conn().cursor()
            try:
                yield cur
            finally:
                cur.close()

    def _init_schema(self) -> None:
        with self.cursor() as cur:
            cur.executescript(_SCHEMA)

    # -- sync helpers --------------------------------------------------------

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self.cursor() as cur:
            cur.execute(sql, tuple(params))
            return cur

    def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self.cursor() as cur:
            cur.execute(sql, tuple(params))
            return cur.fetchall()

    def fetchone(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        with self.cursor() as cur:
            cur.execute(sql, tuple(params))
            return cur.fetchone()

    # -- async wrappers ------------------------------------------------------

    async def aexecute(self, sql: str, params: Iterable[Any] = ()) -> None:
        await asyncio.to_thread(self.execute, sql, params)

    async def afetchall(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self.fetchall, sql, params)

    async def afetchone(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        return await asyncio.to_thread(self.fetchone, sql, params)

    # -- domain helpers ------------------------------------------------------

    def log_trade(self, **kw: Any) -> int:
        cols = ",".join(kw.keys())
        placeholders = ",".join("?" for _ in kw)
        with self.cursor() as cur:
            cur.execute(
                f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
                tuple(kw.values()),
            )
            return cur.lastrowid or 0

    def upsert_position(self, **kw: Any) -> None:
        """
        Full-row insert/update. All NOT NULL columns (entry_ts, entry_price,
        amount_token, amount_sol, high_water, mode) must be present.
        For partial updates use `update_position` instead.
        """
        cols = list(kw.keys())
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "token_mint")
        with self.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO positions ({",".join(cols)}) VALUES ({placeholders})
                ON CONFLICT(token_mint) DO UPDATE SET {updates}
                """,
                tuple(kw.values()),
            )

    def update_position(self, token_mint: str, **kw: Any) -> None:
        """Partial UPDATE - no INSERT, no NOT NULL pitfalls."""
        if not kw:
            return
        set_clause = ",".join(f"{c}=?" for c in kw.keys())
        params = list(kw.values()) + [token_mint]
        with self.cursor() as cur:
            cur.execute(
                f"UPDATE positions SET {set_clause} WHERE token_mint=?",
                tuple(params),
            )

    def delete_position(self, token_mint: str) -> None:
        self.execute("DELETE FROM positions WHERE token_mint=?", (token_mint,))

    def get_positions(self) -> list[sqlite3.Row]:
        return self.fetchall("SELECT * FROM positions ORDER BY entry_ts DESC")

    def add_blacklist(self, token_mint: str, reason: str, ts: int) -> None:
        self.execute(
            "INSERT OR REPLACE INTO blacklist (token_mint, reason, added_ts) VALUES (?,?,?)",
            (token_mint, reason, ts),
        )

    def is_blacklisted(self, token_mint: str) -> bool:
        row = self.fetchone("SELECT 1 FROM blacklist WHERE token_mint=?", (token_mint,))
        return row is not None

    def log_ai_score(self, ts: int, token_mint: str, model: str, score: float) -> None:
        self.execute(
            "INSERT INTO ai_scores (ts, token_mint, model, score) VALUES (?,?,?,?)",
            (ts, token_mint, model, score),
        )


# Single module-wide instance
db: Database = Database()
