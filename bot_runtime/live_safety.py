"""Trading-safety runtime, liquidation gates, and entry-state recording."""

from __future__ import annotations

def _ensure_trading_safety_runtime(self):
    owner = getattr(self, "ctrl", None) or self
    store = (
        getattr(owner, "trading_state_store", None)
        or getattr(self, "trading_state_store", None)
    )
    if store is None:
        exchange_id = str(getattr(getattr(self, "exchange", None), "id", "") or "").lower()
        runtime_dir = getattr(owner, "runtime_dir", None) or getattr(self, "runtime_dir", None)
        uninitialized_signal_engine = (
            self.__class__.__name__ == "SignalEngine"
            and not hasattr(self, "crypto_entry_lock_reason")
        )
        default_state_path = (
            os.path.join(str(runtime_dir), "trading_state.sqlite3")
            if runtime_dir
            else ":memory:"
            if exchange_id == "test" or uninitialized_signal_engine
            else "runtime/trading_state.sqlite3"
        )
        state_path = os.getenv("CRYPTO_TRADING_STATE_DB", default_state_path)
        store = SQLiteTradingStateStore(state_path)
    owner.trading_state_store = store
    self.trading_state_store = store
    if not getattr(owner, "_engine_performance_stats_restored", False):
        _restore_engine_performance_stats(store)
        owner._engine_performance_stats_restored = True
    self._engine_performance_stats_restored = True
    gateway = (
        getattr(owner, "idempotent_order_gateway", None)
        or getattr(self, "idempotent_order_gateway", None)
    )
    if gateway is None or gateway.exchange is not getattr(self, "exchange", None):
        async def _position_fetcher(symbol):
            if not hasattr(self, "position_cache"):
                self.position_cache = None
            if not hasattr(self, "position_cache_time"):
                self.position_cache_time = 0
            getter = getattr(self, "get_server_position")
            try:
                return await getter(symbol, use_cache=False)
            except TypeError:
                return await getter(symbol)
        gateway = IdempotentOrderGateway(
            self.exchange,
            store,
            position_fetcher=_position_fetcher,
        )
        owner.idempotent_order_gateway = gateway
    self.idempotent_order_gateway = gateway
    service = getattr(owner, 'crypto_execution_service', None)
    if service is None or service.exchange is not getattr(self, 'exchange', None):
        service = CryptoExecutionService(
            self.exchange,
            store,
            gateway,
            BinanceAlgoOrderGateway(self.exchange),
            accounting_service=TradeAccountingFinalizer(self.exchange, store),
        )
        owner.crypto_execution_service = service
    return gateway


def _extract_liquidation_stop_price(self, side, entry_price, payload=None, leverage=None):
    payload = payload if isinstance(payload, dict) else {}
    for key in ('stop_loss', 'stop_loss_price', 'sl_price', 'initial_sl_price', 'stop_price'):
        value = _safe_float_or_none(payload.get(key))
        if value is not None and value > 0:
            return value
    risk_distance = _safe_float_or_none(payload.get('risk_distance'))
    if risk_distance is not None and risk_distance > 0:
        return (
            float(entry_price) - risk_distance
            if str(side).lower() == 'long'
            else float(entry_price) + risk_distance
        )
    stop_loss_pct = _safe_float_or_none(payload.get('stop_loss_pct'))
    if stop_loss_pct is not None and stop_loss_pct > 0 and leverage:
        distance = float(entry_price) * (stop_loss_pct / 100.0) / max(float(leverage), 1.0)
        return float(entry_price) - distance if str(side).lower() == 'long' else float(entry_price) + distance
    return None


def _binance_symbol_id(self, symbol):
    try:
        market = self.exchange.market(symbol)
    except Exception:
        market = {}
    if isinstance(market, dict) and market.get('id'):
        return str(market.get('id'))
    return self._normalize_protection_symbol(symbol)


