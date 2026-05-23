"""
Meteora client (DLMM + Dynamic AMM).

Uses the public read API at https://app.meteora.ag/clmm-api and
https://amm-v2.meteora.ag. We only need read-side data; swaps are routed
via Jupiter.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import aiohttp

from utils.helpers import RateLimiter, async_retry
from utils.logger import get_logger

log = get_logger(__name__)

CLMM_BASE = "https://dlmm-api.meteora.ag"
AMM_BASE = "https://amm-v2.meteora.ag"


class Meteora:
    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        self._rl = RateLimiter(rate=3.0, capacity=6.0)

    async def _ensure(self) -> aiohttp.ClientSession:
        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=15),
                    headers={"User-Agent": "ai-solana-sniper/1.0"},
                )
            return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @async_retry(attempts=3, delay=0.5)
    async def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        await self._rl.acquire()
        session = await self._ensure()
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return None
            return await resp.json()

    async def list_dlmm_pairs(self) -> List[Dict[str, Any]]:
        data = await self._get(f"{CLMM_BASE}/pair/all")
        if isinstance(data, list):
            return data
        return []

    async def find_pair(self, token_mint: str) -> Optional[Dict[str, Any]]:
        pairs = await self.list_dlmm_pairs()
        for p in pairs:
            if token_mint in (p.get("mint_x"), p.get("mint_y")):
                return p
        return None


meteora = Meteora()
