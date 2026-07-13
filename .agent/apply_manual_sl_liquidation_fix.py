from pathlib import Path


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"{label} block not found in {path}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


root = Path(".")

# 1) Core liquidation validator: generated orders remain MARK_PRICE-only by
# default, while callers may explicitly accept an external CONTRACT_PRICE stop
# with a much larger liquidation buffer.
liq = root / "trading_safety/liquidation_guard.py"
replace_once(
    liq,
    '''def validate_stop_against_liquidation(
    side: str,
    stop_price: Any,
    liquidation_price: Any,
    tick_size: Any,
    minimum_buffer_pct: Any,
    minimum_buffer_ticks: int,
    working_type: str,
    entry_price: Any = 0,
) -> LiquidationSafetyResult:
''',
    '''def validate_stop_against_liquidation(
    side: str,
    stop_price: Any,
    liquidation_price: Any,
    tick_size: Any,
    minimum_buffer_pct: Any,
    minimum_buffer_ticks: int,
    working_type: str,
    entry_price: Any = 0,
    *,
    accepted_working_types: set[str] | frozenset[str] | tuple[str, ...] | None = None,
    non_mark_minimum_buffer_pct: Any | None = None,
    non_mark_buffer_multiplier: Any = 1,
) -> LiquidationSafetyResult:
''',
    "liquidation signature",
)
replace_once(
    liq,
    '''    buffer_rate = as_decimal(minimum_buffer_pct, name="minimum_buffer_pct")
    working = str(working_type or "").strip().upper()

    if side_value not in {"long", "short"}:
''',
    '''    buffer_rate = as_decimal(minimum_buffer_pct, name="minimum_buffer_pct")
    working = str(working_type or "").strip().upper()
    accepted = {
        str(value or "").strip().upper()
        for value in (accepted_working_types or {"MARK_PRICE"})
        if str(value or "").strip()
    }
    if not accepted:
        accepted = {"MARK_PRICE"}
    effective_buffer_rate = buffer_rate
    if working != "MARK_PRICE" and working in accepted:
        multiplier = as_decimal(non_mark_buffer_multiplier, name="non_mark_buffer_multiplier")
        if multiplier < Decimal("1"):
            multiplier = Decimal("1")
        effective_buffer_rate = buffer_rate * multiplier
        if non_mark_minimum_buffer_pct is not None:
            effective_buffer_rate = max(
                effective_buffer_rate,
                as_decimal(
                    non_mark_minimum_buffer_pct,
                    name="non_mark_minimum_buffer_pct",
                ),
            )

    if side_value not in {"long", "short"}:
''',
    "liquidation accepted working types",
)
replace_once(
    liq,
    '''    buffer_abs = max(
        liquidation * buffer_rate,
''',
    '''    buffer_abs = max(
        liquidation * effective_buffer_rate,
''',
    "liquidation effective buffer",
)
replace_once(
    liq,
    '''    if working != "MARK_PRICE":
        reason = "STOP_WORKING_TYPE_NOT_MARK_PRICE"
        valid = False
''',
    '''    if working not in accepted:
        reason = (
            "STOP_WORKING_TYPE_NOT_MARK_PRICE"
            if accepted == {"MARK_PRICE"}
            else "STOP_WORKING_TYPE_NOT_ACCEPTED"
        )
        valid = False
''',
    "liquidation working type decision",
)
replace_once(
    liq,
    '''    else:
        reason = "SAFE"
        valid = True
''',
    '''    else:
        reason = "SAFE" if working == "MARK_PRICE" else f"SAFE_EXTERNAL_{working}"
        valid = True
''',
    "liquidation safe reason",
)

