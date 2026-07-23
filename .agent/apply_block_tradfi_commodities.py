from __future__ import annotations

from pathlib import Path


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"{label}: target block not found in {path}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


coinselector = Path("utbreakout/coinselector.py")
replace_once(
    coinselector,
    "HARD_MIN_QUOTE_VOLUME_USDT = 100_000_000.0\n\n",
    '''HARD_MIN_QUOTE_VOLUME_USDT = 100_000_000.0

# Physical-commodity TradFi contracts are excluded from NEW positions.
# Equity, ETF and index contracts remain eligible unless the ETF itself is a
# known commodity tracker. Explicit market metadata is preferred over ticker
# aliases so ordinary stocks with ambiguous tickers such as CL, NG, GC or SI
# are not blocked when Binance identifies them as equities.
BLOCKED_TRADIFI_COMMODITY_BASES = {
    "NATGAS", "NGAS", "NATURALGAS", "NG", "UNG",
    "CL", "WTI", "BRENT", "USOIL", "UKOIL", "CRUDEOIL", "USO", "BNO",
    "XAU", "GOLD", "GC", "GLD", "IAU",
    "XAG", "SILVER", "SI", "SLV",
    "XPT", "PLATINUM", "PL", "PPLT",
    "XPD", "PALLADIUM", "PA", "PALL",
    "COPPER", "HG", "CPER",
    "ALUMINUM", "ALUMINIUM", "ALI",
    "ZINC", "NICKEL", "LEAD", "TIN",
    "IRON", "IRONORE", "LITHIUM", "COBALT", "URANIUM",
}

BLOCKED_TRADIFI_COMMODITY_MARKERS = {
    "COMMODITY", "COMMODITIES", "ENERGY",
    "METAL", "METALS", "MINERAL", "MINERALS",
    "PRECIOUSMETAL", "PRECIOUSMETALS",
    "INDUSTRIALMETAL", "INDUSTRIALMETALS",
}

TRADIFI_DIRECT_NON_COMMODITY_MARKERS = {
    "EQUITY", "EQUITIES", "STOCK", "STOCKS", "INDEX", "INDICES",
}

''',
    "commodity constants",
)
replace_once(
    coinselector,
    '''def market_is_tradifi_perpetual(symbol, market):
    if not market_is_usdt_perpetual(symbol, market):
        return False
    if not isinstance(market, dict):
        return False
    contract_type = str(_info_value(market, "contractType", "")).upper()
    return contract_type == "TRADIFI_PERPETUAL"


def extract_ticker_metrics(symbol, ticker):
''',
    '''def market_is_tradifi_perpetual(symbol, market):
    if not market_is_usdt_perpetual(symbol, market):
        return False
    if not isinstance(market, dict):
        return False
    contract_type = str(_info_value(market, "contractType", "")).upper()
    return contract_type == "TRADIFI_PERPETUAL"


def _flatten_market_metadata(value):
    if value is None:
        return []
    if isinstance(value, dict):
        items = []
        for nested in value.values():
            items.extend(_flatten_market_metadata(nested))
        return items
    if isinstance(value, (list, tuple, set)):
        items = []
        for nested in value:
            items.extend(_flatten_market_metadata(nested))
        return items
    return [str(value)]


def _normalize_market_metadata_token(value):
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def market_is_blocked_tradifi_commodity(symbol, market):
    """Return True for TradFi energy, precious-metal and mineral contracts."""
    if not market_is_tradifi_perpetual(symbol, market):
        return False

    market = market if isinstance(market, dict) else {}
    info = market.get("info") if isinstance(market.get("info"), dict) else {}
    metadata_keys = (
        "underlyingType",
        "underlyingSubType",
        "assetType",
        "category",
        "sector",
        "subType",
        "productType",
        "name",
        "displayName",
        "description",
    )
    metadata_values = []
    for source in (market, info):
        for key in metadata_keys:
            metadata_values.extend(_flatten_market_metadata(source.get(key)))
    normalized_metadata = {
        _normalize_market_metadata_token(value)
        for value in metadata_values
        if str(value).strip()
    }

    # Explicit stock/equity/index classification prevents ticker-name false
    # positives. ETF is intentionally not in this override because commodity
    # tracker ETFs (GLD, SLV, USO, UNG, etc.) must also remain blocked.
    if normalized_metadata & TRADIFI_DIRECT_NON_COMMODITY_MARKERS:
        return False
    if normalized_metadata & BLOCKED_TRADIFI_COMMODITY_MARKERS:
        return True

    base_candidates = {
        symbol_base(symbol),
        str(market.get("base") or "").upper(),
        str(info.get("baseAsset") or "").upper(),
        str(info.get("underlying") or "").upper(),
    }
    normalized_bases = {
        _normalize_market_metadata_token(value)
        for value in base_candidates
        if str(value).strip()
    }
    return bool(normalized_bases & BLOCKED_TRADIFI_COMMODITY_BASES)


def extract_ticker_metrics(symbol, ticker):
''',
    "commodity classifier",
)
replace_once(
    coinselector,
    '''    if not market_is_usdt_perpetual(symbol, market):
        return "INVALID_MARKET"
    # B. Low 24h quote volume:
''',
    '''    if not market_is_usdt_perpetual(symbol, market):
        return "INVALID_MARKET"
    if market_is_blocked_tradifi_commodity(symbol, market):
        return "REJECTED_TRADIFI_COMMODITY"
    # B. Low 24h quote volume:
''',
    "scanner commodity rejection",
)
replace_once(
    coinselector,
    '''        "exchange_symbol": symbol,
        "tradifi_perpetual": market_is_tradifi_perpetual(symbol, market),
        "sector_tags": sector_tags,
''',
    '''        "exchange_symbol": symbol,
        "tradifi_perpetual": market_is_tradifi_perpetual(symbol, market),
        "tradifi_commodity_blocked": market_is_blocked_tradifi_commodity(symbol, market),
        "sector_tags": sector_tags,
''',
    "candidate commodity flag",
)

