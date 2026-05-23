"""
Correlation AI - detect ecosystem rotations.

Maintains a short rolling window of price-change deltas per token, then
computes pairwise Pearson correlations. If "majors" (BONK, WIF, popcat,
SOL, etc.) start rallying in unison, we flag the ecosystem as hot - a
signal that boosts the AI buy threshold sensitivity downstream.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional, Sequence

import numpy as np

from utils.logger import get_logger

log = get_logger(__name__)

DEFAULT_MAJORS: List[str] = [
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",   # BONK
    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",   # WIF (dogwifhat)
    "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",   # POPCAT
]


class CorrelationAI:
    def __init__(self, window: int = 30) -> None:
        self.window = window
        self.series: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=self.window))

    def update(self, mint: str, pct_change_5m: float) -> None:
        self.series[mint].append(float(pct_change_5m))

    def ecosystem_heat(self, majors: Optional[Sequence[str]] = None) -> float:
        """Average pairwise correlation between majors in [-1, 1].

        Returns 0.0 if too few samples.
        """
        mints = list(majors or DEFAULT_MAJORS)
        vectors = [list(self.series[m]) for m in mints if len(self.series[m]) >= 5]
        if len(vectors) < 2:
            return 0.0
        min_len = min(len(v) for v in vectors)
        mat = np.array([v[-min_len:] for v in vectors], dtype=np.float64)
        if mat.shape[1] < 3:
            return 0.0
        try:
            corr = np.corrcoef(mat)
        except Exception:
            return 0.0
        n = corr.shape[0]
        if n < 2:
            return 0.0
        iu = np.triu_indices(n, k=1)
        vals = corr[iu]
        vals = vals[~np.isnan(vals)]
        if vals.size == 0:
            return 0.0
        return float(np.clip(vals.mean(), -1.0, 1.0))


correlation_ai = CorrelationAI()