# 2) Preserve fields used by manual Binance UI close-all stops.
algo = root / "trading_safety/binance_algo_gateway.py"
replace_once(
    algo,
    '''            "reduceOnly": str(info.get("reduceOnly", "false")).lower() == "true"
            if not isinstance(info.get("reduceOnly"), bool)
            else info.get("reduceOnly"),
            "workingType": info.get("workingType"),
''',
    '''            "reduceOnly": str(info.get("reduceOnly", "false")).lower() == "true"
            if not isinstance(info.get("reduceOnly"), bool)
            else info.get("reduceOnly"),
            "closePosition": str(info.get("closePosition", "false")).lower() == "true"
            if not isinstance(info.get("closePosition"), bool)
            else info.get("closePosition"),
            "positionSide": info.get("positionSide"),
            "workingType": info.get("workingType"),
''',
    "algo close position normalization",
)

# 3) Startup/reconnect reconciliation accepts closePosition quantity=0 and a
# wide external CONTRACT_PRICE stop, but generated stops stay strict.
reconciliation = root / "trading_safety/reconciliation.py"
replace_once(
    reconciliation,
    '''def _is_reduce_only(order: dict[str, Any]) -> bool:
    info = _as_dict(order.get("info"))
    for value in (order.get("reduceOnly"), info.get("reduceOnly"), info.get("closePosition")):
''',
    '''def _is_reduce_only(order: dict[str, Any]) -> bool:
    info = _as_dict(order.get("info"))
    for value in (
        order.get("reduceOnly"),
        order.get("closePosition"),
        info.get("reduceOnly"),
        info.get("closePosition"),
    ):
''',
    "reconciliation reduce only",
)
replace_once(
    reconciliation,
    '''def _order_qty(order: dict[str, Any]) -> float:
''',
    '''def _is_close_position_order(order: dict[str, Any]) -> bool:
    info = _as_dict(order.get("info"))
    for value in (order.get("closePosition"), info.get("closePosition")):
        if isinstance(value, bool) and value:
            return True
        if str(value or "").strip().lower() in {"true", "1", "yes"}:
            return True
    return False


def _order_qty(order: dict[str, Any]) -> float:
''',
    "reconciliation close position helper",
)
replace_once(
    reconciliation,
    '''            if _order_qty(stop) + max(position_qty * 0.01, 1e-12) < position_qty:
                stop_failures.append("stop_qty_insufficient")
                continue
''',
    '''            if (
                not _is_close_position_order(stop)
                and _order_qty(stop) + max(position_qty * 0.01, 1e-12) < position_qty
            ):
                stop_failures.append("stop_qty_insufficient")
                continue
''',
    "reconciliation stop quantity",
)
replace_once(
    reconciliation,
    '''            safety = validate_stop_against_liquidation(
                side,
                _order_trigger_price(stop),
                liquidation_price,
                tick_size,
                safety_cfg.minimum_buffer_pct,
                safety_cfg.minimum_buffer_ticks,
                _order_working_type(stop),
                entry_price,
            )
''',
    '''            working_type = _order_working_type(stop) or "CONTRACT_PRICE"
            safety = validate_stop_against_liquidation(
                side,
                _order_trigger_price(stop),
                liquidation_price,
                tick_size,
                safety_cfg.minimum_buffer_pct,
                safety_cfg.minimum_buffer_ticks,
                working_type,
                entry_price,
                accepted_working_types={"MARK_PRICE", "CONTRACT_PRICE"},
                non_mark_minimum_buffer_pct="0.05",
                non_mark_buffer_multiplier="2",
            )
''',
    "reconciliation external stop validation",
)

