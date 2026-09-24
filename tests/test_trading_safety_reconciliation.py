import asyncio

import emas

from trading_safety.order_state import OrderRecord, OrderState, SQLiteTradingStateStore
from trading_safety.reconciliation import reconcile_exchange_state


class ReconcileExchange:
    def __init__(self, positions=None, orders=None):
        self.positions = positions or []
        self.orders = orders or []

    def fetch_positions(self):
        return self.positions

    def fetch_open_orders(self):
        return self.orders


def test_exchange_position_without_local_record_blocks_startup(tmp_path):
    async def scenario():
        exchange = ReconcileExchange(
            positions=[{"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 1, "entryPrice": 100}],
            orders=[{"symbol": "BTC/USDT:USDT", "type": "STOP_MARKET", "reduceOnly": True, "id": "sl-1"}],
        )
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        result = await reconcile_exchange_state(exchange, store)
        assert result.safe_to_trade is False
        assert any("without_local_record" in issue for issue in result.issues)
        assert store.list_by_states({OrderState.FILLED_UNPROTECTED})

    asyncio.run(scenario())


def test_binance_adopted_external_position_unlocks_after_verified_stop(tmp_path):
    class BinanceExternalPositionExchange:
        id = "binance"

        def __init__(self):
            self.entry_lookups = []

        def fetch_positions(self):
            return [{
                "symbol": "KORU/USDT:USDT",
                "side": "short",
                "contracts": 20.81,
                "entryPrice": 20.3444,
                "markPrice": 20.3310,
                "liquidationPrice": 24.0,
            }]

        def fetch_open_orders(self):
            return []

        def fapiPrivateGetOpenAlgoOrders(self, params):
            return [{
                "algoId": "4000001772804400",
                "clientAlgoId": "manual-stop",
                "symbol": "KORUUSDT",
                "orderType": "STOP_MARKET",
                "side": "BUY",
                "closePosition": "true",
                "quantity": "0",
                "triggerPrice": "21.36",
                "reduceOnly": "true",
                "workingType": "MARK_PRICE",
                "algoStatus": "NEW",
            }]

        def fapiPrivateGetOrder(self, params):
            self.entry_lookups.append(dict(params))
            raise RuntimeError("-2013 Order does not exist")

        def market(self, symbol):
            return {
                "precision": {"price": 0.01},
                "info": {
                    "filters": [{
                        "filterType": "PRICE_FILTER",
                        "tickSize": "0.01",
                    }]
                },
            }

    async def scenario():
        exchange = BinanceExternalPositionExchange()
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")

        adopted = await reconcile_exchange_state(exchange, store)
        verified = await reconcile_exchange_state(exchange, store)

        assert adopted.safe_to_trade is False
        assert any(
            "exchange_position_without_local_record" in issue
            for issue in adopted.issues
        )
        assert verified.safe_to_trade is True
        assert verified.unresolved_records == []
        assert exchange.entry_lookups == []
        records = store.records_for_symbol("KORU/USDT:USDT")
        synthetic = next(
            record
            for record in records
            if record.strategy == "EXTERNAL_OR_PRE_RECONCILIATION"
        )
        assert synthetic.order_state == OrderState.PROTECTED.value
        assert synthetic.stop_order_id == "4000001772804400"

    asyncio.run(scenario())


def test_unprotected_position_blocks_and_verified_stop_recovers(tmp_path):
    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        store.upsert(
            OrderRecord(
                "cid-1", "BTC/USDT:USDT", "LONG", "UTB", "1", 1.0,
                order_state=OrderState.FILLED_UNPROTECTED.value,
            )
        )
        position = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 1, "entryPrice": 100}
        blocked = await reconcile_exchange_state(ReconcileExchange([position], []), store)
        recovered = await reconcile_exchange_state(
            ReconcileExchange(
                [position],
                [{"symbol": "BTC/USDT:USDT", "type": "STOP_MARKET", "reduceOnly": True, "id": "sl-1"}],
            ),
            store,
        )
        assert blocked.safe_to_trade is False
        assert recovered.safe_to_trade is True
        assert store.get("cid-1").order_state == OrderState.PROTECTED.value

    asyncio.run(scenario())


def test_intentional_ema200_first_stage_without_stop_reconciles_safely(tmp_path):
    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        store.upsert(
            OrderRecord(
                "ema-entry-1",
                "BTC/USDT:USDT",
                "LONG",
                "ema200_utbot_rsi_2h",
                "1",
                1.0,
                order_state=OrderState.PROTECTED.value,
                metadata={"strategy_managed_no_stop": True},
            )
        )
        position = {
            "symbol": "BTC/USDT:USDT",
            "side": "long",
            "contracts": 1,
            "entryPrice": 100,
        }

        result = await reconcile_exchange_state(
            ReconcileExchange([position], []),
            store,
        )

        assert result.safe_to_trade is True
        assert not any(
            "position_without_verified_stop" in issue
            for issue in result.issues
        )
        assert store.get("ema-entry-1").order_state == OrderState.PROTECTED.value

    asyncio.run(scenario())


def test_local_active_without_exchange_position_requires_reconciliation(tmp_path):
    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        store.upsert(
            OrderRecord(
                "cid-1", "BTC/USDT:USDT", "LONG", "UTB", "1", 1.0,
                order_state=OrderState.PROTECTED.value,
            )
        )
        result = await reconcile_exchange_state(ReconcileExchange([], []), store)
        assert result.safe_to_trade is False
        assert any("local_active_without_exchange_position" in issue for issue in result.issues)
        assert store.get("cid-1").order_state == OrderState.PROTECTED.value

    asyncio.run(scenario())


def test_binance_terminal_entry_record_closes_when_exchange_position_is_flat(tmp_path):
    class BinanceFlatExchange:
        id = "binance"

        def fetch_positions(self):
            return []

        def fetch_open_orders(self):
            return []

        def fapiPrivateGetOpenAlgoOrders(self, params):
            return []

        def fapiPrivateGetOrder(self, params):
            assert params["symbol"] == "LTCUSDC"
            return {
                "symbol": "LTCUSDC",
                "clientOrderId": params["origClientOrderId"],
                "status": "FILLED",
            }

    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        store.upsert(
            OrderRecord(
                "cid-usdc",
                "LTC/USDC:USDC",
                "LONG",
                "UTB",
                "1",
                1.0,
                order_state=OrderState.PROTECTED.value,
            )
        )

        result = await reconcile_exchange_state(
            BinanceFlatExchange(),
            store,
            position_visibility_grace_seconds=0,
        )

        assert result.safe_to_trade is True
        record = store.get("cid-usdc")
        assert record.order_state == OrderState.CLOSED.value
        assert record.metadata["reconciled_terminal_order_status"] == "FILLED"
        assert result.closed_position_symbols == ["LTC/USDC:USDC"]

    asyncio.run(scenario())


def test_recent_fill_waits_for_position_visibility_instead_of_closing(tmp_path):
    class EventuallyVisibleBinanceExchange:
        id = "binance"

        def __init__(self):
            self.positions = []

        def fetch_positions(self):
            return list(self.positions)

        def fetch_open_orders(self):
            return []

        def fapiPrivateGetOpenAlgoOrders(self, params):
            return [{
                "algoId": "sl-1",
                "clientAlgoId": "sl-client",
                "symbol": "BTCUSDT",
                "orderType": "STOP_MARKET",
                "side": "SELL",
                "quantity": "1",
                "triggerPrice": "90",
                "reduceOnly": "true",
                "workingType": "MARK_PRICE",
                "algoStatus": "NEW",
            }]

        def fapiPrivateGetOrder(self, params):
            return {
                "symbol": "BTCUSDT",
                "clientOrderId": params["origClientOrderId"],
                "status": "FILLED",
            }

        def market(self, symbol):
            return {
                "precision": {"price": 0.01},
                "info": {
                    "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"}]
                },
            }

    async def scenario():
        exchange = EventuallyVisibleBinanceExchange()
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        store.upsert(
            OrderRecord(
                "entry-race",
                "BTC/USDT:USDT",
                "LONG",
                "UTB",
                "1",
                1.0,
                filled_qty=1.0,
                average_fill_price=100.0,
                order_state=OrderState.FILLED_UNPROTECTED.value,
            )
        )

        pending = await reconcile_exchange_state(exchange, store)

        assert pending.safe_to_trade is False
        assert "position_visibility_pending:BTC/USDT:USDT" in pending.issues
        assert store.get("entry-race").order_state == OrderState.FILLED_UNPROTECTED.value

        exchange.positions = [{
            "symbol": "BTC/USDT:USDT",
            "side": "long",
            "contracts": 1,
            "entryPrice": 100,
            "liquidationPrice": 50,
        }]
        recovered = await reconcile_exchange_state(exchange, store)

        assert recovered.safe_to_trade is True
        assert store.get("entry-race").order_state == OrderState.PROTECTED.value
        assert not any(
            record.strategy == "EXTERNAL_OR_PRE_RECONCILIATION"
            for record in store.records_for_symbol("BTC/USDT:USDT")
        )

    asyncio.run(scenario())


