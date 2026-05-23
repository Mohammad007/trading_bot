"""
Multi-RPC manager with health monitoring and automatic failover.

Each chain registers a list of endpoint URLs. A background task pings
every endpoint every 30s with `getLatestBlockhash` (Solana) or
`eth_blockNumber` (EVM). We keep latency stats and route every call to
the lowest-latency healthy endpoint. Failed endpoints are quarantined
for 60s.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import aiohttp

from utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class RPCEndpoint:
    url: str
    label: str = ""
    chain: str = ""
    latency_ms: float = 9999.0
    healthy: bool = True
    consecutive_failures: int = 0
    quarantine_until_ts: float = 0.0

    def is_available(self) -> bool:
        return self.healthy and time.time() >= self.quarantine_until_ts


class RPCPool:
    """Chain-scoped RPC pool. Use `best_url()` to pick an endpoint."""

    def __init__(self, chain: str) -> None:
        self.chain = chain
        self._endpoints: List[RPCEndpoint] = []
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    def add(self, url: str, label: str = "") -> None:
        if not url:
            return
        if any(e.url == url for e in self._endpoints):
            return
        self._endpoints.append(RPCEndpoint(url=url, label=label or url[:30], chain=self.chain))

    def endpoints(self) -> List[RPCEndpoint]:
        return list(self._endpoints)

    def best_url(self) -> Optional[str]:
        available = [e for e in self._endpoints if e.is_available()]
        if not available:
            # Fall back to first one even if quarantined (better than None).
            return self._endpoints[0].url if self._endpoints else None
        available.sort(key=lambda e: e.latency_ms)
        return available[0].url

    # -- health monitor ------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._monitor_loop(), name=f"rpc-{self.chain}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _monitor_loop(self) -> None:
        log.info("RPC pool [%s] monitoring %d endpoints.", self.chain, len(self._endpoints))
        while not self._stop.is_set():
            await self._probe_all()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    async def _probe_all(self) -> None:
        tasks = [self._probe(ep) for ep in self._endpoints]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _probe(self, ep: RPCEndpoint) -> None:
        method = "eth_blockNumber" if self.chain != "solana" else "getLatestBlockhash"
        params: List = [] if self.chain != "solana" else []
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        t0 = time.perf_counter()
        try:
            timeout = aiohttp.ClientTimeout(total=4)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(ep.url, json=body) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"status={resp.status}")
                    data = await resp.json()
                    if "error" in data:
                        raise RuntimeError(str(data["error"]))
            ep.latency_ms = (time.perf_counter() - t0) * 1000.0
            ep.healthy = True
            ep.consecutive_failures = 0
        except Exception as exc:
            ep.consecutive_failures += 1
            if ep.consecutive_failures >= 3:
                was_healthy = ep.healthy
                ep.healthy = False
                ep.quarantine_until_ts = time.time() + 60
                # Only log on the *transition* into quarantine - not every probe.
                if was_healthy:
                    log.warning("RPC [%s] %s quarantined: %s", self.chain, ep.label, exc)


# Global registry: chain -> pool
_pools: Dict[str, RPCPool] = {}


def pool(chain: str) -> RPCPool:
    if chain not in _pools:
        _pools[chain] = RPCPool(chain)
    return _pools[chain]


def all_pools() -> List[RPCPool]:
    return list(_pools.values())


async def start_all() -> None:
    for p in _pools.values():
        await p.start()


async def stop_all() -> None:
    for p in _pools.values():
        await p.stop()