# 4) Runtime audit: distinguish bot-owned and manual orders; preserve a valid
# external stop instead of cancelling it and force-closing the position.
emas = root / "emas.py"
replace_once(
    emas,
    '''    def _protection_working_type(self, order):
        if not isinstance(order, dict):
            return ''
        info = self._protection_order_info(order)
        return str(
            order.get('workingType')
            or order.get('working_type')
            or info.get('workingType')
            or info.get('working_type')
            or ''
        ).strip().upper()

    def _liquidation_safety_config(self, extra=None):
''',
    '''    def _protection_working_type(self, order):
        if not isinstance(order, dict):
            return ''
        info = self._protection_order_info(order)
        return str(
            order.get('workingType')
            or order.get('working_type')
            or info.get('workingType')
            or info.get('working_type')
            or ''
        ).strip().upper()

    def _is_bot_managed_protection_order(self, order):
        client_id = re.sub(
            r'[^a-z0-9]',
            '',
            self._protection_client_order_id(order).lower(),
        )
        return client_id.startswith('utb')

    def _liquidation_safety_config(self, extra=None):
''',
    "runtime bot managed protection helper",
)
replace_once(
    emas,
    '''    def _protection_order_type(self, order):
''',
    '''    def _validate_existing_position_stop_liquidation(
        self,
        symbol,
        pos,
        stop_price,
        working_type,
        order,
        cfg=None,
    ):
        result = self._validate_position_stop_liquidation(
            symbol,
            pos,
            stop_price,
            working_type,
            cfg,
        )
        if result is None or result.valid or self._is_bot_managed_protection_order(order):
            return result
        if result.reason not in {
            'STOP_WORKING_TYPE_NOT_MARK_PRICE',
            'STOP_WORKING_TYPE_NOT_ACCEPTED',
        }:
            return result
        actual_working_type = str(working_type or 'CONTRACT_PRICE').strip().upper()
        if actual_working_type not in {'CONTRACT_PRICE', 'LAST_PRICE'}:
            return result
        side = str((pos or {}).get('side') or '').lower()
        liquidation_price = _safe_float_or_none(
            (pos or {}).get('liquidationPrice')
            or (pos or {}).get('liquidation_price')
            or self._protection_order_info(pos or {}).get('liquidationPrice')
        )
        entry_price = _safe_float_or_none(
            (pos or {}).get('entryPrice') or (pos or {}).get('entry_price')
        ) or 0.0
        tick_size = self._liquidation_tick_size(symbol)
        if side not in {'long', 'short'} or not liquidation_price or tick_size <= 0:
            return None
        safety_cfg = self._liquidation_safety_config(cfg)
        try:
            common = self.get_runtime_common_settings()
        except Exception:
            common = {}
        minimum_external_buffer_pct = max(
            0.05,
            float((common or {}).get('external_stop_minimum_liquidation_buffer_pct', 0.05) or 0.05),
        )
        return validate_stop_against_liquidation(
            side,
            stop_price,
            liquidation_price,
            tick_size,
            safety_cfg.minimum_buffer_pct,
            safety_cfg.minimum_buffer_ticks,
            'CONTRACT_PRICE',
            entry_price,
            accepted_working_types={'MARK_PRICE', 'CONTRACT_PRICE'},
            non_mark_minimum_buffer_pct=minimum_external_buffer_pct,
            non_mark_buffer_multiplier=2,
        )

    def _protection_order_type(self, order):
''',
    "runtime existing stop validator",
)
replace_once(
    emas,
    '''        runner_state = getattr(self, 'utbreakout_trailing_states', {}).get(symbol)
        managed_sl_active = (
''',
    '''        runner_state = getattr(self, 'utbreakout_trailing_states', {}).get(symbol)
        tracked_records = []
        try:
            if hasattr(self, 'trading_state_store'):
                tracked_records = self.trading_state_store.active_for_symbol(symbol) or []
        except Exception:
            tracked_records = []
        external_position = not isinstance(runner_state, dict) and not bool(tracked_records)
        status['external_position'] = bool(external_position)
        if external_position:
            expected_tp = False
            status['tp_expected'] = False
        managed_sl_active = (
''',
    "runtime external position detection",
)
replace_once(
    emas,
    '''                    liquidation_result = self._validate_position_stop_liquidation(
                        symbol,
                        pos,
                        order_price,
                        working_type,
                    )
''',
    '''                    liquidation_result = self._validate_existing_position_stop_liquidation(
                        symbol,
                        pos,
                        order_price,
                        working_type,
                        order,
                    )
''',
    "runtime existing stop call",
)
replace_once(
    emas,
    '''                        if liquidation_result is None:
                            status['liquidation_safety'] = 'UNKNOWN'
                            status['liquidation_safety_reason'] = 'LIQUIDATION_PRICE_UNAVAILABLE'
                            status['stop_working_type'] = working_type or 'UNKNOWN'
                            liquidation_unsafe_orders.append(order)
                            continue
                        liquidation_safety_results.append(liquidation_result)
                        status['liquidation_price'] = float(liquidation_result.liquidation_price)
                        status['liquidation_buffer_pct'] = float(liquidation_result.buffer_pct)
                        status['stop_price'] = float(liquidation_result.stop_price)
                        status['stop_working_type'] = liquidation_result.working_type
                        if not liquidation_result.valid:
                            status['liquidation_safety'] = 'UNSAFE'
                            status['liquidation_safety_reason'] = liquidation_result.reason
                            liquidation_unsafe_orders.append(order)
                            continue
                        status['liquidation_safety'] = 'SAFE'
                        status['liquidation_safety_reason'] = liquidation_result.reason
''',
    '''                        if liquidation_result is None:
                            status['liquidation_safety'] = 'UNKNOWN'
                            status['liquidation_safety_reason'] = 'LIQUIDATION_PRICE_UNAVAILABLE'
                            status['stop_working_type'] = working_type or 'UNKNOWN'
                            if external_position:
                                status['liquidation_safety'] = 'UNVERIFIED_EXTERNAL_SL_PRESERVED'
                                status['liquidation_safety_reason'] = (
                                    'LIQUIDATION_PRICE_UNAVAILABLE_EXTERNAL_SL_PRESERVED'
                                )
                                self._set_crypto_entry_lock(
                                    f'FILLED_UNVERIFIED_LIQUIDATION:{symbol}'
                                )
                            else:
                                liquidation_unsafe_orders.append(order)
                                continue
                        else:
                            liquidation_safety_results.append(liquidation_result)
                            status['liquidation_price'] = float(liquidation_result.liquidation_price)
                            status['liquidation_buffer_pct'] = float(liquidation_result.buffer_pct)
                            status['stop_price'] = float(liquidation_result.stop_price)
                            status['stop_working_type'] = liquidation_result.working_type
                            if not liquidation_result.valid:
                                status['liquidation_safety'] = 'UNSAFE'
                                status['liquidation_safety_reason'] = liquidation_result.reason
                                liquidation_unsafe_orders.append(order)
                                continue
                            status['liquidation_safety'] = (
                                'SAFE_EXTERNAL'
                                if str(liquidation_result.reason).startswith('SAFE_EXTERNAL_')
                                else 'SAFE'
                            )
                            status['liquidation_safety_reason'] = liquidation_result.reason
''',
    "runtime preserve external stop",
)

