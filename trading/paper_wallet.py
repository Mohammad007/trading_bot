"""
Paper trading wallet (production-parity simulator).

Simulates REAL mode execution as faithfully as possible:
  - slippage based on trade-size vs pool liquidity
  - base + priority gas fees deducted from SOL balance
  - random transaction-failure rate
  - random execution latency (asyncio.sleep before fill)

The point: whatever PnL & winrate you see in PAPER should be a believable
forecast of REAL mode performance. Toggle realism via env:
  PAPER_SIMULATION_REALISM=full   (default - production parity)
  PAPER_SIMULATION_REALISM=instant (legacy fast/idealized)
"""
from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

import orjson

from config import ROOT_DIR, settings
from database.db import db
from utils.helpers import safe_div
from utils.logger import get_logger

log = get_logger(__name__)

# Conservative default SOL price for converting USDT starting balance.
_SOL_USD_DEFAULT = 150.0

# -- Real-execution simulation knobs ----------------------------------------
_REALISM = os.getenv("PAPER_SIMULATION_REALISM", "full").lower()
_SIM_FULL = _REALISM == "full"

# Per-swap costs (calibrated against typical Solana mainnet conditions).
GAS_BASE_SOL = 0.000005           # base tx fee
GAS_PRIORITY_SOL = 0.00025        # average priority fee for a competitive snipe
SLIPPAGE_BASE_PCT = 0.005         # 0.5% min slippage (Jupiter routes)
SLIPPAGE_DEPTH_FACTOR = 0.5       # how much depth-impact adds to slippage
TX_FAILURE_RATE = 0.05            # 5% of submitted txs fail on mainnet
LATENCY_MIN_S = 0.6
LATENCY_MAX_S = 2.2


def _simulated_slippage(amount_sol: float, liquidity_usd: float, sol_usd: float = 150.0) -> float:
    """
    Returns a slippage fraction (e.g. 0.03 = 3%). Larger swaps in thinner
    pools hurt exponentially - matches actual AMM x*y=k behavior.
    """
    if not _SIM_FULL:
        return 0.0
    if liquidity_usd <= 0:
        return SLIPPAGE_BASE_PCT + 0.10   # treat unknown liq as thin
    swap_usd = amount_sol * sol_usd
    pool_sol_equivalent = max(liquidity_usd / 2.0, 1.0)   # one side of the pool
    impact = swap_usd / pool_sol_equivalent
    return SLIPPAGE_BASE_PCT + impact * SLIPPAGE_DEPTH_FACTOR


def _gas_cost_sol() -> float:
    if not _SIM_FULL:
        return 0.0
    return GAS_BASE_SOL + GAS_PRIORITY_SOL


async def _maybe_simulate_latency() -> None:
    if not _SIM_FULL:
        return
    await asyncio.sleep(random.uniform(LATENCY_MIN_S, LATENCY_MAX_S))


def _maybe_simulate_failure() -> bool:
    """Returns True if the transaction should be treated as failed."""
    if not _SIM_FULL:
        return False
    return random.random() < TX_FAILURE_RATE


@dataclass
class PaperHolding:
    amount: float          # token units
    avg_cost_sol: float    # cost basis (SOL per token)


