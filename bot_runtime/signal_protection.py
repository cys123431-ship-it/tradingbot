"""Protective-order inspection, audit, repair, and stop replacement."""

from __future__ import annotations

from .ema200_utbot_rsi import EMA200_UTBOT_RSI_STRATEGY

from utbreakout.small_account.risk import (
    classify_stop_geometry,
    position_mark_price,
)
from utbreakout.small_account.state import is_managed_position_state


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

    @staticmethod
    def _managed_position_side(value):
        text = str(value or '').strip().lower()
        if text in {'long', 'buy'}:
            return 'long'
        if text in {'short', 'sell'}:
            return 'short'
        return ''

    @staticmethod
    def _binance_bool(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value or '').strip().lower()
        if text in {'true', '1', 'yes', 'y', 'on'}:
            return True
        if text in {'false', '0', 'no', 'n', 'off'}:
            return False
        return None

    async def _binance_position_mode_status(self, symbol=None):
        exchange_id = str(getattr(self.exchange, 'id', '') or '').strip().lower()
        if exchange_id not in {'binance', 'binanceusdm'}:
            return {
                'ok': True,
                'status': 'NOT_BINANCE',
                'hedged': False,
            }
        try:
            fetch_mode = getattr(self.exchange, 'fetch_position_mode', None)
            if callable(fetch_mode):
                response = await asyncio.to_thread(fetch_mode, symbol)
            else:
                endpoint = (
                    getattr(self.exchange, 'fapiPrivateGetPositionSideDual', None)
                    or getattr(
                        self.exchange,
                        'fapi_private_get_position_side_dual',
                        None,
                    )
                )
                if not callable(endpoint):
                    raise RuntimeError(
                        'Binance position mode endpoint is unavailable'
                    )
                response = await asyncio.to_thread(endpoint)
        except Exception as exc:
            return {
                'ok': False,
                'status': 'POSITION_MODE_UNAVAILABLE',
                'hedged': None,
                'reason': f'{type(exc).__name__}: {exc}',
            }

        raw = None
        if isinstance(response, dict):
            raw = response.get('hedged')
            if raw is None:
                raw = response.get('dualSidePosition')
            if raw is None and isinstance(response.get('info'), dict):
                raw = response['info'].get('dualSidePosition')
        hedged = self._binance_bool(raw)
        if hedged is None:
            return {
                'ok': False,
                'status': 'POSITION_MODE_UNAVAILABLE',
                'hedged': None,
                'reason': f'unrecognized Binance position mode response: {response}',
            }
        if hedged:
            return {
                'ok': False,
                'status': 'UNSUPPORTED_HEDGE_MODE',
                'hedged': True,
                'reason': (
                    'Binance Hedge Mode is not supported for managed crypto '
                    'futures protection'
                ),
            }
        return {
            'ok': True,
            'status': 'ONE_WAY',
            'hedged': False,
        }

    async def _require_binance_one_way_mode(
        self,
        symbol,
        *,
        operation='crypto futures operation',
        record_ema_status=False,
    ):
        result = await self._binance_position_mode_status(symbol)
        if result.get('ok'):
            return result

        status_code = str(result.get('status') or 'POSITION_MODE_UNAVAILABLE')
        reason = str(result.get('reason') or status_code)
        lock_reason = f'{status_code}:{symbol}'
        try:
            self._set_crypto_entry_lock(lock_reason)
        except Exception:
            logger.exception(
                'Failed to persist crypto entry lock for %s position-mode failure',
                symbol,
            )
        if record_ema_status:
            setter = getattr(self, '_set_ema200_profit_stop_status', None)
            if callable(setter):
                setter(symbol, status_code, reason=reason)
            else:
                states = getattr(self, 'last_ema200_profit_stop_status', None)
                if not isinstance(states, dict):
                    states = {}
                    self.last_ema200_profit_stop_status = states
                states[symbol] = {
                    'status': status_code,
                    'reason': reason,
                }
        logger.warning(
            '%s blocked for %s: %s',
            operation,
            symbol,
            reason,
        )
        return result

    def _protection_replace_lock(self, symbol):
        locks = getattr(self, '_protection_replace_locks', None)
        if not isinstance(locks, dict):
            locks = {}
            self._protection_replace_locks = locks
        key = self._normalize_protection_symbol(symbol) or str(symbol)
        lock = locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            locks[key] = lock
        return lock

    def _compatible_stop_orders_for_position(self, pos, orders):
        pos_side = self._managed_position_side((pos or {}).get('side'))
        if pos_side not in {'long', 'short'}:
            return []
        close_side = 'sell' if pos_side == 'long' else 'buy'
        try:
            current_qty = abs(float(
                self._position_signed_contracts(pos)
                or (pos or {}).get('contracts', 0)
                or 0
            ))
        except (TypeError, ValueError):
            current_qty = 0.0
        compatible = []
        for order in orders or []:
            if self._classify_protection_order(order) != 'sl':
                continue
            order_side = self._protection_order_side(order)
            if order_side and order_side != close_side:
                continue
            order_qty = self._protection_order_amount(order)
            if (
                current_qty > 0
                and order_qty is not None
                and not self._qty_matches_plan(current_qty, order_qty)
            ):
                continue
            trigger = self._protection_trigger_price(order)
            if trigger is None:
                continue
            compatible.append(order)
        return compatible

    def _best_stop_order_for_position(self, pos, orders):
        side = self._managed_position_side((pos or {}).get('side'))
        compatible = self._compatible_stop_orders_for_position(pos, orders)
        if not compatible:
            return None
        key = lambda order: float(self._protection_trigger_price(order))
        return (
            max(compatible, key=key)
            if side == 'long'
            else min(compatible, key=key)
        )

    def _stop_is_at_least_as_protective(self, side, existing_stop, target_stop):
        existing = _safe_float_or_none(existing_stop)
        target = _safe_float_or_none(target_stop)
        if existing is None or target is None:
            return False
        tolerance = max(abs(float(target)) * 1e-9, 1e-12)
        if str(side).lower() == 'long':
            return float(existing) + tolerance >= float(target)
        if str(side).lower() == 'short':
            return float(existing) - tolerance <= float(target)
        return False

    def _ema200_record_allows_current_position(self, record, pos):
        """Allow partial reductions without losing EMA entry attribution."""
        if record is None or not isinstance(pos, dict):
            return False
        current_side = self._managed_position_side(pos.get('side'))
        record_side = self._managed_position_side(
            getattr(record, 'side', '')
            or (getattr(record, 'metadata', {}) or {}).get('position_side')
        )
        if current_side not in {'long', 'short'} or record_side != current_side:
            return False
        try:
            current_qty = abs(float(
                self._position_signed_contracts(pos)
                or pos.get('contracts', 0)
                or 0
            ))
        except (TypeError, ValueError):
            return False
        if current_qty <= 0:
            return False

        record_qty = None
        for value in (
            getattr(record, 'filled_qty', None),
            getattr(record, 'requested_qty', None),
        ):
            parsed = _safe_float_or_none(value)
            if parsed is not None and parsed > 0:
                record_qty = float(parsed)
                break
        if record_qty is not None:
            tolerance = max(1e-9, abs(record_qty) * 0.001)
            # A manual/strategy partial close may reduce quantity, but a larger
            # live quantity can represent an external add or a different
            # lifecycle and must not be silently attributed to this entry.
            if current_qty > record_qty + tolerance:
                return False

        record_entry = _safe_float_or_none(
            getattr(record, 'average_fill_price', None)
        )
        current_entry = _safe_float_or_none(
            pos.get('entryPrice') or pos.get('entry_price')
        )
        if record_entry is not None and current_entry is not None:
            if not self._price_matches_plan(
                record_entry,
                current_entry,
                tolerance_pct=0.1,
            ):
                return False
        return True

    def _ema200_matching_position_records(self, symbol, pos, records=None):
        if records is None:
            store = getattr(self, 'trading_state_store', None)
            if store is None:
                return []
            try:
                records = store.active_for_symbol(symbol) or []
            except Exception:
                logger.exception(
                    'EMA200 position-state lookup failed for %s',
                    symbol,
                )
                return []
        side = self._managed_position_side((pos or {}).get('side'))
        try:
            current_qty = abs(float(
                self._position_signed_contracts(pos)
                or (pos or {}).get('contracts', 0)
                or 0
            ))
        except (TypeError, ValueError):
            current_qty = 0.0
        matched = []
        for record in records or []:
            if (
                str(getattr(record, 'strategy', '') or '').strip().lower()
                != EMA200_UTBOT_RSI_STRATEGY
            ):
                continue
            record_side = self._managed_position_side(
                getattr(record, 'side', '')
                or (getattr(record, 'metadata', {}) or {}).get('position_side')
            )
            if side and record_side != side:
                continue
            if not self._ema200_record_allows_current_position(record, pos):
                continue
            matched.append(record)
        matched.sort(
            key=lambda record: str(
                getattr(record, 'updated_at', '')
                or getattr(record, 'created_at', '')
                or ''
            ),
            reverse=True,
        )
        return matched

    def _ema200_primary_position_record(self, symbol, pos, records=None):
        matched = self._ema200_matching_position_records(
            symbol,
            pos,
            records=records,
        )
        if len(matched) > 1:
            logger.warning(
                'Multiple EMA200 active records match %s; using newest record %s',
                symbol,
                getattr(matched[0], 'client_order_id', None),
            )
        return matched[0] if matched else None

    @staticmethod
    def _ema200_pending_metadata_keys():
        return (
            'ema200_profit_stop_pending_client_order_id',
            'ema200_profit_stop_pending_side',
            'ema200_profit_stop_pending_qty',
            'ema200_profit_stop_pending_trigger_price',
            'ema200_profit_stop_pending_created_at_ns',
            'ema200_profit_stop_pending_position_signature',
        )

    def _update_ema200_record_metadata(
        self,
        record,
        *,
        updates=None,
        remove_keys=(),
        stop_order_id=None,
        update_stop_order_id=False,
    ):
        store = getattr(self, 'trading_state_store', None)
        if store is None or record is None:
            return None
        metadata = dict(getattr(record, 'metadata', {}) or {})
        for key in remove_keys or ():
            metadata.pop(key, None)
        if isinstance(updates, dict):
            metadata.update(updates)
        changes = {'metadata': metadata}
        if update_stop_order_id:
            changes['stop_order_id'] = stop_order_id
        return store.transition(
            record.client_order_id,
            record.order_state,
            **changes,
        )

    def _persist_ema200_profit_stop_pending_identity(
        self,
        symbol,
        pos,
        *,
        client_order_id,
        side,
        qty,
        trigger_price,
    ):
        record = self._ema200_primary_position_record(symbol, pos)
        if record is None:
            logger.warning(
                'EMA200 profit stop write-ahead skipped: no matching active record for %s',
                symbol,
            )
            return None
        return self._update_ema200_record_metadata(
            record,
            updates={
                'ema200_profit_stop_pending_client_order_id': str(
                    client_order_id or ''
                ),
                'ema200_profit_stop_pending_side': self._managed_position_side(
                    side
                ),
                'ema200_profit_stop_pending_qty': float(qty),
                'ema200_profit_stop_pending_trigger_price': float(trigger_price),
                'ema200_profit_stop_pending_created_at_ns': time.time_ns(),
                'ema200_profit_stop_pending_position_signature': (
                    self._protection_position_signature(pos)
                ),
            },
        )

    def _clear_ema200_profit_stop_pending_identity(self, record):
        if record is None:
            return None
        return self._update_ema200_record_metadata(
            record,
            remove_keys=self._ema200_pending_metadata_keys(),
        )

    def _ema200_record_matches_profit_stop_order(self, record, pos, order):
        if record is None or not isinstance(order, dict):
            return False
        if (
            str(getattr(record, 'strategy', '') or '').strip().lower()
            != EMA200_UTBOT_RSI_STRATEGY
        ):
            return False
        if self._classify_protection_order(order) != 'sl':
            return False
        pos_side = self._managed_position_side((pos or {}).get('side'))
        if pos_side not in {'long', 'short'}:
            return False
        record_side = self._managed_position_side(
            getattr(record, 'side', '')
            or (getattr(record, 'metadata', {}) or {}).get('position_side')
        )
        if record_side != pos_side:
            return False
        close_side = 'sell' if pos_side == 'long' else 'buy'
        order_side = self._protection_order_side(order)
        if order_side and order_side != close_side:
            return False
        record_symbol = str(getattr(record, 'symbol', '') or '').strip()
        if (
            record_symbol
            and not self._protection_order_matches_symbol(order, record_symbol)
        ):
            return False
        try:
            current_qty = abs(float(
                self._position_signed_contracts(pos)
                or (pos or {}).get('contracts', 0)
                or 0
            ))
        except (TypeError, ValueError):
            return False
        order_qty = self._protection_order_amount(order)
        if (
            current_qty <= 0
            or order_qty is None
            or not self._qty_matches_plan(current_qty, order_qty)
        ):
            return False

        if not self._ema200_record_allows_current_position(record, pos):
            return False

        metadata = dict(getattr(record, 'metadata', {}) or {})
        order_id = str(self._protection_order_id(order) or '').strip()
        client_id = str(self._protection_client_order_id(order) or '').strip()
        confirmed_order_ids = {
            str(value).strip()
            for value in (
                getattr(record, 'stop_order_id', None),
                metadata.get('ema200_profit_stop_order_id'),
            )
            if value not in (None, '')
        }
        confirmed_client_ids = {
            str(value).strip()
            for value in (
                metadata.get('ema200_profit_stop_client_order_id'),
                metadata.get('stop_client_order_id'),
            )
            if value not in (None, '')
        }
        if (
            (order_id and order_id in confirmed_order_ids)
            or (client_id and client_id in confirmed_client_ids)
        ):
            return True

        pending_client = str(
            metadata.get('ema200_profit_stop_pending_client_order_id') or ''
        ).strip()
        if not pending_client or not client_id or client_id != pending_client:
            return False
        pending_side = self._managed_position_side(
            metadata.get('ema200_profit_stop_pending_side')
        )
        if pending_side != pos_side:
            return False
        pending_qty = _safe_float_or_none(
            metadata.get('ema200_profit_stop_pending_qty')
        )
        if (
            pending_qty is None
            or not self._qty_matches_plan(current_qty, pending_qty)
            or not self._qty_matches_plan(order_qty, pending_qty)
        ):
            return False
        pending_trigger = _safe_float_or_none(
            metadata.get('ema200_profit_stop_pending_trigger_price')
        )
        order_trigger = self._protection_trigger_price(order)
        if pending_trigger is not None and order_trigger is not None:
            tolerance = max(abs(float(pending_trigger)) * 1e-9, 1e-12)
            if abs(float(order_trigger) - float(pending_trigger)) > tolerance:
                return False
        return True

    def _is_ema200_managed_profit_stop_order(
        self,
        symbol,
        pos,
        order,
        tracked_records=None,
    ):
        """Verify EMA200 stop ownership from durable order state, side and qty."""
        records = self._ema200_matching_position_records(
            symbol,
            pos,
            records=tracked_records,
        )
        return any(
            self._ema200_record_matches_profit_stop_order(record, pos, order)
            for record in records
        )

    def _persist_ema200_profit_stop_identity(self, symbol, pos, order):
        """Confirm EMA200 stop identity and clear its write-ahead marker."""
        store = getattr(self, 'trading_state_store', None)
        if store is None or not order:
            return 0
        record = self._ema200_primary_position_record(symbol, pos)
        if record is None:
            logger.warning(
                'EMA200 profit stop confirmation skipped: no matching record for %s',
                symbol,
            )
            return 0
        order_id = str(self._protection_order_id(order) or '').strip() or None
        client_id = str(self._protection_client_order_id(order) or '').strip() or None
        side = self._managed_position_side((pos or {}).get('side'))
        qty = self._protection_order_amount(order)
        self._update_ema200_record_metadata(
            record,
            updates={
                'ema200_profit_stop_order_id': order_id,
                'ema200_profit_stop_client_order_id': client_id,
                'ema200_profit_stop_side': side,
                'ema200_profit_stop_qty': float(qty) if qty is not None else None,
                'ema200_profit_stop_updated_at_ns': time.time_ns(),
                'ema200_profit_stop_position_signature': (
                    self._protection_position_signature(pos)
                ),
            },
            remove_keys=self._ema200_pending_metadata_keys(),
            stop_order_id=order_id,
            update_stop_order_id=True,
        )
        return 1

    @staticmethod
    def _protection_order_terminal_status(order):
        if not isinstance(order, dict):
            return ''
        info = order.get('info') if isinstance(order.get('info'), dict) else {}
        return str(
            order.get('status')
            or order.get('algoStatus')
            or info.get('algoStatus')
            or info.get('status')
            or ''
        ).strip().upper()

    async def _confirm_cancelled_stop_orders_absent(self, symbol, orders):
        """Fail closed unless every cancelled Binance Algo stop is terminal/absent."""
        selected = [
            order for order in (orders or [])
            if self._classify_protection_order(order) == 'sl'
        ]
        if not selected:
            return {'status': 'CONFIRMED_ABSENT', 'orders': []}

        exchange_id = str(getattr(self.exchange, 'id', '') or '').lower()
        if exchange_id not in {'binance', 'binanceusdm'}:
            return {
                'status': 'UNKNOWN',
                'reason': 'no authoritative post-cancel lookup for exchange',
            }

        gateway = BinanceAlgoOrderGateway(self.exchange)
        terminal = {'CANCELED', 'CANCELLED', 'EXPIRED', 'FILLED', 'REJECTED'}
        outcomes = []
        for order in selected:
            client_id = str(
                self._protection_client_order_id(order) or ''
            ).strip()
            if not client_id:
                return {
                    'status': 'UNKNOWN',
                    'reason': 'cancelled stop has no client order id',
                }
            lookup = await gateway.fetch_by_client_id(client_id)
            if lookup.status == AlgoLookupStatus.UNKNOWN:
                return {
                    'status': 'UNKNOWN',
                    'client_order_id': client_id,
                    'reason': lookup.error,
                }
            if lookup.status == AlgoLookupStatus.NOT_FOUND:
                outcomes.append({
                    'client_order_id': client_id,
                    'status': 'NOT_FOUND',
                })
                continue
            found = lookup.order or {}
            found_status = self._protection_order_terminal_status(found)
            if found_status in terminal:
                outcomes.append({
                    'client_order_id': client_id,
                    'status': found_status,
                })
                continue
            return {
                'status': 'FOUND_OPEN',
                'client_order_id': client_id,
                'order': found,
                'order_status': found_status or 'UNKNOWN_OPEN',
            }
        return {
            'status': 'CONFIRMED_ABSENT',
            'orders': outcomes,
        }

    async def _reconcile_ema200_profit_stop_pending_identity(
        self,
        symbol,
        *,
        pos=None,
        protection_orders=None,
    ):
        store = getattr(self, 'trading_state_store', None)
        if store is None:
            return {'status': 'NO_STORE'}
        try:
            if hasattr(store, 'records_for_symbol'):
                records = store.records_for_symbol(symbol) or []
            else:
                records = store.active_for_symbol(symbol) or []
        except Exception as exc:
            return {
                'status': 'STATE_LOOKUP_FAILED',
                'error': f'{type(exc).__name__}: {exc}',
            }
        pending_records = [
            record for record in records
            if (
                str(getattr(record, 'strategy', '') or '').strip().lower()
                == EMA200_UTBOT_RSI_STRATEGY
                and str(
                    (getattr(record, 'metadata', {}) or {}).get(
                        'ema200_profit_stop_pending_client_order_id'
                    )
                    or ''
                ).strip()
            )
        ]
        if not pending_records:
            return {'status': 'NO_PENDING'}

        open_orders = list(protection_orders or [])
        outcomes = []
        gateway = (
            BinanceAlgoOrderGateway(self.exchange)
            if str(getattr(self.exchange, 'id', '') or '').lower() == 'binance'
            else None
        )
        for record in pending_records:
            metadata = dict(getattr(record, 'metadata', {}) or {})
            client_id = str(
                metadata.get('ema200_profit_stop_pending_client_order_id') or ''
            ).strip()
            order = next(
                (
                    item for item in open_orders
                    if self._protection_client_order_id(item) == client_id
                ),
                None,
            )
            lookup_status = None
            if order is None and gateway is not None:
                lookup = await gateway.fetch_by_client_id(client_id)
                lookup_status = lookup.status
                if lookup.status == AlgoLookupStatus.FOUND:
                    order = lookup.order
                elif lookup.status == AlgoLookupStatus.UNKNOWN:
                    reason = (
                        f'PENDING_PROTECTION_LOOKUP_UNKNOWN:{symbol}:'
                        f'{lookup.error}'
                    )
                    self._set_crypto_entry_lock(reason)
                    outcomes.append({
                        'status': 'UNKNOWN',
                        'client_order_id': client_id,
                        'error': lookup.error,
                    })
                    continue
                elif lookup.status == AlgoLookupStatus.NOT_FOUND:
                    record_state = str(
                        getattr(record, 'order_state', '') or ''
                    ).upper()
                    terminal_lifecycle = record_state in {
                        'CLOSED',
                        'FAILED',
                        'CANCELED',
                        'CANCELLED',
                    }
                    if terminal_lifecycle:
                        self._clear_ema200_profit_stop_pending_identity(record)
                        outcomes.append({
                            'status': 'NOT_FOUND_CLEANED',
                            'client_order_id': client_id,
                            'record_state': record_state,
                        })
                    else:
                        reason = (
                            f'PENDING_PROTECTION_RECONCILIATION:{symbol}:'
                            f'NOT_FOUND_ACTIVE:{record_state or "UNKNOWN"}'
                        )
                        self._set_crypto_entry_lock(reason)
                        outcomes.append({
                            'status': 'NOT_FOUND_PRESERVED',
                            'client_order_id': client_id,
                            'record_state': record_state,
                        })
                    continue

            if order is not None and pos is not None:
                if self._ema200_record_matches_profit_stop_order(
                    record,
                    pos,
                    order,
                ):
                    self._persist_ema200_profit_stop_identity(
                        symbol,
                        pos,
                        order,
                    )
                    outcomes.append({
                        'status': 'FOUND_CONFIRMED',
                        'client_order_id': client_id,
                    })
                else:
                    outcomes.append({
                        'status': 'FOUND_MISMATCH',
                        'client_order_id': client_id,
                    })
                continue

            if lookup_status == AlgoLookupStatus.NOT_FOUND:
                outcomes.append({
                    'status': 'NOT_FOUND_PRESERVED',
                    'client_order_id': client_id,
                })
            else:
                outcomes.append({
                    'status': 'PENDING_PRESERVED',
                    'client_order_id': client_id,
                })

        if len(outcomes) == 1:
            return outcomes[0]
        return {
            'status': 'MULTI',
            'outcomes': outcomes,
        }

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
        revision=None,
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
            str(revision or ''),
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
        is_binance = str(getattr(self.exchange, 'id', '') or '').lower() in {'binance', 'binanceusdm'}
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
            # Binance's symbol-scoped regular-order endpoint is authoritative.
            # Conditional/algo orders are queried separately below, so an empty
            # result does not require the expensive all-symbol fallback.
            if open_orders or scope is None or (is_binance and scope is not None):
                break

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

            position_fetch_ok, pos = await self._fetch_server_position_checked(symbol)
            if not position_fetch_ok:
                status['pending'] += len(orders)
                status['symbols'][symbol] = {
                    'pending': len(orders),
                    'cancelled': 0,
                    'status': 'POSITION_RECHECK_FAILED',
                }
                logger.warning(
                    "Protection orphan recheck failed for %s; retaining %s order(s)",
                    symbol,
                    len(orders),
                )
                continue
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
                pending_status = await self._reconcile_ema200_profit_stop_pending_identity(
                    symbol,
                    pos=None,
                    protection_orders=[],
                )
                closed_records = _mark_crypto_symbol_closed(self, symbol, reason)
                final_status['ema200_pending_reconciliation'] = pending_status
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
            final_status['ema200_pending_reconciliation'] = (
                await self._reconcile_ema200_profit_stop_pending_identity(
                    symbol,
                    pos=None,
                    protection_orders=[],
                )
            )
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
            active_strategy = str(
                self.get_runtime_strategy_params().get('active_strategy', '') or ''
            ).lower()
        except Exception:
            active_strategy = ''
        position_strategy = self._position_entry_strategy(symbol)
        strategy_owner = position_strategy or active_strategy
        if strategy_owner == EMA200_UTBOT_RSI_STRATEGY:
            # The first sub-$1,000 stage is intentionally strategy-managed with
            # no exchange stop.  That exception is durable and narrowly bound
            # to an EMA200 entry record; every later loss stage still requires
            # the emergency stop.
            try:
                records = self.trading_state_store.active_for_symbol(symbol)
            except Exception:
                records = []
            ema_records = [
                record for record in records
                if str(record.strategy or '').strip().lower()
                == EMA200_UTBOT_RSI_STRATEGY
            ]
            if ema_records and all(
                bool((record.metadata or {}).get('strategy_managed_no_stop'))
                and not getattr(record, 'stop_order_id', None)
                for record in ema_records
            ):
                return False, False
            return False, True
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
            'managed_stop_trigger_pending': False,
            'managed_stop_trigger_prices': [],
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

        exchange_id = str(getattr(self.exchange, 'id', '') or '').lower()
        if exchange_id in {'binance', 'binanceusdm'}:
            mode = await self._require_binance_one_way_mode(
                symbol,
                operation='protection audit',
                record_ema_status=False,
            )
            if not mode.get('ok'):
                status_code = str(
                    mode.get('status') or 'POSITION_MODE_UNAVAILABLE'
                )
                status['status'] = status_code
                status['position_mode_status'] = status_code
                status['position_mode_reason'] = mode.get('reason')
                status['tp_count'] = sum(
                    1 for order in protection_orders
                    if self._classify_protection_order(order) == 'tp'
                )
                status['sl_count'] = sum(
                    1 for order in protection_orders
                    if self._classify_protection_order(order) == 'sl'
                )
                status['tp_present'] = status['tp_count'] > 0
                status['sl_present'] = status['sl_count'] > 0
                status['missing_tp'] = False
                status['missing_sl'] = False
                self._clear_protection_missing_candidates(symbol)
                self.last_protection_order_status[symbol] = status
                logger.warning(
                    'Protection audit mutation blocked for %s: %s',
                    symbol,
                    mode.get('reason') or status_code,
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
        try:
            pending_reconcile = await self._reconcile_ema200_profit_stop_pending_identity(
                symbol,
                pos=pos,
                protection_orders=protection_orders,
            )
            status['ema200_pending_reconciliation'] = pending_reconcile
            if hasattr(self, 'trading_state_store'):
                tracked_records = self.trading_state_store.active_for_symbol(symbol) or []
        except Exception:
            logger.exception(
                'EMA200 pending profit-stop reconciliation failed for %s',
                symbol,
            )
            status['ema200_pending_reconciliation'] = {
                'status': 'ERROR',
            }
        external_position = not isinstance(runner_state, dict) and not bool(tracked_records)
        status['external_position'] = bool(external_position)
        if external_position:
            expected_tp = False
            status['tp_expected'] = False
        managed_position_state = is_managed_position_state(
            runner_state,
            side=pos_side,
        )
        current_mark_price = position_mark_price(pos)
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
                    ema200_managed_stop = self._is_ema200_managed_profit_stop_order(
                        symbol,
                        pos,
                        order,
                        tracked_records=tracked_records,
                    )
                    bot_managed_stop = bool(
                        (
                            managed_position_state
                            and self._is_bot_managed_protection_order(order)
                        )
                        or ema200_managed_stop
                    )
                    if ema200_managed_stop:
                        status.setdefault('ema200_managed_stop_order_ids', []).append(
                            self._protection_order_id(order)
                        )
                    stop_geometry = classify_stop_geometry(
                        side=pos_side,
                        stop_price=order_price,
                        entry_price=pos_entry_price,
                        mark_price=current_mark_price,
                        bot_managed=bot_managed_stop,
                    )
                    if stop_geometry.crossed_live_mark:
                        # Never cancel an accepted managed stop at the exact
                        # moment its trigger is being crossed.  Binance may be
                        # transitioning the Algo order to its execution order;
                        # cancellation here creates a naked position window.
                        status['managed_stop_trigger_pending'] = True
                        status['managed_stop_trigger_prices'].append(
                            float(order_price)
                        )
                    if not stop_geometry.valid:
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
            keep = (
                self._best_stop_order_for_position(pos, valid_orders)
                if kind == 'sl'
                else self._newest_protection_order(valid_orders)
            )
            if keep is None:
                continue
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
        if status['managed_stop_trigger_pending'] and isinstance(runner_state, dict):
            runner_state['protection_status'] = 'TRIGGER_PENDING'
            runner_state['managed_stop_trigger_prices'] = list(
                status['managed_stop_trigger_prices']
            )
            self._set_utbreakout_trailing_state(symbol, runner_state)
        elif (
            status['sl_present']
            and isinstance(runner_state, dict)
            and managed_position_state
        ):
            runner_state['protection_status'] = 'PROTECTED'
            runner_state.pop('managed_stop_trigger_prices', None)
            self._set_utbreakout_trailing_state(symbol, runner_state)
        status['tp_orders'] = [
            {
                'tp_label': self._protection_tp_label(order, planned_tp_orders),
                'price': (
                    _safe_float_or_none(order.get('price'))
                    or self._protection_trigger_price(order)
                ),
                'qty': self._protection_order_amount(order),
                'order_id': self._protection_order_id(order),
            }
            for order in valid_tp
        ]
        if expected_tp_labels:
            status['missing_tp1'] = 'TP1' in expected_tp_labels and not status['tp1_present']
            status['missing_tp2'] = 'TP2' in expected_tp_labels and not status['tp2_present']
            status['missing_tp'] = status['missing_tp1'] or status['missing_tp2']
        else:
            status['missing_tp'] = bool(expected_tp) and not status['tp_present']
        status['missing_sl'] = bool(expected_sl) and not status['sl_present']

        # Binance can remove a filled TP order from the open-order snapshot a
        # few moments before the position endpoint reflects the reduced size.
        # Re-read the position before classifying a planned TP as missing so a
        # normal partial fill cannot be mistaken for lost protection.
        if (
            planned_by_label
            and status['missing_tp']
            and isinstance(runner_state, dict)
        ):
            position_fetch_ok, fresh_pos = await self._fetch_server_position_checked(symbol)
            status['position_refresh_after_missing_tp'] = bool(position_fetch_ok)
            if position_fetch_ok and not fresh_pos:
                self._clear_protection_missing_candidates(symbol)
                status.update({
                    'missing_tp': False,
                    'missing_tp1': False,
                    'missing_tp2': False,
                    'missing_sl': False,
                    'status': 'POSITION_CLOSED_DURING_AUDIT',
                })
                self.last_protection_order_status[symbol] = status
                return status
            if position_fetch_ok and fresh_pos:
                pos = fresh_pos
                current_qty = abs(float(
                    self._position_signed_contracts(fresh_pos)
                    or fresh_pos.get('contracts', 0)
                    or 0
                ))
                status['refreshed_position_qty'] = current_qty
                runner_state = self._update_utbreakout_fill_flags_from_position_qty(
                    symbol,
                    runner_state,
                    current_qty,
                )
                expected_tp_labels = [
                    label
                    for label, plan in planned_by_label.items()
                    if not bool(runner_state.get(f"{label.lower()}_filled", False))
                    and not bool(plan.get('filled', False))
                ]
                for label in planned_by_label:
                    filled = bool(runner_state.get(f"{label.lower()}_filled", False))
                    planned_by_label[label]['filled'] = filled
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
                status['missing_tp1'] = (
                    'TP1' in expected_tp_labels and not status['tp1_present']
                )
                status['missing_tp2'] = (
                    'TP2' in expected_tp_labels and not status['tp2_present']
                )
                status['missing_tp'] = (
                    status['missing_tp1'] or status['missing_tp2']
                )
                self._set_utbreakout_trailing_state(symbol, runner_state)
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
        elif status['managed_stop_trigger_pending']:
            status['status'] = 'MANAGED_STOP_TRIGGER_PENDING'
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
            if (
                confirmed
                and active_strategy in UTBREAKOUT_STRATEGIES
                and status.get('missing_sl')
            ):
                # Missing SL leaves the position without a bounded loss and is
                # therefore fail-closed. Missing TP still has an active SL and
                # is repaired by the ladder manager; force-closing it would
                # destroy a valid runner after a normal partial take-profit.
                protection_lockout_reason = "STOP_LOSS_PROTECTION_FAILED_FORCE_CLOSED"
                protection_label = "SL"
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
        retry_delay_sec=0.7,
        skip_initial_recovery=False,
        position_mode_verified=False,
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
        if is_binance_algo and not position_mode_verified:
            mode = await self._require_binance_one_way_mode(
                symbol,
                operation=f'{label} protection submit',
                record_ema_status=False,
            )
            if not mode.get('ok'):
                raise ProtectionOrderLookupUnavailable(
                    f"{mode.get('status')}:{symbol}:{mode.get('reason') or ''}"
                )
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
                    lookup_order = lookup.order or {}
                    lookup_info = (
                        lookup_order.get('info')
                        if isinstance(lookup_order.get('info'), dict)
                        else {}
                    )
                    lookup_state = str(
                        lookup_order.get('status')
                        or lookup_info.get('algoStatus')
                        or lookup_info.get('status')
                        or ''
                    ).strip().upper()
                    if lookup_state not in {
                        'CANCELED',
                        'CANCELLED',
                        'EXPIRED',
                        'FINISHED',
                        'FILLED',
                        'REJECTED',
                    }:
                        return lookup_order
                    logger.info(
                        "%s terminal protection order ignored before resubmit: "
                        "%s clientOrderId=%s status=%s",
                        label,
                        symbol,
                        client_order_id,
                        lookup_state,
                    )
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
            # A pre-entry complete empty snapshot plus a new unique client ID
            # proves the first submission cannot be a duplicate.  Submit the SL
            # immediately; retain exchange reconciliation on every retry/error.
            existing = (
                None
                if skip_initial_recovery and attempt == 1
                else await _recover_existing_protection_order()
            )
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
        lock = self._protection_replace_lock(symbol)
        async with lock:
            return await self._replace_stop_loss_order_locked(
                symbol,
                pos,
                stop_price,
                reason=reason,
            )

    async def _replace_stop_loss_order_locked(self, symbol, pos, stop_price, reason='stop replacement'):
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
        is_binance = str(getattr(self.exchange, 'id', '') or '').lower() == 'binance'
        if is_binance:
            mode = await self._require_binance_one_way_mode(
                symbol,
                operation='EMA200 profit stop replacement'
                if str(reason or '').startswith('EMA200 margin ROI ')
                else 'stop replacement',
                record_ema_status=str(reason or '').startswith('EMA200 margin ROI '),
            )
            if not mode.get('ok'):
                return None
        if is_binance:
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
        ema200_profit_replacement = str(reason or '').startswith(
            'EMA200 margin ROI '
        )
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

        if ema200_profit_replacement:
            pending_result = await self._reconcile_ema200_profit_stop_pending_identity(
                symbol,
                pos=pos,
                protection_orders=initial_orders,
            )
            pending_status = str(pending_result.get('status') or '')
            if pending_status == 'FOUND_CONFIRMED':
                refresh_ok, refreshed_orders = (
                    await self._collect_protection_orders_checked(symbol)
                )
                if not refresh_ok:
                    self._set_crypto_entry_lock(
                        f'PENDING_PROTECTION_RECONCILIATION:{symbol}'
                    )
                    setter = getattr(
                        self,
                        '_set_ema200_profit_stop_status',
                        None,
                    )
                    if callable(setter):
                        setter(
                            symbol,
                            'PENDING_PROTECTION_RECONCILIATION',
                            reason='confirmed pending order snapshot refresh failed',
                        )
                    return None
                initial_orders = list(refreshed_orders or [])
                if not any(
                    self._protection_client_order_id(order)
                    == str(pending_result.get('client_order_id') or '')
                    for order in initial_orders
                ):
                    self._set_crypto_entry_lock(
                        f'PENDING_PROTECTION_RECONCILIATION:{symbol}'
                    )
                    setter = getattr(
                        self,
                        '_set_ema200_profit_stop_status',
                        None,
                    )
                    if callable(setter):
                        setter(
                            symbol,
                            'PENDING_PROTECTION_RECONCILIATION',
                            reason=(
                                'pending stop was found by client ID but is not '
                                'yet visible in the complete open-order snapshot'
                            ),
                        )
                    return None
            elif pending_status not in {'NO_PENDING', 'NO_STORE'}:
                self._set_crypto_entry_lock(
                    f'PENDING_PROTECTION_RECONCILIATION:{symbol}'
                )
                setter = getattr(self, '_set_ema200_profit_stop_status', None)
                if callable(setter):
                    setter(
                        symbol,
                        'PENDING_PROTECTION_RECONCILIATION',
                        reason=(
                            'existing EMA200 pending protection identity must be '
                            f'reconciled before replacement: {pending_status}'
                        ),
                    )
                return None

        existing_sl = [
            order for order in (initial_orders or [])
            if self._classify_protection_order(order) == 'sl'
        ]
        best_existing_sl = self._best_stop_order_for_position(pos, existing_sl)
        best_existing_stop = (
            self._protection_trigger_price(best_existing_sl)
            if best_existing_sl
            else None
        )
        if (
            best_existing_sl is not None
            and self._stop_is_at_least_as_protective(
                side,
                best_existing_stop,
                safe_stop,
            )
        ):
            logger.info(
                'SL replacement skipped for %s: existing winner %.12f is '
                'already at least as protective as target %.12f (%s)',
                symbol,
                float(best_existing_stop),
                float(safe_stop),
                reason,
            )
            return best_existing_sl
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
                    resolution = await self._confirm_cancelled_stop_orders_absent(
                        symbol,
                        existing_sl,
                    )
                    if resolution.get('status') == 'CONFIRMED_ABSENT':
                        logger.info(
                            'SL cancellation confirmed through direct lookup '
                            'for %s after snapshot failure (%s)',
                            symbol,
                            reason,
                        )
                        remaining_sl = []
                        break
                    self._set_crypto_entry_lock(
                        f'PENDING_PROTECTION_RECONCILIATION:{symbol}'
                    )
                    setter = getattr(
                        self,
                        '_set_ema200_profit_stop_status',
                        None,
                    )
                    if callable(setter) and ema200_profit_replacement:
                        setter(
                            symbol,
                            'PENDING_PROTECTION_RECONCILIATION',
                            reason=(
                                'SL cancellation state is uncertain: '
                                f"{resolution.get('status')} "
                                f"{resolution.get('reason') or ''}"
                            ).strip(),
                        )
                    self.last_protection_order_status[symbol] = {
                        'tp_expected': False,
                        'sl_expected': True,
                        'tp_present': False,
                        'sl_present': (
                            resolution.get('status') == 'FOUND_OPEN'
                        ),
                        'tp_count': 0,
                        'sl_count': (
                            1 if resolution.get('status') == 'FOUND_OPEN' else 0
                        ),
                        'missing_tp': False,
                        'missing_sl': False,
                        'duplicate_cancelled': 0,
                        'status': 'PENDING_PROTECTION_RECONCILIATION',
                        'cancel_reconciliation': resolution,
                    }
                    logger.warning(
                        'SL replacement blocked for %s because cancellation '
                        'could not be proven terminal/absent: %s (%s)',
                        symbol,
                        resolution,
                        reason,
                    )
                    return None
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
            winner = self._best_stop_order_for_position(pos, remaining_sl)
            winner_stop = (
                self._protection_trigger_price(winner)
                if winner is not None
                else None
            )
            if (
                winner is not None
                and self._stop_is_at_least_as_protective(
                    side,
                    winner_stop,
                    safe_stop,
                )
            ):
                redundant = [
                    order for order in remaining_sl
                    if order != winner
                ]
                if redundant:
                    await self._cancel_protection_orders(
                        symbol,
                        reason=f'{reason} preserve better winner',
                        orders=redundant,
                    )
                logger.info(
                    'SL replacement aborted for %s: a better/equal winner '
                    'appeared during cancellation confirmation target=%.12f '
                    'winner=%.12f (%s)',
                    symbol,
                    float(safe_stop),
                    float(winner_stop),
                    reason,
                )
                return winner
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

        # The mark can cross the requested stop after the pre-cancel check but
        # before the replacement is submitted. Reconcile again while the old
        # stop is gone so Binance -2021 cannot leave a live position naked.
        post_cancel_fetch_ok, post_cancel_pos = (
            await self._fetch_server_position_checked(symbol)
        )
        if not post_cancel_fetch_ok:
            await self._notify_protection_issue(
                symbol,
                f"sl_replace_post_cancel_fetch_failed:{self._protection_position_signature(pos)}",
                f"🚨 {self.ctrl.format_symbol_for_display(symbol)} SL 교체 중 "
                "기존 SL 취소 후 포지션 재조회에 실패했습니다. 보호 공백 방지를 위해 비상청산을 시도합니다.",
                cooldown_sec=60,
            )
            await self._fail_closed_unprotected_position(
                symbol,
                reason=(
                    "SL replacement post-cancel position fetch failed: "
                    f"{reason}"
                ),
                status_code="SL_REPLACE_POST_CANCEL_FETCH_FAILED",
                expected_tp=False,
                emergency_close=True,
            )
            return None
        if not post_cancel_pos:
            logger.info(
                "SL replacement stopped for %s: position became flat after cancel (%s)",
                symbol,
                reason,
            )
            return None
        pos = post_cancel_pos
        post_cancel_qty = self.safe_amount(
            symbol,
            abs(
                float(
                    self._position_signed_contracts(pos)
                    or pos.get('contracts', 0)
                    or 0
                )
            ),
        )
        if float(post_cancel_qty) <= 0:
            return None
        qty = post_cancel_qty
        post_cancel_info = (
            pos.get('info')
            if isinstance(pos.get('info'), dict)
            else {}
        )
        post_cancel_mark = _safe_float_or_none(
            pos.get('markPrice')
            or pos.get('mark_price')
            or post_cancel_info.get('markPrice')
            or pos.get('last')
            or pos.get('currentPrice')
        )
        post_cancel_crossed = bool(
            post_cancel_mark is not None
            and (
                float(safe_stop) >= float(post_cancel_mark)
                if side == 'long'
                else float(safe_stop) <= float(post_cancel_mark)
            )
        )
        if post_cancel_crossed:
            await self._notify_protection_issue(
                symbol,
                f"sl_replace_crossed_after_cancel:{self._protection_position_signature(pos)}",
                f"🚨 {self.ctrl.format_symbol_for_display(symbol)} SL 교체 중 가격이 목표선 "
                f"{float(safe_stop):.8f}을 통과했습니다(현재 {float(post_cancel_mark):.8f}). "
                "거부되는 SL을 반복하지 않고 즉시 비상청산합니다.",
                cooldown_sec=60,
            )
            await self._fail_closed_unprotected_position(
                symbol,
                reason=(
                    "SL replacement target crossed after existing stop was "
                    f"cancelled: {reason}"
                ),
                status_code="SL_REPLACE_TARGET_CROSSED_AFTER_CANCEL",
                expected_tp=False,
                emergency_close=True,
            )
            return None

        replacement_client_id = self._build_protection_client_order_id(
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
            revision=f"replace-{time.time_ns()}",
        )
        pending_record = None
        if ema200_profit_replacement:
            pending_record = self._persist_ema200_profit_stop_pending_identity(
                symbol,
                pos,
                client_order_id=replacement_client_id,
                side=side,
                qty=qty,
                trigger_price=safe_stop,
            )
            if (
                getattr(self, 'trading_state_store', None) is not None
                and pending_record is None
            ):
                self._set_crypto_entry_lock(
                    f'EMA200_POSITION_STATE_MISMATCH:{symbol}'
                )
                setter = getattr(self, '_set_ema200_profit_stop_status', None)
                if callable(setter):
                    setter(
                        symbol,
                        'EMA200_POSITION_STATE_MISMATCH',
                        reason=(
                            'no durable EMA200 record matches current position '
                            'side and quantity; profit stop submission blocked'
                        ),
                    )
                return None
        try:
            replacement = await self._create_protection_order_with_retries(
                symbol,
                'stop_market',
                sl_side,
                qty,
                None,
                {
                    'stopPrice': safe_stop,
                    'reduceOnly': True,
                    'newClientOrderId': replacement_client_id,
                },
                'SL',
                max_attempts=3,
                position_mode_verified=is_binance,
            )
        except Exception as exc:
            if ema200_profit_replacement and pending_record is not None:
                text = f'{type(exc).__name__}: {exc}'.lower()
                ambiguous = (
                    'submitted_unknown' in text
                    or 'lookup_unknown' in text
                    or 'timeout' in text
                    or 'network' in text
                    or 'connection' in text
                    or 'unavailable' in text
                )
                if not ambiguous:
                    self._clear_ema200_profit_stop_pending_identity(
                        pending_record
                    )
            raise
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
            if ema200_profit_replacement:
                self._persist_ema200_profit_stop_identity(
                    symbol,
                    pos,
                    replacement,
                )
        return replacement