async def _preflight_liquidation_safety(self, symbol, side, entry_price, stop_price, leverage, qty, cfg=None):
    if self.is_upbit_mode() or str(getattr(self.exchange, 'id', '') or '').lower() != 'binance':
        return {'valid': True, 'status': 'NOT_BINANCE_FUTURES', 'selected_leverage': int(leverage)}
    if stop_price is None or float(stop_price or 0.0) <= 0:
        return {'valid': False, 'status': 'LIQUIDATION_SAFETY_UNKNOWN', 'reason': 'STOP_PRICE_UNAVAILABLE'}

    safety_cfg = self._liquidation_safety_config(cfg)
    symbol_id = _binance_symbol_id(self, symbol)
    tick_size = self._liquidation_tick_size(symbol)
    if tick_size <= 0:
        return {'valid': False, 'status': 'LIQUIDATION_SAFETY_UNKNOWN', 'reason': 'TICK_SIZE_UNAVAILABLE'}

    symbol_config_method = getattr(self.exchange, 'fapiPrivateGetSymbolConfig', None)
    bracket_method = getattr(self.exchange, 'fapiPrivateGetLeverageBracket', None)
    if not callable(symbol_config_method) or not callable(bracket_method):
        return {'valid': False, 'status': 'LIQUIDATION_SAFETY_UNKNOWN', 'reason': 'BINANCE_RISK_ENDPOINT_UNAVAILABLE'}
    try:
        symbol_config_raw = await asyncio.to_thread(symbol_config_method, {'symbol': symbol_id})
        bracket_raw = await asyncio.to_thread(bracket_method, {'symbol': symbol_id})
    except Exception as exc:
        logger.exception("Liquidation safety preflight exchange lookup failed for %s", symbol)
        return {
            'valid': False,
            'status': 'LIQUIDATION_SAFETY_UNKNOWN',
            'reason': f'BINANCE_RISK_LOOKUP_FAILED:{type(exc).__name__}',
        }

    symbol_rows = symbol_config_raw if isinstance(symbol_config_raw, list) else [symbol_config_raw]
    symbol_config = next(
        (row for row in symbol_rows if isinstance(row, dict) and str(row.get('symbol') or symbol_id) == symbol_id),
        None,
    )
    if not isinstance(symbol_config, dict):
        return {'valid': False, 'status': 'LIQUIDATION_SAFETY_UNKNOWN', 'reason': 'SYMBOL_CONFIG_UNAVAILABLE'}
    margin_type = str(symbol_config.get('marginType') or '').upper()
    configured_leverage = int(float(symbol_config.get('leverage') or 0))
    if margin_type != 'ISOLATED':
        return {
            'valid': False,
            'status': 'LIQUIDATION_SAFETY_UNKNOWN',
            'reason': f'MARGIN_MODE_NOT_ISOLATED:{margin_type or "UNKNOWN"}',
        }
    if configured_leverage != int(leverage):
        return {
            'valid': False,
            'status': 'LIQUIDATION_SAFETY_UNKNOWN',
            'reason': f'LEVERAGE_NOT_CONFIRMED:{configured_leverage}!={int(leverage)}',
        }

    bracket_rows = bracket_raw if isinstance(bracket_raw, list) else [bracket_raw]
    bracket_entry = next(
        (row for row in bracket_rows if isinstance(row, dict) and str(row.get('symbol') or symbol_id) == symbol_id),
        None,
    )
    notional = abs(float(entry_price) * float(qty))
    selected_bracket = None
    for bracket in (bracket_entry or {}).get('brackets', []) if isinstance(bracket_entry, dict) else []:
        try:
            floor = float(bracket.get('notionalFloor') or 0.0)
            cap = float(bracket.get('notionalCap') or float('inf'))
        except (TypeError, ValueError):
            continue
        if floor <= notional <= cap:
            selected_bracket = bracket
            break
    mmr = _safe_float_or_none((selected_bracket or {}).get('maintMarginRatio'))
    if mmr is None or mmr < 0:
        return {'valid': False, 'status': 'LIQUIDATION_SAFETY_UNKNOWN', 'reason': 'MAINTENANCE_MARGIN_RATE_UNAVAILABLE'}

    candidate_leverages = [int(leverage)]
    if safety_cfg.auto_reduce_leverage:
        candidate_leverages.extend(range(int(leverage) - 1, 0, -1))
    last_result = None
    for candidate_leverage in candidate_leverages:
        estimated_liquidation = estimate_isolated_liquidation_price(
            side,
            entry_price,
            candidate_leverage,
            mmr,
        )
        result = validate_stop_against_liquidation(
            side,
            stop_price,
            estimated_liquidation,
            tick_size,
            safety_cfg.minimum_buffer_pct,
            safety_cfg.minimum_buffer_ticks,
            safety_cfg.stop_working_type,
            entry_price,
        )
        last_result = result
        if result.valid:
            snapshot = {
                'valid': True,
                'status': 'SAFE',
                'reason': result.reason,
                'symbol': symbol,
                'side': str(side).lower(),
                'entry_price': float(result.entry_price),
                'stop_price': float(result.stop_price),
                'liquidation_price': float(result.liquidation_price),
                'buffer_pct': float(result.buffer_pct),
                'working_type': result.working_type,
                'selected_leverage': candidate_leverage,
                'margin_type': margin_type,
                'estimated': True,
            }
            _ensure_trading_safety_runtime(self)
            self.trading_state_store.set_runtime_state(f'liquidation_safety:{symbol}', snapshot)
            return snapshot
    return {
        'valid': False,
        'status': 'ENTRY_REJECTED_LIQUIDATION_CONFLICT',
        'reason': last_result.reason if last_result is not None else 'LIQUIDATION_SAFETY_UNKNOWN',
        'selected_leverage': None,
        'liquidation_price': float(last_result.liquidation_price) if last_result is not None else None,
        'stop_price': float(stop_price),
        'working_type': safety_cfg.stop_working_type,
        'estimated': True,
    }