controller = Path("bot_runtime/controller_exchange.py")
replace_once(
    controller,
    "from __future__ import annotations\n\n",
    '''from __future__ import annotations

from utbreakout.coinselector import market_is_blocked_tradifi_commodity

''',
    "controller commodity import",
)
replace_once(
    controller,
    '''        market_symbol = market.get('symbol') or normalized
        is_tradifi = coin_selector_market_is_tradifi_perpetual(market_symbol, market)
        if is_tradifi and mode != BINANCE_MAINNET:
''',
    '''        market_symbol = market.get('symbol') or normalized
        is_tradifi = coin_selector_market_is_tradifi_perpetual(market_symbol, market)
        if is_tradifi and market_is_blocked_tradifi_commodity(market_symbol, market):
            raise ValueError(
                f"REJECTED_TRADIFI_COMMODITY: 에너지·귀금속·광물 원자재 TradFi 신규 진입은 차단됩니다: {normalized}"
            )
        if is_tradifi and mode != BINANCE_MAINNET:
''',
    "watch symbol commodity block",
)
replace_once(
    controller,
    '''            if not coin_selector_market_is_tradifi_perpetual(symbol, market):
                continue
            normalized = normalize_coin_selector_custom_symbols([symbol])
''',
    '''            if not coin_selector_market_is_tradifi_perpetual(symbol, market):
                continue
            if market_is_blocked_tradifi_commodity(symbol, market):
                continue
            normalized = normalize_coin_selector_custom_symbols([symbol])
''',
    "auto TradFi commodity exclusion",
)
replace_once(
    controller,
    '''        is_tradifi = coin_selector_market_is_tradifi_perpetual(market.get('symbol') or resolved, market)
        if is_tradifi and mode == BINANCE_TESTNET:
''',
    '''        market_symbol = market.get('symbol') or resolved
        is_tradifi = coin_selector_market_is_tradifi_perpetual(market_symbol, market)
        if is_tradifi and market_is_blocked_tradifi_commodity(market_symbol, market):
            raise ValueError(
                f"REJECTED_TRADIFI_COMMODITY: 에너지·귀금속·광물 원자재 TradFi 신규 진입은 차단됩니다: {symbol}"
            )
        if is_tradifi and mode == BINANCE_TESTNET:
''',
    "order preflight commodity block",
)
replace_once(
    controller,
    '''        elif mode == BINANCE_MAINNET:
            lines.append("TradeFi 심볼은 메인넷에서만 허용되며 실제 TRADING market에 있을 때만 사용할 수 있습니다.")
''',
    '''        elif mode == BINANCE_MAINNET:
            lines.append("TradeFi 주식·ETF·지수 심볼은 메인넷에서 허용되며, 에너지·귀금속·광물 원자재는 신규 진입이 차단됩니다.")
''',
    "watchlist status wording",
)

scanner = Path("bot_runtime/signal_scanner.py")
replace_once(
    scanner,
    '''                except Exception as exc:
                    normalized = normalize_coin_selector_custom_symbols([requested_symbol])
                    normalized = normalized[0] if normalized else str(requested_symbol or '').strip().upper()
                    custom_resolution_rejected.append({
                        'symbol': normalized,
                        'exchange_symbol': normalized,
                        'normalized_symbol': normalized,
                        'accepted': False,
                        'reject_reasons': ['REJECTED_EXCHANGE_MODE_SYMBOL'],
                        'analysis_error': str(exc),
                        'selection_state': 'REJECTED',
                        'custom_universe': True,
                    })
''',
    '''                except Exception as exc:
                    normalized = normalize_coin_selector_custom_symbols([requested_symbol])
                    normalized = normalized[0] if normalized else str(requested_symbol or '').strip().upper()
                    error_text = str(exc)
                    reject_code = (
                        'REJECTED_TRADIFI_COMMODITY'
                        if error_text.startswith('REJECTED_TRADIFI_COMMODITY:')
                        else 'REJECTED_EXCHANGE_MODE_SYMBOL'
                    )
                    custom_resolution_rejected.append({
                        'symbol': normalized,
                        'exchange_symbol': normalized,
                        'normalized_symbol': normalized,
                        'accepted': False,
                        'reject_reasons': [reject_code],
                        'analysis_error': error_text,
                        'selection_state': 'REJECTED',
                        'custom_universe': True,
                    })
''',
    "custom universe commodity code",
)

live_orders = Path("bot_runtime/live_orders.py")
replace_once(
    live_orders,
    '''async def execute_live_order_plan(self, plan, cfg):
    cfg = enforce_activation_stage(cfg if isinstance(cfg, dict) else {})
''',
    '''async def execute_live_order_plan(self, plan, cfg):
    # Final new-entry gate. This protects automatic strategies and the user
    # custom-entry path even if a stale watchlist or cached selector result
    # somehow contains a blocked TradFi commodity. Existing-position exit and
    # protection flows do not call this entry function and remain unaffected.
    ctrl = getattr(self, "ctrl", None)
    if ctrl is not None and hasattr(ctrl, "_assert_symbol_tradeable_in_current_exchange_mode"):
        validated_symbol = await ctrl._assert_symbol_tradeable_in_current_exchange_mode(plan.symbol)
        if validated_symbol:
            plan.symbol = validated_symbol
    cfg = enforce_activation_stage(cfg if isinstance(cfg, dict) else {})
''',
    "final live entry commodity gate",
)

test_file = Path("tests/test_tradifi_commodity_block.py")
test_file.write_text(r'''import asyncio
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
''', encoding="utf-8")
