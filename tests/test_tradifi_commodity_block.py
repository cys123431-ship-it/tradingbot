import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import emas
from bot_runtime import live_orders
from utbreakout.coinselector import (
    build_base_candidate,
    default_coin_selector_config,
    market_is_blocked_tradifi_commodity,
)


def _market(base, *, underlying_type=None, subtype=None, name=None):
    info = {
        "symbol": f"{base}USDT",
        "contractType": "TRADIFI_PERPETUAL",
        "status": "TRADING",
    }
    if underlying_type is not None:
        info["underlyingType"] = underlying_type
    if subtype is not None:
        info["underlyingSubType"] = subtype
    if name is not None:
        info["name"] = name
    return {
        "symbol": f"{base}/USDT:USDT",
        "id": f"{base}USDT",
        "base": base,
        "quote": "USDT",
        "settle": "USDT",
        "swap": True,
        "active": True,
        "type": "swap",
        "info": info,
    }


def _ticker():
    return {
        "quoteVolume": 500_000_000,
        "percentage": 1.0,
        "count": 100_000,
        "bid": 100.0,
        "ask": 100.01,
    }


class _Exchange:
    def __init__(self, markets):
        self._markets = markets

    def load_markets(self):
        return self._markets


@pytest.mark.parametrize(
    "base,underlying_type,subtype",
    [
        ("NATGAS", "COMMODITY", "Energy"),
        ("CL", "COMMODITY", "Crude Oil"),
        ("XAU", "COMMODITY", "Precious Metals"),
        ("XAG", "COMMODITY", "Precious Metals"),
        ("XPT", "COMMODITY", "Precious Metals"),
        ("XPD", "COMMODITY", "Precious Metals"),
        ("COPPER", "COMMODITY", "Industrial Metals"),
        ("LITHIUM", "COMMODITY", "Minerals"),
    ],
)
def test_coinselector_rejects_blocked_tradifi_commodities(base, underlying_type, subtype):
    market = _market(base, underlying_type=underlying_type, subtype=subtype)
    candidate = build_base_candidate(
        f"{base}/USDT:USDT",
        _ticker(),
        market,
        default_coin_selector_config(),
    )

    assert market_is_blocked_tradifi_commodity(f"{base}/USDT:USDT", market) is True
    assert candidate["accepted"] is False
    assert candidate["tradifi_commodity_blocked"] is True
    assert "REJECTED_TRADIFI_COMMODITY" in candidate["reject_reasons"]


@pytest.mark.parametrize(
    "base,underlying_type",
    [("QQQ", "ETF"), ("EWY", "INDEX"), ("SKHY", "EQUITY"), ("CL", "EQUITY")],
)
def test_equity_etf_and_index_tradifi_remain_allowed(base, underlying_type):
    market = _market(base, underlying_type=underlying_type)
    candidate = build_base_candidate(
        f"{base}/USDT:USDT",
        _ticker(),
        market,
        default_coin_selector_config(),
    )

    assert market_is_blocked_tradifi_commodity(f"{base}/USDT:USDT", market) is False
    assert candidate["accepted"] is True
    assert candidate["tradifi_commodity_blocked"] is False


def test_known_commodity_etf_alias_is_blocked_but_qqq_is_not():
    gld = _market("GLD", underlying_type="ETF", name="SPDR Gold Shares")
    qqq = _market("QQQ", underlying_type="ETF", name="Invesco QQQ")

    assert market_is_blocked_tradifi_commodity("GLD/USDT:USDT", gld) is True
    assert market_is_blocked_tradifi_commodity("QQQ/USDT:USDT", qqq) is False


def test_mainnet_watchlist_and_order_preflight_block_commodity_only():
    markets = {
        "XAU/USDT:USDT": _market("XAU", underlying_type="COMMODITY"),
        "QQQ/USDT:USDT": _market("QQQ", underlying_type="ETF"),
    }
    controller = emas.MainController.__new__(emas.MainController)
    controller.cfg = {"api": {"exchange_mode": emas.BINANCE_MAINNET, "use_testnet": False}}
    controller.exchange = _Exchange(markets)

    with pytest.raises(ValueError, match="REJECTED_TRADIFI_COMMODITY"):
        controller._resolve_futures_watch_symbol_from_markets(
            "XAU/USDT", markets, exchange_mode=emas.BINANCE_MAINNET
        )
    assert controller._resolve_futures_watch_symbol_from_markets(
        "QQQ/USDT", markets, exchange_mode=emas.BINANCE_MAINNET
    ) == "QQQ/USDT:USDT"

    with pytest.raises(ValueError, match="REJECTED_TRADIFI_COMMODITY"):
        asyncio.run(controller._assert_symbol_tradeable_in_current_exchange_mode("XAU/USDT"))
    assert asyncio.run(
        controller._assert_symbol_tradeable_in_current_exchange_mode("QQQ/USDT")
    ) == "QQQ/USDT:USDT"

    assert controller._get_tradifi_symbols_from_markets(markets) == ["QQQ/USDT:USDT"]


def test_final_live_order_gateway_blocks_before_any_order_logic():
    blocker = AsyncMock(side_effect=ValueError("REJECTED_TRADIFI_COMMODITY: XAU"))
    owner = SimpleNamespace(
        ctrl=SimpleNamespace(_assert_symbol_tradeable_in_current_exchange_mode=blocker)
    )
    plan = SimpleNamespace(symbol="XAU/USDT:USDT", side="long")

    with pytest.raises(ValueError, match="REJECTED_TRADIFI_COMMODITY"):
        asyncio.run(live_orders.execute_live_order_plan(owner, plan, {}))
    blocker.assert_awaited_once_with("XAU/USDT:USDT")
