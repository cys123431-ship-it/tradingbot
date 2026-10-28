"""3/200 SMA: leverage (2~5x) choice and the mainnet-only TradFi scope."""
import asyncio
from datetime import datetime, timezone

import pytest

from btc_ma_cross.config import LEVERAGE_CHOICES, normalize_btc_ma_cross_config
from btc_ma_cross.service import (
    TRADFI_MAINNET_ONLY_REASON,
    TRADFI_SESSION_CLOSED_REASON,
    market_in_tradfi_scope,
    market_is_tradfi_gold,
    market_is_tradfi_us_equity,
)
from tests.test_btc_ma_cross import _callbacks, _controller
from tests.test_btc_ma_cross_profit_lock_switch import _labeled

# 2026-10-28 is a Wednesday; 13:30 UTC = 09:30 New York (regular session open).
SESSION_OPEN_MS = int(datetime(2026, 10, 28, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)
SESSION_CLOSED_MS = int(datetime(2026, 10, 28, 3, 0, tzinfo=timezone.utc).timestamp() * 1000)
WEEKEND_MS = int(datetime(2026, 10, 31, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)


def tradfi_market(market_id, underlying_type="STOCK", status="TRADING", base=None):
    """A USDⓈ-M TradFi perpetual market as ccxt/Binance reports it."""
    base = base or market_id[:-4]
    return {
        "id": market_id,
        "symbol": f"{base}/USDT:USDT",
        "base": base,
        "quote": "USDT",
        "settle": "USDT",
        "swap": True,
        "linear": True,
        "active": True,
        "type": "swap",
        "info": {"symbol": market_id, "contractType": "TRADIFI_PERPETUAL",
                 "underlyingType": underlying_type, "status": status},
    }


def crypto_market(market_id):
    base = market_id[:-4]
    return {
        "id": market_id, "symbol": f"{base}/USDT:USDT", "base": base,
        "quote": "USDT", "settle": "USDT", "swap": True, "linear": True,
        "active": True, "type": "swap",
        "info": {"symbol": market_id, "contractType": "PERPETUAL", "status": "TRADING"},
    }


# ------------------------------------------------------------------ leverage
def test_leverage_choices_are_two_to_five():
    assert LEVERAGE_CHOICES == (2, 3, 4, 5)
    for lev in LEVERAGE_CHOICES:
        assert normalize_btc_ma_cross_config({"leverage": lev})["leverage"] == lev
    for bad in (0, 1, 6, 10, 20, -3, "abc", None):
        assert normalize_btc_ma_cross_config({"leverage": bad})["leverage"] == 5


def test_leverage_button_saves_and_applies_from_the_next_entry(tmp_path):
    controller = _controller(tmp_path)
    text, markup = asyncio.run(controller._handle_btc_ma_cross_action("status"))
    assert all(f"bm:lev:{lev}" in _callbacks(markup) for lev in LEVERAGE_CHOICES)
    text, markup = asyncio.run(controller._handle_btc_ma_cross_action("lev:3"))
    assert controller.cfg.data["btc_ma_cross"]["leverage"] == 3
    assert "레버리지: 3x" in text
    assert "다음 진입부터 적용" in text
    # Invalid value is ignored, the stored choice stays.
    asyncio.run(controller._handle_btc_ma_cross_action("lev:20"))
    assert controller.cfg.data["btc_ma_cross"]["leverage"] == 3


def test_leverage_reaches_the_entry_sizing(tmp_path):
    from tests.test_btc_ma_cross import Harness

    h = Harness(tmp_path, leverage=2)
    assert h.run()["action"] == "entered"
    assert h.fake.leverage == 2
    assert h.state()["trade"]["leverage"] == 2


# ------------------------------------------------------- TradFi classification
def test_tradfi_scope_takes_us_equities_and_gold_only():
    # US equities are in scope.
    assert market_in_tradfi_scope("AAPL/USDT:USDT", tradfi_market("AAPLUSDT")) is True
    assert market_is_tradfi_us_equity("KORU/USDT:USDT", tradfi_market("KORUUSDT")) is True
    # Gold is in scope even though the main engine blocks it as a commodity.
    gold = tradfi_market("XAUUSDT", underlying_type="COMMODITY", base="XAU")
    assert market_is_tradfi_gold("XAU/USDT:USDT", gold) is True
    assert market_in_tradfi_scope("XAU/USDT:USDT", gold) is True
    gold_by_name = tradfi_market("GOLDUSDT", underlying_type="METAL")
    assert market_is_tradfi_gold("GOLD/USDT:USDT", gold_by_name) is True
    # ...but other commodities are not.
    for symbol, market in (
        ("XAG/USDT:USDT", tradfi_market("XAGUSDT", underlying_type="METAL", base="XAG")),
        ("WTI/USDT:USDT", tradfi_market("WTIUSDT", underlying_type="ENERGY", base="WTI")),
        ("NATGAS/USDT:USDT", tradfi_market("NATGASUSDT", underlying_type="ENERGY", base="NATGAS")),
    ):
        assert market_in_tradfi_scope(symbol, market) is False
    # Crypto perps are never in the TradFi scope.
    assert market_in_tradfi_scope("BTC/USDT:USDT", crypto_market("BTCUSDT")) is False


def _harness_at(tmp_path, now_ms, *, network, **cfg):
    """Harness whose clock is pinned to a specific instant."""
    from tests.test_btc_ma_cross import Harness

    h = Harness(tmp_path, network=network, scan_scope="tradfi", **cfg)
    h.now_ms = now_ms
    return h


# ------------------------------------------------------------ TradFi guard
def test_tradfi_scope_is_mainnet_only(tmp_path):
    testnet = _harness_at(tmp_path, SESSION_OPEN_MS, network="testnet")
    assert testnet.service.tradifi_scope_state("testnet")["reason"] == TRADFI_MAINNET_ONLY_REASON
    assert testnet.service.tradfi_entry_block("testnet", testnet.service.config()) == TRADFI_MAINNET_ONLY_REASON
    # Even during the US session a testnet TradFi entry is refused before any scan.
    result = testnet.run()
    assert result["action"] == "blocked" and result["reason"] == TRADFI_MAINNET_ONLY_REASON
    assert testnet.fake.orders == []


def test_tradfi_entries_only_open_during_the_us_regular_session(tmp_path):
    closed = _harness_at(tmp_path, SESSION_CLOSED_MS, network="mainnet")
    assert closed.service.us_session_status()["open"] is False
    assert closed.service.tradfi_entry_block("mainnet", closed.service.config()) == TRADFI_SESSION_CLOSED_REASON
    result = closed.run()
    assert result["action"] == "waiting" and result["reason"] == TRADFI_SESSION_CLOSED_REASON
    assert closed.fake.orders == []

    weekend = _harness_at(tmp_path, WEEKEND_MS, network="mainnet")
    assert weekend.service.us_session_status()["open"] is False
    assert weekend.run()["reason"] == TRADFI_SESSION_CLOSED_REASON

    opened = _harness_at(tmp_path, SESSION_OPEN_MS, network="mainnet")
    assert opened.service.us_session_status()["open"] is True
    assert opened.service.tradfi_entry_block("mainnet", opened.service.config()) == ""


def test_tradfi_universe_keeps_only_us_equities_and_gold():
    import asyncio as _asyncio

    from tests.test_btc_ma_cross_scope import MultiFake

    coins = {
        "AAPLUSDT": {"volume": 900, "rows": [], "mark": 200.0, "contract_type": "TRADIFI_PERPETUAL"},
        "KORUUSDT": {"volume": 800, "rows": [], "mark": 10.0, "contract_type": "TRADIFI_PERPETUAL"},
        "XAUUSDT": {"volume": 700, "rows": [], "mark": 2400.0, "contract_type": "TRADIFI_PERPETUAL"},
        "BTCUSDT": {"volume": 5000, "rows": [], "mark": 85_000.0},
    }
    fake = MultiFake(coins)
    # Mark the metadata the exchange uses to classify TradFi contracts.
    fake.markets["AAPL/USDT:USDT"]["info"]["underlyingType"] = "STOCK"
    fake.markets["KORU/USDT:USDT"]["info"]["underlyingType"] = "STOCK"
    fake.markets["XAU/USDT:USDT"]["info"]["underlyingType"] = "COMMODITY"
    fake.markets["BTC/USDT:USDT"]["info"]["contractType"] = "PERPETUAL"
    fake.markets["XAU/USDT:USDT"]["info"]["underlyingSubType"] = "GOLD"

    from btc_ma_cross.service import BtcMaCrossService

    async def no_sleep(_):
        return None

    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cfg = {"enabled": True, "trading_mode": "LIVE", "scan_scope": "tradfi"}
        service = BtcMaCrossService(
            config_getter=lambda: cfg,
            credentials_getter=lambda n: {"api_key": "k", "secret_key": "s"},
            exchange_factory=lambda n, c: fake,
            runtime_dir=Path(tmp), notifier=lambda t: None,
            main_network_getter=lambda: "mainnet",
            clock=lambda: SESSION_OPEN_MS / 1000, sleep=no_sleep, environ={},
        )
        universe = _asyncio.run(service._tradfi_universe("mainnet", service.config()))
    ids = [item[1] for item in universe]
    assert set(ids) == {"AAPLUSDT", "KORUUSDT", "XAUUSDT"}
    assert "BTCUSDT" not in ids
    assert ids[0] == "AAPLUSDT"          # 24h volume order is kept


# ------------------------------------------------------------ Telegram scope
def test_tradfi_button_is_refused_on_testnet(tmp_path):
    controller = _controller(tmp_path)                     # harness network = testnet
    text, markup = asyncio.run(controller._handle_btc_ma_cross_action("scope:tradfi"))
    assert "메인넷에서만" in text
    assert controller.cfg.data["btc_ma_cross"].get("scan_scope") != "tradfi"
    assert any(data == "bm:scope:tradfi" for _, data in _labeled(markup))


def test_tradfi_button_selects_the_scope_on_mainnet(tmp_path):
    controller = _controller(tmp_path)
    controller.btc_ma_cross_service.main_network_getter = lambda: "mainnet"
    text, markup = asyncio.run(controller._handle_btc_ma_cross_action("scope:tradfi"))
    assert controller.cfg.data["btc_ma_cross"]["scan_scope"] == "tradfi"
    assert "TradFi만 선택" in text
    assert "미국 정규장" in text
    assert "🏦 TradFi" in asyncio.run(controller._format_btc_ma_cross_status())


def test_testnet_switch_turns_tradfi_into_alts_scope():
    """메인넷 TradFi 사용 중 테스트넷으로 전환하면 '알트 전용'으로 바뀐다."""
    from bot_runtime.controller_exchange import ControllerExchangeMixin

    class Cfg:
        def __init__(self, data):
            self.data = data

        def get(self, key, default=None):
            return self.data.get(key, default)

        async def update_value(self, path, value):
            node = self.data
            for key in path[:-1]:
                node = node.setdefault(key, {})
            node[path[-1]] = value

    class Controller(ControllerExchangeMixin):
        pass

    notes = []

    controller = Controller()
    controller.cfg = Cfg({"btc_ma_cross": {"scan_scope": "tradfi"},
                          "signal_engine": {"coin_selector": {"scan_scope": "tradfi_only"}}})

    async def notify_plain(text, event_type=None):
        notes.append(text)

    controller.notify_plain = notify_plain
    result = asyncio.run(controller._repair_unsupported_coin_selector_scope("testnet", persist=True))
    assert result["btc_ma_cross"]["changed"] is True
    assert result["btc_ma_cross"]["previous_scope"] == "tradfi"
    assert controller.cfg.data["btc_ma_cross"]["scan_scope"] == "alts"
    assert any("알트 전용" in note for note in notes)
    # Mainnet keeps the user's choice untouched.
    controller.cfg.data["btc_ma_cross"]["scan_scope"] = "tradfi"
    asyncio.run(controller._repair_unsupported_coin_selector_scope("mainnet", persist=True))
    assert controller.cfg.data["btc_ma_cross"]["scan_scope"] == "tradfi"


def test_alts_scope_still_selectable_after_the_auto_switch(tmp_path):
    """자동으로 알트 전용이 된 뒤에도 버튼으로 자유롭게 바꿀 수 있다."""
    controller = _controller(tmp_path)
    controller.cfg.data["btc_ma_cross"]["scan_scope"] = "alts"
    asyncio.run(controller._handle_btc_ma_cross_action("scope:btc"))
    assert controller.cfg.data["btc_ma_cross"]["scan_scope"] == "btc"
    asyncio.run(controller._handle_btc_ma_cross_action("scope:alts"))
    assert controller.cfg.data["btc_ma_cross"]["scan_scope"] == "alts"
