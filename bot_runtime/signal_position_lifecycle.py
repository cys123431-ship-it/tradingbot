"""UTBreakout position state, quality diagnostics, and persistence."""

from __future__ import annotations


class SignalPositionLifecycleMixin:
    def _utbreakout_trailing_state_aliases(self, symbol):
        try:
            aliases = [
                key
                for key in self._utbreakout_plan_symbol_keys(symbol)
                if not str(key).startswith('__key__:')
            ]
        except Exception:
            aliases = [str(symbol or '').strip()]
        out = []
        for alias in aliases:
            alias = str(alias or '').strip()
            if alias and alias not in out:
                out.append(alias)
        return out

    def _get_utbreakout_trailing_state(self, symbol, *, migrate=True):
        states = getattr(self, 'utbreakout_trailing_states', None)
        if not isinstance(states, dict):
            return None
        aliases = self._utbreakout_trailing_state_aliases(symbol)
        canonical = aliases[0] if aliases else str(symbol or '').strip()
        for alias in aliases:
            state = states.get(alias)
            if not isinstance(state, dict):
                continue
            if migrate and canonical and alias != canonical:
                states[canonical] = state
                states.pop(alias, None)
                state['state_symbol_migrated_from'] = alias
                state['state_symbol'] = canonical
            return state
        return None

    def _set_utbreakout_trailing_state(self, symbol, state):
        if not isinstance(state, dict):
            return None
        states = getattr(self, 'utbreakout_trailing_states', None)
        if not isinstance(states, dict):
            states = {}
            self.utbreakout_trailing_states = states
        aliases = self._utbreakout_trailing_state_aliases(symbol)
        canonical = aliases[0] if aliases else str(symbol or '').strip()
        if not canonical:
            return None
        for alias in aliases:
            if alias != canonical:
                states.pop(alias, None)
        state['state_symbol'] = canonical
        states[canonical] = state
        return state

    def _clear_utbreakout_trailing_state(self, symbol, *, finalize=False, reason='cleared', exit_price=None):
        states = getattr(self, 'utbreakout_trailing_states', None)
        if isinstance(states, dict):
            aliases = self._utbreakout_trailing_state_aliases(symbol)
            canonical = aliases[0] if aliases else str(symbol or '').strip()
            state = self._get_utbreakout_trailing_state(symbol)
            if state and finalize:
                try:
                    price_val = None
                    if exit_price is not None:
                        price_val = float(exit_price)
                    import asyncio
                    asyncio.create_task(
                        self._check_and_record_sl_lockout_async(
                            canonical,
                            state,
                            exit_price=price_val
                        )
                    )
                except Exception as e:
                    logger.debug(f"Failed to spawn SL fill check task: {e}")
            if finalize:
                self._finalize_utbreakout_runner_state(
                    canonical,
                    reason=reason,
                    exit_price=exit_price,
                )
            for alias in aliases:
                states.pop(alias, None)

    def _aggressive_growth_symbol_key(self, symbol):
        return self._normalize_protection_symbol(symbol)

    def _update_aggressive_growth_high_watermark(self, equity):
        value = _safe_float_or_none(equity)
        if value is None or value <= 0:
            return float(getattr(self, 'aggressive_growth_high_watermark', 0.0) or 0.0)
        current = float(getattr(self, 'aggressive_growth_high_watermark', 0.0) or 0.0)
        current = max(current, float(value))
        self.aggressive_growth_high_watermark = current
        return current

    def _clear_aggressive_growth_position(self, symbol):
        positions = getattr(self, 'aggressive_growth_positions', None)
        if not isinstance(positions, dict):
            self.aggressive_growth_positions = {}
            return
        key = self._aggressive_growth_symbol_key(symbol)
        positions.pop(key, None)
        positions.pop(symbol, None)

    def _record_aggressive_growth_position(self, symbol, pos=None, plan=None, qty=None, current_price=None):
        positions = getattr(self, 'aggressive_growth_positions', None)
        if not isinstance(positions, dict):
            positions = {}
            self.aggressive_growth_positions = positions
        plan = dict(plan or {})
        pos = dict(pos or {})
        key = self._aggressive_growth_symbol_key(symbol or self._protection_position_symbol(pos))
        if not key:
            return None
        position_qty = _safe_float_or_none(qty)
        if position_qty is None or position_qty <= 0:
            position_qty = abs(float(self._position_signed_contracts(pos) or pos.get('contracts', 0) or 0))
        side = str(pos.get('side') or plan.get('side') or 'long').lower()
        if side != 'long' or position_qty <= 0:
            self._clear_aggressive_growth_position(symbol)
            return None
        entry_price = _safe_float_or_none(pos.get('entryPrice')) or _safe_float_or_none(plan.get('entry_price')) or 0.0
        mark_price = (
            _safe_float_or_none(current_price)
            or _safe_float_or_none(pos.get('markPrice'))
            or _safe_float_or_none(pos.get('lastPrice'))
            or entry_price
        )
        notional = position_qty * max(mark_price, entry_price, 0.0)
        previous = positions.get(key, {}) if isinstance(positions.get(key), dict) else {}
        add_count = int(plan.get('pyramid_add_count', previous.get('pyramid_add_count', previous.get('pyramid_adds', 0))) or 0)
        meta = dict(previous)
        meta.update({
            'symbol': symbol,
            'symbol_key': key,
            'side': side,
            'qty': float(position_qty),
            'base_qty': float(previous.get('base_qty') or plan.get('base_qty') or position_qty),
            'entry_price': float(entry_price or 0.0),
            'last_price': float(mark_price or 0.0),
            'notional': float(notional or 0.0),
            'risk_distance': _safe_float_or_none(plan.get('risk_distance')) or previous.get('risk_distance'),
            'last_stop_price': _safe_float_or_none(plan.get('stop_loss')) or previous.get('last_stop_price'),
            'aggressive_growth_score': plan.get('aggressive_growth_score', previous.get('aggressive_growth_score')),
            'pyramid_add_count': add_count,
            'pyramid_adds': add_count,
            'updated_at': datetime.now(timezone.utc).isoformat(),
        })
        positions[key] = meta
        return meta

    def _calculate_aggressive_growth_exposure(self, positions=None, symbol=None, price_by_symbol=None):
        tracked = getattr(self, 'aggressive_growth_positions', None)
        if not isinstance(tracked, dict):
            tracked = {}
            self.aggressive_growth_positions = tracked
        price_map = {}
        for raw_key, raw_price in (price_by_symbol or {}).items():
            price = _safe_float_or_none(raw_price)
            if price is not None and price > 0:
                price_map[self._aggressive_growth_symbol_key(raw_key)] = price

        position_by_key = {}
        if positions is not None:
            for position in positions or []:
                if not isinstance(position, dict):
                    continue
                key = self._aggressive_growth_symbol_key(self._protection_position_symbol(position))
                if key:
                    position_by_key[key] = position

        total = 0.0
        symbol_total = 0.0
        active = {}
        target_key = self._aggressive_growth_symbol_key(symbol) if symbol else None
        for key, meta in list(tracked.items()):
            if not isinstance(meta, dict):
                tracked.pop(key, None)
                continue
            position = position_by_key.get(key)
            if positions is not None and not position:
                tracked.pop(key, None)
                continue
            side = str((position or meta).get('side') or meta.get('side') or '').lower()
            try:
                qty = abs(float(
                    self._position_signed_contracts(position)
                    if position else meta.get('qty', 0.0)
                ))
            except (TypeError, ValueError):
                qty = 0.0
            if side != 'long' or qty <= 0:
                tracked.pop(key, None)
                continue
            entry = (
                _safe_float_or_none((position or {}).get('entryPrice'))
                or _safe_float_or_none(meta.get('entry_price'))
                or 0.0
            )
            mark = (
                price_map.get(key)
                or _safe_float_or_none((position or {}).get('markPrice'))
                or _safe_float_or_none((position or {}).get('lastPrice'))
                or _safe_float_or_none(meta.get('last_price'))
                or entry
            )
            exposure = qty * max(float(mark or 0.0), float(entry or 0.0), 0.0)
            if exposure <= 0:
                tracked.pop(key, None)
                continue
            meta.update({
                'qty': float(qty),
                'entry_price': float(entry or 0.0),
                'last_price': float(mark or 0.0),
                'notional': float(exposure),
                'updated_at': datetime.now(timezone.utc).isoformat(),
            })
            active[key] = dict(meta)
            total += exposure
            if target_key and key == target_key:
                symbol_total += exposure
        return {
            'total_notional': float(total),
            'symbol_notional': float(symbol_total),
            'open_positions': len(active),
            'positions': active,
        }

    def _build_utbreakout_position_sizing_portfolio_context(
        self,
        symbol=None,
        side=None,
        cfg=None,
        positions=None,
    ):
        cfg = dict(cfg or {})
        side = str(side or '').lower()
        risk_pct = (
            _safe_float_or_none(cfg.get('risk_per_trade_percent'))
            or _safe_float_or_none(cfg.get('risk_per_trade_pct'))
            or 1.0
        )
        open_positions = 0
        same_direction_positions = 0
        target_key = self._futures_symbol_key(symbol) if symbol else ''
        for pos in positions or []:
            if not isinstance(pos, dict):
                continue
            normalized = self._normalize_server_position(pos)
            if not normalized:
                continue
            open_positions += 1
            pos_side = str(normalized.get('side') or '').lower()
            if side and pos_side == side:
                same_direction_positions += 1
        return {
            'open_positions': int(open_positions),
            'same_direction_positions': int(same_direction_positions),
            # Conservative estimate: each active position consumes one configured trade risk budget.
            'total_open_risk_pct': float(open_positions) * float(risk_pct),
            'target_symbol_key': target_key,
        }

    def _get_recent_strategy_closed_trade_pnls(
        self,
        strategies,
        limit,
        *,
        today_only=False,
    ):
        """Return loss-streak inputs attributed only to the selected strategy family."""
        limit = max(1, int(limit or 1))
        if isinstance(strategies, str):
            strategies = [strategies]
        strategy_values = {
            str(value or '').strip().lower()
            for value in (strategies or [])
            if str(value or '').strip()
        }
        if not strategy_values:
            return []

        store = getattr(self, 'trading_state_store', None)
        load_results = getattr(store, 'load_trade_results', None)
        if callable(load_results):
            try:
                today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
                matches = []
                for trade in load_results() or []:
                    if not isinstance(trade, dict):
                        continue
                    attributed = {
                        str(trade.get(key) or '').strip().lower()
                        for key in ('primary_strategy', 'selected_strategy', 'strategy')
                        if str(trade.get(key) or '').strip()
                    }
                    if not attributed.intersection(strategy_values):
                        continue
                    exit_time = str(trade.get('exit_time') or '').strip()
                    if not exit_time or (today_only and not exit_time.startswith(today)):
                        continue
                    pnl = _safe_float_or_none(
                        trade.get('net_pnl_usdt')
                        if trade.get('net_pnl_usdt') is not None
                        else trade.get('gross_pnl_usdt')
                    )
                    if pnl is None:
                        continue
                    matches.append((exit_time, float(pnl)))
                matches.sort(key=lambda item: item[0], reverse=True)
                if matches:
                    return [pnl for _, pnl in matches[:limit]]
            except Exception as exc:
                logger.debug(
                    "Strategy-attributed live trade history unavailable: %s",
                    exc,
                )

        db = getattr(self, 'db', None)
        getter = getattr(db, 'get_recent_closed_trade_pnls', None)
        if not callable(getter):
            return []
        try:
            return getter(
                limit,
                today_only=today_only,
                strategies=strategy_values,
            )
        except TypeError:
            # A legacy DB implementation cannot safely provide attribution.
            return []

    def _finalize_utbreakout_runner_state(self, symbol, *, reason='position closed', exit_price=None):
        state = self._get_utbreakout_trailing_state(symbol)
        if not isinstance(state, dict):
            return None
        side = str(state.get('side') or '').lower()
        if side not in {'long', 'short'}:
            return None
        try:
            entry = float(state.get('entry_price') or 0.0)
            risk = float(state.get('risk_distance') or 0.0)
            exit_px = float(exit_price if exit_price is not None else state.get('last_close') or state.get('last_stop_price') or entry)
        except (TypeError, ValueError):
            return None
        if entry <= 0 or risk <= 0 or exit_px <= 0:
            return None
        pnl_r = (exit_px - entry) / risk if side == 'long' else (entry - exit_px) / risk
        mfe_r = float(state.get('mfe_r') or max(0.0, pnl_r))
        mae_r = float(state.get('mae_r') or 0.0)
        capture = pnl_r / max(mfe_r, 1e-9) if mfe_r > 0 else 0.0
        status = {
            'candidate_side': side,
            'entry_timeframe': state.get('entry_timeframe'),
            'htf_timeframe': state.get('htf_timeframe'),
            'auto_selected_set_id': state.get('auto_selected_set_id'),
            'auto_selected_set_name': state.get('auto_selected_set_name'),
            'decision_candle_ts': state.get('decision_candle_ts'),
            'entry_price': entry,
            'reason': reason,
            'entry_plan': {
                'entry_price': entry,
                'stop_loss': state.get('initial_stop_price'),
                'risk_distance': risk,
                'decision_candle_ts': state.get('decision_candle_ts'),
            }
        }
        extra = {
            'code': 'RUNNER_OUTCOME',
            'runner_outcome': 'position_closed',
            'exit_price': exit_px,
            'pnl_r': pnl_r,
            'mfe_r': mfe_r,
            'mae_r': mae_r,
            'mfe_capture_ratio': capture,
            'last_stop_price': state.get('last_stop_price'),
            'runner_mode': state.get('runner_mode'),
            'runner_updates': state.get('runner_updates'),
        }
        try:
            self._record_utbreakout_diagnostic_event(symbol, status, event='runner_outcome', extra=extra)
            self._record_profit_alpha_meta_outcome(symbol, state, extra)
            if isinstance(getattr(self, 'utbreakout_runner_stats_cache', None), dict):
                self.utbreakout_runner_stats_cache.clear()
        except Exception:
            logger.debug("UTBreak runner outcome logging skipped", exc_info=True)
        return extra

    def _utbreakout_tp_distance_after_front_run(self, risk_distance, r_multiple, cfg=None, atr_value=None):
        try:
            risk = float(risk_distance or 0.0)
            rr = float(r_multiple or 0.0)
        except (TypeError, ValueError):
            return None
        if risk <= 0 or rr <= 0:
            return None
        cfg = cfg if isinstance(cfg, dict) else {}
        try:
            atr = float(atr_value if atr_value is not None else cfg.get('atr') or 0.0)
        except (TypeError, ValueError):
            atr = 0.0
        try:
            front_atr = max(0.0, float(cfg.get('take_profit_front_run_atr', 0.0) or 0.0))
        except (TypeError, ValueError):
            front_atr = 0.0
        try:
            front_pct = max(0.0, float(cfg.get('take_profit_front_run_pct', 0.0) or 0.0))
        except (TypeError, ValueError):
            front_pct = 0.0
        try:
            min_rr = float(cfg.get('tp_front_run_min_r_multiple', max(0.1, rr - 0.15)) or max(0.1, rr - 0.15))
        except (TypeError, ValueError):
            min_rr = max(0.1, rr - 0.15)
        min_rr = max(0.1, min(rr, min_rr))
        target_distance = risk * rr
        front_distance = max((atr * front_atr) if atr > 0 else 0.0, target_distance * front_pct)
        front_distance = min(front_distance, max(0.0, target_distance - risk * min_rr))
        return max(risk * min_rr, target_distance - front_distance)

    def _resolve_utbreakout_entry_timestamp_ms(self, symbol, plan=None, existing_state=None):
        plan = plan if isinstance(plan, dict) else {}
        existing_state = existing_state if isinstance(existing_state, dict) else {}
        for source in (plan, existing_state):
            for key in (
                'entry_timestamp_ms',
                'entry_time_ms',
                'entry_timestamp',
                'entry_time',
                'filled_at',
                'created_at',
            ):
                parsed = _timestamp_ms_or_none(source.get(key))
                if parsed is not None:
                    return int(parsed)
        try:
            record = self._utbreakout_entry_record_for_symbol(symbol)
        except Exception:
            record = None
        if record is not None:
            for value in (
                getattr(record, 'created_at', None),
                getattr(record, 'updated_at', None),
            ):
                parsed = _timestamp_ms_or_none(value)
                if parsed is not None:
                    return int(parsed)
        return int(time.time() * 1000.0)

    def _utbreakout_post_entry_closed_bars(self, closed, state, cfg):
        """Return only complete bars that opened after the entry-containing bar."""
        state = state if isinstance(state, dict) else {}
        cfg = cfg if isinstance(cfg, dict) else {}
        timeframe = (
            state.get('entry_timeframe')
            or state.get('exit_timeframe')
            or cfg.get('entry_timeframe')
            or cfg.get('timeframe')
            or '15m'
        )
        timeframe_ms = self._timeframe_to_ms(timeframe) or (15 * 60 * 1000)
        return full_post_entry_closed_bars(
            closed,
            entry_timestamp=(
                state.get('entry_timestamp_ms')
                or state.get('created_at')
            ),
            timeframe_ms=timeframe_ms,
        )

    def _register_utbreakout_trailing_state(self, symbol, side, entry_price, qty, plan, cfg):
        cfg = apply_profit_opportunity_effective_overrides(dict(cfg or {}))
        for key in (
            'fixed_take_profit_enabled',
            'partial_take_profit_enabled',
            'partial_take_profit_r_multiple',
            'partial_take_profit_ratio',
            'second_take_profit_enabled',
            'second_take_profit_r_multiple',
            'second_take_profit_ratio',
            'runner_pct',
            'atr_trailing_enabled',
            'atr_trailing_multiplier',
            'atr_trailing_activation_r',
            'runner_exit_enabled',
            'runner_chandelier_enabled',
            'tp1_breakeven_enabled',
            'tp1_breakeven_trigger_r',
            'ev_exit_profile_name',
            'ev_time_stop_enabled',
            'ev_time_stop_bars',
            'ev_time_stop_min_mfe_r',
            'ev_mfe_profit_lock_enabled',
            'ev_mfe_lock_trigger_1_r',
            'ev_mfe_lock_trigger_2_r',
            'ev_mfe_lock_trigger_3_r',
            'profit_alpha_follow_through_enabled',
            'profit_alpha_follow_through_bars',
            'profit_alpha_follow_through_min_mfe_r',
            'profit_alpha_early_exit_max_mae_r',
            'take_profit_front_run_atr',
            'take_profit_front_run_pct',
            'tp_front_run_min_r_multiple',
            'structure_stop_buffer_atr',
            'soft_stop_enabled',
            'soft_stop_confirm_bars',
            'near_miss_tp_enabled',
            'near_miss_tp_arm_ratio',
            'near_miss_tp_lock_r',
            'near_miss_tp_rejection_atr',
            'profit_alpha_stall_exit_max_current_r',
            'ev_time_stop_max_current_r',
            'time_stop_max_current_r',
        ):
            if key in plan:
                cfg[key] = plan[key]
        if (
            not bool(cfg.get('atr_trailing_enabled', False))
            and not bool(cfg.get('tp1_breakeven_enabled', True))
        ):
            self._clear_utbreakout_trailing_state(symbol)
            return None
        if bool(plan.get('recovered_from_exchange')):
            cfg = dict(cfg)
            cfg['take_profit_front_run_atr'] = 0.0
            cfg['take_profit_front_run_pct'] = 0.0
        try:
            risk_distance = float(plan.get('risk_distance', 0.0) or 0.0)
            initial_qty = abs(float(qty or 0.0))
            entry = float(entry_price or 0.0)
        except (TypeError, ValueError):
            return None
        if risk_distance <= 0 or initial_qty <= 0 or entry <= 0:
            return None
        ratio = min(1.0, max(0.0, float(cfg.get('partial_take_profit_ratio', 0.20) or 0.20)))
        runner_ratio = min(
            1.0,
            max(
                0.0,
                float(cfg.get('runner_pct', plan.get('runner_pct', 0.0)) or 0.0)
            )
        )
        preserve_runner_qty = runner_ratio > 0
        second_enabled = bool(cfg.get('second_take_profit_enabled', True))
        partial_enabled = bool(cfg.get('partial_take_profit_enabled', True))
        partial_ratio = ratio if partial_enabled else 0.0
        if preserve_runner_qty:
            second_room = max(0.0, 1.0 - partial_ratio - runner_ratio)
        else:
            second_room = max(0.0, 1.0 - partial_ratio)
        second_ratio = min(
            second_room,
            max(0.0, float(cfg.get('second_take_profit_ratio', 0.40) or 0.40))
        ) if second_enabled else 0.0
        raw_tp_quantities = []
        planned_tp_orders = []
        partial_r = max(0.1, float(cfg.get('partial_take_profit_r_multiple', 1.00) or 1.00))
        second_r = max(0.1, float(cfg.get('second_take_profit_r_multiple', cfg.get('take_profit_r_multiple', 3.50)) or 3.50))
        atr_for_front_run = plan.get('atr')
        partial_distance = self._utbreakout_tp_distance_after_front_run(
            risk_distance,
            partial_r,
            cfg,
            atr_for_front_run,
        ) or (risk_distance * partial_r)
        second_distance = self._utbreakout_tp_distance_after_front_run(
            risk_distance,
            second_r,
            cfg,
            atr_for_front_run,
        ) or (risk_distance * second_r)
        if partial_enabled and partial_ratio > 0:
            raw_tp_quantities.append(initial_qty * partial_ratio)
            planned_tp_orders.append({
                'tp_index': 1,
                'tp_label': 'TP1',
                'tp_name': 'TP1',
                'side': 'sell' if str(side or '').lower() == 'long' else 'buy',
                'price': entry + partial_distance if str(side or '').lower() == 'long' else entry - partial_distance,
                'target_r': partial_r,
                'effective_r': partial_distance / risk_distance,
                'qty': None,
                'pct': partial_ratio * 100.0,
                'filled': False,
            })
        if second_enabled and second_ratio > 0:
            raw_tp_quantities.append(initial_qty * second_ratio)
            planned_tp_orders.append({
                'tp_index': len(planned_tp_orders) + 1,
                'tp_label': 'TP2',
                'tp_name': 'TP2',
                'side': 'sell' if str(side or '').lower() == 'long' else 'buy',
                'price': entry + second_distance if str(side or '').lower() == 'long' else entry - second_distance,
                'target_r': second_r,
                'effective_r': second_distance / risk_distance,
                'qty': None,
                'pct': second_ratio * 100.0,
                'filled': False,
            })
        if preserve_runner_qty:
            residual_qtys = []
            remaining_tp_qty = initial_qty
            for raw_qty in raw_tp_quantities:
                desired_qty = min(max(0.0, float(raw_qty or 0.0)), remaining_tp_qty)
                qty_value = max(0.0, _safe_float_value(self.safe_amount(symbol, desired_qty), 0.0))
                residual_qtys.append(qty_value)
                remaining_tp_qty = max(0.0, remaining_tp_qty - qty_value)
        else:
            residual_qtys = _calculate_residual_tp_quantities(symbol, initial_qty, raw_tp_quantities, self.safe_amount)
        for index, qty_value in enumerate(residual_qtys):
            if index < len(planned_tp_orders):
                planned_tp_orders[index]['qty'] = qty_value
        planned_tp_orders, tp_collapse = self._collapse_tp_targets_for_exchange(
            symbol,
            initial_qty,
            planned_tp_orders,
        )
        if tp_collapse:
            runner_ratio = 0.0
            preserve_runner_qty = False
            logger.warning(
                "[Protection] trailing state uses single TP fallback for %s: qty=%.12f min_amount=%.12f",
                symbol,
                float(tp_collapse["total_qty"]),
                float(tp_collapse["min_amount"]),
            )
        activation_r = max(
            0.0,
            float(
                cfg.get(
                    'atr_trailing_activation_r',
                    cfg.get('partial_take_profit_r_multiple', 1.00)
                ) or 1.00
            )
        )
        existing_state = self._get_utbreakout_trailing_state(symbol)
        entry_timestamp_ms = self._resolve_utbreakout_entry_timestamp_ms(
            symbol,
            plan=plan,
            existing_state=existing_state,
        )
        state = {
            'side': str(side or '').lower(),
            'entry_price': entry,
            'initial_qty': initial_qty,
            'remaining_ratio': runner_ratio if preserve_runner_qty else max(0.0, 1.0 - ratio),
            'tp1_expected_remaining_ratio': max(0.0, 1.0 - partial_ratio),
            'runner_pct': runner_ratio,
            'preserve_runner_qty': preserve_runner_qty,
            'aggressive_growth_overlay': bool(plan.get('aggressive_growth_overlay', False)),
            'pyramid_add_count': int(plan.get('pyramid_add_count', 0) or 0),
            'planned_tp_orders': planned_tp_orders,
            'tp_orders': list(planned_tp_orders),
            'expected_tp_count': len(planned_tp_orders),
            'risk_distance': risk_distance,
            'activation_r': activation_r,
            'trailing_atr_multiplier': max(0.1, float(cfg.get('atr_trailing_multiplier', 3.50) or 3.50)),
            'breakeven_enabled': bool(cfg.get('atr_trailing_breakeven_enabled', True)),
            'atr_trailing_enabled': bool(cfg.get('atr_trailing_enabled', False)),
            'tp1_breakeven_enabled': bool(cfg.get('tp1_breakeven_enabled', True)),
            'tp1_breakeven_trigger_r': max(
                0.1,
                float(
                    cfg.get(
                        'tp1_breakeven_trigger_r',
                        cfg.get('partial_take_profit_r_multiple', 1.00)
                    ) or 1.00
                )
            ),
            'tp1_breakeven_offset_r': max(0.0, float(cfg.get('tp1_breakeven_offset_r', 0.03) or 0.03)),
            'tp1_breakeven_wait_for_partial': bool(cfg.get('tp1_breakeven_wait_for_partial', True)),
            'tp1_breakeven_qty_tolerance': min(
                0.5,
                max(0.0, float(cfg.get('tp1_breakeven_qty_tolerance', 0.08) or 0.08))
            ),
            'breakeven_armed': False,
            'last_stop_price': float(plan.get('stop_loss', 0.0) or 0.0),
            'initial_stop_price': float(plan.get('stop_loss', 0.0) or 0.0),
            'hard_stop_price': float(plan.get('hard_stop_loss', plan.get('stop_loss', 0.0)) or 0.0),
            'soft_stop_price': _safe_float_or_none(plan.get('soft_stop_loss')),
            'structure_stop': _safe_float_or_none(plan.get('structure_stop')),
            'structure_stop_with_buffer': _safe_float_or_none(plan.get('structure_stop_with_buffer')),
            'soft_stop_enabled': bool(plan.get('soft_stop_enabled', cfg.get('soft_stop_enabled', True))),
            'soft_stop_confirm_bars': int(plan.get('soft_stop_confirm_bars', cfg.get('soft_stop_confirm_bars', 2)) or 2),
            'soft_stop_breach_count': 0,
            'near_miss_tp_enabled': bool(plan.get('near_miss_tp_enabled', cfg.get('near_miss_tp_enabled', True))),
            'near_miss_tp_arm_ratio': max(0.50, min(0.99, float(plan.get('near_miss_tp_arm_ratio', cfg.get('near_miss_tp_arm_ratio', 0.86)) or 0.86))),
            'near_miss_tp_lock_r': max(0.0, float(plan.get('near_miss_tp_lock_r', cfg.get('near_miss_tp_lock_r', 0.28)) or 0.28)),
            'near_miss_tp_rejection_atr': max(0.0, float(plan.get('near_miss_tp_rejection_atr', cfg.get('near_miss_tp_rejection_atr', 0.12)) or 0.12)),
            'near_miss_tp1_armed': False,
            'near_miss_tp2_armed': False,
            'profit_alpha_stall_exit_max_current_r': float(plan.get('profit_alpha_stall_exit_max_current_r', cfg.get('profit_alpha_stall_exit_max_current_r', 0.0)) or 0.0),
            'ev_time_stop_max_current_r': float(plan.get('ev_time_stop_max_current_r', cfg.get('ev_time_stop_max_current_r', 0.0)) or 0.0),
            'runner_exit_enabled': bool(cfg.get('runner_exit_enabled', False)),
            'runner_chandelier_enabled': bool(cfg.get('runner_chandelier_enabled', False)),
            'runner_mode': 'dynamic_chandelier' if cfg.get('runner_chandelier_enabled', False) else 'atr_close',
            'runner_chandelier_lookback': int(cfg.get('runner_chandelier_lookback', 22) or 22),
            'runner_structure_lookback': int(cfg.get('runner_structure_lookback', 5) or 5),
            'highest_price': entry,
            'lowest_price': entry,
            'mfe_r': 0.0,
            'mae_r': 0.0,
            'runner_updates': 0,
            'trend_health': {
                'score': plan.get('trend_health_score'),
                'summary': plan.get('trend_health_summary'),
                'risk_multiplier': plan.get('trend_health_risk_multiplier'),
            },
            'strategy_quality': {
                'score': plan.get('strategy_quality_score'),
                'summary': plan.get('strategy_quality_summary'),
                'risk_multiplier': plan.get('strategy_quality_risk_multiplier'),
            },
            'entry_timeframe': plan.get('entry_timeframe'),
            'entry_timestamp_ms': entry_timestamp_ms,
            'htf_timeframe': plan.get('htf_timeframe'),
            'auto_selected_set_id': plan.get('auto_selected_set_id'),
            'auto_selected_set_name': plan.get('auto_selected_set_name'),
            'decision_candle_ts': plan.get('decision_candle_ts'),
            'ev_adaptive_enabled': bool(plan.get('ev_adaptive_enabled', False)),
            'ev_adaptive_mode': plan.get('ev_adaptive_mode'),
            'ev_exit_profile_name': plan.get('ev_exit_profile_name'),
            'ev_exit_mode': plan.get('ev_exit_mode'),
            'ev_net_edge_r': plan.get('ev_net_edge_r'),
            'ev_time_stop_enabled': bool(plan.get('ev_time_stop_enabled', False)),
            'ev_time_stop_bars': int(plan.get('ev_time_stop_bars', 8) or 8),
            'ev_time_stop_min_mfe_r': float(plan.get('ev_time_stop_min_mfe_r', 0.45) or 0.45),
            'ev_mfe_profit_lock_enabled': bool(
                plan.get(
                    'ev_mfe_profit_lock_enabled',
                    cfg.get('ev_mfe_profit_lock_enabled', True),
                )
            ),
            'ev_mfe_lock_trigger_1_r': float(
                plan.get(
                    'ev_mfe_lock_trigger_1_r',
                    cfg.get('ev_mfe_lock_trigger_1_r', 1.50),
                )
                or 1.50
            ),
            'ev_mfe_lock_trigger_2_r': float(
                plan.get(
                    'ev_mfe_lock_trigger_2_r',
                    cfg.get('ev_mfe_lock_trigger_2_r', 2.20),
                )
                or 2.20
            ),
            'ev_mfe_lock_trigger_3_r': float(
                plan.get(
                    'ev_mfe_lock_trigger_3_r',
                    cfg.get('ev_mfe_lock_trigger_3_r', 3.20),
                )
                or 3.20
            ),
            'ev_mfe_lock_stage': 0,
            'ev_mfe_lock_r': 0.0,
            'profit_alpha_enabled': bool(plan.get('profit_alpha_enabled', False)),
            'profit_alpha_engine': plan.get('profit_alpha_engine'),
            'profit_alpha_entry_type': plan.get('profit_alpha_entry_type') or plan.get('entry_edge_entry_type'),
            'profit_alpha_direction_score': plan.get('profit_alpha_direction_score') or plan.get('entry_edge_direction_score'),
            'profit_alpha_exit_policy': plan.get('profit_alpha_exit_policy') or plan.get('entry_edge_exit_policy'),
            'profit_alpha_score': plan.get('profit_alpha_score'),
            'profit_alpha_probability': plan.get('profit_alpha_probability'),
            'profit_alpha_risk_multiplier': plan.get('profit_alpha_risk_multiplier'),
            'profit_alpha_meta_key': plan.get('profit_alpha_meta_key'),
            'profit_alpha_follow_through_enabled': bool(
                plan.get(
                    'profit_alpha_follow_through_enabled',
                    cfg.get('profit_alpha_follow_through_enabled', True),
                )
            ),
            'profit_alpha_follow_through_bars': int(
                plan.get(
                    'profit_alpha_follow_through_bars',
                    cfg.get('profit_alpha_follow_through_bars', 3),
                )
                or 3
            ),
            'profit_alpha_follow_through_min_mfe_r': float(
                plan.get(
                    'profit_alpha_follow_through_min_mfe_r',
                    cfg.get('profit_alpha_follow_through_min_mfe_r', 0.35),
                )
                or 0.35
            ),
            'profit_alpha_early_exit_max_mae_r': float(
                plan.get(
                    'profit_alpha_early_exit_max_mae_r',
                    cfg.get('profit_alpha_early_exit_max_mae_r', 0.75),
                )
                or 0.75
            ),
            'bars_seen': 0,
            'last_bar_ts': None,
            'active': False,
            'tp1_filled': False,
            'tp2_filled': False,
            'tp2_fallback_reached_loops': 0,
            'created_at': datetime.now(timezone.utc).isoformat(),
        }
        return self._set_utbreakout_trailing_state(symbol, state)

    def _utbreakout_entry_record_for_symbol(self, symbol, *, include_historic=False):
        store = getattr(self, 'trading_state_store', None)
        if store is None:
            return None
        try:
            records = list(store.active_for_symbol(symbol) or [])
            if include_historic and not records:
                records = list(store.records_for_symbol(symbol) or [])
        except Exception:
            logger.debug(
                "UTBreak entry-state lookup failed for %s",
                symbol,
                exc_info=True,
            )
            return None
        entries = [
            record
            for record in records
            if str(getattr(record, 'order_intent', '') or '').upper() == 'ENTRY'
        ]
        if not entries:
            return None
        return max(
            entries,
            key=lambda record: str(
                getattr(record, 'updated_at', None)
                or getattr(record, 'created_at', None)
                or ''
            ),
        )

    def _persist_active_entry_protection_refs(
        self,
        symbol,
        *,
        stop_order_id=None,
        take_profit_order_ids=None,
        take_profit_order_labels=None,
    ):
        record = self._utbreakout_entry_record_for_symbol(symbol)
        store = getattr(self, 'trading_state_store', None)
        if record is None or store is None:
            return None
        changes = {}
        if stop_order_id not in (None, ''):
            changes['stop_order_id'] = str(stop_order_id)
        existing_ids = [
            str(value)
            for value in (getattr(record, 'take_profit_order_ids', None) or [])
            if value not in (None, '')
        ]
        if take_profit_order_ids is not None:
            for value in take_profit_order_ids:
                text = str(value or '').strip()
                if text and text not in existing_ids:
                    existing_ids.append(text)
            changes['take_profit_order_ids'] = existing_ids
        labels = dict(
            (getattr(record, 'metadata', {}) or {}).get(
                'take_profit_order_labels',
                {},
            )
            or {}
        )
        if isinstance(take_profit_order_labels, dict):
            labels.update({
                str(order_id): str(label).upper()
                for order_id, label in take_profit_order_labels.items()
                if order_id not in (None, '') and label not in (None, '')
            })
            changes['take_profit_order_labels'] = labels
        if not changes:
            return record
        try:
            return store.transition(
                record.client_order_id,
                record.order_state,
                **changes,
            )
        except Exception:
            logger.debug(
                "UTBreak protection reference persistence failed for %s",
                symbol,
                exc_info=True,
            )
            return None

    def _update_utbreakout_fill_flags_from_position_qty(
        self,
        symbol,
        state,
        current_qty,
    ):
        if not isinstance(state, dict):
            return state
        initial_qty = max(0.0, float(state.get('initial_qty', 0.0) or 0.0))
        remaining_qty = max(0.0, float(current_qty or 0.0))
        if initial_qty <= 0:
            return state
        try:
            amount_step = max(
                0.0,
                float(self._get_amount_step_for_symbol(symbol) or 0.0),
            )
        except Exception:
            amount_step = 0.0
        qty_tolerance = max(
            initial_qty * 1e-8,
            amount_step * 0.51,
            1e-12,
        )
        raw_orders = [
            item
            for item in (
                state.get('planned_tp_orders')
                or state.get('tp_orders')
                or []
            )
            if isinstance(item, dict)
        ]
        raw_orders.sort(key=lambda item: int(item.get('tp_index') or 999))
        cumulative_exit_qty = 0.0
        newly_filled = []
        for index, item in enumerate(raw_orders, 1):
            label = _normalize_tp_plan_label(
                item.get('tp_label') or item.get('tp_name') or item.get('label'),
                f'TP{index}',
            )
            planned_qty = max(
                0.0,
                float(item.get('qty') or item.get('quantity') or 0.0),
            )
            if planned_qty <= 0:
                continue
            cumulative_exit_qty = min(
                initial_qty,
                cumulative_exit_qty + planned_qty,
            )
            expected_remaining = max(0.0, initial_qty - cumulative_exit_qty)
            filled = remaining_qty <= expected_remaining + qty_tolerance
            state_key = f'{label.lower()}_filled'
            if filled and not bool(state.get(state_key)):
                newly_filled.append(label)
                state[state_key] = True
                state[f'{label.lower()}_filled_at_qty'] = remaining_qty
                state[f'{label.lower()}_expected_remaining_qty'] = (
                    expected_remaining
                )
            if filled:
                item['filled'] = True
        state['current_qty'] = remaining_qty
        state['current_remaining_ratio'] = remaining_qty / initial_qty
        state['last_fill_sync_new'] = newly_filled
        return state

    def _sync_utbreakout_state_with_protection_orders(
        self,
        symbol,
        state,
        protection_orders,
    ):
        if not isinstance(state, dict):
            return state
        planned = [
            item
            for item in (
                state.get('planned_tp_orders')
                or state.get('tp_orders')
                or []
            )
            if isinstance(item, dict)
        ]
        actual_tps = [
            order
            for order in (protection_orders or [])
            if self._classify_protection_order(order) == 'tp'
        ]
        labels_by_id = {}
        for order in actual_tps:
            label = self._protection_tp_label(order, planned)
            if not label:
                continue
            label = _normalize_tp_plan_label(label)
            target = next(
                (
                    item
                    for item in planned
                    if _normalize_tp_plan_label(
                        item.get('tp_label') or item.get('tp_name')
                    ) == label
                ),
                None,
            )
            if target is None:
                continue
            info = self._protection_order_info(order)
            price = _safe_float_or_none(
                order.get('price')
                or info.get('price')
                or self._protection_trigger_price(order)
            )
            qty = self._protection_order_amount(order)
            order_id = self._protection_order_id(order)
            client_id = self._protection_client_order_id(order)
            if price is not None:
                target['price'] = float(price)
            if qty is not None:
                target['qty'] = float(qty)
            target['order_id'] = order_id
            target['client_order_id'] = client_id
            if order_id:
                labels_by_id[str(order_id)] = label
        state['planned_tp_orders'] = planned
        state['tp_orders'] = list(planned)
        sl_orders = [
            order
            for order in (protection_orders or [])
            if self._classify_protection_order(order) == 'sl'
        ]
        newest_sl = self._newest_protection_order(sl_orders)
        if newest_sl is not None:
            state['sl_order_id'] = self._protection_order_id(newest_sl)
        self._persist_active_entry_protection_refs(
            symbol,
            stop_order_id=state.get('sl_order_id'),
            take_profit_order_ids=list(labels_by_id),
            take_profit_order_labels=labels_by_id,
        )
        return self._set_utbreakout_trailing_state(symbol, state)

    async def _recover_utbreakout_trailing_state_from_exchange(self, symbol, pos, strategy_params=None):
        symbol = self._canonical_futures_symbol(symbol)
        existing = self._get_utbreakout_trailing_state(symbol)
        if isinstance(existing, dict):
            return existing
        if not pos:
            return None
        side = str(pos.get('side') or '').lower()
        if side not in {'long', 'short'}:
            return None
        current_qty = abs(float(
            self._position_signed_contracts(pos)
            or pos.get('contracts', 0)
            or 0
        ))
        entry_record = self._utbreakout_entry_record_for_symbol(symbol)
        entry_metadata = dict(getattr(entry_record, 'metadata', {}) or {})
        entry_plan = dict(entry_metadata.get('entry_plan_summary') or {})
        record_entry_price = _safe_float_or_none(
            getattr(entry_record, 'average_fill_price', None)
        )
        entry_price = (
            record_entry_price
            or _safe_float_or_none(pos.get('entryPrice'))
        )
        record_filled_qty = float(
            getattr(entry_record, 'filled_qty', 0.0) or 0.0
        )
        record_initial_qty = (
            record_filled_qty
            if record_filled_qty > 0
            else float(getattr(entry_record, 'requested_qty', 0.0) or 0.0)
        )
        original_qty = max(current_qty, record_initial_qty)
        if entry_price is None or entry_price <= 0 or current_qty <= 0:
            return None

        fetch_ok, protection_orders = await self._collect_protection_orders_checked(symbol)
        if not fetch_ok:
            return None
        close_side = 'sell' if side == 'long' else 'buy'
        bot_stop_orders = [
            order
            for order in protection_orders or []
            if (
                self._classify_protection_order(order) == 'sl'
                and self._protection_order_side(order) == close_side
                and self._protection_client_order_id(order).lower().startswith('utbsl')
                and self._protection_trigger_price(order) is not None
            )
        ]
        adverse_stops = [
            order
            for order in bot_stop_orders
            if (
                side == 'long'
                and float(self._protection_trigger_price(order)) < float(entry_price)
            ) or (
                side == 'short'
                and float(self._protection_trigger_price(order)) > float(entry_price)
            )
        ]
        persisted_risk_distance = _safe_float_or_none(
            entry_plan.get('risk_distance')
        )
        if persisted_risk_distance is not None:
            persisted_risk_distance = abs(float(persisted_risk_distance))
        initial_stop = None
        if persisted_risk_distance is not None and persisted_risk_distance > 0:
            initial_stop = (
                float(entry_price) - persisted_risk_distance
                if side == 'long'
                else float(entry_price) + persisted_risk_distance
            )
        elif adverse_stops:
            initial_stop_order = self._newest_protection_order(adverse_stops)
            initial_stop = float(
                self._protection_trigger_price(initial_stop_order)
            )
        risk_distance = (
            abs(float(entry_price) - float(initial_stop))
            if initial_stop is not None
            else 0.0
        )
        if risk_distance <= 0:
            return None

        protective_stop_order = None
        protective_stop = float(initial_stop)
        if bot_stop_orders:
            protective_stop_order = (
                max(
                    bot_stop_orders,
                    key=lambda order: float(
                        self._protection_trigger_price(order)
                    ),
                )
                if side == 'long'
                else min(
                    bot_stop_orders,
                    key=lambda order: float(
                        self._protection_trigger_price(order)
                    ),
                )
            )
            protective_stop = float(
                self._protection_trigger_price(protective_stop_order)
            )
        try:
            cfg = self._get_utbot_filtered_breakout_config(
                strategy_params if isinstance(strategy_params, dict) else self.get_runtime_strategy_params()
            )
        except Exception:
            cfg = build_default_utbot_filtered_breakout_config()
        cfg = apply_profit_opportunity_effective_overrides(dict(cfg or {}))
        recovery_plan = dict(entry_plan)
        recovery_plan.update({
            'risk_distance': risk_distance,
            'stop_loss': float(initial_stop),
            'recovered_from_exchange': True,
            'entry_timestamp_ms': (
                _timestamp_ms_or_none(getattr(entry_record, 'created_at', None))
                or _timestamp_ms_or_none(pos.get('timestamp'))
                or _timestamp_ms_or_none(pos.get('datetime'))
            ),
        })
        state = self._register_utbreakout_trailing_state(
            symbol,
            side,
            float(entry_price),
            original_qty,
            recovery_plan,
            cfg,
        )
        if not isinstance(state, dict):
            return None
        state['last_stop_price'] = protective_stop
        state['sl_order_id'] = (
            self._protection_order_id(protective_stop_order)
            if protective_stop_order is not None
            else None
        )
        state['active'] = (
            protective_stop >= float(entry_price)
            if side == 'long'
            else protective_stop <= float(entry_price)
        )
        state['recovered_from_exchange'] = True
        state['recovered_at'] = datetime.now(timezone.utc).isoformat()
        state['recovered_current_qty'] = current_qty
        state['recovered_original_qty'] = original_qty
        state = self._sync_utbreakout_state_with_protection_orders(
            symbol,
            state,
            protection_orders,
        )
        state = self._update_utbreakout_fill_flags_from_position_qty(
            symbol,
            state,
            current_qty,
        )
        self._set_utbreakout_trailing_state(symbol, state)
        logger.warning(
            "[Protection Recovery] restored UTBreak state for %s: side=%s qty=%.12f/%.12f "
            "entry=%.12f initial_stop=%.12f active_stop=%.12f",
            symbol,
            side,
            current_qty,
            original_qty,
            float(entry_price),
            float(initial_stop),
            protective_stop,
        )
        return state

    async def _recover_open_utbreakout_positions_on_start(self):
        try:
            strategy_params = self.get_runtime_strategy_params()
            active_strategy = str(strategy_params.get('active_strategy') or '').lower()
            if self.is_upbit_mode():
                return {'status': 'SKIPPED', 'recovered': 0}
            positions = await asyncio.to_thread(self.exchange.fetch_positions)
        except Exception as exc:
            logger.warning(f"UTBreak startup protection recovery position fetch failed: {exc}")
            return {'status': 'POSITION_FETCH_FAILED', 'recovered': 0, 'error': str(exc)}

        recovered = 0
        audited = 0
        for raw_pos in positions or []:
            try:
                pos = self._normalize_server_position(raw_pos)
                if not pos:
                    continue
                raw_symbol = self._protection_position_symbol(pos)
                symbol_key = self._normalize_protection_symbol(raw_symbol)
                symbol = self._canonical_futures_symbol(
                    self._protection_unified_symbol_from_key(
                        symbol_key,
                        raw_symbol,
                    )
                )
                if not symbol:
                    continue
                self.active_symbols.add(symbol)
                state = None
                if active_strategy in UTBREAKOUT_STRATEGIES:
                    state = await self._recover_utbreakout_trailing_state_from_exchange(
                        symbol,
                        pos,
                        strategy_params,
                    )
                if isinstance(state, dict):
                    recovered += 1
                    cfg = self._get_utbot_filtered_breakout_config(strategy_params)
                    await self._audit_and_repair_live_ladder_protection(
                        symbol,
                        pos,
                        state,
                        cfg,
                        reason='UTBreak startup protection recovery',
                    )
                else:
                    await self._audit_protection_orders(
                        symbol,
                        pos=pos,
                        expected_tp=False,
                        expected_sl=True,
                        alert=True,
                    )
                audited += 1
            except Exception as exc:
                logger.exception(
                    "UTBreak startup protection recovery failed for %s: %s",
                    self._protection_position_symbol(raw_pos),
                    exc,
                )
        logger.warning(
            "[Protection Recovery] startup sweep complete: recovered=%s audited=%s",
            recovered,
            audited,
        )
        return {'status': 'OK', 'recovered': recovered, 'audited': audited}

    def _utbreakout_short_guard_passes(self, cfg, values):
        if not bool(cfg.get('short_conservative_enabled', True)):
            return True, "short guard off"
        try:
            htf_close = float(values.get('htf_close'))
            htf_fast = float(values.get('htf_ema_fast'))
            htf_slow = float(values.get('htf_ema_slow'))
            adx = float(values.get('adx'))
            plus_di = float(values.get('plus_di'))
            minus_di = float(values.get('minus_di'))
            threshold = float(cfg.get('short_adx_threshold', cfg.get('adx_threshold', 25.0)) or 25.0)
            dmi_gap_min = float(cfg.get('short_dmi_min_gap', 4.0) or 4.0)
        except (TypeError, ValueError):
            return False, "short guard data pending"
        htf_supertrend = str(values.get('htf_supertrend_direction') or '').lower()
        entry_price = values.get('entry_price')
        ema50 = values.get('ema50')
        ema50_prev = values.get('ema50_prev')
        momentum_6 = values.get('momentum_6_pct')
        momentum_12 = values.get('momentum_12_pct')
        checks = [
            (htf_fast < htf_slow, f"HTF EMA{int(cfg.get('ema_fast', 50) or 50)} < EMA{int(cfg.get('ema_slow', 200) or 200)}"),
            (htf_close < htf_slow, "HTF close < EMA slow"),
            (adx >= threshold, f"ADX >= {threshold:.1f}"),
            (minus_di > plus_di, "-DI > +DI"),
            ((minus_di - plus_di) >= dmi_gap_min, f"-DI gap >= {dmi_gap_min:.1f}"),
        ]
        if bool(cfg.get('short_require_htf_supertrend', True)):
            checks.append((htf_supertrend == 'short', "HTF Supertrend SHORT"))
        if bool(cfg.get('short_require_entry_ema_downtrend', True)):
            ema_downtrend = False
            if (
                self._is_valid_number(entry_price)
                and self._is_valid_number(ema50)
                and self._is_valid_number(ema50_prev)
            ):
                ema_downtrend = float(entry_price) < float(ema50) and float(ema50) < float(ema50_prev)
            checks.append((ema_downtrend, "entry close < EMA50 and EMA50 falling"))
        if bool(cfg.get('short_require_momentum_downtrend', True)):
            momentum_downtrend = False
            if self._is_valid_number(momentum_6) and self._is_valid_number(momentum_12):
                momentum_downtrend = float(momentum_6) < 0 and float(momentum_12) < 0
            checks.append((momentum_downtrend, "6/12 momentum negative"))
        failed = [label for ok, label in checks if not ok]
        if failed:
            return False, "; ".join(failed)
        return True, "short guard passed"

    def _build_utbreakout_short_guard_status_item(self, cfg, values):
        if not bool(cfg.get('short_conservative_enabled', True)):
            return ("보수적 숏 가드", True, "OFF")
        ok, reason = self._utbreakout_short_guard_passes(cfg, values)
        try:
            risk_multiplier = float(cfg.get('short_risk_multiplier', 0.5) or 0.5)
        except (TypeError, ValueError):
            risk_multiplier = 0.5
        detail = f"ON / {reason} / 숏 리스크 x{risk_multiplier:.2f}"
        return ("보수적 숏 가드", ok, detail)

    def _evaluate_utbreakout_market_quality(self, side, cfg, values):
        side = str(side or '').lower()
        if side not in {'long', 'short'}:
            return {
                'enabled': False,
                'state': False,
                'risk_multiplier': 0.0,
                'hard_block': True,
                'summary': 'invalid side',
                'reasons': ['invalid side'],
                'positives': [],
            }
        if not bool(cfg.get('market_quality_enabled', True)):
            return {
                'enabled': False,
                'state': True,
                'risk_multiplier': 1.0,
                'hard_block': False,
                'summary': 'OFF',
                'reasons': ['OFF'],
                'positives': [],
            }

        values = dict(values or {})
        relaxation_mode = str(cfg.get('utbreak_entry_relaxation_mode') or 'balanced').strip().lower()
        if relaxation_mode not in {'strict', 'balanced', 'active'}:
            relaxation_mode = 'balanced'
        risk_multiplier = 1.0
        hard_block = False
        reasons = []
        positives = []
        data_seen = 0
        diagnostics = {
            'stale_policy': relaxation_mode,
            'funding_action': 'pass',
            'oi_action': 'pass',
            'taker_flow_action': 'pass',
            'regime_action': 'pass',
            'market_quality_action': 'pass',
        }

        def _f(key):
            value = values.get(key)
            if self._is_valid_number(value):
                return float(value)
            return None

        def _reduce(factor, reason):
            nonlocal risk_multiplier
            risk_multiplier *= max(0.0, min(1.0, float(factor or 0.0)))
            reasons.append(reason)

        def _block(reason):
            nonlocal hard_block, risk_multiplier
            hard_block = True
            risk_multiplier = 0.0
            reasons.append(reason)

        def _soft_or_block(factor, reason, action_key, *, severe=False):
            if relaxation_mode == 'strict' or severe:
                diagnostics[action_key] = 'block'
                _block(reason)
                return
            diagnostics[action_key] = 'size_reduce'
            _reduce(factor, f"{reason}; {action_key}=size_reduce x{float(factor):.2f}")

        atr_pct = _f('atr_pct')
        if atr_pct is not None:
            data_seen += 1
            entry_timeframe = (
                values.get('entry_timeframe')
                or values.get('timeframe')
                or cfg.get('entry_timeframe')
                or '15m'
            )
            atr_scale_kwargs = {
                'reference_timeframe': cfg.get('atr_threshold_reference_timeframe', '15m'),
                'enabled': bool(cfg.get('atr_threshold_timeframe_scaling_enabled', True)),
                'max_scale': cfg.get('atr_threshold_max_scale', 5.0),
            }
            high_atr = scale_atr_percent_threshold(
                cfg.get('market_quality_high_atr_pct', 1.5),
                entry_timeframe,
                **atr_scale_kwargs,
            )
            extreme_atr = scale_atr_percent_threshold(
                cfg.get('market_quality_extreme_atr_pct', 2.5),
                entry_timeframe,
                **atr_scale_kwargs,
            )
            diagnostics['atr_timeframe'] = str(entry_timeframe)
            diagnostics['atr_high_threshold_pct'] = round(float(high_atr), 6)
            diagnostics['atr_extreme_threshold_pct'] = round(float(extreme_atr), 6)
            if atr_pct >= extreme_atr:
                if side == 'long':
                    _reduce(
                        0.50,
                        f"ATR% {atr_pct:.3f} extreme >= {extreme_atr:.3f} ({entry_timeframe})",
                    )
                else:
                    _block(
                        f"ATR% {atr_pct:.3f} >= extreme {extreme_atr:.3f} ({entry_timeframe})"
                    )
            elif atr_pct >= high_atr:
                _reduce(
                    0.50,
                    f"ATR% {atr_pct:.3f} high >= {high_atr:.3f} ({entry_timeframe})",
                )
            else:
                positives.append(f"ATR% {atr_pct:.3f}")

        funding = _f('funding_rate')
        if funding is not None:
            data_seen += 1
            adverse_funding = funding if side == 'long' else -funding
            soft = float(cfg.get('market_quality_adverse_funding_soft', 0.0006) or 0.0006)
            hard = float(cfg.get('market_quality_adverse_funding_hard', 0.0015) or 0.0015)
            if adverse_funding >= hard:
                if side == 'long':
                    _reduce(0.50, f"funding adverse {funding:.6f}")
                else:
                    _soft_or_block(
                        0.55 if relaxation_mode == 'balanced' else 0.65,
                        f"funding adverse {funding:.6f}",
                        'funding_action',
                        severe=adverse_funding >= hard * 1.5,
                    )
            elif adverse_funding >= soft * 1.5:
                diagnostics['funding_action'] = 'size_reduce'
                _reduce(0.50, f"funding adverse {funding:.6f}")
            elif adverse_funding >= soft:
                diagnostics['funding_action'] = 'size_reduce'
                _reduce(0.75, f"funding mildly adverse {funding:.6f}")
            else:
                positives.append(f"funding {funding:.6f}")

        long_short = _f('long_short_ratio')
        if long_short is not None:
            data_seen += 1
            if side == 'long' and long_short >= 1.85:
                _reduce(0.75, f"L/S crowded {long_short:.2f}")
            elif side == 'short' and long_short <= 0.55:
                _reduce(0.75, f"L/S short crowded {long_short:.2f}")
            else:
                positives.append(f"L/S {long_short:.2f}")

        oi_delta = _f('open_interest_delta_pct')
        if oi_delta is not None:
            data_seen += 1
            if oi_delta <= -0.40:
                diagnostics['oi_action'] = 'size_reduce'
                _reduce(0.75, f"OI not confirming {oi_delta:.2f}%")
            elif oi_delta >= 0.20:
                positives.append(f"OI confirms {oi_delta:.2f}%")
            else:
                positives.append(f"OI flat {oi_delta:.2f}%")

        taker_ratio = _f('taker_buy_sell_ratio')
        if taker_ratio is not None:
            data_seen += 1
            if side == 'long':
                if taker_ratio < 0.95:
                    diagnostics['taker_flow_action'] = 'size_reduce'
                    _reduce(0.75, f"taker flow against {taker_ratio:.3f}")
                elif taker_ratio >= 1.03:
                    positives.append(f"taker flow {taker_ratio:.3f}")
            else:
                if taker_ratio > 1.05:
                    diagnostics['taker_flow_action'] = 'size_reduce'
                    _reduce(0.75, f"taker flow against {taker_ratio:.3f}")
                elif taker_ratio <= 0.97:
                    positives.append(f"taker flow {taker_ratio:.3f}")

        basis_pct = _f('basis_pct')
        if basis_pct is not None:
            data_seen += 1
            adverse_basis = basis_pct if side == 'long' else -basis_pct
            if adverse_basis >= 0.35:
                _reduce(0.50, f"basis adverse {basis_pct:.3f}%")
            elif adverse_basis >= 0.15:
                _reduce(0.75, f"basis mildly adverse {basis_pct:.3f}%")
            else:
                positives.append(f"basis {basis_pct:.3f}%")

        spread_pct = _f('futures_spread_pct')
        if spread_pct is not None:
            data_seen += 1
            if spread_pct >= 0.10:
                _block(f"spread too wide {spread_pct:.4f}%")
            elif spread_pct >= 0.05:
                _reduce(0.50, f"spread wide {spread_pct:.4f}%")
            else:
                positives.append(f"spread {spread_pct:.4f}%")

        regime = values.get('market_regime_context')
        if bool(cfg.get('market_quality_regime_enabled', True)) and isinstance(regime, dict):
            items = regime.get('items') if isinstance(regime.get('items'), dict) else {}
            strong_move = float(cfg.get('market_quality_regime_strong_move_pct', 1.5) or 1.5)
            for raw_symbol, item in items.items():
                if not isinstance(item, dict):
                    continue
                data_seen += 1
                symbol_label = str(raw_symbol or '')
                direction = str(item.get('direction') or 'neutral').lower()
                ret_pct = item.get('return_lookback_pct')
                ret_value = float(ret_pct) if self._is_valid_number(ret_pct) else 0.0
                opposite = direction == ('short' if side == 'long' else 'long')
                aligned = direction == side
                strong_opposite = (
                    side == 'long' and ret_value <= -strong_move
                    or side == 'short' and ret_value >= strong_move
                )
                is_btc = symbol_label.upper().startswith('BTC')
                if opposite and is_btc and strong_opposite:
                    if side == 'long':
                        diagnostics['regime_action'] = 'size_reduce'
                        _reduce(0.50, f"BTC strong opposite regime {direction.upper()} {ret_value:.2f}%")
                    else:
                        _soft_or_block(
                            0.50 if relaxation_mode == 'balanced' else 0.60,
                            f"BTC strong opposite regime {direction.upper()} {ret_value:.2f}%",
                            'regime_action',
                            severe=abs(ret_value) >= strong_move * 2.0,
                        )
                elif opposite and is_btc:
                    diagnostics['regime_action'] = 'size_reduce'
                    _reduce(0.50, f"BTC opposite regime {direction.upper()}")
                elif opposite:
                    diagnostics['regime_action'] = 'size_reduce'
                    _reduce(0.75, f"{symbol_label} opposite regime {direction.upper()}")
                elif aligned:
                    positives.append(f"{symbol_label} {direction.upper()}")

        if (
            side == 'long'
            and self._is_utbreakout_live_runtime(cfg)
            and bool(cfg.get('market_quality_long_hard_block_on_multi_adverse_enabled', True))
            and len(reasons) >= int(cfg.get('market_quality_long_multi_adverse_min_reasons', 3) or 3)
            and risk_multiplier <= float(cfg.get('market_quality_long_multi_adverse_max_multiplier', 0.35) or 0.35)
        ):
            if relaxation_mode == 'strict':
                diagnostics['market_quality_action'] = 'block'
                _block(
                    "LONG market multi-adverse: "
                    + "; ".join(str(reason) for reason in reasons[:6])
                )
            else:
                diagnostics['market_quality_action'] = 'size_reduce'
                floor = 0.30 if relaxation_mode == 'balanced' else 0.35
                risk_multiplier = max(risk_multiplier, floor)
                reasons.append(
                    "LONG market multi-adverse softened; "
                    f"market_quality_action=size_reduce floor x{floor:.2f}"
                )

        min_multiplier = float(cfg.get('market_quality_min_risk_multiplier', 0.25) or 0.25)
        if data_seen <= 0 and bool(cfg.get('market_quality_data_required', False)):
            _block('market quality data missing')
        elif data_seen <= 0:
            positives.append('market data neutral')
        if not hard_block and risk_multiplier < min_multiplier:
            if side == 'long':
                reasons.append(f"risk floor {min_multiplier:.2f} after reductions")
                risk_multiplier = min_multiplier
            else:
                relaxed_floor = min_multiplier * (0.75 if relaxation_mode == 'balanced' else 0.60)
                if relaxation_mode != 'strict' and risk_multiplier + 1e-12 >= relaxed_floor:
                    diagnostics['market_quality_action'] = 'size_reduce'
                    reasons.append(
                        f"risk floor {min_multiplier:.2f} after reductions; "
                        "market_quality_action=size_reduce"
                    )
                    risk_multiplier = min_multiplier
                else:
                    diagnostics['market_quality_action'] = 'block'
                    _block(f"risk multiplier {risk_multiplier:.2f} < min {min_multiplier:.2f}")

        risk_multiplier = max(0.0, min(1.0, float(risk_multiplier)))
        if hard_block:
            state = False
            headline = "BLOCK"
        elif risk_multiplier < 0.999:
            state = 'reduced'
            headline = "REDUCE"
        else:
            state = True
            headline = "PASS"
        detail_parts = reasons if reasons else positives
        if not detail_parts:
            detail_parts = ['neutral']
        summary = f"{headline} x{risk_multiplier:.2f}: " + "; ".join(detail_parts[:5])
        return {
            'enabled': True,
            'state': state,
            'risk_multiplier': round(risk_multiplier, 4),
            'hard_block': bool(hard_block),
            'summary': summary,
            'reasons': list(reasons),
            'positives': list(positives),
            'diagnostics': diagnostics,
        }

    def _build_utbreakout_market_quality_status_item(self, side, cfg, values):
        quality = self._evaluate_utbreakout_market_quality(side, cfg, values)
        return ("시장 품질 게이트", quality.get('state'), quality.get('summary'))

    def _evaluate_utbreakout_bias_continuation(self, side, cfg, status, values, selected_set=None):
        side = str(side or '').lower()
        cfg = dict(cfg or {})
        status = dict(status or {})
        values = dict(values or {})

        def _f(key):
            value = values.get(key)
            if self._is_valid_number(value):
                return float(value)
            return None

        def _sf(key):
            value = status.get(key)
            if self._is_valid_number(value):
                return float(value)
            return None

        def _cfg_float(key, default):
            try:
                return float(cfg.get(key, default) or default)
            except (TypeError, ValueError):
                return float(default)

        def _cfg_int(key, default):
            try:
                return int(cfg.get(key, default) or default)
            except (TypeError, ValueError):
                return int(default)

        candidate_type = str(status.get('candidate_type') or '').lower()
        if side not in {'long', 'short'}:
            return {
                'enabled': True,
                'state': False,
                'risk_multiplier': 0.0,
                'summary': 'BLOCK: invalid side',
                'reasons': ['invalid side'],
                'positives': [],
            }
        if candidate_type not in {'bias_state', 'bias_continuation'}:
            return {
                'enabled': bool(cfg.get('bias_continuation_enabled', True)),
                'state': True,
                'risk_multiplier': 1.0,
                'summary': 'fresh signal',
                'reasons': [],
                'positives': ['fresh signal'],
            }
        if not bool(cfg.get('bias_continuation_enabled', True)):
            return {
                'enabled': False,
                'state': True,
                'risk_multiplier': 1.0,
                'summary': 'OFF',
                'reasons': ['OFF'],
                'positives': [],
            }

        entry_tf = str(cfg.get('entry_timeframe', status.get('entry_timeframe', '15m')) or '15m').lower()
        tf_ms = self._timeframe_to_ms(entry_tf) or (15 * 60 * 1000)
        is_fast_tf = tf_ms <= 15 * 60 * 1000
        risk_multiplier = _cfg_float('bias_continuation_risk_multiplier', 0.65)
        if is_fast_tf:
            risk_multiplier = min(
                risk_multiplier,
                _cfg_float('bias_continuation_15m_risk_multiplier', 0.50),
            )
        risk_multiplier = max(0.0, min(1.0, risk_multiplier))

        reasons = []
        positives = []
        soft_reductions = []
        selected_score = None
        signal_age_candles = None
        extension_atr = None

        selected_id = None
        if isinstance(selected_set, dict):
            selected_id = selected_set.get('id')
        if selected_id is None:
            selected_id = status.get('auto_selected_set_id') or cfg.get('active_set_id')
        try:
            selected_id = int(selected_id)
        except (TypeError, ValueError):
            selected_id = None
        if selected_id in {49, 50}:
            reasons.append(f"Set{selected_id} fallback not allowed for bias continuation")

        decision_ts = _sf('decision_candle_ts')
        signal_ts = _sf('ut_signal_ts')
        max_age = (
            _cfg_int('bias_continuation_15m_max_signal_age_candles', 3)
            if is_fast_tf else
            _cfg_int('bias_continuation_max_signal_age_candles', 6)
        )
        if decision_ts is None or signal_ts is None or signal_ts <= 0 or decision_ts <= 0 or signal_ts > decision_ts:
            reasons.append('UT signal age missing')
        else:
            signal_age_candles = (decision_ts - signal_ts) / max(float(tf_ms), 1.0)
            if signal_age_candles > max_age:
                if bool(cfg.get('bias_continuation_stale_soft_reduce_enabled', True)):
                    stale_multiplier = _utbreakout_stale_signal_multiplier(
                        cfg,
                        signal_age_candles,
                        max_age,
                    )
                    risk_multiplier *= stale_multiplier
                    soft_reductions.append(
                        f"UT signal stale REDUCE x{stale_multiplier:.2f}: "
                        f"{signal_age_candles:.1f}>{max_age} candles"
                    )
                else:
                    reasons.append(f"UT signal stale {signal_age_candles:.1f}>{max_age} candles")
            else:
                positives.append(f"UT age {signal_age_candles:.1f}/{max_age} candles")

        if bool(cfg.get('adaptive_timeframe_enabled', False)):
            adaptive = status.get('adaptive_timeframe_decision')
            if not isinstance(adaptive, dict):
                adaptive = cfg.get('_adaptive_timeframe_decision') if isinstance(cfg.get('_adaptive_timeframe_decision'), dict) else {}
            if self._is_valid_number(adaptive.get('selected_score')):
                selected_score = float(adaptive.get('selected_score'))
            min_score = (
                _cfg_float('bias_continuation_15m_min_adaptive_tf_score', 50.0)
                if is_fast_tf else
                _cfg_float('bias_continuation_min_adaptive_tf_score', 42.0)
            )
            if selected_score is None:
                reasons.append('adaptive TF score missing')
            elif selected_score < min_score:
                reasons.append(f"adaptive TF score {selected_score:.1f}<{min_score:.1f}")
            else:
                positives.append(f"adaptive TF score {selected_score:.1f}>={min_score:.1f}")

        adx = _f('adx')
        plus_di = _f('plus_di')
        minus_di = _f('minus_di')
        min_adx = (
            _cfg_float('bias_continuation_15m_min_adx', 20.0)
            if is_fast_tf else
            _cfg_float('bias_continuation_min_adx', 18.0)
        )
        if adx is None or plus_di is None or minus_di is None:
            reasons.append('ADX/DMI missing')
        else:
            dmi_ok = plus_di > minus_di if side == 'long' else minus_di > plus_di
            if adx < min_adx:
                reasons.append(f"ADX {adx:.1f}<{min_adx:.1f}")
            if not dmi_ok:
                reasons.append(f"DMI against +DI {plus_di:.1f} / -DI {minus_di:.1f}")
            if adx >= min_adx and dmi_ok:
                positives.append(f"ADX/DMI aligned {adx:.1f}")

        close_value = _f('entry_price')
        open_value = _f('open')
        ema50 = _f('ema50')
        ema50_prev = _f('ema50_prev')
        ema_ok = False
        if close_value is not None and ema50 is not None and ema50_prev is not None:
            if side == 'long':
                ema_ok = close_value >= ema50 and ema50 >= ema50_prev
            else:
                ema_ok = close_value <= ema50 and ema50 <= ema50_prev

        htf_ok = False
        htf_supertrend_direction = str(values.get('htf_supertrend_direction') or '').lower()
        if htf_supertrend_direction == side:
            htf_ok = True
        if bool(values.get('htf_ready')):
            htf_close = _f('htf_close')
            htf_fast = _f('htf_ema_fast')
            htf_slow = _f('htf_ema_slow')
            if htf_close is not None and htf_fast is not None and htf_slow is not None:
                if side == 'long' and htf_close > htf_slow and htf_fast > htf_slow:
                    htf_ok = True
                elif side == 'short' and htf_close < htf_slow and htf_fast < htf_slow:
                    htf_ok = True
        if not (ema_ok or htf_ok):
            reasons.append('trend alignment missing')
        else:
            positives.append('trend aligned')

        atr_pct = _f('atr_pct')
        max_extension = (
            _cfg_float('bias_continuation_15m_max_extension_atr', 1.50)
            if is_fast_tf else
            _cfg_float('bias_continuation_max_extension_atr', 1.60)
        )
        extension_refs = []
        if close_value is None or atr_pct is None or atr_pct <= 0:
            reasons.append('ATR extension data missing')
        else:
            for label, key in (('EMA50', 'ema50'), ('VWAP', 'vwap'), ('BB mid', 'bb_mid')):
                ref = _f(key)
                if ref is None or ref <= 0:
                    continue
                dist_pct = abs(close_value - ref) / max(abs(close_value), 1e-9) * 100.0
                extension_refs.append((dist_pct / max(atr_pct, 1e-9), label, ref))
            if not extension_refs:
                reasons.append('extension reference missing')
            else:
                extension_atr, extension_label, _ = min(extension_refs, key=lambda item: item[0])
                if extension_atr > max_extension:
                    reasons.append(f"extension {extension_atr:.2f}ATR>{max_extension:.2f}ATR from {extension_label}")
                else:
                    positives.append(f"extension {extension_atr:.2f}ATR")

        volume_ratio = _f('volume_ratio')
        min_volume = (
            _cfg_float('bias_continuation_15m_min_volume_ratio', 0.45)
            if is_fast_tf else
            _cfg_float('bias_continuation_min_volume_ratio', 0.40)
        )
        if volume_ratio is None:
            if is_fast_tf:
                reasons.append('volume ratio missing')
            else:
                positives.append('volume neutral')
        elif volume_ratio < 0.25:
            reasons.append(f"volume ratio extremely weak {volume_ratio:.2f}<0.25")
        elif volume_ratio < min_volume:
            pass
        else:
            positives.append(f"volume ratio {volume_ratio:.2f}>={min_volume:.2f}")

        pullback_ok = False
        rebreak_ok = False
        flow_ok = False
        if close_value is not None and atr_pct is not None and atr_pct > 0:
            for _, key in (('EMA50', 'ema50'), ('VWAP', 'vwap'), ('BB mid', 'bb_mid')):
                ref = _f(key)
                if ref is None or ref <= 0:
                    continue
                dist_pct = abs(close_value - ref) / max(abs(close_value), 1e-9) * 100.0
                dist_atr = dist_pct / max(atr_pct, 1e-9)
                if side == 'long':
                    pullback_ok = pullback_ok or (close_value >= ref and dist_atr <= max_extension)
                else:
                    pullback_ok = pullback_ok or (close_value <= ref and dist_atr <= max_extension)
            if side == 'long':
                for key in ('donchian_high_prev', 'keltner_upper', 'bb_upper'):
                    level = _f(key)
                    rebreak_ok = rebreak_ok or (level is not None and close_value > level)
            else:
                for key in ('donchian_low_prev', 'keltner_lower', 'bb_lower'):
                    level = _f(key)
                    rebreak_ok = rebreak_ok or (level is not None and close_value < level)
            range_expansion = _f('range_expansion_ratio')
            candle_ok = True
            if open_value is not None:
                candle_ok = close_value >= open_value if side == 'long' else close_value <= open_value
            flow_ok = (
                volume_ratio is not None
                and volume_ratio >= min_volume
                and range_expansion is not None
                and range_expansion >= 1.05
                and candle_ok
            )
        if not (pullback_ok or rebreak_ok or flow_ok):
            reasons.append('no pullback/rebreak/flow continuation setup')
        else:
            setup_parts = []
            if pullback_ok:
                setup_parts.append('pullback')
            if rebreak_ok:
                setup_parts.append('rebreak')
            if flow_ok:
                setup_parts.append('flow')
            positives.append('setup ' + '/'.join(setup_parts))

        if side == 'long':
            def _is_long_soft_reason(reason):
                text = str(reason or '').lower()
                return any(fragment in text for fragment in (
                    'adaptive tf',
                    'adx',
                    'dmi',
                    'trend alignment',
                    'extension',
                    'volume',
                    'pullback',
                    'rebreak',
                    'flow continuation',
                ))

            def _is_long_soft_positive(positive):
                text = str(positive or '').lower()
                return any(fragment in text for fragment in (
                    'adaptive tf',
                    'adx',
                    'trend aligned',
                    'extension',
                    'volume',
                    'setup ',
                ))

            hard_reasons = [reason for reason in reasons if not _is_long_soft_reason(reason)]
            soft_failures = [reason for reason in reasons if _is_long_soft_reason(reason)]
            soft_pass_count = sum(1 for positive in positives if _is_long_soft_positive(positive))
            soft_total = soft_pass_count + len(soft_failures)
            if hard_reasons:
                return {
                    'enabled': True,
                    'state': False,
                    'risk_multiplier': 0.0,
                    'summary': 'BLOCK: ' + '; '.join(hard_reasons[:5]),
                    'reasons': hard_reasons + soft_failures,
                    'hard_reasons': hard_reasons,
                    'soft_failures': soft_failures,
                    'soft_pass_count': soft_pass_count,
                    'soft_total': soft_total,
                    'positives': positives,
                    'signal_age_candles': signal_age_candles,
                    'extension_atr': extension_atr,
                    'selected_tf_score': selected_score,
                }
            if soft_total <= 0 or soft_pass_count <= 0:
                return {
                    'enabled': True,
                    'state': False,
                    'risk_multiplier': 0.0,
                    'summary': 'BLOCK: no soft continuation checks passed',
                    'reasons': soft_failures or ['no soft continuation checks passed'],
                    'hard_reasons': [],
                    'soft_failures': soft_failures,
                    'soft_pass_count': soft_pass_count,
                    'soft_total': soft_total,
                    'positives': positives,
                    'signal_age_candles': signal_age_candles,
                    'extension_atr': extension_atr,
                    'selected_tf_score': selected_score,
                }
            if soft_pass_count >= 3:
                soft_multiplier = 1.0
            elif soft_pass_count == 2:
                soft_multiplier = 0.65
            else:
                soft_multiplier = 0.35
            adjusted_multiplier = max(0.0, min(1.0, risk_multiplier * soft_multiplier))
            state = True if soft_multiplier >= 0.999 else 'reduced'
            if soft_reductions:
                state = 'reduced'
            state_label = 'PASS' if state is True else 'REDUCE'
            summary_bits = list(soft_reductions)
            summary_bits.extend(positives[:5])
            if soft_failures:
                summary_bits.append('soft misses: ' + '; '.join(soft_failures[:3]))
            return {
                'enabled': True,
                'state': state,
                'risk_multiplier': round(adjusted_multiplier, 4),
                'summary': (
                    f"{state_label} x{adjusted_multiplier:.2f}: "
                    f"soft {soft_pass_count}/{soft_total}; " + '; '.join(summary_bits[:6])
                ),
                'reasons': soft_failures,
                'hard_reasons': [],
                'soft_failures': soft_failures,
                'soft_reductions': soft_reductions,
                'soft_pass_count': soft_pass_count,
                'soft_total': soft_total,
                'positives': positives,
                'signal_age_candles': signal_age_candles,
                'extension_atr': extension_atr,
                'selected_tf_score': selected_score,
            }

        if reasons:
            return {
                'enabled': True,
                'state': False,
                'risk_multiplier': 0.0,
                'summary': 'BLOCK: ' + '; '.join(reasons[:5]),
                'reasons': reasons,
                'positives': positives,
                'signal_age_candles': signal_age_candles,
                'extension_atr': extension_atr,
                'selected_tf_score': selected_score,
            }
        return {
            'enabled': True,
            'state': 'reduced' if soft_reductions else True,
            'risk_multiplier': round(risk_multiplier, 4),
            'summary': (
                f"{'REDUCE' if soft_reductions else 'PASS'} x{risk_multiplier:.2f}: "
                + '; '.join([*soft_reductions, *positives][:5])
            ),
            'reasons': [],
            'soft_reductions': soft_reductions,
            'positives': positives,
            'signal_age_candles': signal_age_candles,
            'extension_atr': extension_atr,
            'selected_tf_score': selected_score,
        }

    def _build_utbreakout_bias_continuation_status_item(self, side, cfg, status, values, selected_set=None):
        continuation = self._evaluate_utbreakout_bias_continuation(side, cfg, status, values, selected_set)
        return ("Bias continuation", continuation.get('state'), continuation.get('summary'))

    def _build_utbreakout_quality_score_v2(
        self,
        side,
        cfg,
        status,
        values,
        *,
        trend_health=None,
        strategy_quality=None,
        market_quality=None,
        selector_quality=None,
    ):
        side = str(side or '').lower()
        cfg = dict(cfg or {})
        status = dict(status or {})
        values = dict(values or {})
        if not bool(cfg.get('quality_score_v2_enabled', True)):
            return {
                'enabled': False,
                'score': 100.0,
                'state': True,
                'risk_multiplier': 1.0,
                'summary': 'quality score v2 OFF',
                'components': {},
                'reasons': ['OFF'],
            }

        def _score(value, default=50.0):
            if self._is_valid_number(value):
                return max(0.0, min(100.0, float(value)))
            return float(default)

        def _risk(value, default=1.0):
            if self._is_valid_number(value):
                return max(0.0, min(1.0, float(value)))
            return float(default)

        trend_health = dict(trend_health or {})
        strategy_quality = dict(strategy_quality or {})
        market_quality = dict(market_quality or {})
        selector_quality = dict(selector_quality or {})

        trend_score = _score(trend_health.get('score'))
        strategy_score = _score(strategy_quality.get('score'))
        market_multiplier = _risk(market_quality.get('risk_multiplier'))
        if market_quality.get('hard_block') or market_quality.get('state') is False:
            market_score = 0.0
        else:
            market_score = max(35.0, market_multiplier * 100.0)
        selector_score = _score(selector_quality.get('score'), 70.0)
        adaptive = status.get('adaptive_timeframe_decision')
        if not isinstance(adaptive, dict):
            adaptive = cfg.get('_adaptive_timeframe_decision') if isinstance(cfg.get('_adaptive_timeframe_decision'), dict) else {}
        adaptive_score = _score(adaptive.get('selected_score'), 65.0)
        candidate_type = str(status.get('candidate_type') or '').lower()
        if candidate_type == 'fresh_signal':
            signal_score = 92.0
        elif candidate_type == 'bias_continuation':
            signal_score = 74.0
        else:
            signal_score = 58.0

        components = {
            'trend_health': round(trend_score, 2),
            'strategy_quality': round(strategy_score, 2),
            'market_quality': round(market_score, 2),
            'selector_quality': round(selector_score, 2),
            'adaptive_timeframe': round(adaptive_score, 2),
            'signal_freshness': round(signal_score, 2),
        }
        score = (
            trend_score * 0.24
            + strategy_score * 0.28
            + market_score * 0.16
            + selector_score * 0.12
            + adaptive_score * 0.10
            + signal_score * 0.10
        )

        entry_tf = str(cfg.get('entry_timeframe', status.get('entry_timeframe', '15m')) or '15m').lower()
        tf_ms = self._timeframe_to_ms(entry_tf) or (15 * 60 * 1000)
        is_fast_tf = tf_ms <= 15 * 60 * 1000
        base_block_key = 'quality_score_v2_15m_block_below' if is_fast_tf else 'quality_score_v2_block_below'
        base_reduce_key = 'quality_score_v2_15m_reduce_below' if is_fast_tf else 'quality_score_v2_reduce_below'
        side_block_key = f"quality_score_v2_{side}_{'15m_' if is_fast_tf else ''}block_below"
        side_reduce_key = f"quality_score_v2_{side}_{'15m_' if is_fast_tf else ''}reduce_below"
        base_block_default = 62.0 if is_fast_tf else 60.0
        base_reduce_default = 72.0 if is_fast_tf else 70.0
        if side == 'long':
            base_block_default = 50.0
            base_reduce_default = 60.0
        block_below = float(cfg.get(side_block_key, cfg.get(base_block_key, base_block_default)) or base_block_default)
        reduce_below = float(cfg.get(side_reduce_key, cfg.get(base_reduce_key, base_reduce_default)) or base_reduce_default)
        full_score = max(reduce_below, float(cfg.get('quality_score_v2_full_score', 82.0) or 82.0))
        min_multiplier = max(0.05, min(1.0, float(cfg.get('quality_score_v2_min_risk_multiplier', 0.50) or 0.50)))

        reasons = []
        if trend_score < reduce_below:
            reasons.append(f"trend {trend_score:.0f}")
        if strategy_score < reduce_below:
            reasons.append(f"strategy {strategy_score:.0f}")
        if market_score < reduce_below:
            reasons.append(f"market {market_score:.0f}")
        if adaptive_score < block_below:
            reasons.append(f"adaptive {adaptive_score:.0f}")
        if candidate_type not in {'fresh_signal', 'bias_continuation'}:
            reasons.append('signal state weak')
        if not reasons:
            reasons.append('confluence aligned')

        if score < block_below:
            state = False
            risk_multiplier = 0.0
        elif score < reduce_below:
            state = 'reduced'
            risk_multiplier = min_multiplier
        elif score < full_score:
            state = 'reduced'
            scale = (score - reduce_below) / max(full_score - reduce_below, 1e-9)
            risk_multiplier = min_multiplier + (1.0 - min_multiplier) * max(0.0, min(1.0, scale))
        else:
            state = True
            risk_multiplier = 1.0

        state_label = 'BLOCK' if state is False else 'REDUCE' if state == 'reduced' else 'PASS'
        return {
            'enabled': True,
            'score': round(score, 2),
            'state': state,
            'risk_multiplier': round(risk_multiplier, 4),
            'components': components,
            'reasons': reasons,
            'block_below': block_below,
            'reduce_below': reduce_below,
            'summary': (
                f"quality score v2 {state_label} {score:.1f}/100 x{risk_multiplier:.2f} "
                f"(trend {trend_score:.0f}, strat {strategy_score:.0f}, market {market_score:.0f}, "
                f"selector {selector_score:.0f}, tf {adaptive_score:.0f}, sig {signal_score:.0f}; "
                f"{'; '.join(reasons[:4])})"
            ),
        }

    def _build_utbreakout_dynamic_tp2(self, side, cfg, quality_score_v2, trend_health=None, strategy_quality=None):
        cfg = dict(cfg or {})
        if not bool(cfg.get('dynamic_take_profit_enabled', True)):
            base_r = float(cfg.get('second_take_profit_r_multiple', cfg.get('take_profit_r_multiple', 3.50)) or 3.50)
            return {
                'enabled': False,
                'second_take_profit_r_multiple': base_r,
                'summary': f'dynamic TP2 OFF: {base_r:.2f}R',
                'tier': 'off',
            }
        quality_score_v2 = dict(quality_score_v2 or {})
        trend_health = dict(trend_health or {})
        strategy_quality = dict(strategy_quality or {})
        score = float(quality_score_v2.get('score', 0.0) or 0.0)
        trend_score = float(trend_health.get('score', 0.0) or 0.0)
        strategy_score = float(strategy_quality.get('score', 0.0) or 0.0)
        base_r = float(cfg.get('dynamic_tp2_base_r_multiple', 3.20) or 3.20)
        strong_r = float(cfg.get('dynamic_tp2_strong_r_multiple', 5.00) or 5.00)
        elite_r = float(cfg.get('dynamic_tp2_elite_r_multiple', 7.00) or 7.00)
        strong_score = float(cfg.get('dynamic_tp2_strong_score', 72.0) or 72.0)
        elite_score = float(cfg.get('dynamic_tp2_elite_score', 82.0) or 82.0)
        if score >= elite_score and trend_score >= 75.0 and strategy_score >= 75.0:
            tier = 'elite'
            second_r = elite_r
        elif score >= strong_score and trend_score >= 65.0 and strategy_score >= 65.0:
            tier = 'strong'
            second_r = strong_r
        else:
            tier = 'base'
            second_r = base_r
        second_r = max(base_r, min(elite_r, second_r))
        return {
            'enabled': True,
            'tier': tier,
            'second_take_profit_r_multiple': round(second_r, 4),
            'summary': (
                f"dynamic TP2 {tier}: {second_r:.2f}R "
                f"(Q {score:.1f}, trend {trend_score:.1f}, strat {strategy_score:.1f})"
            ),
        }

    def _calculate_utbreakout_directional_efficiency(self, closed, lookback=12):
        try:
            if closed is None or len(closed) < 3:
                return None, None
            lookback = max(2, int(lookback or 12))
            close = pd.to_numeric(closed['close'], errors='coerce').dropna().astype(float)
            if len(close) < lookback + 1:
                return None, None
            window = close.tail(lookback + 1)
            diffs = window.diff().abs().dropna()
            path = float(diffs.sum())
            if path <= 0:
                return 0.0, 0.0
            signed = float(window.iloc[-1] - window.iloc[0]) / path
            return abs(signed), signed
        except Exception:
            return None, None

    def _calculate_utbreakout_volatility_expansion_ratio(self, atr_series, cfg):
        try:
            if atr_series is None or len(atr_series) < 3:
                return None
            long_len = max(3, int(cfg.get('trend_health_volatility_long_length', 50) or 50))
            clean = pd.to_numeric(atr_series, errors='coerce').dropna().astype(float)
            if len(clean) < min(long_len, 5):
                return None
            current = float(clean.iloc[-1])
            baseline = float(clean.tail(long_len).mean())
            if current <= 0 or baseline <= 0:
                return None
            return current / baseline
        except Exception:
            return None

    def _enrich_utbreakout_trend_health_values(self, values, closed, cfg, atr_series=None):
        values = dict(values or {})
        efficiency, signed_efficiency = self._calculate_utbreakout_directional_efficiency(
            closed,
            cfg.get('trend_health_directional_lookback', 12)
        )
        if efficiency is not None:
            values['directional_efficiency'] = efficiency
            values['directional_efficiency_signed'] = signed_efficiency
        vol_ratio = self._calculate_utbreakout_volatility_expansion_ratio(atr_series, cfg)
        if vol_ratio is not None:
            values['volatility_expansion_ratio'] = vol_ratio
        if 'atr' not in values and atr_series is not None and len(atr_series):
            last_atr = atr_series.iloc[-1]
            if self._is_valid_number(last_atr):
                values['atr'] = float(last_atr)
        return values

    def _build_utbreakout_trend_health(self, side, cfg, values):
        return build_trend_health_score(cfg, values, side)

    def _calculate_utbreakout_hurst_exponent(self, close_series, lookback=64):
        try:
            clean = pd.to_numeric(close_series, errors='coerce').dropna().astype(float)
            if len(clean) < 20:
                return None
            lookback = max(20, int(lookback or 64))
            window = clean.tail(min(lookback, len(clean))).clip(lower=1e-12)
            values = np.log(window.to_numpy(dtype=float))
            lags = []
            tau_values = []
            for lag in (2, 4, 8, 16):
                if len(values) <= lag + 2:
                    continue
                diffs = values[lag:] - values[:-lag]
                tau = float(np.std(diffs))
                if np.isfinite(tau) and tau > 0:
                    lags.append(float(lag))
                    tau_values.append(tau)
            if len(lags) < 2:
                return None
            slope = float(np.polyfit(np.log(lags), np.log(tau_values), 1)[0])
            if not np.isfinite(slope):
                return None
            return max(0.0, min(1.0, slope))
        except Exception:
            return None

    def _enrich_utbreakout_strategy_quality_values(self, values, closed, cfg):
        values = dict(values or {})
        try:
            if closed is None or len(closed) < 5:
                return values
            local = closed.copy().reset_index(drop=True)
            for col in ['open', 'high', 'low', 'close']:
                local[col] = pd.to_numeric(local[col], errors='coerce')
            local = local.dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
            if len(local) < 5:
                return values
            close = local['close'].astype(float)
            last_close = float(close.iloc[-1])
            for lookback in (6, 12, 24):
                if len(close) > lookback:
                    start = float(close.iloc[-lookback - 1])
                    values[f'momentum_{lookback}_pct'] = (last_close / max(abs(start), 1e-9) - 1.0) * 100.0
            regression_lookback = max(6, int(cfg.get('strategy_quality_regression_lookback', 24) or 24))
            regression_window = close.tail(min(regression_lookback, len(close)))
            if len(regression_window) >= 6:
                x = np.arange(len(regression_window), dtype=float)
                y = regression_window.to_numpy(dtype=float)
                slope = float(np.polyfit(x, y, 1)[0])
                values['trend_slope_pct'] = slope * len(regression_window) / max(abs(float(y[-1])), 1e-9) * 100.0
            hurst = self._calculate_utbreakout_hurst_exponent(
                close,
                cfg.get('strategy_quality_hurst_lookback', 64)
            )
            if hurst is not None:
                values['hurst_exponent'] = hurst
            rebound_lookback = max(3, int(cfg.get('strategy_quality_rebound_lookback', 12) or 12))
            recent = local.tail(min(rebound_lookback, len(local)))
            min_low = float(recent['low'].min())
            max_high = float(recent['high'].max())
            values['recent_rebound_pct'] = (last_close / max(abs(min_low), 1e-9) - 1.0) * 100.0
            values['recent_drawdown_pct'] = (max_high - last_close) / max(abs(max_high), 1e-9) * 100.0
            row = local.iloc[-1]
            open_value = float(row['open'])
            high_value = float(row['high'])
            low_value = float(row['low'])
            candle_range = max(high_value - low_value, 1e-12)
            values['close_location'] = (last_close - low_value) / candle_range
            values['upper_wick_ratio'] = max(0.0, high_value - max(open_value, last_close)) / candle_range
            values['lower_wick_ratio'] = max(0.0, min(open_value, last_close) - low_value) / candle_range
            values['body_ratio'] = abs(last_close - open_value) / candle_range
        except Exception as exc:
            logger.debug(f"UTBreak strategy quality enrichment failed: {exc}")
        return values

    def _build_utbreakout_strategy_quality(self, side, cfg, values):
        return build_strategy_quality_score(cfg, values, side)

    def _utbreakout_shadow_key(self, symbol, side, decision_ts, set_id):
        return f"{str(symbol or '').upper()}:{str(side or '').lower()}:{int(decision_ts or 0)}:{set_id or 'set'}"

    def _register_utbreakout_shadow_candidate(self, symbol, side, status, plan, cfg, selected_set):
        if not bool(cfg.get('shadow_triple_barrier_enabled', True)):
            return None
        if not isinstance(plan, dict):
            return None
        decision_ts = int(plan.get('decision_candle_ts') or status.get('decision_candle_ts') or 0)
        if decision_ts <= 0:
            return None
        set_id = selected_set.get('id') if isinstance(selected_set, dict) else status.get('auto_selected_set_id')
        key = self._utbreakout_shadow_key(symbol, side, decision_ts, set_id)
        if key in getattr(self, 'utbreakout_shadow_resolved_keys', set()):
            return None
        pending = getattr(self, 'utbreakout_shadow_pending', {})
        if not isinstance(pending, dict):
            pending = {}
            self.utbreakout_shadow_pending = pending
        if key in pending:
            return pending[key]
        item = {
            'key': key,
            'symbol': symbol,
            'side': str(side or '').lower(),
            'decision_ts': decision_ts,
            'entry_price': plan.get('entry_price') or status.get('entry_price'),
            'stop_loss': plan.get('stop_loss'),
            'take_profit': plan.get('take_profit'),
            'risk_distance': plan.get('risk_distance'),
            'take_profit_r_multiple': plan.get('rr_multiple') or cfg.get('take_profit_r_multiple', 3.50),
            'max_bars': int(cfg.get('shadow_triple_barrier_max_bars', 24) or 24),
            'shadow_triple_logged': False,
            'shadow_runner_logged': False,
            'shadow_runner_exit_enabled': bool(cfg.get('shadow_runner_exit_enabled', False)),
            'shadow_runner_max_bars': int(cfg.get('shadow_runner_max_bars', 48) or 48),
            'runner_cfg': {
                key: cfg.get(key)
                for key in (
                    'atr_length',
                    'partial_take_profit_r_multiple',
                    'partial_take_profit_ratio',
                    'atr_trailing_activation_r',
                    'atr_trailing_breakeven_enabled',
                    'runner_chandelier_lookback',
                    'runner_structure_lookback',
                    'runner_structure_buffer_atr',
                    'runner_dynamic_multiplier_enabled',
                    'runner_chandelier_multiplier',
                    'runner_chandelier_multiplier_min',
                    'runner_chandelier_multiplier_max',
                    'runner_mfe_tighten_r',
                    'runner_mfe_tighten_delta',
                    'shadow_runner_max_bars',
                )
            },
            'auto_selected_set_id': set_id,
            'auto_selected_set_name': selected_set.get('name') if isinstance(selected_set, dict) else status.get('auto_selected_set_name'),
            'entry_timeframe': plan.get('entry_timeframe') or cfg.get('entry_timeframe', '15m'),
            'htf_timeframe': plan.get('htf_timeframe') or cfg.get('htf_timeframe', '1h'),
            'created_at': datetime.now(timezone.utc).isoformat(),
        }
        pending[key] = item
        return item

    def _update_utbreakout_shadow_triple_barrier(self, symbol, closed, cfg):
        if not bool(cfg.get('shadow_triple_barrier_enabled', True)):
            return []
        pending = getattr(self, 'utbreakout_shadow_pending', {})
        if not isinstance(pending, dict) or not pending:
            return []
        if closed is None or len(closed) < 2:
            return []
        resolved = []
        for key, item in list(pending.items()):
            if item.get('symbol') != symbol:
                continue
            status = {
                'candidate_side': item.get('side'),
                'entry_timeframe': item.get('entry_timeframe'),
                'htf_timeframe': item.get('htf_timeframe'),
                'auto_selected_set_id': item.get('auto_selected_set_id'),
                'auto_selected_set_name': item.get('auto_selected_set_name'),
                'decision_candle_ts': item.get('decision_ts'),
                'entry_price': item.get('entry_price'),
                'reason': "shadow triple-barrier",
                'entry_plan': {
                    'entry_price': item.get('entry_price'),
                    'stop_loss': item.get('stop_loss'),
                    'take_profit': item.get('take_profit'),
                    'risk_distance': item.get('risk_distance'),
                    'rr_multiple': item.get('take_profit_r_multiple'),
                    'decision_candle_ts': item.get('decision_ts'),
                }
            }
            if not item.get('shadow_triple_logged'):
                result = evaluate_shadow_triple_barrier(
                    side=item.get('side'),
                    entry_price=item.get('entry_price'),
                    stop_loss=item.get('stop_loss'),
                    take_profit=item.get('take_profit'),
                    risk_distance=item.get('risk_distance'),
                    take_profit_r_multiple=item.get('take_profit_r_multiple', 3.50),
                    decision_ts=item.get('decision_ts'),
                    bars=closed,
                    max_bars=item.get('max_bars', cfg.get('shadow_triple_barrier_max_bars', 24)),
                )
                if isinstance(result, dict):
                    extra = {
                        'code': result.get('code'),
                        'shadow_key': key,
                        'shadow_outcome': result.get('outcome'),
                        'exit_price': result.get('exit_price'),
                        'pnl_r': result.get('pnl_r'),
                        'mfe_r': result.get('mfe_r'),
                        'mae_r': result.get('mae_r'),
                        'bars_elapsed': result.get('bars_elapsed'),
                        'observation_window_bars': result.get('observation_window_bars'),
                        'shadow_exit_ts': result.get('exit_ts'),
                    }
                    self._record_utbreakout_diagnostic_event(symbol, status, event='shadow_outcome', extra=extra)
                    if isinstance(getattr(self, 'utbreakout_shadow_stats_cache', None), dict):
                        self.utbreakout_shadow_stats_cache.clear()
                    item['shadow_triple_logged'] = True
                    resolved.append(extra)

            if bool(item.get('shadow_runner_exit_enabled', cfg.get('shadow_runner_exit_enabled', False))) and not item.get('shadow_runner_logged'):
                runner_cfg = dict(item.get('runner_cfg') or {})
                runner_cfg.update({
                    'shadow_runner_max_bars': item.get('shadow_runner_max_bars', cfg.get('shadow_runner_max_bars', 48)),
                })
                runner_result = evaluate_shadow_runner_exit(
                    side=item.get('side'),
                    entry_price=item.get('entry_price'),
                    stop_loss=item.get('stop_loss'),
                    risk_distance=item.get('risk_distance'),
                    decision_ts=item.get('decision_ts'),
                    bars=closed,
                    cfg=runner_cfg,
                    max_bars=item.get('shadow_runner_max_bars', cfg.get('shadow_runner_max_bars', 48)),
                )
                if isinstance(runner_result, dict):
                    runner_extra = {
                        'code': runner_result.get('code'),
                        'shadow_key': key,
                        'runner_outcome': runner_result.get('outcome'),
                        'exit_price': runner_result.get('exit_price'),
                        'pnl_r': runner_result.get('pnl_r'),
                        'mfe_r': runner_result.get('mfe_r'),
                        'mae_r': runner_result.get('mae_r'),
                        'mfe_capture_ratio': runner_result.get('mfe_capture_ratio'),
                        'partial_filled': runner_result.get('partial_filled'),
                        'bars_elapsed': runner_result.get('bars_elapsed'),
                        'observation_window_bars': runner_result.get('observation_window_bars'),
                        'runner_exit_ts': runner_result.get('exit_ts'),
                    }
                    self._record_utbreakout_diagnostic_event(symbol, status, event='runner_shadow_outcome', extra=runner_extra)
                    if isinstance(getattr(self, 'utbreakout_runner_stats_cache', None), dict):
                        self.utbreakout_runner_stats_cache.clear()
                    item['shadow_runner_logged'] = True
                    resolved.append(runner_extra)

            runner_required = bool(item.get('shadow_runner_exit_enabled', cfg.get('shadow_runner_exit_enabled', False)))
            if item.get('shadow_triple_logged') and (not runner_required or item.get('shadow_runner_logged')):
                pending.pop(key, None)
                resolved_keys = getattr(self, 'utbreakout_shadow_resolved_keys', set())
                if not isinstance(resolved_keys, set):
                    resolved_keys = set(resolved_keys or [])
                    self.utbreakout_shadow_resolved_keys = resolved_keys
                resolved_keys.add(key)
        return resolved

    def _get_utbreakout_shadow_stats(self, symbol, side, cfg, selected_set=None):
        days = int(cfg.get('adaptive_exit_lookback_days', 14) or 14)
        set_id = selected_set.get('id') if isinstance(selected_set, dict) else None
        cache_key = f"{symbol}:{side}:{set_id}:{days}"
        now = time.time()
        cache = getattr(self, 'utbreakout_shadow_stats_cache', {})
        if isinstance(cache, dict):
            cached = cache.get(cache_key)
            if isinstance(cached, dict) and (now - float(cached.get('cached_at', 0) or 0)) < 60:
                return dict(cached.get('stats') or {})
        else:
            cache = {}
            self.utbreakout_shadow_stats_cache = cache
        events = read_utbreakout_diagnostic_events(days=days)
        min_samples = min(
            int(cfg.get('adaptive_exit_min_samples', 8) or 8),
            int(cfg.get('meta_labeling_min_samples', 12) or 12),
        )
        scopes = [
            ('symbol_side_set', {'symbol': symbol, 'side': side, 'set_id': set_id}),
            ('symbol_side', {'symbol': symbol, 'side': side, 'set_id': None}),
            ('side', {'symbol': None, 'side': side, 'set_id': None}),
            ('all', {'symbol': None, 'side': None, 'set_id': None}),
        ]
        selected_stats = None
        selected_scope = 'none'
        for scope, kwargs in scopes:
            stats = summarize_shadow_outcomes(events, **kwargs)
            if selected_stats is None or int(stats.get('sample_count') or 0) > int(selected_stats.get('sample_count') or 0):
                selected_stats = stats
                selected_scope = scope
            if int(stats.get('sample_count') or 0) >= min_samples:
                selected_stats = stats
                selected_scope = scope
                break
        selected_stats = dict(selected_stats or summarize_shadow_outcomes(events))
        selected_stats['scope'] = selected_scope
        cache[cache_key] = {'cached_at': now, 'stats': selected_stats}
        return dict(selected_stats)

    def _get_utbreakout_runner_stats(self, symbol, side, cfg, selected_set=None):
        days = int(cfg.get('adaptive_exit_lookback_days', 14) or 14)
        set_id = selected_set.get('id') if isinstance(selected_set, dict) else None
        cache_key = f"{symbol}:{side}:{set_id}:{days}:runner"
        now = time.time()
        cache = getattr(self, 'utbreakout_runner_stats_cache', {})
        if isinstance(cache, dict):
            cached = cache.get(cache_key)
            if isinstance(cached, dict) and (now - float(cached.get('cached_at', 0) or 0)) < 60:
                return dict(cached.get('stats') or {})
        else:
            cache = {}
            self.utbreakout_runner_stats_cache = cache
        events = read_utbreakout_diagnostic_events(days=days)
        min_samples = int(cfg.get('adaptive_exit_min_samples', 8) or 8)
        scopes = [
            ('symbol_side_set', {'symbol': symbol, 'side': side, 'set_id': set_id}),
            ('symbol_side', {'symbol': symbol, 'side': side, 'set_id': None}),
            ('side', {'symbol': None, 'side': side, 'set_id': None}),
            ('all', {'symbol': None, 'side': None, 'set_id': None}),
        ]
        selected_stats = None
        selected_scope = 'none'
        for scope, kwargs in scopes:
            stats = summarize_runner_outcomes(events, **kwargs)
            if selected_stats is None or int(stats.get('sample_count') or 0) > int(selected_stats.get('sample_count') or 0):
                selected_stats = stats
                selected_scope = scope
            if int(stats.get('sample_count') or 0) >= min_samples:
                selected_stats = stats
                selected_scope = scope
                break
        selected_stats = dict(selected_stats or summarize_runner_outcomes(events))
        selected_stats['scope'] = selected_scope
        cache[cache_key] = {'cached_at': now, 'stats': selected_stats}
        return dict(selected_stats)

    def _build_direction_metrics_dict(self, ohlcv_data) -> dict:
        if ohlcv_data is None:
            return {}

        import pandas as pd

        try:
            if hasattr(ohlcv_data, "empty") and ohlcv_data.empty:
                return {}
            if hasattr(ohlcv_data, "__len__") and len(ohlcv_data) < 50:
                return {}
        except Exception:
            return {}

        if isinstance(ohlcv_data, pd.DataFrame):
            df = ohlcv_data.copy()
            if not {'timestamp', 'open', 'high', 'low', 'close', 'volume'}.issubset(df.columns):
                df = df.iloc[:, :6].copy()
                df.columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
        else:
            df = pd.DataFrame(ohlcv_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df = df.dropna(subset=['close']).reset_index(drop=True)
        if len(df) < 50:
            return {}
        close_series = df['close'].astype(float)
        ema20 = close_series.ewm(span=20, adjust=False).mean()
        ema50 = close_series.ewm(span=50, adjust=False).mean()

        ema50_last = float(ema50.iloc[-1])
        ema50_prev = float(ema50.iloc[-2]) if len(ema50) > 1 else ema50_last
        ema_slope = (ema50_last - ema50_prev) / max(ema50_prev, 1e-9) * 100.0

        return {
            "close": float(close_series.iloc[-1]),
            "ema20": float(ema20.iloc[-1]),
            "ema50": float(ema50.iloc[-1]),
            "ema_slope": ema_slope,
        }

    def _build_utbreakout_strategy_adaptation(self, symbol, side, cfg, selected_set, filter_values):
        cfg = apply_profit_opportunity_effective_overrides(dict(cfg or {}))
        stats = self._get_utbreakout_shadow_stats(symbol, side, cfg, selected_set)
        runner_stats = self._get_utbreakout_runner_stats(symbol, side, cfg, selected_set)
        trend_health = self._build_utbreakout_trend_health(side, cfg, filter_values)
        strategy_quality = self._build_utbreakout_strategy_quality(side, cfg, filter_values)
        adaptation = build_strategy_adaptation(
            cfg,
            stats,
            side=side,
            atr_pct=(filter_values or {}).get('atr_pct'),
            runner_stats=runner_stats,
            trend_health=trend_health,
            strategy_quality=strategy_quality,
        )
        return adaptation

    def _record_utbot_filtered_breakout_failure(self, symbol, side, candle_ts, reason):
        side = str(side or '').lower()
        if side not in {'long', 'short'}:
            return
        symbol_failures = self.utbot_filtered_breakout_failures.setdefault(symbol, {})
        failures = list(symbol_failures.get(side, []))
        failures.append({'ts': int(candle_ts or 0), 'reason': str(reason or '')})
        symbol_failures[side] = failures[-10:]

    def _store_utbot_filtered_breakout_status(self, symbol, status):
        status = dict(status or {})
        self.last_utbot_filtered_breakout_status[symbol] = status
        signal_ts = int(status.get('ut_signal_ts') or 0)
        feed_ts = int(status.get('feed_last_ts') or 0)
        self._update_stateful_diag(
            symbol,
            stage=status.get('stage', 'evaluate'),
            strategy='UTBOT_FILTERED_BREAKOUT_V1',
            raw_state=(status.get('candidate_side') or 'none'),
            raw_signal=(status.get('candidate_signal') or 'none'),
            entry_sig=(status.get('accepted_side') or 'none'),
            pos_side=status.get('pos_side', 'UNKNOWN'),
            ut_key=status.get('utbot_key_value'),
            ut_atr=status.get('utbot_atr_period'),
            ut_ha='ON' if status.get('use_heikin_ashi') else 'OFF',
            src=status.get('ut_curr_src'),
            stop=status.get('ut_curr_stop'),
            tf_used=status.get('entry_timeframe', '15m'),
            signal_ts=signal_ts or None,
            signal_ts_human=datetime.fromtimestamp(signal_ts / 1000).strftime('%m-%d %H:%M') if signal_ts else None,
            feed_last_ts=feed_ts or None,
            feed_last_ts_human=datetime.fromtimestamp(feed_ts / 1000).strftime('%m-%d %H:%M') if feed_ts else None,
            utbreakout_reject_code=status.get('reject_code'),
            utbreakout_reason=status.get('reason'),
            utbreakout_htf=status.get('htf_summary'),
            utbreakout_metrics=status.get('metric_summary'),
            utbreakout_risk=status.get('risk_summary'),
            utbreakout_adaptive_tf=status.get('adaptive_timeframe_summary'),
            note=status.get('reject_code') or status.get('accepted_code') or status.get('adaptive_timeframe_summary') or status.get('reason')
        )

    def _record_utbreakout_diagnostic_event(self, symbol, status, event=None, extra=None):
        try:
            status = dict(status or {})
            side = status.get('accepted_side') or status.get('candidate_side') or status.get('candidate_signal')
            side = str(side or '').lower()
            if side not in {'long', 'short'}:
                return
            code = (
                status.get('accepted_code')
                or status.get('reject_code')
                or (extra or {}).get('code')
                or 'CANDIDATE'
            )
            if event is None:
                if code == 'ACCEPTED_ENTRY':
                    event = 'accepted'
                elif str(code).startswith('REJECTED_'):
                    event = 'rejected'
                else:
                    event = 'candidate'
            auto_scores = status.get('auto_scores') if isinstance(status.get('auto_scores'), dict) else {}
            protection_status = self.last_protection_order_status.get(symbol)

            payload = {
                'ts': datetime.now(timezone.utc).isoformat(),
                'event': event,
                'symbol': symbol,
                'side': side,
                'code': code,
                'reason': status.get('reason'),
                'candidate_type': status.get('candidate_type'),
                'fresh_signal': status.get('fresh_signal'),
                'ut_bias_side': status.get('ut_bias_side'),
                'selection_mode': status.get('selection_mode'),
                'auto_selected_set_id': status.get('auto_selected_set_id'),
                'auto_selected_set_name': status.get('auto_selected_set_name'),
                'auto_selected_set_family': status.get('auto_selected_set_family'),
                'auto_selection_reason': status.get('auto_selection_reason'),
                'auto_stable_selection_detail': status.get('auto_stable_selection_detail'),
                'auto_scores': status.get('auto_scores'),
                'auto_top3': auto_scores.get('auto_top3'),
                'auto_score_margin': _safe_float_or_none(auto_scores.get('auto_score_margin')),
                'auto_confidence': auto_scores.get('auto_confidence'),
                'set_filters': status.get('set_filters'),
                'entry_timeframe': status.get('entry_timeframe'),
                'htf_timeframe': status.get('htf_timeframe'),
                'adaptive_timeframe_enabled': status.get('adaptive_timeframe_enabled'),
                'adaptive_selected_tf': status.get('adaptive_selected_tf'),
                'adaptive_previous_tf': status.get('adaptive_previous_tf'),
                'adaptive_timeframe_summary': status.get('adaptive_timeframe_summary'),
                'adaptive_top3': status.get('adaptive_top3'),
                'decision_candle_ts': status.get('decision_candle_ts'),
                'ut_signal_ts': status.get('ut_signal_ts'),
                'utbot_key_value': _safe_float_or_none(status.get('utbot_key_value')),
                'utbot_atr_period': status.get('utbot_atr_period'),
                'entry_price': _safe_float_or_none(status.get('entry_price')),
                'rsi': _safe_float_or_none(status.get('rsi')),
                'adx': _safe_float_or_none(status.get('adx')),
                'atr_pct': _safe_float_or_none(status.get('atr_pct')),
                'ema_near_pct': _safe_float_or_none(status.get('ema_near_pct')),
                'donchian_width_pct': _safe_float_or_none(status.get('donchian_width_pct')),
                'donchian_high_prev': _safe_float_or_none(status.get('donchian_high_prev')),
                'donchian_low_prev': _safe_float_or_none(status.get('donchian_low_prev')),
                'bb_width_percentile': _safe_float_or_none(status.get('bb_width_percentile')),
                'keltner_squeeze_on': status.get('keltner_squeeze_on'),
                'squeeze_release_state': status.get('squeeze_release_state'),
                'htf_summary': status.get('htf_summary'),
                'metric_summary': status.get('metric_summary'),
                'risk_summary': status.get('risk_summary'),
                'funding_rate': _safe_float_or_none(status.get('funding_rate')),
                'next_funding_time': status.get('next_funding_time'),
                'open_interest': _safe_float_or_none(status.get('open_interest')),
                'open_interest_delta_pct': _safe_float_or_none(status.get('open_interest_delta_pct')),
                'open_interest_delta_z': _safe_float_or_none(status.get('open_interest_delta_z')),
                'open_interest_acceleration': _safe_float_or_none(status.get('open_interest_acceleration')),
                'open_interest_hist_samples': status.get('open_interest_hist_samples'),
                'mark_price': _safe_float_or_none(status.get('mark_price')),
                'basis_pct': _safe_float_or_none(status.get('basis_pct')),
                'taker_buy_sell_ratio': _safe_float_or_none(status.get('taker_buy_sell_ratio')),
                'long_short_ratio': _safe_float_or_none(status.get('long_short_ratio')),
                'orderbook_imbalance_pct': _safe_float_or_none(status.get('orderbook_imbalance_pct')),
                'rolling_orderbook_imbalance_pct': _safe_float_or_none(status.get('rolling_orderbook_imbalance_pct')),
                'rolling_orderbook_imbalance_delta': _safe_float_or_none(status.get('rolling_orderbook_imbalance_delta')),
                'rolling_ofi_score': _safe_float_or_none(status.get('rolling_ofi_score')),
                'rolling_ofi_samples': status.get('rolling_ofi_samples'),
                'futures_spread_pct': _safe_float_or_none(status.get('futures_spread_pct')),
                'bid_depth_usdt': _safe_float_or_none(status.get('bid_depth_usdt')),
                'ask_depth_usdt': _safe_float_or_none(status.get('ask_depth_usdt')),
                'prediction_up_probability': _safe_float_or_none(status.get('prediction_up_probability')),
                'prediction_edge': _safe_float_or_none(status.get('prediction_edge')),
                'prediction_score': _safe_float_or_none(status.get('prediction_score')),
                'prediction_title': status.get('prediction_title'),
                'bias_continuation_summary': status.get('bias_continuation_summary'),
                'bias_continuation_risk_multiplier': _safe_float_or_none(status.get('bias_continuation_risk_multiplier')),
                'bias_continuation_signal_age_candles': _safe_float_or_none(status.get('bias_continuation_signal_age_candles')),
                'bias_continuation_extension_atr': _safe_float_or_none(status.get('bias_continuation_extension_atr')),
                'bias_continuation_selected_tf_score': _safe_float_or_none(status.get('bias_continuation_selected_tf_score')),
                'market_quality_summary': status.get('market_quality_summary'),
                'market_quality_risk_multiplier': _safe_float_or_none(status.get('market_quality_risk_multiplier')),
                'market_regime_summary': status.get('market_regime_summary'),
                'selector_quality_summary': status.get('selector_quality_summary'),
                'selector_quality_score': _safe_float_or_none(status.get('selector_quality_score')),
                'selector_quality_risk_multiplier': _safe_float_or_none(status.get('selector_quality_risk_multiplier')),
                'feature_score_value': _safe_float_or_none(status.get('feature_score_value')),
                'feature_score_components': status.get('feature_score_components'),
                'feature_score_reason': status.get('feature_score_reason'),
                'feature_score_risk_multiplier': _safe_float_or_none(status.get('feature_score_risk_multiplier')),
                'strategy_adaptation_summary': status.get('strategy_adaptation_summary'),
                'strategy_adaptive_risk_multiplier': _safe_float_or_none(status.get('strategy_adaptive_risk_multiplier')),
                'volatility_risk_multiplier': _safe_float_or_none(status.get('volatility_risk_multiplier')),
                'meta_label_risk_multiplier': _safe_float_or_none(status.get('meta_label_risk_multiplier')),
                'trend_health_risk_multiplier': _safe_float_or_none(status.get('trend_health_risk_multiplier')),
                'trend_health_score': _safe_float_or_none(status.get('trend_health_score')),
                'trend_health_state': status.get('trend_health_state'),
                'trend_health_summary': status.get('trend_health_summary'),
                'strategy_quality_risk_multiplier': _safe_float_or_none(status.get('strategy_quality_risk_multiplier')),
                'strategy_quality_score': _safe_float_or_none(status.get('strategy_quality_score')),
                'strategy_quality_state': status.get('strategy_quality_state'),
                'strategy_quality_summary': status.get('strategy_quality_summary'),
                'quality_score_v2_score': _safe_float_or_none(status.get('quality_score_v2_score')),
                'quality_score_v2_state': status.get('quality_score_v2_state'),
                'quality_score_v2_summary': status.get('quality_score_v2_summary'),
                'quality_score_v2_risk_multiplier': _safe_float_or_none(status.get('quality_score_v2_risk_multiplier')),
                'dynamic_take_profit_summary': status.get('dynamic_take_profit_summary'),
                'dynamic_tp2_r_multiple': _safe_float_or_none(status.get('dynamic_tp2_r_multiple')),
                'adaptive_exit_summary': status.get('adaptive_exit_summary'),
                'shadow_sample_count': status.get('shadow_sample_count'),
                'shadow_win_rate': _safe_float_or_none(status.get('shadow_win_rate')),
                'shadow_avg_pnl_r': _safe_float_or_none(status.get('shadow_avg_pnl_r')),
                'runner_sample_count': status.get('runner_sample_count'),
                'runner_avg_mfe_capture_ratio': _safe_float_or_none(status.get('runner_avg_mfe_capture_ratio')),
                'runner_avg_pnl_r': _safe_float_or_none(status.get('runner_avg_pnl_r')),
                'raw_final_risk_multiplier_before_position_sizing': _safe_float_or_none(
                    status.get('raw_final_risk_multiplier_before_position_sizing')
                ),
                'position_sizing_risk_multiplier': _safe_float_or_none(
                    status.get('position_sizing_risk_multiplier')
                ),
                'position_sizing_components': status.get('position_sizing_components'),
                'position_sizing_reasons': status.get('position_sizing_reasons'),
                'raw_final_risk_multiplier': _safe_float_or_none(status.get('raw_final_risk_multiplier')),
                'final_risk_multiplier': _safe_float_or_none(status.get('final_risk_multiplier')),
                'decision_trace': status.get('decision_trace'),
                'protection_status': protection_status
            }
            plan = status.get('entry_plan')
            if isinstance(plan, dict):
                if not payload.get('decision_candle_ts'):
                    payload['decision_candle_ts'] = plan.get('decision_candle_ts')
                payload.update({
                    'risk_usdt': _safe_float_or_none(plan.get('risk_usdt')),
                    'risk_distance': _safe_float_or_none(plan.get('risk_distance')),
                    'risk_distance_pct': _safe_float_or_none(plan.get('risk_distance_pct')),
                    'stop_loss': _safe_float_or_none(plan.get('stop_loss')),
                    'take_profit': _safe_float_or_none(plan.get('take_profit')),
                    'take_profit_pct': _safe_float_or_none(plan.get('take_profit_pct')),
                    'planned_qty': _safe_float_or_none(plan.get('qty')),
                    'planned_notional': _safe_float_or_none(plan.get('planned_notional')),
                    'planned_margin': _safe_float_or_none(plan.get('planned_margin')),
                    'expected_profit_usdt': _safe_float_or_none(plan.get('expected_profit_usdt')),
                    'leverage': _safe_float_or_none(plan.get('leverage')),
                    'rr_multiple': _safe_float_or_none(plan.get('rr_multiple')),
                    'fixed_take_profit_enabled': plan.get('fixed_take_profit_enabled'),
                    'partial_take_profit_enabled': plan.get('partial_take_profit_enabled'),
                    'partial_take_profit_r_multiple': _safe_float_or_none(plan.get('partial_take_profit_r_multiple')),
                    'partial_take_profit_ratio': _safe_float_or_none(plan.get('partial_take_profit_ratio')),
                    'second_take_profit_enabled': plan.get('second_take_profit_enabled'),
                    'second_take_profit_r_multiple': _safe_float_or_none(plan.get('second_take_profit_r_multiple')),
                    'second_take_profit_ratio': _safe_float_or_none(plan.get('second_take_profit_ratio')),
                    'atr_trailing_multiplier': _safe_float_or_none(plan.get('atr_trailing_multiplier')),
                    'atr_trailing_activation_r': _safe_float_or_none(plan.get('atr_trailing_activation_r')),
                    'bias_continuation_summary': plan.get('bias_continuation_summary'),
                    'bias_continuation_risk_multiplier': _safe_float_or_none(plan.get('bias_continuation_risk_multiplier')),
                    'bias_continuation_signal_age_candles': _safe_float_or_none(plan.get('bias_continuation_signal_age_candles')),
                    'bias_continuation_extension_atr': _safe_float_or_none(plan.get('bias_continuation_extension_atr')),
                    'bias_continuation_selected_tf_score': _safe_float_or_none(plan.get('bias_continuation_selected_tf_score')),
                    'strategy_adaptive_risk_multiplier': _safe_float_or_none(plan.get('strategy_adaptive_risk_multiplier')),
                    'selector_quality_risk_multiplier': _safe_float_or_none(plan.get('selector_quality_risk_multiplier')),
                    'selector_quality_score': _safe_float_or_none(plan.get('selector_quality_score')),
                    'selector_quality_summary': plan.get('selector_quality_summary'),
                    'feature_score_value': _safe_float_or_none(plan.get('feature_score_value')),
                    'feature_score_components': plan.get('feature_score_components'),
                    'feature_score_reason': plan.get('feature_score_reason'),
                    'feature_score_risk_multiplier': _safe_float_or_none(plan.get('feature_score_risk_multiplier')),
                    'volatility_risk_multiplier': _safe_float_or_none(plan.get('volatility_risk_multiplier')),
                    'meta_label_risk_multiplier': _safe_float_or_none(plan.get('meta_label_risk_multiplier')),
                    'trend_health_risk_multiplier': _safe_float_or_none(plan.get('trend_health_risk_multiplier')),
                    'trend_health_score': _safe_float_or_none(plan.get('trend_health_score')),
                    'trend_health_summary': plan.get('trend_health_summary'),
                    'strategy_quality_risk_multiplier': _safe_float_or_none(plan.get('strategy_quality_risk_multiplier')),
                    'strategy_quality_score': _safe_float_or_none(plan.get('strategy_quality_score')),
                    'strategy_quality_summary': plan.get('strategy_quality_summary'),
                    'quality_score_v2_score': _safe_float_or_none(plan.get('quality_score_v2_score')),
                    'quality_score_v2_state': plan.get('quality_score_v2_state'),
                    'quality_score_v2_summary': plan.get('quality_score_v2_summary'),
                    'quality_score_v2_risk_multiplier': _safe_float_or_none(plan.get('quality_score_v2_risk_multiplier')),
                    'dynamic_take_profit_summary': plan.get('dynamic_take_profit_summary'),
                    'dynamic_tp2_r_multiple': _safe_float_or_none(plan.get('dynamic_tp2_r_multiple')),
                    'tp1_breakeven_enabled': plan.get('tp1_breakeven_enabled'),
                    'adaptive_exit_summary': plan.get('adaptive_exit_summary'),
                    'shadow_sample_count': plan.get('shadow_sample_count'),
                    'shadow_win_rate': _safe_float_or_none(plan.get('shadow_win_rate')),
                    'shadow_avg_pnl_r': _safe_float_or_none(plan.get('shadow_avg_pnl_r')),
                    'runner_sample_count': plan.get('runner_sample_count'),
                    'runner_avg_mfe_capture_ratio': _safe_float_or_none(plan.get('runner_avg_mfe_capture_ratio')),
                    'runner_avg_pnl_r': _safe_float_or_none(plan.get('runner_avg_pnl_r'))
                })
            if isinstance(extra, dict):
                for key, value in extra.items():
                    if key not in payload:
                        payload[key] = value
            clean_payload = {k: v for k, v in payload.items() if v is not None}
            utbreakout_diag_logger.info(json.dumps(clean_payload, ensure_ascii=False, separators=(',', ':')))
        except Exception as e:
            logger.debug(f"UT breakout diagnostic log write failed: {e}")