# 5) Regression tests based on the user's KORU short screenshot values.
liq_test = root / "tests/test_liquidation_guard.py"
text = liq_test.read_text(encoding="utf-8")
if "test_external_contract_price_stop_requires_larger_buffer" not in text:
    text += '''\n\ndef test_external_contract_price_stop_requires_larger_buffer():
    safe = validate_stop_against_liquidation(
        "short",
        "467.14",
        "518.3729034",
        "0.01",
        "0.02",
        20,
        "CONTRACT_PRICE",
        entry_price="424.6755308",
        accepted_working_types={"MARK_PRICE", "CONTRACT_PRICE"},
        non_mark_minimum_buffer_pct="0.05",
        non_mark_buffer_multiplier="2",
    )
    too_close = validate_stop_against_liquidation(
        "short",
        "500",
        "518.3729034",
        "0.01",
        "0.02",
        20,
        "CONTRACT_PRICE",
        entry_price="424.6755308",
        accepted_working_types={"MARK_PRICE", "CONTRACT_PRICE"},
        non_mark_minimum_buffer_pct="0.05",
        non_mark_buffer_multiplier="2",
    )
    assert safe.valid is True
    assert safe.reason == "SAFE_EXTERNAL_CONTRACT_PRICE"
    assert too_close.valid is False
    assert too_close.reason == "SHORT_STOP_LIQUIDATION_BUFFER_INSUFFICIENT"
'''
    liq_test.write_text(text, encoding="utf-8")