async def _fetch_position_with_liquidation(self, symbol, initial_position=None):
    if str(getattr(self.exchange, 'id', '') or '').lower() != 'binance':
        if initial_position is not None:
            return True, self._normalize_server_position(initial_position, symbol) or initial_position
        return await self._fetch_server_position_checked(symbol)
    delays = (0.0, 0.5, 1.0, 2.0, 4.0, 8.0)
    candidate = initial_position
    last_fetch_ok = candidate is not None
    for delay in delays:
        if delay:
            await asyncio.sleep(delay)
        if candidate is not None:
            normalized = self._normalize_server_position(candidate, symbol) or candidate
            liquidation = _safe_float_or_none(
                normalized.get('liquidationPrice')
                or normalized.get('liquidation_price')
                or self._protection_order_info(normalized).get('liquidationPrice')
            )
            if liquidation is not None and liquidation > 0:
                return True, normalized
        last_fetch_ok, candidate = await self._fetch_server_position_checked(symbol)
        if last_fetch_ok and not candidate:
            return True, None
    return bool(last_fetch_ok), candidate


async def _handle_liquidation_safety_failure(
    self,
    symbol,
    pos,
    reason,
    *,
    stop_price=None,
    working_type=None,
    cfg=None,
    client_order_id=None,
):
    _ensure_trading_safety_runtime(self)
    state = (
        OrderState.FILLED_LIQUIDATION_CONFLICT
        if reason not in {'LIQUIDATION_PRICE_UNAVAILABLE', 'LIQUIDATION_SAFETY_UNKNOWN'}
        else OrderState.FILLED_UNVERIFIED_LIQUIDATION
    )
    records = self.trading_state_store.active_for_symbol(symbol)
    for record in records:
        if client_order_id and record.client_order_id != client_order_id:
            continue
        self.trading_state_store.transition(record.client_order_id, state, last_error=str(reason))
    self._set_crypto_entry_lock(f'{state.value}:{symbol}:{reason}')
    self.critical_pause_reason = f'Liquidation safety conflict: {reason}'
    if getattr(self, 'ctrl', None) is not None and hasattr(self.ctrl, 'is_paused'):
        self.ctrl.is_paused = True
    self._set_crypto_entry_lock(f'CRITICAL_PAUSE:LIQUIDATION_SAFETY_CONFLICT:{symbol}')
    liquidation_price = _safe_float_or_none(
        (pos or {}).get('liquidationPrice')
        or (pos or {}).get('liquidation_price')
        or self._protection_order_info(pos or {}).get('liquidationPrice')
    )
    mark_price = _safe_float_or_none((pos or {}).get('markPrice') or (pos or {}).get('mark_price'))
    side = str((pos or {}).get('side') or '').lower()
    qty = abs(float(self._position_signed_contracts(pos or {}) or (pos or {}).get('contracts', 0) or 0))
    await self.ctrl.notify(
        "[긴급] 스탑로스·청산가 충돌\n"
        f"심볼: {self.ctrl.format_symbol_for_display(symbol)}\n"
        f"방향: {side.upper() or 'UNKNOWN'}\n"
        f"수량: {qty}\nMark Price: {mark_price}\n"
        f"스탑로스: {stop_price}\n청산가: {liquidation_price}\n"
        f"스탑 트리거 기준: {working_type or 'UNKNOWN'}\n"
        f"상태: {state.value}\n자동 대응: reduceOnly 긴급 청산\n신규 진입 잠금: ON"
    )
    await self._cancel_protection_orders(symbol, reason='liquidation safety conflict')
    close_status = await self._emergency_close_position_without_stop_loss(
        symbol,
        reason=f'liquidation safety failure: {reason}',
        max_attempts=5,
        lockout_reason='LIQUIDATION_SAFETY_FORCE_CLOSED',
        protection_label='LIQUIDATION_SAFETY',
        critical_pause_reason_code='LIQUIDATION_SAFETY_EMERGENCY_CLOSE_FAILED',
        persist_critical_pause=False,
    )
    if not close_status.get('closed'):
        for record in self.trading_state_store.active_for_symbol(symbol):
            self.trading_state_store.transition(
                record.client_order_id,
                OrderState.EMERGENCY_CLOSE_FAILED,
                last_error=str(close_status.get('error') or reason),
            )
    self.critical_pause_status = dict(close_status)
    try:
        write_critical_pause_state(
            symbol,
            'LIQUIDATION_SAFETY_CONFLICT',
            reason,
            cfg or {},
            scope="GLOBAL",
            reason_code="LIQUIDATION_SAFETY_CONFLICT",
            origin_symbol=symbol,
        )
    except Exception:
        logger.exception("Failed to persist liquidation safety critical pause")
    close_status['liquidation_safety_reason'] = reason
    return close_status


