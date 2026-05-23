"""AI prediction engine."""

from dataclasses import dataclass, field
from typing import Dict


@dataclass
class FeatureVector:
    """The canonical feature set used by every model.

    Keep this list stable - models are trained on it.
    """

    liquidity_usd: float = 0.0
    volume_5m_usd: float = 0.0
    volume_24h_usd: float = 0.0
    market_cap: float = 0.0
    price_change_5m: float = 0.0
    price_change_1h: float = 0.0
    price_change_24h: float = 0.0
    buys_5m: int = 0
    sells_5m: int = 0
    txns_5m: int = 0
    buy_pressure: float = 0.0      # (buys-sells)/total
    age_minutes: float = 0.0
    whale_buys_5m: int = 0
    smart_money_score: float = 0.0

    def as_dict(self) -> Dict[str, float]:
        return {
            "liquidity_usd": self.liquidity_usd,
            "volume_5m_usd": self.volume_5m_usd,
            "volume_24h_usd": self.volume_24h_usd,
            "market_cap": self.market_cap,
            "price_change_5m": self.price_change_5m,
            "price_change_1h": self.price_change_1h,
            "price_change_24h": self.price_change_24h,
            "buys_5m": float(self.buys_5m),
            "sells_5m": float(self.sells_5m),
            "txns_5m": float(self.txns_5m),
            "buy_pressure": self.buy_pressure,
            "age_minutes": self.age_minutes,
            "whale_buys_5m": float(self.whale_buys_5m),
            "smart_money_score": self.smart_money_score,
        }


FEATURE_ORDER = list(FeatureVector().as_dict().keys())
