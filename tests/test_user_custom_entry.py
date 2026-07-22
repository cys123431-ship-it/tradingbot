import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import emas
from bot_runtime import live_orders
from bot_runtime.controller_custom_entry import parse_user_custom_entry_text
from bot_runtime.signal_custom_entry import SignalCustomEntryMixin


class _ConfigStub:
    def __init__(self, enabled=True):
        self.config = {
            "signal_engine": {
                "user_custom_entry": {
                    "enabled": enabled,
                    "timeframe": "15m",
                    "atr_period": 14,
                    "stop_atr_multiplier": 1.5,
                    "max_spread_pct": 0.08,
                    "require_quote_volume_gate": True,
                    "require_orderbook_gate": True,
                }
            }
        }

    def get(self, key, default=None):
        return self.config.get(key, default)


class _ExchangeStub:
    def fetch_ticker(self, symbol):
        assert symbol == "KORU/USDT:USDT"
        return {"last": 100.0, "bid": 99.99, "ask": 100.01}


class _ControllerStub:
    def __init__(self, enabled=True):
        self.cfg = _ConfigStub(enabled=enabled)
        self.is_paused = False

    async def _assert_symbol_tradeable_in_current_exchange_mode(self, symbol):
        assert symbol in {"KORUUSDT", "KORU/USDT:USDT"}
        return "KORU/USDT:USDT"


class _CustomEngineStub(SignalCustomEntryMixin):
    def __init__(self, *, enabled=True, daily_loss=False):
        self.ctrl = _ControllerStub(enabled=enabled)
        self.exchange = _ExchangeStub()
        self.market_data_exchange = self.exchange
        self.last_entry_reason = {}
        self.last_live_entry_snapshot = {}
        self.active_symbols = set()
        self.db = MagicMock()
        self.db.get_daily_entry_count.side_effect = AssertionError(
            "custom entries must not consult the strategy trade-count cap"
        )
        self._daily_loss = daily_loss
        self._flat_check = AsyncMock()
        self._validate_mainnet_entry_quote_volume = AsyncMock(
            return_value={
                "allowed": True,
                "status": "PASS",
                "quote_volume": 500_000_000.0,
            }
        )
        self._evaluate_shared_l2_gate = AsyncMock(
            return_value={
                "state": "calm",
                "allowed": True,
                "reason": "depth and spread healthy",
                "risk_multiplier": 1.0,
            }
        )
        self._fetch_live_ohlcv_rows_for_context = AsyncMock(
            return_value=[
                {
                    "timestamp": index * 900_000,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.0,
                    "volume": 1000.0,
                }
                for index in range(50)
            ]
        )

    def is_upbit_mode(self):
        return False

    def _build_user_custom_live_config(self):
        return {
            "live_activation_stage": "PAPER_ONLY",
            "risk_per_trade_pct": 0.5,
            "max_risk_per_trade_pct": 1.0,
            "initial_stop_atr_distance": 1.5,
            "entry_order_type": "MARKET",
            "leverage": 10,
            "timeframe": "15m",
            "adx_length": 14,
            "live_tp1_rr": 1.0,
            "live_tp2_rr": 3.5,
            "live_tp1_pct": 0.25,
            "live_tp2_pct": 0.75,
            "min_notional_usdt": 5.0,
        }

    async def check_daily_loss_limit(self):
        return self._daily_loss

    async def _assert_user_custom_flat_state(self):
        return await self._flat_check()

    async def get_balance_info(self):
        return 100.0, 100.0, 0.0

    def _calculate_live_adx_dmi(self, rows, cfg):
        assert len(rows) >= 16
        return {"atr": 2.0}

    def _get_min_notional_for_symbol(self, symbol, cfg):
        return 5.0


def test_user_custom_parser_requires_an_explicit_entry_phrase():
    parsed = parse_user_custom_entry_text("KORUUSDT 숏 시장가로 바로 진입")
    assert parsed == {
        "symbol": "KORUUSDT",
        "side": "short",
        "immediate": True,
        "order_type": "market",
    }
    assert parse_user_custom_entry_text("BTCUSDT 롱 시장가로 진입")["immediate"] is False
    assert parse_user_custom_entry_text("/customentry ETHUSDT short now")["immediate"] is True
    assert parse_user_custom_entry_text("ETHUSDT short") is None
    assert parse_user_custom_entry_text("ETHUSDT short limit 진입") is None


def test_custom_plan_uses_risk_sizing_sl_tp_and_ignores_trade_count_cap():
    engine = _CustomEngineStub()
    prepared = asyncio.run(engine.prepare_user_custom_entry("KORUUSDT", "short"))

    plan = prepared["plan"]
    assert plan.engine == "USER_CUSTOM"
    assert plan.entry_type == "MARKET"
    assert plan.entry_price == pytest.approx(100.0)
    assert plan.initial_sl_price == pytest.approx(103.0)
    assert plan.qty == pytest.approx((100.0 * 0.005) / 3.0)
    assert len(plan.tp_orders) == 2
    assert plan.tp_orders[0].price == pytest.approx(97.0)
    assert plan.tp_orders[1].price == pytest.approx(89.5)
    assert plan.max_trade_count_bypassed is True
    assert plan.signal_timestamp.startswith("user:")
    engine.db.get_daily_entry_count.assert_not_called()
    engine._flat_check.assert_awaited_once()


