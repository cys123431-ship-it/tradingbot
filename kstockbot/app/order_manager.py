import datetime
import uuid
from typing import Optional
from .settings import Settings
from .logger import get_logger
from .kis_client import kis_client
from .risk_manager import risk_manager
from .storage import approvals_storage, orders_storage
from .market_calendar import now_kst
from html import escape

logger = get_logger("order_manager")

class OrderManager:
    def __init__(self):
        self._pending_approvals = approvals_storage.load()
        if not isinstance(self._pending_approvals, dict):
            self._pending_approvals = {}

    def _save_approvals(self):
        approvals_storage.save(self._pending_approvals)

    def _save_to_ledger(self, order: dict):
        history = orders_storage.load()
        if not isinstance(history, list):
            history = []
        history.append(order)
        orders_storage.save(history)

    def normalize_limit_price(self, price: int | float) -> int:
        """Normalize limit price for Korean stock orders.

        MVP behavior:
        - Convert to integer KRW.
        - Reject non-positive prices.
        - Keep tick-size logic simple for now.
        - Future step: replace with official KRX/KIS tick-size table.
        """
        try:
            normalized = int(round(float(price)))
        except Exception:
            raise ValueError(f"Invalid price: {price}")

        if normalized <= 0:
            raise ValueError("Price must be > 0")

        return normalized

    def build_limit_order(self, side: str, symbol: str, ref_price: int | float, amount: int) -> dict:
        if not symbol.isdigit() or len(symbol) != 6:
            raise ValueError("Symbol must be a 6-digit string")
            
        price = self.normalize_limit_price(ref_price)
        qty = amount // price
        if qty <= 0:
            raise ValueError("Calculated quantity is 0")
            
        return {
            "order_id": str(uuid.uuid4()),
            "side": side.upper(),
            "symbol": symbol,
            "qty": qty,
            "price": price,
            "order_type": "limit",
            "timestamp": now_kst().isoformat()
        }

    async def process_order_request(self, side: str, symbol: str, ref_price: int | float, amount: int) -> tuple[bool, str]:
        side = side.upper()
        if side == "CLOSE":
            return False, "CLOSE is not supported until position-based close is implemented"
            
        try:
            order = self.build_limit_order(side, symbol, ref_price, amount)
        except ValueError as e:
            logger.warning(f"Failed to build order: {e}")
            return False, f"Order build failed: {e}"

        mode = Settings.MODE

        # MVP safety guard: live auto trading is blocked before any risk/order path.
        if mode == "live":
            logger.critical("LIVE auto order rejected in MVP from OrderManager.process_order_request")
            order["status"] = "live_auto_rejected_mvp"
            order["reject_reason"] = "LIVE auto trading is disabled in MVP. Use manual_live with Telegram approval."
            self._save_to_ledger(order)
            return False, "LIVE auto trading is disabled in MVP. Use manual_live with Telegram approval."

        # Validate via RiskManager after the live MVP guard.
        is_valid, msg = risk_manager.validate_order(order, {})
        if not is_valid:
            logger.warning(f"Order rejected by RiskManager: {msg}")
            return False, f"Risk rejection: {msg}"

        if mode == "paper":
            res = await self.submit_paper_order(order)
            if res.get("status") in ["submitted", "paper_recorded_internal_only"]:
                return True, f"Paper order submitted."
            else:
                return False, f"Paper order failed: {res.get('kis_error', 'Unknown Error')}"
            
        elif mode == "manual_live":
            approval_id = await self.request_manual_approval(order)
            return True, f"Requested manual approval. ID: {approval_id}"
            
        else: # analysis or unknown
            return False, "Mode is analysis, skipping order"

    async def submit_paper_order(self, order: dict) -> dict:
        logger.info(f"Submitting PAPER order: {order}")
        risk_manager.record_order_attempt(order)
        
        if Settings.KIS_IS_MOCK:
            try:
                res = await kis_client.place_order(
                    side=order["side"], 
                    symbol=order["symbol"], 
                    qty=order["qty"], 
                    price=order["price"]
                )
                order["kis_response"] = res
                order["status"] = "submitted"
                logger.info("KIS Mock order success.")
                risk_manager.record_order_success(order)
            except Exception as e:
                logger.error(f"KIS Mock order failed: {e}")
                order["kis_error"] = str(e)
                order["status"] = "failed"
        else:
            logger.info("KIS_IS_MOCK is false. Using internal paper ledger only.")
            order["status"] = "paper_recorded_internal_only"
            risk_manager.record_order_success(order)
            
        self._save_to_ledger(order)
        return order

    async def submit_live_order(self, order: dict) -> dict:
        logger.info(f"Submitting LIVE order: {order}")
        
        if Settings.MODE not in ["live", "manual_live"]:
            raise RuntimeError("Live order attempted in non-live mode")
        if Settings.KIS_IS_MOCK:
            raise RuntimeError("Live order attempted with MOCK=true")
        if not risk_manager.config.get("allow_live_trading", False):
            raise RuntimeError("Live trading disabled in config")
        if Settings.CONFIRM_LIVE_TRADING != "yes-i-understand-real-money-risk":
            raise RuntimeError("Live trading not explicitly confirmed")
            
        risk_manager.record_order_attempt(order)
        try:
            res = await kis_client.place_order(
                side=order["side"], 
                symbol=order["symbol"], 
                qty=order["qty"], 
                price=order["price"]
            )
            order["kis_response"] = res
            order["status"] = "submitted"
            logger.info("KIS Live order success.")
            risk_manager.record_order_success(order)
        except Exception as e:
            logger.error(f"KIS Live order failed: {e}")
            order["kis_error"] = str(e)
            order["status"] = "failed"
            self._save_to_ledger(order)
            raise
            
        self._save_to_ledger(order)
        return order

    async def request_manual_approval(self, order: dict) -> str:
        approval_id = order["order_id"][:8]
        order["request_time"] = now_kst().isoformat()
        order["status"] = "pending_approval"
        self._pending_approvals[approval_id] = order
        self._save_approvals()
        logger.info(f"Order pending approval: {approval_id}")
        
        from .telegram_bot import telegram_bot
        
        safe_id = escape(str(approval_id))
        safe_side = escape(str(order["side"]))
        safe_symbol = escape(str(order["symbol"]))
        
        await telegram_bot.send_message(
            f"⚠️ <b>Manual Approval Required</b>\n"
            f"ID: <code>{safe_id}</code>\n"
            f"Side: {safe_side} | Symbol: {safe_symbol}\n"
            f"Qty: {order['qty']} | Price: {order['price']}\n\n"
            f"Reply <code>/approve {safe_id}</code> or <code>/reject {safe_id}</code>"
        )
        return approval_id

    async def submit_live_order_after_approval(self, approval_id: str) -> dict:
        order = self._pending_approvals.get(approval_id)
        if not order:
            raise ValueError("Approval ID not found")
            
        req_time = datetime.datetime.fromisoformat(order.get("request_time", "2000-01-01T00:00:00+09:00"))
        if (now_kst() - req_time).total_seconds() > 600:
            logger.warning(f"Approval {approval_id} expired (>10 mins)")
            order["status"] = "approval_expired"
            self._save_to_ledger(order)
            self._pending_approvals.pop(approval_id, None)
            self._save_approvals()
            return {"error": "Approval expired"}
        
        # Re-validate risk
        is_valid, msg = risk_manager.validate_order(order, {})
        if not is_valid:
            logger.warning(f"Approved order rejected by RiskManager: {msg}")
            order["status"] = "approval_rejected_by_risk"
            order["reject_reason"] = msg
            self._save_to_ledger(order)
            self._pending_approvals.pop(approval_id, None)
            self._save_approvals()
            return {"error": f"Risk rejection: {msg}"}

        submit_attempts = int(order.get("submit_attempts", 0))
        if submit_attempts >= 3:
            order["status"] = "approval_submit_attempts_exceeded"
            order["reject_reason"] = "Maximum approval submit attempts exceeded"
            self._save_to_ledger(order)
            self._pending_approvals.pop(approval_id, None)
            self._save_approvals()
            return {"error": "Maximum approval submit attempts exceeded"}

        order["submit_attempts"] = submit_attempts + 1
        order["last_submit_attempt_time"] = now_kst().isoformat()
        self._save_approvals()
            
        try:
            res = await self.submit_live_order(order)
            self._pending_approvals.pop(approval_id, None)
            self._save_approvals()
            return res
        except Exception as e:
            logger.error(f"Approved live order submission failed for {approval_id}: {e}")
            order["status"] = "approval_submit_failed"
            order["submit_error"] = str(e)
            order["last_submit_failure_time"] = now_kst().isoformat()
            self._save_to_ledger(order)
            self._save_approvals()
            return {"error": f"Live order submission failed: {e}"}

    def reject_order(self, approval_id: str) -> tuple[bool, str]:
        if approval_id not in self._pending_approvals:
            logger.warning(f"Reject requested for unknown approval id: {approval_id}")
            return False, "Approval ID not found"

        order = self._pending_approvals.pop(approval_id)
        order["status"] = "manual_rejected"
        order["manual_rejected_time"] = now_kst().isoformat()
        self._save_to_ledger(order)
        self._save_approvals()
        logger.info(f"Order {approval_id} manually rejected.")
        return True, "Order manually rejected"

    async def cancel_stale_orders(self) -> list:
        # In MVP, we only cancel if explicitly called, and we don't blind cancel.
        # This is a stub for now.
        logger.warning("Cancel stale orders requested, but full time-based cancel is not implemented yet.")
        return []

order_manager = OrderManager()