async def _verify_actual_liquidation_safety(
    self,
    symbol,
    side,
    stop_price,
    pos,
    cfg=None,
    client_order_id=None,
):
    if self.is_upbit_mode() or str(getattr(self.exchange, 'id', '') or '').lower() != 'binance':
        return {'valid': True, 'status': 'NOT_BINANCE_FUTURES', 'position': pos}
    if client_order_id:
        _mark_crypto_entry_state(
            self,
            client_order_id,
            OrderState.FILLED_UNVERIFIED_LIQUIDATION,
        )
    self._set_crypto_entry_lock(f'FILLED_UNVERIFIED_LIQUIDATION:{symbol}')
    fetch_ok, actual_pos = await _fetch_position_with_liquidation(self, symbol, pos)
    if not fetch_ok or not actual_pos:
        reason = 'LIQUIDATION_PRICE_UNAVAILABLE'
        close_status = await _handle_liquidation_safety_failure(
            self,
            symbol,
            actual_pos or pos,
            reason,
            stop_price=stop_price,
            working_type='MARK_PRICE',
            cfg=cfg,
            client_order_id=client_order_id,
        )
        return {'valid': False, 'status': reason, 'close_status': close_status, 'position': actual_pos or pos}
    result = self._validate_position_stop_liquidation(
        symbol,
        actual_pos,
        stop_price,
        'MARK_PRICE',
        cfg,
    )
    if result is None:
        reason = 'LIQUIDATION_PRICE_UNAVAILABLE'
    elif not result.valid:
        reason = result.reason
    else:
        snapshot = {
            'valid': True,
            'status': 'SAFE',
            'reason': result.reason,
            'symbol': symbol,
            'side': str(side).lower(),
            'entry_price': float(result.entry_price),
            'mark_price': _safe_float_or_none(actual_pos.get('markPrice')),
            'stop_price': float(result.stop_price),
            'liquidation_price': float(result.liquidation_price),
            'buffer_pct': float(result.buffer_pct),
            'working_type': result.working_type,
            'margin_type': actual_pos.get('marginType'),
            'leverage': actual_pos.get('leverage'),
            'estimated': False,
        }
        _ensure_trading_safety_runtime(self)
        self.trading_state_store.set_runtime_state(f'liquidation_safety:{symbol}', snapshot)
        if client_order_id:
            _mark_crypto_entry_state(self, client_order_id, OrderState.FILLED_UNPROTECTED)
        self._set_crypto_entry_lock(f'FILLED_UNPROTECTED:{client_order_id or symbol}')
        return {**snapshot, 'position': actual_pos, 'result': result}
    close_status = await _handle_liquidation_safety_failure(
        self,
        symbol,
        actual_pos,
        reason,
        stop_price=stop_price,
        working_type='MARK_PRICE',
        cfg=cfg,
        client_order_id=client_order_id,
    )
    return {'valid': False, 'status': reason, 'close_status': close_status, 'position': actual_pos, 'result': result}


