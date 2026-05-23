"""
XGBoost pump-probability model.

Predicts: P(token will rise >= +20% within next 30 minutes)

The model is loaded from `ai/saved_models/xgb_pump.json` if present.
If absent, we ship a deterministic heuristic fallback so the bot still
works out of the box and you can train later from the trade history
collected in paper mode.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

from ai import FEATURE_ORDER, FeatureVector
from config import settings
from utils.helpers import clamp
from utils.logger import get_logger

log = get_logger(__name__)


class XGBPumpModel:
    def __init__(self) -> None:
        self.model = None
        self.model_path = Path(settings.models_dir) / "xgb_pump.json"
        self._try_load()

    def _try_load(self) -> None:
        if not self.model_path.exists():
            log.info("XGB model not found at %s - using heuristic fallback.", self.model_path)
            return
        try:
            import xgboost as xgb  # noqa: PLC0415
            booster = xgb.Booster()
            booster.load_model(str(self.model_path))
            self.model = booster
            log.info("Loaded XGB pump model from %s", self.model_path)
        except Exception as exc:
            log.warning("Failed to load XGB model: %s (using heuristic).", exc)

    # ------------------------------------------------------------------

    def predict(self, features: FeatureVector) -> float:
        """Returns probability in [0, 1]."""
        if self.model is not None:
            try:
                import xgboost as xgb  # noqa: PLC0415
                vec = np.array(
                    [list(features.as_dict().values())],
                    dtype=np.float32,
                )
                dmat = xgb.DMatrix(vec, feature_names=FEATURE_ORDER)
                p = float(self.model.predict(dmat)[0])
                return clamp(p, 0.0, 1.0)
            except Exception as exc:
                log.warning("XGB predict failed (%s); using heuristic.", exc)
        return self._heuristic(features)

    @staticmethod
    def _heuristic(f: FeatureVector) -> float:
        """
        Deterministic fallback combining liquidity, momentum, buy pressure,
        whale activity, and age. Calibrated to land between 0.05 and 0.95.
        """
        score = 0.0
        if f.liquidity_usd > 0:
            score += clamp(np.log10(f.liquidity_usd + 1) / 6.0, 0, 0.20)
        if f.volume_5m_usd > 0:
            score += clamp(np.log10(f.volume_5m_usd + 1) / 5.0, 0, 0.15)
        score += clamp(f.buy_pressure * 0.20, -0.10, 0.20)
        score += clamp(f.price_change_5m * 0.30, -0.15, 0.20)
        score += clamp(f.price_change_1h * 0.10, -0.05, 0.10)
        score += clamp(f.whale_buys_5m * 0.05, 0, 0.15)
        score += clamp(f.smart_money_score * 0.20, 0, 0.20)
        # Penalize old & quiet tokens.
        if f.age_minutes > 0:
            score -= clamp((f.age_minutes - 240) / 4800.0, 0, 0.10)
        if f.txns_5m < 5:
            score -= 0.10
        return clamp(0.5 + score, 0.05, 0.95)


xgb_model = XGBPumpModel()
