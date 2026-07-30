import asyncio

import emas


def _market(base, contract_type):
    return {
        "symbol": f"{base}/USDT:USDT",
        "id": f"{base}USDT",
        "base": base,
        "quote": "USDT",
        "settle": "USDT",
        "swap": True,
        "active": True,
        "type": "swap",
        "info": {
            "symbol": f"{base}USDT",
            "contractType": contract_type,
            "status": "TRADING",
        },
    }


class _Exchange:
    def __init__(self, markets):
        self.markets = markets
        self.ohlcv_requests = []

    def load_markets(self):
        return self.markets

    def fetch_ohlcv(self, symbol, timeframe, limit=None):
        self.ohlcv_requests.append((symbol, timeframe, limit))
        raise AssertionError("TradFi regime bypass must not request BTC/ETH OHLCV")


def _engine(markets):
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    exchange = _Exchange(markets)
    engine.exchange = exchange
    engine.market_data_exchange = exchange
    engine.coin_selector_symbol_scores = {}
    engine.tradifi_symbol_classification_cache = {}
    engine.utbreakout_market_regime_cache = {}
    engine.is_upbit_mode = lambda: False
    return engine, exchange


def test_tradifi_symbol_skips_btc_eth_regime_fetch_entirely():
    qqq = _market("QQQ", "TRADIFI_PERPETUAL")
    engine, exchange = _engine({"QQQ/USDT:USDT": qqq})

    context = asyncio.run(
        engine._fetch_utbreakout_market_regime_context(
            {
                "market_quality_regime_enabled": True,
                "market_quality_regime_symbols": ["BTC/USDT", "ETH/USDT"],
                "market_quality_regime_timeframe": "4h",
            },
            symbol="QQQ/USDT:USDT",
        )
    )

    assert context["bypassed"] is True
    assert context["bypass_reason"] == "TRADIFI_BTC_ETH_REGIME_BYPASS"
    assert context["items"] == {}
    assert exchange.ohlcv_requests == []


def test_crypto_symbol_is_not_misclassified_as_tradifi():
    btc = _market("BTC", "PERPETUAL")
    engine, _ = _engine({"BTC/USDT:USDT": btc})

    assert asyncio.run(
        engine._is_tradifi_perpetual_symbol("BTC/USDT:USDT")
    ) is False