def test_each_custom_request_has_a_unique_idempotency_key():
    engine = _CustomEngineStub()
    first = asyncio.run(engine.prepare_user_custom_entry("KORUUSDT", "long"))
    second = asyncio.run(engine.prepare_user_custom_entry("KORUUSDT", "long"))
    assert first["plan"].signal_timestamp != second["plan"].signal_timestamp


def test_custom_plan_still_blocks_on_daily_loss_limit():
    engine = _CustomEngineStub(daily_loss=True)
    with pytest.raises(emas.TradingSafetyError, match="daily loss limit"):
        asyncio.run(engine.prepare_user_custom_entry("KORUUSDT", "long"))
    engine._flat_check.assert_not_awaited()


def test_custom_mode_off_blocks_before_any_market_check():
    engine = _CustomEngineStub(enabled=False)
    with pytest.raises(emas.TradingSafetyError, match="USER_CUSTOM_MODE_OFF"):
        asyncio.run(engine.prepare_user_custom_entry("KORUUSDT", "long"))
    engine._flat_check.assert_not_awaited()


def test_custom_flat_guard_rejects_any_existing_global_position():
    engine = _CustomEngineStub()
    engine.exchange = SimpleNamespace(
        fetch_positions=lambda: [
            {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0.1}
        ],
        fetch_open_orders=lambda: [],
    )
    engine._position_signed_contracts = lambda position: position["contracts"]
    with pytest.raises(emas.TradingSafetyError, match="single-position"):
        asyncio.run(SignalCustomEntryMixin._assert_user_custom_flat_state(engine))


def test_custom_plan_reduces_size_when_orderbook_risk_is_mixed():
    engine = _CustomEngineStub()
    engine._evaluate_shared_l2_gate.return_value.update(
        {"state": "deep_balanced", "risk_multiplier": 0.5}
    )
    prepared = asyncio.run(engine.prepare_user_custom_entry("KORUUSDT", "long"))
    assert prepared["plan"].qty == pytest.approx(((100.0 * 0.005) / 3.0) * 0.5)
    assert prepared["plan"].l2_risk_multiplier == pytest.approx(0.5)


def test_custom_execution_records_strategy_for_monthly_report():
    engine = _CustomEngineStub()
    prepared = asyncio.run(engine.prepare_user_custom_entry("KORUUSDT", "long"))
    engine.prepare_user_custom_entry = AsyncMock(return_value=prepared)
    engine.execute_live_order_plan = AsyncMock(
        return_value={
            "status": "LIVE_ORDER_PLAN_EXECUTED",
            "plan": prepared["plan"],
            "tp_orders": [{"id": "tp1"}, {"id": "tp2"}],
        }
    )

    result = asyncio.run(engine.execute_user_custom_entry("KORUUSDT", "long"))

    assert result["status"] == "LIVE_ORDER_PLAN_EXECUTED"
    engine.db.log_trade_entry.assert_called_once()
    assert engine.db.log_trade_entry.call_args.kwargs["strategy"] == "user_custom"
    assert "KORU/USDT:USDT" in engine.active_symbols


def test_live_plan_preserves_configured_stop_atr_distance_before_fill():
    source = __import__("inspect").getsource(live_orders.execute_live_order_plan)
    assert 'cfg.get("initial_stop_atr_distance", 2.0)' in source
    assert "/ stop_atr_distance" in source


def test_automatic_entry_returns_before_strategy_work_when_custom_mode_is_on():
    class EntryStub(emas.SignalEntryMixin):
        last_entry_reason = {}

        @staticmethod
        def is_user_custom_entry_mode_enabled():
            return True

    engine = EntryStub()
    assert asyncio.run(engine.entry("BTC/USDT:USDT", "long", 100.0)) is None
    assert "USER_CUSTOM_MODE_ACTIVE" in engine.last_entry_reason["BTC/USDT:USDT"]


def test_shared_order_gateway_late_gate_blocks_non_custom_strategy():
    owner = SimpleNamespace(is_user_custom_entry_mode_enabled=lambda: True)
    outcome = asyncio.run(
        emas._submit_idempotent_crypto_entry(
            owner,
            "BTC/USDT:USDT",
            "long",
            0.01,
            "UTBREAK",
            {},
        )
    )
    assert outcome.submission is None
    assert "USER_CUSTOM_MODE_ACTIVE" in outcome.entry_block_reason


def test_custom_mode_default_is_persisted_off(tmp_path):
    cfg = emas.TradingConfig(str(tmp_path / "config.json"))
    custom = cfg.get("signal_engine", {})["user_custom_entry"]
    assert custom["enabled"] is False
    assert custom["require_quote_volume_gate"] is True
    assert custom["require_orderbook_gate"] is True
