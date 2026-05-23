"""
Jupiter v6 swap client.

Used in REAL mode for Solana spot swaps. We hit:
  GET  /quote
  POST /swap

The endpoint returns an unsigned base64 VersionedTransaction that we
sign with the real wallet and submit through the RPC.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import aiohttp

from config import settings
from trading.real_wallet import RealWallet, get_real_wallet
from utils.helpers import async_retry
from utils.logger import get_logger

log = get_logger(__name__)

WSOL_MINT = "So11111111111111111111111111111111111111112"


class JupiterSwap:
    """Async Jupiter v6 client."""

    def __init__(self) -> None:
        self.base = settings.jupiter_base.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=20),
                    headers={"User-Agent": "ai-solana-sniper/1.0"},
                )
            return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @async_retry(attempts=3, delay=0.5)
    async def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Get a swap quote. `amount` is in lamports / smallest units."""
        session = await self._ensure_session()
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps or settings.slippage_bps),
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false",
        }
        url = f"{self.base}/quote"
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                log.warning("Jupiter quote %s -> %d", output_mint[:8], resp.status)
                return None
            return await resp.json()

    @async_retry(attempts=3, delay=0.5)
    async def get_swap_tx(
        self,
        quote: Dict[str, Any],
        user_pubkey: str,
        priority_fee_microlamports: Optional[int] = None,
    ) -> Optional[str]:
        """Return unsigned base64 VersionedTransaction or None."""
        session = await self._ensure_session()
        body = {
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "useSharedAccounts": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": priority_fee_microlamports
            or settings.priority_fee_microlamports,
        }
        url = f"{self.base}/swap"
        async with session.post(url, json=body) as resp:
            if resp.status != 200:
                txt = await resp.text()
                log.warning("Jupiter /swap %d: %s", resp.status, txt[:200])
                return None
            data = await resp.json()
            return data.get("swapTransaction")

    # -- high-level ----------------------------------------------------------

    async def swap_sol_for_token(
        self,
        token_mint: str,
        amount_sol: float,
        slippage_bps: Optional[int] = None,
    ) -> Optional[str]:
        """BUY: spend `amount_sol` SOL to receive `token_mint`."""
        wallet = await get_real_wallet()
        if wallet is None:
            log.error("BUY blocked: real wallet unavailable.")
            return None

        lamports = int(amount_sol * 1_000_000_000)
        quote = await self.get_quote(WSOL_MINT, token_mint, lamports, slippage_bps)
        if not quote:
            return None
        unsigned = await self.get_swap_tx(quote, wallet.pubkey_str)
        if not unsigned:
            return None
        return await self._sign_and_submit(wallet, unsigned)

    async def swap_token_for_sol(
        self,
        token_mint: str,
        amount_token_units: int,
        slippage_bps: Optional[int] = None,
    ) -> Optional[str]:
        """SELL: convert `amount_token_units` of token (raw integer units) back to SOL."""
        wallet = await get_real_wallet()
        if wallet is None:
            log.error("SELL blocked: real wallet unavailable.")
            return None

        quote = await self.get_quote(token_mint, WSOL_MINT, amount_token_units, slippage_bps)
        if not quote:
            return None
        unsigned = await self.get_swap_tx(quote, wallet.pubkey_str)
        if not unsigned:
            return None
        return await self._sign_and_submit(wallet, unsigned)

    async def _sign_and_submit(self, wallet: RealWallet, unsigned_b64: str) -> Optional[str]:
        try:
            signed = wallet.sign_versioned_tx(unsigned_b64)
        except Exception as exc:
            log.error("Failed to sign Jupiter tx: %s", exc)
            return None
        return await wallet.send_signed_tx(signed)


jupiter = JupiterSwap()