def test_tracked_entry_closes_duplicate_synthetic_reconciliation_record(tmp_path):
    class BinancePositionExchange:
        id = "binance"

        def fetch_positions(self):
            return [{
                "symbol": "BTC/USDT:USDT",
                "side": "long",
                "contracts": 1,
                "entryPrice": 100,
                "liquidationPrice": 50,
            }]

        def fetch_open_orders(self):
            return []

        def fapiPrivateGetOpenAlgoOrders(self, params):
            return [{
                "algoId": "sl-1",
                "clientAlgoId": "sl-client",
                "symbol": "BTCUSDT",
                "orderType": "STOP_MARKET",
                "side": "SELL",
                "quantity": "1",
                "triggerPrice": "90",
                "reduceOnly": "true",
                "workingType": "MARK_PRICE",
                "algoStatus": "NEW",
            }]

        def fapiPrivateGetOrder(self, params):
            return {
                "symbol": "BTCUSDT",
                "clientOrderId": params["origClientOrderId"],
                "status": "FILLED",
            }

        def market(self, symbol):
            return {
                "precision": {"price": 0.01},
                "info": {
                    "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"}]
                },
            }

    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        store.upsert(
            OrderRecord(
                "entry-primary",
                "BTC/USDT:USDT",
                "LONG",
                "UTB",
                "1",
                1.0,
                filled_qty=1.0,
                order_state=OrderState.PROTECTED.value,
                stop_order_id="sl-1",
            )
        )
        store.upsert(
            OrderRecord(
                "entry-synthetic",
                "BTC/USDT:USDT",
                "LONG",
                "EXTERNAL_OR_PRE_RECONCILIATION",
                "2",
                1.0,
                filled_qty=1.0,
                order_state=OrderState.PROTECTED.value,
                stop_order_id="sl-1",
                metadata={"source": "startup_exchange_reconciliation"},
            )
        )

        result = await reconcile_exchange_state(BinancePositionExchange(), store)

        assert result.safe_to_trade is True
        duplicate = store.get("entry-synthetic")
        assert duplicate.order_state == OrderState.CLOSED.value
        assert duplicate.metadata["duplicate_of_client_order_id"] == "entry-primary"
        assert store.get("entry-primary").order_state == OrderState.PROTECTED.value

    asyncio.run(scenario())


