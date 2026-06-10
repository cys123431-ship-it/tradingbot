import psutil
from html import escape
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from .settings import Settings
from .logger import get_logger

logger = get_logger("telegram_bot")

class TelegramBot:
    def __init__(self):
        self.token = Settings.TELEGRAM_BOT_TOKEN
        self.owner_id = str(Settings.TELEGRAM_OWNER_ID)
        self.application = None
        self.is_running = False

    async def initialize(self):
        if not self.token:
            logger.warning("Telegram token not set. Telegram bot will be disabled.")
            return

        self.application = Application.builder().token(self.token).build()

        self.application.add_handler(CommandHandler("start", self.cmd_start))
        self.application.add_handler(CommandHandler("status", self.cmd_status))
        self.application.add_handler(CommandHandler("mode", self.cmd_mode))
        self.application.add_handler(CommandHandler("help", self.cmd_help))
        self.application.add_handler(CommandHandler("watchlist", self.cmd_watchlist))
        self.application.add_handler(CommandHandler("scan", self.cmd_scan))
        self.application.add_handler(CommandHandler("risk", self.cmd_risk))
        self.application.add_handler(CommandHandler("orders", self.cmd_orders))
        self.application.add_handler(CommandHandler("positions", self.cmd_positions))
        self.application.add_handler(CommandHandler("stoptrading", self.cmd_stoptrading))
        self.application.add_handler(CommandHandler("starttrading", self.cmd_starttrading))
        self.application.add_handler(CommandHandler("approve", self.cmd_approve))
        self.application.add_handler(CommandHandler("reject", self.cmd_reject))
        self.application.add_handler(CommandHandler("kischeck", self.cmd_kischeck))
        self.application.add_handler(CommandHandler("price", self.cmd_price))
        self.application.add_handler(CommandHandler("balance", self.cmd_balance))
        self.application.add_handler(CommandHandler("cash", self.cmd_cash))
        self.application.add_handler(CommandHandler("dryrun", self.cmd_dryrun))
        self.application.add_handler(CommandHandler("webhooks", self.cmd_webhooks))

        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling()
        self.is_running = True
        logger.info("Telegram bot initialized and polling started.")

    async def stop(self):
        if self.application and self.is_running:
            await self.application.updater.stop()
            await self.application.stop()
            await self.application.shutdown()
            self.is_running = False
            logger.info("Telegram bot stopped.")

    async def send_message(self, text: str):
        if self.application and self.owner_id and self.is_running:
            try:
                # Use HTML to avoid markdown escape issues
                await self.application.bot.send_message(chat_id=self.owner_id, text=text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Failed to send telegram message: {e}")

    def _is_owner(self, update: Update) -> bool:
        if not update.effective_user:
            logger.warning("Telegram update without effective_user rejected")
            return False
        user_id = str(update.effective_user.id)
        if user_id != self.owner_id:
            logger.warning(f"Unauthorized Telegram access attempt by {user_id}")
            return False
        return True

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update): return
        await update.message.reply_text("KStockBot is running.")

    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update): return
        text = (
            f"⚙️ <b>Mode Settings</b>\n"
            f"- MODE: {Settings.MODE}\n"
            f"- MOCK: {Settings.KIS_IS_MOCK}\n"
            f"- CONFIRM_LIVE_TRADING: {'Yes' if Settings.CONFIRM_LIVE_TRADING == 'yes-i-understand-real-money-risk' else 'No'}"
        )
        await update.message.reply_text(text, parse_mode="HTML")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update): return
        
        from .risk_manager import risk_manager
        from .universe import universe
        from .order_manager import order_manager
        
        mem = psutil.virtual_memory()
        
        safe_stop_reason = escape(str(risk_manager.state.get("emergency_stop_reason", "")))
        safe_stop_time = escape(str(risk_manager.state.get("emergency_stop_time", "")))
        
        status_text = (
            f"📊 <b>KStockBot Status</b>\n"
            f"- Mode: {Settings.MODE}\n"
            f"- MOCK: {Settings.KIS_IS_MOCK}\n"
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
            f"- Mem: {mem.percent}%\n"
        )
        await update.message.reply_text(status_text, parse_mode="HTML")

    async def cmd_watchlist(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update): return
        from .universe import universe
        wl = universe.get_watchlist()
        text = "📋 <b>Watchlist</b>\n"
        for i in wl:
            safe_name = escape(str(i.get('name', '')))
            safe_symbol = escape(str(i.get('symbol', '')))
            text += f"- {safe_name} ({safe_symbol})\n"
        if not wl: text += "Empty."
        await update.message.reply_text(text, parse_mode="HTML")

    async def cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update): return
        await update.message.reply_text("🔍 <b>Scan Started...</b>", parse_mode="HTML")
        from .strategy import scan_watchlist
        from .kis_client import kis_client
        from .universe import universe
        try:
            results = await scan_watchlist(kis_client, universe)
            text = "📊 <b>Scan Results:</b>\n"
            for r in results[:5]:
                safe_symbol = escape(str(r.get('symbol', '')))
                safe_reason = escape(str(r.get('reason', '')))
                text += f"- {safe_symbol}: Score {r['score']} ({safe_reason})\n"
            await update.message.reply_text(text, parse_mode="HTML")
        except Exception as e:
            safe_e = escape(str(e))
            await update.message.reply_text(f"Scan failed: {safe_e}")

    async def cmd_risk(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update): return
        from .risk_manager import risk_manager
        config = risk_manager.config
        lines = []
        for k, v in config.items():
            lines.append(f"- {escape(str(k))}: {escape(str(v))}")
        text = "🛡️ <b>Risk Config</b>\n" + "\n".join(lines)
        await update.message.reply_text(text, parse_mode="HTML")

    async def cmd_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update): return
        from .kis_client import kis_client
        try:
            orders = await kis_client.get_open_orders()
            text = f"📝 <b>Open Orders</b> ({len(orders)})\n"
            for o in orders:
                safe_pdno = escape(str(o.get('pdno', '')))
                safe_dvsn = escape(str(o.get('sll_buy_dvsn_cd_name', '')))
                text += f"- {safe_pdno} {safe_dvsn} {o.get('ord_qty')} qty @ {o.get('ord_unpr')}\n"
            if not orders: text += "No open orders."
            await update.message.reply_text(text, parse_mode="HTML")
        except Exception as e:
            safe_e = escape(str(e))
            await update.message.reply_text(f"Error fetching orders: {safe_e}")

    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update): return
        from .kis_client import kis_client
        try:
            bal = await kis_client.get_balance()
            output = bal.get("output1", []) if bal else []
            text = f"💼 <b>Positions</b>\n"
            for p in output:
                safe_name = escape(str(p.get('prdt_name', '')))
                safe_pdno = escape(str(p.get('pdno', '')))
                text += f"- {safe_name} ({safe_pdno}): {p.get('hldg_qty')} qty (Ret: {p.get('evlu_pfls_rt')}%)\n"
            if not output: text += "No positions."
            await update.message.reply_text(text, parse_mode="HTML")
        except Exception as e:
            safe_e = escape(str(e))
            await update.message.reply_text(f"Error fetching positions: {safe_e}")

    async def cmd_stoptrading(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update): return
        from .risk_manager import risk_manager
        risk_manager.emergency_stop("Triggered by Telegram Owner")
        await update.message.reply_text("🛑 <b>EMERGENCY STOP ACTIVATED</b>. All new orders are blocked.", parse_mode="HTML")

    async def cmd_starttrading(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update): return
        from .risk_manager import risk_manager
        
        args = context.args
        if Settings.MODE in ["live", "manual_live"]:
            if not args or args[0] != "CONFIRM":
                await update.message.reply_text(
                    "Live/Manual_Live mode requires confirmation to resume.\n"
                    "Type: <code>/starttrading CONFIRM</code>",
                    parse_mode="HTML"
                )
                return
                
        risk_manager.resume_trading()
        await update.message.reply_text("✅ <b>Trading Resumed</b>.", parse_mode="HTML")

    async def cmd_approve(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update): return
        if Settings.MODE != "manual_live":
            await update.message.reply_text("Approve is only available in manual_live mode.")
            return
            
        args = context.args
        if not args:
            await update.message.reply_text("Usage: /approve <order_id>")
            return
        
        from .order_manager import order_manager
        approval_id = args[0]
        try:
            res = await order_manager.submit_live_order_after_approval(approval_id)
            if "error" in res:
                safe_e = escape(str(res["error"]))
                await update.message.reply_text(
                    f"❌ Failed to approve: {safe_e}",
                    parse_mode="HTML"
                )
            else:
                safe_res = escape(str(res))
                await update.message.reply_text(
                    f"✅ Order Approved and Submitted.\nResult: {safe_res}",
                    parse_mode="HTML"
                )
        except Exception as e:
            safe_e = escape(str(e))
            await update.message.reply_text(
                f"❌ Failed to approve: {safe_e}",
                parse_mode="HTML"
            )

    async def cmd_reject(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update): return
        args = context.args
        if not args:
            await update.message.reply_text("Usage: /reject <order_id>")
            return
            
        from .order_manager import order_manager
        approval_id = args[0]
        ok, msg = order_manager.reject_order(approval_id)
        safe_approval_id = escape(str(approval_id))
        safe_msg = escape(str(msg))
        
        if ok:
            await update.message.reply_text(
                f"🚫 Order <code>{safe_approval_id}</code> manually rejected.",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                f"❌ Reject failed for <code>{safe_approval_id}</code>: {safe_msg}",
                parse_mode="HTML"
            )

    async def cmd_kischeck(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update):
            return

        from .kis_client import kis_client
        from .settings import Settings

        try:
            token_ok = False
            token_error = ""

            try:
                token = await kis_client.auth.ensure_token_valid()
                token_ok = bool(token)
            except Exception as e:
                token_error = str(e)

            account_configured = bool(Settings.KIS_ACCOUNT_NO and Settings.KIS_ACCOUNT_PRODUCT_CODE)
            app_configured = bool(Settings.KIS_APP_KEY and Settings.KIS_APP_SECRET)
            webhook_configured = bool(Settings.WEBHOOK_SECRET and Settings.WEBHOOK_SECRET != "change-me")

            text = (
                "🔎 <b>KIS Connection Check</b>\n"
                f"- Mode: {escape(str(Settings.MODE))}\n"
                f"- Mock: {escape(str(Settings.KIS_IS_MOCK))}\n"
                f"- App Credentials: {app_configured}\n"
                f"- Account Configured: {account_configured}\n"
                f"- Token OK: {token_ok}\n"
                f"- Webhook Secret Configured: {webhook_configured}\n"
            )

            if token_error:
                safe_err = escape(token_error[:200])
                text += f"- Token Error: {safe_err}\n"

            text += "\n이 명령은 주문을 실행하지 않습니다."
            await update.message.reply_text(text, parse_mode="HTML")

        except Exception as e:
            safe_e = escape(str(e))
            await update.message.reply_text(f"❌ KIS check failed: {safe_e}", parse_mode="HTML")

    async def cmd_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update):
            return

        args = context.args
        if not args:
            await update.message.reply_text("Usage: /price <6-digit-symbol>\nExample: /price 005930")
            return

        symbol = str(args[0]).strip()
        if not (symbol.isdigit() and len(symbol) == 6):
            await update.message.reply_text("❌ Symbol must be a 6-digit Korean stock code. Example: /price 005930")
            return

        from .kis_client import kis_client

        try:
            res = await kis_client.get_price(symbol)
            output = res.get("output", {}) if isinstance(res, dict) else {}

            price = output.get("stck_prpr", "N/A")
            change_rate = output.get("prdy_ctrt", "N/A")
            volume = output.get("acml_vol", "N/A")

            text = (
                f"💹 <b>Price</b>\n"
                f"- Symbol: <code>{escape(symbol)}</code>\n"
                f"- Price: {escape(str(price))}\n"
                f"- Change Rate: {escape(str(change_rate))}%\n"
                f"- Volume: {escape(str(volume))}\n"
            )
            await update.message.reply_text(text, parse_mode="HTML")

        except Exception as e:
            safe_e = escape(str(e)[:300])
            await update.message.reply_text(f"❌ Price fetch failed: {safe_e}", parse_mode="HTML")

    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update):
            return

        from .kis_client import kis_client

        try:
            bal = await kis_client.get_balance()
            output1 = bal.get("output1", []) if isinstance(bal, dict) else []
            output2 = bal.get("output2", []) if isinstance(bal, dict) else []

            text = "💼 <b>KIS Balance Summary</b>\n"
            text += f"- Holdings Count: {len(output1) if isinstance(output1, list) else 0}\n"

            if isinstance(output2, list) and output2:
                summary = output2[0]
                total_eval = summary.get("tot_evlu_amt", "N/A")
                cash_like = summary.get("dnca_tot_amt", "N/A")
                text += f"- Total Evaluation: {escape(str(total_eval))}\n"
                text += f"- Cash-like Amount: {escape(str(cash_like))}\n"

            if isinstance(output1, list) and output1:
                text += "\n<b>Top Holdings</b>\n"
                for p in output1[:5]:
                    name = escape(str(p.get("prdt_name", "")))
                    pdno = escape(str(p.get("pdno", "")))
                    qty = escape(str(p.get("hldg_qty", "")))
                    text += f"- {name} (<code>{pdno}</code>): {qty} qty\n"
            else:
                text += "- No holdings or unavailable.\n"

            await update.message.reply_text(text, parse_mode="HTML")

        except Exception as e:
            safe_e = escape(str(e)[:300])
            await update.message.reply_text(f"❌ Balance fetch failed: {safe_e}", parse_mode="HTML")

    async def cmd_cash(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update):
            return

        args = context.args
        symbol = ""
        price = 0

        if len(args) >= 1:
            symbol = str(args[0]).strip()
            if symbol and not (symbol.isdigit() and len(symbol) == 6):
                await update.message.reply_text("❌ Symbol must be a 6-digit Korean stock code. Example: /cash 005930 80000")
                return

        if len(args) >= 2:
            try:
                price = int(args[1])
            except Exception:
                await update.message.reply_text("❌ Price must be an integer. Example: /cash 005930 80000")
                return

        from .kis_client import kis_client

        try:
            res = await kis_client.get_buyable_cash(symbol=symbol, price=price)
            output = res.get("output", {}) if isinstance(res, dict) else {}

            text = "💰 <b>Buyable Cash Check</b>\n"
            text += f"- Symbol: <code>{escape(symbol or 'N/A')}</code>\n"
            text += f"- Price: {escape(str(price or 'N/A'))}\n"

            if output:
                for k in ["ord_psbl_cash", "nrcvb_buy_amt", "max_buy_qty", "ord_psbl_qty"]:
                    if k in output:
                        text += f"- {escape(str(k))}: {escape(str(output.get(k)))}\n"
            else:
                text += "- Output unavailable or empty.\n"

            await update.message.reply_text(text, parse_mode="HTML")

        except Exception as e:
            safe_e = escape(str(e)[:300])
            await update.message.reply_text(f"❌ Cash check failed: {safe_e}", parse_mode="HTML")

    async def cmd_dryrun(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update):
            return

        from .kis_client import kis_client
        from .settings import Settings

        checks = []

        async def run_check(name: str, coro):
            try:
                result = await coro
                checks.append((name, True, "OK"))
                return result
            except Exception as e:
                err = str(e)
                if len(err) > 180:
                    err = err[:180] + "..."
                checks.append((name, False, err))
                return None

        await update.message.reply_text(
            "🧪 <b>KIS Dry Run Started</b>\n"
            "이 명령은 주문을 실행하지 않습니다.",
            parse_mode="HTML"
        )

        token = await run_check("token", kis_client.auth.ensure_token_valid())
        price = await run_check("price 005930", kis_client.get_price("005930"))
        balance = await run_check("balance", kis_client.get_balance())
        cash = await run_check("cash 005930 80000", kis_client.get_buyable_cash("005930", 80000))

        lines = [
            "🧪 <b>KIS Dry Run Result</b>",
            f"- Mode: {escape(str(Settings.MODE))}",
            f"- Mock: {escape(str(Settings.KIS_IS_MOCK))}",
            "- Order Execution: NO",
            ""
        ]

        for name, ok, msg in checks:
            safe_name = escape(str(name))
            safe_msg = escape(str(msg))
            mark = "✅" if ok else "❌"
            lines.append(f"{mark} {safe_name}: {safe_msg}")

        # Add tiny sanitized hints from successful responses.
        if isinstance(price, dict):
            output = price.get("output", {})
            current_price = output.get("stck_prpr", "N/A")
            lines.append(f"- Sample Price 005930: {escape(str(current_price))}")

        if isinstance(balance, dict):
            output1 = balance.get("output1", [])
            lines.append(f"- Holdings Rows: {len(output1) if isinstance(output1, list) else 'N/A'}")

        if isinstance(cash, dict):
            output = cash.get("output", {})
            if isinstance(output, dict):
                ord_psbl_cash = output.get("ord_psbl_cash", "N/A")
                lines.append(f"- Buyable Cash Field: {escape(str(ord_psbl_cash))}")

        lines.append("")
        lines.append("다음 단계: analysis 모드 확인 후에만 paper 모드로 전환하세요.")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def cmd_webhooks(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update):
            return

        from .webhook_events import list_recent_events

        try:
            limit = 5
            if context.args:
                try:
                    limit = max(1, min(int(context.args[0]), 10))
                except Exception:
                    limit = 5

            events = list_recent_events(limit)

            if not events:
                await update.message.reply_text("No webhook events recorded yet.")
                return

            lines = ["🧾 <b>Recent Webhook Events</b>"]
            for event in events:
                event_id = escape(str(event.get("event_id", "")))
                status = escape(str(event.get("status", "")))
                action = escape(str(event.get("action", "")))
                symbol = escape(str(event.get("symbol", "")))
                updated_at = escape(str(event.get("updated_at", "")))
                message = escape(str(event.get("message", ""))[:120])

                lines.append(
                    f"\n- <code>{event_id}</code> | {status} | {action} <code>{symbol}</code>\n"
                    f"  {updated_at}\n"
                    f"  {message}"
                )

            await update.message.reply_text("\n".join(lines), parse_mode="HTML")

        except Exception as e:
            safe_e = escape(str(e)[:300])
            await update.message.reply_text(f"❌ Webhook event check failed: {safe_e}", parse_mode="HTML")

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update): return
        
        help_text = (
            "Available commands:\n"
            "/start - Start bot interaction\n"
            "/status - Check bot status\n"
            "/mode - View current mode\n"
            "/watchlist - View current watchlist\n"
            "/scan - Run strategy scan (no trade)\n"
            "/risk - View current risk limits\n"
            "/orders - View today's orders\n"
            "/positions - View current holdings\n"
            "/stoptrading - Emergency stop all trading\n"
            "/starttrading - Resume trading\n"
            "/approve <id> - Approve pending order (manual_live)\n"
            "/reject <id> - Reject pending order (manual_live)\n"
            "/kischeck - KIS token/account/mock 설정 점검, 주문 없음\n"
            "/dryrun - KIS token/price/balance/cash read-only 통합 점검, 주문 없음\n"
            "/price <종목코드> - 현재가 조회, 주문 없음\n"
            "/balance - 잔고 요약 조회, 주문 없음\n"
            "/cash [종목코드] [가격] - 주문가능금액 조회, 주문 없음\n"
            "/webhooks [n] - 최근 TradingView webhook 처리 결과 확인, 주문 없음\n"
            "/help - Show this help\n\n"
            "⚠️ live 완전자동 실전 주문은 MVP 단계에서 전면 봉인되어 있습니다.\n"
            "MVP에서는 live 완전자동 주문이 거부됩니다. 실전 주문은 manual_live + Telegram 승인 방식으로만 별도 검증 후 진행하세요."
        )
        await update.message.reply_text(help_text)

telegram_bot = TelegramBot()
