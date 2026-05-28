"""
Telegram alerts + control commands.

Commands:
  /start, /stop          - pause / resume the engine
  /positions             - list open positions
  /balance               - show paper or real balances + P/L
  /buy <mint> [amount]   - manual buy (paper mode only by default)
  /sell <mint>           - manual sell
  /mode                  - show current mode
  /winrate               - winrate summary
  /topup <usd>           - add USD to paper wallet (paper only)

If TELEGRAM_ENABLED=false or no token configured, this module silently
no-ops, so it's safe to import unconditionally.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from config import settings
from utils.logger import get_logger

log = get_logger(__name__)


class TelegramBot:
    """Lightweight wrapper around python-telegram-bot v21."""

    def __init__(self) -> None:
        self.app = None
        self._enabled = bool(
            settings.telegram_enabled and settings.telegram_bot_token and settings.telegram_chat_id
        )
        self._pause_event: Optional[asyncio.Event] = None
        self._on_buy: Optional[Callable[..., Awaitable[None]]] = None
        self._on_sell: Optional[Callable[..., Awaitable[None]]] = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------

    def configure(
        self,
        pause_event: asyncio.Event,
        on_buy: Callable[..., Awaitable[None]],
        on_sell: Callable[..., Awaitable[None]],
    ) -> None:
        self._pause_event = pause_event
        self._on_buy = on_buy
        self._on_sell = on_sell

    async def start(self) -> None:
        if not self._enabled:
            log.info("Telegram disabled (set TELEGRAM_ENABLED=true to enable).")
            return
        try:
            from telegram import Update                     # noqa: PLC0415
            from telegram.ext import (                       # noqa: PLC0415
                ApplicationBuilder,
                CommandHandler,
                ContextTypes,
            )
        except ImportError:
            log.error("python-telegram-bot not installed; Telegram disabled.")
            self._enabled = False
            return

        self.app = ApplicationBuilder().token(settings.telegram_bot_token).build()
        self.app.add_handler(CommandHandler("start", self._h_start))
        self.app.add_handler(CommandHandler("stop", self._h_stop))
        self.app.add_handler(CommandHandler("positions", self._h_positions))
        self.app.add_handler(CommandHandler("balance", self._h_balance))
        self.app.add_handler(CommandHandler("buy", self._h_buy))
        self.app.add_handler(CommandHandler("sell", self._h_sell))
        self.app.add_handler(CommandHandler("mode", self._h_mode))
        self.app.add_handler(CommandHandler("winrate", self._h_winrate))
        self.app.add_handler(CommandHandler("topup", self._h_topup))

        # Swallow polling-loop errors (e.g. Conflict when a stale instance is
        # still holding the long-poll). The Updater retries automatically.
        async def _on_error(update, context) -> None:
            err = getattr(context, "error", None)
            log.warning("Telegram polling error: %s", err)

        self.app.add_error_handler(_on_error)

        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(
            drop_pending_updates=True,
            error_callback=lambda exc: log.warning("Telegram poll exc: %s", exc),
        )
        log.info("Telegram polling started.")

    async def stop(self) -> None:
        if not self._enabled or self.app is None:
            return
        try:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
        except Exception as exc:
            log.debug("telegram stop error: %s", exc)

    async def send(self, text: str) -> None:
        if not self._enabled or self.app is None:
            return
        try:
            # Plain text only - meme-coin symbols often contain markdown
            # specials (_ * ` [) and break the parser.
            await self.app.bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=text,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            log.debug("telegram send failed: %s", exc)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _auth_ok(self, update) -> bool:
        try:
            return str(update.effective_chat.id) == str(settings.telegram_chat_id)
        except Exception:
            return False

    async def _h_start(self, update, context) -> None:
        if not self._auth_ok(update):
            return
        if self._pause_event:
            self._pause_event.clear()
        await update.message.reply_text("Engine resumed.")

    async def _h_stop(self, update, context) -> None:
        if not self._auth_ok(update):
            return
        if self._pause_event:
            self._pause_event.set()
        await update.message.reply_text("Engine paused.")

    async def _h_mode(self, update, context) -> None:
        if not self._auth_ok(update):
            return
        await update.message.reply_text(
            f"Mode: {settings.mode}  (real_enabled={settings.enable_real_trading})"
        )

    async def _h_positions(self, update, context) -> None:
        if not self._auth_ok(update):
            return
        from trading.position_manager import position_manager
        lines = []
        for p in position_manager.list():
            label = (p.token_symbol or p.token_mint[:6])[:20]
            lines.append(f"{label} | qty={p.amount_token:.2f} entry={p.entry_price:.8f}")
        await update.message.reply_text("\n".join(lines) or "No open positions.")

    async def _h_balance(self, update, context) -> None:
        if not self._auth_ok(update):
            return
        from trading.paper_wallet import paper_wallet, _SOL_USD_DEFAULT
        from analytics.pnl import compute_realized_pnl

        if settings.is_real:
            from trading.real_wallet import get_real_wallet
            w = await get_real_wallet()
            if w:
                sol = await w.get_sol_balance()
                balance_usdt = sol * _SOL_USD_DEFAULT
                realized = compute_realized_pnl(_SOL_USD_DEFAULT)
                profit_usdt = max(realized.total_usd_approx, 0.0)
                loss_usdt = max(-realized.total_usd_approx, 0.0)
                emoji = "🟢" if realized.total_usd_approx >= 0 else "🔴"
                await update.message.reply_text(
                    f"REAL Wallet\n"
                    f"────────────\n"
                    f"Balance: ${balance_usdt:.2f}\n"
                    f"{emoji} Profit: ${profit_usdt:.2f}\n"
                    f"🔻 Loss:   ${loss_usdt:.2f}\n"
                    f"────────────\n"
                    f"Trades: {realized.trades} "
                    f"({realized.wins}W / {realized.losses}L)"
                )
                return
            await update.message.reply_text("Real wallet not loaded.")
            return

        # PAPER mode — all values in USDT only.
        sol_bal = paper_wallet.balance_sol()
        usdt_bal = paper_wallet.balance_usdt()
        holdings_cost_sol = sum(
            h.amount * h.avg_cost_sol for h in paper_wallet.holdings.values()
        )
        balance_usdt = sol_bal * _SOL_USD_DEFAULT + usdt_bal
        invested_usdt = holdings_cost_sol * _SOL_USD_DEFAULT
        equity_usdt = balance_usdt + invested_usdt
        start_usdt = settings.paper_starting_balance_usdt
        total_pl_usdt = equity_usdt - start_usdt
        pl_pct = (total_pl_usdt / start_usdt * 100) if start_usdt > 0 else 0.0

        realized = compute_realized_pnl(_SOL_USD_DEFAULT)
        profit_usdt = max(realized.total_usd_approx, 0.0)
        loss_usdt = max(-realized.total_usd_approx, 0.0)
        emoji = "🟢" if total_pl_usdt >= 0 else "🔴"

        msg = (
            f"PAPER Wallet\n"
            f"────────────\n"
            f"Balance:  ${balance_usdt:.2f} USDT\n"
            f"Invested: ${invested_usdt:.2f} USDT  ({len(paper_wallet.holdings)} open)\n"
            f"Total:    ${equity_usdt:.2f} USDT\n"
            f"────────────\n"
            f"🟢 Profit: ${profit_usdt:.2f}\n"
            f"🔻 Loss:   ${loss_usdt:.2f}\n"
            f"{emoji} Net P/L: ${total_pl_usdt:+.2f}  ({pl_pct:+.2f}%)\n"
            f"────────────\n"
            f"Trades: {realized.trades} "
            f"({realized.wins}W / {realized.losses}L)"
        )
        await update.message.reply_text(msg)

    async def _h_buy(self, update, context) -> None:
        if not self._auth_ok(update) or self._on_buy is None:
            return
        args = context.args or []
        if not args:
            await update.message.reply_text("Usage: /buy <mint> [amount_sol]")
            return
        mint = args[0]
        amount = float(args[1]) if len(args) > 1 else settings.default_buy_amount_sol
        try:
            await self._on_buy(mint=mint, amount_sol=amount)
            await update.message.reply_text(f"Buy requested: {mint[:8]} amount={amount}")
        except Exception as exc:
            await update.message.reply_text(f"Buy failed: {exc}")

    async def _h_sell(self, update, context) -> None:
        if not self._auth_ok(update) or self._on_sell is None:
            return
        args = context.args or []
        if not args:
            await update.message.reply_text("Usage: /sell <mint>")
            return
        try:
            await self._on_sell(mint=args[0])
            await update.message.reply_text(f"Sell requested: {args[0][:8]}")
        except Exception as exc:
            await update.message.reply_text(f"Sell failed: {exc}")

    async def _h_topup(self, update, context) -> None:
        if not self._auth_ok(update):
            return
        if settings.is_real:
            await update.message.reply_text(
                "Topup is paper-only. Real mode needs funds sent to the wallet."
            )
            return
        args = context.args or []
        if not args:
            await update.message.reply_text("Usage: /topup <usd_amount>   e.g. /topup 50")
            return
        try:
            amount = float(args[0])
        except ValueError:
            await update.message.reply_text("Invalid amount. Usage: /topup <usd_amount>")
            return
        if amount <= 0:
            await update.message.reply_text("Amount must be positive.")
            return

        from trading.paper_wallet import paper_wallet, _SOL_USD_DEFAULT
        sol_added = await paper_wallet.topup(amount)
        await update.message.reply_text(
            f"PAPER topup OK\n"
            f"+${amount:.2f}  (+{sol_added:.4f} SOL @ ${_SOL_USD_DEFAULT:.0f}/SOL)\n"
            f"New balance: {paper_wallet.balance_sol():.4f} SOL"
        )

    async def _h_winrate(self, update, context) -> None:
        if not self._auth_ok(update):
            return
        from analytics.winrate import winrate_last_n_days, overall_winrate
        w7 = winrate_last_n_days(7)
        wall = overall_winrate()
        msg = (
            f"7d:  {w7.wins}W / {w7.losses}L  ({w7.winrate:.1%})\n"
            f"All: {wall.wins}W / {wall.losses}L  ({wall.winrate:.1%})"
        )
        await update.message.reply_text(msg)


telegram_bot = TelegramBot()
