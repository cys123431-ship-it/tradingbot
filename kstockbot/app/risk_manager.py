import json
import os
import datetime
from .settings import Settings
from .logger import get_logger
from .storage import state_storage
from .market_calendar import now_kst, can_buy_now

logger = get_logger("risk_manager")

RISK_FILE = os.path.join(Settings.CONFIG_DIR, "risk.json")
EXAMPLE_RISK_FILE = os.path.join(Settings.CONFIG_DIR, "risk.example.json")

class RiskManager:
    def __init__(self):
        self.config = self._load_risk_config()
        self._load_state()

    def _load_risk_config(self) -> dict:
        default_config = {
            "max_daily_buy_amount": 1000000,
            "max_order_amount": 500000,
            "max_position_amount_per_symbol": 500000,
            "max_positions": 5,
            "max_orders_per_day": 3,
            "max_orders_per_symbol_per_day": 1,
            "cooldown_minutes_per_symbol": 60,
            "no_buy_after_time": "15:10",
            "no_buy_before_time": "09:10",
            "daily_loss_limit": 50000,
            "monthly_loss_limit": 200000,
            "allow_market_order": False,
            "allow_live_trading": False,
            "manual_approval_required": True,
            "allow_sell_when_emergency_stop": False,
            "default_order_amount": 100000,
            "max_order_attempts_per_day": 10
        }
        
        # Ensure example file is up to date with defaults
        os.makedirs(Settings.CONFIG_DIR, exist_ok=True)
        try:
            example_data = default_config.copy()
            if os.path.exists(EXAMPLE_RISK_FILE):
                with open(EXAMPLE_RISK_FILE, "r", encoding="utf-8") as f:
                    example_data = json.load(f)
                    
            changed = False
            for k, v in default_config.items():
                if k not in example_data:
                    example_data[k] = v
                    changed = True
                    
            if changed or not os.path.exists(EXAMPLE_RISK_FILE):
                with open(EXAMPLE_RISK_FILE, "w", encoding="utf-8") as f:
                    json.dump(example_data, f, indent=4)
        except Exception as e:
            logger.warning(f"Failed to update risk.example.json: {e}")
                
        target_file = RISK_FILE if os.path.exists(RISK_FILE) else EXAMPLE_RISK_FILE
        try:
            with open(target_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                for k, v in default_config.items():
                    if k not in loaded:
                        loaded[k] = v
                return loaded
        except Exception as e:
            logger.error(f"Failed to load risk config, using defaults: {e}")
            return default_config

    def _load_state(self):
        state = state_storage.load()
        today = now_kst().strftime("%Y-%m-%d")
        
        # Reset if new day
        saved_date = state.get("current_date")
        if saved_date != today:
            state["current_date"] = today
            state["daily_orders"] = 0
            state["daily_order_attempts"] = 0
            state["daily_buys"] = 0
            state["daily_buy_amount"] = 0
            state["symbol_orders_today"] = {}
            # Keep emergency stop state persistent across days unless manually cleared
            state_storage.save(state)
            
        self.state = state

    def _save_state(self):
        state_storage.save(self.state)

    @property
    def emergency_stop_active(self):
        return self.state.get("emergency_stop_active", False)

    @emergency_stop_active.setter
    def emergency_stop_active(self, value: bool):
        self.state["emergency_stop_active"] = value
        self._save_state()

    def can_trade_now(self, action: str = "BUY") -> tuple[bool, str]:
        if self.emergency_stop_active:
            if action == "SELL" and self.config.get("allow_sell_when_emergency_stop"):
                logger.info("Emergency stop active but SELL allowed by config")
            else:
                return False, "EMERGENCY STOP is active"
                
        if Settings.MODE == "analysis":
            return False, "Mode is analysis. Trading disabled."
            
        if Settings.MODE == "live":
            if not self.config.get("allow_live_trading", False):
                return False, "Live trading disabled in risk config"
            if Settings.CONFIRM_LIVE_TRADING != "yes-i-understand-real-money-risk":
                return False, "Live trading requires explicit CONFIRM_LIVE_TRADING string"
            if Settings.KIS_IS_MOCK:
                return False, "Live mode cannot be used with MOCK=true"
            
        return True, "Trading OK"

    def validate_signal(self, signal: dict) -> tuple[bool, str]:
        action = signal.get("action", "").upper()
        can_trade, msg = self.can_trade_now(action)
        if not can_trade: return False, msg
        
        if action == "BUY":
            is_open, m_msg = can_buy_now(
                self.config["no_buy_before_time"], 
                self.config["no_buy_after_time"]
            )
            if not is_open: return False, m_msg
            
        return True, "Signal OK"

    def can_place_buy(self, symbol: str, amount: int) -> tuple[bool, str]:
        can_trade, msg = self.can_trade_now("BUY")
        if not can_trade: return False, msg
        
        is_open, m_msg = can_buy_now(
            self.config["no_buy_before_time"], 
            self.config["no_buy_after_time"]
        )
        if not is_open: return False, m_msg
        
        if self.state.get("daily_order_attempts", 0) >= self.config["max_order_attempts_per_day"]:
            return False, "Max daily order attempts reached"
            
        if self.state.get("daily_orders", 0) >= self.config["max_orders_per_day"]:
            return False, "Max daily orders reached"
            
        symbol_orders = self.state.get("symbol_orders_today", {}).get(symbol, 0)
        if symbol_orders >= self.config["max_orders_per_symbol_per_day"]:
            return False, "Max orders per symbol per day reached"
            
        if amount > self.config["max_order_amount"]:
            return False, f"Exceeds max order amount ({amount} > {self.config['max_order_amount']})"
            
        daily_amount = self.state.get("daily_buy_amount", 0)
        if daily_amount + amount > self.config["max_daily_buy_amount"]:
            return False, f"Exceeds max daily buy amount ({daily_amount + amount} > {self.config['max_daily_buy_amount']})"
            
        last_time_str = self.state.get("last_order_time_per_symbol", {}).get(symbol)
        if last_time_str:
            try:
                last_time = datetime.datetime.fromisoformat(last_time_str)
                now = now_kst()
                diff_mins = (now - last_time).total_seconds() / 60
                if diff_mins < self.config["cooldown_minutes_per_symbol"]:
                    return False, f"Cooldown active for {symbol}"
            except Exception as e:
                logger.warning(f"Failed to parse last order time: {e}")
                
        return True, "Buy OK"

    def can_place_sell(self, symbol: str, qty: int) -> tuple[bool, str]:
        can_trade, msg = self.can_trade_now("SELL")
        if not can_trade:
            return False, msg

        is_open, m_msg = can_buy_now(
            self.config["no_buy_before_time"],
            self.config["no_buy_after_time"]
        )
        if not is_open:
            return False, f"Sell blocked outside regular MVP trading window: {m_msg}"

        if qty <= 0:
            return False, "Sell quantity must be > 0"

        return True, "Sell OK"

    def validate_order(self, order: dict, portfolio: dict) -> tuple[bool, str]:
        if order.get("order_type") == "market":
            logger.warning("Market orders are disabled for MVP.")
            return False, "Market orders are forbidden by risk config (MVP constraint)"
            
        side = order.get("side", "").upper()
        symbol = order.get("symbol")
        qty = order.get("qty", 0)
        price = order.get("price", 0)
        amount = qty * price
        
        if side == "BUY":
            return self.can_place_buy(symbol, amount)
        elif side == "SELL":
            return self.can_place_sell(symbol, qty)
        return False, f"Unknown side: {side}"

    def record_order_attempt(self, order: dict) -> None:
        symbol = order.get("symbol")
        side = order.get("side", "").upper()
        
        self.state["daily_order_attempts"] = self.state.get("daily_order_attempts", 0) + 1
        
        last_times = self.state.get("last_order_attempt_time_per_symbol", {})
        last_times[symbol] = now_kst().isoformat()
        self.state["last_order_attempt_time_per_symbol"] = last_times
        
        self._save_state()

    def record_order_success(self, order: dict) -> None:
        symbol = order.get("symbol")
        side = order.get("side", "").upper()
        
        self.state["daily_orders"] = self.state.get("daily_orders", 0) + 1
        
        if side == "BUY":
            self.state["daily_buys"] = self.state.get("daily_buys", 0) + 1
            amount = order.get("qty", 0) * order.get("price", 0)
            self.state["daily_buy_amount"] = self.state.get("daily_buy_amount", 0) + amount
            
        symbol_orders_today = self.state.get("symbol_orders_today", {})
        symbol_orders_today[symbol] = symbol_orders_today.get(symbol, 0) + 1
        self.state["symbol_orders_today"] = symbol_orders_today
        
        last_times = self.state.get("last_order_time_per_symbol", {})
        last_times[symbol] = now_kst().isoformat()
        self.state["last_order_time_per_symbol"] = last_times
        
        self._save_state()

    def emergency_stop(self, reason: str) -> None:
        self.state["emergency_stop_active"] = True
        self.state["emergency_stop_reason"] = reason
        self.state["emergency_stop_time"] = now_kst().isoformat()
        self._save_state()
        logger.critical(f"EMERGENCY STOP TRIGGERED: {reason}")
        
    def resume_trading(self) -> None:
        self.state["emergency_stop_active"] = False
        self.state["resume_trading_time"] = now_kst().isoformat()
        self._save_state()
        logger.info("Trading resumed from emergency stop.")
        
    def reset_daily_counters(self) -> None:
        self.state["current_date"] = now_kst().strftime("%Y-%m-%d")
        self.state["daily_orders"] = 0
        self.state["daily_order_attempts"] = 0
        self.state["daily_buys"] = 0
        self.state["daily_buy_amount"] = 0
        self.state["symbol_orders_today"] = {}
        self._save_state()
        logger.info("Daily risk counters reset.")

risk_manager = RiskManager()
