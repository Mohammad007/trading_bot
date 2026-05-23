"""
MEV protection helpers.

What we actually do on a CPU laptop without Jito:

1. Use a dynamic priority fee that adapts to recent block congestion
   (sample from Helius/RPC `getRecentPrioritizationFees` if available).

2. Cap slippage tightly when book is thin to avoid sandwich pain.

3. Random small jitter on submission so we are not deterministic prey.
"""
from __future__ import annotations

import asyncio
import random
from typing import Optional

import aiohttp

from config import settings
from utils.helpers import async_retry, clamp
from utils.logger import get_logger

log = get_logger(__name__)


@async_retry(attempts=2, delay=0.5)
async def _recent_priority_fees(rpc_url: str) -> Optional[int]:
    """Return p75 of recent prioritization fees in micro-lamports."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "getRecentPrioritizationFees", "params": []}
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(rpc_url, json=body) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    fees = data.get("result", [])
    if not fees:
        return None
    vals = sorted(int(f.get("prioritizationFee", 0)) for f in fees)
    p75 = vals[int(len(vals) * 0.75)] if vals else 0
    return p75


async def suggest_priority_fee() -> int:
    """Adapt the configured priority fee to current network state."""
    base = settings.priority_fee_microlamports
    try:
        p75 = await _recent_priority_fees(settings.effective_rpc)
        if p75 is None:
            return base
        # Pay 1.25x the 75th percentile, bounded.
        suggested = int(clamp(p75 * 1.25, base * 0.5, base * 5))
        return suggested
    except Exception as exc:
        log.debug("priority-fee suggest failed: %s", exc)
        return base


def adjust_slippage(base_bps: int, liquidity_usd: float) -> int:
    """Tighten slippage on thin pools, loosen on deep ones."""
    if liquidity_usd < 5_000:
        return min(base_bps + 200, 1500)
    if liquidity_usd < 25_000:
        return base_bps + 100
    if liquidity_usd > 250_000:
        return max(base_bps - 100, 50)
    return base_bps


async def jitter() -> None:
    """Random 50-250 ms delay before a submission."""
    await asyncio.sleep(random.uniform(0.05, 0.25))
