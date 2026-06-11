from types import MethodType
from telegram.ext import CommandHandler


def apply_mock_commands(telegram_bot):
    if getattr(telegram_bot, '_mock_commands_applied', False):
        return telegram_bot

    async def cmd_pnl(self, update, context):
        if not self._is_owner(update):
            return
        from .kis_client import kis_client
        from .performance_report import build_report, pnl_text
        await update.message.reply_text(pnl_text(await build_report(kis_client)), parse_mode='HTML')

    async def cmd_report(self, update, context):
        if not self._is_owner(update):
            return
        from .kis_client import kis_client
        from .performance_report import build_report, pnl_text, trades_text
        report = await build_report(kis_client)
        text = pnl_text(report) + '\n\n' + trades_text(report.get('today', {}), 'Today Ledger Trades')
        await update.message.reply_text(text[:3900], parse_mode='HTML')

    async def cmd_trades(self, update, context):
        if not self._is_owner(update):
            return
        from .performance_report import trades, trades_text
        limit = 10
        if context.args:
            try:
                limit = max(1, min(int(context.args[0]), 20))
            except Exception:
                limit = 10
        await update.message.reply_text(trades_text(trades(days=None, limit=limit)), parse_mode='HTML')

    async def cmd_today(self, update, context):
        if not self._is_owner(update):
            return
        from .performance_report import trades, trades_text
        await update.message.reply_text(trades_text(trades(days=1, limit=10), 'Today Paper/Mock Trades'), parse_mode='HTML')

    async def cmd_performance(self, update, context):
        if not self._is_owner(update):
            return
        await cmd_report(self, update, context)

    telegram_bot.cmd_pnl = MethodType(cmd_pnl, telegram_bot)
    telegram_bot.cmd_report = MethodType(cmd_report, telegram_bot)
    telegram_bot.cmd_trades = MethodType(cmd_trades, telegram_bot)
    telegram_bot.cmd_today = MethodType(cmd_today, telegram_bot)
    telegram_bot.cmd_performance = MethodType(cmd_performance, telegram_bot)

    original_initialize = telegram_bot.initialize

    async def initialize(self):
        await original_initialize()
        if not self.application:
            return
        self.application.add_handler(CommandHandler('pnl', self.cmd_pnl))
        self.application.add_handler(CommandHandler('report', self.cmd_report))
        self.application.add_handler(CommandHandler('trades', self.cmd_trades))
        self.application.add_handler(CommandHandler('today', self.cmd_today))
        self.application.add_handler(CommandHandler('performance', self.cmd_performance))

    telegram_bot.initialize = MethodType(initialize, telegram_bot)
    telegram_bot._mock_commands_applied = True
    return telegram_bot
