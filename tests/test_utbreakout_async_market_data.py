import asyncio

import emas


def test_signal_engine_fetch_ohlcv_async_uses_market_data_exchange():
    calls = []
    candles = [[1, 100, 101, 99, 100.5, 10]]

    class MarketDataExchange:
        def fetch_ohlcv(self, symbol, timeframe, limit=300):
            calls.append((symbol, timeframe, limit))
            return candles

    engine = object.__new__(emas.SignalEngine)
    engine.market_data_exchange = MarketDataExchange()

    result = asyncio.run(
        engine.fetch_ohlcv_async(
            "SOL/USDT:USDT",
            "1h",
            limit=60,
        )
    )

    assert result == candles
    assert calls == [("SOL/USDT:USDT", "1h", 60)]
