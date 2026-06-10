from pydantic import BaseModel, field_validator
from typing import Optional
import time
from .settings import Settings
from .logger import get_logger
from .risk_manager import risk_manager
from .order_manager import order_manager
from html import escape

logger = get_logger("tradingview_webhook")

class TradingViewPayload(BaseModel):
    secret: str
    source: str
    strategy: str
    action: str
    symbol: str
    name: Optional[str] = None
    price: float
    time: str
    timeframe: str
    confidence: Optional[float] = None
    reason: Optional[str] = None

    @field_validator("action")
    def action_must_be_valid(cls, v):
        valid = ["BUY", "SELL", "CLOSE", "HOLD"]
        if v.upper() not in valid:
            raise ValueError(f"action must be one of {valid}")
        return v.upper()

    @field_validator("symbol")
    def symbol_must_be_6_digits(cls, v):
        if not v.isdigit() or len(v) != 6:
            raise ValueError("symbol must be 6 digits")
        return v

    @field_validator("price")
    def price_must_be_positive(cls, v):
        if v <= 0:
            raise ValueError("price must be > 0")
        return v

class WebhookProcessor:
    def __init__(self):
        from .storage import signals_storage
        self.signal_cache = signals_storage.load()
        if not isinstance(self.signal_cache, dict):
            self.signal_cache = {}
        self.dedup_seconds = 300 # 5 minutes

    def validate_secret_only(self, payload: TradingViewPayload) -> tuple[bool, str]:
        """Validate webhook secret without doing Telegram/KIS/risk/order work.

        Used by FastAPI endpoint fast-ack path so invalid requests are rejected
        immediately and valid requests can be processed in background.
        """
        if not Settings.WEBHOOK_SECRET or Settings.WEBHOOK_SECRET == "change-me":
            logger.warning("WEBHOOK_SECRET not configured. Rejecting.")
            return False, "Unauthorized (not configured)"

        if payload.secret != Settings.WEBHOOK_SECRET:
            logger.warning("Webhook secret mismatch")
            return False, "Unauthorized"

        return True, "OK"

    async def _send_telegram_message(self, text: str) -> None:
        try:
            from .telegram_bot import telegram_bot
            await telegram_bot.send_message(text)
        except Exception as e:
            logger.error(f"Failed to send Telegram signal message: {e}")

    def _save_cache(self):
        try:
            from .storage import signals_storage
            signals_storage.save(self.signal_cache)
        except Exception as e:
            logger.error(f"Failed to save webhook signal cache: {e}")

    async def process(self, payload: TradingViewPayload) -> dict:
        # 1. Validate Secret
        secret_ok, secret_msg = self.validate_secret_only(payload)
        if not secret_ok:
            return {"status": "error", "message": secret_msg}
            
        # 2. Basic signal logging and Telegram notification
        action = payload.action
        logger.info(f"Processing signal: {action} {payload.symbol} at {payload.price}")

        # Pre-telegram dedup for order-capable BUY/SELL signals.
        # This prevents repeated TradingView alerts from spamming Telegram.
        order_capable_signal = action in ["BUY", "SELL"] and Settings.MODE in ["paper", "manual_live"]
        cache_key = f"{payload.symbol}_{payload.action}_{payload.timeframe}_{payload.strategy}_{payload.time}"

        if order_capable_signal:
            now = time.time()
            last_time = self.signal_cache.get(cache_key, 0)
            if now - last_time < self.dedup_seconds:
                logger.info(f"Duplicate signal ignored before Telegram notification: {cache_key}")
                return {"status": "ignored", "message": "Duplicate signal"}

            self.signal_cache[cache_key] = now
            self._save_cache()
        
        safe_strategy = escape(str(payload.strategy))
        safe_symbol = escape(str(payload.symbol))
        safe_action = escape(str(action))
        safe_price = escape(str(payload.price))
        safe_reason = escape(str(payload.reason or ""))
        
        await self._send_telegram_message(
            f"🔔 <b>TradingView Signal</b>\n"
            f"Strategy: <code>{safe_strategy}</code>\n"
            f"Action: <b>{safe_action}</b>\n"
            f"Symbol: <code>{safe_symbol}</code>\n"
            f"Price: {safe_price}\n"
            f"Reason: {safe_reason}"
        )
        
        if action == "HOLD":
            return {"status": "ok", "message": "Hold signal logged"}
            
        if Settings.MODE == "analysis":
            return {"status": "ok", "message": "Mode is analysis. Logged only."}
            
        if action == "CLOSE":
            logger.warning("CLOSE action received but ignored (MVP constraint).")
            return {"status": "ignored", "message": "CLOSE not implemented yet"}
            
        if Settings.MODE == "live":
            logger.critical("LIVE auto trading webhook rejected: live mode is disabled in MVP")
            return {
                "status": "error",
                "message": "LIVE auto trading is disabled in MVP. Use manual_live with Telegram approval."
            }
            

        
        # 5. Risk validation and route to order manager
        amount = risk_manager.config.get("default_order_amount", 100000)
        
        # Risk Manager validate first
        is_valid, msg = risk_manager.validate_signal(payload.model_dump())
        if not is_valid:
            logger.warning(f"RiskManager rejected signal: {msg}")
            return {"status": "error", "message": f"Risk rejection: {msg}"}
            
        ok, msg = await order_manager.process_order_request(action, payload.symbol, payload.price, amount)
        logger.info(f"Order processing result: {ok}, {msg}")

        if not ok:
            return {
                "status": "error",
                "action": action,
                "symbol": payload.symbol,
                "message": msg,
            }

        return {
            "status": "ok",
            "action": action,
            "symbol": payload.symbol,
            "message": msg,
        }

webhook_processor = WebhookProcessor()