def test_startup_closes_synthetic_record_after_confirmed_emergency_close(tmp_path):
    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        store.upsert(
            OrderRecord(
                "recon-hmstr", "HMSTR/USDT:USDT", "LONG",
                "EXTERNAL_OR_PRE_RECONCILIATION", "1", 10.0,
                filled_qty=10.0,
                order_state=OrderState.FILLED_UNPROTECTED.value,
                created_at="2026-07-11T12:53:15+00:00",
                updated_at="2026-07-11T12:53:15+00:00",
            )
        )
        store.upsert(
            OrderRecord(
                "emerg-hmstr", "HMSTR/USDT", "LONG",
                "EMERGENCY_PROTECTION_CLOSE", "2", 10.0,
                filled_qty=10.0,
                order_state=OrderState.CLOSED.value,
                created_at="2026-07-11T12:53:21+00:00",
                updated_at="2026-07-11T12:53:22+00:00",
            )
        )

        result = await reconcile_exchange_state(ReconcileExchange([], []), store)

        assert result.safe_to_trade is True
        assert store.get("recon-hmstr").order_state == OrderState.CLOSED.value
        assert (
            store.get("recon-hmstr").metadata["reconciled_by_close_client_order_id"]
            == "emerg-hmstr"
        )

    asyncio.run(scenario())


