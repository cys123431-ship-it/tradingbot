"""Trailing exits, pyramiding, TP/SL placement, and position closing."""

from __future__ import annotations

import math

from utbreakout.adaptive_breakout_trend import small_account_short_entry_blocked
from utbreakout.convex_rotation import evaluate_convex_rotation_exit


def _mark_aware_excursion_bounds(
    state,
    *,
    entry_price,
    closed_high,
    closed_low,
    current_mark=None,
):
    """Blend completed-candle extrema with the latest polled exchange mark."""

    highest = max(
        float((state or {}).get('highest_price') or entry_price),
        float(closed_high),
    )
    lowest = min(
        float((state or {}).get('lowest_price') or entry_price),
        float(closed_low),
    )
    try:
        mark = float(current_mark)
    except (TypeError, ValueError):
        mark = None
    if mark is not None and math.isfinite(mark) and mark > 0:
        highest = max(highest, mark)
        lowest = min(lowest, mark)
    return highest, lowest


class SignalExitMixin:
    async def _maybe_apply_adaptive_trend_pyramiding(self, symbol, pos, df, cfg):
        """Add only to a profitable standalone trend while preserving stop risk."""

        if self.is_upbit_mode() or not pos:
            return None
        side = str(pos.get('side', '') or '').lower()
        if side not in {'long', 'short'}:
            return None
        state = self._get_utbreakout_trailing_state(symbol)
        if not isinstance(state, dict):
            return None
        if (
            str(state.get('strategy') or '').lower()
            != ADAPTIVE_BREAKOUT_TREND_STRATEGY
            or not bool(state.get('adaptive_trend_pyramid_enabled', False))
        ):
            return None
        if small_account_short_entry_blocked(
            side,
            small_account_active=bool(
                state.get('small_account_aggressive_active', False)
            ),
            config=state,
        ):
            return {
                'status': 'DISABLED',
                'reason': (
                    'small-account aggressive profile is long-only; '
                    'SHORT position additions disabled'
                ),
            }

        triggers = tuple(
            float(value)
            for value in (
                state.get('adaptive_trend_pyramid_trigger_r')
                or (0.50, 1.00, 1.50)
            )
        )
        targets = tuple(
            float(value)
            for value in (
                state.get('adaptive_trend_pyramid_target_fractions')
                or (0.80, 0.90, 1.00)
            )
        )
        add_count = max(
            0,
            int(state.get('adaptive_trend_pyramid_add_count', 0) or 0),
        )
        if add_count >= min(len(triggers), len(targets)):
            return {'status': 'COMPLETE', 'reason': 'adaptive trend pyramid complete'}

        current_qty = abs(
            float(self._position_signed_contracts(pos) or pos.get('contracts', 0) or 0)
        )
        current_price = _safe_float_or_none(pos.get('markPrice'))
        if current_price is None and df is not None and len(df) >= 2:
            current_price = _safe_float_or_none(df.iloc[-2].get('close'))
        initial_entry = (
            _safe_float_or_none(state.get('adaptive_trend_initial_entry_price'))
            or _safe_float_or_none(state.get('entry_price'))
            or _safe_float_or_none(pos.get('entryPrice'))
        )
        initial_risk = (
            _safe_float_or_none(state.get('adaptive_trend_initial_risk_distance'))
            or _safe_float_or_none(state.get('risk_distance'))
        )
        target_qty = _safe_float_or_none(state.get('adaptive_trend_target_qty'))
        if (
            current_qty <= 0
            or current_price is None
            or current_price <= 0
            or initial_entry is None
            or initial_entry <= 0
            or initial_risk is None
            or initial_risk <= 0
            or target_qty is None
            or target_qty <= current_qty
        ):
            return None

        direction = 1.0 if side == 'long' else -1.0
        pnl_r = direction * (current_price - initial_entry) / initial_risk
        trigger_r = triggers[add_count]
        if pnl_r < trigger_r:
            return {
                'status': 'WAITING',
                'reason': f'adaptive trend {pnl_r:.2f}R below {trigger_r:.2f}R',
                'pnl_r': pnl_r,
            }

        desired_total_qty = target_qty * max(0.0, min(1.0, targets[add_count]))
        add_qty = max(0.0, desired_total_qty - current_qty)
        min_amount_resolver = getattr(self, '_get_min_amount_for_symbol', None)
        try:
            min_amount = (
                max(0.0, float(min_amount_resolver(symbol) or 0.0))
                if callable(min_amount_resolver)
                else 0.0
            )
        except Exception:
            min_amount = 0.0

        # A filled position can differ from its fractional pyramid target by a
        # sub-step remainder after exchange precision is applied. Treat that
        # remainder as the stage target being reached; otherwise every candle
        # retries an order Binance can never accept.
        if add_qty <= 1e-12 or (
            min_amount > 0 and add_qty + 1e-12 < min_amount
        ):
            next_add_count = add_count + 1
            state.update({
                'adaptive_trend_pyramid_add_count': next_add_count,
                'last_update_ts': datetime.now(timezone.utc).isoformat(),
            })
            self._set_utbreakout_trailing_state(symbol, state)
            complete = next_add_count >= min(len(triggers), len(targets))
            return {
                'status': 'COMPLETE' if complete else 'STAGE_COMPLETE',
                'reason': (
                    'adaptive trend pyramid target already reached or within '
                    f'exchange minimum amount: residual={add_qty:.12f} '
                    f'min={min_amount:.12f}'
                ),
                'residual_qty': add_qty,
                'min_amount': min_amount,
            }
        try:
            total_equity, free_balance, _ = await self.get_balance_info()
        except Exception:
            total_equity, free_balance = 0.0, 0.0
        leverage = max(1, int(float(state.get('leverage', 5) or 5)))
        if free_balance and free_balance > 0:
            margin_qty_cap = float(free_balance) * leverage * 0.98 / current_price
            add_qty = min(add_qty, margin_qty_cap)
        if min_amount > 0 and 0 < add_qty + 1e-12 < min_amount:
            return {
                'status': 'BLOCKED',
                'reason': (
                    'adaptive trend free-margin add quantity below exchange '
                    f'minimum: qty={add_qty:.12f} min={min_amount:.12f}'
                ),
                'add_qty': add_qty,
                'min_amount': min_amount,
            }
        add_qty = float(self.safe_amount(symbol, add_qty))
        if add_qty <= 0 or (min_amount > 0 and add_qty + 1e-12 < min_amount):
            return {'status': 'BLOCKED', 'reason': 'adaptive trend add quantity rounded to zero'}

        # Keep the original entry stop in place while the add order is being
        # filled.  The combined, fee-aware break-even can only be calculated
        # from the exchange's post-fill average entry.
        pre_add_stop = initial_entry
        current_stop = await self._current_stop_loss_price(symbol, state)
        if current_stop is not None:
            current_stop = float(current_stop)
            tighter_stop_is_live = (
                initial_entry <= current_stop < current_price
                if side == 'long'
                else initial_entry >= current_stop > current_price
            )
            if tighter_stop_is_live:
                pre_add_stop = current_stop
        if bool(state.get('small_account_aggressive_active', False)):
            average_entry = (
                _safe_float_or_none(pos.get('entryPrice'))
                or _safe_float_or_none(state.get('entry_price'))
                or initial_entry
            )
            existing_stop_risk = (
                max(0.0, average_entry - pre_add_stop) * current_qty
                if side == 'long'
                else max(0.0, pre_add_stop - average_entry) * current_qty
            )
            added_stop_risk = (
                max(0.0, current_price - pre_add_stop) * add_qty
                if side == 'long'
                else max(0.0, pre_add_stop - current_price) * add_qty
            )
            cost_buffer_percent = max(
                0.0,
                float(
                    state.get('small_account_aggressive_cost_buffer_percent', 0.20)
                    or 0.20
                ),
            )
            projected_notional = (
                current_qty * average_entry + add_qty * current_price
            )
            projected_loss = (
                existing_stop_risk
                + added_stop_risk
                + projected_notional * cost_buffer_percent / 100.0
            )
            max_loss_usdt = max(
                0.0,
                float(state.get('small_account_aggressive_max_loss_usdt', 0.0) or 0.0),
            )
            effective_loss_cap = max_loss_usdt
            if effective_loss_cap <= 0.0 or projected_loss > effective_loss_cap + 1e-9:
                return {
                    'status': 'BLOCKED',
                    'reason': (
                        f'adaptive trend small-account add risk {projected_loss:.2f} '
                        f'exceeds remaining cap {effective_loss_cap:.2f}'
                    ),
                    'projected_loss_usdt': projected_loss,
                    'effective_loss_cap_usdt': effective_loss_cap,
                }
        stop_needs_tightening = bool(
            current_stop is None
            or (side == 'long' and current_stop < pre_add_stop)
            or (side == 'short' and current_stop > pre_add_stop)
        )
        if stop_needs_tightening:
            replacement = await self._replace_stop_loss_order(
                symbol,
                pos,
                pre_add_stop,
                reason='Adaptive Trend pyramid breakeven',
            )
            if not replacement:
                return {'status': 'BLOCKED', 'reason': 'adaptive trend breakeven SL replacement failed'}
            state.update({
                'active': True,
                'breakeven_armed': True,
                'last_stop_price': pre_add_stop,
                'last_update_ts': datetime.now(timezone.utc).isoformat(),
            })
            self._set_utbreakout_trailing_state(symbol, state)

        pre_add_audit = await self._audit_protection_orders(
            symbol,
            pos=pos,
            expected_tp=bool(self._planned_tp_orders_from_state(symbol, state)),
            expected_sl=True,
            planned_tp_orders=self._planned_tp_orders_from_state(symbol, state),
            alert=True,
        )
        if not pre_add_audit.get('sl_present'):
            return {
                'status': 'BLOCKED',
                'reason': 'adaptive trend breakeven SL audit failed',
                'audit': pre_add_audit,
            }

        daily_loss_exempt = bool(state.get('small_account_aggressive_active', False))
        entry_blocker = _crypto_entry_block_reason(
            self,
            symbol,
            ignore_daily_loss_lock=daily_loss_exempt,
        )
        if entry_blocker and str(entry_blocker).startswith('PROTECTED:'):
            # A protected same-symbol entry is the required parent of a
            # position add. The execution gateway rechecks all other blocking
            # states with for_position_add=True under its global lock.
            entry_blocker = None
        if entry_blocker:
            return {'status': 'BLOCKED', 'reason': entry_blocker}
        await self.ensure_market_settings(symbol, leverage=leverage, position=pos)
        liquidation_preflight = await self._preflight_liquidation_safety(
            symbol,
            side,
            current_price,
            pre_add_stop,
            leverage,
            current_qty + add_qty,
            cfg,
        )
        if not liquidation_preflight.get('valid'):
            return {
                'status': 'BLOCKED',
                'reason': liquidation_preflight.get('reason') or liquidation_preflight.get('status'),
            }

        _ensure_trading_safety_runtime(self)
        stage = add_count + 1
        add_submission = await self.crypto_execution.submit_position_add(
            strategy='ADAPTIVE_TREND_PYRAMID',
            symbol=symbol,
            side=side,
            signal_timestamp=state.get('last_bar_ts') or int(time.time()),
            qty=add_qty,
            stage=str(stage),
            daily_loss_exempt=daily_loss_exempt,
        )
        if not add_submission.accepted:
            return {
                'status': add_submission.state,
                'reason': add_submission.error or 'adaptive trend add order not accepted',
                'client_order_id': add_submission.client_order_id,
            }

        self.position_cache = None
        self.position_cache_time = 0
        _, new_pos = await self._fetch_position_with_liquidation(
            symbol,
            add_submission.position,
        )
        if not new_pos or str(new_pos.get('side', '') or '').lower() != side:
            self._set_crypto_entry_lock(f'SUBMITTED_UNKNOWN:{add_submission.client_order_id}')
            return {'status': 'ORDER_SENT_POSITION_MISSING', 'order': add_submission.order or {}}
        total_qty = abs(
            float(self._position_signed_contracts(new_pos) or new_pos.get('contracts', 0) or 0)
        )
        avg_entry = _safe_float_or_none(new_pos.get('entryPrice')) or current_price
        post_fill_mark = (
            _safe_float_or_none(new_pos.get('markPrice'))
            or _safe_float_or_none(new_pos.get('mark_price'))
            or current_price
        )
        # A winner add can leave the already-protective stop beyond the new
        # combined average (profit-locking).  The only unsafe geometry is a
        # stop that is no longer live relative to the current mark.  Treating
        # a profitable stop as invalid used to abandon post-fill protection
        # refresh/state registration and could later recreate an already
        # filled TP from a stale quantity baseline.
        valid_live_stop_geometry = bool(
            total_qty > 0
            and pre_add_stop > 0
            and post_fill_mark is not None
            and post_fill_mark > 0
            and (
                (side == 'long' and pre_add_stop < post_fill_mark)
                or (side == 'short' and pre_add_stop > post_fill_mark)
            )
        )
        if not valid_live_stop_geometry:
            _mark_crypto_entry_state(
                self,
                add_submission.client_order_id,
                OrderState.FILLED_UNPROTECTED,
            )
            self._set_crypto_entry_lock(
                f'FILLED_UNPROTECTED:{add_submission.client_order_id}'
            )
            close_status = await self._fail_closed_unprotected_position(
                symbol,
                reason=(
                    'adaptive trend post-fill stop is not live: '
                    f'side={side} stop={pre_add_stop} mark={post_fill_mark}'
                ),
                status_code='PYRAMID_POST_FILL_STOP_NOT_LIVE',
                expected_tp=bool(
                    self._planned_tp_orders_from_state(symbol, state)
                ),
                emergency_close=True,
            )
            return {
                'status': 'FILLED_UNPROTECTED',
                'reason': 'adaptive trend post-fill stop is not live',
                'close_status': close_status,
            }

        actual_liquidation = await self._verify_actual_liquidation_safety(
            symbol,
            side,
            pre_add_stop,
            new_pos,
            cfg,
            add_submission.client_order_id,
        )
        if not actual_liquidation.get('valid'):
            return {
                'status': actual_liquidation.get('status'),
                'reason': 'adaptive trend post-fill liquidation safety failed',
                'close_status': actual_liquidation.get('close_status'),
            }
        new_pos = actual_liquidation.get('position') or new_pos
        pre_add_risk_distance = abs(avg_entry - pre_add_stop)
        partial_r_original = max(
            0.1,
            float(state.get('adaptive_trend_partial_r_multiple', 2.0) or 2.0),
        )
        target_price = initial_entry + direction * initial_risk * partial_r_original
        target_distance = direction * (target_price - avg_entry)
        partial_ratio = max(
            0.0,
            min(0.20, float(state.get('adaptive_trend_partial_ratio', 0.15) or 0.15)),
        )
        tp1_already_filled = bool(state.get('tp1_filled', False)) or any(
            bool(item.get('filled', False))
            and _normalize_tp_plan_label(
                item.get('tp_label') or item.get('tp_name') or item.get('label')
            ) == 'TP1'
            for item in (
                state.get('planned_tp_orders')
                or state.get('tp_orders')
                or []
            )
            if isinstance(item, dict)
        )
        tp_targets = []
        if target_distance > 0 and partial_ratio > 0 and not tp1_already_filled:
            tp_targets.append({
                'label': 'TP1',
                'kind': 'tp1',
                'distance': target_distance,
                'qty_ratio': partial_ratio,
                'target_r': target_distance / max(pre_add_risk_distance, 1e-9),
            })
        await self._place_tp_sl_orders(
            symbol,
            side,
            avg_entry,
            total_qty,
            sl_distance=pre_add_risk_distance,
            tp_targets=tp_targets,
            preserve_runner_qty=True,
            sl_price_override=pre_add_stop,
        )

        # The pre-add stop protects the order while it fills, but it is not
        # break-even for the enlarged position.  Move only the SL (leaving the
        # new TP orders intact) to the actual combined average plus a bounded
        # round-trip cost allowance.
        default_cost_buffer = (
            state.get('small_account_aggressive_cost_buffer_percent', 0.20)
            if bool(state.get('small_account_aggressive_active', False))
            else (cfg or {}).get('adaptive_trend_breakeven_cost_buffer_percent', 0.12)
        )
        cost_buffer_percent = max(
            0.0,
            min(0.50, float(default_cost_buffer or 0.0)),
        )
        cost_multiplier = cost_buffer_percent / 100.0
        raw_cost_break_even = avg_entry * (
            1.0 + cost_multiplier if side == 'long' else 1.0 - cost_multiplier
        )
        minimum_live_gap_percent = max(
            0.10,
            min(
                1.00,
                float(
                    (cfg or {}).get(
                        'adaptive_trend_breakeven_min_live_gap_percent',
                        0.30,
                    )
                    or 0.30
                ),
            ),
        )
        live_gap_multiplier = minimum_live_gap_percent / 100.0
        if side == 'long':
            live_safe_boundary = post_fill_mark * (1.0 - live_gap_multiplier)
            desired_post_add_stop = max(
                pre_add_stop,
                min(raw_cost_break_even, live_safe_boundary),
            )
        else:
            live_safe_boundary = post_fill_mark * (1.0 + live_gap_multiplier)
            desired_post_add_stop = min(
                pre_add_stop,
                max(raw_cost_break_even, live_safe_boundary),
            )
        desired_stop_is_live = bool(
            post_fill_mark
            and (
                (
                    side == 'long'
                    and avg_entry <= desired_post_add_stop < post_fill_mark
                )
                or (
                    side == 'short'
                    and avg_entry >= desired_post_add_stop > post_fill_mark
                )
            )
        )
        applied_post_add_stop = pre_add_stop
        stop_tolerance = max(abs(pre_add_stop) * 1e-9, 1e-12)
        desired_stop_is_tighter = (
            desired_post_add_stop > pre_add_stop + stop_tolerance
            if side == 'long'
            else desired_post_add_stop < pre_add_stop - stop_tolerance
        )
        if desired_stop_is_live and desired_stop_is_tighter:
            replacement = await self._replace_stop_loss_order(
                symbol,
                new_pos,
                desired_post_add_stop,
                reason='Adaptive Trend pyramid combined fee-aware breakeven',
            )
            if replacement:
                applied_post_add_stop = desired_post_add_stop
            else:
                # A fast move can cross the tighter target between the mark
                # snapshot and Binance acknowledgement.  Immediately restore
                # the still-valid pre-add stop instead of waiting for a later
                # audit with the enlarged position unprotected.
                fallback_replacement = await self._replace_stop_loss_order(
                    symbol,
                    new_pos,
                    pre_add_stop,
                    reason='Adaptive Trend pyramid restore pre-add stop',
                )
                if fallback_replacement:
                    applied_post_add_stop = pre_add_stop

        post_add_audit = await self._audit_protection_orders(
            symbol,
            pos=new_pos,
            expected_tp=bool(tp_targets),
            expected_sl=True,
            alert=True,
        )
        if not (
            post_add_audit.get('fetch_ok')
            and post_add_audit.get('sl_present')
            and not post_add_audit.get('sl_qty_mismatch')
        ):
            self._set_crypto_entry_lock(f'FILLED_UNPROTECTED:{add_submission.client_order_id}')
            return {
                'status': 'FILLED_UNPROTECTED',
                'reason': 'adaptive trend add protection audit failed',
                'audit': post_add_audit,
            }
        # R remains anchored to the original trade risk.  A profitable stop
        # can sit only a few ticks from the combined entry and must not inflate
        # all later R-based targets/trailing metrics.
        new_risk_distance = initial_risk
        _mark_crypto_entry_state(self, add_submission.client_order_id, OrderState.PROTECTED)
        if str(getattr(self, 'crypto_entry_lock_reason', '') or '').startswith('FILLED_'):
            self._set_crypto_entry_lock(None)

        effective_cfg = dict(cfg or {})
        effective_cfg.update({
            'partial_take_profit_enabled': bool(tp_targets),
            'partial_take_profit_r_multiple': (
                target_distance / max(new_risk_distance, 1e-9)
                if tp_targets
                else 2.0
            ),
            'partial_take_profit_ratio': partial_ratio if tp_targets else 0.0,
            'second_take_profit_enabled': False,
            'second_take_profit_ratio': 0.0,
            'runner_pct': 1.0 - (partial_ratio if tp_targets else 0.0),
            'atr_trailing_enabled': True,
            'atr_trailing_activation_r': (
                target_distance / max(new_risk_distance, 1e-9)
                if target_distance > 0
                else 1.0
            ),
            'atr_trailing_multiplier': float(state.get('trailing_atr_multiplier', 3.8) or 3.8),
            'runner_exit_enabled': True,
            'runner_chandelier_enabled': True,
            'tp1_breakeven_enabled': True,
        })
        state_plan = dict(state)
        applied_cost_buffer_percent = max(
            0.0,
            direction * (applied_post_add_stop - avg_entry) / avg_entry * 100.0,
        )
        state_plan.update({
            'strategy': ADAPTIVE_BREAKOUT_TREND_STRATEGY,
            'side': side,
            'entry_price': avg_entry,
            'risk_distance': new_risk_distance,
            'stop_loss': applied_post_add_stop,
            'hard_stop_loss': applied_post_add_stop,
            'last_stop_price': applied_post_add_stop,
            'adaptive_trend_breakeven_cost_buffer_percent': cost_buffer_percent,
            'adaptive_trend_breakeven_applied_cost_buffer_percent': applied_cost_buffer_percent,
            'adaptive_trend_breakeven_min_live_gap_percent': minimum_live_gap_percent,
            'adaptive_trend_pyramid_enabled': True,
            'adaptive_trend_pyramid_add_count': stage,
            'adaptive_trend_initial_entry_price': initial_entry,
            'adaptive_trend_initial_risk_distance': initial_risk,
            'adaptive_trend_partial_r_multiple': partial_r_original,
            'adaptive_trend_partial_ratio': partial_ratio,
        })
        self._register_utbreakout_trailing_state(
            symbol,
            side,
            avg_entry,
            total_qty,
            state_plan,
            effective_cfg,
        )
        runner_state = self._get_utbreakout_trailing_state(symbol)
        if isinstance(runner_state, dict) and tp1_already_filled:
            runner_state['tp1_filled'] = True
            runner_state['tp1_filled_preserved_after_add'] = True
            self._set_utbreakout_trailing_state(symbol, runner_state)
        audit_status = await self._audit_protection_orders(
            symbol,
            pos=new_pos,
            expected_tp=bool(tp_targets),
            expected_sl=True,
            planned_tp_orders=self._planned_tp_orders_from_state(symbol, runner_state),
            alert=True,
        )
        fee_buffer_fully_applied = abs(
            applied_post_add_stop - raw_cost_break_even
        ) <= max(abs(raw_cost_break_even) * 1e-9, 1e-12)
        stop_label = (
            '비용포함 합산 BE'
            if applied_post_add_stop != pre_add_stop and fee_buffer_fully_applied
            else '합산 BE'
            if applied_post_add_stop != pre_add_stop
            else '기존 BE 유지'
        )
        await self.ctrl.notify(
            f"📈 Adaptive Trend 승자 증액: {self.ctrl.format_symbol_for_display(symbol)} "
            f"{side.upper()} +`{add_qty:.6f}` / stage `{stage}/{len(triggers)}` / "
            f"PnL `{pnl_r:.2f}R` / SL `"
            f"{stop_label} "
            f"{applied_post_add_stop:.4f}`"
        )
        return {
            'status': 'ADDED',
            'order': add_submission.order or {},
            'stage': stage,
            'pnl_r': pnl_r,
            'add_qty': add_qty,
            'audit': audit_status,
            'equity': total_equity,
        }

    async def _manage_utbreakout_partial_trailing(self, symbol, pos, df, cfg):
        state = self._get_utbreakout_trailing_state(symbol)
        if not isinstance(state, dict):
            return None
        if bool(state.get('advanced_live_ladder_state', False)):
            return None
        if not pos:
            cleanup = await self._reconcile_closed_position_protection(
                symbol,
                reason='position closed before runner update',
                alert=True,
                attempts=2,
            )
            if not (
                isinstance(cleanup, dict)
                and cleanup.get('cleanup_confirmed')
            ):
                return {'status': 'FLAT_CLEANUP_PENDING', 'audit': cleanup}
            accounting_state = dict(state)
            accounting_state['_require_exchange_fills'] = True
            accounting = await self._record_closed_trade_accounting(
                symbol,
                'position closed before runner update',
                state=accounting_state,
            )
            if str((accounting or {}).get('status') or '').upper() not in {
                'RECORDED',
                'NO_OPEN_TRADE',
            }:
                return {'status': 'FLAT_ACCOUNTING_PENDING', 'accounting': accounting}
            self._clear_utbreakout_trailing_state(
                symbol,
                finalize=True,
                reason='position closed before runner update',
                exit_price=(accounting or {}).get('exit_price'),
            )
            return {'status': 'FLAT_CLEANED', 'accounting': accounting}
        # Position-exit policy is fixed when the order is opened.  Prefer the
        # persisted entry state so a later global config change cannot tighten
        # or disable an existing strategy's runner unexpectedly.
        atr_trailing_enabled = bool(state.get('atr_trailing_enabled', cfg.get('atr_trailing_enabled', False)))
        tp1_breakeven_enabled = bool(state.get('tp1_breakeven_enabled', cfg.get('tp1_breakeven_enabled', False)))
        soft_stop_enabled = bool(state.get('soft_stop_enabled', cfg.get('soft_stop_enabled', False)))
        near_miss_tp_enabled = bool(state.get('near_miss_tp_enabled', cfg.get('near_miss_tp_enabled', False)))
        small_account_roe_lock_enabled = bool(
            state.get('small_account_aggressive_active', False)
            and state.get('small_account_roe_profit_lock_enabled', False)
        )
        if not atr_trailing_enabled and not tp1_breakeven_enabled and not soft_stop_enabled and not near_miss_tp_enabled and not small_account_roe_lock_enabled:
            return None
        side = str(pos.get('side', '') or '').lower()
        if side != str(state.get('side', '')).lower():
            self._clear_utbreakout_trailing_state(symbol)
            return None
        try:
            current_qty = abs(float(pos.get('contracts', 0) or 0))
            entry_price = float(pos.get('entryPrice') or state.get('entry_price') or 0.0)
            initial_qty = float(state.get('initial_qty') or 0.0)
            risk_distance = float(state.get('risk_distance') or 0.0)
            activation_r = float(state.get('activation_r') or 1.5)
            trailing_mult = float(state.get('trailing_atr_multiplier', cfg.get('atr_trailing_multiplier', 2.0)) or 2.0)
        except (TypeError, ValueError):
            return None
        if current_qty <= 0 or entry_price <= 0 or initial_qty <= 0 or risk_distance <= 0:
            self._clear_utbreakout_trailing_state(symbol)
            return None

        # The mark-ROE staircase must not wait for the first completed candle
        # after entry. Use the entry ATR persisted in the plan (or the most
        # recent live ATR) to arm and place the exchange stop immediately.
        if small_account_roe_lock_enabled:
            position_info = pos.get('info') if isinstance(pos.get('info'), dict) else {}
            early_mark = (
                _safe_float_or_none(pos.get('markPrice'))
                or _safe_float_or_none(position_info.get('markPrice'))
                or _safe_float_or_none(pos.get('lastPrice'))
                or _safe_float_or_none(position_info.get('lastPrice'))
            )
            if early_mark is not None and early_mark > 0:
                leverage = max(
                    1,
                    int(
                        float(
                            pos.get('leverage')
                            or position_info.get('leverage')
                            or state.get('leverage', 1)
                            or 1
                        )
                    ),
                )
                current_roe = (
                    (early_mark - entry_price) / entry_price * leverage * 100.0
                    if side == 'long'
                    else (entry_price - early_mark) / entry_price * leverage * 100.0
                )
                peak_roe = max(
                    float(state.get('small_account_roe_profit_lock_peak_percent', 0.0) or 0.0),
                    float(current_roe),
                )
                early_atr = (
                    _safe_float_or_none(state.get('last_atr'))
                    or _safe_float_or_none(state.get('entry_atr'))
                    or 0.0
                )
                lock_cfg = dict(cfg or {})
                for key in (
                    'small_account_roe_profit_lock_enabled',
                    'small_account_roe_profit_lock_first_trigger_percent',
                    'small_account_roe_profit_lock_second_trigger_percent',
                    'small_account_roe_profit_lock_step_percent',
                    'small_account_roe_profit_lock_atr_multiplier',
                    'small_account_roe_profit_lock_min_gap_percent',
                    'small_account_roe_profit_lock_max_gap_percent',
                    'small_account_roe_profit_lock_min_floor_percent',
                ):
                    if state.get(key) is not None:
                        lock_cfg[key] = state.get(key)
                early_lock = evaluate_small_account_roe_profit_lock(
                    peak_roe_percent=peak_roe,
                    leverage=leverage,
                    atr_percent=early_atr / entry_price * 100.0,
                    config=lock_cfg,
                )
                state.update({
                    'small_account_roe_profit_lock_peak_percent': float(peak_roe),
                    'small_account_roe_profit_lock_current_percent': float(current_roe),
                    'small_account_roe_profit_lock_stage': int(early_lock.stage),
                    'small_account_roe_profit_lock_floor_percent': float(early_lock.lock_roe_percent),
                    'small_account_roe_profit_lock_gap_percent': float(early_lock.giveback_roe_percent),
                    'small_account_roe_profit_lock_reason': early_lock.reason,
                })
                self._set_utbreakout_trailing_state(symbol, state)
                if early_lock.active:
                    price_return = early_lock.lock_roe_percent / (leverage * 100.0)
                    early_stop = (
                        entry_price * (1.0 + price_return)
                        if side == 'long'
                        else entry_price * (1.0 - price_return)
                    )
                    floor_breached = (
                        early_mark <= early_stop
                        if side == 'long'
                        else early_mark >= early_stop
                    )
                    if floor_breached:
                        reason = (
                            f"Small-account ROE profit floor breached: current={current_roe:.2f}% "
                            f"floor={early_lock.lock_roe_percent:.2f}% peak={peak_roe:.2f}%"
                        )
                        close_result = await self._close_position_reduce_only_market(
                            symbol,
                            pos,
                            reason=reason,
                            cfg=cfg,
                        )
                        if (
                            bool((close_result or {}).get('_flat_confirmed'))
                            and bool((close_result or {}).get('_cleanup_confirmed'))
                        ):
                            self._clear_utbreakout_trailing_state(
                                symbol,
                                finalize=True,
                                reason=reason,
                                exit_price=((close_result or {}).get('_accounting') or {}).get('exit_price'),
                            )
                            return {
                                'status': 'EXITED',
                                'reason': 'SMALL_ACCOUNT_ROE_PROFIT_FLOOR',
                                'detail': reason,
                            }
                        return {
                            'status': 'EXIT_PENDING',
                            'reason': 'SMALL_ACCOUNT_ROE_PROFIT_FLOOR',
                            'detail': reason,
                            'order': close_result,
                        }
                    last_stop = float(state.get('last_stop_price') or 0.0)
                    tighter = (
                        early_stop > last_stop
                        if side == 'long'
                        else last_stop <= 0 or early_stop < last_stop
                    )
                    valid = early_stop < early_mark if side == 'long' else early_stop > early_mark
                    min_improve = max(abs(early_mark) * 0.0002, early_atr * 0.05)
                    improved = (
                        early_stop > last_stop + min_improve
                        if side == 'long'
                        else last_stop <= 0 or early_stop < last_stop - min_improve
                    )
                    if tighter and valid and improved:
                        replacement_order = await self._replace_stop_loss_order(
                            symbol,
                            pos,
                            early_stop,
                            reason='Small-account mark ROE profit lock update',
                        )
                        if replacement_order:
                            state.update({
                                'active': True,
                                'last_stop_price': float(early_stop),
                                'last_close': float(early_mark),
                                'runner_mode': f'small_account_roe_lock_stage_{early_lock.stage}',
                                'runner_multiplier': None,
                                'runner_updates': int(state.get('runner_updates') or 0) + 1,
                                'last_update_ts': datetime.now(timezone.utc).isoformat(),
                            })
                            self._set_utbreakout_trailing_state(symbol, state)
                            await self.ctrl.notify(
                                f"🔒 소액계좌 수익보호 SL: {self.ctrl.format_symbol_for_display(symbol)} "
                                f"{side.upper()} ROE 최고 `{peak_roe:.2f}%` → "
                                f"SL 수익 `{early_lock.lock_roe_percent:.2f}%` "
                                f"(`{float(early_stop):.4f}`)"
                            )
                            return state

        if state.get('planned_tp_orders') or state.get('tp_orders'):
            # Record quantity reductions as filled TP legs before auditing.
            # Auditing first could misread a legitimately filled TP as missing
            # and recreate it, producing an unintended extra partial exit.
            state = await self._refresh_ladder_fill_state(
                symbol,
                pos,
                state,
                cfg,
            )
            audit_status = dict(
                (getattr(self, 'last_protection_order_status', {}) or {}).get(
                    symbol,
                    {},
                )
                or {}
            )
            fallback_status = await self._maybe_tp2_fallback_close(symbol, pos, state, cfg, audit_status=audit_status)
            if fallback_status.get('status') == 'TP2_FALLBACK_CLOSED':
                self._clear_utbreakout_trailing_state(
                    symbol,
                    finalize=True,
                    reason='TP2 fallback close',
                    exit_price=(
                        ((fallback_status.get('order') or {}).get('_accounting') or {}).get('exit_price')
                    ),
                )
                return {'status': 'EXITED', 'reason': 'TP2_FALLBACK_CLOSE', 'fallback': fallback_status}

        closed = df.iloc[:-1].copy().reset_index(drop=True) if df is not None and len(df) >= 3 else None
        if closed is None or len(closed) < int(cfg.get('atr_length', 14) or 14) + 2:
            return None
        for col in ['open', 'high', 'low', 'close']:
            closed[col] = pd.to_numeric(closed[col], errors='coerce')
        closed = closed.dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
        if closed.empty:
            return None
        metric_closed, post_entry_gate = self._utbreakout_post_entry_closed_bars(
            closed,
            state,
            cfg,
        )
        if post_entry_gate and (
            metric_closed is None or metric_closed.empty
        ):
            state['profit_alpha_follow_through_reason'] = (
                'waiting for first full post-entry closed bar'
            )
            self._set_utbreakout_trailing_state(symbol, state)
            return state
        if metric_closed is None or metric_closed.empty:
            metric_closed = closed

        current_close = float(metric_closed.iloc[-1]['close'])
        atr_series = self._calculate_wilder_atr_series(closed, int(cfg.get('atr_length', 14) or 14))
        atr_value = float(atr_series.iloc[-1]) if self._is_valid_number(atr_series.iloc[-1]) else 0.0
        if atr_value <= 0:
            return None
        position_info = pos.get('info') if isinstance(pos.get('info'), dict) else {}
        current_mark = (
            _safe_float_or_none(pos.get('markPrice'))
            or _safe_float_or_none(position_info.get('markPrice'))
            or _safe_float_or_none(pos.get('lastPrice'))
            or _safe_float_or_none(position_info.get('lastPrice'))
        )
        highest_price, lowest_price = _mark_aware_excursion_bounds(
            state,
            entry_price=entry_price,
            closed_high=float(metric_closed['high'].max()),
            closed_low=float(metric_closed['low'].min()),
            current_mark=current_mark,
        )
        mfe_r = (
            max(float(state.get('mfe_r') or 0.0), (highest_price - entry_price) / risk_distance)
            if side == 'long' else
            max(float(state.get('mfe_r') or 0.0), (entry_price - lowest_price) / risk_distance)
        )
        mae_r = (
            max(float(state.get('mae_r') or 0.0), (entry_price - lowest_price) / risk_distance)
            if side == 'long' else
            max(float(state.get('mae_r') or 0.0), (highest_price - entry_price) / risk_distance)
        )

        favorable_move = (
            current_close - entry_price
            if side == 'long'
            else entry_price - current_close
        )
        current_r = favorable_move / risk_distance
        remaining_ratio = float(state.get('remaining_ratio', 0.5) or 0.5)
        tp1_remaining_value = state.get('tp1_expected_remaining_ratio', remaining_ratio)
        if tp1_remaining_value is None:
            tp1_remaining_value = remaining_ratio
        tp1_expected_remaining_ratio = float(tp1_remaining_value)
        qty_tolerance = min(
            0.5,
            max(0.0, float(state.get('tp1_breakeven_qty_tolerance', cfg.get('tp1_breakeven_qty_tolerance', 0.08)) or 0.08))
        )
        partial_qty_seen = bool(state.get('tp1_filled')) or (
            current_qty <= initial_qty * min(
                0.98,
                max(0.05, tp1_expected_remaining_ratio + qty_tolerance),
            )
        )
        raw_bar_ts = (
            metric_closed.iloc[-1].get('timestamp')
            if 'timestamp' in metric_closed.columns
            else metric_closed.index[-1]
        )
        try:
            current_bar_ts = int(raw_bar_ts)
        except (TypeError, ValueError, OverflowError):
            current_bar_ts = str(raw_bar_ts)
        if post_entry_gate:
            state['bars_seen'] = max(
                int(state.get('bars_seen', 0) or 0),
                int(metric_closed['timestamp'].nunique())
                if 'timestamp' in metric_closed.columns
                else len(metric_closed),
            )
            state['last_bar_ts'] = current_bar_ts
        elif current_bar_ts != state.get('last_bar_ts'):
            state['bars_seen'] = int(state.get('bars_seen', 0) or 0) + 1
            state['last_bar_ts'] = current_bar_ts
        state.update({
            'highest_price': float(highest_price),
            'lowest_price': float(lowest_price),
            'mfe_r': float(mfe_r),
            'mae_r': float(mae_r),
            'excursion_source': 'closed_bar_plus_polled_mark',
        })
        self._set_utbreakout_trailing_state(symbol, state)

        if bool(state.get('convex_rotation_exit_enabled', False)):
            bars_held = int(state.get('bars_seen', 0) or 0)
            min_bars = max(
                1,
                int(state.get('convex_rotation_min_holding_bars', 32) or 32),
            )
            if bars_held >= min_bars:
                try:
                    numeric_bar_ts = int(current_bar_ts)
                    divisor = 3_600_000 if numeric_bar_ts > 10_000_000_000 else 3_600
                    rank_hour = numeric_bar_ts // divisor
                except (TypeError, ValueError, OverflowError):
                    rank_hour = bars_held // 4
                if rank_hour != state.get('convex_rotation_last_rank_hour'):
                    state['convex_rotation_last_rank_hour'] = rank_hour
                    quality_builder = getattr(
                        self,
                        '_build_utbreakout_selector_quality',
                        None,
                    )
                    quality = quality_builder(symbol) if callable(quality_builder) else {}
                    candidate = (
                        quality.get('candidate')
                        if isinstance(quality, dict)
                        and isinstance(quality.get('candidate'), dict)
                        else {}
                    )
                    current_percentile = _safe_float_or_none(
                        candidate.get('convex_rotation_percentile')
                    )
                    entry_percentile = _safe_float_or_none(
                        state.get('convex_rotation_entry_percentile')
                    )
                    percentile_floor = float(
                        state.get('convex_rotation_rank_percentile_floor', 35.0)
                        or 35.0
                    )
                    reaccelerating = bool(
                        candidate.get(
                            'adaptive_breakout_trend_continuation_reacceleration'
                        )
                        or candidate.get(
                            'adaptive_breakout_trend_compression_breakout'
                        )
                    )
                    no_progress = bool(
                        float(mfe_r)
                        < float(state.get('convex_rotation_max_mfe_r', 0.35) or 0.35)
                        and float(current_r)
                        <= float(
                            state.get('convex_rotation_max_current_r', 0.25)
                            or 0.25
                        )
                    )
                    rotation_decision = evaluate_convex_rotation_exit(
                        enabled=True,
                        bars_held=bars_held,
                        min_bars=min_bars,
                        max_bars=int(
                            state.get('convex_rotation_max_holding_bars', 48)
                            or 48
                        ),
                        mfe_r=float(mfe_r),
                        max_mfe_r=float(
                            state.get('convex_rotation_max_mfe_r', 0.35)
                            or 0.35
                        ),
                        current_r=float(current_r),
                        max_current_r=float(
                            state.get('convex_rotation_max_current_r', 0.25)
                            or 0.25
                        ),
                        entry_percentile=entry_percentile,
                        current_percentile=current_percentile,
                        percentile_floor=percentile_floor,
                        reaccelerating=reaccelerating,
                        prior_bad_count=int(
                            state.get('convex_rotation_bad_rank_count', 0) or 0
                        ),
                        required_confirmations=int(
                            state.get('convex_rotation_rank_confirmations', 2)
                            or 2
                        ),
                    )
                    state.update({
                        'convex_rotation_current_percentile': current_percentile,
                        'convex_rotation_current_rank': candidate.get(
                            'convex_rotation_rank'
                        ),
                        'convex_rotation_current_score': _safe_float_or_none(
                            candidate.get('convex_rotation_score')
                        ),
                        'convex_rotation_bad_rank_count': rotation_decision.bad_count,
                        'convex_rotation_no_progress': no_progress,
                        'convex_rotation_reaccelerating': reaccelerating,
                        'convex_rotation_exit_reason': rotation_decision.reason,
                    })
                    self._set_utbreakout_trailing_state(symbol, state)
                    if rotation_decision.should_exit:
                        reason = (
                            'Convex rotation stale trend: '
                            f'{rotation_decision.reason}'
                        )
                        close_result = await self._close_position_reduce_only_market(
                            symbol,
                            pos,
                            reason=reason,
                            cfg=cfg,
                        )
                        if (
                            bool((close_result or {}).get('_flat_confirmed'))
                            and bool((close_result or {}).get('_cleanup_confirmed'))
                        ):
                            self._clear_utbreakout_trailing_state(
                                symbol,
                                finalize=True,
                                reason=reason,
                                exit_price=((close_result or {}).get('_accounting') or {}).get('exit_price'),
                            )
                            return {
                                'status': 'EXITED',
                                'reason': 'CONVEX_ROTATION_STALE',
                                'detail': reason,
                            }
                        return {
                            'status': 'EXIT_PENDING',
                            'reason': 'CONVEX_ROTATION_STALE',
                            'detail': reason,
                            'order': close_result,
                        }

        if soft_stop_enabled:
            soft_stop = _safe_float_or_none(state.get('soft_stop_price'))
            if soft_stop is not None and soft_stop > 0:
                breached = current_close <= soft_stop if side == 'long' else current_close >= soft_stop
                if breached:
                    state['soft_stop_breach_count'] = int(state.get('soft_stop_breach_count', 0) or 0) + 1
                else:
                    state['soft_stop_breach_count'] = 0
                self._set_utbreakout_trailing_state(symbol, state)
                confirm_bars = max(
                    1,
                    int(state.get('soft_stop_confirm_bars', cfg.get('soft_stop_confirm_bars', 2)) or 2),
                )
                if int(state.get('soft_stop_breach_count', 0) or 0) >= confirm_bars:
                    reason = (
                        f"Soft structure stop: close {current_close:.8f} "
                        f"breached {soft_stop:.8f} for {confirm_bars} bar(s)"
                    )
                    close_result = await self._close_position_reduce_only_market(
                        symbol,
                        pos,
                        reason=reason,
                        cfg=cfg,
                    )
                    if (
                        bool((close_result or {}).get('_flat_confirmed'))
                        and bool((close_result or {}).get('_cleanup_confirmed'))
                    ):
                        self._clear_utbreakout_trailing_state(
                            symbol,
                            finalize=True,
                            reason=reason,
                            exit_price=((close_result or {}).get('_accounting') or {}).get('exit_price'),
                        )
                        return {
                            'status': 'EXITED',
                            'reason': 'SOFT_STRUCTURE_STOP',
                            'detail': reason,
                        }
                    return {
                        'status': 'EXIT_PENDING',
                        'reason': 'SOFT_STRUCTURE_STOP',
                        'detail': reason,
                        'order': close_result,
                    }

        if near_miss_tp_enabled and state.get('planned_tp_orders'):
            try:
                arm_ratio = max(
                    0.50,
                    min(
                        0.99,
                        float(state.get('near_miss_tp_arm_ratio', cfg.get('near_miss_tp_arm_ratio', 0.86)) or 0.86),
                    ),
                )
                base_lock_r = max(
                    0.0,
                    float(state.get('near_miss_tp_lock_r', cfg.get('near_miss_tp_lock_r', 0.28)) or 0.28),
                )
                rejection_atr = max(
                    0.0,
                    float(
                        state.get(
                            'near_miss_tp_rejection_atr',
                            cfg.get('near_miss_tp_rejection_atr', 0.12),
                        )
                        or 0.12
                    ),
                )
            except (TypeError, ValueError):
                arm_ratio = 0.94
                base_lock_r = 0.28
                rejection_atr = 0.12
            for tp_order in self._planned_tp_orders_from_state(symbol, state):
                label = _normalize_tp_plan_label(tp_order.get('tp_label') or tp_order.get('tp_name') or tp_order.get('label'), 'TP')
                label_key = label.lower()
                if bool(state.get(f'{label_key}_filled')) or bool(state.get(f'near_miss_{label_key}_armed')):
                    continue
                target_price = _safe_float_or_none(tp_order.get('price'))
                if target_price is None or target_price <= 0:
                    continue
                target_distance = abs(target_price - entry_price)
                if target_distance <= 0:
                    continue
                if side == 'long':
                    arm_price = entry_price + target_distance * arm_ratio
                    target_reached = highest_price >= target_price
                    near_reached = highest_price >= arm_price and not target_reached
                    rejection_confirmed = (
                        highest_price - current_close
                    ) >= atr_value * rejection_atr
                    lock_r = min(max(base_lock_r, (arm_ratio * target_distance / risk_distance) - 0.12), 0.80)
                    near_stop = entry_price + risk_distance * lock_r
                    valid_stop = near_stop < current_close
                    improved = near_stop > float(state.get('last_stop_price') or 0.0)
                else:
                    arm_price = entry_price - target_distance * arm_ratio
                    target_reached = lowest_price <= target_price
                    near_reached = lowest_price <= arm_price and not target_reached
                    rejection_confirmed = (
                        current_close - lowest_price
                    ) >= atr_value * rejection_atr
                    lock_r = min(max(base_lock_r, (arm_ratio * target_distance / risk_distance) - 0.12), 0.80)
                    near_stop = entry_price - risk_distance * lock_r
                    last_stop = float(state.get('last_stop_price') or 0.0)
                    valid_stop = near_stop > current_close
                    improved = last_stop <= 0 or near_stop < last_stop
                if near_reached:
                    state[f'near_miss_{label_key}_touched'] = True
                touched = bool(state.get(f'near_miss_{label_key}_touched'))
                if not (
                    touched
                    and not target_reached
                    and rejection_confirmed
                    and valid_stop
                    and improved
                ):
                    continue
                attempt_key = f'near_miss_{label_key}_last_attempt_bar_ts'
                if state.get(attempt_key) == current_bar_ts:
                    continue
                state[attempt_key] = current_bar_ts
                self._set_utbreakout_trailing_state(symbol, state)
                replacement_order = await self._replace_stop_loss_order(
                    symbol,
                    pos,
                    near_stop,
                    reason=f'UTBreak near-miss {label} profit lock'
                )
                if replacement_order:
                    state.update({
                        'active': True,
                        f'near_miss_{label_key}_armed': True,
                        f'near_miss_{label_key}_rejection_confirmed': True,
                        'last_stop_price': float(near_stop),
                        'last_atr': float(atr_value),
                        'last_close': float(current_close),
                        'highest_price': float(highest_price),
                        'lowest_price': float(lowest_price),
                        'mfe_r': float(mfe_r),
                        'mae_r': float(mae_r),
                        'runner_mode': f'near_miss_{label_key}_lock',
                        'runner_multiplier': None,
                        'runner_updates': int(state.get('runner_updates') or 0) + 1,
                        'last_update_ts': datetime.now(timezone.utc).isoformat(),
                    })
                    self._set_utbreakout_trailing_state(symbol, state)
                    await self.ctrl.notify(
                        f"?㎛ UTBreak near-miss {label}: {self.ctrl.format_symbol_for_display(symbol)} "
                        f"{side.upper()} SL `{float(near_stop):.4f}` ({lock_r:.2f}R lock)"
                    )
                    return state

        alpha_follow_exit = evaluate_alpha_follow_through_exit(
            enabled=bool(state.get('profit_alpha_enabled')) and bool(
                state.get('profit_alpha_follow_through_enabled', False)
            ),
            bars_held=int(state.get('bars_seen', 0) or 0),
            mfe_r=float(mfe_r),
            mae_r=float(mae_r),
            tp1_filled=bool(state.get('tp1_filled')) or partial_qty_seen,
            follow_through_bars=int(
                state.get(
                    'profit_alpha_follow_through_bars',
                    cfg.get('profit_alpha_follow_through_bars', 3),
                )
                or 3
            ),
            follow_through_min_mfe_r=float(
                state.get(
                    'profit_alpha_follow_through_min_mfe_r',
                    cfg.get('profit_alpha_follow_through_min_mfe_r', 0.35),
                )
                or 0.35
            ),
            early_exit_max_mae_r=float(
                state.get(
                    'profit_alpha_early_exit_max_mae_r',
                    cfg.get('profit_alpha_early_exit_max_mae_r', 0.75),
                )
                or 0.75
            ),
            current_r=current_r,
            stall_exit_max_current_r=float(
                state.get(
                    'profit_alpha_stall_exit_max_current_r',
                    cfg.get('profit_alpha_stall_exit_max_current_r', 0.0),
                )
                or 0.0
            ),
        )
        state['profit_alpha_follow_through_reason'] = alpha_follow_exit.reason
        if alpha_follow_exit.should_exit:
            close_result = await self._close_position_reduce_only_market(
                symbol,
                pos,
                reason=f"Profit alpha follow-through: {alpha_follow_exit.reason}",
                cfg=cfg,
            )
            if (
                bool((close_result or {}).get('_flat_confirmed'))
                and bool((close_result or {}).get('_cleanup_confirmed'))
            ):
                self._clear_utbreakout_trailing_state(
                    symbol,
                    finalize=True,
                    reason=alpha_follow_exit.reason,
                    exit_price=((close_result or {}).get('_accounting') or {}).get('exit_price'),
                )
                return {
                    'status': 'EXITED',
                    'reason': 'PROFIT_ALPHA_FOLLOW_THROUGH',
                    'detail': alpha_follow_exit.reason,
                }
            return {
                'status': 'EXIT_PENDING',
                'reason': 'PROFIT_ALPHA_FOLLOW_THROUGH',
                'detail': alpha_follow_exit.reason,
                'order': close_result,
            }

        if bool(state.get('ev_time_stop_enabled', cfg.get('ev_time_stop_enabled', False))):
            time_stop = evaluate_ev_time_stop(
                bars_held=state.get('bars_seen', 0),
                mfe_r=mfe_r,
                tp1_filled=bool(state.get('tp1_filled')) or partial_qty_seen,
                max_bars=state.get('ev_time_stop_bars', cfg.get('ev_time_stop_bars', 8)),
                min_mfe_r=state.get(
                    'ev_time_stop_min_mfe_r',
                    cfg.get('ev_time_stop_min_mfe_r', 0.45),
                ),
                current_r=current_r,
                max_current_r=state.get(
                    'ev_time_stop_max_current_r',
                    cfg.get('ev_time_stop_max_current_r', 0.0),
                ),
            )
            state['ev_time_stop_reason'] = time_stop.reason
            if time_stop.should_exit:
                close_result = await self._close_position_reduce_only_market(
                    symbol,
                    pos,
                    reason=f"EV time stop: {time_stop.reason}",
                    cfg=cfg,
                )
                if (
                    bool((close_result or {}).get('_flat_confirmed'))
                    and bool((close_result or {}).get('_cleanup_confirmed'))
                ):
                    self._clear_utbreakout_trailing_state(
                        symbol,
                        finalize=True,
                        reason=time_stop.reason,
                        exit_price=((close_result or {}).get('_accounting') or {}).get('exit_price'),
                    )
                    return {
                        'status': 'EXITED',
                        'reason': 'EV_TIME_STOP',
                        'detail': time_stop.reason,
                    }
                return {
                    'status': 'EXIT_PENDING',
                    'reason': 'EV_TIME_STOP',
                    'detail': time_stop.reason,
                    'order': close_result,
                }

        tp1_trigger_r = max(
            0.1,
            float(state.get('tp1_breakeven_trigger_r', cfg.get('tp1_breakeven_trigger_r', activation_r)) or activation_r)
        )
        price_reached_tp1 = favorable_move >= tp1_trigger_r * risk_distance
        wait_for_partial = bool(state.get('tp1_breakeven_wait_for_partial', cfg.get('tp1_breakeven_wait_for_partial', True)))
        if (
            tp1_breakeven_enabled
            and not bool(state.get('breakeven_armed'))
            and (partial_qty_seen if wait_for_partial else (partial_qty_seen or price_reached_tp1))
        ):
            offset_r = max(0.0, float(state.get('tp1_breakeven_offset_r', cfg.get('tp1_breakeven_offset_r', 0.03)) or 0.03))
            breakeven_stop = (
                entry_price + risk_distance * offset_r
                if side == 'long' else
                entry_price - risk_distance * offset_r
            )
            last_stop = float(state.get('last_stop_price') or 0.0)
            improved = (
                breakeven_stop > last_stop
                if side == 'long' else
                last_stop <= 0 or breakeven_stop < last_stop
            )
            valid_stop = breakeven_stop < current_close if side == 'long' else breakeven_stop > current_close
            if improved and valid_stop:
                replacement_order = await self._replace_stop_loss_order(
                    symbol,
                    pos,
                    breakeven_stop,
                    reason='UTBreak TP1 breakeven protection'
                )
                if replacement_order:
                    state.update({
                        'active': True,
                        'breakeven_armed': True,
                        'last_stop_price': float(breakeven_stop),
                        'last_atr': float(atr_value),
                        'last_close': float(current_close),
                        'highest_price': float(highest_price),
                        'lowest_price': float(lowest_price),
                        'mfe_r': float(mfe_r),
                        'mae_r': float(mae_r),
                        'runner_mode': 'tp1_breakeven',
                        'runner_multiplier': None,
                        'runner_updates': int(state.get('runner_updates') or 0) + 1,
                        'last_update_ts': datetime.now(timezone.utc).isoformat(),
                    })
                    self._set_utbreakout_trailing_state(symbol, state)
                    audit_status = await self._audit_protection_orders(
                        symbol,
                        pos=pos,
                        expected_tp=True,
                        expected_sl=True,
                        planned_tp_orders=self._planned_tp_orders_from_state(symbol, state),
                        alert=True
                    )
                    if audit_status.get('missing_tp2') or audit_status.get('tp2_qty_mismatch') or audit_status.get('sl_qty_mismatch'):
                        await self._audit_and_repair_live_ladder_protection(
                            symbol,
                            pos,
                            state,
                            cfg,
                            reason='UTBreak TP1 breakeven residual audit'
                        )
                    await self.ctrl.notify(
                        f"🛡️ UTBreak TP1 보호: {self.ctrl.format_symbol_for_display(symbol)} "
                        f"{side.upper()} SL `{float(breakeven_stop):.4f}` (BE+{offset_r:.2f}R)"
                    )
                    return state
        if not atr_trailing_enabled:
            return None
        active = (
            bool(state.get('active'))
            or favorable_move >= activation_r * risk_distance
            or partial_qty_seen
        )
        if not active:
            return None

        state_cfg = dict(cfg or {})
        state_cfg.update({
            'atr_trailing_multiplier': trailing_mult,
            'atr_trailing_breakeven_enabled': bool(state.get('breakeven_enabled', cfg.get('atr_trailing_breakeven_enabled', True))),
        })
        if bool(state.get('runner_exit_enabled', cfg.get('runner_exit_enabled', False))) and bool(state.get('runner_chandelier_enabled', cfg.get('runner_chandelier_enabled', False))):
            lookback = max(
                int(state.get('runner_chandelier_lookback', cfg.get('runner_chandelier_lookback', 22)) or 22),
                int(state.get('runner_structure_lookback', cfg.get('runner_structure_lookback', 5)) or 5),
            )
            structure_lookback = max(2, int(state.get('runner_structure_lookback', cfg.get('runner_structure_lookback', 5)) or 5))
            recent = closed.tail(max(lookback, structure_lookback))
            recent_swing_low = float(recent.tail(structure_lookback)['low'].min())
            recent_swing_high = float(recent.tail(structure_lookback)['high'].max())
            highest_price = max(highest_price, float(metric_closed['high'].max()))
            lowest_price = min(lowest_price, float(metric_closed['low'].min()))
            mfe_r = (
                max(float(state.get('mfe_r') or 0.0), (highest_price - entry_price) / risk_distance)
                if side == 'long' else
                max(float(state.get('mfe_r') or 0.0), (entry_price - lowest_price) / risk_distance)
            )
            mae_r = (
                max(float(state.get('mae_r') or 0.0), (entry_price - lowest_price) / risk_distance)
                if side == 'long' else
                max(float(state.get('mae_r') or 0.0), (highest_price - entry_price) / risk_distance)
            )
            trend_health = state.get('trend_health') if isinstance(state.get('trend_health'), dict) else None
            stop_info = build_dynamic_chandelier_stop(
                side=side,
                current_stop=state.get('last_stop_price'),
                entry_price=entry_price,
                current_close=current_close,
                atr_value=atr_value,
                highest_high=highest_price,
                lowest_low=lowest_price,
                recent_swing_low=recent_swing_low,
                recent_swing_high=recent_swing_high,
                risk_distance=risk_distance,
                trend_health=trend_health,
                cfg=state_cfg,
            )
            if not isinstance(stop_info, dict):
                return None
            raw_trail = float(stop_info['stop_price'])
            runner_mode = stop_info.get('mode')
            runner_multiplier = stop_info.get('multiplier')
        else:
            raw_trail = (
                current_close - (atr_value * trailing_mult)
                if side == 'long'
                else current_close + (atr_value * trailing_mult)
            )
            if bool(state.get('breakeven_enabled', cfg.get('atr_trailing_breakeven_enabled', True))):
                raw_trail = max(entry_price, raw_trail) if side == 'long' else min(entry_price, raw_trail)
            runner_mode = 'atr_close'
            runner_multiplier = trailing_mult

        lock_cfg = dict(cfg or {})
        for key in (
            'ev_mfe_profit_lock_enabled',
            'ev_mfe_lock_trigger_1_r',
            'ev_mfe_lock_trigger_2_r',
            'ev_mfe_lock_trigger_3_r',
        ):
            if state.get(key) is not None:
                lock_cfg[key] = state.get(key)
        mfe_lock = evaluate_mfe_profit_lock(
            mfe_r=mfe_r,
            mode=state.get('ev_adaptive_mode') or state.get('ev_exit_profile_name') or 'TREND',
            config=lock_cfg,
        )
        state['ev_mfe_lock_stage'] = int(mfe_lock.stage)
        state['ev_mfe_lock_r'] = float(mfe_lock.lock_r)
        state['ev_mfe_lock_reason'] = mfe_lock.reason
        if mfe_lock.active:
            lock_stop = (
                entry_price + risk_distance * mfe_lock.lock_r
                if side == 'long'
                else entry_price - risk_distance * mfe_lock.lock_r
            )
            valid_lock = (
                lock_stop < current_close
                if side == 'long'
                else lock_stop > current_close
            )
            tighter_lock = (
                lock_stop > raw_trail
                if side == 'long'
                else lock_stop < raw_trail
            )
            if valid_lock and tighter_lock:
                raw_trail = lock_stop
                runner_mode = f'mfe_lock_stage_{mfe_lock.stage}'
                runner_multiplier = None
        self._set_utbreakout_trailing_state(symbol, state)

        last_stop = float(state.get('last_stop_price') or 0.0)
        new_stop = max(last_stop, raw_trail) if side == 'long' else (min(last_stop, raw_trail) if last_stop > 0 else raw_trail)
        stop_reference_price = current_mark or current_close
        if side == 'long' and new_stop >= stop_reference_price:
            return None
        if side == 'short' and new_stop <= stop_reference_price:
            return None

        min_improve = max(abs(stop_reference_price) * 0.0002, atr_value * 0.05)
        improved = (
            new_stop > last_stop + min_improve
            if side == 'long'
            else last_stop <= 0 or new_stop < last_stop - min_improve
        )
        if not improved and state.get('active'):
            return None

        replacement_order = await self._replace_stop_loss_order(
            symbol,
            pos,
            new_stop,
            reason='UTBreak ATR trailing stop update'
        )
        if not replacement_order:
            return None
        state.update({
            'active': True,
            'last_stop_price': float(new_stop),
            'last_atr': float(atr_value),
            'last_close': float(current_close),
            'highest_price': float(highest_price),
            'lowest_price': float(lowest_price),
            'mfe_r': float(mfe_r),
            'mae_r': float(mae_r),
            'runner_mode': runner_mode,
            'runner_multiplier': runner_multiplier,
            'ev_mfe_lock_stage': int(mfe_lock.stage),
            'ev_mfe_lock_r': float(mfe_lock.lock_r),
            'ev_mfe_lock_reason': mfe_lock.reason,
            'runner_updates': int(state.get('runner_updates') or 0) + 1,
            'last_update_ts': datetime.now(timezone.utc).isoformat(),
        })
        self._set_utbreakout_trailing_state(symbol, state)
        audit_status = await self._audit_protection_orders(
            symbol,
            pos=pos,
            expected_tp=True,
            expected_sl=True,
            planned_tp_orders=self._planned_tp_orders_from_state(symbol, state),
            alert=True
        )
        if audit_status.get('missing_tp2') or audit_status.get('tp2_qty_mismatch') or audit_status.get('sl_qty_mismatch'):
            await self._audit_and_repair_live_ladder_protection(
                symbol,
                pos,
                state,
                cfg,
                reason='UTBreak ATR trailing residual audit'
            )
        await self.ctrl.notify(
            f"🧭 UTBreak Runner SL 갱신: {self.ctrl.format_symbol_for_display(symbol)} "
            f"{side.upper()} SL `{float(new_stop):.4f}` ({runner_mode}, MFE {mfe_r:.2f}R)"
        )
        return state

    async def _current_stop_loss_price(self, symbol, state=None):
        state_stop = _safe_float_or_none((state or {}).get('last_stop_price'))
        if state_stop is not None and state_stop > 0:
            return float(state_stop)
        orders = await self._collect_protection_orders(symbol)
        sl_orders = [
            order for order in (orders or [])
            if self._classify_protection_order(order) == 'sl'
        ]
        newest = self._newest_protection_order(sl_orders)
        if not newest:
            return None
        stop = self._protection_trigger_price(newest)
        if stop is None:
            info = self._protection_order_info(newest)
            stop = newest.get('stopPrice') or info.get('stopPrice')
        parsed = _safe_float_or_none(stop)
        return float(parsed) if parsed is not None and parsed > 0 else None

    def _build_aggressive_growth_tp_targets(self, side, entry_price, risk_distance, growth_score):
        split = choose_aggressive_exit_split(growth_score)
        targets = []
        if split['tp1_pct'] > 0:
            targets.append({
                'label': 'TP1',
                'kind': 'tp1',
                'distance': risk_distance * 1.0,
                'qty_ratio': split['tp1_pct'],
            })
        if split['tp2_pct'] > 0:
            targets.append({
                'label': 'TP2',
                'kind': 'tp2',
                'distance': risk_distance * 2.0,
                'qty_ratio': split['tp2_pct'],
            })
        if not targets:
            targets.append({
                'label': 'TP',
                'kind': 'tp',
                'distance': risk_distance * 2.0,
                'qty_ratio': 1.0,
            })
        return targets, split

    async def _maybe_apply_aggressive_growth_pyramiding(self, symbol, pos, df, cfg):
        if self.is_upbit_mode():
            return None
        try:
            active_strategy = str(self.get_runtime_strategy_params().get('active_strategy', '') or '').lower()
        except Exception:
            active_strategy = ''
        if active_strategy == ADAPTIVE_BREAKOUT_TREND_STRATEGY:
            return await self._maybe_apply_adaptive_trend_pyramiding(
                symbol,
                pos,
                df,
                cfg,
            )
        if not bool(cfg.get('aggressive_growth_enabled', False)):
            return None
        if not bool(cfg.get('aggressive_growth_pyramiding_enabled', True)):
            return None
        if active_strategy not in UTBREAKOUT_STRATEGIES:
            return None
        if not pos or str(pos.get('side', '') or '').lower() != 'long':
            self._clear_aggressive_growth_position(symbol)
            return None

        key = self._aggressive_growth_symbol_key(symbol)
        tracked = getattr(self, 'aggressive_growth_positions', {})
        meta = tracked.get(key) if isinstance(tracked, dict) else None
        state = self._get_utbreakout_trailing_state(symbol)
        if not isinstance(meta, dict) and isinstance(state, dict) and bool(state.get('aggressive_growth_overlay')):
            meta = self._record_aggressive_growth_position(symbol, pos, state)
        if not isinstance(meta, dict):
            return None

        closed = df.iloc[:-1].copy().reset_index(drop=True) if df is not None and len(df) >= 3 else None
        if closed is None or closed.empty:
            return None
        for col in ['open', 'high', 'low', 'close']:
            if col not in closed.columns:
                return None
            closed[col] = pd.to_numeric(closed[col], errors='coerce')
        if 'volume' in closed.columns:
            closed['volume'] = pd.to_numeric(closed['volume'], errors='coerce')
        closed = closed.dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
        if closed.empty:
            return None

        current_price = (
            _safe_float_or_none(pos.get('markPrice'))
            or _safe_float_or_none(closed.iloc[-1]['close'])
            or _safe_float_or_none(meta.get('last_price'))
            or 0.0
        )
        entry_price = (
            _safe_float_or_none(meta.get('entry_price'))
            or _safe_float_or_none((state or {}).get('entry_price'))
            or _safe_float_or_none(pos.get('entryPrice'))
            or 0.0
        )
        risk_distance = (
            _safe_float_or_none(meta.get('risk_distance'))
            or _safe_float_or_none((state or {}).get('risk_distance'))
            or 0.0
        )
        base_qty = (
            _safe_float_or_none(meta.get('base_qty'))
            or _safe_float_or_none((state or {}).get('initial_qty'))
            or abs(float(self._position_signed_contracts(pos) or pos.get('contracts', 0) or 0))
        )
        add_count = int(meta.get('pyramid_add_count', meta.get('pyramid_adds', (state or {}).get('pyramid_add_count', 0))) or 0)
        if current_price <= 0 or entry_price <= 0 or risk_distance <= 0 or base_qty <= 0:
            return None

        stop_price = await self._current_stop_loss_price(symbol, state)
        if stop_price is None or stop_price <= 0:
            return {
                'status': 'BLOCKED',
                'reason': 'current SL unavailable',
            }
        sl_can_move = current_price > entry_price

        try:
            total_equity, _, _ = await self.get_balance_info()
        except Exception:
            total_equity = 0.0
        equity = _safe_float_or_none(total_equity) or 0.0
        high_watermark = self._update_aggressive_growth_high_watermark(equity)
        daily_pnl = 0.0
        weekly_pnl = 0.0
        try:
            _, daily_pnl = self.db.get_daily_stats()
        except Exception:
            daily_pnl = 0.0
        try:
            _, weekly_pnl = self.db.get_weekly_stats()
        except Exception:
            weekly_pnl = 0.0

        try:
            all_positions = await asyncio.to_thread(self.exchange.fetch_positions)
        except Exception:
            all_positions = [pos]
        exposure = self._calculate_aggressive_growth_exposure(all_positions, symbol=symbol)
        try:
            common_cfg = self.get_runtime_common_settings()
        except Exception:
            common_cfg = {}
        leverage = (
            _safe_float_or_none((common_cfg or {}).get('leverage'))
            or _safe_float_or_none(cfg.get('leverage'))
            or 5.0
        )
        sleeve_cap, sleeve_pct = calculate_aggressive_sleeve_notional_cap(
            equity,
            leverage=leverage,
            cfg=cfg,
            high_watermark=high_watermark,
        )
        available_sleeve_notional = max(0.0, sleeve_cap - float(exposure.get('total_notional', 0.0) or 0.0))
        available_sleeve_qty = available_sleeve_notional / max(current_price, 1e-12)

        futures_context = {}
        try:
            futures_context = await self._fetch_utbreakout_futures_context(symbol)
        except Exception as exc:
            futures_context = {'futures_context_error': str(exc)}
        futures_context = dict(futures_context or {})
        price_change_1h = None
        price_change_4h = None
        close_series = closed['close'].astype(float)
        if len(close_series) >= 5:
            prev = float(close_series.iloc[-5])
            if prev > 0:
                price_change_1h = (current_price - prev) / prev * 100.0
        if len(close_series) >= 17:
            prev = float(close_series.iloc[-17])
            if prev > 0:
                price_change_4h = (current_price - prev) / prev * 100.0

        volume_ok = False
        if 'volume' in closed.columns and len(closed) >= 10:
            recent_volume = _safe_float_or_none(closed.iloc[-1].get('volume'))
            avg_volume = _safe_float_or_none(closed['volume'].tail(20).mean())
            if recent_volume is not None and avg_volume is not None and avg_volume > 0:
                volume_ratio = recent_volume / avg_volume
                volume_ok = volume_ratio >= float(cfg.get('bias_continuation_min_volume_ratio', 0.50) or 0.50)
        else:
            volume_ratio = None

        trend_bullish = False
        if len(close_series) >= 60:
            ema_fast = close_series.ewm(span=20, adjust=False).mean().iloc[-1]
            ema_slow = close_series.ewm(span=50, adjust=False).mean().iloc[-1]
            trend_bullish = bool(current_price > ema_fast > ema_slow)
        elif len(close_series) >= 20:
            trend_bullish = bool(current_price > close_series.tail(20).mean())
        quality_score = _safe_float_or_none(meta.get('aggressive_growth_score')) or _safe_float_or_none((state or {}).get('aggressive_growth_score'))
        quality_ok = quality_score is None or quality_score >= 60.0
        funding_value = _safe_float_or_none(futures_context.get('funding_rate'))
        funding_not_overheated = (
            funding_value is None
            or funding_value <= float(cfg.get('funding_long_max', 0.0008) or 0.0008)
        )
        growth_context = {
            'side': 'long',
            'entry_price': entry_price,
            'current_price': current_price,
            'risk_distance': risk_distance,
            'initial_qty': base_qty,
            'pyramid_add_count': add_count,
            'stop_loss_price': stop_price,
            'account_equity': equity,
            'daily_pnl_usdt': daily_pnl,
            'weekly_pnl_usdt': weekly_pnl,
            'sl_can_move_to_breakeven': sl_can_move,
            'available_sleeve_qty': available_sleeve_qty,
            'symbol_trend_bullish': trend_bullish,
            'htf_trend_bullish': trend_bullish,
            'volume_ok': volume_ok,
            'quality_ok': quality_ok,
            'funding_not_overheated': funding_not_overheated,
            'volatility_safe': True,
            'growth_score': quality_score,
            'funding_rate': futures_context.get('funding_rate'),
            'funding_percentile_7d': futures_context.get('funding_percentile_7d'),
            'funding_percentile_30d': futures_context.get('funding_percentile_30d'),
            'open_interest_change_1h': futures_context.get('open_interest_delta_pct'),
            'open_interest_change_4h': futures_context.get('open_interest_change_4h'),
            'price_change_1h': price_change_1h,
            'price_change_4h': price_change_4h,
            'long_short_ratio': futures_context.get('long_short_ratio'),
            'taker_buy_sell_ratio': futures_context.get('taker_buy_sell_ratio'),
            'liquidation_imbalance': futures_context.get('liquidation_imbalance'),
        }
        derivatives_score, derivatives_reasons = evaluate_derivatives_growth_score(growth_context)
        growth_context['growth_context_valid'] = (
            trend_bullish
            and volume_ok
            and funding_not_overheated
            and not any(
                reason in {'funding_overheated', 'long_crowded', 'oi_up_price_stall', 'oi_4h_up_price_stall'}
                for reason in derivatives_reasons
            )
        )
        pyramid_plan = build_aggressive_growth_pyramid_plan(
            {
                'side': 'long',
                'entry_price': entry_price,
                'risk_distance': risk_distance,
                'initial_qty': base_qty,
                'pyramid_add_count': add_count,
            },
            cfg,
            growth_context,
        )
        if not pyramid_plan.get('accepted'):
            try:
                trigger_r = float(cfg.get('aggressive_growth_pyramid_trigger_r', 1.0) or 1.0)
                pnl_r = (current_price - entry_price) / max(risk_distance, 1e-12)
                now_ts = time.time()
                last_notice = float(meta.get('last_pyramid_block_notice_ts', 0.0) or 0.0)
                if pnl_r >= trigger_r and now_ts - last_notice >= 300:
                    meta['last_pyramid_block_notice_ts'] = now_ts
                    await self.ctrl.notify(
                        f"⚠️ Aggressive Growth 추가진입 차단: {self.ctrl.format_symbol_for_display(symbol)} "
                        f"{pyramid_plan.get('reason')}"
                    )
            except Exception:
                logger.debug("Aggressive growth pyramid block notice skipped", exc_info=True)
            return {'status': 'BLOCKED', **pyramid_plan}

        breakeven_stop = float(pyramid_plan.get('breakeven_stop_price') or entry_price)
        if bool(pyramid_plan.get('requires_sl_move')):
            replacement = await self._replace_stop_loss_order(
                symbol,
                pos,
                breakeven_stop,
                reason='Aggressive Growth pyramid breakeven'
            )
            if not replacement:
                await self.ctrl.notify(
                    f"⚠️ Aggressive Growth 추가진입 차단: {self.ctrl.format_symbol_for_display(symbol)} "
                    "BE SL 교체 확인 실패"
                )
                return {'status': 'BLOCKED', 'reason': 'breakeven SL replacement failed'}
            if isinstance(state, dict):
                state.update({
                    'active': True,
                    'breakeven_armed': True,
                    'last_stop_price': breakeven_stop,
                    'last_update_ts': datetime.now(timezone.utc).isoformat(),
                })
                self._set_utbreakout_trailing_state(symbol, state)
            pre_add_audit = await self._audit_protection_orders(
                symbol,
                pos=pos,
                expected_tp=True,
                expected_sl=True,
                planned_tp_orders=self._planned_tp_orders_from_state(symbol, state),
                alert=True
            )
            if not pre_add_audit.get('sl_present'):
                await self.ctrl.notify(
                    f"⚠️ Aggressive Growth 추가진입 차단: {self.ctrl.format_symbol_for_display(symbol)} "
                    "BE SL 거래소 확인 실패"
                )
                return {'status': 'BLOCKED', 'reason': 'breakeven SL audit failed', 'audit': pre_add_audit}
        else:
            pre_add_audit = await self._audit_protection_orders(
                symbol,
                pos=pos,
                expected_tp=True,
                expected_sl=True,
                planned_tp_orders=self._planned_tp_orders_from_state(symbol, state),
                alert=True
            )
            if not pre_add_audit.get('sl_present'):
                await self.ctrl.notify(
                    f"⚠️ Aggressive Growth 추가진입 차단: {self.ctrl.format_symbol_for_display(symbol)} "
                    "기존 BE SL 거래소 확인 실패"
                )
                return {'status': 'BLOCKED', 'reason': 'existing breakeven SL audit failed', 'audit': pre_add_audit}

        add_qty = self.safe_amount(symbol, float(pyramid_plan.get('add_qty') or 0.0))
        if float(add_qty) <= 0:
            return {'status': 'BLOCKED', 'reason': 'pyramid quantity rounded to zero'}
        entry_blocker = _crypto_entry_block_reason(self, symbol)
        if entry_blocker and str(entry_blocker).startswith('PROTECTED:'):
            entry_blocker = None
        if entry_blocker:
            return {'status': 'BLOCKED', 'reason': entry_blocker}
        await self.ensure_market_settings(
            symbol,
            leverage=int(max(1, float(leverage or 1))),
            position=pos,
        )
        liquidation_preflight = await self._preflight_liquidation_safety(
            symbol,
            'long',
            current_price,
            breakeven_stop,
            int(max(1, float(leverage or 1))),
            base_qty + float(add_qty),
            cfg,
        )
        if not liquidation_preflight.get('valid'):
            return {
                'status': 'BLOCKED',
                'reason': liquidation_preflight.get('reason') or liquidation_preflight.get('status'),
            }
        _ensure_trading_safety_runtime(self)
        add_stage = int(meta.get('pyramid_add_count', 0) or 0) + 1
        add_submission = await self.crypto_execution.submit_position_add(
            strategy='AGGRESSIVE_GROWTH_PYRAMID',
            symbol=symbol,
            side='long',
            signal_timestamp=state.get('last_bar_ts') or meta.get('last_update_ts') or int(time.time()),
            qty=float(add_qty),
            stage=str(add_stage),
        )
        if not add_submission.accepted:
            return {
                'status': add_submission.state,
                'reason': add_submission.error or 'pyramid add order not accepted',
                'client_order_id': add_submission.client_order_id,
            }
        order = add_submission.order or {}
        self.position_cache = None
        self.position_cache_time = 0
        _, new_pos = await self._fetch_position_with_liquidation(
            symbol,
            add_submission.position,
        )
        if not new_pos or str(new_pos.get('side', '') or '').lower() != 'long':
            self._set_crypto_entry_lock(f'SUBMITTED_UNKNOWN:{add_submission.client_order_id}')
            await self.ctrl.notify(
                f"⚠️ Aggressive Growth 추가진입 후 포지션 확인 실패: {self.ctrl.format_symbol_for_display(symbol)}"
            )
            return {'status': 'ORDER_SENT_POSITION_MISSING', 'order': order}

        total_qty = abs(float(self._position_signed_contracts(new_pos) or new_pos.get('contracts', 0) or 0))
        avg_entry = _safe_float_or_none(new_pos.get('entryPrice')) or entry_price
        post_fill_mark = (
            _safe_float_or_none(new_pos.get('markPrice'))
            or _safe_float_or_none(new_pos.get('mark_price'))
            or current_price
        )
        valid_live_stop_geometry = bool(
            total_qty > 0
            and breakeven_stop > 0
            and post_fill_mark is not None
            and post_fill_mark > 0
            and breakeven_stop < post_fill_mark
        )
        if not valid_live_stop_geometry:
            await self.ctrl.notify(
                f"⚠️ Aggressive Growth 추가진입 후 SL 실효성 검증 실패: {self.ctrl.format_symbol_for_display(symbol)}"
            )
            _mark_crypto_entry_state(
                self,
                add_submission.client_order_id,
                OrderState.FILLED_UNPROTECTED,
            )
            self._set_crypto_entry_lock(
                f'FILLED_UNPROTECTED:{add_submission.client_order_id}'
            )
            close_status = await self._fail_closed_unprotected_position(
                symbol,
                reason=(
                    'aggressive growth post-fill stop is not live: '
                    f'stop={breakeven_stop} mark={post_fill_mark}'
                ),
                status_code='PYRAMID_POST_FILL_STOP_NOT_LIVE',
                expected_tp=True,
                emergency_close=True,
            )
            return {
                'status': 'FILLED_UNPROTECTED',
                'reason': 'aggressive growth post-fill stop is not live',
                'close_status': close_status,
            }
        actual_liquidation = await self._verify_actual_liquidation_safety(
            symbol,
            'long',
            breakeven_stop,
            new_pos,
            cfg,
            add_submission.client_order_id,
        )
        if not actual_liquidation.get('valid'):
            return {
                'status': actual_liquidation.get('status'),
                'reason': 'pyramid post-fill liquidation safety failed',
                'close_status': actual_liquidation.get('close_status'),
            }
        new_pos = actual_liquidation.get('position') or new_pos
        new_risk_distance = abs(avg_entry - breakeven_stop)
        growth_score = (
            pyramid_plan.get('growth_score')
            if pyramid_plan.get('growth_score') is not None
            else quality_score
        )
        tp_targets, split = self._build_aggressive_growth_tp_targets(
            'long',
            avg_entry,
            new_risk_distance,
            growth_score or 0.0,
        )
        await self._place_tp_sl_orders(
            symbol,
            'long',
            avg_entry,
            total_qty,
            sl_distance=new_risk_distance,
            tp_targets=tp_targets,
            preserve_runner_qty=True,
            sl_price_override=breakeven_stop,
        )
        post_add_audit = await self._audit_protection_orders(
            symbol,
            pos=new_pos,
            expected_tp=True,
            expected_sl=True,
            alert=True,
        )
        if not (
            post_add_audit.get('fetch_ok')
            and post_add_audit.get('sl_present')
            and not post_add_audit.get('sl_qty_mismatch')
        ):
            self._set_crypto_entry_lock(f'FILLED_UNPROTECTED:{add_submission.client_order_id}')
            return {
                'status': 'FILLED_UNPROTECTED',
                'reason': 'pyramid add protection audit failed',
                'audit': post_add_audit,
            }
        _mark_crypto_entry_state(
            self,
            add_submission.client_order_id,
            OrderState.PROTECTED,
        )
        if str(getattr(self, 'crypto_entry_lock_reason', '') or '').startswith('FILLED_'):
            self._set_crypto_entry_lock(None)
        effective_cfg = dict(cfg or {})
        effective_cfg.update({
            'partial_take_profit_enabled': True,
            'partial_take_profit_r_multiple': 1.0,
            'partial_take_profit_ratio': split['tp1_pct'],
            'second_take_profit_enabled': split['tp2_pct'] > 0,
            'second_take_profit_r_multiple': 3.50,
            'second_take_profit_ratio': split['tp2_pct'],
            'runner_pct': split['runner_pct'],
            'atr_trailing_enabled': True,
            'atr_trailing_multiplier': float(cfg.get('aggressive_growth_trailing_atr_multiplier', 2.5) or 2.5),
            'runner_exit_enabled': True,
            'runner_chandelier_enabled': True,
            'tp1_breakeven_enabled': True,
        })
        state_plan = dict(meta)
        state_plan.update({
            'side': 'long',
            'entry_price': avg_entry,
            'risk_distance': new_risk_distance,
            'stop_loss': breakeven_stop,
            'aggressive_growth_overlay': True,
            'aggressive_growth_score': growth_score,
            'runner_pct': split['runner_pct'],
            'pyramid_add_count': int(pyramid_plan.get('pyramid_add_count') or add_count + 1),
            'base_qty': base_qty,
        })
        self._register_utbreakout_trailing_state(
            symbol,
            'long',
            avg_entry,
            total_qty,
            state_plan,
            effective_cfg,
        )
        runner_state = self._get_utbreakout_trailing_state(symbol)
        audit_status = await self._audit_protection_orders(
            symbol,
            pos=new_pos,
            expected_tp=True,
            expected_sl=True,
            planned_tp_orders=self._planned_tp_orders_from_state(symbol, runner_state),
            alert=True
        )
        if audit_status.get('missing_tp2') or audit_status.get('tp2_qty_mismatch') or audit_status.get('sl_qty_mismatch'):
            await self._audit_and_repair_live_ladder_protection(
                symbol,
                new_pos,
                runner_state,
                effective_cfg,
                reason='Aggressive Growth pyramid residual audit'
            )
        self._record_aggressive_growth_position(
            symbol,
            new_pos,
            state_plan,
            qty=total_qty,
            current_price=avg_entry,
        )
        await self.ctrl.notify(
            f"🚀 Aggressive Growth 추가진입: {self.ctrl.format_symbol_for_display(symbol)} "
            f"+`{float(add_qty):.6f}` / add `{int(pyramid_plan.get('pyramid_add_count') or add_count + 1)}` / "
            f"SL `{breakeven_stop:.4f}` / split `{split['tp1_pct']:.0%}/{split['tp2_pct']:.0%}/{split['runner_pct']:.0%}`"
        )
        return {
            'status': 'ADDED',
            'order': order,
            'plan': pyramid_plan,
            'audit': audit_status,
            'sleeve_pct': sleeve_pct,
            'derivatives_score': derivatives_score,
            'derivatives_reasons': derivatives_reasons,
        }

    async def _place_tp_sl_orders(
        self,
        symbol,
        side,
        entry_price,
        qty,
        tp_distance=None,
        sl_distance=None,
        tp_qty_ratio=1.0,
        tp_targets=None,
        preserve_runner_qty=False,
        sl_price_override=None,
    ):
        """Place reduce-only TP/SL protection orders for the current futures position."""
        try:
            if self.is_upbit_mode():
                logger.info("TP/SL order placement skipped in Upbit spot mode.")
                return

            sl_order = None
            tp_price = None
            sl_price = None
            pos = None
            side = str(side or '').lower()
            entry_price = float(entry_price or 0.0)
            try:
                protection_cfg = self._get_utbot_filtered_breakout_config(self.get_runtime_strategy_params())
            except Exception:
                protection_cfg = build_default_utbot_filtered_breakout_config()
            try:
                common_cfg = self.get_runtime_common_settings()
            except Exception:
                common_cfg = {}
            common_cfg = common_cfg if isinstance(common_cfg, dict) else {}
            protection_cfg = dict(protection_cfg or {})
            sl_max_attempts = max(1, int(protection_cfg.get('sl_place_max_retries', 3) or 3))
            sl_retry_delay = max(0.0, float(protection_cfg.get('sl_retry_delay_sec', 0.7) or 0.0))
            emergency_close_on_sl_fail = bool(protection_cfg.get('emergency_close_on_sl_fail', True))
            if side not in {'long', 'short'} or entry_price <= 0:
                logger.error(f"Protection placement skipped: invalid side/entry ({symbol}, {side}, {entry_price})")
                if sl_distance is not None:
                    await self._fail_closed_unprotected_position(
                        symbol,
                        reason=f'invalid protection side/entry: side={side}, entry={entry_price}',
                        status_code='INVALID_PROTECTION_INPUT',
                        expected_tp=bool(tp_targets or tp_distance),
                        emergency_close=emergency_close_on_sl_fail,
                    )
                return

            pos = await self.get_server_position(symbol, use_cache=False)
            if pos and str(pos.get('side', '')).lower() == side:
                pos_contracts = abs(float(pos.get('contracts', 0) or 0))
                if pos_contracts > 0:
                    qty = self.safe_amount(symbol, pos_contracts)
                pos_entry = float(pos.get('entryPrice') or 0.0)
                if pos_entry > 0:
                    entry_price = pos_entry
            raw_qty = abs(float(qty or 0))
            sl_qty = self.safe_amount(symbol, raw_qty)
            if float(sl_qty) <= 0:
                logger.error(f"Protection placement skipped: invalid qty for {symbol}: {sl_qty}")
                if sl_distance is not None:
                    await self._fail_closed_unprotected_position(
                        symbol,
                        reason=f'invalid protection quantity: {sl_qty}',
                        status_code='INVALID_PROTECTION_QTY',
                        expected_tp=bool(tp_targets or tp_distance),
                        emergency_close=emergency_close_on_sl_fail,
                    )
                return
            await self._cancel_protection_orders(symbol, reason='before new protection placement')
            await asyncio.sleep(0.25)
            remaining_before_place = await self._collect_protection_orders(symbol)
            if remaining_before_place:
                await self._cancel_protection_orders(
                    symbol,
                    reason='stale protection still open before placement',
                    orders=remaining_before_place
                )

            if side == 'long':
                tp_side = 'sell'
                sl_side = 'sell'
                if sl_price_override is not None:
                    sl_price = self.safe_price(symbol, sl_price_override)
                elif sl_distance is not None and sl_distance > 0:
                    sl_price = self.safe_price(symbol, entry_price - sl_distance)
            else:
                tp_side = 'buy'
                sl_side = 'buy'
                if sl_price_override is not None:
                    sl_price = self.safe_price(symbol, sl_price_override)
                elif sl_distance is not None and sl_distance > 0:
                    sl_price = self.safe_price(symbol, entry_price + sl_distance)

            if sl_price is not None and pos:
                rounded_liquidation_check = await self._verify_actual_liquidation_safety(
                    symbol,
                    side,
                    sl_price,
                    pos,
                    protection_cfg,
                    None,
                )
                if not rounded_liquidation_check.get('valid'):
                    self.last_protection_order_status[symbol] = {
                        'tp_expected': bool(tp_targets or tp_distance),
                        'sl_expected': True,
                        'tp_present': False,
                        'sl_present': False,
                        'missing_tp': bool(tp_targets or tp_distance),
                        'missing_sl': True,
                        'liquidation_safety': 'UNSAFE',
                        'liquidation_safety_reason': rounded_liquidation_check.get('status'),
                        'status': 'LIQUIDATION_SAFETY_FAILED',
                    }
                    return
                pos = rounded_liquidation_check.get('position') or pos

            tp_ladder_status = None
            raw_targets = tp_targets
            combined_ladder_cfg = dict(protection_cfg)
            combined_ladder_cfg.update(common_cfg)
            use_live_ladder = (
                _normalize_live_real_stage(common_cfg.get("live_activation_stage")) == "LIVE_REAL_SMALL_CAP"
                and str(combined_ladder_cfg.get("live_tp_ladder_mode", "tp1_tp2_full_exit") or "").lower()
                == "tp1_tp2_full_exit"
                and sl_price is not None
                and not preserve_runner_qty
            )
            if use_live_ladder:
                tp_ladder_status = self._build_tp_ladder_orders(
                    symbol,
                    side,
                    entry_price,
                    sl_price,
                    raw_qty,
                    combined_ladder_cfg,
                )
                if tp_ladder_status.get("ok") and tp_ladder_status.get("tp_targets"):
                    raw_targets = list(tp_ladder_status.get("tp_targets") or [])
                    preserve_runner_qty = False
                elif tp_ladder_status.get("reason"):
                    raw_targets = []
                    logger.warning(
                        "[Protection] TP ladder unavailable for %s: %s",
                        symbol,
                        tp_ladder_status.get("reason"),
                    )

            normalized_tp_targets = []
            if raw_targets is None and tp_distance is not None:
                raw_targets = [{
                    'label': 'TP',
                    'kind': 'tp',
                    'distance': tp_distance,
                    'qty_ratio': tp_qty_ratio,
                }]
            for index, target in enumerate(list(raw_targets or []), 1):
                if not isinstance(target, dict):
                    continue
                try:
                    distance = float(target.get('distance', 0.0) or 0.0)
                    ratio = min(1.0, max(0.0, float(target.get('qty_ratio', 0.0) or 0.0)))
                except (TypeError, ValueError):
                    continue
                if distance <= 0 or ratio <= 0:
                    continue
                target_price = self.safe_price(
                    symbol,
                    entry_price + distance if side == 'long' else entry_price - distance
                )
                normalized_tp_targets.append({
                    'label': _normalize_tp_plan_label(target.get('label'), f'TP{index}'),
                    'kind': str(target.get('kind') or f'tp{index}'),
                    'tp_index': index,
                    'tp_label': _normalize_tp_plan_label(target.get('label'), f'TP{index}'),
                    'distance': distance,
                    'qty_ratio': ratio,
                    'raw_qty': raw_qty * ratio,
                    'price': target_price,
                })
            if preserve_runner_qty:
                residual_tp_qtys = []
                remaining_tp_qty = raw_qty
                for target in normalized_tp_targets:
                    desired_qty = min(max(0.0, float(target.get('raw_qty') or 0.0)), remaining_tp_qty)
                    qty_value = max(0.0, _safe_float_value(self.safe_amount(symbol, desired_qty), 0.0))
                    residual_tp_qtys.append(qty_value)
                    remaining_tp_qty = max(0.0, remaining_tp_qty - qty_value)
            else:
                residual_tp_qtys = _calculate_residual_tp_quantities(
                    symbol,
                    raw_qty,
                    [target.get('raw_qty') for target in normalized_tp_targets],
                    self.safe_amount,
                )
            for index, target in enumerate(normalized_tp_targets):
                target['qty'] = (
                    residual_tp_qtys[index]
                    if index < len(residual_tp_qtys)
                    else 0.0
                )
            normalized_tp_targets, tp_collapse = self._collapse_tp_targets_for_exchange(
                symbol,
                raw_qty,
                normalized_tp_targets,
            )
            if tp_collapse:
                if isinstance(tp_ladder_status, dict):
                    tp_ladder_status = dict(tp_ladder_status)
                    tp_ladder_status.update({
                        "ok": True,
                        "mode": tp_collapse.get("mode", "single_tp2_with_warning"),
                        "reason": tp_collapse.get("reason", "qty_too_small_for_tp1_tp2_split"),
                        "warnings": list(tp_ladder_status.get("warnings") or []) + [tp_collapse.get("reason", "qty_too_small_for_tp1_tp2_split")],
                    })
                logger.warning(
                    "[Protection] collapsed TP ladder for %s to one order: "
                    "qty=%.12f min_amount=%.12f",
                    symbol,
                    float(tp_collapse["total_qty"]),
                    float(tp_collapse["min_amount"]),
                )
            if isinstance(tp_ladder_status, dict) and normalized_tp_targets:
                placed_orders = []
                for target in normalized_tp_targets:
                    placed_orders.append({
                        "name": target.get("label") or target.get("tp_label") or "TP",
                        "price": target.get("price"),
                        "qty": target.get("qty"),
                        "rr": target.get("effective_r") or target.get("target_r"),
                    })
                tp_ladder_status["tp_orders"] = placed_orders
                tp_ladder_status["tp_targets"] = [dict(target) for target in normalized_tp_targets]
            if normalized_tp_targets:
                logger.info(
                    "[Protection] planned TP ladder for %s: entry=%.12f qty=%.12f targets=%s",
                    symbol,
                    entry_price,
                    raw_qty,
                    [
                        {
                            'label': target.get('label'),
                            'price': target.get('price'),
                            'qty': target.get('qty'),
                        }
                        for target in normalized_tp_targets
                    ],
                )
            tp_price = normalized_tp_targets[0]['price'] if normalized_tp_targets else None

            def _valid_price(direction, price_value):
                try:
                    price_float = float(price_value)
                except (TypeError, ValueError):
                    return False
                if direction == 'tp':
                    return price_float > entry_price if side == 'long' else price_float < entry_price
                # Managed winner stops may intentionally lock profit beyond
                # entry. Validate against the live mark, not average entry.
                reference_price = None
                if isinstance(pos, dict):
                    info = pos.get('info') if isinstance(pos.get('info'), dict) else {}
                    reference_price = _safe_float_or_none(
                        pos.get('markPrice')
                        or pos.get('mark_price')
                        or info.get('markPrice')
                        or pos.get('last')
                        or pos.get('currentPrice')
                    )
                reference_price = reference_price or entry_price
                return (
                    price_float < reference_price
                    if side == 'long'
                    else price_float > reference_price
                )

            # Stop Loss is placed first. A position without SL is the riskiest failure mode.
            if sl_price is not None:
                if not _valid_price('sl', sl_price):
                    await self._notify_protection_issue(
                        symbol,
                        'invalid_sl_price',
                        f"🚨 {self.ctrl.format_symbol_for_display(symbol)} SL 가격 오류: entry {entry_price:.6f}, SL {sl_price}"
                    )
                    await self._cancel_protection_orders(symbol, reason='invalid SL price')
                    self.last_protection_order_status[symbol] = {
                        'tp_expected': tp_price is not None,
                        'sl_expected': True,
                        'tp_present': False,
                        'sl_present': False,
                        'missing_tp': tp_price is not None,
                        'missing_sl': True,
                        'status': 'INVALID_SL_PRICE'
                    }
                    if emergency_close_on_sl_fail:
                        await self._fail_closed_unprotected_position(
                            symbol,
                            reason=f'invalid stop-loss price after rounding: entry={entry_price}, stop={sl_price}',
                            status_code='INVALID_SL_PRICE',
                            expected_tp=tp_price is not None,
                            emergency_close=True,
                        )
                    return
                else:
                    try:
                        sl_order = await self._create_protection_order_with_retries(
                            symbol,
                            'stop_market',
                            sl_side,
                            sl_qty,
                            None,
                            {
                                'stopPrice': sl_price,
                                'reduceOnly': True,
                                'newClientOrderId': self._build_protection_client_order_id(
                                    symbol,
                                    side,
                                    'sl',
                                    pos,
                                    trigger_price=sl_price,
                                    quantity=sl_qty,
                                    leg='sl',
                                    position_identity=(
                                        pos.get('entry_client_order_id')
                                        or pos.get('clientOrderId')
                                        or pos.get('timestamp')
                                        or self._protection_position_signature(pos)
                                    ),
                                    revision=f"place-{time.time_ns()}",
                                )
                            },
                            'SL',
                            max_attempts=sl_max_attempts,
                            retry_delay_sec=sl_retry_delay
                        )
                        logger.info(f"SL order placed: {sl_side.upper()} @ {sl_price} (stop)")
                    except Exception as sl_e:
                        logger.error(f"SL order failed after retries: {sl_e}")
                        await self._cancel_protection_orders(symbol, reason='SL placement failed')
                        await self._notify_protection_issue(
                            symbol,
                            'sl_place_failed',
                            f"🚨 {self.ctrl.format_symbol_for_display(symbol)} SL 주문 생성 실패({sl_max_attempts}회 재시도). "
                            f"{'즉시 시장가 청산을 시도합니다.' if emergency_close_on_sl_fail else '거래소에서 수동 확인하세요.'}: {sl_e}",
                            cooldown_sec=30
                        )
                        self.last_protection_order_status[symbol] = {
                            'tp_expected': tp_price is not None,
                            'sl_expected': True,
                            'tp_present': False,
                            'sl_present': False,
                            'missing_tp': tp_price is not None,
                            'missing_sl': True,
                            'status': 'SL_PLACE_FAILED'
                        }
                        if emergency_close_on_sl_fail:
                            close_status = await self._emergency_close_position_without_stop_loss(
                                symbol,
                                reason='SL placement failed after entry',
                                max_attempts=5
                            )
                            self.last_protection_order_status[symbol]['emergency_close_status'] = close_status.get('status')
                        return

            # Take Profit is allowed to be split; SL always covers the full current size.
            valid_tp_targets = []
            tp_order_ids = []
            tp_order_labels = {}
            for target in normalized_tp_targets:
                target_label = target.get('label') or 'TP'
                target_price = target.get('price')
                target_qty = target.get('qty')
                if float(target_qty) <= 0:
                    logger.warning(
                        f"{target_label} placement skipped: TP qty rounds to zero for "
                        f"{symbol}: ratio={target.get('qty_ratio')}"
                    )
                    continue
                if not _valid_price('tp', target_price):
                    await self._notify_protection_issue(
                        symbol,
                        'invalid_tp_price',
                        f"⚠️ {self.ctrl.format_symbol_for_display(symbol)} {target_label} 가격 오류: "
                        f"entry {entry_price:.6f}, TP {target_price}"
                    )
                    continue
                try:
                    target_order_type = str(
                        protection_cfg.get(f"{str(target_label).lower()}_order_type")
                        or protection_cfg.get("tp_order_type", "LIMIT")
                    ).upper()
                    order_type = 'take_profit_market' if target_order_type in {'TAKE_PROFIT_MARKET', 'TP_MARKET'} else 'limit'
                    order_price = None if order_type == 'take_profit_market' else target_price
                    order_params = {
                        'reduceOnly': True,
                        'newClientOrderId': self._build_protection_client_order_id(
                            symbol,
                            side,
                            target.get('kind') or 'tp',
                            pos,
                            trigger_price=target_price,
                            quantity=target_qty,
                            leg=target_label,
                            position_identity=(
                                pos.get('entry_client_order_id')
                                or pos.get('clientOrderId')
                                or pos.get('timestamp')
                                or self._protection_position_signature(pos)
                            ),
                        )
                    }
                    if order_type == 'take_profit_market':
                        order_params['stopPrice'] = target_price
                        order_params['closePosition'] = False
                    tp_order = await self._create_protection_order_with_retries(
                        symbol,
                        order_type,
                        tp_side,
                        target_qty,
                        order_price,
                        order_params,
                        target_label,
                        max_attempts=2
                    )
                    order_id = self._protection_order_id(tp_order)
                    client_order_id = self._protection_client_order_id(tp_order)
                    if order_id not in (None, ''):
                        target['order_id'] = str(order_id)
                        tp_order_ids.append(str(order_id))
                        tp_order_labels[str(order_id)] = str(target_label).upper()
                    if client_order_id:
                        target['client_order_id'] = str(client_order_id)
                    valid_tp_targets.append(target)
                    logger.info(
                        f"{target_label} order placed: {tp_side.upper()} @ {target_price} "
                        f"qty={target_qty} reduceOnly=True"
                    )
                except Exception as tp_e:
                    logger.error(f"{target_label} order failed: {tp_e}")
                    await self._notify_protection_issue(
                        symbol,
                        f"{str(target_label).lower()}_place_failed",
                        f"⚠️ {self.ctrl.format_symbol_for_display(symbol)} {target_label} 주문 생성 실패. SL은 유지됩니다: {tp_e}",
                        cooldown_sec=60
                    )

            self._persist_active_entry_protection_refs(
                symbol,
                stop_order_id=(
                    self._protection_order_id(sl_order) if sl_order else None
                ),
                take_profit_order_ids=tp_order_ids,
                take_profit_order_labels=tp_order_labels,
            )
            runner_state = self._get_utbreakout_trailing_state(symbol)
            if isinstance(runner_state, dict) and normalized_tp_targets:
                state_targets = self._planned_tp_orders_from_state(
                    symbol,
                    runner_state,
                )
                created_by_label = {
                    _normalize_tp_plan_label(item.get('tp_label') or item.get('label')): item
                    for item in normalized_tp_targets
                    if isinstance(item, dict)
                }
                for item in state_targets:
                    label = _normalize_tp_plan_label(
                        item.get('tp_label') or item.get('tp_name')
                    )
                    created = created_by_label.get(label)
                    if created:
                        if created.get('order_id'):
                            item['order_id'] = created['order_id']
                        if created.get('client_order_id'):
                            item['client_order_id'] = created['client_order_id']
                runner_state['planned_tp_orders'] = state_targets
                runner_state['tp_orders'] = list(state_targets)
                if sl_order:
                    runner_state['sl_order_id'] = self._protection_order_id(sl_order)
                self._set_utbreakout_trailing_state(symbol, runner_state)

            notice_parts = []
            if isinstance(tp_ladder_status, dict):
                notice_parts.append(
                    "TP Ladder: "
                    f"`mode={tp_ladder_status.get('mode')}`"
                    + (
                        f" / reason `{tp_ladder_status.get('reason')}`"
                        if tp_ladder_status.get('reason') else ""
                    )
                )
            for target in valid_tp_targets:
                notice_parts.append(
                    f"🎯 {target.get('label')}: `{float(target.get('price')):.2f}` "
                    f"x `{float(target.get('qty')):.6f}`"
                )
            if sl_order and sl_price is not None:
                notice_parts.append(f"🛑 SL: `{float(sl_price):.2f}` x `{float(sl_qty):.6f}`")
            if notice_parts:
                await self.ctrl.notify(" | ".join(notice_parts))

            audit_delay = max(
                0.5,
                min(
                    1.5,
                    float(protection_cfg.get('protection_audit_after_place_delay_sec', 0.5) or 0.5)
                )
            )
            await asyncio.sleep(audit_delay)
            position_fetch_ok, audit_pos = await self._fetch_server_position_checked(symbol)
            if not position_fetch_ok:
                logger.warning(
                    f"Protection audit skipped for {symbol}: position fetch failed after TP/SL placement"
                )
                return
            first_audit = await self._audit_protection_orders(
                symbol,
                pos=audit_pos,
                expected_tp=bool(valid_tp_targets) or bool(use_live_ladder and isinstance(tp_ladder_status, dict)),
                expected_sl=sl_price is not None,
                planned_tp_orders=normalized_tp_targets,
                alert=False
            )
            if isinstance(tp_ladder_status, dict):
                current_status = dict((getattr(self, 'last_protection_order_status', {}) or {}).get(symbol, {}) or {})
                current_status['tp_ladder'] = dict(tp_ladder_status)
                current_status['tp_ladder_mode'] = tp_ladder_status.get('mode')
                current_status['tp_ladder_reason'] = tp_ladder_status.get('reason')
                if tp_ladder_status.get('mode') == 'single_tp2_with_warning' and current_status.get('status') == 'OK':
                    current_status['status'] = 'OK_WITH_WARNING'
                self.last_protection_order_status[symbol] = current_status
            if (
                first_audit.get('fetch_ok', True)
                and (
                    first_audit.get('missing_sl')
                    or first_audit.get('missing_tp')
                    or first_audit.get('missing_tp1')
                    or first_audit.get('missing_tp2')
                )
            ):
                await asyncio.sleep(audit_delay)
                position_fetch_ok, audit_pos = await self._fetch_server_position_checked(symbol)
                if not position_fetch_ok:
                    logger.warning(
                        f"Protection re-audit skipped for {symbol}: position fetch failed"
                    )
                    return
                await self._audit_protection_orders(
                    symbol,
                    pos=audit_pos,
                    expected_tp=bool(valid_tp_targets) or bool(use_live_ladder and isinstance(tp_ladder_status, dict)),
                    expected_sl=sl_price is not None,
                    planned_tp_orders=normalized_tp_targets,
                    alert=True
                )

        except Exception as e:
            logger.exception("TP/SL order placement error for %s", symbol)
            if (
                locals().get('sl_distance') is not None
                and not locals().get('sl_order')
                and bool(locals().get('emergency_close_on_sl_fail', True))
            ):
                try:
                    await self._fail_closed_unprotected_position(
                        symbol,
                        reason=f'unhandled protection setup error: {type(e).__name__}: {e}',
                        status_code='PROTECTION_SETUP_EXCEPTION',
                        expected_tp=bool(locals().get('tp_targets') or locals().get('tp_distance')),
                        emergency_close=True,
                    )
                except Exception:
                    logger.exception("Emergency close after protection setup exception failed for %s", symbol)

    async def exit_position(self, symbol, reason):
        logger.info(f"?뱾 [Signal] Attempting exit: {reason}")
        if self.is_upbit_mode():
            await self._exit_upbit_spot(symbol, reason)
            return

        # 癒쇱? TP/SL 二쇰Ц 痍⑥냼 (?덈뒗 寃쎌슦)
        # 보호주문은 포지션 조회가 성공하고 실제 청산이 확인될 때까지 유지한다.
        self.position_cache = None
        position_fetch_ok, pos = await self._fetch_server_position_checked(symbol)
        if not position_fetch_ok:
            logger.error(f"Exit skipped for {symbol}: position status could not be confirmed")
            await self.ctrl.notify(
                f"⚠️ {self.ctrl.format_symbol_for_display(symbol)} 청산 보류: "
                "포지션 조회 실패로 기존 TP/SL을 유지합니다."
            )
            return
        if not pos:
            logger.info("No position to exit")
            await self._cancel_protection_orders(symbol, reason='exit requested but no position')
            return

        contracts = abs(float(pos.get('contracts', 0) or 0))
        if contracts <= 0:
            logger.info("No contracts to exit")
            await self._cancel_protection_orders(symbol, reason='exit requested but zero contracts')
            return

        initial_pos = dict(pos)
        pnl = float(initial_pos.get('unrealizedPnl', 0) or 0)
        pnl_pct = float(initial_pos.get('percentage', 0) or 0)
        max_retries = 4
        order = None
        last_error = None
        remaining_pos = pos

        for attempt in range(1, max_retries + 1):
            try:
                remaining_contracts = abs(float(remaining_pos.get('contracts', 0) or 0))
                if remaining_contracts <= 0:
                    remaining_pos = None
                    break
                qty = self.safe_amount(symbol, remaining_contracts)
                if float(qty) <= 0:
                    raise ValueError(f"invalid close qty: {qty}")
                side = 'sell' if remaining_pos['side'] == 'long' else 'buy'
                params = self._close_order_params_for_position(remaining_pos)
                logger.info(
                    f"Exit params attempt {attempt}/{max_retries}: "
                    f"{side} {qty} (position: {remaining_pos['side']} {remaining_contracts})"
                )
                _ensure_trading_safety_runtime(self)
                close_submission = await self.crypto_execution.submit_reduce_only_close(
                    strategy='SIGNAL_ENGINE_EXIT',
                    symbol=symbol,
                    position_side=str(remaining_pos.get('side') or ''),
                    position_signature=(
                        initial_pos.get('timestamp')
                        or initial_pos.get('entryPrice')
                        or f"{initial_pos.get('side')}:{contracts}"
                    ),
                    qty=qty,
                    reason=reason,
                    params=params,
                )
                if close_submission.state == OrderState.SUBMITTED_UNKNOWN.value:
                    raise RuntimeError(
                        f"close order state unknown: {close_submission.client_order_id}"
                    )
                order = close_submission.order or {}
            except Exception as e:
                last_error = e
                logger.error(f"Exit attempt {attempt}/{max_retries} failed: {e}")
                if attempt < max_retries:
                    await asyncio.sleep(1)  # 1珥??湲????ъ떆??
                    await self.ctrl.notify(f"⚠️ 청산 재시도 중... ({attempt + 1}/{max_retries})")
                continue

            await asyncio.sleep(0.8)
            self.position_cache = None
            self.position_cache_time = 0
            remaining_fetch_ok, remaining_pos = await self._fetch_server_position_checked(symbol)
            if not remaining_fetch_ok:
                logger.error(
                    f"Exit order accepted but remaining position could not be verified: {symbol}"
                )
                await self.ctrl.notify(
                    f"⚠️ {self.ctrl.format_symbol_for_display(symbol)} 청산 주문 접수 후 "
                    "포지션 재조회 실패. 기존 보호주문을 유지하고 수동 확인이 필요합니다."
                )
                return
            if not remaining_pos:
                break

            remaining_qty = abs(float(remaining_pos.get('contracts', 0) or 0))
            logger.warning(
                f"Exit order accepted but position still open: {symbol} "
                f"{remaining_pos.get('side')} {remaining_qty}"
            )
            if attempt < max_retries:
                await self.ctrl.notify(
                    f"⚠️ 청산 주문 후 잔여 포지션 확인: {self.ctrl.format_symbol_for_display(symbol)} "
                    f"{remaining_pos.get('side', '').upper()} `{remaining_qty}`. 재시도합니다."
                )

        if remaining_pos:
            remaining_qty = abs(float(remaining_pos.get('contracts', 0) or 0))
            logger.error(
                f"Exit failed to flatten {symbol} after {max_retries} attempts: "
                f"{remaining_pos.get('side')} {remaining_qty}"
            )
            await self._audit_protection_orders(symbol, pos=remaining_pos, alert=True)
            await self.ctrl.notify(
                f"🚨 청산 미완료: {self.ctrl.format_symbol_for_display(symbol)} "
                f"{remaining_pos.get('side', '').upper()} `{remaining_qty}` 잔여. 거래소에서 즉시 수동 확인하세요."
            )
            return

        if not order:
            logger.error(f"Exit failed after {max_retries} attempts: {last_error}")
            await self.ctrl.notify(f"🚨 청산 실패! 즉시 수동 청산이 필요합니다: {last_error}")
            return

        exit_price = (
            float(order.get('average', 0))
            if isinstance(order, dict) and order.get('average')
            else float(initial_pos.get('markPrice', 0) or 0)
        )

        # 罹먯떆 ?꾩쟾 臾댄슚??
        self.position_cache = None
        self.position_cache_time = 0
        await self._cancel_all_orders_variants(symbol, reason='after exit order success')
        await self._cancel_protection_orders(symbol, reason='after exit order success')
        await self._reconcile_closed_position_protection(
            symbol,
            reason='after exit flat confirmed',
            alert=True,
            attempts=3
        )

        active_strategy = str(self.get_runtime_strategy_params().get('active_strategy', '') or '').lower()
        if active_strategy == 'utsmc' or str(reason or '').startswith('UTSMC'):
            self._clear_utsmc_pending_entry(symbol)
            self._clear_utsmc_entry_invalidation(symbol)
            self._clear_utsmc_fixed_exit_ob(symbol)
            self.utsmc_last_entry_signal_ts.pop(symbol, None)

        accounting_state = self._get_utbreakout_trailing_state(symbol)
        await self._record_closed_trade_accounting(
            symbol,
            reason,
            exit_price=exit_price,
            state=accounting_state,
        )
        _mark_crypto_symbol_closed(self, symbol, reason)
        self._clear_utbreakout_trailing_state(symbol, finalize=True, reason=reason, exit_price=exit_price)
        self._clear_aggressive_growth_position(symbol)
        await self.ctrl.notify(
            self._build_signal_exit_notice(symbol, pos, reason, pnl, pnl_pct, exit_price)
        )
        logger.info(f"??Exit order success: {order.get('id', 'N/A')}")

    async def _exit_upbit_spot(self, symbol, reason):
        logger.info(f"[Upbit] Attempting spot exit: {reason}")

        try:
            try:
                open_orders = await asyncio.to_thread(self.exchange.fetch_open_orders, symbol)
                for open_order in open_orders or []:
                    try:
                        await asyncio.to_thread(self.exchange.cancel_order, open_order['id'], symbol)
                    except Exception as cancel_e:
                        logger.warning(f"Upbit open order cancel failed for {symbol}: {cancel_e}")
            except Exception as fetch_e:
                logger.debug(f"Upbit open-order fetch skipped for {symbol}: {fetch_e}")

            self.position_cache = None
            pos = await self.get_server_position(symbol, use_cache=False)
            if not pos:
                logger.info("No Upbit spot position to exit")
                return

            contracts = abs(float(pos.get('contracts', 0) or 0))
            if contracts <= 0:
                logger.info("No Upbit contracts to exit")
                return

            cfg = self.get_runtime_common_settings()
            min_order_krw = max(5000.0, float(cfg.get('min_order_krw', 5000.0) or 5000.0))
            est_price = float(pos.get('markPrice', 0) or pos.get('entryPrice', 0) or 0)
            est_notional = contracts * est_price
            if est_price > 0 and est_notional < min_order_krw:
                display_symbol = self.ctrl.format_symbol_for_display(symbol)
                await self.ctrl.notify(
                    f"ℹ️ [Upbit {reason}] {display_symbol}\n보유 평가금액이 최소 주문금액 `{min_order_krw:,.0f} KRW` 미만이라 자동 매도가 불가합니다."
                )
                logger.info(f"[Upbit] Exit skipped below min notional: {symbol} value={est_notional:,.0f} KRW")
                return

            qty = self.safe_amount(symbol, contracts)
            order = await asyncio.to_thread(
                self.exchange.create_order,
                symbol,
                'market',
                'sell',
                float(qty)
            )

            pnl = float(pos.get('unrealizedPnl', 0) or 0)
            pnl_pct = float(pos.get('percentage', 0) or 0)
            exit_price = float(order.get('average', 0) or 0) or float(pos.get('markPrice', 0) or 0)

            self.position_cache = None
            self.position_cache_time = 0
            self.db.log_trade_close(symbol, pnl, pnl_pct, exit_price, reason)

            display_symbol = self.ctrl.format_symbol_for_display(symbol)
            await self.ctrl.notify(
                f"📊 [Upbit {reason}] {display_symbol}\nPnL: {pnl:+,.0f} KRW ({pnl_pct:+.2f}%)"
            )
            logger.info(f"[Upbit] Exit order success: {order.get('id', 'N/A')}")
        except Exception as e:
            logger.error(f"Upbit spot exit error: {e}")
            await self.ctrl.notify(f"❌ 업비트 매도 실패: {e}")
