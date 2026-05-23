"""
EVM new-pair sniper.

Subscribes to Uniswap V2-style PairCreated events via eth_subscribe.
When a new pair is created, we resolve the non-native token, query
liquidity, hand it to rugcheck + smart_entry, and route the buy through
auto_buy (paper or real).
"""
from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable, Dict, List, Optional

import orjson
import websockets

from chains import EVM_CHAINS, Chain
from chains.evm.rpc import get_w3
from utils.helpers import async_retry
from utils.logger import get_logger

log = get_logger(__name__)

PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
# keccak("PairCreated(address,address,address,uint256)")


def _ws_url_for(chain: Chain) -> Optional[str]:
    """Get a websocket-capable RPC URL for the chain."""
    # We expect HELIUS_API_KEY-style premium URLs in env per chain.
    import os
    return (
        os.getenv(f"{chain.name}_WS_URL")
        or os.getenv("EVM_WS_URL")
        or None
    )


class EVMSniper:
    """One instance per chain. Run in background with `run()`."""

    def __init__(
        self,
        chain: Chain,
        on_pair_created: Callable[[Chain, str, str, str], Awaitable[None]],
    ) -> None:
        """
        `on_pair_created(chain, pair_address, token0, token1)` is invoked
        for every new V2 pair on the chain.
        """
        self.chain = chain
        self.on_pair_created = on_pair_created
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        ws_url = _ws_url_for(self.chain)
        if not ws_url:
            log.info("EVM sniper [%s] idle (no WS URL configured).", self.chain.value)
            return

        spec = EVM_CHAINS.get(self.chain)
        if spec is None:
            return

        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(ws_url, ping_interval=30) as ws:
                    log.info("EVM sniper [%s] WS connected.", self.chain.value)
                    backoff = 1.0
                    sub = {
                        "jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
                        "params": ["logs", {
                            "address": spec.uniswap_v2_factory,
                            "topics": [PAIR_CREATED_TOPIC],
                        }],
                    }
                    await ws.send(orjson.dumps(sub).decode())
                    async for raw in ws:
                        if self._stop.is_set():
                            return
                        await self._handle_msg(raw)
            except Exception as exc:
                log.warning("EVM sniper [%s] WS error: %s (retry in %.1fs)",
                            self.chain.value, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _handle_msg(self, raw: str) -> None:
        try:
            msg = orjson.loads(raw)
        except Exception:
            return
        params = msg.get("params") or {}
        result = params.get("result") or {}
        topics = result.get("topics") or []
        if len(topics) < 3:
            return
        # PairCreated(indexed token0, indexed token1, pair, uint)
        token0 = "0x" + topics[1][-40:]
        token1 = "0x" + topics[2][-40:]
        data = result.get("data") or "0x"
        if len(data) < 66:
            return
        # First 32 bytes of data = pair address (left-padded).
        pair = "0x" + data[26:66]
        try:
            await self.on_pair_created(self.chain, pair, token0, token1)
        except Exception as exc:
            log.debug("on_pair_created handler error: %s", exc)
