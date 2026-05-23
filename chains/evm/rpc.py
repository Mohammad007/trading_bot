"""
EVM RPC client wrapper around web3.py.

Holds a Web3 instance per chain. Endpoint chosen from the RPC pool so
failover happens automatically.
"""
from __future__ import annotations

from typing import Dict, Optional

from sniper.rpc_failover import pool
from utils.logger import get_logger

log = get_logger(__name__)


def _import_web3():
    from web3 import Web3                              # noqa: PLC0415
    from web3.middleware import ExtraDataToPOAMiddleware  # noqa: PLC0415
    return Web3, ExtraDataToPOAMiddleware


_w3_cache: Dict[str, object] = {}


def get_w3(chain: str):
    """
    Returns a Web3 instance for the given chain string ('ethereum',
    'bsc', 'polygon', ...). Refreshes endpoint each call via the RPC
    pool's best_url(), so health switching is automatic.
    """
    Web3, POA = _import_web3()
    rpc_url = pool(chain).best_url()
    if not rpc_url:
        return None
    cache_key = f"{chain}|{rpc_url}"
    cached = _w3_cache.get(cache_key)
    if cached is not None:
        return cached
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 8}))
    # POA chains (BSC, Polygon, Avalanche subnet) need this middleware.
    if chain in ("bsc", "polygon", "avalanche"):
        try:
            w3.middleware_onion.inject(POA, layer=0)
        except Exception:
            pass
    _w3_cache[cache_key] = w3
    return w3


def block_number(chain: str) -> Optional[int]:
    w3 = get_w3(chain)
    if w3 is None:
        return None
    try:
        return int(w3.eth.block_number)
    except Exception as exc:
        log.debug("block_number(%s) failed: %s", chain, exc)
        return None
