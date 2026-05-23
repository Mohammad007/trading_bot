"""
Tabular Q-learning agent for entry decisions.

State = discretized bucket of (AI score, buy pressure, momentum, smart-money).
Actions = {HOLD, BUY_SMALL, BUY_BIG, SKIP}.

The agent is persisted to JSON. Every closed trade calls `update(...)`
with the realized reward (PnL %), so the policy improves over time.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

from config import settings
from utils.logger import get_logger

log = get_logger(__name__)

ACTIONS: Tuple[str, ...] = ("SKIP", "HOLD", "BUY_SMALL", "BUY_BIG")


def _bucket(x: float, edges: List[float]) -> int:
    for i, e in enumerate(edges):
        if x < e:
            return i
    return len(edges)


def discretize(ai_score: float, buy_pressure: float, mom5: float, smart: float) -> str:
    """Encode state as a short string key for Q-table."""
    a = _bucket(ai_score, [0.4, 0.6, 0.75])         # 0..3
    b = _bucket(buy_pressure, [-0.2, 0.0, 0.3])     # 0..3
    m = _bucket(mom5, [-0.05, 0.0, 0.05, 0.15])     # 0..4
    s = _bucket(smart, [0.0, 0.3, 0.6])             # 0..3
    return f"{a}{b}{m}{s}"


@dataclass
class QAgent:
    alpha: float = 0.15                 # learning rate
    gamma: float = 0.85                 # discount
    epsilon: float = 0.10               # exploration
    q: Dict[str, Dict[str, float]] = None  # state -> action -> value

    def __post_init__(self) -> None:
        if self.q is None:
            self.q = {}
        self.path = Path(settings.models_dir) / "qtable.json"
        self.load()

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            self.q = json.loads(self.path.read_text())
            log.info("Loaded Q-table (%d states)", len(self.q))
        except Exception as exc:
            log.warning("Failed to load Q-table: %s", exc)
            self.q = {}

    def save(self) -> None:
        try:
            self.path.write_text(json.dumps(self.q))
        except Exception as exc:
            log.warning("Failed to save Q-table: %s", exc)

    # -- decision logic ----------------------------------------------------

    def _row(self, state: str) -> Dict[str, float]:
        if state not in self.q:
            self.q[state] = {a: 0.0 for a in ACTIONS}
        return self.q[state]

    def choose(self, state: str) -> str:
        row = self._row(state)
        if random.random() < self.epsilon:
            return random.choice(ACTIONS)
        # Argmax with tie-breaking favoring conservative actions.
        best = max(ACTIONS, key=lambda a: (row[a], -ACTIONS.index(a)))
        return best

    def update(self, state: str, action: str, reward: float, next_state: str) -> None:
        if action not in ACTIONS:
            return
        row = self._row(state)
        next_row = self._row(next_state)
        target = reward + self.gamma * max(next_row.values())
        row[action] += self.alpha * (target - row[action])
        self.save()


agent = QAgent()


def adjust_buy_amount(base_sol: float, action: str) -> float:
    """Map a chosen action to a concrete buy amount in SOL."""
    if action == "BUY_BIG":
        return base_sol * 1.5
    if action == "BUY_SMALL":
        return base_sol * 0.6
    return base_sol
