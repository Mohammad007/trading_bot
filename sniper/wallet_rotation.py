"""
Wallet rotation policy.

Rotation triggers:
- N trades on the same wallet (default 5)
- Daily PnL crosses a threshold
- Manual /rotate command via Telegram

Only meaningful in REAL mode. In PAPER mode this is a no-op.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config import settings
from trading.real_wallet import get_real_wallet
from utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class WalletRotator:
    trades_per_rotation: int = 5
    _counter: int = 0

    async def on_trade(self) -> None:
        if not settings.is_real:
            return
        self._counter += 1
        if self._counter < self.trades_per_rotation:
            return
        wallet = await get_real_wallet()
        if wallet is None or not wallet.rotations:
            return
        wallet.rotate()
        self._counter = 0
        log.info("Wallet rotated; new primary=%s", wallet.pubkey_str[:10])

    async def force_rotate(self) -> Optional[str]:
        wallet = await get_real_wallet()
        if wallet is None or not wallet.rotations:
            return None
        wallet.rotate()
        self._counter = 0
        return wallet.pubkey_str


rotator = WalletRotator()
