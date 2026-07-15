import asyncio
from types import SimpleNamespace

import emas


def test_startup_user_stream_network_follows_controller_exchange_mode(monkeypatch):
    created = []

    class FakeStream:
        def __init__(self, exchange, store, **kwargs):
            self.exchange = exchange
            self.store = store
            self.testnet = kwargs["testnet"]
            created.append(self)

        def start(self):
            return None

    async def scenario():
        engine = object.__new__(emas.SignalEngine)
        engine.ctrl = SimpleNamespace(
            get_exchange_mode=lambda: emas.BINANCE_MAINNET,
            notify=lambda message: None,
        )
        engine.exchange = object()
        engine.trading_state_store = object()
        engine.crypto_entry_lock_reason = None
        engine.get_runtime_common_settings = lambda: {
            "testnet": True,
            "user_data_stream_enabled": True,
        }
        engine.is_upbit_mode = lambda: False
        engine._set_crypto_entry_lock = lambda reason: setattr(
            engine, "crypto_entry_lock_reason", reason
        )

        async def reconcile(**kwargs):
            return SimpleNamespace(safe_to_trade=True)

        async def recover():
            return None

        engine._reconcile_crypto_exchange_state = reconcile
        engine._recover_open_utbreakout_positions_on_start = recover

        await engine._startup_crypto_safety_reconciliation()

    monkeypatch.setattr(emas, "BinanceUserDataStream", FakeStream)
    asyncio.run(scenario())

    assert len(created) == 1
    assert created[0].testnet is False


def test_shutdown_crypto_safety_runtime_cancels_startup_and_stops_stream():
    async def scenario():
        engine = object.__new__(emas.SignalEngine)
        waiting = asyncio.Event()
        startup_task = asyncio.create_task(waiting.wait())

        class FakeStream:
            stopped = False

            async def stop(self):
                self.stopped = True

        stream = FakeStream()
        engine.crypto_safety_startup_task = startup_task
        engine.user_data_stream = stream

        await engine._shutdown_crypto_safety_runtime()

        assert startup_task.cancelled()
        assert stream.stopped is True
        assert engine.crypto_safety_startup_task is None
        assert engine.user_data_stream is None

    asyncio.run(scenario())


def test_protection_symbol_normalization_handles_usdc_contracts():
    engine = object.__new__(emas.SignalEngine)

    assert engine._normalize_protection_symbol("LTC/USDC:USDC") == "LTCUSDC"
    assert engine._protection_order_matches_symbol(
        {"symbol": "LTCUSDC"},
        "LTC/USDC:USDC",
    )
    assert "LTC/USDC:USDC" in engine._protection_cancel_symbol_candidates(
        "LTCUSDC"
    )


def test_main_controller_order_symbol_handles_usdc_settlement():
    controller = object.__new__(emas.MainController)

    assert controller._futures_symbol_for_order("LTC/USDC:USDC") == "LTC/USDC"