def _resolve_crypto_signal_timestamp(self, symbol, payload=None):
    payload = payload if isinstance(payload, dict) else {}
    for key in (
        "decision_candle_ts", "signal_timestamp", "signal_ts", "signal_candle_ts",
        "candle_timestamp", "closed_candle_ts", "last_closed_candle_ts",
    ):
        value = payload.get(key)
        if value not in (None, ""):
            return value
    for mapping_name in (
        "last_processed_candle_ts",
        "utbreakout_adaptive_last_decision_ts",
        "utbreakout_last_ready_ts",
    ):
        mapping = getattr(self, mapping_name, None)
        if not isinstance(mapping, dict):
            continue
        value = mapping.get(symbol)
        if isinstance(value, dict):
            values = [item for item in value.values() if isinstance(item, (int, float))]
            value = max(values) if values else None
        if value not in (None, ""):
            return value
    timeframe_seconds = 15 * 60
    timeframe = str(payload.get("timeframe") or payload.get("entry_tf") or "15m").lower()
    match = re.fullmatch(r"(\d+)([mhd])", timeframe)
    if match:
        unit_seconds = {"m": 60, "h": 3600, "d": 86400}[match.group(2)]
        timeframe_seconds = max(60, int(match.group(1)) * unit_seconds)
    now = int(time.time())
    return now - (now % timeframe_seconds)


def _crypto_entry_block_reason(self, symbol=None, *, include_critical_pause=True):
    _ensure_trading_safety_runtime(self)
    uninitialized_test_engine = (
        self.__class__.__name__ == 'SignalEngine'
        and not hasattr(self, 'crypto_entry_lock_reason')
    )
    if include_critical_pause:
        critical_pause = None if uninitialized_test_engine else load_critical_pause_state()
        if symbol:
            decision = evaluate_critical_pause_block(state=critical_pause, requested_symbol=symbol)
            if decision.blocked:
                return f"CRITICAL_PAUSE:{decision.reason_code}"
        else:
            if critical_pause and str(critical_pause.get('status')).upper().strip() == 'CRITICAL_PAUSED':
                return f"CRITICAL_PAUSE:{critical_pause.get('reason') or 'manual reconciliation required'}"

    runtime_reason = getattr(self, "crypto_entry_lock_reason", None)
    if runtime_reason:
        return str(runtime_reason)
    if getattr(getattr(self, "ctrl", None), "is_paused", False):
        return "CRITICAL_PAUSE_OR_BOT_PAUSED"
    persisted = self.trading_state_store.get_runtime_state("entry_lock_reason")
    if persisted:
        return str(persisted)
    return self.trading_state_store.entry_block_reason(symbol)


def _crypto_safety_status_lines(self):
    try:
        _ensure_trading_safety_runtime(self)
        lock_reason = _crypto_entry_block_reason(self)
        stream = self.trading_state_store.get_runtime_state(
            "user_data_stream",
            {"connected": False, "reason": "not_started"},
        )
        reconciliation = self.trading_state_store.get_runtime_state(
            "last_reconciliation",
            {},
        )
        last_stream_event = self.trading_state_store.get_runtime_state(
            "last_user_stream_event",
            {},
        )
        critical_pause = load_critical_pause_state()
        active = self.trading_state_store.list_by_states(
            {
                OrderState.SUBMITTED_UNKNOWN,
                OrderState.PARTIALLY_FILLED,
                OrderState.FILLED_UNVERIFIED_LIQUIDATION,
                OrderState.FILLED_LIQUIDATION_CONFLICT,
                OrderState.FILLED_UNPROTECTED,
                OrderState.PROTECTED,
                OrderState.CLOSING,
                OrderState.EMERGENCY_CLOSE_FAILED,
            }
        )
        active_text = ", ".join(
            f"{item.symbol}:{item.order_state}"
            for item in active[:4]
        ) or "none"
        lines = [
            "Trading Safety:",
            f"entry_lock={lock_reason or 'none'}",
            f"user_stream_connected={bool((stream or {}).get('connected'))} / reason={(stream or {}).get('reason') or 'n/a'}",
            f"reconciliation_safe={bool((reconciliation or {}).get('safe_to_trade'))} / last={(reconciliation or {}).get('reconciled_at') or 'n/a'}",
            f"exchange_positions={len((reconciliation or {}).get('positions') or [])} / exchange_open_orders={len((reconciliation or {}).get('open_orders') or [])}",
            f"local_active_orders={active_text}",
            f"critical_pause={bool(critical_pause)} / last_user_event={(last_stream_event or {}).get('event_type') or 'none'}",
        ]
        positions = (reconciliation or {}).get('positions') or []
        if positions:
            raw_pos = positions[0]
            pos = self._normalize_server_position(raw_pos) or raw_pos
            symbol = self._protection_position_symbol(pos)
            snapshot = self.trading_state_store.get_runtime_state(f'liquidation_safety:{symbol}', {}) or {}
            protection = (getattr(self, 'last_protection_order_status', {}) or {}).get(symbol, {}) or {}
            lines.extend([
                "Liquidation Safety:",
                f"symbol={symbol} / entry={pos.get('entryPrice')} / mark={pos.get('markPrice')}",
                f"liquidation={pos.get('liquidationPrice')} / stop={protection.get('stop_price') or snapshot.get('stop_price')}",
                f"working_type={protection.get('stop_working_type') or snapshot.get('working_type') or 'UNKNOWN'} / safety={protection.get('liquidation_safety') or snapshot.get('status') or 'UNKNOWN'}",
                f"reason={protection.get('liquidation_safety_reason') or snapshot.get('reason') or 'not_evaluated'}",
            ])
        return lines
    except Exception as exc:
        logger.exception("Trading safety status rendering failed")
        return ["Trading Safety: unknown", f"reason={type(exc).__name__}"]


