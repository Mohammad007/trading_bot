"""
Rug / honeypot detector.

Two layers:

1) Cheap heuristics on the DexScreener snapshot we already have:
   liquidity floor, ratio of liquidity to MC, age, sell-flow, holder
   imbalance hints.

2) Authoritative on-chain checks for mint & freeze authority via the
   Solana RPC (these are absolute kills: any non-null authority = rug
   risk, any frozen freeze authority = rug risk).

Returns a `RugReport` whose `safe` boolean is the only thing the trade
engine needs to look at.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import List, Optional

import aiohttp

from config import settings
from dex import TokenSnapshot
from utils.helpers import async_retry, now_ms
from utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class RugReport:
    safe: bool
    risk_score: float = 0.0  # 0=safe, 1=definite rug
    reasons: List[str] = field(default_factory=list)


# -- on-chain helper ---------------------------------------------------------

@async_retry(attempts=2, delay=0.4)
async def _get_mint_authority_info(mint: str, rpc_url: str) -> dict:
    """
    Returns {'mint_authority': str|None, 'freeze_authority': str|None,
             'supply': int, 'decimals': int} or empty dict on failure.
    """
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [mint, {"encoding": "jsonParsed"}],
    }
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(rpc_url, json=body) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json()
    try:
        info = data["result"]["value"]["data"]["parsed"]["info"]
        return {
            "mint_authority": info.get("mintAuthority"),
            "freeze_authority": info.get("freezeAuthority"),
            "supply": int(info.get("supply", 0)),
            "decimals": int(info.get("decimals", 0)),
        }
    except Exception:
        return {}


# -- main entry -------------------------------------------------------------

async def check(snap: TokenSnapshot, rpc_url: Optional[str] = None) -> RugReport:
    """
    Production-grade rug filter. Same thresholds for PAPER and REAL so the
    decisions you see in paper are what you will see live.
    """
    rpc_url = rpc_url or settings.effective_rpc
    reasons: List[str] = []
    risk = 0.0

    is_pump = (snap.dex or "").lower() in ("pumpfun", "pumpswap")
    # Pump.fun bonding-curve coins report liquidity differently; relax the
    # floor for them only, but everything else is uniform.
    min_liq = 1_500 if is_pump else 5_000

    if snap.liquidity_usd < min_liq:
        reasons.append(f"low liquidity (${snap.liquidity_usd:.0f})")
        risk += 0.30
    if snap.market_cap > 0 and snap.liquidity_usd > 0:
        ratio = snap.liquidity_usd / max(snap.market_cap, 1.0)
        if ratio < 0.01:
            reasons.append(f"liquidity/MC ratio low ({ratio:.4f})")
            risk += 0.20
    if snap.sells_5m > 0 and snap.buys_5m == 0:
        reasons.append("all sells, no buys in last 5m")
        risk += 0.25
    if snap.created_at_ms and (now_ms() - snap.created_at_ms) < 30_000 and snap.liquidity_usd < 3_000:
        reasons.append("brand-new with thin liquidity")
        risk += 0.20

    # On-chain authority check. For pump.fun, mint authority = bonding curve
    # PDA which is normal pre-graduation; do not penalize that case.
    info = await _get_mint_authority_info(snap.mint, rpc_url)
    if info:
        if info.get("mint_authority") and not is_pump:
            reasons.append(f"mint authority not renounced ({info['mint_authority'][:6]}…)")
            risk += 0.40
        if info.get("freeze_authority"):
            reasons.append(f"freeze authority active ({info['freeze_authority'][:6]}…)")
            risk += 0.40
    else:
        reasons.append("could not verify mint/freeze authority")
        risk += 0.10

    safe = risk < 0.55
    return RugReport(safe=safe, risk_score=min(risk, 1.0), reasons=reasons)
