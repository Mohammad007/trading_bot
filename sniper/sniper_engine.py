"""
Sniper engine.

Orchestrates the feeds:

  - pump.fun WS new-token stream
  - DexScreener trending poll
  - Raydium / Meteora / LaunchLab polled for fresh pairs

When a candidate appears, we run rugcheck -> smart_entry.decide. If the
decision says BUY, we hand the candidate to the trading.auto_buy module
(which routes to paper or real wallet).
"""
from __future__ import annotations

import asyncio
import time
from typing import Dict, Optional, Set

from ai.correlation_ai import correlation_ai
from ai.smart_entry import decide
from config import settings
from database.db import db
from dex import TokenSnapshot
from dex.dexscreener import dexscreener
from dex.launchlab import launchlab
from dex.meteora import meteora
from dex.pumpfun import pumpfun
from dex.raydium import raydium
from sniper.rugcheck import check as rugcheck
from utils.helpers import now_ms
from utils.logger import get_logger

log = get_logger(__name__)


class SniperEngine:
    """Drives token discovery + entry decisions."""

    def __init__(self, on_buy_signal) -> None:
        """
        `on_buy_signal` is an async callable:
            await on_buy_signal(snap, decision)
        """
        self.on_buy_signal = on_buy_signal
        self._seen: Set[str] = set()
        self._last_seen_ts: Dict[str, int] = {}
        self._stop = asyncio.Event()
        self.candidates_seen: int = 0
        self.buys_emitted: int = 0

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        log.info("Sniper engine starting (mode=%s).", settings.mode)
        tasks = [
            asyncio.create_task(self._loop_pumpfun_ws(), name="pumpfun_ws"),
            asyncio.create_task(self._loop_pumpfun_poll(), name="pumpfun_poll"),
            asyncio.create_task(self._loop_dexscreener_trending(), name="dexscreener"),
            asyncio.create_task(self._loop_raydium_pools(), name="raydium"),
            asyncio.create_task(self._loop_meteora_pairs(), name="meteora"),
            asyncio.create_task(self._loop_launchlab(), name="launchlab"),
            asyncio.create_task(self._loop_cleanup(), name="cleanup"),
        ]
        try:
            await self._stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            log.info("Sniper engine stopped.")

    # ------------------------------------------------------------------
    # Feeds
    # ------------------------------------------------------------------

    async def _loop_pumpfun_ws(self) -> None:
        async for event in pumpfun.stream_new_tokens():
            if self._stop.is_set():
                return
            mint = event.get("mint")
            if not mint:
                continue
            # Pump.fun WS gives us enough metadata to build a snapshot
            # WITHOUT waiting 30-90s for DexScreener to index. We try
            # DexScreener first (for richer data); if it's not indexed yet,
            # we synthesize from the WS payload so the AI can score the
            # token immediately on launch.
            snap = await dexscreener.get_token(mint)
            if snap is None:
                snap = self._snapshot_from_pump_event(event)
            if snap is None:
                continue
            await self._handle_snapshot(snap, source="pumpfun_ws")

    @staticmethod
    def _snapshot_from_pump_event(event: dict):
        """Best-effort TokenSnapshot from a pumpportal create event."""
        try:
            mint = event.get("mint")
            if not mint:
                return None
            sol_in_pool = float(event.get("vSolInBondingCurve", 0))
            tokens_in_pool = float(event.get("vTokensInBondingCurve", 1) or 1)
            mc_sol = float(event.get("marketCapSol", 0))
            sol_usd = 150.0  # rough; only used for USD-ish liquidity proxy
            price_sol = (sol_in_pool / tokens_in_pool) if tokens_in_pool else 0.0
            return TokenSnapshot(
                mint=mint,
                symbol=event.get("symbol", "")[:20],
                name=event.get("name", "")[:32],
                dex="pumpfun",
                pair_address=event.get("bondingCurveKey") or "",
                price_usd=price_sol * sol_usd,
                price_sol=price_sol,
                liquidity_usd=sol_in_pool * sol_usd * 2,  # both sides
                market_cap=mc_sol * sol_usd,
                volume_5m_usd=float(event.get("solAmount", 0)) * sol_usd,
                buys_5m=1,        # creation = first buy
                sells_5m=0,
                txns_5m=1,
                created_at_ms=now_ms(),
                raw=event,
            )
        except Exception:
            return None

    async def _loop_pumpfun_poll(self) -> None:
        consecutive_failures = 0
        disabled = False
        while not self._stop.is_set():
            try:
                snaps = await pumpfun.latest(limit=30)
                if snaps:
                    consecutive_failures = 0
                    for s in snaps:
                        await self._handle_snapshot(s, source="pumpfun_poll")
                else:
                    consecutive_failures += 1
            except Exception as exc:
                consecutive_failures += 1
                log.debug("pumpfun poll error: %s", exc)

            # frontend-api.pump.fun gets decommissioned periodically. After
            # 5 dry/failed cycles, stop polling - the WS stream still works
            # and DexScreener trending covers stale tokens.
            if consecutive_failures >= 5 and not disabled:
                log.warning(
                    "pump.fun REST endpoint appears unreachable (5x). "
                    "Disabling REST poll; WebSocket + DexScreener still active."
                )
                disabled = True
            if disabled:
                await self._sleep(120.0)   # check again in 2 min
                # one-shot recheck
                try:
                    snaps = await pumpfun.latest(limit=5)
                    if snaps:
                        log.info("pump.fun REST endpoint recovered; resuming poll.")
                        disabled = False
                        consecutive_failures = 0
                except Exception:
                    pass
                continue

            await self._sleep(8.0)

    async def _loop_dexscreener_trending(self) -> None:
        while not self._stop.is_set():
            try:
                snaps = await dexscreener.trending()
                for s in snaps:
                    correlation_ai.update(s.mint, s.price_change_5m / 100.0)
                    await self._handle_snapshot(s, source="dexscreener")
            except Exception as exc:
                log.debug("dexscreener trending error: %s", exc)
            await self._sleep(20.0)

    async def _loop_raydium_pools(self) -> None:
        while not self._stop.is_set():
            try:
                pools = await raydium.list_pools(page=1, page_size=50)
                for p in pools:
                    mint = (p.get("mintA") or {}).get("address")
                    if not mint:
                        continue
                    await self._handle_candidate_mint(mint, source="raydium")
            except Exception as exc:
                log.debug("raydium pools error: %s", exc)
            await self._sleep(30.0)

    async def _loop_meteora_pairs(self) -> None:
        while not self._stop.is_set():
            try:
                pairs = await meteora.list_dlmm_pairs()
                for p in pairs[:200]:
                    for k in ("mint_x", "mint_y"):
                        m = p.get(k)
                        if m:
                            await self._handle_candidate_mint(m, source="meteora")
            except Exception as exc:
                log.debug("meteora poll error: %s", exc)
            await self._sleep(60.0)

    async def _loop_launchlab(self) -> None:
        while not self._stop.is_set():
            try:
                snaps = await launchlab.new_pairs()
                for s in snaps:
                    await self._handle_snapshot(s, source="launchlab")
            except Exception as exc:
                log.debug("launchlab poll error: %s", exc)
            await self._sleep(45.0)

    async def _loop_cleanup(self) -> None:
        """Periodically forget seen-mints older than 6h to bound memory."""
        while not self._stop.is_set():
            await self._sleep(600.0)
            cutoff = now_ms() - 6 * 3600 * 1000
            to_drop = [m for m, ts in self._last_seen_ts.items() if ts < cutoff]
            for m in to_drop:
                self._seen.discard(m)
                self._last_seen_ts.pop(m, None)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------------
    # Candidate handling
    # ------------------------------------------------------------------

    async def _handle_candidate_mint(self, mint: str, source: str) -> None:
        if mint in self._seen:
            return
        snap = await dexscreener.get_token(mint)
        if snap is None:
            return
        await self._handle_snapshot(snap, source=source)

    async def _handle_snapshot(self, snap: TokenSnapshot, source: str) -> None:
        if not snap.mint or snap.mint in self._seen:
            return
        if db.is_blacklisted(snap.mint):
            return

        self._seen.add(snap.mint)
        self._last_seen_ts[snap.mint] = now_ms()
        self.candidates_seen += 1

        # Persist token info for analytics.
        try:
            db.execute(
                """
                INSERT OR REPLACE INTO tokens
                (mint, symbol, name, dex, first_seen_ts, last_seen_ts,
                 liquidity_usd, volume_24h, market_cap, blacklisted)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (snap.mint, snap.symbol, snap.name, snap.dex,
                 now_ms(), now_ms(),
                 snap.liquidity_usd, snap.volume_24h_usd, snap.market_cap),
            )
        except Exception:
            pass

        report = await rugcheck(snap)
        if not report.safe:
            db.add_blacklist(snap.mint, "; ".join(report.reasons)[:240], now_ms())
            log.info("rug-skip %s (%s): %s",
                     snap.symbol or snap.mint[:8], snap.dex, "; ".join(report.reasons[:2]))
            return

        decision = decide(snap)
        verdict = "BUY" if decision.should_buy else "skip"
        # Always show the WHY so the user can debug gating.
        reason = "; ".join(decision.reasons[:2]) if decision.reasons else ""
        log.info(
            "[%s] %s liq=$%.0f vol5m=$%.0f bp=%+.2f xgb=%.2f conf=%.2f -> %s | %s",
            source, snap.symbol or snap.mint[:8],
            snap.liquidity_usd, snap.volume_5m_usd, snap.buy_pressure,
            decision.xgb_score, decision.confidence,
            verdict, reason,
        )
        if decision.should_buy:
            self.buys_emitted += 1
            try:
                await self.on_buy_signal(snap, decision)
            except Exception as exc:
                log.exception("on_buy_signal failed: %s", exc)