def test_oversized_reduce_only_order_blocks_startup(tmp_path):
    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        position = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 1, "entryPrice": 100}
        store.upsert(
            OrderRecord(
                "cid-1", "BTC/USDT:USDT", "LONG", "UTB", "1", 1.0,
                order_state=OrderState.PROTECTED.value,
            )
        )
        orders = [
            {"symbol": "BTC/USDT:USDT", "type": "STOP_MARKET", "reduceOnly": True, "amount": 1, "id": "sl"},
            {"symbol": "BTC/USDT:USDT", "type": "LIMIT", "reduceOnly": True, "amount": 1.1, "id": "tp"},
        ]
        result = await reconcile_exchange_state(ReconcileExchange([position], orders), store)
        assert result.safe_to_trade is False
        assert any("reduce_only_qty_exceeds_position" in issue for issue in result.issues)

    asyncio.run(scenario())


def _ema_first_stage_record(
    client_order_id,
    *,
    metadata=None,
    stop_order_id=None,
    order_state=OrderState.PROTECTED.value,
):
    return OrderRecord(
        client_order_id,
        "BTC/USDT:USDT",
        "LONG",
        "ema200_utbot_rsi_2h",
        "1",
        1.0,
        filled_qty=1.0,
        average_fill_price=100.0,
        order_state=order_state,
        stop_order_id=stop_order_id,
        metadata={
            "strategy_managed_no_stop": True,
            **dict(metadata or {}),
        },
    )


def _btc_long_position():
    return {
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "contracts": 1.0,
        "entryPrice": 100.0,
    }


def test_generic_reconciliation_rejects_no_stop_exception_when_profit_stop_pending(tmp_path):
    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        store.upsert(_ema_first_stage_record(
            "ema-pending",
            metadata={
                "ema200_profit_stop_pending_client_order_id": "pending-stop-client",
                "ema200_profit_stop_pending_side": "long",
                "ema200_profit_stop_pending_qty": 1.0,
                "ema200_profit_stop_pending_trigger_price": 101.0,
            },
        ))

        result = await reconcile_exchange_state(
            ReconcileExchange([_btc_long_position()], []),
            store,
        )

        assert result.safe_to_trade is False
        combined = list(result.issues) + list(result.unresolved_records)
        assert any(
            "ema200_profit_stop_pending_unresolved" in item
            for item in combined
        )
        store.close()

    asyncio.run(scenario())


def test_first_stage_no_stop_exception_still_allowed_before_profit_stop_lifecycle(tmp_path):
    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        store.upsert(_ema_first_stage_record("ema-no-stop-yet"))

        result = await reconcile_exchange_state(
            ReconcileExchange([_btc_long_position()], []),
            store,
        )

        assert result.safe_to_trade is True
        assert not any(
            "position_without_verified_stop" in issue
            for issue in result.issues
        )
        assert not any(
            "ema200_profit_stop_pending_unresolved" in item
            for item in list(result.issues) + list(result.unresolved_records)
        )
        store.close()

    asyncio.run(scenario())


def test_confirmed_profit_stop_disables_no_stop_reconciliation_exception(tmp_path):
    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        store.upsert(_ema_first_stage_record(
            "ema-confirmed-stop",
            stop_order_id="profit-stop-1",
            metadata={
                "ema200_profit_stop_order_id": "profit-stop-1",
                "ema200_profit_stop_client_order_id": "profit-stop-client-1",
                "ema200_profit_stop_side": "long",
                "ema200_profit_stop_qty": 1.0,
            },
        ))

        result = await reconcile_exchange_state(
            ReconcileExchange([_btc_long_position()], []),
            store,
        )

        assert result.safe_to_trade is False
        assert any(
            "position_without_verified_stop" in issue
            for issue in result.issues
        )
        store.close()

    asyncio.run(scenario())


def test_stale_first_stage_record_cannot_exempt_newer_protected_trade(tmp_path):
    """An old active record must not waive a newer trade's required SL."""
    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / 'state.sqlite3')
        store.upsert(_ema_first_stage_record('old-no-stop'))
        store.upsert(_ema_first_stage_record(
            'new-with-profit-stop',
            stop_order_id='now-missing-stop',
            metadata={'ema200_profit_stop_order_id': 'now-missing-stop'},
        ))
        result = await reconcile_exchange_state(
            ReconcileExchange([_btc_long_position()], []),
            store,
        )
        assert result.safe_to_trade is False
        assert any('position_without_verified_stop' in issue for issue in result.issues)
        store.close()

    asyncio.run(scenario())


