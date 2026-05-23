"""
LSTM short-term trend predictor.

Tries to load a Keras model at `ai/saved_models/lstm_trend.keras`.
If absent (default), falls back to a stable rolling-stats trend score
so we don't force users to install TensorFlow up-front.

Input: a list of recent {close, volume} candles (len >= 8).
Output: probability in [0, 1] that the next candle will close higher.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence

import numpy as np

from config import settings
from utils.helpers import clamp
from utils.logger import get_logger

log = get_logger(__name__)


class LSTMTrendModel:
    def __init__(self) -> None:
        self.model = None
        self.model_path = Path(settings.models_dir) / "lstm_trend.keras"
        self._try_load()

    def _try_load(self) -> None:
        if not self.model_path.exists():
            log.info("LSTM model not found - using statistical fallback.")
            return
        try:
            from tensorflow import keras  # noqa: PLC0415
            self.model = keras.models.load_model(str(self.model_path))
            log.info("Loaded LSTM model from %s", self.model_path)
        except Exception as exc:
            log.warning("Failed to load LSTM model: %s (using fallback).", exc)

    # ------------------------------------------------------------------

    def predict(self, candles: Sequence[dict]) -> float:
        """`candles` shaped like [{'close': float, 'volume': float}, ...] newest last."""
        if len(candles) < 8:
            return 0.5
        if self.model is not None:
            try:
                import numpy as np
                closes = np.array([float(c["close"]) for c in candles[-32:]])
                vols = np.array([float(c.get("volume", 0)) for c in candles[-32:]])
                # Pad/truncate to 32 length.
                if len(closes) < 32:
                    pad = 32 - len(closes)
                    closes = np.pad(closes, (pad, 0), mode="edge")
                    vols = np.pad(vols, (pad, 0), mode="edge")
                x = np.stack([closes / max(closes.max(), 1e-9),
                              vols / max(vols.max(), 1e-9)], axis=-1)
                x = x[np.newaxis, ...].astype(np.float32)
                p = float(self.model.predict(x, verbose=0)[0][0])
                return clamp(p, 0.0, 1.0)
            except Exception as exc:
                log.warning("LSTM predict failed: %s", exc)
        return self._fallback(candles)

    @staticmethod
    def _fallback(candles: Sequence[dict]) -> float:
        closes = np.array([float(c["close"]) for c in candles], dtype=np.float64)
        if closes.size < 5:
            return 0.5
        # Short EMA over long EMA + momentum + recent volatility shrinkage.
        ema_s = _ema(closes, span=5)
        ema_l = _ema(closes, span=20 if closes.size >= 20 else closes.size)
        cross = (ema_s[-1] - ema_l[-1]) / max(abs(ema_l[-1]), 1e-9)
        mom = (closes[-1] - closes[-min(8, closes.size)]) / max(abs(closes[-min(8, closes.size)]), 1e-9)
        vol = float(np.std(np.diff(closes[-min(10, closes.size):]))) / max(abs(closes[-1]), 1e-9)
        score = 0.5 + 1.5 * cross + 0.8 * mom - 0.5 * vol
        return clamp(score, 0.05, 0.95)


def _ema(arr: np.ndarray, span: int) -> np.ndarray:
    """Numpy EMA, no pandas."""
    alpha = 2.0 / (span + 1)
    out = np.empty_like(arr, dtype=np.float64)
    out[0] = arr[0]
    for i in range(1, arr.size):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out


lstm_model = LSTMTrendModel()