def _resolve_entry_block_context(owner):
    engine = None
    controller = None

    if isinstance(owner, SignalEngine):
        engine = owner
        controller = getattr(owner, "ctrl", None)
    else:
        controller = owner
        engine = getattr(owner, "active_engine", None)
        if engine is None:
            engines = getattr(owner, "engines", None)
            if isinstance(engines, dict):
                engine = engines.get(CORE_ENGINE) or engines.get("signal")
                if engine is None:
                    engine = next((item for item in engines.values() if isinstance(item, SignalEngine)), None)

    runtime_owner = engine or owner
    try:
        if runtime_owner is not None:
            _ensure_trading_safety_runtime(runtime_owner)
    except Exception:
        logger.exception("Failed to initialize trading safety runtime for entry block handling")

    notifier = getattr(controller, "notify", None) if controller is not None else None
    if notifier is None and engine is not None:
        notifier = getattr(getattr(engine, "ctrl", None), "notify", None)

    store = getattr(owner, "trading_state_store", None)
    if store is None and controller is not None:
        store = getattr(controller, "trading_state_store", None)
    if store is None and engine is not None:
        store = getattr(engine, "trading_state_store", None)

    trace_callback = getattr(engine, "_utbreakout_trace_event", None) if engine is not None else None
    return engine, controller, notifier, store, trace_callback


def _record_entry_block_state(
    owner,
    *,
    symbol,
    raw_symbol,
    side,
    reason_text,
    trace_code,
    qty,
    phase,
    trace_extra=None,
):
    engine, controller, notifier, store, trace_callback = _resolve_entry_block_context(owner)
    last_reasons = getattr(engine, "last_entry_reason", None) if engine is not None else None
    if isinstance(last_reasons, dict):
        last_reasons[symbol] = reason_text
        if raw_symbol and raw_symbol != symbol:
            last_reasons[raw_symbol] = reason_text
    else:
        logger.warning("Entry block reason store unavailable for symbol=%s", symbol)

    if callable(trace_callback):
        try:
            data = {
                "side": side,
                "qty": qty,
                "reason": reason_text,
                "phase": phase,
                "order_sent": False,
            }
            if trace_extra:
                data.update(trace_extra)
            trace_callback(symbol, "ENTRY_BLOCKED", trace_code, **data)
        except Exception:
            logger.exception("Failed to record ENTRY_BLOCKED trace for %s", symbol)
    else:
        logger.warning("ENTRY_BLOCKED trace callback unavailable for symbol=%s", symbol)
    return engine, controller, notifier, store