algo_test = root / "tests/test_binance_algo_gateway.py"
text = algo_test.read_text(encoding="utf-8")
if "test_normalize_preserves_manual_close_position_fields" not in text:
    text += '''\n\ndef test_normalize_preserves_manual_close_position_fields():
    normalized = BinanceAlgoOrderGateway.normalize({
        "algoId": 77,
        "clientAlgoId": "manual-stop",
        "symbol": "KORUUSDT",
        "orderType": "STOP_MARKET",
        "side": "BUY",
        "triggerPrice": "467.14",
        "closePosition": "true",
        "positionSide": "BOTH",
        "workingType": "CONTRACT_PRICE",
        "algoStatus": "NEW",
    })
    assert normalized["closePosition"] is True
    assert normalized["positionSide"] == "BOTH"
    assert normalized["triggerPrice"] == "467.14"
'''
    algo_test.write_text(text, encoding="utf-8")

reconciliation_test = root / "tests/test_liquidation_reconciliation.py"
replace_once(
    reconciliation_test,
    '''def test_restart_rejects_last_price_stop_and_missing_liquidation():
    async def scenario():
        for pos, order, issue in (
            (position(), stop("0.00021", "CONTRACT_PRICE"), "STOP_WORKING_TYPE_NOT_MARK_PRICE"),
            (position("0"), stop("0.00021"), "liquidation_price_unavailable"),
        ):
            result = await reconcile_exchange_state(
                FakeExchange(),
                SQLiteTradingStateStore(":memory:"),
                position_fetcher=lambda pos=pos: _async([pos]),
                open_orders_fetcher=lambda order=order: _async([order]),
            )
            assert result.safe_to_trade is False
            assert any(issue in value for value in result.issues)

    asyncio.run(scenario())
''',
    '''def test_restart_accepts_wide_contract_price_stop_but_rejects_missing_liquidation():
    async def scenario():
        external_stop = stop("0.00021", "CONTRACT_PRICE")
        external_stop["closePosition"] = True
        external_stop["amount"] = 0
        external_stop["reduceOnly"] = False
        store = SQLiteTradingStateStore(":memory:")
        store.upsert(
            OrderRecord(
                client_order_id="entry-external-stop",
                symbol="HMSTR/USDT:USDT",
                side="LONG",
                strategy="UTBREAKOUT",
                signal_timestamp="1",
                requested_qty=1000,
                filled_qty=1000,
                order_state=OrderState.FILLED_UNPROTECTED.value,
            )
        )
        accepted = await reconcile_exchange_state(
            FakeExchange(),
            store,
            position_fetcher=lambda: _async([position()]),
            open_orders_fetcher=lambda: _async([external_stop]),
        )
        assert accepted.safe_to_trade is True
        assert store.get("entry-external-stop").order_state == OrderState.PROTECTED.value

        missing = await reconcile_exchange_state(
            FakeExchange(),
            SQLiteTradingStateStore(":memory:"),
            position_fetcher=lambda: _async([position("0")]),
            open_orders_fetcher=lambda: _async([stop("0.00021")]),
        )
        assert missing.safe_to_trade is False
        assert any("liquidation_price_unavailable" in value for value in missing.issues)

    asyncio.run(scenario())
''',
    "reconciliation manual stop test",
)

helpers_test = root / "tests/test_utbreakout_helpers.py"
text = helpers_test.read_text(encoding="utf-8")
if "from unittest.mock import AsyncMock" not in text:
    text = text.replace("import re\n", "import re\nfrom unittest.mock import AsyncMock\n", 1)