def test_stronger_protection_lock_is_not_cleared_by_safe_generic_reconciliation(tmp_path, monkeypatch):
    # Another test may have persisted a global critical pause.  This case
    # isolates ownership of the protection lock from that independent guard.
    monkeypatch.setattr(emas, 'load_critical_pause_state', lambda: None)
    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        engine = emas.SignalEngine.__new__(emas.SignalEngine)
        engine.exchange = ReconcileExchange([], [])
        engine.trading_state_store = store
        engine.get_runtime_common_settings = lambda: {
            "single_position_mode": True,
        }

        async def account_for_flat(_result):
            return []

        engine._account_for_reconciled_flat_trades = account_for_flat
        engine._set_crypto_entry_lock(
            "PENDING_PROTECTION_RECONCILIATION:BTC/USDT:USDT"
        )

        result = await engine._reconcile_crypto_exchange_state(
            user_stream_ready=True,
            require_user_stream=False,
        )

        assert result.safe_to_trade is True
        assert engine.crypto_entry_lock_reason == (
            "PENDING_PROTECTION_RECONCILIATION:BTC/USDT:USDT"
        )
        assert store.get_runtime_state("entry_lock_reason") == (
            "PENDING_PROTECTION_RECONCILIATION:BTC/USDT:USDT"
        )
        store.close()

    asyncio.run(scenario())


def test_startup_reconciliation_does_not_clear_pending_profit_stop_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(emas, 'load_critical_pause_state', lambda: None)
    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        store.upsert(_ema_first_stage_record(
            "ema-startup-pending",
            metadata={
                "ema200_profit_stop_pending_client_order_id": "pending-startup",
                "ema200_profit_stop_pending_side": "long",
                "ema200_profit_stop_pending_qty": 1.0,
                "ema200_profit_stop_pending_trigger_price": 101.0,
            },
        ))

        class StartupExchange(ReconcileExchange):
            id = "binance"

            def fapiPrivateGetOpenAlgoOrders(self, params):
                return []

            def fapiPrivateGetAlgoOrder(self, params):
                raise TimeoutError("pending lookup timeout")

            def fapiPrivateGetOrder(self, params):
                raise RuntimeError("-2013 Order does not exist.")

        exchange = StartupExchange([_btc_long_position()], [])
        engine = emas.SignalEngine.__new__(emas.SignalEngine)
        engine.exchange = exchange
        engine.trading_state_store = store
        engine.ctrl = None
        engine.crypto_entry_lock_reason = None
        engine.is_upbit_mode = lambda: False
        engine.get_runtime_common_settings = lambda: {
            "single_position_mode": True,
            "user_data_stream_enabled": False,
        }

        async def account_for_flat(_result):
            return []

        async def recover_pending():
            result = await engine._reconcile_ema200_profit_stop_pending_identity(
                "BTC/USDT:USDT",
                pos=_btc_long_position(),
                protection_orders=[],
            )
            assert result["status"] == "UNKNOWN"
            return {"status": "PENDING", "recovered": 0}

        engine._account_for_reconciled_flat_trades = account_for_flat
        engine._recover_open_utbreakout_positions_on_start = recover_pending

        await engine._startup_crypto_safety_reconciliation()

        assert engine.crypto_entry_lock_reason is not None
        assert "PENDING_PROTECTION" in engine.crypto_entry_lock_reason
        assert "PENDING_PROTECTION" in str(
            store.get_runtime_state("entry_lock_reason")
        )
        final = store.get_runtime_state("last_reconciliation")
        assert final["safe_to_trade"] is False
        store.close()

    asyncio.run(scenario())

def test_pending_profit_stop_restart_keeps_entry_blocked_until_reconciled(tmp_path):
    async def scenario():
        path = tmp_path / "state.sqlite3"
        store = SQLiteTradingStateStore(path)
        store.upsert(_ema_first_stage_record(
            "ema-restart-pending",
            metadata={
                "ema200_profit_stop_pending_client_order_id": "pending-restart",
                "ema200_profit_stop_pending_side": "long",
                "ema200_profit_stop_pending_qty": 1.0,
                "ema200_profit_stop_pending_trigger_price": 101.0,
            },
        ))
        store.close()

        reopened = SQLiteTradingStateStore(path)
        result = await reconcile_exchange_state(
            ReconcileExchange([_btc_long_position()], []),
            reopened,
        )

        assert result.safe_to_trade is False
        assert any(
            "ema200_profit_stop_pending_unresolved" in item
            for item in list(result.issues) + list(result.unresolved_records)
        )
        reopened.close()

    asyncio.run(scenario())
