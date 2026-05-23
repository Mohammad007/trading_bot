"""
Smart entry decision v2.

Now blends:
  - XGBoost pump probability (or heuristic fallback)
  - LSTM trend (or statistical fallback)
  - Chart AI (S/R, BOS, FVG, sweeps, exhaustion)
  - Orderflow AI (buy/sell aggression, whales, seller exhaustion)
  - Smart-money score (wallets we know are profitable)
  - Correlation AI (ecosystem heat)
  - Reinforcement-learning action

Outputs a SmartEntryDecision with action, blended confidence, ATR-based
position sizing plan, and individual signal scores for logging.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from ai import FeatureVector
from ai.chart_ai import ChartSignal, evaluate as chart_evaluate
from ai.correlation_ai import correlation_ai
from ai.lstm_model import lstm_model
from ai.orderflow_ai import OrderFlowSignal, evaluate as orderflow_evaluate
from ai.position_sizing import SizingPlan, size as plan_size
from ai.reinforcement import adjust_buy_amount, agent, discretize
from ai.smart_money import smart_money
from ai.xgb_model import xgb_model
from config import settings
from database.db import db
from dex import TokenSnapshot
from market.candles import candles
from market.orderflow import orderflow
from trading.paper_wallet import paper_wallet
from utils.helpers import clamp, now_ms
from utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class SmartEntryDecision:
    should_buy: bool
    confidence: float
    suggested_sol: float
    sizing: Optional[SizingPlan] = None
    rl_action: str = "HOLD"
    xgb_score: float = 0.0
    lstm_score: float = 0.0
    chart_score: float = 0.5
    orderflow_score: float = 0.0
    smart_money_score: float = 0.0
    ecosystem_heat: float = 0.0
    state_key: str = ""
    reasons: List[str] = field(default_factory=list)
    chart_signal: Optional[ChartSignal] = None
    orderflow_signal: Optional[OrderFlowSignal] = None


def _features(snap: TokenSnapshot, smart_money_score: float, whale_buys: int) -> FeatureVector:
    age_min = 0.0
    if snap.created_at_ms:
        age_min = max(0.0, (now_ms() - snap.created_at_ms) / 60_000.0)
    return FeatureVector(
        liquidity_usd=snap.liquidity_usd,
        volume_5m_usd=snap.volume_5m_usd,
        volume_24h_usd=snap.volume_24h_usd,
        market_cap=snap.market_cap,
        price_change_5m=snap.price_change_5m / 100.0,
        price_change_1h=snap.price_change_1h / 100.0,
        price_change_24h=snap.price_change_24h / 100.0,
        buys_5m=snap.buys_5m,
        sells_5m=snap.sells_5m,
        txns_5m=snap.txns_5m,
        buy_pressure=snap.buy_pressure,
        age_minutes=age_min,
        whale_buys_5m=whale_buys,
        smart_money_score=smart_money_score,
    )


def _ingest_into_caches(snap: TokenSnapshot) -> None:
    """Push the DexScreener snapshot tick into the candle + orderflow caches."""
    if snap.price_sol > 0:
        # Use price_sol as the "close"; volume_5m is per 5m so we approximate.
        candles.add_tick(
            token=snap.mint,
            price=snap.price_sol,
            vol_quote=snap.volume_5m_usd,
            ts_ms=now_ms(),
        )
    # Synthesize per-print events from m5 buys/sells. Average size = vol/n.
    n = snap.buys_5m + snap.sells_5m
    if n > 0 and snap.volume_5m_usd > 0:
        avg = snap.volume_5m_usd / n
        # Replay 1 print per side to avoid blowing the cache.
        if snap.buys_5m:
            orderflow.add_print(
                snap.mint, ts_ms=now_ms(), price=snap.price_usd,
                size_usd=avg, is_buy=True,
            )
        if snap.sells_5m:
            orderflow.add_print(
                snap.mint, ts_ms=now_ms(), price=snap.price_usd,
                size_usd=avg, is_buy=False,
            )


def decide(
    snap: TokenSnapshot,
    smart_money_addresses: Optional[List[str]] = None,
    whale_buys_5m: int = 0,
    candle_history: Optional[Sequence[dict]] = None,
) -> SmartEntryDecision:
    _ingest_into_caches(snap)

    smart_money_addresses = smart_money_addresses or []
    sm_score = smart_money.score(smart_money_addresses) if smart_money_addresses else 0.0

    # --- legacy signals (XGB + LSTM) ---------------------------------------
    f = _features(snap, sm_score, whale_buys_5m)
    xgb_score = xgb_model.predict(f)
    lstm_score = lstm_model.predict(candle_history or [])

    # --- new signals -------------------------------------------------------
    chart_candles = candles.candles(snap.mint, tf="1m", n=64)
    chart_sig = chart_evaluate(chart_candles)
    chart_score = chart_sig.bullish

    of_snap = orderflow.snapshot(snap.mint, now_ms())
    of_sig = orderflow_evaluate(of_snap)
    of_score = clamp((of_sig.conviction + 1.0) / 2.0, 0.0, 1.0)   # remap to 0..1

    heat = correlation_ai.ecosystem_heat()

    # --- blended confidence ------------------------------------------------
    # If we have no chart history (fresh token), don't let neutral chart=0.5
    # drag the score down - lean on XGB heuristics + DexScreener tape.
    has_chart = len(chart_candles) >= 8
    if has_chart:
        blended = (
            0.30 * chart_score
            + 0.25 * xgb_score
            + 0.15 * of_score
            + 0.10 * lstm_score
            + 0.10 * sm_score
            + 0.10 * max(heat, 0.0)
        )
    else:
        # Fresh-token regime: heavier on XGB pump probability + flow.
        blended = (
            0.55 * xgb_score
            + 0.20 * of_score
            + 0.15 * sm_score
            + 0.10 * max(heat, 0.0)
        )
    confidence = clamp(blended, 0.0, 1.0)

    # --- RL ---------------------------------------------------------------
    state_key = discretize(xgb_score, snap.buy_pressure, snap.price_change_5m / 100.0, sm_score)
    rl_action = agent.choose(state_key)

    # --- gating ------------------------------------------------------------
    reasons: List[str] = [f"chart={chart_score:.2f}", f"of={of_score:+.2f}"]
    if chart_sig.notes:
        reasons.extend(chart_sig.notes[:2])
    if of_sig.notes:
        reasons.extend(of_sig.notes[:2])

    # --- QUALITY GATES (rug protection without strangling activity) --------
    age_seconds = 0
    if snap.created_at_ms:
        age_seconds = max(0, (now_ms() - snap.created_at_ms) // 1000)

    quality_block: Optional[str] = None
    # 1. Age: skip ultra-fresh (under 15s) - first 15s is rug zone.
    if 0 < age_seconds < 15:
        quality_block = f"too_young ({age_seconds}s)"
    # 2. Activity: need at least *some* real demand (2+ txns).
    elif snap.txns_5m < 2:
        quality_block = f"too_few_txns ({snap.txns_5m})"
    # 3. Liquidity floor - $1500 catches the worst rugs, allows fresh launches.
    elif snap.liquidity_usd < 1_500:
        quality_block = f"liquidity ${snap.liquidity_usd:.0f} < $1500"
    # 4. Strong selling pressure (>2x sells over buys) = let it stabilize first.
    elif snap.sells_5m > snap.buys_5m * 2 and snap.txns_5m >= 5:
        quality_block = f"heavy_selling ({snap.buys_5m}B/{snap.sells_5m}S)"

    should_buy = False
    if quality_block is not None:
        reasons.append(quality_block)
    elif rl_action == "SKIP":
        reasons.append("rl=SKIP")
    elif confidence < settings.ai_buy_threshold:
        reasons.append(f"conf {confidence:.2f} < buy_thr {settings.ai_buy_threshold:.2f}")
    elif chart_sig.near_resistance and not chart_sig.patterns.structure.bos_up:
        reasons.append("at_resistance_no_bos")
    elif of_sig.conviction < -0.4:
        reasons.append(f"selling pressure ({of_sig.conviction:+.2f})")
    else:
        # No extra buffer - if confidence >= ai_buy_threshold, buy.
        # RL "SKIP" already short-circuits above, so HOLD here means "let
        # the threshold decide".
        should_buy = (
            rl_action in ("BUY_SMALL", "BUY_BIG")
            or confidence >= settings.ai_buy_threshold
        )
        if should_buy:
            reasons.append(f"BUY conf={confidence:.2f} rl={rl_action}")

    # --- sizing ------------------------------------------------------------
    bankroll = paper_wallet.balance_sol() if not settings.is_real else 1.0  # real-mode bankroll injected elsewhere
    sizing = plan_size(
        bankroll_sol=max(bankroll, 0.001),
        confidence=confidence,
        price=snap.price_sol if snap.price_sol > 0 else snap.price_usd / 150.0,
        atr_value=chart_sig.atr_value,
        base_sol=settings.default_buy_amount_sol,
    )
    if not should_buy:
        # Even if not buying, scale via RL action.
        sizing.sol_amount = adjust_buy_amount(settings.default_buy_amount_sol, "HOLD")

    # --- persist AI scores -------------------------------------------------
    try:
        db.log_ai_score(int(time.time() * 1000), snap.mint, "xgb", xgb_score)
        db.log_ai_score(int(time.time() * 1000), snap.mint, "lstm", lstm_score)
        db.log_ai_score(int(time.time() * 1000), snap.mint, "chart", chart_score)
        db.log_ai_score(int(time.time() * 1000), snap.mint, "blended", confidence)
    except Exception:
        pass

    return SmartEntryDecision(
        should_buy=should_buy,
        confidence=confidence,
        suggested_sol=sizing.sol_amount,
        sizing=sizing,
        rl_action=rl_action,
        xgb_score=xgb_score,
        lstm_score=lstm_score,
        chart_score=chart_score,
        orderflow_score=of_score,
        smart_money_score=sm_score,
        ecosystem_heat=heat,
        state_key=state_key,
        reasons=reasons,
        chart_signal=chart_sig,
        orderflow_signal=of_sig,
    )
