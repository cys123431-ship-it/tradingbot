"""Protective-order inspection, audit, repair, and stop replacement."""

from __future__ import annotations


class SignalProtectionMixin:
    def _protection_order_info(self, order):
        return order.get('info', {}) if isinstance(order, dict) and isinstance(order.get('info', {}), dict) else {}

    def _protection_bool(self, value):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {'true', '1', 'yes', 'y'}

    def _is_reduce_only_order(self, order):
        if not isinstance(order, dict):
            return False
        info = self._protection_order_info(order)
        return any(
            self._protection_bool(value)
            for value in (
                order.get('reduceOnly'),
                order.get('reduce_only'),
                info.get('reduceOnly'),
                info.get('reduce_only'),
                info.get('closePosition')
            )
        )

    def _protection_working_type(self, order):
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
        values = {}
        try:
            common = self.get_runtime_common_settings()
            if isinstance(common, dict):
                values.update(common)
        except Exception:
            logger.debug("Liquidation safety common config unavailable", exc_info=True)
        if isinstance(extra, dict):
            values.update(extra)
        return resolve_liquidation_safety_config(values)

    def _liquidation_tick_size(self, symbol):
        try:
            market = self.exchange.market(symbol)
        except Exception:
            market = {}
        info = market.get('info', {}) if isinstance(market, dict) else {}
        for item in info.get('filters', []) if isinstance(info, dict) else []:
            if isinstance(item, dict) and item.get('filterType') == 'PRICE_FILTER':
                tick = _safe_float_or_none(item.get('tickSize'))
                if tick is not None and tick > 0:
                    return tick
        precision = market.get('precision', {}) if isinstance(market, dict) else {}
        value = precision.get('price') if isinstance(precision, dict) else None
        try:
            number = float(value)
            if number > 0:
                return number if number < 1 else 10 ** (-int(number))
        except (TypeError, ValueError, OverflowError):
            pass
        return 0.0

    def _validate_position_stop_liquidation(self, symbol, pos, stop_price, working_type='MARK_PRICE', cfg=None):
        if not pos:
            return None
        side = str(pos.get('side') or '').lower()
        liquidation_price = _safe_float_or_none(
            pos.get('liquidationPrice')
            or pos.get('liquidation_price')
            or self._protection_order_info(pos).get('liquidationPrice')
        )
        entry_price = _safe_float_or_none(pos.get('entryPrice') or pos.get('entry_price')) or 0.0
        tick_size = self._liquidation_tick_size(symbol)
        if side not in {'long', 'short'} or liquidation_price is None or liquidation_price <= 0 or tick_size <= 0:
            return None
        safety_cfg = self._liquidation_safety_config(cfg)
        return validate_stop_against_liquidation(
            side,
            stop_price,
            liquidation_price,
            tick_size,
            safety_cfg.minimum_buffer_pct,
            safety_cfg.minimum_buffer_ticks,
            working_type,
            entry_price,
        )

    def _validate_existing_position_stop_liquidation(
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
        if not isinstance(order, dict):
            return ''
        info = self._protection_order_info(order)
        type_values = []
        for value in (
            order.get('type'),
            order.get('orderType'),
            order.get('stopOrderType'),
            order.get('triggerType'),
            info.get('type'),
            info.get('origType'),
            info.get('orderType'),
            info.get('stopOrderType'),
            info.get('triggerType'),
            info.get('strategyType')
        ):
            if value not in (None, ''):
                type_values.append(str(value))
        return " ".join(type_values).strip().lower().replace('-', '_')

    def _protection_order_side(self, order):
        if not isinstance(order, dict):
            return ''
        info = self._protection_order_info(order)
        return str(order.get('side') or info.get('side') or '').strip().lower()

    def _protection_trigger_price(self, order):
        if not isinstance(order, dict):
            return None
        info = self._protection_order_info(order)
        for value in (
            order.get('stopPrice'),
            order.get('triggerPrice'),
            order.get('stop_price'),
            order.get('trigger_price'),
            info.get('stopPrice'),
            info.get('triggerPrice'),
            info.get('stop_price'),
            info.get('trigger_price'),
            info.get('activatePrice')
        ):
            try:
                number = float(value)
                if number > 0:
                    return number
            except (TypeError, ValueError):
                continue
        return None

    def _protection_order_amount(self, order):
        if not isinstance(order, dict):
            return None
        info = self._protection_order_info(order)
        for value in (
            order.get('amount'),
            order.get('remaining'),
            order.get('origQty'),
            order.get('orig_qty'),
            order.get('quantity'),
            order.get('qty'),
            info.get('origQty'),
            info.get('executedQty'),
            info.get('quantity'),
            info.get('qty'),
        ):
            try:
                number = float(value)
                if number > 0:
                    return number
            except (TypeError, ValueError):
                continue
        return None

    def _normalize_protection_symbol(self, value):
        return normalize_futures_market_id(value)

    def _protection_order_symbol(self, order):
        if not isinstance(order, dict):
            return ''
        info = self._protection_order_info(order)
        return str(order.get('symbol') or info.get('symbol') or info.get('pair') or '').strip()

    def _protection_order_matches_symbol(self, order, symbol):
        order_symbol = self._normalize_protection_symbol(self._protection_order_symbol(order))
        target_symbol = self._normalize_protection_symbol(symbol)
        return not order_symbol or order_symbol == target_symbol

    def _classify_protection_order(self, order):
        client_id = self._protection_client_order_id(order).lower()
        compact_client_id = re.sub(r'[^a-z0-9]', '', client_id)
        if compact_client_id.startswith(('utbtp', 'tp1', 'tp2', 'tp3', 'takeprofit')):
            return 'tp'
        if compact_client_id.startswith(('utbsl', 'sl', 'stoploss')):
            return 'sl'
        order_type = self._protection_order_type(order)
        is_reduce_only = self._is_reduce_only_order(order)
        has_trigger_price = self._protection_trigger_price(order) is not None
        if 'take_profit' in order_type or 'takeprofit' in order_type:
            return 'tp'
        if 'stop' in order_type or (is_reduce_only and has_trigger_price):
            return 'sl'
        if is_reduce_only and ('limit' in order_type or order_type == ''):
            return 'tp'
        return None

    def _is_protection_order(self, order):
        return self._classify_protection_order(order) in {'tp', 'sl'}

    def _protection_order_id(self, order):
        if not isinstance(order, dict):
            return None
        info = self._protection_order_info(order)
        for value in (
            order.get('id'),
            order.get('algoId'),
            order.get('orderId'),
            order.get('clientOrderId'),
            order.get('client_order_id'),
            info.get('algoId'),
            info.get('orderId'),
            info.get('clientOrderId'),
            info.get('origClientOrderId'),
        ):
            if value not in (None, ''):
                return str(value)
        return None

    def _protection_client_order_id(self, order):
        if not isinstance(order, dict):
            return ''
        info = self._protection_order_info(order)
        return str(
            order.get('clientOrderId')
            or order.get('client_order_id')
            or order.get('clientAlgoId')
            or info.get('clientOrderId')
            or info.get('origClientOrderId')
            or info.get('clientAlgoId')
            or ''
        ).strip()

    def _planned_tp_orders_from_state(self, symbol, state=None):
        if state is None:
            state = self._get_utbreakout_trailing_state(symbol)
        if not isinstance(state, dict):
            return []
        planned = []
        raw_orders = state.get('planned_tp_orders') or state.get('tp_orders') or []
        for index, item in enumerate(raw_orders or [], 1):
            if not isinstance(item, dict):
                continue
            label = _normalize_tp_plan_label(
                item.get('tp_label') or item.get('tp_name') or item.get('label'),
                f"TP{index}",
            )
            if not label:
                continue
            price = _safe_float_or_none(item.get('price') or item.get('target_price'))
            qty = _safe_float_or_none(item.get('qty') or item.get('quantity'))
            planned.append({
                'tp_index': int(item.get('tp_index') or index),
                'tp_label': label,
                'tp_name': item.get('tp_name') or label,
                'side': str(item.get('side') or '').lower(),
                'price': float(price) if price is not None else None,
                'qty': float(qty) if qty is not None else None,
                'order_id': item.get('order_id'),
                'client_order_id': item.get('client_order_id'),
                'filled': bool(item.get('filled', False)),
            })
        return planned

    def _price_matches_plan(self, expected, actual, tolerance_pct=0.01):
        expected = _safe_float_or_none(expected)
        actual = _safe_float_or_none(actual)
        if expected is None or actual is None:
            return False
        tolerance = max(1e-8, abs(float(expected)) * (float(tolerance_pct) / 100.0))
        return abs(float(actual) - float(expected)) <= tolerance

    def _qty_matches_plan(self, expected, actual, tolerance_pct=0.1):
        expected = _safe_float_or_none(expected)
        actual = _safe_float_or_none(actual)
        if expected is None or actual is None:
            return False
        tolerance = max(1e-9, abs(float(expected)) * (float(tolerance_pct) / 100.0))
        return abs(float(actual) - float(expected)) <= tolerance

    def _protection_tp_label(self, order, planned_tp_orders=None):
        text = " ".join([
            self._protection_client_order_id(order),
            self._protection_order_id(order) or '',
        ]).lower()
        for label in ('tp1', 'tp2', 'tp3'):
            if label in text:
                return label.upper()

        order_side = self._protection_order_side(order)
        order_price = _safe_float_or_none(order.get('price')) or self._protection_trigger_price(order)
        for plan in planned_tp_orders or []:
            if not isinstance(plan, dict):
                continue
            plan_side = str(plan.get('side') or '').lower()
            if plan_side and order_side and plan_side != order_side:
                continue
            if self._price_matches_plan(plan.get('price'), order_price):
                return _normalize_tp_plan_label(plan.get('tp_label') or plan.get('tp_name'))
        return None

    def _protection_order_timestamp(self, order):
        if not isinstance(order, dict):
            return 0
        info = self._protection_order_info(order)
        for value in (
            order.get('timestamp'),
            order.get('lastTradeTimestamp'),
            info.get('updateTime'),
            info.get('time'),
            info.get('workingTime'),
        ):
            try:
                parsed = int(float(value))
                if parsed > 0:
                    return parsed
            except (TypeError, ValueError):
                continue
        return 0

    def _build_protection_client_order_id(
        self,
        symbol,
        side,
        kind,
        pos=None,
        *,
        trigger_price=None,
        quantity=None,
        leg=None,
        position_identity=None,
    ):
        kind_full = re.sub(r'[^a-zA-Z0-9]', '', str(kind or '').lower()) or 'unknown'
        leg_full = re.sub(r'[^a-zA-Z0-9]', '', str(leg or kind_full).lower()) or kind_full
        try:
            normalized_trigger = self.safe_price(symbol, trigger_price) if trigger_price is not None else 'none'
        except (TypeError, ValueError):
            normalized_trigger = str(trigger_price or 'none')
        if quantity is None and isinstance(pos, dict):
            quantity = abs(float(self._position_signed_contracts(pos) or pos.get('contracts', 0) or 0))
        try:
            normalized_qty = self.safe_amount(symbol, quantity) if quantity is not None else 'none'
        except (TypeError, ValueError):
            normalized_qty = str(quantity or 'none')
        stable_position_identity = str(position_identity or '')
        if not stable_position_identity and isinstance(pos, dict):
            stable_position_identity = str(
                pos.get('entry_client_order_id')
                or pos.get('clientOrderId')
                or pos.get('client_order_id')
                or pos.get('timestamp')
                or pos.get('datetime')
                or self._protection_position_signature(pos)
            )
        raw = "|".join([
            self._normalize_protection_symbol(symbol),
            str(side or '').lower(),
            kind_full,
            leg_full,
            stable_position_identity or self._protection_position_signature(pos),
            str(normalized_trigger),
            str(normalized_qty),
        ])
        digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]
        symbol_part = self._normalize_protection_symbol(symbol)[-8:] or 'SYMBOL'
        prefix = f"utb{kind_full[:3]}{leg_full[:3]}{symbol_part}"
        return f"{prefix}{digest}"[:36]

    async def _collect_protection_orders(self, symbol):
        snapshot = await self._collect_protection_order_snapshot(symbol)
        if not snapshot.complete:
            raise ProtectionOrderLookupUnavailable('; '.join(snapshot.errors))
        return list(snapshot.orders)

    async def _collect_protection_orders_checked(self, symbol):
        snapshot = await self._collect_protection_order_snapshot(symbol)
        return snapshot.complete, list(snapshot.orders)

    async def _collect_protection_order_snapshot(self, symbol):
        merged = []
        seen = set()
        regular_fetch_ok = False
        errors = []
        for scope in (symbol, None):
            open_orders = await self._fetch_open_orders_safe(scope)
            if open_orders is None:
                errors.append(f"regular_open_orders_failed:{scope or 'all'}")
                continue
            regular_fetch_ok = True
            for order in open_orders or []:
                if not self._protection_order_matches_symbol(order, symbol):
                    continue
                if not self._is_protection_order(order):
                    continue
                key = (
                    str(order.get('_protection_source') or 'regular'),
                    self._protection_order_id(order) or str(id(order)),
                )
                if key in seen:
                    continue
                seen.add(key)
                merged.append(order)
            if open_orders or scope is None:
                break

        is_binance = str(getattr(self.exchange, 'id', '') or '').lower() in {'binance', 'binanceusdm'}
        algo_fetch_ok = not is_binance
        if is_binance:
            algo_snapshot = await BinanceAlgoOrderGateway(self.exchange).fetch_open_orders()
            algo_fetch_ok = algo_snapshot.ok
            if not algo_snapshot.ok:
                errors.append(algo_snapshot.error or 'algo_open_orders_failed')
            for order in algo_snapshot.orders:
                if not self._protection_order_matches_symbol(order, symbol):
                    continue
                if not self._is_protection_order(order):
                    continue
                key = (
                    str(order.get('_protection_source') or 'binance_algo'),
                    self._protection_order_id(order) or str(id(order)),
                )
                if key in seen:
                    continue
                seen.add(key)
                merged.append(order)
        return ProtectionOrderSnapshot(
            regular_orders_ok=regular_fetch_ok,
            algo_orders_ok=algo_fetch_ok,
            orders=tuple(merged),
            errors=tuple(errors),
        )

    async def _cancel_single_protection_order(self, symbol, order, reason='protection cleanup'):
        order_id = self._protection_order_id(order)
        if not order_id:
            return False
        info = self._protection_order_info(order)
        algo_id = (
            order.get('algoId')
            or info.get('algoId')
            if isinstance(order, dict)
            else None
        )
        if algo_id not in (None, '') or (
            isinstance(order, dict) and order.get('_protection_source') == 'binance_algo'
        ):
            cancel_algo = getattr(self.exchange, 'fapiPrivateDeleteAlgoOrder', None)
            if not callable(cancel_algo):
                logger.warning(
                    f"Protection cleanup cancel failed for {symbol} / {order_id}: "
                    "Binance Algo cancel endpoint unavailable"
                )
                return False
            try:
                await asyncio.to_thread(cancel_algo, {'algoId': algo_id or order_id})
                logger.info(f"Protection cleanup: cancelled Algo {order_id} for {symbol} ({reason})")
                return True
            except Exception as e:
                logger.warning(f"Protection cleanup Algo cancel failed for {symbol} / {order_id}: {e}")
                return False
        order_symbol = str(order.get('symbol') or '').strip() if isinstance(order, dict) else ''
        candidates = self._protection_cancel_symbol_candidates(symbol, order_symbol)
        last_error = None
        for cancel_symbol in candidates:
            try:
                await asyncio.to_thread(self.exchange.cancel_order, order_id, cancel_symbol)
                logger.info(f"Protection cleanup: cancelled {order_id} for {cancel_symbol} ({reason})")
                return True
            except Exception as e:
                last_error = e
        logger.warning(f"Protection cleanup cancel failed for {symbol} / {order_id}: {last_error}")
        return False

    def _newest_protection_order(self, orders):
        if not orders:
            return None
        return max(
            orders,
            key=lambda order: (
                self._protection_order_timestamp(order),
                self._protection_client_order_id(order),
                self._protection_order_id(order) or ''
            )
        )

    async def _fetch_open_orders_safe(self, symbol=None):
        try:
            if symbol:
                return await asyncio.to_thread(self.exchange.fetch_open_orders, symbol)
            return await asyncio.to_thread(self.exchange.fetch_open_orders)
        except Exception as e:
            logger.warning(f"Protection audit: fetch_open_orders failed for {symbol or 'ALL'}: {e}")
        return None

    def _normalize_binance_algo_order(self, order):
        if not isinstance(order, dict):
            return None
        info = dict(order)
        algo_id = order.get('algoId')
        order_type = order.get('orderType') or order.get('type') or ''
        quantity = order.get('quantity') or order.get('origQty') or order.get('qty')
        trigger_price = order.get('triggerPrice') or order.get('stopPrice')
        timestamp = (
            order.get('updateTime')
            or order.get('createTime')
            or order.get('time')
        )
        return {
            'id': str(algo_id) if algo_id not in (None, '') else None,
            'algoId': algo_id,
            'clientOrderId': order.get('clientAlgoId') or order.get('clientOrderId'),
            'clientAlgoId': order.get('clientAlgoId'),
            'symbol': order.get('symbol'),
            'type': str(order_type).lower(),
            'orderType': order_type,
            'side': str(order.get('side') or '').lower(),
            'amount': quantity,
            'quantity': quantity,
            'stopPrice': trigger_price,
            'triggerPrice': trigger_price,
            'reduceOnly': self._protection_bool(order.get('reduceOnly')),
            'workingType': order.get('workingType'),
            'priceProtect': order.get('priceProtect'),
            'status': order.get('algoStatus') or order.get('status'),
            'timestamp': timestamp,
            'info': info,
            '_protection_source': 'binance_algo',
        }

    async def _fetch_binance_algo_orders_safe(self, symbol=None):
        fetch_algo = getattr(self.exchange, 'fapiPrivateGetOpenAlgoOrders', None)
        if not callable(fetch_algo):
            return []
        params = {}
        if symbol:
            normalized = self._normalize_protection_symbol(symbol)
            if normalized:
                params['symbol'] = normalized
        try:
            response = await asyncio.to_thread(fetch_algo, params)
        except Exception as e:
            logger.warning(
                f"Protection audit: Binance open Algo orders failed for {symbol or 'ALL'}: {e}"
            )
            return None

        if isinstance(response, list):
            raw_orders = response
        elif isinstance(response, dict):
            raw_orders = (
                response.get('orders')
                or response.get('algoOrders')
                or response.get('rows')
                or response.get('data')
                or ([response] if response.get('algoId') not in (None, '') else [])
            )
        else:
            raw_orders = []
        if isinstance(raw_orders, dict):
            raw_orders = raw_orders.get('orders') or raw_orders.get('rows') or []

        normalized_orders = []
        for raw_order in raw_orders or []:
            normalized = self._normalize_binance_algo_order(raw_order)
            if normalized is not None:
                normalized_orders.append(normalized)
        return normalized_orders

    async def _collect_all_protection_orders_checked(self):
        merged = []
        seen = set()
        regular_orders = await self._fetch_open_orders_safe(None)
        algo_supported = callable(getattr(self.exchange, 'fapiPrivateGetOpenAlgoOrders', None))
        algo_orders = await self._fetch_binance_algo_orders_safe(None) if algo_supported else []
        fetch_ok = regular_orders is not None or (algo_supported and algo_orders is not None)
        for order in list(regular_orders or []) + list(algo_orders or []):
            if not self._is_protection_order(order):
                continue
            key = (
                str(order.get('_protection_source') or 'regular'),
                self._protection_order_id(order) or str(id(order)),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(order)
        return bool(fetch_ok), merged

    def _protection_position_symbol(self, pos):
        if not isinstance(pos, dict):
            return ''
        info = self._protection_order_info(pos)
        return str(pos.get('symbol') or info.get('symbol') or info.get('pair') or '').strip()

    def _protection_unified_symbol_from_key(self, symbol_key, fallback=None):
        raw = str(fallback or '').strip()
        if '/' in raw:
            return raw.split(':', 1)[0]
        normalized = self._normalize_protection_symbol(symbol_key or raw)
        for quote in ('USDT', 'USDC', 'BUSD'):
            if normalized.endswith(quote) and len(normalized) > len(quote):
                return f"{normalized[:-len(quote)]}/{quote}"
        return raw or normalized

    async def _fetch_active_protection_symbol_keys(self):
        try:
            positions = await asyncio.to_thread(self.exchange.fetch_positions)
        except Exception as e:
            logger.warning(f"Protection orphan sweep: fetch_positions failed: {e}")
            return None

        active_keys = set()
        for pos in positions or []:
            normalized_pos = self._normalize_server_position(pos)
            if not normalized_pos:
                continue
            symbol_key = self._normalize_protection_symbol(
                self._protection_position_symbol(normalized_pos)
            )
            if symbol_key:
                active_keys.add(symbol_key)
        return active_keys

    def _group_protection_orders_by_symbol(self, orders):
        grouped = {}
        unknown_symbol_orders = []
        for order in orders or []:
            if not self._is_protection_order(order):
                continue
            raw_symbol = self._protection_order_symbol(order)
            symbol_key = self._normalize_protection_symbol(raw_symbol)
            if not symbol_key:
                unknown_symbol_orders.append(order)
                continue
            group = grouped.setdefault(
                symbol_key,
                {
                    'symbol': self._protection_unified_symbol_from_key(symbol_key, raw_symbol),
                    'raw_symbol': raw_symbol,
                    'orders': []
                }
            )
            group['orders'].append(order)
        return grouped, unknown_symbol_orders

    def _protection_orders_signature(self, orders):
        return tuple(sorted(
            self._protection_order_id(order) or self._protection_client_order_id(order) or str(id(order))
            for order in orders or []
        ))

    async def _cleanup_orphan_protection_orders(
        self,
        reason='orphan protection sweep',
        alert=True,
        min_interval=None,
        confirm_delay_sec=10.0
    ):
        status = {
            'status': 'SKIPPED',
            'cancelled': 0,
            'closed_records': 0,
            'released_leases': 0,
            'pending': 0,
            'symbols': {}
        }
        if self.is_upbit_mode():
            status['status'] = 'UPBIT_SKIPPED'
            return status

        now_ts = time.time()
        interval = (
            float(min_interval)
            if min_interval is not None
            else float(getattr(self, 'ORPHAN_PROTECTION_SWEEP_INTERVAL', 10.0) or 10.0)
        )
        last_sweep = float(getattr(self, 'last_orphan_protection_sweep_ts', 0.0) or 0.0)
        if interval > 0 and now_ts - last_sweep < interval:
            status['status'] = 'THROTTLED'
            return status
        self.last_orphan_protection_sweep_ts = now_ts

        active_keys = await self._fetch_active_protection_symbol_keys()
        if active_keys is None:
            status['status'] = 'POSITION_FETCH_FAILED'
            return status

        fetch_ok, open_orders = await self._collect_all_protection_orders_checked()
        if not fetch_ok:
            status['status'] = 'OPEN_ORDERS_FETCH_FAILED'
            return status

        grouped, unknown_symbol_orders = self._group_protection_orders_by_symbol(open_orders)
        candidates = getattr(self, 'orphan_protection_candidates', None)
        if not isinstance(candidates, dict):
            candidates = {}
            self.orphan_protection_candidates = candidates

        for symbol_key in list(candidates.keys()):
            if symbol_key in active_keys or symbol_key not in grouped:
                candidates.pop(symbol_key, None)

        for symbol_key, group in grouped.items():
            orders = group.get('orders') or []
            symbol = group.get('symbol') or symbol_key
            if symbol_key in active_keys:
                candidates.pop(symbol_key, None)
                continue

            signature = self._protection_orders_signature(orders)
            candidate = candidates.get(symbol_key)
            if not candidate or tuple(candidate.get('signature') or ()) != signature:
                candidate = {
                    'first_seen_ts': now_ts,
                    'signature': signature,
                    'symbol': symbol
                }
                candidates[symbol_key] = candidate
                if float(confirm_delay_sec or 0.0) <= 0:
                    first_seen = now_ts
                else:
                    status['pending'] += len(orders)
                    status['symbols'][symbol] = {
                        'pending': len(orders),
                        'cancelled': 0,
                        'status': 'PENDING_CONFIRMATION'
                    }
                    continue
            else:
                first_seen = float(candidate.get('first_seen_ts', now_ts) or now_ts)

            if now_ts - first_seen < max(0.0, float(confirm_delay_sec or 0.0)):
                status['pending'] += len(orders)
                status['symbols'][symbol] = {
                    'pending': len(orders),
                    'cancelled': 0,
                    'status': 'PENDING_CONFIRMATION'
                }
                continue

            pos = await self.get_server_position(symbol, use_cache=False)
            if pos:
                candidates.pop(symbol_key, None)
                continue

            cancelled = await self._cancel_protection_orders(
                symbol,
                reason=reason,
                orders=orders
            )
            candidates.pop(symbol_key, None)
            status['cancelled'] += cancelled
            status['symbols'][symbol] = {
                'pending': 0,
                'cancelled': cancelled,
                'status': 'ORPHAN_CANCELLED' if cancelled else 'CANCEL_FAILED'
            }
            self.last_protection_order_status[symbol] = {
                'tp_expected': False,
                'sl_expected': False,
                'tp_present': False,
                'sl_present': False,
                'tp_count': 0,
                'sl_count': 0,
                'missing_tp': False,
                'missing_sl': False,
                'orphan_cancelled': cancelled,
                'status': 'ORPHAN_CANCELLED_GLOBAL' if cancelled else 'ORPHAN_CANCEL_FAILED'
            }
            if alert and cancelled:
                await self._notify_protection_issue(
                    symbol,
                    f'global_orphan_cancelled:{symbol_key}',
                    f"ℹ️ {self.ctrl.format_symbol_for_display(symbol)} 포지션 없음: 잔존 보호주문 {cancelled}건 자동 취소"
                )

        state_store = getattr(self, 'trading_state_store', None)
        state_candidates = getattr(self, 'flat_protected_state_candidates', None)
        if not isinstance(state_candidates, dict):
            state_candidates = {}
            self.flat_protected_state_candidates = state_candidates
        protected_records = (
            state_store.list_by_states([OrderState.PROTECTED])
            if state_store is not None
            else []
        )
        protected_client_ids = {record.client_order_id for record in protected_records}
        for client_order_id in list(state_candidates.keys()):
            if client_order_id not in protected_client_ids:
                state_candidates.pop(client_order_id, None)

        confirm_delay = max(0.0, float(confirm_delay_sec or 0.0))
        for record in protected_records:
            symbol = record.symbol
            symbol_key = self._normalize_protection_symbol(symbol)
            client_order_id = record.client_order_id
            if not symbol_key or symbol_key in active_keys or symbol_key in grouped:
                state_candidates.pop(client_order_id, None)
                continue

            candidate = state_candidates.get(client_order_id)
            if not candidate:
                state_candidates[client_order_id] = {
                    'first_seen_ts': now_ts,
                    'symbol': symbol,
                }
                if confirm_delay > 0:
                    status['pending'] += 1
                    status['symbols'][symbol] = {
                        'pending': 1,
                        'cancelled': 0,
                        'closed_records': 0,
                        'status': 'FLAT_STATE_PENDING_CONFIRMATION',
                    }
                    continue
                first_seen = now_ts
            else:
                first_seen = float(candidate.get('first_seen_ts', now_ts) or now_ts)

            if now_ts - first_seen < confirm_delay:
                status['pending'] += 1
                status['symbols'][symbol] = {
                    'pending': 1,
                    'cancelled': 0,
                    'closed_records': 0,
                    'status': 'FLAT_STATE_PENDING_CONFIRMATION',
                }
                continue

            self.position_cache = None
            self.position_cache_time = 0
            position_fetch_ok, pos = await self._fetch_server_position_checked(symbol)
            if not position_fetch_ok:
                status['pending'] += 1
                status['symbols'][symbol] = {
                    'pending': 1,
                    'cancelled': 0,
                    'closed_records': 0,
                    'status': 'POSITION_FETCH_FAILED',
                }
                continue
            if pos:
                state_candidates.pop(client_order_id, None)
                continue

            protection_fetch_ok, remaining = await self._collect_protection_orders_checked(symbol)
            if not protection_fetch_ok:
                status['pending'] += 1
                status['symbols'][symbol] = {
                    'pending': 1,
                    'cancelled': 0,
                    'closed_records': 0,
                    'status': 'OPEN_ORDERS_FETCH_FAILED',
                }
                continue
            if remaining:
                state_candidates.pop(client_order_id, None)
                continue

            closed_records = _mark_crypto_symbol_closed(
                self,
                symbol,
                f'{reason}: confirmed exchange-flat protected state',
            )
            state_candidates.pop(client_order_id, None)
            status['closed_records'] += closed_records
            status['symbols'][symbol] = {
                'pending': 0,
                'cancelled': 0,
                'closed_records': closed_records,
                'status': 'STALE_PROTECTED_CLOSED' if closed_records else 'ALREADY_CLOSED',
            }
            if closed_records:
                logger.warning(
                    "Protection orphan sweep released %s stale protected state record(s) for %s (%s)",
                    closed_records,
                    symbol,
                    reason,
                )

        entry_lease = state_store.get_entry_lease() if state_store is not None else None
        if entry_lease:
            lease_client_order_id = str(entry_lease.get('client_order_id') or '')
            lease_symbol = str(entry_lease.get('symbol') or '')
            lease_symbol_key = self._normalize_protection_symbol(lease_symbol)
            lease_record = state_store.get(lease_client_order_id) if lease_client_order_id else None
            lease_terminal = bool(
                lease_record
                and lease_record.order_state in {
                    OrderState.CLOSED.value,
                    OrderState.CANCELED.value,
                    OrderState.FAILED.value,
                }
            )
            lease_expired = float(entry_lease.get('expires_at') or 0.0) <= now_ts
            if (
                lease_terminal
                and lease_expired
                and lease_symbol_key
                and lease_symbol_key not in active_keys
                and lease_symbol_key not in grouped
            ):
                self.position_cache = None
                self.position_cache_time = 0
                position_fetch_ok, pos = await self._fetch_server_position_checked(lease_symbol)
                protection_fetch_ok = False
                remaining = None
                if position_fetch_ok and not pos:
                    protection_fetch_ok, remaining = await self._collect_protection_orders_checked(lease_symbol)
                if position_fetch_ok and not pos and protection_fetch_ok and not remaining:
                    released = state_store.release_entry_lease_for_symbol(
                        lease_symbol,
                        reconciliation_confirmed=True,
                    )
                    status['released_leases'] += int(bool(released))
                    if released:
                        logger.warning(
                            "Protection orphan sweep released stale global entry lease for %s (%s)",
                            lease_symbol,
                            reason,
                        )

        if status['cancelled']:
            status['status'] = 'ORPHAN_CANCELLED'
        elif status['closed_records']:
            status['status'] = 'STALE_PROTECTED_CLOSED'
        elif status['released_leases']:
            status['status'] = 'STALE_ENTRY_LEASE_RELEASED'
        elif status['pending']:
            status['status'] = 'PENDING_CONFIRMATION'
        else:
            status['status'] = 'OK'
        if unknown_symbol_orders:
            status['unknown_symbol_orders'] = len(unknown_symbol_orders)
        return status

    def _protection_cancel_symbol_candidates(self, symbol, order_symbol=None):
        candidates = []

        def _add(value):
            text = str(value or '').strip()
            if text and text not in candidates:
                candidates.append(text)

        _add(symbol)
        _add(order_symbol)
        for value in (symbol, order_symbol):
            normalized = self._normalize_protection_symbol(value)
            if not normalized:
                continue
            _add(normalized)
            for quote in ('USDT', 'USDC', 'BUSD'):
                if normalized.endswith(quote) and len(normalized) > len(quote):
                    base = normalized[:-len(quote)]
                    _add(f"{base}/{quote}")
                    _add(f"{base}/{quote}:{quote}")
                    break
        return candidates

    async def _cancel_all_orders_variants(self, symbol, reason='order cleanup'):
        if self.is_upbit_mode():
            return 0
        successes = 0
        last_error = None
        for cancel_symbol in self._protection_cancel_symbol_candidates(symbol):
            try:
                await asyncio.to_thread(self.exchange.cancel_all_orders, cancel_symbol)
                successes += 1
                logger.info(f"All orders cancel requested for {cancel_symbol} ({reason})")
            except Exception as exc:
                last_error = exc
                logger.debug(f"cancel_all_orders failed for {cancel_symbol} ({reason}): {exc}")
        if successes <= 0 and last_error:
            logger.warning(f"cancel_all_orders failed for all symbol variants {symbol}: {last_error}")
        return successes

    async def _cancel_protection_orders(self, symbol, reason='protection cleanup', orders=None):
        if self.is_upbit_mode():
            return 0
        open_orders = orders if orders is not None else await self._collect_protection_orders(symbol)
        if open_orders is None:
            return 0
        cancelled = 0
        for order in open_orders or []:
            if not self._is_protection_order(order):
                continue
            if await self._cancel_single_protection_order(symbol, order, reason=reason):
                cancelled += 1
        if cancelled:
            logger.info(f"Protection cleanup: cancelled {cancelled} orders for {symbol} ({reason})")
        return cancelled

    async def _reconcile_closed_position_protection(self, symbol, reason='position closed', alert=True, attempts=3):
        if self.is_upbit_mode():
            return None
        final_status = None
        total_attempts = max(1, int(attempts or 1))
        for attempt in range(total_attempts):
            self.position_cache = None
            self.position_cache_time = 0
            position_fetch_ok, pos = await self._fetch_server_position_checked(symbol)
            if not position_fetch_ok:
                final_status = {
                    'status': 'POSITION_FETCH_FAILED',
                    'position_fetch_ok': False,
                    'position_active': None,
                    'cleanup_confirmed': False,
                    'remaining_orders': None,
                    'orphan_cancelled': 0,
                }
                self.last_protection_order_status[symbol] = final_status
                logger.warning(
                    f"Protection cleanup skipped for {symbol}: position status could not be confirmed "
                    f"({reason})"
                )
                return final_status
            final_status = await self._audit_protection_orders(
                symbol,
                pos=pos,
                expected_tp=False if not pos else None,
                expected_sl=False if not pos else None,
                alert=alert
            )
            if pos:
                final_status['position_fetch_ok'] = True
                final_status['position_active'] = True
                final_status['cleanup_confirmed'] = False
                return final_status
            fetch_ok, remaining = await self._collect_protection_orders_checked(symbol)
            if not fetch_ok:
                final_status['position_active'] = False
                final_status['cleanup_confirmed'] = False
                final_status['remaining_orders'] = None
                return final_status
            if not remaining:
                closed_records = _mark_crypto_symbol_closed(self, symbol, reason)
                final_status['position_fetch_ok'] = True
                final_status['position_active'] = False
                final_status['cleanup_confirmed'] = True
                final_status['remaining_orders'] = 0
                final_status['closed_records'] = closed_records
                return final_status
            await self._cancel_protection_orders(
                symbol,
                reason=f"{reason} retry {attempt + 1}",
                orders=remaining
            )
            if attempt < total_attempts - 1:
                await asyncio.sleep(0.5)
        fetch_ok, remaining = await self._collect_protection_orders_checked(symbol)
        if final_status is None:
            final_status = {}
        final_status['position_fetch_ok'] = True
        final_status['position_active'] = False
        final_status['cleanup_confirmed'] = bool(fetch_ok and not remaining)
        final_status['remaining_orders'] = len(remaining or []) if fetch_ok else None
        final_status['closed_records'] = 0
        if fetch_ok and remaining:
            final_status['status'] = 'ORPHAN_CANCEL_FAILED'
        elif not fetch_ok:
            final_status['status'] = 'OPEN_ORDERS_FETCH_FAILED'
        else:
            final_status['closed_records'] = _mark_crypto_symbol_closed(self, symbol, reason)
        return final_status

    async def _notify_protection_issue(self, symbol, kind, message, cooldown_sec=300):
        key = f"{symbol}:{kind}"
        now_ts = time.time()
        last_ts = float(self.last_protection_alert_ts.get(key, 0.0) or 0.0)
        if now_ts - last_ts < cooldown_sec:
            return
        self.last_protection_alert_ts[key] = now_ts
        try:
            await self.ctrl.notify(message)
        except Exception as e:
            logger.warning(f"Protection alert failed for {symbol}: {e}")

    def _protection_position_signature(self, pos):
        if not isinstance(pos, dict):
            return 'none'
        side = str(pos.get('side', '') or 'unknown').lower()
        try:
            contracts = round(abs(float(self._position_signed_contracts(pos) or pos.get('contracts', 0) or 0)), 12)
        except (TypeError, ValueError):
            contracts = 0.0
        try:
            entry_price = round(float(pos.get('entryPrice', 0) or 0), 8)
        except (TypeError, ValueError):
            entry_price = 0.0
        return f"{side}:{contracts}:{entry_price}"

    def _clear_protection_missing_candidates(self, symbol, position_signature=None, issue_keys=None):
        candidates = getattr(self, 'protection_missing_candidates', None)
        if not isinstance(candidates, dict):
            self.protection_missing_candidates = {}
            return
        symbol_candidates = candidates.get(symbol)
        if not isinstance(symbol_candidates, dict):
            return
        wanted = None
        if issue_keys is not None:
            wanted = {str(item) for item in issue_keys}
        for issue_key in list(symbol_candidates.keys()):
            candidate = symbol_candidates.get(issue_key) or {}
            if wanted is not None and issue_key in wanted:
                continue
            if position_signature is not None and candidate.get('position_signature') != position_signature:
                continue
            symbol_candidates.pop(issue_key, None)
        if not symbol_candidates:
            candidates.pop(symbol, None)

    def _confirm_protection_missing_issue(
        self,
        symbol,
        issue_key,
        position_signature,
        required_count=2,
        min_age_sec=2.0
    ):
        candidates = getattr(self, 'protection_missing_candidates', None)
        if not isinstance(candidates, dict):
            candidates = {}
            self.protection_missing_candidates = candidates
        issue_key = str(issue_key or '').strip()
        position_signature = str(position_signature or 'none')
        if not issue_key:
            return False
        now_ts = time.time()
        symbol_candidates = candidates.setdefault(symbol, {})
        for existing_key in list(symbol_candidates.keys()):
            existing = symbol_candidates.get(existing_key) or {}
            if existing.get('position_signature') != position_signature:
                symbol_candidates.pop(existing_key, None)
        candidate = symbol_candidates.get(issue_key)
        if not candidate or candidate.get('position_signature') != position_signature:
            symbol_candidates[issue_key] = {
                'position_signature': position_signature,
                'first_seen_ts': now_ts,
                'last_seen_ts': now_ts,
                'count': 1,
            }
            logger.info(
                f"Protection audit candidate: {symbol} {issue_key} first seen, waiting for confirmation"
            )
            return False
        candidate['count'] = int(candidate.get('count', 1) or 1) + 1
        candidate['last_seen_ts'] = now_ts
        first_seen = float(candidate.get('first_seen_ts', now_ts) or now_ts)
        confirmed = (
            int(candidate.get('count', 0) or 0) >= max(1, int(required_count or 1))
            and now_ts - first_seen >= max(0.0, float(min_age_sec or 0.0))
        )
        if not confirmed:
            logger.info(
                f"Protection audit candidate: {symbol} {issue_key} seen "
                f"{int(candidate.get('count', 0) or 0)}/{max(1, int(required_count or 1))}, waiting"
            )
        return confirmed

    def _protection_expected_from_config(self, symbol, pos):
        if self.is_upbit_mode() or not pos:
            return False, False
        try:
            qty = abs(float(self._position_signed_contracts(pos) or pos.get('contracts', 0) or 0))
        except (TypeError, ValueError):
            qty = 0.0
        if qty <= 0:
            return False, False
        try:
            cfg = self.get_runtime_common_settings()
        except Exception:
            cfg = {}
        master = bool(cfg.get('tp_sl_enabled', True))
        return (
            master and bool(cfg.get('take_profit_enabled', True)),
            master and bool(cfg.get('stop_loss_enabled', True))
        )

    async def _audit_protection_orders(
        self,
        symbol,
        pos=None,
        expected_tp=None,
        expected_sl=None,
        alert=True,
        planned_tp_orders=None,
    ):
        status = {
            'tp_expected': bool(expected_tp),
            'sl_expected': bool(expected_sl),
            'tp_present': False,
            'sl_present': False,
            'tp1_present': False,
            'tp2_present': False,
            'tp_count': 0,
            'sl_count': 0,
            'expected_tp_count': 0,
            'actual_tp_count': 0,
            'tp_labels_present': [],
            'missing_tp': False,
            'missing_tp1': False,
            'missing_tp2': False,
            'missing_sl': False,
            'tp_qty_mismatch': False,
            'tp2_qty_mismatch': False,
            'tp_price_mismatch': False,
            'sl_qty_mismatch': False,
            'orphan_cancelled': 0,
            'mismatch_cancelled': 0,
            'duplicate_cancelled': 0,
            'invalid_price_cancelled': 0,
            'liquidation_safety': 'UNKNOWN',
            'liquidation_safety_reason': None,
            'liquidation_price': None,
            'stop_working_type': None,
            'fetch_ok': True,
            'missing_confirmed': False,
            'status': 'SKIPPED'
        }
        if self.is_upbit_mode():
            self.last_protection_order_status[symbol] = status
            return status

        if expected_tp is None or expected_sl is None:
            expected_tp, expected_sl = self._protection_expected_from_config(symbol, pos)
            status['tp_expected'] = bool(expected_tp)
            status['sl_expected'] = bool(expected_sl)

        fetch_ok, protection_orders = await self._collect_protection_orders_checked(symbol)
        status['fetch_ok'] = bool(fetch_ok)
        if not fetch_ok:
            status['status'] = 'OPEN_ORDERS_FETCH_FAILED'
            status['missing_sl'] = False
            status['missing_tp'] = False
            status['missing_tp1'] = False
            status['missing_tp2'] = False
            self._clear_protection_missing_candidates(symbol)
            self.last_protection_order_status[symbol] = status
            logger.warning(
                f"Protection audit open-order fetch failed for {symbol}; "
                "not treating missing protection orders as confirmed."
            )
            return status

        if not pos:
            self._clear_protection_missing_candidates(symbol)
            status['status'] = 'NO_POSITION'
            if protection_orders:
                status['orphan_cancelled'] = await self._cancel_protection_orders(
                    symbol,
                    reason='no position remains',
                    orders=protection_orders
                )
                status['status'] = 'ORPHAN_CANCELLED'
                if alert and status['orphan_cancelled']:
                    await self._notify_protection_issue(
                        symbol,
                        'orphan_cancelled',
                        f"ℹ️ {self.ctrl.format_symbol_for_display(symbol)} 포지션 없음: 잔존 보호주문 {status['orphan_cancelled']}건 자동 취소"
                    )
            self.last_protection_order_status[symbol] = status
            return status

        pos_side = str(pos.get('side', '')).lower()
        close_side = 'sell' if pos_side == 'long' else 'buy'
        try:
            pos_entry_price = float(pos.get('entryPrice') or 0.0)
        except (TypeError, ValueError):
            pos_entry_price = 0.0
        current_qty = abs(float(self._position_signed_contracts(pos) or pos.get('contracts', 0) or 0))
        runner_state = self._get_utbreakout_trailing_state(symbol)
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
            isinstance(runner_state, dict)
            and bool(runner_state.get('active'))
            and str(runner_state.get('side', '')).lower() == pos_side
        )
        if planned_tp_orders is None:
            planned_tp_orders = self._planned_tp_orders_from_state(symbol, runner_state)
        planned_tp_orders = list(planned_tp_orders or [])
        planned_by_label = {
            _normalize_tp_plan_label(plan.get('tp_label') or plan.get('tp_name')): dict(plan)
            for plan in planned_tp_orders
            if isinstance(plan, dict) and _normalize_tp_plan_label(plan.get('tp_label') or plan.get('tp_name'))
        }
        inferred_filled_labels = []
        if planned_by_label and isinstance(runner_state, dict) and current_qty > 0:
            before = {
                label: bool(runner_state.get(f"{label.lower()}_filled"))
                for label in planned_by_label
            }
            runner_state = self._update_utbreakout_fill_flags_from_position_qty(
                symbol,
                runner_state,
                current_qty,
            )
            for label in planned_by_label:
                filled = bool(
                    runner_state.get(f"{label.lower()}_filled")
                )
                planned_by_label[label]['filled'] = filled
                if filled and not before.get(label):
                    inferred_filled_labels.append(label)
            if inferred_filled_labels:
                runner_state['last_tp_fill_inferred_qty'] = current_qty
                runner_state['last_tp_fill_inferred_at'] = (
                    datetime.now(timezone.utc).isoformat()
                )
                self._set_utbreakout_trailing_state(symbol, runner_state)
                status['tp_filled_inferred_labels'] = list(
                    inferred_filled_labels
                )
        expected_tp_labels = []
        if planned_by_label:
            for label, plan in planned_by_label.items():
                if isinstance(runner_state, dict) and bool(runner_state.get(f"{label.lower()}_filled", False)):
                    continue
                if bool(plan.get('filled', False)):
                    continue
                expected_tp_labels.append(label)
            status['expected_tp_count'] = len(expected_tp_labels)
            status['planned_tp_orders'] = [
                {
                    'tp_label': label,
                    'price': planned_by_label[label].get('price'),
                    'qty': planned_by_label[label].get('qty'),
                    'side': planned_by_label[label].get('side'),
                }
                for label in expected_tp_labels
            ]
            if expected_tp_labels:
                expected_tp = True
                status['tp_expected'] = True
        valid_tp = []
        valid_sl = []
        mismatched = []
        invalid_price_orders = []
        liquidation_unsafe_orders = []
        liquidation_safety_results = []
        enforce_liquidation_safety = (
            str(getattr(self.exchange, 'id', '') or '').lower() == 'binance'
            or _safe_float_or_none(pos.get('liquidationPrice') or pos.get('liquidation_price')) is not None
        )
        for order in protection_orders:
            kind = self._classify_protection_order(order)
            order_side = self._protection_order_side(order)
            if order_side and order_side != close_side:
                mismatched.append(order)
                continue
            if kind in {'tp', 'sl'} and not self._is_reduce_only_order(order):
                mismatched.append(order)
                continue
            trigger_price = self._protection_trigger_price(order)
            order_price = _safe_float_or_none(order.get('price')) or trigger_price
            if pos_entry_price > 0 and order_price:
                if kind == 'sl':
                    working_type = self._protection_working_type(order)
                    liquidation_result = self._validate_existing_position_stop_liquidation(
                        symbol,
                        pos,
                        order_price,
                        working_type,
                        order,
                    )
                    if enforce_liquidation_safety:
                        if liquidation_result is None:
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
                    invalid_sl = False if managed_sl_active else (
                        (pos_side == 'long' and float(order_price) >= pos_entry_price)
                        or (pos_side == 'short' and float(order_price) <= pos_entry_price)
                    )
                    if invalid_sl:
                        invalid_price_orders.append(order)
                        continue
                elif kind == 'tp':
                    invalid_tp = (
                        pos_side == 'long' and float(order_price) <= pos_entry_price
                    ) or (
                        pos_side == 'short' and float(order_price) >= pos_entry_price
                    )
                    if invalid_tp:
                        invalid_price_orders.append(order)
                        continue
            if kind == 'tp':
                valid_tp.append(order)
            elif kind == 'sl':
                valid_sl.append(order)

        if mismatched:
            status['mismatch_cancelled'] = await self._cancel_protection_orders(
                symbol,
                reason='wrong close side',
                orders=mismatched
            )

        if invalid_price_orders:
            status['invalid_price_cancelled'] = await self._cancel_protection_orders(
                symbol,
                reason='invalid protection price for current position',
                orders=invalid_price_orders
            )

        if liquidation_unsafe_orders:
            status['invalid_price_cancelled'] += await self._cancel_protection_orders(
                symbol,
                reason='liquidation safety validation failed',
                orders=liquidation_unsafe_orders,
            )
            status['sl_present'] = False
            status['missing_sl'] = True
            status['status'] = 'LIQUIDATION_SAFETY_FAILED'
            reason = status.get('liquidation_safety_reason') or 'LIQUIDATION_PRICE_UNAVAILABLE'
            self.last_protection_order_status[symbol] = status
            if hasattr(self, '_handle_liquidation_safety_failure'):
                close_status = await self._handle_liquidation_safety_failure(
                    symbol,
                    pos,
                    reason,
                    stop_price=status.get('stop_price'),
                    working_type=status.get('stop_working_type'),
                    cfg=None,
                )
                status['emergency_close_status'] = close_status.get('status')
                status['emergency_close_closed'] = bool(close_status.get('closed'))
            return status

        try:
            strategy_params = self.get_runtime_strategy_params()
            active_strategy = str(strategy_params.get('active_strategy', '') or '').lower()
        except Exception:
            active_strategy = ''
        allow_split_tp = active_strategy in UTBREAKOUT_STRATEGIES
        for kind, valid_orders in (('tp', valid_tp), ('sl', valid_sl)):
            if len(valid_orders) <= 1:
                continue
            if kind == 'tp' and allow_split_tp:
                continue
            keep = self._newest_protection_order(valid_orders)
            duplicates = [
                order for order in valid_orders
                if (self._protection_order_id(order) or id(order)) != (self._protection_order_id(keep) or id(keep))
            ]
            if duplicates:
                status['duplicate_cancelled'] += await self._cancel_protection_orders(
                    symbol,
                    reason=f'duplicate {kind.upper()} protection',
                    orders=duplicates
                )
                if kind == 'tp':
                    valid_tp = [keep]
                else:
                    valid_sl = [keep]

        status['tp_count'] = len(valid_tp)
        status['sl_count'] = len(valid_sl)
        status['order_types'] = [self._protection_order_type(order) for order in protection_orders]
        tp_orders_by_label = {}
        for order in valid_tp:
            label = self._protection_tp_label(order, planned_tp_orders)
            if label:
                tp_orders_by_label.setdefault(label, []).append(order)
        status['tp_labels_present'] = sorted(tp_orders_by_label.keys())
        status['tp1_present'] = bool(tp_orders_by_label.get('TP1'))
        status['tp2_present'] = bool(tp_orders_by_label.get('TP2'))
        status['actual_tp_count'] = len(valid_tp)
        status['tp_present'] = len(valid_tp) > 0
        status['sl_present'] = len(valid_sl) > 0
        if expected_tp_labels:
            status['missing_tp1'] = 'TP1' in expected_tp_labels and not status['tp1_present']
            status['missing_tp2'] = 'TP2' in expected_tp_labels and not status['tp2_present']
            status['missing_tp'] = status['missing_tp1'] or status['missing_tp2']
        else:
            status['missing_tp'] = bool(expected_tp) and not status['tp_present']
        status['missing_sl'] = bool(expected_sl) and not status['sl_present']
        tp_qty_mismatches = []
        tp_price_mismatches = []
        for label in expected_tp_labels:
            order = self._newest_protection_order(tp_orders_by_label.get(label) or [])
            if not order:
                continue
            plan = planned_by_label.get(label, {})
            expected_qty = plan.get('qty')
            preserve_runner = bool(
                isinstance(runner_state, dict)
                and (
                    runner_state.get('preserve_runner_qty')
                    or _safe_float_value(
                        runner_state.get('runner_pct'),
                        0.0,
                    ) > 0
                )
            )
            if (
                label == 'TP2'
                and isinstance(runner_state, dict)
                and bool(runner_state.get('tp1_filled', False))
                and not preserve_runner
            ):
                expected_qty = current_qty
            actual_qty = self._protection_order_amount(order)
            if expected_qty is not None and actual_qty is not None and not self._qty_matches_plan(expected_qty, actual_qty):
                tp_qty_mismatches.append(label)
            actual_price = _safe_float_or_none(order.get('price')) or self._protection_trigger_price(order)
            if plan.get('price') is not None and actual_price is not None and not self._price_matches_plan(plan.get('price'), actual_price):
                tp_price_mismatches.append(label)
        status['tp_qty_mismatch_labels'] = sorted(tp_qty_mismatches)
        status['tp_price_mismatch_labels'] = sorted(tp_price_mismatches)
        status['tp_qty_mismatch'] = bool(tp_qty_mismatches)
        status['tp2_qty_mismatch'] = 'TP2' in tp_qty_mismatches
        status['tp_price_mismatch'] = bool(tp_price_mismatches)
        sl_qty_mismatch = False
        for order in valid_sl:
            actual_sl_qty = self._protection_order_amount(order)
            if actual_sl_qty is not None and current_qty > 0 and not self._qty_matches_plan(current_qty, actual_sl_qty):
                sl_qty_mismatch = True
                break
        status['sl_qty_mismatch'] = sl_qty_mismatch
        position_signature = self._protection_position_signature(pos)
        missing_required_count = max(
            1,
            int(getattr(self, 'PROTECTION_MISSING_REQUIRED_COUNT', 2) or 2)
        )
        missing_min_age = max(
            0.0,
            float(getattr(self, 'PROTECTION_MISSING_MIN_AGE_SEC', 2.0) or 0.0)
        )
        issue_key = None
        issue_message = None
        issue_severity = None
        if status['missing_sl'] and status['missing_tp2']:
            status['status'] = 'MISSING_SL_AND_TP2'
            issue_key = 'missing_sl_tp2'
            issue_severity = 'critical'
            issue_message = (
                f"🚨 {self.ctrl.format_symbol_for_display(symbol)} 보호주문 누락 확인: "
                "SL과 TP2 없음. 같은 포지션에서 2회 연속 확인됨. 거래소 주문을 즉시 확인하세요."
            )
        elif status['missing_sl']:
            status['status'] = 'MISSING_SL'
            issue_key = 'missing_sl'
            issue_severity = 'critical'
            issue_message = (
                f"🚨 {self.ctrl.format_symbol_for_display(symbol)} 보호주문 누락 확인: "
                "SL 없음. 같은 포지션에서 2회 연속 확인됨. 거래소 주문을 즉시 확인하세요."
            )
        elif status['missing_tp2']:
            status['status'] = 'MISSING_TP2'
            issue_key = 'missing_tp2'
            issue_severity = 'warning'
            issue_message = (
                f"⚠️ {self.ctrl.format_symbol_for_display(symbol)} 보호주문 누락 확인: "
                "TP2 없음. 같은 포지션에서 2회 연속 확인됨."
            )
        elif status['missing_tp1']:
            status['status'] = 'MISSING_TP1'
            issue_key = 'missing_tp1'
            issue_severity = 'warning'
            issue_message = (
                f"⚠️ {self.ctrl.format_symbol_for_display(symbol)} 보호주문 누락 확인: "
                "TP1 없음. 같은 포지션에서 2회 연속 확인됨."
            )
        elif status['missing_tp']:
            status['status'] = 'MISSING_TP'
            issue_key = 'missing_tp'
            issue_severity = 'warning'
            issue_message = (
                f"⚠️ {self.ctrl.format_symbol_for_display(symbol)} 보호주문 누락 확인: "
                "TP 없음. 같은 포지션에서 2회 연속 확인됨."
            )
        elif status['mismatch_cancelled']:
            status['status'] = 'MISMATCH_CANCELLED'
        elif status['invalid_price_cancelled']:
            status['status'] = 'INVALID_PRICE_CANCELLED'
        elif status['duplicate_cancelled']:
            status['status'] = 'DUPLICATE_CANCELLED'
        elif status['tp2_qty_mismatch']:
            status['status'] = 'TP2_QTY_MISMATCH'
        elif status['tp_qty_mismatch']:
            status['status'] = 'TP_QTY_MISMATCH'
        elif status['tp_price_mismatch']:
            status['status'] = 'TP_PRICE_MISMATCH'
        elif status['sl_qty_mismatch']:
            status['status'] = 'SL_QTY_MISMATCH'
        else:
            status['status'] = 'OK'
        if issue_key:
            self._clear_protection_missing_candidates(
                symbol,
                position_signature=position_signature,
                issue_keys={issue_key}
            )
            confirmed = self._confirm_protection_missing_issue(
                symbol,
                issue_key,
                position_signature,
                required_count=missing_required_count,
                min_age_sec=missing_min_age
            )
            status['missing_issue_key'] = issue_key
            status['missing_issue_severity'] = issue_severity
            status['missing_confirmed'] = bool(confirmed)
            if alert and confirmed:
                await self._notify_protection_issue(
                    symbol,
                    f"{issue_key}:{position_signature}",
                    issue_message,
                    cooldown_sec=30 * 24 * 60 * 60
                )
            protection_lockout_reason = None
            protection_label = None
            if confirmed and active_strategy in UTBREAKOUT_STRATEGIES:
                if status.get('missing_sl'):
                    protection_lockout_reason = "STOP_LOSS_PROTECTION_FAILED_FORCE_CLOSED"
                    protection_label = "SL"
                elif (
                    status.get('missing_tp')
                    or status.get('missing_tp1')
                    or status.get('missing_tp2')
                ):
                    protection_lockout_reason = "TAKE_PROFIT_PROTECTION_FAILED_FORCE_CLOSED"
                    protection_label = "TP"
            if protection_lockout_reason:
                close_status = await self._emergency_close_position_without_stop_loss(
                    symbol,
                    reason=(
                        f"{protection_label} protection missing confirmed after entry: "
                        f"{issue_key}"
                    ),
                    max_attempts=5,
                    lockout_reason=protection_lockout_reason,
                    protection_label=protection_label,
                    critical_pause_reason_code=(
                        "SL_FAILED_AND_EMERGENCY_CLOSE_FAILED"
                        if protection_label == "SL"
                        else "TP_FAILED_AND_EMERGENCY_CLOSE_FAILED"
                    ),
                )
                status['emergency_close_status'] = close_status.get('status')
                status['emergency_close_closed'] = bool(close_status.get('closed'))
                status['daily_lockout_reason'] = (
                    protection_lockout_reason if close_status.get('closed') else None
                )
        else:
            if getattr(self, 'protection_missing_candidates', {}).get(symbol):
                logger.info(f"Protection audit recovered: {symbol} SL/TP present")
            self._clear_protection_missing_candidates(symbol)
        self.last_protection_order_status[symbol] = status
        return status

    async def _create_protection_order_with_retries(
        self,
        symbol,
        order_type,
        side,
        qty,
        price,
        params,
        label,
        max_attempts=3,
        retry_delay_sec=0.7
    ):
        last_error = None
        total_attempts = max(1, int(max_attempts or 1))
        retry_delay = max(0.0, float(retry_delay_sec or 0.0))
        client_order_id = str((params or {}).get('newClientOrderId') or '')
        canonical_type = str(order_type or '').upper().replace('-', '_')
        canonical_type = {
            'STOP_MARKET': 'STOP_MARKET',
            'TAKE_PROFIT_MARKET': 'TAKE_PROFIT_MARKET',
        }.get(canonical_type, canonical_type)
        is_binance_algo = (
            str(getattr(self.exchange, 'id', '') or '').lower() == 'binance'
            and canonical_type in CONDITIONAL_TYPES
        )
        algo_gateway = BinanceAlgoOrderGateway(self.exchange) if is_binance_algo else None
        submit_params = dict(params or {})
        if canonical_type == 'STOP_MARKET':
            submit_params['workingType'] = 'MARK_PRICE'
            submit_params['priceProtect'] = False

        async def _recover_existing_protection_order():
            if not client_order_id:
                return None
            fetch_ok, open_orders = await self._collect_protection_orders_checked(symbol)
            if not fetch_ok:
                reason = f"ALGO_ORDER_SNAPSHOT_UNAVAILABLE:{symbol}"
                self._set_crypto_entry_lock(reason)
                raise ProtectionOrderLookupUnavailable(reason)
            existing = next(
                (
                    order for order in (open_orders or [])
                    if self._protection_client_order_id(order) == client_order_id
                ),
                None,
            )
            if existing is not None:
                return existing
            if algo_gateway is not None:
                lookup = await algo_gateway.fetch_by_client_id(client_order_id)
                if lookup.status == AlgoLookupStatus.FOUND:
                    return lookup.order
                if lookup.status == AlgoLookupStatus.UNKNOWN:
                    reason = f"ALGO_ORDER_LOOKUP_UNKNOWN:{symbol}:{lookup.error}"
                    self._set_crypto_entry_lock(reason)
                    if getattr(self, 'ctrl', None) is not None:
                        try:
                            await self.ctrl.notify(
                                f"CRITICAL: {symbol} protection order lookup unavailable. "
                                "New protection submission and entries are locked pending reconciliation."
                            )
                        except Exception:
                            logger.exception("Protection lookup warning notification failed")
                    raise ProtectionOrderLookupUnavailable(reason)
            return None

        for attempt in range(1, total_attempts + 1):
            existing = await _recover_existing_protection_order()
            if existing is not None:
                logger.info(
                    "%s protection order recovered before submit: %s clientOrderId=%s",
                    label,
                    symbol,
                    client_order_id,
                )
                return existing
            try:
                if algo_gateway is not None:
                    trigger_price = submit_params.get('triggerPrice') or submit_params.get('stopPrice')
                    if trigger_price in (None, ''):
                        raise ValueError(f"{label} conditional order requires trigger price")
                    order = await algo_gateway.create_conditional_order(
                        symbol,
                        canonical_type,
                        side,
                        qty,
                        trigger_price=trigger_price,
                        client_algo_id=client_order_id,
                        reduce_only=bool(submit_params.get('reduceOnly', True)),
                        working_type=str(submit_params.get('workingType') or 'MARK_PRICE'),
                        price_protect=bool(submit_params.get('priceProtect', False)),
                        position_side=submit_params.get('positionSide'),
                        close_position=bool(submit_params.get('closePosition', False)),
                        price=price,
                    )
                else:
                    order = await asyncio.to_thread(
                        self.exchange.create_order,
                        symbol,
                        order_type,
                        side,
                        qty,
                        price,
                        submit_params,
                    )
                if attempt > 1:
                    logger.info(f"{label} protection order succeeded on retry {attempt}: {symbol}")
                return order
            except Exception as e:
                last_error = e
                logger.error(f"{label} order attempt {attempt}/{total_attempts} failed for {symbol}: {e}")
                recovered = await _recover_existing_protection_order()
                if recovered is not None:
                    logger.warning(
                        "%s protection response was lost but exchange order was recovered: %s clientOrderId=%s",
                        label,
                        symbol,
                        client_order_id,
                    )
                    return recovered
                error_name = type(e).__name__.lower()
                error_text = str(e).lower()
                ambiguous = (
                    any(token in error_name for token in ('timeout', 'network', 'connection', 'exchangeunavailable'))
                    or any(token in error_text for token in ('timed out', 'timeout', 'connection reset', 'response lost'))
                )
                if ambiguous:
                    raise RuntimeError(
                        f"{label} SUBMITTED_UNKNOWN and could not be reconciled: {e}"
                    ) from e
            if attempt < total_attempts and retry_delay > 0:
                await asyncio.sleep(retry_delay)
        raise last_error

    async def _fail_closed_unprotected_position(
        self,
        symbol,
        *,
        reason,
        status_code,
        expected_tp=False,
        emergency_close=True,
    ):
        """Record a protection construction failure and flatten instead of leaving naked risk."""
        status = {
            'tp_expected': bool(expected_tp),
            'sl_expected': True,
            'tp_present': False,
            'sl_present': False,
            'missing_tp': bool(expected_tp),
            'missing_sl': True,
            'status': str(status_code or 'PROTECTION_SETUP_FAILED'),
            'reason': str(reason or 'protection setup failed'),
        }
        self.last_protection_order_status[symbol] = status
        if emergency_close:
            close_status = await self._emergency_close_position_without_stop_loss(
                symbol,
                reason=str(reason or 'protection setup failed'),
                max_attempts=5,
            )
            status['emergency_close_status'] = close_status.get('status')
            status['emergency_close'] = close_status
            self.last_protection_order_status[symbol] = status
        return status

    async def _emergency_close_position_without_stop_loss(
        self,
        symbol,
        *,
        reason='SL placement failed',
        max_attempts=5,
        lockout_reason="STOP_LOSS_PROTECTION_FAILED_FORCE_CLOSED",
        protection_label="SL",
        critical_pause_reason_code="SL_FAILED_AND_EMERGENCY_CLOSE_FAILED",
        persist_critical_pause=True,
    ):
        status = {
            'status': 'SKIPPED',
            'symbol': symbol,
            'attempts': 0,
            'closed': False,
            'error': None,
        }
        if self.is_upbit_mode():
            status['status'] = 'UPBIT_SKIPPED'
            return status

        last_error = None
        total_attempts = max(1, int(max_attempts or 1))
        for attempt in range(1, total_attempts + 1):
            self.position_cache = None
            self.position_cache_time = 0
            position_fetch_ok, pos = await self._fetch_server_position_checked(symbol)
            if not position_fetch_ok:
                last_error = RuntimeError("position fetch failed during emergency close")
                break
            if not pos:
                status.update({'status': 'ALREADY_FLAT', 'closed': True, 'attempts': attempt - 1})
                await self._reconcile_closed_position_protection(symbol, reason=reason, alert=True, attempts=2)
                return status

            side = str(pos.get('side', '') or '').lower()
            if side not in {'long', 'short'}:
                last_error = ValueError(f"invalid position side for emergency close: {side}")
                break
            contracts = abs(float(self._position_signed_contracts(pos) or pos.get('contracts', 0) or 0))
            qty = self.safe_amount(symbol, contracts)
            if float(qty) <= 0:
                last_error = ValueError(f"invalid emergency close qty: {qty}")
                break
            params = self._close_order_params_for_position(pos)

            try:
                status['attempts'] = attempt
                _ensure_trading_safety_runtime(self)
                close_submission = await self.crypto_execution.submit_reduce_only_close(
                    strategy='EMERGENCY_PROTECTION_CLOSE',
                    symbol=symbol,
                    position_side=side,
                    position_signature=(
                        pos.get('timestamp')
                        or pos.get('entryPrice')
                        or f"{side}:{contracts}"
                    ),
                    qty=float(qty),
                    reason=reason,
                    params=params,
                )
                if close_submission.state == OrderState.SUBMITTED_UNKNOWN.value:
                    raise RuntimeError(
                        f"emergency close submission unknown: {close_submission.client_order_id}"
                    )
            except Exception as close_error:
                last_error = close_error
                logger.error(
                    f"Emergency close after {protection_label} failure attempt {attempt}/{total_attempts} "
                    f"failed for {symbol}: {close_error}"
                )
                if attempt < total_attempts:
                    await asyncio.sleep(1.0)
                continue

            await asyncio.sleep(0.8)
            self.position_cache = None
            self.position_cache_time = 0
            remaining_fetch_ok, remaining = await self._fetch_server_position_checked(symbol)
            if not remaining_fetch_ok:
                last_error = RuntimeError("position verification failed after emergency close order")
                break
            if not remaining:
                await self._cancel_all_orders_variants(symbol, reason=f'after emergency close: {reason}')
                await self._reconcile_closed_position_protection(symbol, reason=reason, alert=True, attempts=3)
                try:
                    self._record_utbreakout_daily_sl_lockout(
                        symbol,
                        side=side,
                        reason=lockout_reason,
                        detail=(
                            f"{reason}; emergency close qty={qty}; "
                            f"attempt={attempt}/{total_attempts}"
                        ),
                    )
                except Exception as lockout_error:
                    logger.warning(
                        "Failed to record daily SL lockout after emergency close for %s: %s",
                        symbol,
                        lockout_error,
                    )
                status.update({'status': 'EMERGENCY_CLOSED', 'closed': True})
                _mark_crypto_symbol_closed(self, symbol, reason)
                await self.ctrl.notify(
                    f"🚨 {self.ctrl.format_symbol_for_display(symbol)} {protection_label} 생성 실패로 포지션을 즉시 시장가 청산했습니다."
                )
                return status

            logger.warning(
                f"Emergency close order accepted but position still open after {protection_label} failure: "
                f"{symbol} {remaining.get('side')} {remaining.get('contracts')}"
            )

        status.update({
            'status': 'CRITICAL_PAUSED',
            'emergency_close_status': 'EMERGENCY_CLOSE_FAILED',
            'closed': False,
            'error': str(last_error) if last_error else 'position still open',
        })
        if persist_critical_pause:
            try:
                self.critical_pause_reason = (
                    f"Emergency close failed after {protection_label} placement failure: {status['error']}"
                )
                self.critical_pause_status = dict(status)
                if getattr(self, 'ctrl', None) is not None and hasattr(self.ctrl, 'is_paused'):
                    self.ctrl.is_paused = True
                self._set_crypto_entry_lock(
                    f"CRITICAL_PAUSE:{critical_pause_reason_code}:{symbol}"
                )
            except Exception as pause_error:
                logger.exception("Critical pause runtime state update failed for %s", symbol)

            try:
                write_critical_pause_state(
                    symbol=symbol,
                    reason=critical_pause_reason_code,
                    exception=last_error if last_error else RuntimeError("position still open"),
                    cfg=self.get_runtime_common_settings() if hasattr(self, "get_runtime_common_settings") else None,
                    scope="GLOBAL",
                    reason_code=critical_pause_reason_code,
                    origin_symbol=symbol,
                )
            except Exception:
                logger.exception("Failed to persist CRITICAL_PAUSED state for %s", symbol)
        audit_fetch_ok, audit_pos = await self._fetch_server_position_checked(symbol)
        if audit_fetch_ok and protection_label != 'LIQUIDATION_SAFETY':
            await self._audit_protection_orders(symbol, pos=audit_pos, alert=True)
        await self.ctrl.notify(
            f"🚨 {self.ctrl.format_symbol_for_display(symbol)} {protection_label} 생성 실패 후 긴급 청산도 실패했습니다. "
            f"CRITICAL_PAUSED 상태로 전환했습니다. 거래소에서 즉시 수동 청산하세요: {status['error']}"
        )
        return status

    async def _cancel_protection_orders_by_kind(self, symbol, kinds, reason='protection cleanup'):
        wanted = {str(kind).lower() for kind in (kinds or [])}
        orders = await self._collect_protection_orders(symbol)
        selected = [
            order for order in (orders or [])
            if self._classify_protection_order(order) in wanted
        ]
        if not selected:
            return 0
        return await self._cancel_protection_orders(symbol, reason=reason, orders=selected)

    async def _replace_stop_loss_order(self, symbol, pos, stop_price, reason='stop replacement'):
        if not pos:
            return None
        side = str(pos.get('side', '') or '').lower()
        if side not in {'long', 'short'}:
            return None
        qty = self.safe_amount(symbol, abs(float(self._position_signed_contracts(pos) or pos.get('contracts', 0) or 0)))
        if float(qty) <= 0:
            return None
        sl_side = 'sell' if side == 'long' else 'buy'
        safe_stop = self.safe_price(symbol, float(stop_price))
        if str(getattr(self.exchange, 'id', '') or '').lower() == 'binance':
            position_fetch_ok, fresh_pos = await self._fetch_position_with_liquidation(symbol, pos)
            liquidation_result = (
                self._validate_position_stop_liquidation(
                    symbol,
                    fresh_pos,
                    safe_stop,
                    'MARK_PRICE',
                )
                if position_fetch_ok and fresh_pos
                else None
            )
            if liquidation_result is None or not liquidation_result.valid:
                failure_reason = (
                    liquidation_result.reason
                    if liquidation_result is not None
                    else 'LIQUIDATION_PRICE_UNAVAILABLE'
                )
                await self._handle_liquidation_safety_failure(
                    symbol,
                    fresh_pos or pos,
                    failure_reason,
                    stop_price=safe_stop,
                    working_type='MARK_PRICE',
                )
                return None
            pos = fresh_pos
        initial_fetch_ok, initial_orders = await self._collect_protection_orders_checked(symbol)
        if not initial_fetch_ok:
            self.last_protection_order_status[symbol] = {
                'tp_expected': False,
                'sl_expected': True,
                'tp_present': False,
                'sl_present': False,
                'tp_count': 0,
                'sl_count': 0,
                'missing_tp': False,
                'missing_sl': False,
                'duplicate_cancelled': 0,
                'status': 'SL_REPLACE_FETCH_FAILED'
            }
            logger.warning(
                f"SL replacement skipped for {symbol}: existing SL status could not be fetched ({reason})"
            )
            return None
        existing_sl = [
            order for order in (initial_orders or [])
            if self._classify_protection_order(order) == 'sl'
        ]
        newest_existing_sl = self._newest_protection_order(existing_sl)
        existing_stop = (
            self._protection_trigger_price(newest_existing_sl)
            if newest_existing_sl
            else None
        )
        reference_price = _safe_float_or_none(
            pos.get('markPrice')
            or pos.get('mark_price')
            or pos.get('last')
            or pos.get('currentPrice')
        )
        if reference_price is not None:
            stop_is_live_safe = (
                float(safe_stop) < float(reference_price)
                if side == 'long'
                else float(safe_stop) > float(reference_price)
            )
            if not stop_is_live_safe:
                logger.warning(
                    "SL replacement skipped for %s: requested stop %.12f is "
                    "already beyond current mark %.12f (%s)",
                    symbol,
                    float(safe_stop),
                    float(reference_price),
                    reason,
                )
                if existing_sl:
                    await self._notify_protection_issue(
                        symbol,
                        f"sl_replace_price_crossed:{self._protection_position_signature(pos)}",
                        f"UTBreak {self.ctrl.format_symbol_for_display(symbol)} SL 교체 보류: "
                        f"목표 SL {float(safe_stop):.8f}가 현재가 "
                        f"{float(reference_price):.8f}를 이미 통과했습니다. 기존 SL을 유지합니다.",
                        cooldown_sec=60,
                    )
                    return None
                self.last_protection_order_status[symbol] = {
                    'tp_expected': False,
                    'sl_expected': True,
                    'tp_present': False,
                    'sl_present': False,
                    'tp_count': 0,
                    'sl_count': 0,
                    'missing_tp': False,
                    'missing_sl': True,
                    'duplicate_cancelled': 0,
                    'status': 'SL_REPLACE_TARGET_CROSSED_UNPROTECTED',
                }
                await self._notify_protection_issue(
                    symbol,
                    f"sl_replace_crossed_unprotected:{self._protection_position_signature(pos)}",
                    f"🚨 {self.ctrl.format_symbol_for_display(symbol)} 보호 SL이 없고 "
                    f"교체 목표 {float(safe_stop):.8f}도 현재가 "
                    f"{float(reference_price):.8f}를 이미 통과했습니다. 즉시 비상청산합니다.",
                    cooldown_sec=60,
                )
                await self._emergency_close_position_without_stop_loss(
                    symbol,
                    reason=(
                        "SL replacement target already crossed while no "
                        f"existing SL was open: {reason}"
                    ),
                    max_attempts=5,
                )
                return None
        if existing_stop is not None:
            stop_tolerance = max(abs(float(safe_stop)) * 1e-9, 1e-12)
            if abs(float(existing_stop) - float(safe_stop)) <= stop_tolerance:
                logger.debug(
                    "SL replacement skipped for %s: existing stop already matches %s (%s)",
                    symbol,
                    safe_stop,
                    reason,
                )
                return newest_existing_sl
        cancelled_count = await self._cancel_protection_orders(
            symbol,
            reason=reason,
            orders=existing_sl
        )
        confirm_attempts = max(1, int(getattr(self, 'PROTECTION_REPLACE_CONFIRM_ATTEMPTS', 3) or 3))
        confirm_delay = max(0.0, float(getattr(self, 'PROTECTION_REPLACE_CONFIRM_DELAY', 0.25) or 0.0))
        remaining_sl = []
        latest_protection_orders = []
        confirmation_unverified = False
        for attempt in range(confirm_attempts):
            if confirm_delay > 0:
                await asyncio.sleep(confirm_delay)
            fetch_ok, protection_orders = await self._collect_protection_orders_checked(symbol)
            if not fetch_ok:
                if existing_sl and cancelled_count > 0:
                    confirmation_unverified = True
                    logger.warning(
                        f"SL cancellation confirmation unavailable for {symbol}; creating the replacement "
                        f"to avoid leaving the position unprotected ({reason})"
                    )
                    break
                status = {
                    'tp_expected': False,
                    'sl_expected': True,
                    'tp_present': False,
                    'sl_present': False,
                    'tp_count': 0,
                    'sl_count': 0,
                    'missing_tp': False,
                    'missing_sl': True,
                    'duplicate_cancelled': 0,
                    'status': 'SL_REPLACE_FETCH_FAILED'
                }
                self.last_protection_order_status[symbol] = status
                logger.warning(
                    f"SL replacement aborted for {symbol}: open-order fetch failed during "
                    f"confirmation ({reason})"
                )
                await self._notify_protection_issue(
                    symbol,
                    f"sl_replace_fetch_failed:{self._protection_position_signature(pos)}",
                    f"🚨 {self.ctrl.format_symbol_for_display(symbol)} SL 교체 중단: "
                    "기존 SL 취소 여부를 조회하지 못해 새 SL을 만들지 않았습니다.",
                    cooldown_sec=60
                )
                return None
            latest_protection_orders = list(protection_orders or [])
            remaining_sl = [
                order for order in latest_protection_orders
                if self._classify_protection_order(order) == 'sl'
            ]
            if not remaining_sl:
                break
            if attempt < confirm_attempts - 1:
                await self._cancel_protection_orders(
                    symbol,
                    reason=f"{reason} confirm retry {attempt + 1}",
                    orders=remaining_sl
                )

        if remaining_sl:
            status = {
                'tp_expected': False,
                'sl_expected': True,
                'tp_present': False,
                'sl_present': True,
                'tp_count': 0,
                'sl_count': len(remaining_sl),
                'missing_tp': False,
                'missing_sl': False,
                'duplicate_cancelled': 0,
                'status': 'SL_REPLACE_CANCEL_FAILED'
            }
            self.last_protection_order_status[symbol] = status
            logger.warning(
                f"SL replacement aborted for {symbol}: existing SL still open after "
                f"{confirm_attempts} confirmation attempt(s) ({reason})"
            )
            await self._notify_protection_issue(
                symbol,
                f"sl_replace_cancel_failed:{self._protection_position_signature(pos)}",
                f"🚨 {self.ctrl.format_symbol_for_display(symbol)} SL 교체 중단: "
                f"기존 SL {len(remaining_sl)}건 취소 확인 실패. 중복 SL 방지를 위해 새 SL을 만들지 않았습니다.",
                cooldown_sec=60
            )
            return None

        replacement = await self._create_protection_order_with_retries(
            symbol,
            'stop_market',
            sl_side,
            qty,
            None,
            {
                'stopPrice': safe_stop,
                'reduceOnly': True,
                'newClientOrderId': self._build_protection_client_order_id(
                    symbol,
                    side,
                    'sl',
                    pos,
                    trigger_price=safe_stop,
                    quantity=qty,
                    leg='sl',
                    position_identity=(
                        pos.get('entry_client_order_id')
                        or pos.get('clientOrderId')
                        or pos.get('timestamp')
                        or self._protection_position_signature(pos)
                    ),
                )
            },
            'SL',
            max_attempts=3
        )
        if replacement and confirmation_unverified:
            self.last_protection_order_status[symbol] = {
                'tp_expected': False,
                'sl_expected': True,
                'tp_present': False,
                'sl_present': True,
                'tp_count': 0,
                'sl_count': 1,
                'missing_tp': False,
                'missing_sl': False,
                'duplicate_cancelled': 0,
                'status': 'SL_REPLACED_CONFIRMATION_UNVERIFIED'
            }
        if replacement:
            self._persist_active_entry_protection_refs(
                symbol,
                stop_order_id=self._protection_order_id(replacement),
            )
        return replacement