if "test_manual_short_close_position_sl_is_preserved_and_not_force_closed" not in text:
    text += '''\n\ndef test_manual_short_close_position_sl_is_preserved_and_not_force_closed():
    pos = {
        "symbol": "KORU/USDT:USDT",
        "side": "short",
        "contracts": "60.11",
        "entryPrice": "424.6755308",
        "markPrice": "443.45",
        "liquidationPrice": "518.3729034",
    }
    manual_sl = {
        "id": "manual-koru-sl",
        "symbol": "KORU/USDT:USDT",
        "side": "buy",
        "type": "stop_market",
        "amount": None,
        "reduceOnly": False,
        "closePosition": True,
        "workingType": "CONTRACT_PRICE",
        "triggerPrice": "467.14",
        "info": {
            "symbol": "KORUUSDT",
            "origType": "STOP_MARKET",
            "triggerPrice": "467.14",
            "closePosition": "true",
            "workingType": "CONTRACT_PRICE",
        },
    }
    engine = _protection_engine([manual_sl], positions=[pos])
    engine.exchange.id = "binance"
    engine.exchange.market = lambda symbol: {
        "precision": {"price": 0.01},
        "info": {"filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"}]},
    }
    engine.exchange.fapiPrivateGetOpenAlgoOrders = lambda params=None: {"orders": []}
    engine.get_runtime_common_settings = lambda: {}
    engine._set_crypto_entry_lock = lambda reason: setattr(engine, "crypto_entry_lock_reason", reason)
    engine._handle_liquidation_safety_failure = AsyncMock()

    status = asyncio.run(
        engine._audit_protection_orders(
            "KORU/USDT",
            pos=pos,
            expected_tp=None,
            expected_sl=True,
            alert=True,
        )
    )

    assert status["status"] == "OK"
    assert status["external_position"] is True
    assert status["tp_expected"] is False
    assert status["sl_present"] is True
    assert status["liquidation_safety"] == "SAFE_EXTERNAL"
    assert status["liquidation_safety_reason"] == "SAFE_EXTERNAL_CONTRACT_PRICE"
    assert engine.exchange.cancelled == []
    assert not [order for order in engine.exchange.created if order["type"] == "market"]
    engine._handle_liquidation_safety_failure.assert_not_awaited()


def test_manual_external_sl_is_preserved_when_liquidation_price_is_temporarily_missing():
    pos = {
        "symbol": "KORU/USDT:USDT",
        "side": "short",
        "contracts": "60.11",
        "entryPrice": "424.6755308",
        "markPrice": "443.45",
        "liquidationPrice": "0",
    }
    manual_sl = {
        "id": "manual-koru-sl-no-liq",
        "symbol": "KORU/USDT:USDT",
        "side": "buy",
        "type": "stop_market",
        "closePosition": True,
        "workingType": "CONTRACT_PRICE",
        "triggerPrice": "467.14",
        "info": {
            "symbol": "KORUUSDT",
            "origType": "STOP_MARKET",
            "triggerPrice": "467.14",
            "closePosition": "true",
            "workingType": "CONTRACT_PRICE",
        },
    }
    engine = _protection_engine([manual_sl], positions=[pos])
    engine.exchange.id = "binance"
    engine.exchange.market = lambda symbol: {
        "precision": {"price": 0.01},
        "info": {"filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"}]},
    }
    engine.exchange.fapiPrivateGetOpenAlgoOrders = lambda params=None: {"orders": []}
    engine.get_runtime_common_settings = lambda: {}
    engine._set_crypto_entry_lock = lambda reason: setattr(engine, "crypto_entry_lock_reason", reason)
    engine._handle_liquidation_safety_failure = AsyncMock()

    status = asyncio.run(
        engine._audit_protection_orders(
            "KORU/USDT",
            pos=pos,
            expected_tp=False,
            expected_sl=True,
            alert=True,
        )
    )

    assert status["status"] == "OK"
    assert status["sl_present"] is True
    assert status["liquidation_safety"] == "UNVERIFIED_EXTERNAL_SL_PRESERVED"
    assert "FILLED_UNVERIFIED_LIQUIDATION" in engine.crypto_entry_lock_reason
    assert engine.exchange.cancelled == []
    assert not [order for order in engine.exchange.created if order["type"] == "market"]
    engine._handle_liquidation_safety_failure.assert_not_awaited()
'''
helpers_test.write_text(text, encoding="utf-8")
