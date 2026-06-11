from html import escape
from types import MethodType

import psutil

from .settings import Settings


def apply_telegram_status_patch(telegram_bot):
    """Patch Telegram /status to include webhook runtime diagnostics."""

    async def cmd_status(self, update, context):
        if not self._is_owner(update):
            return

        from .risk_manager import risk_manager
        from .universe import universe
        from .order_manager import order_manager
        from .webhook_events import last_event_summary

        mem = psutil.virtual_memory()
        last_webhook = last_event_summary()

        safe_stop_reason = escape(str(risk_manager.state.get("emergency_stop_reason", "")))
        safe_stop_time = escape(str(risk_manager.state.get("emergency_stop_time", "")))

        status_text = (
            "📊 <b>KStockBot Status</b>\n"
            f"- Mode: {escape(str(Settings.MODE))}\n"
            f"- MOCK: {escape(str(Settings.KIS_IS_MOCK))}\n"
            f"- Emergency Stop: {risk_manager.emergency_stop_active}\n"
        )

        if risk_manager.emergency_stop_active:
            status_text += (
                f"- Stop Reason: {safe_stop_reason}\n"
                f"- Stop Time: {safe_stop_time}\n"
            )

        webhook_configured = bool(Settings.WEBHOOK_SECRET and Settings.WEBHOOK_SECRET != "change-me")
        status_text += (
            f"- Daily Buys: {risk_manager.state.get('daily_buys', 0)}\n"
            f"- Daily Orders: {risk_manager.state.get('daily_orders', 0)}\n"
            f"- Daily Attempts: {risk_manager.state.get('daily_order_attempts', 0)}\n"
            f"- Daily Buy Amount: {risk_manager.state.get('daily_buy_amount', 0)}\n"
            f"- Watchlist Count: {len(universe.watchlist)}\n"
            f"- Pending Approvals: {len(order_manager._pending_approvals)}\n"
            f"- Webhook Secret Configured: {webhook_configured}\n"
            f"- Webhook Fast ACK: {Settings.WEBHOOK_FAST_ACK}\n"
            f"- Webhook Background Concurrency: {Settings.WEBHOOK_BACKGROUND_MAX_CONCURRENCY}\n"
            f"- Mem: {mem.percent}%\n"
        )

        if last_webhook:
            status_text += (
                "\n<b>Last Webhook Event</b>\n"
                f"- ID: <code>{escape(str(last_webhook.get('event_id', '')))}</code>\n"
                f"- Status: {escape(str(last_webhook.get('status', '')))}\n"
                f"- Signal: {escape(str(last_webhook.get('action', '')))} "
                f"<code>{escape(str(last_webhook.get('symbol', '')))}</code>\n"
                f"- Updated: {escape(str(last_webhook.get('updated_at', '')))}\n"
                f"- Message: {escape(str(last_webhook.get('message', ''))[:120])}\n"
            )

        await update.message.reply_text(status_text, parse_mode="HTML")

    telegram_bot.cmd_status = MethodType(cmd_status, telegram_bot)
    return telegram_bot
