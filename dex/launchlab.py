"""
LaunchLab client (Raydium LaunchLab tokens).

There's no stable public REST for LaunchLab; in practice the cleanest
way to see fresh LaunchLab launches is to scan DexScreener for pairs
whose `dexId` starts with "raydium-launchlab".

We expose two helpers used by the sniper:

    - new_pairs()    : returns recent LaunchLab pairs as TokenSnapshot
    - get(mint)      : convenience
"""
from __future__ import annotations

from typing import List, Optional

from dex import TokenSnapshot
from dex.dexscreener import dexscreener
from utils.logger import get_logger

log = get_logger(__name__)


class LaunchLab:
    async def new_pairs(self, limit: int = 30) -> List[TokenSnapshot]:
        # DexScreener "search" against the literal string filters by name/symbol;
        # the most reliable path is the trending boosts list filtered by dexId.
        snaps = await dexscreener.trending()
        return [s for s in snaps if "launchlab" in (s.dex or "").lower()][:limit]

    async def get(self, mint: str) -> Optional[TokenSnapshot]:
        snap = await dexscreener.get_token(mint)
        if snap and "launchlab" in (snap.dex or "").lower():
            return snap
        return None


launchlab = LaunchLab()
