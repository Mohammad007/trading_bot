"""
DexScreener client.

Endpoints used:
  GET /latest/dex/tokens/{mint}
  GET /latest/dex/search?q=
  GET /token-boosts/latest/v1            (trending boosted tokens)
  GET /token-boosts/top/v1
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import aiohttp

from config import settings
from dex import TokenSnapshot
from utils.helpers import RateLimiter, async_retry
from utils.logger import get_logger

log = get_logger(__name__)


class DexScreener:
    def __init__(self) -> None:
        self.base = settings.dexscreener_base.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        # ~5 req/s is comfortable; bursts up to 10.
        self._rl = RateLimiter(rate=5.0, capacity=10.0)

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
    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        await self._rl.acquire()
        session = await self._ensure()
        url = f"{self.base}{path}"
        async with session.get(url, params=params) as resp:
            if resp.status == 429:
                log.debug("DexScreener 429, backing off.")
                await asyncio.sleep(1.0)
                return None
            if resp.status != 200:
                return None
            return await resp.json()

    # ------------------------------------------------------------------

    @staticmethod
    def _parse_pair(p: Dict[str, Any]) -> Optional[TokenSnapshot]:
        try:
            base = p.get("baseToken", {})
            price_native = float(p.get("priceNative") or 0)
            price_usd = float(p.get("priceUsd") or 0)
            liq = p.get("liquidity", {}) or {}
            vol = p.get("volume", {}) or {}
            change = p.get("priceChange", {}) or {}
            txns = p.get("txns", {}) or {}
            t5 = txns.get("m5", {}) or {}
            return TokenSnapshot(
                mint=base.get("address", ""),
                symbol=base.get("symbol", ""),
                name=base.get("name", ""),
                dex=str(p.get("dexId", "")),
                chain=str(p.get("chainId", "solana")).lower(),
                pair_address=p.get("pairAddress", ""),
                price_usd=price_usd,
                price_sol=price_native,
                liquidity_usd=float(liq.get("usd") or 0),
                volume_24h_usd=float(vol.get("h24") or 0),
                volume_5m_usd=float(vol.get("m5") or 0),
                market_cap=float(p.get("fdv") or 0),
                price_change_5m=float(change.get("m5") or 0),
                price_change_1h=float(change.get("h1") or 0),
                price_change_24h=float(change.get("h24") or 0),
                buys_5m=int(t5.get("buys") or 0),
                sells_5m=int(t5.get("sells") or 0),
                txns_5m=int((t5.get("buys") or 0) + (t5.get("sells") or 0)),
                created_at_ms=int(p.get("pairCreatedAt") or 0),
                raw=p,
            )
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _enabled() -> set[str]:
        """Lowercased set of enabled chain IDs from settings."""
        return {c.strip().lower() for c in settings.enabled_chains if c.strip()}

    async def get_token(self, mint: str) -> Optional[TokenSnapshot]:
        data = await self._get(f"/latest/dex/tokens/{mint}")
        if not data:
            return None
        pairs = data.get("pairs") or []
        enabled = self._enabled()
        matching = [p for p in pairs if str(p.get("chainId", "")).lower() in enabled]
        if not matching:
            return None
        # Choose the deepest-liquidity pair within enabled chains.
        matching.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
        return self._parse_pair(matching[0])

    async def search(self, query: str) -> List[TokenSnapshot]:
        data = await self._get("/latest/dex/search", params={"q": query})
        if not data:
            return []
        enabled = self._enabled()
        out: List[TokenSnapshot] = []
        for p in data.get("pairs") or []:
            if str(p.get("chainId", "")).lower() not in enabled:
                continue
            snap = self._parse_pair(p)
            if snap:
                out.append(snap)
        return out

    async def trending(self) -> List[TokenSnapshot]:
        """Return tokens currently boosted on DexScreener across enabled chains."""
        data = await self._get("/token-boosts/latest/v1")
        if not data:
            return []
        results: List[TokenSnapshot] = []
        items = data if isinstance(data, list) else data.get("items", []) or []
        enabled = self._enabled()
        sem = asyncio.Semaphore(5)

        async def _resolve(addr: str) -> None:
            async with sem:
                snap = await self.get_token(addr)
                if snap:
                    results.append(snap)

        tasks = []
        for it in items:
            if str(it.get("chainId", "")).lower() not in enabled:
                continue
            addr = it.get("tokenAddress")
            if addr:
                tasks.append(asyncio.create_task(_resolve(addr)))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return results


dexscreener = DexScreener()
