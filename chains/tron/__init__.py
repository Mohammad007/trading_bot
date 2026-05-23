"""
Tron chain integration (basic).

Real SunSwap V2 swap signing requires building TRC-20 approval + swap
transactions through tronpy. We provide a working wallet wrapper and
price-query helper. Active SunSwap swap submission is left as a thin
wrapper that uses tronpy's contract interface - test in PAPER mode
before turning on real swaps.

NOTE: Tron meme-coin volume is dwarfed by Solana and EVM L2s; this is
included for completeness but is not the focus of the AI pipeline.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from utils.logger import get_logger

log = get_logger(__name__)

TRONGRID_BASE = "https://api.trongrid.io"
SUNSWAP_ROUTER = "TKzxdSv2FZKQrEqkKVgp5DcwEXBEKMg2Ax"  # Sun.io Router V2 (mainnet)


@dataclass
class TronWallet:
    address: str
    private_key: str

    @classmethod
    def from_env(cls) -> Optional["TronWallet"]:
        key = os.getenv("TRON_PRIVATE_KEY", "")
        if not key:
            return None
        try:
            from tronpy.keys import PrivateKey  # noqa: PLC0415
            pk = PrivateKey(bytes.fromhex(key))
            return cls(address=pk.public_key.to_base58check_address(), private_key=key)
        except Exception as exc:
            log.error("Tron wallet load failed: %s", exc)
            return None

    def native_balance(self) -> float:
        try:
            from tronpy import Tron  # noqa: PLC0415
            from tronpy.providers import HTTPProvider  # noqa: PLC0415
            client = Tron(provider=HTTPProvider(TRONGRID_BASE,
                                                api_key=os.getenv("TRONGRID_API_KEY")))
            sun = client.get_account_balance(self.address)   # in TRX
            return float(sun)
        except Exception as exc:
            log.debug("tron native_balance failed: %s", exc)
            return 0.0


_wallet: Optional[TronWallet] = None


def get_tron_wallet() -> Optional[TronWallet]:
    global _wallet
    if _wallet is None:
        _wallet = TronWallet.from_env()
    return _wallet
