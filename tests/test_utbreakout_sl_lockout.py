import pytest
import os
import time
import json
import asyncio
import logging
from unittest.mock import MagicMock
from emas import SignalEngine

def test_sl_lockout_logic(tmp_path):
    async def run_test():
        # Setup mock bot
        bot = SignalEngine.__new__(SignalEngine)
        
        # Mock logger using standard library
        bot.logger = logging.getLogger("test")
        
        # Mock exchange and trace hooks
        bot.exchange = MagicMock()
        bot._utbreakout_trace_event = MagicMock()
        
        # State and lockouts containers
        bot.utbreakout_trailing_states = {}
        bot.utbreakout_daily_sl_symbol_lockouts = {}
        
        # Define mock behavior for symbol normalization and daily key helpers
        bot._normalize_market_symbol = lambda s: s
        bot._utbreakout_today_key = lambda: "2026-06-29"
        bot._ensure_runtime_state_container = lambda name: bot.utbreakout_daily_sl_symbol_lockouts
        bot._save_utbreakout_daily_sl_lockouts = MagicMock()
        
        # Mock record method to register lockouts properly in container
        def mock_record_lockout(symbol, side=None, reason="UNKNOWN", detail=""):
            bot.utbreakout_daily_sl_symbol_lockouts[symbol] = {
                "date": "2026-06-29",
                "reason": reason,
                "side": side,
                "ts": int(time.time() * 1000),
                "detail": detail
            }
        bot._record_utbreakout_daily_sl_lockout = mock_record_lockout
        
        # Scenario 1: No lockout active initially
        symbol = "BTC/USDT:USDT"
        is_active = await bot._is_sl_lockout_active(symbol)
        assert is_active is False
        
        # Scenario 2: SL check with invalid state or exit price far from SL should not lock out
        state = {
            "active": True,
            "entry_price": 50000.0,
            "risk_distance": 1000.0,
            "side": "long",
            "last_stop_price": 49000.0
        }
        
        # exchange.fetch_order mock utilizing sync function (matching asyncio.to_thread usage)
        def mock_fetch_order_empty(order_id, sym):
            return {}
        bot.exchange.fetch_order = mock_fetch_order_empty
        
        await bot._check_and_record_sl_lockout_async(symbol, state, exit_price=52000.0) # far from SL
        is_active = await bot._is_sl_lockout_active(symbol)
        assert is_active is False
        
        # Scenario 3: SL check when exit_price hits stop loss price closely
        state["sl_order_id"] = "12345"
        
        # exchange.fetch_order mock utilizing sync function
        def mock_fetch_order_filled(order_id, sym):
            return {
                "id": "12345",
                "status": "closed",
                "average": 49001.0,
                "price": 49000.0
            }
        bot.exchange.fetch_order = mock_fetch_order_filled
        
        await bot._check_and_record_sl_lockout_async(symbol, state, exit_price=49000.5)
        is_active = await bot._is_sl_lockout_active(symbol)
        assert is_active is True
        
        # Scenario 4: Simulate lockout expiration (date mismatch)
        bot.utbreakout_daily_sl_symbol_lockouts[symbol] = {
            "date": "2026-06-28", # different day
            "reason": "STOP_LOSS_FILLED",
            "side": "long"
        }
        is_active = await bot._is_sl_lockout_active(symbol)
        assert is_active is False

    asyncio.run(run_test())
