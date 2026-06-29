import emas


def test_binance_exchange_builders_disable_global_open_order_warning(monkeypatch):
    calls = []

    def fake_binance(options):
        calls.append(options)
        return object()

    monkeypatch.setattr(emas.ccxt, "binance", fake_binance)

    ctrl = object.__new__(emas.MainController)
    ctrl.get_exchange_mode = lambda: emas.BINANCE_TESTNET

    ctrl._build_exchange({"api_key": "k", "secret_key": "s"})
    ctrl._build_public_market_data_exchange()

    assert len(calls) == 2
    for call in calls:
        opts = call["options"]
        assert opts["defaultType"] == "future"
        assert opts["warnOnFetchOpenOrdersWithoutSymbol"] is False
