import asyncio
import logging

import emas


class _SettingsExchange:
    def __init__(self):
        self.markets = {}
        self.calls = []

    def load_markets(self):
        self.calls.append(("load_markets",))
        self.markets = {"BTC/USDT:USDT": {"symbol": "BTC/USDT:USDT"}}
        return self.markets

    def set_position_mode(self, hedged, symbol=None):
        assert self.markets
        self.calls.append(("set_position_mode", hedged, symbol))

    def set_margin_mode(self, margin_mode, symbol):
        assert self.markets
        self.calls.append(("set_margin_mode", margin_mode, symbol))

    def set_leverage(self, leverage, symbol):
        assert self.markets
        self.calls.append(("set_leverage", leverage, symbol))


def _engine(exchange=None):
    engine = object.__new__(emas.BaseEngine)
    engine.exchange = exchange or _SettingsExchange()
    engine.cfg = {}
    engine.is_upbit_mode = lambda: False
    return engine


def _signal_engine():
    return object.__new__(emas.SignalEngine)


def test_market_settings_load_markets_before_symbol_scoped_calls():
    exchange = _SettingsExchange()
    engine = _engine(exchange)

    asyncio.run(engine.ensure_market_settings("BTC/USDT:USDT", leverage=5))

    assert exchange.calls == [
        ("load_markets",),
        ("set_position_mode", False, "BTC/USDT:USDT"),
        ("set_margin_mode", "ISOLATED", "BTC/USDT:USDT"),
        ("set_leverage", 5, "BTC/USDT:USDT"),
    ]


def test_poll_position_alias_normalization_is_not_a_warning(caplog):
    engine = _signal_engine()

    with caplog.at_level(logging.WARNING):
        canonical = engine._canonicalize_utbreakout_symbol_for_use(
            "BTC/USDT",
            source="poll_tick_target",
        )

    assert canonical == "BTC/USDT:USDT"
    assert "UTBREAK_SYMBOL_NORMALIZED" not in caplog.text


def test_non_poll_symbol_normalization_remains_visible(caplog):
    engine = _signal_engine()

    with caplog.at_level(logging.WARNING):
        canonical = engine._canonicalize_utbreakout_symbol_for_use(
            "BTC/USDT",
            source="entry_preflight",
        )

    assert canonical == "BTC/USDT:USDT"
    assert "UTBREAK_SYMBOL_NORMALIZED" in caplog.text
