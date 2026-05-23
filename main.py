"""
AI Multi-Chain Sniper - main entry point (v2).

Wires together:

  * Solana sniper engine (DEX feeds + AI smart_entry)
  * EVM new-pair sniper per enabled chain
  * RPC pool with latency monitoring + failover
  * Auto-buy + auto-sell routers
  * Paper / real wallet (Solana + EVM)
  * Telegram remote control
  * Rich terminal dashboard with multi-chain status
  * Ctrl-C aware graceful shutdown

Default mode is PAPER. REAL mode requires:
  1. MODE=REAL
  2. ENABLE_REAL_TRADING=true
  3. Chain-appropriate WALLET_PRIVATE_KEY / EVM_PRIVATE_KEY
  4. Interactive typed confirmation at startup
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from typing import Dict, List, Optional

from rich.align import Align
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from alerts.telegram import telegram_bot
from analytics.pnl import compute_realized_pnl
from analytics.winrate import overall_winrate, winrate_last_n_days
from chains import EVM_CHAINS, Chain
from config import settings
from dex.dexscreener import dexscreener
from sniper import rpc_failover
from sniper.sniper_engine import SniperEngine
from trading.auto_buy import on_buy_signal, risk_gate
from trading.auto_sell import auto_seller
from trading.copy_trading import CopyTrader
from trading.jupiter_swap import jupiter
from trading.paper_wallet import paper_wallet
from trading.position_manager import position_manager
from trading.real_wallet import get_real_wallet
from utils.logger import console, get_logger

log = get_logger("main")


REAL_WARNING = (
    "╔══════════════════════════════════════════════════════════════════╗\n"
    "║                       *** REAL MONEY MODE ***                    ║\n"
    "║                                                                  ║\n"
    "║  Live swaps will be executed against your wallet across the      ║\n"
    "║  enabled chains. Crypto trading carries risk of total loss.      ║\n"
    "║  You are responsible for every transaction this bot signs.       ║\n"
    "║                                                                  ║\n"
    "║  Stop now if you did not mean to enable this mode.               ║\n"
    "╚══════════════════════════════════════════════════════════════════╝"
)


# ---------------------------------------------------------------------------
# Safety flow
# ---------------------------------------------------------------------------

async def confirm_real_mode() -> bool:
    if settings.mode != "REAL":
        return True
    if not settings.enable_real_trading:
        log.warning("MODE=REAL but ENABLE_REAL_TRADING=false. Staying in PAPER.")
        return False

    # Headless deployments (Railway, Docker, systemd) have no TTY - we
    # cannot prompt for input. Require the explicit env confirmation instead.
    if not sys.stdin.isatty():
        env_confirm = os.getenv("REAL_CONFIRMED", "")
        if env_confirm.strip() == "I UNDERSTAND THE RISKS":
            log.warning("Headless REAL mode: confirmation via REAL_CONFIRMED env var.")
        else:
            log.error(
                "Headless environment detected; REAL mode requires env var "
                "REAL_CONFIRMED='I UNDERSTAND THE RISKS'. Falling back to PAPER."
            )
            return False
    else:
        console().print(Panel.fit(Text(REAL_WARNING, style="bold red"), border_style="red"))
        console().print("[yellow]Type exactly  I UNDERSTAND THE RISKS  to continue (60s timeout):[/yellow]")

        try:
            line = await asyncio.wait_for(asyncio.to_thread(sys.stdin.readline), timeout=60)
        except asyncio.TimeoutError:
            log.error("REAL confirmation timed out. Falling back to PAPER.")
            return False
        if line.strip() != "I UNDERSTAND THE RISKS":
            log.error("Confirmation phrase mismatch. Falling back to PAPER.")
            return False

    # Solana wallet is mandatory if Solana enabled.
    if "solana" in settings.enabled_chains:
        wallet = await get_real_wallet()
        if wallet is None:
            log.error("No usable Solana wallet. Falling back to PAPER.")
            return False
        try:
            bal = await wallet.get_sol_balance()
            log.info("Solana wallet ready: %s | %.4f SOL", wallet.pubkey_str, bal)
        except Exception as exc:
            log.error("Could not read Solana balance (%s).", exc)
            return False

    # EVM wallet check if any EVM chain enabled.
    if any(c != "solana" and c != "tron" for c in settings.enabled_chains):
        from chains.evm.wallet import get_evm_wallet
        ew = get_evm_wallet()
        if ew is None:
            log.error("EVM chains enabled but no EVM wallet configured.")
            return False
        log.info("EVM wallet ready: %s", ew.address)

    return True


# ---------------------------------------------------------------------------
# RPC pool setup
# ---------------------------------------------------------------------------

def _setup_rpc_pools() -> None:
    """Register endpoints for each enabled chain in the RPC pool."""
    for chain in settings.enabled_chains:
        p = rpc_failover.pool(chain)
        for url in settings.chain_rpcs(chain):
            p.add(url, label=f"{chain}-{url[:30]}")


# ---------------------------------------------------------------------------
# EVM pair-handler
# ---------------------------------------------------------------------------

async def _on_evm_pair_created(chain: Chain, pair: str, token0: str, token1: str) -> None:
    """When a new EVM pair appears we'd integrate with DexScreener for
    metadata - here we just log it. Wiring this into the full AI pipeline
    is straightforward but DexScreener takes 30-90s to index a new EVM pair.
    """
    log.info("[%s] new pair %s (%s / %s)", chain.value, pair[:10], token0[:8], token1[:8])
    # TODO: resolve to DexScreener once indexed, then feed sniper engine.


# ---------------------------------------------------------------------------
# Manual control hooks
# ---------------------------------------------------------------------------

async def manual_buy(mint: str, amount_sol: float) -> None:
    snap = await dexscreener.get_token(mint)
    if snap is None:
        log.warning("manual buy: token not found %s", mint)
        return
    from ai.smart_entry import SmartEntryDecision
    fake = SmartEntryDecision(
        should_buy=True,
        confidence=0.99,
        suggested_sol=amount_sol,
        rl_action="BUY_SMALL",
        xgb_score=0.99,
        lstm_score=0.5,
        chart_score=0.5,
        orderflow_score=0.5,
        smart_money_score=0.0,
        ecosystem_heat=0.0,
        state_key="manual",
        reasons=["manual override"],
    )
    await on_buy_signal(snap, fake)


async def manual_sell(mint: str) -> None:
    if not position_manager.has(mint):
        log.warning("manual sell: no open position for %s", mint[:8])
        return
    pos = position_manager.positions[mint]
    snap = await dexscreener.get_token(mint)
    if snap is None or snap.price_sol <= 0:
        log.warning("manual sell: no live price for %s", mint[:8])
        return
    await auto_seller._exit(pos, snap.price_sol, "manual")


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def _build_dashboard(
    mode: str,
    sniper: SniperEngine,
    paused: bool,
    real_sol: Optional[float],
) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(name="positions", ratio=2),
        Layout(name="sidebar", ratio=1),
    )

    mode_color = "red bold" if mode == "REAL" else "green bold"
    pause_lbl = "[yellow]PAUSED[/yellow]" if paused else "[bold]LIVE[/bold]"
    chains = ", ".join(settings.enabled_chains)
    header = Text.from_markup(
        f"[{mode_color}]MODE: {mode}[/]  |  {pause_lbl}  |  "
        f"chains: {chains}  |  candidates: {sniper.candidates_seen}  |  buys: {sniper.buys_emitted}",
        justify="center",
    )
    layout["header"].update(Panel(Align.center(header), title="AI Multi-Chain Sniper",
                                  border_style="cyan"))

    table = Table(expand=True, header_style="bold magenta")
    table.add_column("Token")
    table.add_column("DEX")
    table.add_column("Entry", justify="right")
    table.add_column("Qty", justify="right")
    table.add_column("HighWtr", justify="right")
    table.add_column("AI", justify="right")
    for p in position_manager.list():
        table.add_row(
            (p.token_symbol or p.token_mint[:6])[:14],
            (p.dex or "-")[:10],
            f"{p.entry_price:.8f}",
            f"{p.amount_token:.2f}",
            f"{p.high_water:.8f}",
            f"{p.ai_score:.2f}",
        )
    if not position_manager.list():
        table.add_row("—", "—", "—", "—", "—", "—")
    layout["positions"].update(Panel(table, title=f"Open Positions ({position_manager.count()})",
                                     border_style="blue"))

    side = Table.grid(padding=(0, 1))
    side.add_column(justify="right", style="bold")
    side.add_column()
    if mode == "REAL":
        side.add_row("SOL", f"{(real_sol or 0):.4f}")
    else:
        side.add_row("SOL (paper)", f"{paper_wallet.balance_sol():.4f}")
        side.add_row("USDT (paper)", f"{paper_wallet.balance_usdt():.2f}")
        side.add_row("Holdings", f"{len(paper_wallet.holdings)}")
    side.add_row("", "")
    wr7 = winrate_last_n_days(7)
    wrall = overall_winrate()
    side.add_row("7d W/L", f"{wr7.wins}/{wr7.losses} ({wr7.winrate:.0%})")
    side.add_row("All W/L", f"{wrall.wins}/{wrall.losses} ({wrall.winrate:.0%})")
    realized = compute_realized_pnl()
    pnl_color = "green" if realized.total_sol >= 0 else "red"
    side.add_row("Realized PnL", f"[{pnl_color}]{realized.total_sol:+.4f} SOL[/]")
    side.add_row("Daily PnL gate", f"${risk_gate.daily_realized_usd:+.2f}")

    # RPC pool status
    side.add_row("", "")
    for p in rpc_failover.all_pools():
        eps = p.endpoints()
        if not eps:
            continue
        healthy = sum(1 for e in eps if e.is_available())
        best = min(eps, key=lambda e: e.latency_ms) if eps else None
        if best:
            side.add_row(f"RPC {p.chain}", f"{healthy}/{len(eps)} ok | {best.latency_ms:.0f}ms")
    layout["sidebar"].update(Panel(side, title="Status", border_style="green"))

    layout["footer"].update(
        Panel(
            Text.from_markup(
                "[dim]Ctrl-C to stop  |  Telegram: /start /stop /positions /balance /winrate[/dim]",
                justify="center",
            ),
            border_style="grey39",
        )
    )
    return layout


# ---------------------------------------------------------------------------
# Main lifecycle
# ---------------------------------------------------------------------------

async def main() -> None:
    log.info("Booting AI Multi-Chain Sniper... mode=%s chains=%s",
             settings.mode, settings.enabled_chains)

    real_ok = await confirm_real_mode()
    if settings.mode == "REAL" and not real_ok:
        log.warning("Downgrading to PAPER mode for safety.")
        settings.mode = "PAPER"  # type: ignore[assignment]
        settings.enable_real_trading = False

    # ---- RPC pools ----
    _setup_rpc_pools()
    await rpc_failover.start_all()

    paused_event = asyncio.Event()
    telegram_bot.configure(pause_event=paused_event, on_buy=manual_buy, on_sell=manual_sell)

    async def gated_buy_signal(snap, decision):
        if paused_event.is_set():
            return False
        return await on_buy_signal(snap, decision)

    # ---- Solana sniper ----
    sniper = SniperEngine(on_buy_signal=gated_buy_signal)

    # ---- Copy trader ----
    smart_wallets = [w.strip() for w in (os.getenv("SMART_WALLETS") or "").split(",") if w.strip()]

    async def whale_candidate(mint: str, source: str) -> None:
        await sniper._handle_candidate_mint(mint, source=source)

    copy_trader = CopyTrader(wallets=smart_wallets, on_candidate=whale_candidate)

    # ---- EVM snipers ----
    evm_snipers = []
    if any(c not in ("solana", "tron") for c in settings.enabled_chains):
        from chains.evm.sniper import EVMSniper
        for chain_name in settings.enabled_chains:
            try:
                chain = Chain(chain_name)
            except ValueError:
                continue
            if chain == Chain.SOLANA or chain == Chain.TRON:
                continue
            if chain not in EVM_CHAINS:
                continue
            evm_snipers.append(EVMSniper(chain=chain, on_pair_created=_on_evm_pair_created))

    tasks = [
        asyncio.create_task(sniper.run(), name="solana_sniper"),
        asyncio.create_task(auto_seller.run(), name="seller"),
        asyncio.create_task(copy_trader.run(), name="copy"),
        asyncio.create_task(telegram_bot.start(), name="telegram"),
    ]
    for es in evm_snipers:
        tasks.append(asyncio.create_task(es.run(), name=f"evm_{es.chain.value}"))

    # ---- shutdown signals ----
    stop_event = asyncio.Event()

    def _request_stop(*_: object) -> None:
        if not stop_event.is_set():
            log.info("Shutdown signal received.")
            stop_event.set()

    try:
        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                asyncio.get_running_loop().add_signal_handler(s, _request_stop)
            except (NotImplementedError, RuntimeError):
                signal.signal(s, _request_stop)
    except Exception:
        pass

    real_sol_cached: Optional[float] = None
    last_real_check = 0.0
    headless = not sys.stdout.isatty()

    async def _refresh_real_balance() -> None:
        nonlocal real_sol_cached, last_real_check
        if settings.is_real and time.monotonic() - last_real_check > 30:
            try:
                wallet = await get_real_wallet()
                if wallet:
                    real_sol_cached = await wallet.get_sol_balance()
            except Exception:
                pass
            last_real_check = time.monotonic()

    try:
        if headless:
            # Railway / Docker / systemd - no TTY. Log periodic status instead
            # of running a Rich Live dashboard that would spam ANSI codes.
            log.info("Headless mode detected - dashboard disabled; periodic status logs.")
            last_status_log = 0.0
            while not stop_event.is_set():
                await _refresh_real_balance()
                if time.monotonic() - last_status_log > 60:
                    log.info(
                        "STATUS mode=%s positions=%d candidates=%d buys=%d sol=%.4f",
                        settings.mode, position_manager.count(),
                        sniper.candidates_seen, sniper.buys_emitted,
                        real_sol_cached if settings.is_real else paper_wallet.balance_sol(),
                    )
                    last_status_log = time.monotonic()
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
        else:
            with Live(console=console(), refresh_per_second=2, screen=False) as live:
                while not stop_event.is_set():
                    await _refresh_real_balance()
                    live.update(_build_dashboard(
                        mode=settings.mode, sniper=sniper,
                        paused=paused_event.is_set(), real_sol=real_sol_cached,
                    ))
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
    finally:
        log.info("Stopping engines...")
        sniper.stop()
        auto_seller.stop()
        copy_trader.stop()
        for es in evm_snipers:
            es.stop()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        await rpc_failover.stop_all()
        try:
            await telegram_bot.stop()
        except Exception:
            pass
        try:
            await dexscreener.close()
            await jupiter.close()
        except Exception:
            pass
        log.info("Goodbye.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
