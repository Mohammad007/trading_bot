"""
Pump.fun client.

Uses the public frontend API:
  GET /coins/latest                       (newest tokens)
  GET /coins?offset=0&limit=50&sort=...   (browse)
  GET /coins/{mint}

We also expose a websocket feed for live new-coin notifications, which
the sniper engine consumes.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict, List, Optional

import aiohttp

from config import settings
from dex import TokenSnapshot
from utils.helpers import RateLimiter, async_retry
from utils.logger import get_logger

log = get_logger(__name__)

PUMP_WS = "wss://pumpportal.fun/api/data"


class PumpFun:
    def __init__(self) -> None:
        self.base = settings.pumpfun_base.rstrip("/")
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
    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        await self._rl.acquire()
        session = await self._ensure()
        async with session.get(f"{self.base}{path}", params=params) as resp:
            if resp.status != 200:
                return None
            return await resp.json()

    @staticmethod
    def _parse(coin: Dict[str, Any]) -> TokenSnapshot:
        mint = coin.get("mint") or coin.get("address") or ""
        price_usd = float(coin.get("usd_market_cap") or 0) / max(float(coin.get("total_supply") or 1), 1.0)
        return TokenSnapshot(
            mint=mint,
            symbol=coin.get("symbol", ""),
            name=coin.get("name", ""),
            dex="pumpfun",
            pair_address=coin.get("bonding_curve") or "",
            price_usd=price_usd,
            price_sol=float(coin.get("virtual_sol_reserves") or 0)
                      / max(float(coin.get("virtual_token_reserves") or 1), 1.0),
            liquidity_usd=float(coin.get("usd_market_cap") or 0) * 0.5,
            volume_24h_usd=float(coin.get("volume_24h") or 0),
            market_cap=float(coin.get("usd_market_cap") or 0),
            created_at_ms=int(coin.get("created_timestamp") or 0),
            raw=coin,
        )

    async def latest(self, limit: int = 50) -> List[TokenSnapshot]:
        data = await self._get("/coins/latest", params={"limit": limit})
        if not isinstance(data, list):
            return []
        return [self._parse(c) for c in data]

    async def get_coin(self, mint: str) -> Optional[TokenSnapshot]:
        data = await self._get(f"/coins/{mint}")
        if not isinstance(data, dict) or not data:
            return None
        return self._parse(data)

    # ------------------------------------------------------------------
    # WebSocket stream of *new* tokens via pumpportal.fun (free).
    # ------------------------------------------------------------------

    async def stream_new_tokens(self) -> AsyncIterator[Dict[str, Any]]:
        """
        Async generator yielding new-token events. Reconnects automatically.
        """
        import websockets  # local import to keep top-level light
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(PUMP_WS, ping_interval=20) as ws:
                    log.info("pump.fun WS connected.")
                    backoff = 1.0
                    await ws.send('{"method":"subscribeNewToken"}')
                    async for raw in ws:
                        try:
                            import orjson
                            msg = orjson.loads(raw)
                        except Exception:
                            continue
                        if isinstance(msg, dict) and (msg.get("mint") or msg.get("txType") == "create"):
                            yield msg
            except Exception as exc:
                log.warning("pump.fun WS error: %s (retry in %.1fs)", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)


pumpfun = PumpFun()