@dataclass
class PaperWallet:
    """
    A virtual wallet. Persisted to JSON at `${DATA_DIR}/paper_wallet.json`
    so balances survive restarts (Railway volume / Docker volume / disk).
    """

    sol_balance: float = 0.0
    usdt_balance: float = 0.0
    holdings: Dict[str, PaperHolding] = field(default_factory=dict)
    state_path: Path = field(default_factory=lambda: Path(settings.data_dir) / "paper_wallet.json")
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # -- persistence ---------------------------------------------------------

    def load(self) -> None:
        if not self.state_path.exists():
            self._initialize_fresh()
            self.save()
            return
        try:
            raw = self.state_path.read_bytes()
            data = orjson.loads(raw)
            self.sol_balance = float(data.get("sol_balance", 0.0))
            self.usdt_balance = float(data.get("usdt_balance", 0.0))
            self.holdings = {
                mint: PaperHolding(
                    amount=float(h["amount"]),
                    avg_cost_sol=float(h["avg_cost_sol"]),
                )
                for mint, h in data.get("holdings", {}).items()
            }
            log.info("Paper wallet loaded (SOL=%.4f USDT=%.2f holdings=%d)",
                     self.sol_balance, self.usdt_balance, len(self.holdings))
        except Exception as exc:
            log.warning("Failed to load paper wallet (%s); reinitializing.", exc)
            self._initialize_fresh()
            self.save()

    def save(self) -> None:
        # Cast everything to plain python floats - numpy.float64 (which sneaks
        # in via chart_ai / position_sizing) is not JSON-serializable by orjson
        # without OPT_SERIALIZE_NUMPY, and even with it we prefer plain floats
        # on disk so the file is portable.
        data = {
            "sol_balance": float(self.sol_balance),
            "usdt_balance": float(self.usdt_balance),
            "holdings": {
                m: {"amount": float(h.amount), "avg_cost_sol": float(h.avg_cost_sol)}
                for m, h in self.holdings.items()
            },
        }
        try:
            self.state_path.write_bytes(
                orjson.dumps(data, option=orjson.OPT_INDENT_2 | orjson.OPT_SERIALIZE_NUMPY)
            )
        except Exception as exc:
            # Never let a serialization hiccup break the trade flow.
            log.warning("paper_wallet.save() failed (will retry next trade): %s", exc)

    def _initialize_fresh(self) -> None:
        usdt = settings.paper_starting_balance_usdt
        self.usdt_balance = usdt
        # Convert half the USDT into SOL so we can trade.
        sol_from_usdt = (usdt * 0.5) / _SOL_USD_DEFAULT
        self.sol_balance = sol_from_usdt
        self.usdt_balance = usdt * 0.5
        self.holdings = {}
        log.info("Initialized paper wallet: %.4f SOL + %.2f USDT", self.sol_balance, self.usdt_balance)

    # -- read API ------------------------------------------------------------

    def balance_sol(self) -> float:
        return self.sol_balance

    def balance_usdt(self) -> float:
        return self.usdt_balance

    def holding(self, mint: str) -> Optional[PaperHolding]:
        return self.holdings.get(mint)

    def equity_sol(self, prices_sol: Dict[str, float]) -> float:
        """Total equity expressed in SOL, given a {mint: price_in_sol} map."""
        equity = self.sol_balance
        for mint, h in self.holdings.items():
            equity += h.amount * prices_sol.get(mint, h.avg_cost_sol)
        return equity

    # -- write API -----------------------------------------------------------

    async def buy(
        self,
        token_mint: str,
        amount_sol: float,
        price_sol_per_token: float,
        symbol: str = "",
        dex: str = "",
        ai_score: Optional[float] = None,
        liquidity_usd: float = 0.0,
    ) -> bool:
        """Spend SOL to acquire tokens. Simulates real Solana execution."""
        amount_sol = float(amount_sol)
        price_sol_per_token = float(price_sol_per_token)
        liquidity_usd = float(liquidity_usd)
        if amount_sol <= 0:
            return False
        # Simulate latency BEFORE locking so concurrent buys can be in flight.
        await _maybe_simulate_latency()
        # Random tx failure (REAL mode equivalent of slippage exceeded /
        # block not landing / priority too low).
        if _maybe_simulate_failure():
            log.warning("[SIM] tx failed for %s (network simulation)", symbol or token_mint[:8])
            return False

        slippage = _simulated_slippage(amount_sol, liquidity_usd)
        gas = _gas_cost_sol()
        effective_price = price_sol_per_token * (1.0 + slippage)

        async with self._lock:
            need = amount_sol + gas
            if self.sol_balance < need:
                log.warning("Paper buy rejected: insufficient SOL (%.4f < %.4f incl. gas)",
                            self.sol_balance, need)
                return False
            tokens = float(safe_div(amount_sol, effective_price, 0.0))
            if tokens <= 0:
                log.warning("Paper buy rejected: price=%s yields 0 tokens", effective_price)
                return False

            self.sol_balance = float(self.sol_balance - need)
            existing = self.holdings.get(token_mint)
            if existing is None:
                self.holdings[token_mint] = PaperHolding(
                    amount=tokens, avg_cost_sol=effective_price
                )
            else:
                total_cost = float(existing.amount * existing.avg_cost_sol) + amount_sol
                new_amount = float(existing.amount + tokens)
                existing.amount = new_amount
                existing.avg_cost_sol = float(
                    safe_div(total_cost, new_amount, effective_price)
                )
            log.debug("[SIM BUY] slip=%.2f%% gas=%.5f SOL price %.8f -> %.8f",
                      slippage * 100, gas, price_sol_per_token, effective_price)

            self.save()
            db.log_trade(
                ts=int(time.time() * 1000),
                mode="PAPER",
                side="BUY",
                token_mint=token_mint,
                token_symbol=symbol,
                dex=dex,
                amount_sol=amount_sol,
                amount_token=tokens,
                price_sol=price_sol_per_token,
                ai_score=ai_score,
                notes="paper",
            )
            log.info("[PAPER BUY] %s %s tokens=%.4f sol=%.4f price=%.8f",
                     symbol or token_mint[:6], dex, tokens, amount_sol, price_sol_per_token)
            return True

    async def sell(
        self,
        token_mint: str,
        amount_token: float,
        price_sol_per_token: float,
        symbol: str = "",
        dex: str = "",
        ai_score: Optional[float] = None,
        liquidity_usd: float = 0.0,
    ) -> bool:
        """Sell `amount_token` units. Use -1 to sell all. Simulates real execution."""
        amount_token = float(amount_token)
        price_sol_per_token = float(price_sol_per_token)
        liquidity_usd = float(liquidity_usd)

        # Pre-lock: simulate network latency + possible failure first.
        await _maybe_simulate_latency()
        if _maybe_simulate_failure():
            log.warning("[SIM] sell tx failed for %s", token_mint[:8])
            return False

        async with self._lock:
            holding = self.holdings.get(token_mint)
            if holding is None or holding.amount <= 0:
                log.warning("Paper sell rejected: no holding for %s", token_mint[:8])
                return False
            if amount_token < 0 or amount_token > holding.amount:
                amount_token = float(holding.amount)
            if amount_token <= 0:
                return False

            # Slippage on sell (loss-side): receive LESS than mid-price.
            notional_sol = amount_token * price_sol_per_token
            slippage = _simulated_slippage(notional_sol, liquidity_usd)
            gas = _gas_cost_sol()
            effective_price = price_sol_per_token * (1.0 - slippage)
            sol_in = float(amount_token * effective_price - gas)
            sol_in = max(sol_in, 0.0)

            self.sol_balance = float(self.sol_balance + sol_in)
            holding.amount = float(holding.amount - amount_token)
            if holding.amount <= 1e-12:
                del self.holdings[token_mint]
            log.debug("[SIM SELL] slip=%.2f%% gas=%.5f SOL price %.8f -> %.8f",
                      slippage * 100, gas, price_sol_per_token, effective_price)

            self.save()
            db.log_trade(
                ts=int(time.time() * 1000),
                mode="PAPER",
                side="SELL",
                token_mint=token_mint,
                token_symbol=symbol,
                dex=dex,
                amount_sol=sol_in,
                amount_token=amount_token,
                price_sol=price_sol_per_token,
                ai_score=ai_score,
                notes="paper",
            )
            log.info("[PAPER SELL] %s %s tokens=%.4f sol=%.4f price=%.8f",
                     symbol or token_mint[:6], dex, amount_token, sol_in, price_sol_per_token)
            return True


# Singleton
paper_wallet = PaperWallet()
paper_wallet.load()
