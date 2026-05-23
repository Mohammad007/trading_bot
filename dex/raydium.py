"""
Raydium client.

Public endpoints used:
  GET https://api-v3.raydium.io/pools/info/list           (paginated)
  GET https://api-v3.raydium.io/main/auto-fee
  GET https://api.raydium.io/v2/main/price                (token prices)

We use it as a secondary source for price + liquidity. For new-pool
detection we fall back to DexScreener (it indexes Raydium quickly).
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import aiohttp

from utils.helpers import RateLimiter, async_retry
from utils.logger import get_logger

log = get_logger(__name__)

V3_BASE = "https://api-v3.raydium.io"
V2_BASE = "https://api.raydium.io"


class Raydium:
    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        self._rl = RateLimiter(rate=4.0, capacity=8.0)

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

    async def get_prices(self, mints: List[str]) -> Dict[str, float]:
        if not mints:
            return {}
        data = await self._get(f"{V2_BASE}/v2/main/price")
        if not isinstance(data, dict):
            return {}
        result = {}
        for m in mints:
            v = data.get(m)
            if v is not None:
                try:
                    result[m] = float(v)
                except (TypeError, ValueError):
                    continue
        return result

    async def list_pools(self, page: int = 1, page_size: int = 100) -> List[Dict[str, Any]]:
        data = await self._get(
            f"{V3_BASE}/pools/info/list",
            params={
                "poolType": "all",
                "poolSortField": "liquidity",
                "sortType": "desc",
                "pageSize": page_size,
                "page": page,
            },
        )
        if not data:
            return []
        return data.get("data", {}).get("data", []) or []

    async def find_pool_for(self, token_mint: str) -> Optional[Dict[str, Any]]:
        """Cheap lookup: scan first page of top pools for this mint."""
        pools = await self.list_pools(page=1, page_size=100)
        for p in pools:
            mint_a = p.get("mintA", {}).get("address")
            mint_b = p.get("mintB", {}).get("address")
            if token_mint in (mint_a, mint_b):
                return p
        return None


raydium = Raydium()
