import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, Response, BackgroundTasks
# APScheduler is a required runtime dependency.
# Run `pip install -r requirements.txt` before importing this module in tests or production.
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .settings import Settings
from .logger import get_logger
from .telegram_bot import telegram_bot
from .telegram_status_patch import apply_telegram_status_patch
from .tradingview_webhook import TradingViewPayload, webhook_processor
from .risk_manager import risk_manager
from .order_manager import order_manager
from .kis_client import kis_client
from .webhook_events import create_event, update_event, last_event_summary

logger = get_logger("main")
scheduler = AsyncIOScheduler(timezone=Settings.TIMEZONE)
webhook_background_semaphore = asyncio.Semaphore(Settings.WEBHOOK_BACKGROUND_MAX_CONCURRENCY)
apply_telegram_status_patch(telegram_bot)

async def scheduled_token_refresh():
    logger.info("Running scheduled KIS token refresh...")
    try:
        await kis_client.auth.ensure_token_valid()
    except Exception as e:
        logger.error(f"Scheduled token refresh failed: {e}")

async def scheduled_daily_reset():
    try:
        risk_manager.reset_daily_counters()
        await telegram_bot.send_message("Daily risk counters have been reset.")
    except Exception as e:
        logger.error(f"Scheduled daily reset failed: {e}")
    
async def scheduled_cancel_stale_orders():
    try:
        if Settings.MODE != "analysis":
            await order_manager.cancel_stale_orders()
    except Exception as e:
        logger.error(f"Scheduled cancel stale orders failed: {e}")

async def process_webhook_background(payload: TradingViewPayload, event_id: str | None = None):
    """Process TradingView webhook payload after Fast ACK response."""
    async with webhook_background_semaphore:
        try:
            result = await webhook_processor.process(payload)
            logger.info(f"Background webhook processed: {result}")

            if event_id:
                status = str(result.get("status", "unknown"))
                message = str(result.get("message", ""))[:300]
                update_event(event_id, status, message, result)

        except Exception as e:
            logger.error(f"Background webhook processing failed: {e}")
            if event_id:
                update_event(event_id, "failed", str(e)[:300])

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info(f"Starting KStockBot... MODE: {Settings.MODE}, MOCK: {Settings.KIS_IS_MOCK}")
    
    try:
        await telegram_bot.initialize()
    except Exception as e:
        logger.critical(f"Telegram Bot failed to initialize. Error: {e}")
    
    # Schedule tasks
    scheduler.add_job(
        scheduled_token_refresh, 
        CronTrigger(hour="8,12,14", minute="0", timezone=Settings.TIMEZONE),
        id="token_refresh",
        replace_existing=True
    )
    scheduler.add_job(
        scheduled_daily_reset, 
        CronTrigger(hour="8", minute="30", timezone=Settings.TIMEZONE),
        id="daily_reset",
        replace_existing=True
    )
    scheduler.add_job(
        scheduled_cancel_stale_orders, 
        CronTrigger(hour="15", minute="20", timezone=Settings.TIMEZONE),
        id="cancel_stale_orders",
        replace_existing=True
    )
    
    if not scheduler.running:
        scheduler.start()
        logger.info("APScheduler started.")
    
    if telegram_bot.is_running:
        try:
            await telegram_bot.send_message(
                f"🚀 <b>KStockBot started</b>\nMode: {Settings.MODE}\nMock: {Settings.KIS_IS_MOCK}"
            )
        except Exception as e:
            logger.warning(f"Startup Telegram notification failed: {e}")
    
    yield
    
    # Shutdown
    logger.info("Shutting down KStockBot...")
    if scheduler.running:
        scheduler.shutdown()
    try:
        await telegram_bot.stop()
    except Exception as e:
        logger.warning(f"Telegram stop failed: {e}")
    try:
        await kis_client.aclose()
    except Exception as e:
        logger.warning(f"KIS client close failed: {e}")

app = FastAPI(lifespan=lifespan, title="KStockBot API")

@app.get("/")
def read_root():
    return {"status": "ok", "service": "kstockbot"}

@app.get("/health")
def read_health():
    return {
        "status": "ok",
        "mode": Settings.MODE,
        "mock": Settings.KIS_IS_MOCK,
        "telegram_running": telegram_bot.is_running,
        "emergency_stop": risk_manager.emergency_stop_active,
        "scheduler_running": scheduler.running,
        "webhook_secret_configured": bool(Settings.WEBHOOK_SECRET and Settings.WEBHOOK_SECRET != "change-me"),
        "webhook_fast_ack": Settings.WEBHOOK_FAST_ACK,
        "webhook_background_max_concurrency": Settings.WEBHOOK_BACKGROUND_MAX_CONCURRENCY,
        "kis_credentials_configured": bool(Settings.KIS_APP_KEY and Settings.KIS_APP_SECRET and Settings.KIS_ACCOUNT_NO),
    }

@app.get("/status")
def read_status():
    return {
        "status": "ok",
        "mode": Settings.MODE,
        "kis_mock": Settings.KIS_IS_MOCK,
        "emergency_stop": risk_manager.emergency_stop_active,
        "daily_buys": risk_manager.state.get("daily_buys", 0),
        "daily_orders": risk_manager.state.get("daily_orders", 0),
        "daily_order_attempts": risk_manager.state.get("daily_order_attempts", 0),
        "daily_buy_amount": risk_manager.state.get("daily_buy_amount", 0),
        "pending_approvals": len(order_manager._pending_approvals),
        "webhook_secret_configured": bool(Settings.WEBHOOK_SECRET and Settings.WEBHOOK_SECRET != "change-me"),
        "webhook_fast_ack": Settings.WEBHOOK_FAST_ACK,
        "webhook_background_max_concurrency": Settings.WEBHOOK_BACKGROUND_MAX_CONCURRENCY,
        "last_webhook_event": last_event_summary(),
        "telegram_running": telegram_bot.is_running,
        "scheduler_running": scheduler.running,
        "max_scan_symbols": Settings.MAX_SCAN_SYMBOLS,
        "runtime_dir": Settings.RUNTIME_DIR,
        "log_dir": Settings.LOG_DIR,
    }

@app.post("/webhook/tradingview")
async def receive_webhook(payload: TradingViewPayload, response: Response, background_tasks: BackgroundTasks):
    try:
        if Settings.WEBHOOK_FAST_ACK:
            secret_ok, secret_msg = webhook_processor.validate_secret_only(payload)
            if not secret_ok:
                response.status_code = 403
                return {"status": "error", "message": secret_msg}

            event_id = create_event(payload, Settings.MODE, True)
            background_tasks.add_task(process_webhook_background, payload, event_id)
            response.status_code = 202
            return {
                "status": "accepted",
                "message": "Webhook accepted for background processing",
                "mode": Settings.MODE,
                "fast_ack": True,
                "event_id": event_id,
                "symbol": payload.symbol,
                "action": payload.action,
            }

        result = await webhook_processor.process(payload)

        event_id = create_event(payload, Settings.MODE, False)
        update_event(
            event_id,
            str(result.get("status", "unknown")),
            str(result.get("message", ""))[:300],
            result,
        )
        result["event_id"] = event_id
        
        # Adjust HTTP status code based on result
        if result.get("status") == "error":
            message = str(result.get("message", ""))
            if "Unauthorized" in message:
                response.status_code = 403
            elif "disabled in MVP" in message:
                response.status_code = 403
            else:
                response.status_code = 400
        elif result.get("status") == "ignored":
            response.status_code = 202
                
        return result
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")
