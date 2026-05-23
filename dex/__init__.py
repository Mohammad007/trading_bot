"""DEX data sources and price feeds."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TokenSnapshot:
    """Normalized token info from any DEX source."""

    mint: str
    symbol: str = ""
    name: str = ""
    dex: str = ""
    pair_address: str = ""
    price_usd: float = 0.0
    price_sol: float = 0.0
    liquidity_usd: float = 0.0
    volume_24h_usd: float = 0.0
    volume_5m_usd: float = 0.0
    market_cap: float = 0.0
    price_change_5m: float = 0.0
    price_change_1h: float = 0.0
    price_change_24h: float = 0.0
    buys_5m: int = 0
    sells_5m: int = 0
    txns_5m: int = 0
    created_at_ms: int = 0
    raw: dict = field(default_factory=dict)

    @property
    def buy_pressure(self) -> float:
        total = self.buys_5m + self.sells_5m
        if total == 0:
            return 0.0
        return (self.buys_5m - self.sells_5m) / total