async def _handle_critical_pause_entry_block(
    owner,
    *,
    symbol: str,
    raw_symbol: str,
    side: str,
    decision: CriticalPauseBlockDecision,
    qty: float | None = None,
    phase: str = "ENTRY",
) -> None:
    reason_text = f"CRITICAL_PAUSE:{decision.reason_code}"
    engine, controller, notifier, store = _record_entry_block_state(
        owner,
        symbol=symbol,
        raw_symbol=raw_symbol,
        side=side,
        reason_text=reason_text,
        trace_code="CRITICAL_PAUSE",
        qty=qty,
        phase=phase,
        trace_extra={
            "pause_id": decision.pause_id,
            "scope": decision.scope,
            "origin_symbol": decision.origin_symbol,
        },
    )

    if store is None:
        logger.warning("Critical pause notice store unavailable; notification skipped")
        return

    notice_key = build_critical_pause_notice_key(decision, requested_symbol=symbol)
    claim_payload = {
        "status": "SENDING",
        "pause_id": decision.pause_id,
        "reason_code": decision.reason_code,
        "scope": decision.scope,
        "origin_symbol": decision.origin_symbol,
        "requested_symbol": symbol,
        "side": side,
        "qty": qty,
        "phase": phase,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        claimed = store.create_runtime_state_if_absent(notice_key, claim_payload)
    except Exception:
        logger.exception("Failed to claim critical pause notification")
        return
    if not claimed:
        return

    if not callable(notifier):
        logger.warning("Critical pause notifier unavailable; releasing notice claim")
        try:
            store.delete_runtime_state(notice_key)
        except Exception:
            logger.exception("Failed to release critical pause notice claim")
        return

    qty_text = "계산 전 차단" if qty is None else str(qty)
    message = (
        "⛔ UTBreakout 진입 차단 — 주문 미전송\n\n"
        f"요청 심볼: {symbol}\n"
        f"요청 방향: {side}\n"
        f"주문 수량: {qty_text}\n"
        f"차단 사유: {decision.reason_code}\n"
        f"정지 범위: {decision.scope}\n"
        f"정지 발생 심볼: {decision.origin_symbol}\n"
        f"차단 단계: {phase}\n"
        "실제 Binance 주문 전송: 없음"
    )
    try:
        notify_result = notifier(message)
        if inspect.isawaitable(notify_result):
            await notify_result
    except Exception:
        logger.exception("Critical pause notification failed")
        try:
            store.delete_runtime_state(notice_key)
        except Exception:
            logger.exception("Failed to release exact critical pause notice claim")
        return

    sent_payload = dict(claim_payload)
    sent_payload["status"] = "SENT"
    sent_payload["sent_at"] = datetime.now(timezone.utc).isoformat()
    try:
        store.set_runtime_state(notice_key, sent_payload)
    except Exception:
        logger.exception("Failed to mark critical pause notice as SENT")


async def _handle_noncritical_entry_block(
    owner,
    *,
    symbol: str,
    raw_symbol: str,
    side: str,
    reason: str,
    qty: float | None = None,
    phase: str = "ENTRY",
) -> None:
    _record_entry_block_state(
        owner,
        symbol=symbol,
        raw_symbol=raw_symbol,
        side=side,
        reason_text=f"ENTRY_BLOCKED:{reason}",
        trace_code="ENTRY_BLOCKED",
        qty=qty,
        phase=phase,
    )
    logger.info("Entry blocked before exchange submission: symbol=%s reason=%s phase=%s", symbol, reason, phase)

async def _submit_idempotent_crypto_entry(self, symbol, side, qty, strategy, payload=None):
    custom_mode_enabled = False
    custom_mode_reader = getattr(self, 'is_user_custom_entry_mode_enabled', None)
    if callable(custom_mode_reader):
        try:
            custom_mode_enabled = bool(custom_mode_reader())
        except Exception:
            custom_mode_enabled = False
    if custom_mode_enabled and str(strategy or '').strip().upper() != 'USER_CUSTOM':
        return EntrySubmitOutcome.entry_block(
            'USER_CUSTOM_MODE_ACTIVE: automatic strategy entry is suspended'
        )

    pause_state = load_critical_pause_state()
    pause_decision = evaluate_critical_pause_block(state=pause_state, requested_symbol=symbol)
    if pause_decision.blocked:
        return EntrySubmitOutcome.critical_block(pause_decision)

    blocker = _crypto_entry_block_reason(self, symbol, include_critical_pause=False)
    if blocker:
        return EntrySubmitOutcome.entry_block(str(blocker))

    _ensure_trading_safety_runtime(self)
    owner = getattr(self, 'ctrl', None) or self
    result = await owner.crypto_execution_service.submit_entry(
        strategy=strategy,
        symbol=symbol,
        side=side,
        signal_timestamp=_resolve_crypto_signal_timestamp(self, symbol, payload),
        qty=qty,
    )

    if result is None:
        raise RuntimeError("ENTRY_SUBMISSION_RESULT_MISSING")

    raw_state = getattr(result, "state", None)
    state_value = getattr(raw_state, "value", raw_state)
    state_name = str(state_value or "").strip().upper()
    accepted = bool(getattr(result, "accepted", False))
    recovered = bool(getattr(result, "recovered", False))
    client_order_id = getattr(result, "client_order_id", None)
    result_error = getattr(result, "error", None) or getattr(result, "reason", None)

    if state_name == "BLOCKED":
        latest_pause = load_critical_pause_state()
        latest_decision = evaluate_critical_pause_block(state=latest_pause, requested_symbol=symbol)
        if latest_decision.blocked:
            return EntrySubmitOutcome.critical_block(latest_decision, client_order_id=client_order_id)
        else:
            return EntrySubmitOutcome.entry_block(result_error or "ENTRY_BLOCKED", client_order_id=client_order_id)

    if recovered and state_name == "PROTECTED":
        return EntrySubmitOutcome.duplicate(result, client_order_id=client_order_id)

    if accepted:
        return EntrySubmitOutcome.success(result, client_order_id=client_order_id)
    else:
        return EntrySubmitOutcome.not_accepted(
            result,
            result_error or state_name or "ENTRY_SUBMISSION_NOT_ACCEPTED",
            client_order_id=client_order_id
        )


def _mark_crypto_entry_state(self, client_order_id, state, **changes):
    if not client_order_id:
        return None
    _ensure_trading_safety_runtime(self)
    try:
        return self.trading_state_store.transition(client_order_id, state, **changes)
    except KeyError:
        logger.warning("Order state transition skipped for unknown client ID: %s", client_order_id)
        return None


def _mark_crypto_symbol_closed(self, symbol, reason):
    _ensure_trading_safety_runtime(self)
    closed_count = 0
    active_records = list(self.trading_state_store.active_for_symbol(symbol))
    for record in active_records:
        if record.order_state in {
            OrderState.PROTECTED.value,
            OrderState.FILLED_UNVERIFIED_LIQUIDATION.value,
            OrderState.FILLED_LIQUIDATION_CONFLICT.value,
            OrderState.FILLED_UNPROTECTED.value,
            OrderState.PARTIALLY_FILLED.value,
            OrderState.CLOSING.value,
            OrderState.EMERGENCY_CLOSE_FAILED.value,
        }:
            self.trading_state_store.transition(
                record.client_order_id,
                OrderState.CLOSED,
                last_error=None,
                close_reason=str(reason or "position closed"),
            )
            self.trading_state_store.release_entry_lease(
                record.client_order_id,
                OrderState.CLOSED,
            )
            if str(getattr(record, "order_intent", "ENTRY") or "ENTRY").upper() == "ENTRY":
                remember_decision = getattr(
                    self,
                    "_record_utbreakout_consumed_decision",
                    None,
                )
                if callable(remember_decision):
                    remember_decision(
                        symbol,
                        getattr(record, "signal_timestamp", None),
                    )
            closed_count += 1
    if closed_count:
        clear_plan = getattr(
            self,
            "_clear_utbot_filtered_breakout_entry_plan",
            None,
        )
        if callable(clear_plan):
            clear_plan(symbol)
        trace_key_fn = getattr(self, "_utbreakout_trace_key", None)
        trace_key = trace_key_fn(symbol) if callable(trace_key_fn) else str(symbol)
        for mapping_name in (
            "utbreakout_last_ready_signal",
            "utbreakout_last_ready_reason",
            "utbreakout_last_ready_ts",
        ):
            mapping = getattr(self, mapping_name, None)
            if isinstance(mapping, dict):
                mapping.pop(trace_key, None)
                mapping.pop(symbol, None)
    return closed_count

__all__ = (
    '_ensure_trading_safety_runtime',
    '_extract_liquidation_stop_price',
    '_binance_symbol_id',
    '_preflight_liquidation_safety',
    '_fetch_position_with_liquidation',
    '_handle_liquidation_safety_failure',
    '_verify_actual_liquidation_safety',
    '_resolve_crypto_signal_timestamp',
    '_crypto_entry_block_reason',
    '_crypto_safety_status_lines',
    '_resolve_entry_block_context',
    '_record_entry_block_state',
    '_handle_critical_pause_entry_block',
    '_handle_noncritical_entry_block',
    '_submit_idempotent_crypto_entry',
    '_mark_crypto_entry_state',
    '_mark_crypto_symbol_closed',
)
