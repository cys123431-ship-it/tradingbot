"""SMC, RSI, Bollinger, hybrid, and Cameron strategy calculations."""

from __future__ import annotations


class SignalSecondaryStrategiesMixin:
    def _calculate_smc_structure_scope(self, closed, size, scope_name):
        result = {
            'scope_name': str(scope_name or '').lower(),
            'size': max(2, int(size or 2)),
            'trend_bias': 0,
            'trend_label': 'neutral',
            'bullish_types': [],
            'bearish_types': [],
            'pivot_high_level': None,
            'pivot_low_level': None,
            'pivot_high_ts': None,
            'pivot_low_ts': None
        }

        if closed is None or len(closed) < (result['size'] + 3):
            return result

        highs = closed['high'].astype(float).tolist()
        lows = closed['low'].astype(float).tolist()
        closes = closed['close'].astype(float).tolist()
        times = closed['timestamp'].astype(int).tolist()
        n = len(closed)

        pivot_high = None
        pivot_low = None
        trend_bias = 0

        for bar_index in range(n):
            confirm_idx = bar_index - result['size']
            if confirm_idx >= 0:
                future_highs = highs[confirm_idx + 1:bar_index + 1]
                future_lows = lows[confirm_idx + 1:bar_index + 1]
                center_high = highs[confirm_idx]
                center_low = lows[confirm_idx]

                if len(future_highs) == result['size'] and center_high > max(future_highs):
                    pivot_high = {
                        'level': center_high,
                        'ts': times[confirm_idx],
                        'crossed': False
                    }
                if len(future_lows) == result['size'] and center_low < min(future_lows):
                    pivot_low = {
                        'level': center_low,
                        'ts': times[confirm_idx],
                        'crossed': False
                    }

            close_price = closes[bar_index]
            if pivot_high and not pivot_high.get('crossed') and close_price > float(pivot_high.get('level', 0.0)):
                structure_type = 'choch' if trend_bias == -1 else 'bos'
                trend_bias = 1
                pivot_high['crossed'] = True
                if bar_index == (n - 1):
                    result['bullish_types'].append(f"{result['scope_name']}_{structure_type}")

            if pivot_low and not pivot_low.get('crossed') and close_price < float(pivot_low.get('level', 0.0)):
                structure_type = 'choch' if trend_bias == 1 else 'bos'
                trend_bias = -1
                pivot_low['crossed'] = True
                if bar_index == (n - 1):
                    result['bearish_types'].append(f"{result['scope_name']}_{structure_type}")

        result['trend_bias'] = trend_bias
        result['trend_label'] = 'bullish' if trend_bias == 1 else 'bearish' if trend_bias == -1 else 'neutral'
        result['pivot_high_level'] = pivot_high.get('level') if pivot_high else None
        result['pivot_low_level'] = pivot_low.get('level') if pivot_low else None
        result['pivot_high_ts'] = pivot_high.get('ts') if pivot_high else None
        result['pivot_low_ts'] = pivot_low.get('ts') if pivot_low else None
        return result

    def _calculate_smc_structure_signal(self, df, strategy_params):
        cfg = strategy_params.get('UTSMC', {})
        internal_length = max(2, int(cfg.get('internal_length', 5) or 5))
        swing_length = max(internal_length + 1, int(cfg.get('swing_length', 50) or 50))

        closed = df.iloc[:-1].copy().reset_index(drop=True)
        min_bars = max(swing_length + 5, internal_length + 5, 40)
        if len(closed) < min_bars:
            return None, "SMC 구조 데이터 부족", {
                'internal_length': internal_length,
                'swing_length': swing_length,
                'bullish_structure': False,
                'bearish_structure': False,
                'bullish_structure_types': [],
                'bearish_structure_types': [],
                'structure_tags': [],
                'signal_ts': int(closed.iloc[-1]['timestamp']) if len(closed) else 0
            }

        internal_result = self._calculate_smc_structure_scope(closed, internal_length, 'internal')
        swing_result = self._calculate_smc_structure_scope(closed, swing_length, 'swing')

        bullish_types = list(internal_result.get('bullish_types', [])) + list(swing_result.get('bullish_types', []))
        bearish_types = list(internal_result.get('bearish_types', [])) + list(swing_result.get('bearish_types', []))
        bullish_structure = bool(bullish_types)
        bearish_structure = bool(bearish_types)
        structure_tags = bullish_types + bearish_types
        signal_ts = int(closed.iloc[-1]['timestamp']) if len(closed) else 0

        detail = {
            'internal_length': internal_length,
            'swing_length': swing_length,
            'bullish_structure': bullish_structure,
            'bearish_structure': bearish_structure,
            'bullish_structure_types': bullish_types,
            'bearish_structure_types': bearish_types,
            'structure_tags': structure_tags,
            'signal_ts': signal_ts,
            'internal_trend_label': internal_result.get('trend_label'),
            'swing_trend_label': swing_result.get('trend_label'),
            'internal_pivot_high_level': internal_result.get('pivot_high_level'),
            'internal_pivot_low_level': internal_result.get('pivot_low_level'),
            'swing_pivot_high_level': swing_result.get('pivot_high_level'),
            'swing_pivot_low_level': swing_result.get('pivot_low_level')
        }

        if bullish_structure:
            reason = f"SMC BULLISH STRUCTURE: {', '.join(bullish_types)}"
            return 'bullish', reason, detail

        if bearish_structure:
            reason = f"SMC BEARISH STRUCTURE: {', '.join(bearish_types)}"
            return 'bearish', reason, detail

        reason = (
            f"SMC 대기: internal={internal_result.get('trend_label', 'neutral')}, "
            f"swing={swing_result.get('trend_label', 'neutral')}"
        )
        return None, reason, detail

    def _calculate_rma_series(self, values, length):
        series = pd.Series(np.nan, index=range(len(values)), dtype=float)
        if values is None or len(values) < length or length <= 0:
            return series
        base = pd.Series(values, dtype=float)
        series.iloc[length - 1] = float(base.iloc[:length].mean())
        for idx in range(length, len(base)):
            prev_val = float(series.iloc[idx - 1])
            series.iloc[idx] = ((prev_val * (length - 1)) + float(base.iloc[idx])) / length
        return series

    def _update_luxalgo_structure_pivot_state(self, bar_index, size, state, highs, lows, times):
        if size <= 0 or bar_index < size:
            return

        pivot_index = bar_index - size
        future_highs = highs[pivot_index + 1:bar_index + 1]
        future_lows = lows[pivot_index + 1:bar_index + 1]
        if len(future_highs) != size or len(future_lows) != size:
            return

        leg_val = int(state.get('leg', 0) or 0)
        prev_leg = leg_val
        if highs[pivot_index] > max(future_highs):
            leg_val = 0
        elif lows[pivot_index] < min(future_lows):
            leg_val = 1
        state['leg'] = leg_val

        leg_change = leg_val - prev_leg
        if leg_change == 1:
            pivot = state['low']
            pivot['last_level'] = pivot.get('current_level')
            pivot['current_level'] = float(lows[pivot_index])
            pivot['crossed'] = False
            pivot['bar_time'] = int(times[pivot_index])
            pivot['bar_index'] = int(pivot_index)
        elif leg_change == -1:
            pivot = state['high']
            pivot['last_level'] = pivot.get('current_level')
            pivot['current_level'] = float(highs[pivot_index])
            pivot['crossed'] = False
            pivot['bar_time'] = int(times[pivot_index])
            pivot['bar_index'] = int(pivot_index)

    def _store_luxalgo_order_block(self, order_blocks, pivot, current_bar_index, parsed_highs, parsed_lows, times, bias):
        start_idx = pivot.get('bar_index')
        if start_idx is None:
            return
        start_idx = int(start_idx)
        if start_idx < 0 or start_idx >= current_bar_index:
            return

        if bias == -1:
            source_slice = parsed_highs[start_idx:current_bar_index]
            if not source_slice:
                return
            rel_idx = source_slice.index(max(source_slice))
        else:
            source_slice = parsed_lows[start_idx:current_bar_index]
            if not source_slice:
                return
            rel_idx = source_slice.index(min(source_slice))

        parsed_index = start_idx + rel_idx
        order_blocks.insert(0, {
            'top': float(parsed_highs[parsed_index]),
            'bottom': float(parsed_lows[parsed_index]),
            'origin_ts': int(times[parsed_index]),
            'created_ts': int(times[current_bar_index]),
            'bias': int(bias)
        })
        if len(order_blocks) > 100:
            del order_blocks[100:]

    def _delete_luxalgo_order_blocks(self, order_blocks, high_price, low_price, close_price, mitigation_mode):
        mitigation_key = str(mitigation_mode or 'highlow').lower()
        bearish_source = float(close_price) if mitigation_key == 'close' else float(high_price)
        bullish_source = float(close_price) if mitigation_key == 'close' else float(low_price)

        active_blocks = []
        for order_block in order_blocks:
            bias = int(order_block.get('bias', 0) or 0)
            top = float(order_block.get('top', 0.0) or 0.0)
            bottom = float(order_block.get('bottom', 0.0) or 0.0)
            crossed = (
                (bias == -1 and bearish_source > top) or
                (bias == 1 and bullish_source < bottom)
            )
            if not crossed:
                active_blocks.append(order_block)
        return active_blocks

    def _calculate_luxalgo_internal_ob_state(self, closed, internal_length, swing_length, use_confluence_filter=False, order_block_filter='atr', mitigation_mode='highlow'):
        detail = {
            'trend_bias': 0,
            'trend_label': 'neutral',
            'active_bullish_obs': [],
            'active_bearish_obs': [],
            'matched_bullish_ob': None,
            'matched_bearish_ob': None
        }
        if closed is None or len(closed) < max(swing_length + 5, internal_length + 5, 40):
            return detail

        opens = closed['open'].astype(float).tolist()
        highs = closed['high'].astype(float).tolist()
        lows = closed['low'].astype(float).tolist()
        closes = closed['close'].astype(float).tolist()
        times = closed['timestamp'].astype(int).tolist()
        n = len(closed)

        prev_close = pd.Series(closes, dtype=float).shift(1)
        true_range = pd.concat([
            (closed['high'].astype(float) - closed['low'].astype(float)).abs(),
            (closed['high'].astype(float) - prev_close).abs(),
            (closed['low'].astype(float) - prev_close).abs()
        ], axis=1).max(axis=1).fillna(0.0)
        atr_measure = self._calculate_rma_series(true_range.tolist(), 200)
        range_measure = true_range.cumsum() / np.arange(1, n + 1)

        filter_key = str(order_block_filter or 'atr').lower()
        volatility_measure = range_measure if filter_key == 'range' else atr_measure

        parsed_highs = []
        parsed_lows = []
        for idx in range(n):
            high_val = float(highs[idx])
            low_val = float(lows[idx])
            vol_val = volatility_measure.iloc[idx] if hasattr(volatility_measure, 'iloc') else volatility_measure[idx]
            high_volatility_bar = bool(np.isfinite(vol_val) and (high_val - low_val) >= (2.0 * float(vol_val)))
            parsed_highs.append(low_val if high_volatility_bar else high_val)
            parsed_lows.append(high_val if high_volatility_bar else low_val)

        def _new_state():
            return {
                'leg': 0,
                'trend_bias': 0,
                'high': {
                    'current_level': None,
                    'last_level': None,
                    'crossed': False,
                    'bar_time': None,
                    'bar_index': None
                },
                'low': {
                    'current_level': None,
                    'last_level': None,
                    'crossed': False,
                    'bar_time': None,
                    'bar_index': None
                }
            }

        swing_state = _new_state()
        internal_state = _new_state()
        internal_order_blocks = []

        for bar_index in range(n):
            self._update_luxalgo_structure_pivot_state(bar_index, swing_length, swing_state, highs, lows, times)
            self._update_luxalgo_structure_pivot_state(bar_index, internal_length, internal_state, highs, lows, times)

            upper_wick = float(highs[bar_index]) - max(float(closes[bar_index]), float(opens[bar_index]))
            lower_wick = min(float(closes[bar_index]), float(opens[bar_index])) - float(lows[bar_index])
            bullish_bar = True
            bearish_bar = True
            if use_confluence_filter:
                bullish_bar = upper_wick > lower_wick
                bearish_bar = upper_wick < lower_wick

            if bar_index >= 1:
                internal_high = internal_state['high']
                high_level = internal_high.get('current_level')
                swing_high_level = swing_state['high'].get('current_level')
                extra_bullish = bullish_bar and (
                    swing_high_level is None or high_level is None or not np.isclose(float(high_level), float(swing_high_level))
                )
                if (
                    high_level is not None
                    and not internal_high.get('crossed')
                    and float(closes[bar_index - 1]) <= float(high_level)
                    and float(closes[bar_index]) > float(high_level)
                    and extra_bullish
                ):
                    internal_high['crossed'] = True
                    internal_state['trend_bias'] = 1
                    self._store_luxalgo_order_block(
                        internal_order_blocks,
                        internal_high,
                        bar_index,
                        parsed_highs,
                        parsed_lows,
                        times,
                        bias=1
                    )

                internal_low = internal_state['low']
                low_level = internal_low.get('current_level')
                swing_low_level = swing_state['low'].get('current_level')
                extra_bearish = bearish_bar and (
                    swing_low_level is None or low_level is None or not np.isclose(float(low_level), float(swing_low_level))
                )
                if (
                    low_level is not None
                    and not internal_low.get('crossed')
                    and float(closes[bar_index - 1]) >= float(low_level)
                    and float(closes[bar_index]) < float(low_level)
                    and extra_bearish
                ):
                    internal_low['crossed'] = True
                    internal_state['trend_bias'] = -1
                    self._store_luxalgo_order_block(
                        internal_order_blocks,
                        internal_low,
                        bar_index,
                        parsed_highs,
                        parsed_lows,
                        times,
                        bias=-1
                    )

            internal_order_blocks = self._delete_luxalgo_order_blocks(
                internal_order_blocks,
                highs[bar_index],
                lows[bar_index],
                closes[bar_index],
                mitigation_mode
            )

        curr_close = float(closes[-1])
        active_bullish_obs = [ob for ob in internal_order_blocks if int(ob.get('bias', 0) or 0) == 1]
        active_bearish_obs = [ob for ob in internal_order_blocks if int(ob.get('bias', 0) or 0) == -1]
        matched_bullish = next(
            (
                ob for ob in active_bullish_obs
                if float(ob.get('bottom', curr_close + 1.0)) <= curr_close <= float(ob.get('top', curr_close - 1.0))
            ),
            None
        )
        matched_bearish = next(
            (
                ob for ob in active_bearish_obs
                if float(ob.get('bottom', curr_close + 1.0)) <= curr_close <= float(ob.get('top', curr_close - 1.0))
            ),
            None
        )

        detail.update({
            'trend_bias': int(internal_state.get('trend_bias', 0) or 0),
            'trend_label': 'bullish' if internal_state.get('trend_bias') == 1 else 'bearish' if internal_state.get('trend_bias') == -1 else 'neutral',
            'active_bullish_obs': active_bullish_obs,
            'active_bearish_obs': active_bearish_obs,
            'matched_bullish_ob': matched_bullish,
            'matched_bearish_ob': matched_bearish
        })
        return detail

    def _calculate_smc_internal_ob_signal(self, df, strategy_params):
        cfg = strategy_params.get('UTSMC', {})
        internal_length = max(2, int(cfg.get('internal_length', 5) or 5))
        swing_length = max(internal_length + 1, int(cfg.get('swing_length', 50) or 50))
        use_confluence_filter = bool(cfg.get('use_confluence_filter', False))
        order_block_filter = str(cfg.get('order_block_filter', 'atr') or 'atr')
        mitigation_mode = str(cfg.get('order_block_mitigation', 'highlow') or 'highlow')

        closed = df.iloc[:-1].copy().reset_index(drop=True)
        min_bars = max(swing_length + 5, internal_length + 10, 40)
        if len(closed) < min_bars:
            return None, "SMC internal OB 데이터 부족", {
                'internal_length': internal_length,
                'swing_length': swing_length,
                'bullish_internal_ob_entry': False,
                'bearish_internal_ob_entry': False,
                'bullish_ob_top': None,
                'bullish_ob_bottom': None,
                'bearish_ob_top': None,
                'bearish_ob_bottom': None,
                'signal_ts': int(closed.iloc[-1]['timestamp']) if len(closed) else 0
            }

        times = closed['timestamp'].astype(int).tolist()
        closes = closed['close'].astype(float).tolist()
        curr_close = float(closes[-1])
        lux_state = self._calculate_luxalgo_internal_ob_state(
            closed,
            internal_length,
            swing_length,
            use_confluence_filter=use_confluence_filter,
            order_block_filter=order_block_filter,
            mitigation_mode=mitigation_mode
        )
        matched_bullish_ob = lux_state.get('matched_bullish_ob')
        matched_bearish_ob = lux_state.get('matched_bearish_ob')
        latest_bullish_ob = matched_bullish_ob or next(iter(lux_state.get('active_bullish_obs') or []), None)
        latest_bearish_ob = matched_bearish_ob or next(iter(lux_state.get('active_bearish_obs') or []), None)
        bullish_internal_ob_entry = matched_bullish_ob is not None
        bearish_internal_ob_entry = matched_bearish_ob is not None

        detail = {
            'internal_length': internal_length,
            'swing_length': swing_length,
            'trend_label': lux_state.get('trend_label', 'neutral'),
            'signal_ts': int(times[-1]),
            'curr_close': curr_close,
            'bullish_internal_ob_entry': bullish_internal_ob_entry,
            'bearish_internal_ob_entry': bearish_internal_ob_entry,
            'active_bullish_obs': list(lux_state.get('active_bullish_obs') or []),
            'active_bearish_obs': list(lux_state.get('active_bearish_obs') or []),
            'bullish_ob_top': latest_bullish_ob.get('top') if latest_bullish_ob else None,
            'bullish_ob_bottom': latest_bullish_ob.get('bottom') if latest_bullish_ob else None,
            'bearish_ob_top': latest_bearish_ob.get('top') if latest_bearish_ob else None,
            'bearish_ob_bottom': latest_bearish_ob.get('bottom') if latest_bearish_ob else None,
            'bullish_ob_origin_ts': latest_bullish_ob.get('origin_ts') if latest_bullish_ob else None,
            'bearish_ob_origin_ts': latest_bearish_ob.get('origin_ts') if latest_bearish_ob else None,
            'bullish_ob_created_ts': latest_bullish_ob.get('created_ts') if latest_bullish_ob else None,
            'bearish_ob_created_ts': latest_bearish_ob.get('created_ts') if latest_bearish_ob else None,
            'active_bullish_ob_count': len(lux_state.get('active_bullish_obs') or []),
            'active_bearish_ob_count': len(lux_state.get('active_bearish_obs') or []),
            'order_block_filter': order_block_filter,
            'order_block_mitigation': mitigation_mode,
            'ob_tags': [tag for tag, active in (
                ('internal_bullish_ob_entry', bullish_internal_ob_entry),
                ('internal_bearish_ob_entry', bearish_internal_ob_entry),
            ) if active]
        }

        if bullish_internal_ob_entry:
            reason = (
                f"SMC INTERNAL BULLISH OB ENTRY: "
                f"{float(latest_bullish_ob.get('bottom')):.4f} ~ {float(latest_bullish_ob.get('top')):.4f}"
            )
            return 'bullish_ob_entry', reason, detail

        if bearish_internal_ob_entry:
            reason = (
                f"SMC INTERNAL BEARISH OB ENTRY: "
                f"{float(latest_bearish_ob.get('bottom')):.4f} ~ {float(latest_bearish_ob.get('top')):.4f}"
            )
            return 'bearish_ob_entry', reason, detail

        reason = (
            f"SMC internal OB 대기: trend={detail['trend_label']}, "
            f"bull_ob={'Y' if latest_bullish_ob else 'N'}, bear_ob={'Y' if latest_bearish_ob else 'N'}"
        )
        return None, reason, detail

    def _set_utsmc_pending_entry(self, symbol, side, signal_ts, execute_ts, signal_high=None, signal_low=None):
        if side not in {'long', 'short'}:
            return
        self.utsmc_pending_entries[symbol] = {
            'side': side,
            'signal_ts': int(signal_ts or 0),
            'execute_ts': int(execute_ts or 0),
            'signal_high': float(signal_high) if signal_high is not None else None,
            'signal_low': float(signal_low) if signal_low is not None else None
        }

    def _get_utsmc_pending_entry(self, symbol):
        return self.utsmc_pending_entries.get(symbol)

    def _clear_utsmc_pending_entry(self, symbol):
        self.utsmc_pending_entries.pop(symbol, None)

    def _set_utsmc_entry_invalidation(self, symbol, side, signal_ts, signal_high=None, signal_low=None):
        if side not in {'long', 'short'}:
            return
        self.utsmc_entry_invalidation[symbol] = {
            'side': str(side).lower(),
            'signal_ts': int(signal_ts or 0),
            'signal_high': float(signal_high) if signal_high is not None else None,
            'signal_low': float(signal_low) if signal_low is not None else None
        }

    def _get_utsmc_entry_invalidation(self, symbol):
        return self.utsmc_entry_invalidation.get(symbol)

    def _clear_utsmc_entry_invalidation(self, symbol):
        self.utsmc_entry_invalidation.pop(symbol, None)

    def _set_utsmc_fixed_exit_ob(self, symbol, position_side, ob_side, ob_data, tf=None):
        if position_side not in {'long', 'short'} or ob_side not in {'bullish', 'bearish'}:
            return
        if not isinstance(ob_data, dict):
            return
        top = ob_data.get('top')
        bottom = ob_data.get('bottom')
        if top is None or bottom is None:
            return
        self.utsmc_fixed_exit_obs[symbol] = {
            'position_side': str(position_side).lower(),
            'ob_side': str(ob_side).lower(),
            'top': float(top),
            'bottom': float(bottom),
            'origin_ts': int(ob_data.get('origin_ts') or 0),
            'created_ts': int(ob_data.get('created_ts') or 0),
            'tf': tf
        }

    def _get_utsmc_fixed_exit_ob(self, symbol):
        return self.utsmc_fixed_exit_obs.get(symbol)

    def _clear_utsmc_fixed_exit_ob(self, symbol):
        self.utsmc_fixed_exit_obs.pop(symbol, None)

    async def _maybe_execute_utsmc_live_entry(self, symbol, live_candle_ts, live_price, pos_side, strategy_name='UTSMC'):
        pending = self._get_utsmc_pending_entry(symbol)
        if pos_side != 'NONE':
            if pending:
                self._clear_utsmc_pending_entry(symbol)
            return pos_side
        if not pending:
            return pos_side

        execute_ts = int(pending.get('execute_ts') or 0)
        if live_candle_ts < execute_ts:
            return pos_side

        timing_mode = self._get_ut_entry_timing_mode()
        if timing_mode == 'next_candle' and live_candle_ts > execute_ts:
            self._clear_utsmc_pending_entry(symbol)
            return pos_side

        entry_sig = str(pending.get('side') or '').lower()
        if entry_sig not in {'long', 'short'}:
            self._clear_utsmc_pending_entry(symbol)
            return pos_side

        timing_label = self._get_ut_entry_timing_label(timing_mode)
        if not self.is_trade_direction_allowed(entry_sig):
            block_reason = self.format_trade_direction_block_reason(entry_sig)
            self._clear_utsmc_pending_entry(symbol)
            self.last_entry_reason[symbol] = f"{strategy_name} {block_reason}"
            self._update_stateful_diag(
                symbol,
                stage='entry_blocked',
                strategy=strategy_name,
                entry_sig=entry_sig,
                pos_side='NONE',
                note=f"live {timing_mode} entry blocked by direction filter"
            )
            return pos_side

        self._clear_utsmc_pending_entry(symbol)
        self.last_entry_reason[symbol] = f"{strategy_name} {timing_label} 기준 {entry_sig.upper()} 진입"
        self._update_stateful_diag(
            symbol,
            stage='entry_submitted',
            strategy=strategy_name,
            entry_sig=entry_sig,
            pos_side=entry_sig.upper(),
            note=(
                f"live {timing_mode} entry | live_ts={live_candle_ts} | armed_ts={execute_ts} | "
                f"ut_signal_high={pending.get('signal_high')} | ut_signal_low={pending.get('signal_low')}"
            )
        )
        logger.info(f"[{strategy_name}] live {timing_mode} entry {entry_sig.upper()} @ {live_candle_ts}")
        self.utsmc_last_entry_signal_ts[symbol] = int(live_candle_ts)
        self._clear_utsmc_fixed_exit_ob(symbol)
        self._set_utsmc_entry_invalidation(
            symbol,
            entry_sig,
            pending.get('signal_ts'),
            signal_high=pending.get('signal_high'),
            signal_low=pending.get('signal_low')
        )
        await self.entry(symbol, entry_sig, float(live_price))
        return entry_sig.upper()

    def _normalize_utsmc_candidate_filter_mode(self, mode):
        mode_raw = str(mode or 'off').strip().lower()
        aliases = {
            '0': 'off',
            'off': 'off',
            'none': 'off',
            '1': 'candidate1',
            'c1': 'candidate1',
            'candidate1': 'candidate1',
            '2': 'candidate2',
            'c2': 'candidate2',
            'candidate2': 'candidate2',
            '3': 'candidate12',
            '12': 'candidate12',
            'c12': 'candidate12',
            'candidate12': 'candidate12',
            'both': 'candidate12'
        }
        return aliases.get(mode_raw, 'off')

    def _format_utsmc_candidate_filter_mode(self, mode):
        normalized = self._normalize_utsmc_candidate_filter_mode(mode)
        return {
            'off': 'OFF',
            'candidate1': 'C1',
            'candidate2': 'C2',
            'candidate12': 'C1+C2'
        }.get(normalized, 'OFF')

    def _calculate_utsmc_candidate1_filter(self, df, strategy_params):
        cfg = (strategy_params.get('UTSMC', {}) or {}).get('candidate_filter', {}) or {}
        release_window = max(1, int(cfg.get('c1_release_window', 3) or 3))
        bb_length = 20
        kc_length = 20
        kc_mult = 1.5
        mom_length = 20

        result = {
            'long_pass': False,
            'short_pass': False,
            'squeeze_on': False,
            'squeeze_off': False,
            'hist': np.nan,
            'prev_hist': np.nan,
            'release_age': None,
            'reason_long': 'candidate1 data wait',
            'reason_short': 'candidate1 data wait'
        }

        closed = df.iloc[:-1].copy().reset_index(drop=True)
        min_bars = max(bb_length, kc_length, mom_length) + release_window + 2
        if len(closed) < min_bars:
            return result

        close = closed['close']
        high = closed['high']
        low = closed['low']

        bb_basis = close.rolling(bb_length).mean()
        # Match the linked LazyBear script exactly: BB dev also uses the KC multiplier.
        bb_dev = close.rolling(bb_length).std(ddof=0) * kc_mult
        bb_upper = bb_basis + bb_dev
        bb_lower = bb_basis - bb_dev

        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ], axis=1).max(axis=1)

        kc_basis = close.rolling(kc_length).mean()
        kc_range = tr.rolling(kc_length).mean()
        kc_upper = kc_basis + kc_range * kc_mult
        kc_lower = kc_basis - kc_range * kc_mult

        squeeze_on = (bb_lower > kc_lower) & (bb_upper < kc_upper)
        squeeze_off = (bb_lower < kc_lower) & (bb_upper > kc_upper)
        release_signal = squeeze_off & squeeze_on.shift(1).fillna(False)

        highest = high.rolling(mom_length).max()
        lowest = low.rolling(mom_length).min()
        mean_basis = ((highest + lowest) / 2.0 + close.rolling(mom_length).mean()) / 2.0
        mom_source = close - mean_basis

        def _linreg_last(values):
            if np.isnan(values).any():
                return np.nan
            x = np.arange(len(values))
            slope, intercept = np.polyfit(x, values, 1)
            return intercept + slope * x[-1]

        mom_hist = mom_source.rolling(mom_length).apply(_linreg_last, raw=True)
        curr_hist = float(mom_hist.iloc[-1]) if len(mom_hist) >= 1 else np.nan
        prev_hist = float(mom_hist.iloc[-2]) if len(mom_hist) >= 2 else np.nan

        release_age = None
        for age in range(release_window):
            idx = len(release_signal) - 1 - age
            if idx < 0:
                break
            if bool(release_signal.iloc[idx]):
                release_age = age
                break

        result.update({
            'squeeze_on': bool(squeeze_on.iloc[-1]) if len(squeeze_on) else False,
            'squeeze_off': bool(squeeze_off.iloc[-1]) if len(squeeze_off) else False,
            'hist': curr_hist,
            'prev_hist': prev_hist,
            'release_age': release_age
        })

        if release_age is None:
            result['reason_long'] = f"candidate1 no squeeze release within {release_window} bars"
            result['reason_short'] = f"candidate1 no squeeze release within {release_window} bars"
            return result

        if np.isnan(curr_hist) or np.isnan(prev_hist):
            result['reason_long'] = 'candidate1 momentum history insufficient'
            result['reason_short'] = 'candidate1 momentum history insufficient'
            return result

        long_pass = curr_hist > 0 and curr_hist > prev_hist
        short_pass = curr_hist < 0 and curr_hist < prev_hist
        result['long_pass'] = bool(long_pass)
        result['short_pass'] = bool(short_pass)
        result['reason_long'] = (
            f"candidate1 release_age={release_age}, hist={curr_hist:.4f}, prev={prev_hist:.4f}"
            if long_pass else
            f"candidate1 long rejected: release_age={release_age}, hist={curr_hist:.4f}, prev={prev_hist:.4f}"
        )
        result['reason_short'] = (
            f"candidate1 release_age={release_age}, hist={curr_hist:.4f}, prev={prev_hist:.4f}"
            if short_pass else
            f"candidate1 short rejected: release_age={release_age}, hist={curr_hist:.4f}, prev={prev_hist:.4f}"
        )
        return result

    def _calculate_utsmc_candidate2_filter(self, df, strategy_params):
        cfg = (strategy_params.get('UTSMC', {}) or {}).get('candidate_filter', {}) or {}
        breakout_window = max(1, int(cfg.get('c2_breakout_window', 2) or 2))
        box_length = 20
        atr_length = 14
        max_box_width_atr = 1.8
        impulse_atr_mult = 0.8

        result = {
            'long_pass': False,
            'short_pass': False,
            'box_high': np.nan,
            'box_low': np.nan,
            'box_width': np.nan,
            'box_width_atr': np.nan,
            'long_breakout_age': None,
            'short_breakout_age': None,
            'long_impulse_body_atr': np.nan,
            'short_impulse_body_atr': np.nan,
            'reason_long': 'candidate2 data wait',
            'reason_short': 'candidate2 data wait'
        }

        closed = df.iloc[:-1].copy().reset_index(drop=True)
        min_bars = box_length + atr_length + breakout_window + 3
        if len(closed) < min_bars:
            return result

        open_ = closed['open']
        high = closed['high']
        low = closed['low']
        close = closed['close']

        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ], axis=1).max(axis=1)
        atr = tr.rolling(atr_length).mean()

        box_high = high.shift(1).rolling(box_length).max()
        box_low = low.shift(1).rolling(box_length).min()
        box_width = box_high - box_low
        box_width_atr = box_width / atr.replace(0, np.nan)
        valid_box = box_width <= (atr * max_box_width_atr)

        candle_range = (high - low).replace(0, np.nan)
        body = (close - open_).abs()
        body_atr = body / atr.replace(0, np.nan)
        close_top30 = close >= (low + candle_range * 0.7)
        close_bottom30 = close <= (low + candle_range * 0.3)

        long_break = (
            valid_box
            & (close.shift(1) <= box_high)
            & (close > box_high)
            & (body >= atr * impulse_atr_mult)
            & close_top30
        )
        short_break = (
            valid_box
            & (close.shift(1) >= box_low)
            & (close < box_low)
            & (body >= atr * impulse_atr_mult)
            & close_bottom30
        )

        latest_box_high = float(box_high.iloc[-1]) if len(box_high) else np.nan
        latest_box_low = float(box_low.iloc[-1]) if len(box_low) else np.nan
        latest_box_width = float(box_width.iloc[-1]) if len(box_width) else np.nan
        latest_box_width_atr = float(box_width_atr.iloc[-1]) if len(box_width_atr) else np.nan

        result.update({
            'box_high': latest_box_high,
            'box_low': latest_box_low,
            'box_width': latest_box_width,
            'box_width_atr': latest_box_width_atr
        })

        long_breakout_age = None
        long_level = np.nan
        long_impulse = np.nan
        for age in range(breakout_window):
            idx = len(long_break) - 1 - age
            if idx < 0:
                break
            if bool(long_break.iloc[idx]):
                long_breakout_age = age
                long_level = float(box_high.iloc[idx])
                long_impulse = float(body_atr.iloc[idx]) if not np.isnan(body_atr.iloc[idx]) else np.nan
                break

        short_breakout_age = None
        short_level = np.nan
        short_impulse = np.nan
        for age in range(breakout_window):
            idx = len(short_break) - 1 - age
            if idx < 0:
                break
            if bool(short_break.iloc[idx]):
                short_breakout_age = age
                short_level = float(box_low.iloc[idx])
                short_impulse = float(body_atr.iloc[idx]) if not np.isnan(body_atr.iloc[idx]) else np.nan
                break

        result['long_breakout_age'] = long_breakout_age
        result['short_breakout_age'] = short_breakout_age
        result['long_impulse_body_atr'] = long_impulse
        result['short_impulse_body_atr'] = short_impulse

        if np.isnan(latest_box_width_atr):
            result['reason_long'] = 'candidate2 box/atr unavailable'
            result['reason_short'] = 'candidate2 box/atr unavailable'
            return result

        if long_breakout_age is not None and not np.isnan(long_level) and close.iloc[-1] > long_level:
            result['long_pass'] = True
            result['reason_long'] = (
                f"candidate2 breakout_age={long_breakout_age}, box_high={long_level:.4f}, body_atr={long_impulse:.2f}"
            )
        else:
            result['reason_long'] = (
                f"candidate2 long rejected: box_width_atr={latest_box_width_atr:.2f}, breakout_age={long_breakout_age}"
            )

        if short_breakout_age is not None and not np.isnan(short_level) and close.iloc[-1] < short_level:
            result['short_pass'] = True
            result['reason_short'] = (
                f"candidate2 breakout_age={short_breakout_age}, box_low={short_level:.4f}, body_atr={short_impulse:.2f}"
            )
        else:
            result['reason_short'] = (
                f"candidate2 short rejected: box_width_atr={latest_box_width_atr:.2f}, breakout_age={short_breakout_age}"
            )

        return result

    def _calculate_utsmc_candidate_filter_state(self, df, strategy_params):
        utsmc_cfg = strategy_params.get('UTSMC', {}) or {}
        filter_cfg = utsmc_cfg.get('candidate_filter', {}) or {}
        mode = self._normalize_utsmc_candidate_filter_mode(filter_cfg.get('mode', 'off'))
        apply_to_persistent = bool(filter_cfg.get('apply_to_persistent', True))

        c1 = self._calculate_utsmc_candidate1_filter(df, strategy_params)
        c2 = self._calculate_utsmc_candidate2_filter(df, strategy_params)

        detail = {
            'candidate_filter_mode': mode,
            'candidate_filter_mode_label': self._format_utsmc_candidate_filter_mode(mode),
            'candidate_filter_apply_to_persistent': apply_to_persistent,
            'candidate1_long_pass': bool(c1.get('long_pass', False)),
            'candidate1_short_pass': bool(c1.get('short_pass', False)),
            'candidate1_squeeze_on': bool(c1.get('squeeze_on', False)),
            'candidate1_squeeze_off': bool(c1.get('squeeze_off', False)),
            'candidate1_hist': c1.get('hist'),
            'candidate1_prev_hist': c1.get('prev_hist'),
            'candidate1_release_age': c1.get('release_age'),
            'candidate1_reason_long': c1.get('reason_long'),
            'candidate1_reason_short': c1.get('reason_short'),
            'candidate2_long_pass': bool(c2.get('long_pass', False)),
            'candidate2_short_pass': bool(c2.get('short_pass', False)),
            'candidate2_box_high': c2.get('box_high'),
            'candidate2_box_low': c2.get('box_low'),
            'candidate2_box_width': c2.get('box_width'),
            'candidate2_box_width_atr': c2.get('box_width_atr'),
            'candidate2_long_breakout_age': c2.get('long_breakout_age'),
            'candidate2_short_breakout_age': c2.get('short_breakout_age'),
            'candidate2_long_impulse_body_atr': c2.get('long_impulse_body_atr'),
            'candidate2_short_impulse_body_atr': c2.get('short_impulse_body_atr'),
            'candidate2_reason_long': c2.get('reason_long'),
            'candidate2_reason_short': c2.get('reason_short'),
            'candidate_final_long_pass': True,
            'candidate_final_short_pass': True,
            'candidate_reason_long': 'candidate filter off',
            'candidate_reason_short': 'candidate filter off'
        }

        if mode == 'candidate1':
            detail['candidate_final_long_pass'] = detail['candidate1_long_pass']
            detail['candidate_final_short_pass'] = detail['candidate1_short_pass']
            detail['candidate_reason_long'] = detail['candidate1_reason_long']
            detail['candidate_reason_short'] = detail['candidate1_reason_short']
        elif mode == 'candidate2':
            detail['candidate_final_long_pass'] = detail['candidate2_long_pass']
            detail['candidate_final_short_pass'] = detail['candidate2_short_pass']
            detail['candidate_reason_long'] = detail['candidate2_reason_long']
            detail['candidate_reason_short'] = detail['candidate2_reason_short']
        elif mode == 'candidate12':
            detail['candidate_final_long_pass'] = detail['candidate1_long_pass'] and detail['candidate2_long_pass']
            detail['candidate_final_short_pass'] = detail['candidate1_short_pass'] and detail['candidate2_short_pass']
            detail['candidate_reason_long'] = (
                f"C1={detail['candidate1_reason_long']} | C2={detail['candidate2_reason_long']}"
            )
            detail['candidate_reason_short'] = (
                f"C1={detail['candidate1_reason_short']} | C2={detail['candidate2_reason_short']}"
            )

        return detail

    def _calculate_alt_trend_price_signal(self, symbol, df, strategy_params):
        closed = df.iloc[:-1].copy().reset_index(drop=True)
        min_bars = 80
        if len(closed) < min_bars:
            return None

        close_series = closed['close'].astype(float)
        volume_series = closed['volume'].astype(float)
        ema21 = ta.ema(close_series, length=21)
        sma50 = ta.sma(close_series, length=50)
        rsi14 = ta.rsi(close_series, length=14)
        volume_sma20 = ta.sma(volume_series, length=20)

        indicator_df = closed.copy()
        indicator_df['ema21'] = ema21
        indicator_df['sma50'] = sma50
        indicator_df['rsi14'] = rsi14
        indicator_df['volume_sma20'] = volume_sma20
        indicator_df = indicator_df.dropna().reset_index(drop=True)
        if len(indicator_df) < 2:
            return None

        curr = indicator_df.iloc[-1]
        prev = indicator_df.iloc[-2]
        curr_close = float(curr['close'])
        curr_rsi = float(curr['rsi14'])
        curr_ema21 = float(curr['ema21'])
        prev_ema21 = float(prev['ema21'])
        curr_sma50 = float(curr['sma50'])
        curr_volume = float(curr['volume'])
        curr_volume_sma20 = float(curr['volume_sma20']) if float(curr['volume_sma20']) != 0 else np.nan
        volume_ratio = curr_volume / curr_volume_sma20 if not np.isnan(curr_volume_sma20) else np.nan

        if np.isnan(volume_ratio):
            return None

        candidate1 = self._calculate_utsmc_candidate1_filter(df, strategy_params)
        candidate2 = self._calculate_utsmc_candidate2_filter(df, strategy_params)
        candidate1_long_pass = bool(candidate1.get('long_pass', False))
        candidate2_long_pass = bool(candidate2.get('long_pass', False))

        box_high_raw = candidate2.get('box_high')
        box_width_atr_raw = candidate2.get('box_width_atr')
        box_high = float(box_high_raw) if box_high_raw is not None and not np.isnan(box_high_raw) else np.nan
        box_width_atr = float(box_width_atr_raw) if box_width_atr_raw is not None and not np.isnan(box_width_atr_raw) else np.nan

        compact_box_ready = (not np.isnan(box_width_atr)) and box_width_atr <= 1.8
        near_box_high = (not np.isnan(box_high)) and curr_close <= (box_high * 1.03)
        ema21_rising = curr_ema21 > prev_ema21
        close_above_ema21 = curr_close > curr_ema21
        close_above_sma50 = curr_close > curr_sma50

        setup_ready = (
            (candidate1_long_pass or (compact_box_ready and near_box_high))
            and curr_rsi >= 55.0
            and close_above_ema21
            and ema21_rising
            and volume_ratio >= 1.5
        )

        confirm_ready = (
            candidate2_long_pass
            and curr_rsi >= 58.0
            and volume_ratio >= 2.0
            and close_above_ema21
            and close_above_sma50
            and curr_close <= (curr_ema21 * 1.25)
        )

        if not setup_ready and not confirm_ready:
            return None

        score = (
            (volume_ratio * 40.0)
            + (max(0.0, curr_rsi - 50.0) * 2.0)
            + (15.0 if candidate2_long_pass else 0.0)
            + (10.0 if candidate1_long_pass else 0.0)
            + (10.0 if close_above_sma50 else 0.0)
        )

        return {
            'symbol': symbol,
            'stage': 'confirm' if confirm_ready else 'setup',
            'score': float(score),
            'candle_ts': int(curr['timestamp']),
            'curr_close': curr_close,
            'curr_rsi': curr_rsi,
            'curr_ema21': curr_ema21,
            'curr_sma50': curr_sma50,
            'volume_ratio': float(volume_ratio),
            'candidate1_long_pass': candidate1_long_pass,
            'candidate2_long_pass': candidate2_long_pass,
            'candidate2_box_high': box_high,
            'candidate2_box_width_atr': box_width_atr,
            'setup_ready': bool(setup_ready),
            'confirm_ready': bool(confirm_ready),
            'close_above_sma50': bool(close_above_sma50),
            'reason': candidate2.get('reason_long') if confirm_ready else candidate1.get('reason_long')
        }

    def _calculate_utsmc_signal(self, df, strategy_params):
        ut_sig, ut_reason, ut_detail = self._calculate_utbot_signal(df, strategy_params)
        smc_sig, smc_reason, smc_detail = self._calculate_smc_internal_ob_signal(df, strategy_params)
        candidate_detail = self._calculate_utsmc_candidate_filter_state(df, strategy_params)
        ut_state = ut_sig or ut_detail.get('bias_side')
        timing_mode = str(strategy_params.get('ut_entry_timing_mode', 'next_candle') or 'next_candle').lower()
        timing_desc = '다음 진행봉 진입' if timing_mode == 'next_candle' else '신호 유지 시 진입'

        detail = {
            'ut_state': ut_state,
            'ut_signal': ut_sig,
            'ut_reason': ut_reason,
            'ut_signal_ts': ut_detail.get('signal_ts'),
            'ut_detail': ut_detail,
            'smc_signal': smc_sig,
            'smc_reason': smc_reason,
            **(smc_detail or {}),
            **candidate_detail
        }

        if ut_state not in {'long', 'short'}:
            return None, "UTSMC 대기: UT 상태 계산 대기", detail

        exit_wait_label = 'internal bearish OB entry' if ut_state == 'long' else 'internal bullish OB entry'
        if ut_sig in {'long', 'short'}:
            reason = (
                f"UTSMC {ut_sig.upper()} SIGNAL: "
                f"신호봉 이후 {timing_desc}, 청산은 {exit_wait_label}"
            )
        else:
            reason = f"UTSMC 상태 유지: UT {ut_state.upper()} 상태, 마지막 UT 시그널 확정봉 기준 {timing_desc}"
        return ut_sig, reason, detail

    def _utsmc_candidate_filter_allows(self, raw_smc_detail, side, *, is_persistent=False):
        mode = self._normalize_utsmc_candidate_filter_mode(raw_smc_detail.get('candidate_filter_mode', 'off'))
        if side not in {'long', 'short'} or mode == 'off':
            return True, 'candidate filter off'

        apply_to_persistent = bool(raw_smc_detail.get('candidate_filter_apply_to_persistent', True))
        if is_persistent and not apply_to_persistent:
            return True, 'candidate filter persistent bypass'

        pass_key = 'candidate_final_long_pass' if side == 'long' else 'candidate_final_short_pass'
        reason_key = 'candidate_reason_long' if side == 'long' else 'candidate_reason_short'
        allowed = bool(raw_smc_detail.get(pass_key, False))
        reason = raw_smc_detail.get(reason_key) or 'candidate filter no detail'
        return allowed, reason

    def _calculate_rsi_signal(self, df, strategy_params):
        cfg = strategy_params.get('RSIBB', {})
        rsi_length = max(1, int(cfg.get('rsi_length', 6) or 6))

        closed = df.iloc[:-1].copy().reset_index(drop=True)
        min_bars = max(rsi_length + 3, 30)
        if len(closed) < min_bars:
            return None, "RSI 데이터 부족", {}

        close_series = closed['close'].astype(float)
        rsi_series = ta.rsi(close_series, length=rsi_length)
        valid = pd.DataFrame({
            'timestamp': closed['timestamp'],
            'close': close_series,
            'rsi': rsi_series
        }).dropna().reset_index(drop=True)
        if len(valid) < 2:
            return None, "RSI 지표 확정 대기", {}

        prev_row = valid.iloc[-2]
        curr_row = valid.iloc[-1]
        prev_rsi = float(prev_row['rsi'])
        curr_rsi = float(curr_row['rsi'])

        rsi_cross_up = prev_rsi <= 50.0 and curr_rsi > 50.0
        rsi_cross_down = prev_rsi >= 50.0 and curr_rsi < 50.0

        detail = {
            'rsi_length': rsi_length,
            'prev_rsi': prev_rsi,
            'curr_rsi': curr_rsi,
            'curr_close': float(curr_row['close']),
            'signal_ts': int(curr_row['timestamp'])
        }

        if rsi_cross_up:
            reason = f"RSI LONG: RSI 50 상향돌파 (RSI={curr_rsi:.2f}, len={rsi_length})"
            return 'long', reason, detail

        if rsi_cross_down:
            reason = f"RSI SHORT: RSI 50 하향돌파 (RSI={curr_rsi:.2f}, len={rsi_length})"
            return 'short', reason, detail

        reason = f"RSI 대기: RSI={curr_rsi:.2f}, len={rsi_length}"
        return None, reason, detail

    def _calculate_rsi_momentum_trend_signal(self, df, strategy_params):
        cfg = strategy_params.get('RSIMomentumTrend', {}) or {}
        rsi_length = max(1, int(cfg.get('rsi_length', 14) or 14))
        positive_above = float(cfg.get('positive_above', 65) or 65)
        negative_below = float(cfg.get('negative_below', 32) or 32)
        ema_period = max(1, int(cfg.get('ema_period', 5) or 5))

        closed = df.iloc[:-1].copy().reset_index(drop=True)
        min_bars = max(rsi_length + ema_period + 5, 30)
        if len(closed) < min_bars:
            return None, "RSI Momentum Trend 데이터 부족", {}

        close_series = closed['close'].astype(float)
        rsi_series = ta.rsi(close_series, length=rsi_length)
        ema_series = ta.ema(close_series, length=ema_period)
        ema_change = ema_series.diff()

        valid = pd.DataFrame({
            'timestamp': closed['timestamp'],
            'close': close_series,
            'rsi': rsi_series,
            'ema': ema_series,
            'ema_change': ema_change
        }).dropna().reset_index(drop=True)
        if len(valid) < 2:
            return None, "RSI Momentum Trend 지표 확정 대기", {}

        state_side = None
        state_ts = None
        signal_side = None
        signal_ts = None

        for idx, row in valid.iterrows():
            curr_rsi = float(row['rsi'])
            curr_ema_change = float(row['ema_change'])
            prev_rsi = float(valid.iloc[idx - 1]['rsi']) if idx > 0 else np.nan

            p_mom = (
                idx > 0
                and prev_rsi < positive_above
                and curr_rsi > positive_above
                and curr_rsi > negative_below
                and curr_ema_change > 0
            )
            n_mom = curr_rsi < negative_below and curr_ema_change < 0

            current_bar_signal = None
            if p_mom:
                state_side = 'long'
                state_ts = int(row['timestamp'])
                current_bar_signal = 'long'
            elif n_mom:
                state_side = 'short'
                state_ts = int(row['timestamp'])
                current_bar_signal = 'short'

            if idx == (len(valid) - 1):
                signal_side = current_bar_signal
                signal_ts = int(row['timestamp']) if current_bar_signal else None

        curr_row = valid.iloc[-1]
        curr_rsi = float(curr_row['rsi'])
        curr_ema = float(curr_row['ema'])
        curr_ema_change = float(curr_row['ema_change'])

        detail = {
            'rsi_length': rsi_length,
            'positive_above': positive_above,
            'negative_below': negative_below,
            'ema_period': ema_period,
            'curr_rsi': curr_rsi,
            'curr_close': float(curr_row['close']),
            'curr_ema': curr_ema,
            'curr_ema_change': curr_ema_change,
            'state_side': state_side,
            'state_ts': state_ts,
            'signal_side': signal_side,
            'signal_ts': signal_ts
        }

        if signal_side == 'long':
            reason = (
                f"RSI Momentum LONG: RSI {positive_above:.1f} 상향 + "
                f"EMA({ema_period}) 상승 (RSI={curr_rsi:.2f})"
            )
            return 'long', reason, detail

        if signal_side == 'short':
            reason = (
                f"RSI Momentum SHORT: RSI {negative_below:.1f} 아래 + "
                f"EMA({ema_period}) 하락 (RSI={curr_rsi:.2f})"
            )
            return 'short', reason, detail

        if state_side == 'long':
            reason = (
                f"RSI Momentum 상태 유지 (LONG): RSI={curr_rsi:.2f}, "
                f"EMA({ema_period}) 변화={curr_ema_change:.4f}"
            )
            return None, reason, detail

        if state_side == 'short':
            reason = (
                f"RSI Momentum 상태 유지 (SHORT): RSI={curr_rsi:.2f}, "
                f"EMA({ema_period}) 변화={curr_ema_change:.4f}"
            )
            return None, reason, detail

        reason = (
            f"RSI Momentum 대기: RSI={curr_rsi:.2f}, "
            f"EMA({ema_period}) 변화={curr_ema_change:.4f}"
        )
        return None, reason, detail

    def _calculate_bb_signal(self, df, strategy_params):
        cfg = strategy_params.get('RSIBB', {})
        bb_length = max(2, int(cfg.get('bb_length', 200) or 200))
        bb_mult = float(cfg.get('bb_mult', 2.0) or 2.0)

        closed = df.iloc[:-1].copy().reset_index(drop=True)
        min_bars = max(bb_length + 3, 30)
        if len(closed) < min_bars:
            return None, "BB 데이터 부족", {}

        close_series = closed['close'].astype(float)
        bb_basis = ta.sma(close_series, length=bb_length)
        bb_std = close_series.rolling(bb_length).std(ddof=0)
        bb_upper = bb_basis + (bb_std * bb_mult)
        bb_lower = bb_basis - (bb_std * bb_mult)

        valid = pd.DataFrame({
            'timestamp': closed['timestamp'],
            'close': close_series,
            'bb_basis': bb_basis,
            'bb_upper': bb_upper,
            'bb_lower': bb_lower
        }).dropna().reset_index(drop=True)
        if len(valid) < 2:
            return None, "BB 지표 확정 대기", {}

        prev_row = valid.iloc[-2]
        curr_row = valid.iloc[-1]
        prev_close = float(prev_row['close'])
        curr_close = float(curr_row['close'])
        prev_upper = float(prev_row['bb_upper'])
        curr_upper = float(curr_row['bb_upper'])
        prev_lower = float(prev_row['bb_lower'])
        curr_lower = float(curr_row['bb_lower'])

        price_cross_up = prev_close <= prev_lower and curr_close > curr_lower
        price_cross_down = prev_close >= prev_upper and curr_close < curr_upper

        detail = {
            'bb_length': bb_length,
            'bb_mult': bb_mult,
            'curr_close': curr_close,
            'curr_upper': curr_upper,
            'curr_lower': curr_lower,
            'signal_ts': int(curr_row['timestamp'])
        }

        if price_cross_up:
            reason = (
                f"BB LONG: 하단밴드 상향돌파 "
                f"(BB={bb_length}, x{bb_mult:.2f}, close={curr_close:.4f})"
            )
            return 'long', reason, detail

        if price_cross_down:
            reason = (
                f"BB SHORT: 상단밴드 하향돌파 "
                f"(BB={bb_length}, x{bb_mult:.2f}, close={curr_close:.4f})"
            )
            return 'short', reason, detail

        reason = (
            f"BB 대기: Upper={curr_upper:.4f}, "
            f"Lower={curr_lower:.4f}, close={curr_close:.4f}"
        )
        return None, reason, detail

    def _calculate_bb_upper_breakout_signal(self, df, strategy_params):
        cfg = strategy_params.get('RSIBB', {})
        bb_length = max(2, int(cfg.get('bb_length', 200) or 200))
        bb_mult = float(cfg.get('bb_mult', 2.0) or 2.0)

        closed = df.iloc[:-1].copy().reset_index(drop=True)
        min_bars = max(bb_length + 3, 30)
        if len(closed) < min_bars:
            return None, "BB 상단 돌파 데이터 부족", {}

        close_series = closed['close'].astype(float)
        bb_basis = ta.sma(close_series, length=bb_length)
        bb_std = close_series.rolling(bb_length).std(ddof=0)
        bb_upper = bb_basis + (bb_std * bb_mult)

        valid = pd.DataFrame({
            'timestamp': closed['timestamp'],
            'close': close_series,
            'bb_upper': bb_upper
        }).dropna().reset_index(drop=True)
        if len(valid) < 2:
            return None, "BB 상단 돌파 지표 확정 대기", {}

        prev_row = valid.iloc[-2]
        curr_row = valid.iloc[-1]
        prev_close = float(prev_row['close'])
        curr_close = float(curr_row['close'])
        prev_upper = float(prev_row['bb_upper'])
        curr_upper = float(curr_row['bb_upper'])
        breakout_up = prev_close <= prev_upper and curr_close > curr_upper

        detail = {
            'bb_length': bb_length,
            'bb_mult': bb_mult,
            'prev_close': prev_close,
            'curr_close': curr_close,
            'prev_upper': prev_upper,
            'curr_upper': curr_upper,
            'signal_ts': int(curr_row['timestamp'])
        }

        if breakout_up:
            reason = (
                f"BB UPPER BREAKOUT: 상단밴드 상향돌파 "
                f"(BB={bb_length}, x{bb_mult:.2f}, close={curr_close:.4f})"
            )
            return 'exit_long', reason, detail

        reason = f"BB 상단 돌파 대기: Upper={curr_upper:.4f}, close={curr_close:.4f}"
        return None, reason, detail

    def _calculate_bb_mid_cross_down_signal(self, df, strategy_params):
        cfg = strategy_params.get('RSIBB', {})
        bb_length = max(2, int(cfg.get('bb_length', 200) or 200))

        closed = df.iloc[:-1].copy().reset_index(drop=True)
        min_bars = max(bb_length + 3, 30)
        if len(closed) < min_bars:
            return None, "BB 중간선 하락 돌파 데이터 부족", {}

        close_series = closed['close'].astype(float)
        bb_basis = ta.sma(close_series, length=bb_length)
        valid = pd.DataFrame({
            'timestamp': closed['timestamp'],
            'close': close_series,
            'bb_basis': bb_basis
        }).dropna().reset_index(drop=True)
        if len(valid) < 2:
            return None, "BB 중간선 하락 돌파 지표 확정 대기", {}

        prev_row = valid.iloc[-2]
        curr_row = valid.iloc[-1]
        prev_close = float(prev_row['close'])
        curr_close = float(curr_row['close'])
        prev_basis = float(prev_row['bb_basis'])
        curr_basis = float(curr_row['bb_basis'])
        cross_down = prev_close >= prev_basis and curr_close < curr_basis

        detail = {
            'bb_length': bb_length,
            'prev_close': prev_close,
            'curr_close': curr_close,
            'prev_basis': prev_basis,
            'curr_basis': curr_basis,
            'signal_ts': int(curr_row['timestamp'])
        }

        if cross_down:
            reason = f"BB MID SHORT: 중간선 하향돌파 (BB={bb_length}, close={curr_close:.4f})"
            return 'short', reason, detail

        reason = f"BB 중간선 하락 돌파 대기: Basis={curr_basis:.4f}, close={curr_close:.4f}"
        return None, reason, detail

    def _calculate_bb_mid_cross_up_signal(self, df, strategy_params):
        cfg = strategy_params.get('RSIBB', {})
        bb_length = max(2, int(cfg.get('bb_length', 200) or 200))

        closed = df.iloc[:-1].copy().reset_index(drop=True)
        min_bars = max(bb_length + 3, 30)
        if len(closed) < min_bars:
            return None, "BB 중간선 상승 돌파 데이터 부족", {}

        close_series = closed['close'].astype(float)
        bb_basis = ta.sma(close_series, length=bb_length)
        valid = pd.DataFrame({
            'timestamp': closed['timestamp'],
            'close': close_series,
            'bb_basis': bb_basis
        }).dropna().reset_index(drop=True)
        if len(valid) < 2:
            return None, "BB 중간선 상승 돌파 지표 확정 대기", {}

        prev_row = valid.iloc[-2]
        curr_row = valid.iloc[-1]
        prev_close = float(prev_row['close'])
        curr_close = float(curr_row['close'])
        prev_basis = float(prev_row['bb_basis'])
        curr_basis = float(curr_row['bb_basis'])
        cross_up = prev_close <= prev_basis and curr_close > curr_basis

        detail = {
            'bb_length': bb_length,
            'prev_close': prev_close,
            'curr_close': curr_close,
            'prev_basis': prev_basis,
            'curr_basis': curr_basis,
            'signal_ts': int(curr_row['timestamp'])
        }

        if cross_up:
            reason = f"BB MID EXIT: 중간선 상향돌파 (BB={bb_length}, close={curr_close:.4f})"
            return 'exit_short', reason, detail

        reason = f"BB 중간선 상승 돌파 대기: Basis={curr_basis:.4f}, close={curr_close:.4f}"
        return None, reason, detail

    def _calculate_bb_lower_breakout_signal(self, df, strategy_params):
        cfg = strategy_params.get('RSIBB', {})
        bb_length = max(2, int(cfg.get('bb_length', 200) or 200))
        bb_mult = float(cfg.get('bb_mult', 2.0) or 2.0)

        closed = df.iloc[:-1].copy().reset_index(drop=True)
        min_bars = max(bb_length + 3, 30)
        if len(closed) < min_bars:
            return None, "BB 하단 돌파 데이터 부족", {}

        close_series = closed['close'].astype(float)
        bb_basis = ta.sma(close_series, length=bb_length)
        bb_std = close_series.rolling(bb_length).std(ddof=0)
        bb_lower = bb_basis - (bb_std * bb_mult)

        valid = pd.DataFrame({
            'timestamp': closed['timestamp'],
            'close': close_series,
            'bb_lower': bb_lower
        }).dropna().reset_index(drop=True)
        if len(valid) < 2:
            return None, "BB 하단 돌파 지표 확정 대기", {}

        prev_row = valid.iloc[-2]
        curr_row = valid.iloc[-1]
        prev_close = float(prev_row['close'])
        curr_close = float(curr_row['close'])
        prev_lower = float(prev_row['bb_lower'])
        curr_lower = float(curr_row['bb_lower'])
        breakout_down = prev_close >= prev_lower and curr_close < curr_lower

        detail = {
            'bb_length': bb_length,
            'bb_mult': bb_mult,
            'prev_close': prev_close,
            'curr_close': curr_close,
            'prev_lower': prev_lower,
            'curr_lower': curr_lower,
            'signal_ts': int(curr_row['timestamp'])
        }

        if breakout_down:
            reason = (
                f"BB LOWER BREAKOUT: 하단밴드 하향돌파 "
                f"(BB={bb_length}, x{bb_mult:.2f}, close={curr_close:.4f})"
            )
            return 'exit_short', reason, detail

        reason = f"BB 하단 돌파 대기: Lower={curr_lower:.4f}, close={curr_close:.4f}"
        return None, reason, detail

    def _calculate_bb_mid_rebound_fail_short_signal(self, df, strategy_params):
        cfg = strategy_params.get('RSIBB', {})
        bb_length = max(2, int(cfg.get('bb_length', 200) or 200))

        closed = df.iloc[:-1].copy().reset_index(drop=True)
        min_bars = max(bb_length + 6, 30)
        if len(closed) < min_bars:
            return None, "BB 중간선 아래 반등 실패 데이터 부족", {}

        open_series = closed['open'].astype(float)
        high_series = closed['high'].astype(float)
        low_series = closed['low'].astype(float)
        close_series = closed['close'].astype(float)
        bb_basis = ta.sma(close_series, length=bb_length)

        valid = pd.DataFrame({
            'timestamp': closed['timestamp'],
            'open': open_series,
            'high': high_series,
            'low': low_series,
            'close': close_series,
            'bb_basis': bb_basis
        }).dropna().reset_index(drop=True)
        if len(valid) < 3:
            return None, "BB 중간선 아래 반등 실패 지표 확정 대기", {}

        prev2 = valid.iloc[-3]
        prev1 = valid.iloc[-2]
        curr = valid.iloc[-1]

        def _bullish_below_mid(row):
            row_open = float(row['open'])
            row_close = float(row['close'])
            row_basis = float(row['bb_basis'])
            return row_close > row_open and row_open < row_basis and row_close < row_basis

        prev2_bull = _bullish_below_mid(prev2)
        prev1_bull = _bullish_below_mid(prev1)
        curr_open = float(curr['open'])
        curr_close = float(curr['close'])
        curr_basis = float(curr['bb_basis'])
        curr_bear_below_mid = curr_close < curr_open and curr_open < curr_basis and curr_close < curr_basis
        prev1_low = float(prev1['low'])
        prev2_low = float(prev2['low'])

        one_bull_setup = prev1_bull and curr_bear_below_mid and curr_close < prev1_low
        two_bull_setup = prev2_bull and prev1_bull and curr_bear_below_mid and curr_close < prev1_low
        setup_bull_count = 2 if two_bull_setup else (1 if one_bull_setup else 0)

        detail = {
            'bb_length': bb_length,
            'setup_bull_count': setup_bull_count,
            'prev1_low': prev1_low,
            'prev2_low': prev2_low,
            'curr_open': curr_open,
            'curr_close': curr_close,
            'curr_basis': curr_basis,
            'signal_ts': int(curr['timestamp'])
        }

        if setup_bull_count > 0:
            reason = (
                f"BB MID REBOUND FAIL SHORT: 중간선 아래 양봉 {setup_bull_count}개 후 "
                f"음봉 저점 이탈 (BB={bb_length}, close={curr_close:.4f})"
            )
            return 'short', reason, detail

        reason = f"BB 중간선 아래 반등 실패 대기: Basis={curr_basis:.4f}, close={curr_close:.4f}"
        return None, reason, detail

    def _calculate_bb_mid_two_bearish_exit_signal(self, df, strategy_params):
        cfg = strategy_params.get('RSIBB', {})
        bb_length = max(2, int(cfg.get('bb_length', 200) or 200))

        closed = df.iloc[:-1].copy().reset_index(drop=True)
        min_bars = max(bb_length + 4, 30)
        if len(closed) < min_bars:
            return None, "BB 중간선 위 음봉 2개 데이터 부족", {}

        open_series = closed['open'].astype(float)
        close_series = closed['close'].astype(float)
        bb_basis = ta.sma(close_series, length=bb_length)

        valid = pd.DataFrame({
            'timestamp': closed['timestamp'],
            'open': open_series,
            'close': close_series,
            'bb_basis': bb_basis
        }).dropna().reset_index(drop=True)
        if len(valid) < 2:
            return None, "BB 중간선 위 음봉 2개 지표 확정 대기", {}

        prev_row = valid.iloc[-2]
        curr_row = valid.iloc[-1]
        prev_open = float(prev_row['open'])
        prev_close = float(prev_row['close'])
        curr_open = float(curr_row['open'])
        curr_close = float(curr_row['close'])
        prev_basis = float(prev_row['bb_basis'])
        curr_basis = float(curr_row['bb_basis'])

        prev_bearish_above_mid = prev_close < prev_open and prev_open > prev_basis and prev_close > prev_basis
        curr_bearish_above_mid = curr_close < curr_open and curr_open > curr_basis and curr_close > curr_basis
        two_bearish_above_mid = prev_bearish_above_mid and curr_bearish_above_mid

        detail = {
            'bb_length': bb_length,
            'prev_open': prev_open,
            'prev_close': prev_close,
            'prev_basis': prev_basis,
            'curr_open': curr_open,
            'curr_close': curr_close,
            'curr_basis': curr_basis,
            'signal_ts': int(curr_row['timestamp'])
        }

        if two_bearish_above_mid:
            reason = (
                f"BB MID 2BEAR EXIT: 중간선 위 음봉 2개 "
                f"(BB={bb_length}, close={curr_close:.4f})"
            )
            return 'exit_long', reason, detail

        reason = f"BB 중간선 위 음봉 2개 대기: Basis={curr_basis:.4f}, close={curr_close:.4f}"
        return None, reason, detail

    def _calculate_rsibb_signal(self, df, strategy_params):
        rsi_sig, rsi_reason, rsi_detail = self._calculate_rsi_signal(df, strategy_params)
        bb_sig, bb_reason, bb_detail = self._calculate_bb_signal(df, strategy_params)

        detail = {
            'rsi_length': rsi_detail.get('rsi_length'),
            'bb_length': bb_detail.get('bb_length'),
            'bb_mult': bb_detail.get('bb_mult'),
            'curr_rsi': rsi_detail.get('curr_rsi'),
            'curr_close': bb_detail.get('curr_close', rsi_detail.get('curr_close')),
            'curr_upper': bb_detail.get('curr_upper'),
            'curr_lower': bb_detail.get('curr_lower'),
            'signal_ts': bb_detail.get('signal_ts') or rsi_detail.get('signal_ts'),
            'rsi_signal': rsi_sig,
            'bb_signal': bb_sig,
            'rsi_reason': rsi_reason,
            'bb_reason': bb_reason,
            'rsi_detail': rsi_detail,
            'bb_detail': bb_detail
        }

        if rsi_sig and bb_sig and rsi_sig == bb_sig:
            curr_rsi = float(rsi_detail.get('curr_rsi', 0.0))
            bb_length = int(bb_detail.get('bb_length', 0) or 0)
            bb_mult = float(bb_detail.get('bb_mult', 0.0) or 0.0)
            if rsi_sig == 'long':
                reason = (
                    f"RSIBB LONG: RSI 50 상향 + 하단밴드 상향돌파 "
                    f"(RSI={curr_rsi:.2f}, BB={bb_length}, x{bb_mult:.2f})"
                )
                return 'long', reason, detail

            reason = (
                f"RSIBB SHORT: RSI 50 하향 + 상단밴드 하향돌파 "
                f"(RSI={curr_rsi:.2f}, BB={bb_length}, x{bb_mult:.2f})"
            )
            return 'short', reason, detail

        if rsi_sig and bb_sig and rsi_sig != bb_sig:
            reason = f"RSIBB 대기: RSI {rsi_sig.upper()}, BB {bb_sig.upper()} 방향 불일치"
            return None, reason, detail

        if rsi_detail and bb_detail:
            reason = (
                f"RSIBB 대기: RSI={float(rsi_detail.get('curr_rsi', 0.0)):.2f}, "
                f"Upper={float(bb_detail.get('curr_upper', 0.0)):.4f}, "
                f"Lower={float(bb_detail.get('curr_lower', 0.0)):.4f}"
            )
            return None, reason, detail

        reason = f"RSIBB 대기: {rsi_reason} / {bb_reason}"
        return None, reason, detail

    def _rsibb_runtime_guard(self, strategy_params):
        cfg = (strategy_params or {}).get('RSIBB', {}) or {}
        if not bool(cfg.get('rsibb_enabled', False)):
            return False, "RSIBB 안전장치: rsibb_enabled=False (기본 비활성)"
        paper_only = bool(cfg.get('rsibb_paper_only', True))
        mode = None
        try:
            mode = self.ctrl.get_exchange_mode() if getattr(self, 'ctrl', None) else None
        except Exception:
            mode = None
        if paper_only and mode == BINANCE_MAINNET:
            return False, "RSIBB 안전장치: paper_only=True 상태에서는 메인넷 진입 차단"
        if bool(cfg.get('rsibb_regime_guard_enabled', True)):
            return True, "RSIBB regime guard ON"
        return True, "RSIBB guard pass"

    def _calculate_ut_hybrid_signal(self, symbol, df, strategy_params, hybrid_strategy):
        hybrid_strategy = str(hybrid_strategy or '').lower()
        timing_label = UT_HYBRID_TIMING_LABELS.get(hybrid_strategy, 'Signal')
        strategy_label = STRATEGY_DISPLAY_NAMES.get(hybrid_strategy, hybrid_strategy.upper())
        timing_calc_map = {
            'utrsibb': self._calculate_rsibb_signal,
            'utrsi': self._calculate_rsi_signal,
            'utbb': self._calculate_bb_signal
        }
        timing_calc = timing_calc_map.get(hybrid_strategy)
        if timing_calc is None:
            return None, f"{strategy_label} 계산기가 없습니다.", {}

        ut_sig, ut_reason, ut_detail = self._calculate_utbot_signal(df, strategy_params)
        timing_sig, timing_reason, timing_detail = timing_calc(df, strategy_params)
        latch = None
        latch_available = False
        if hybrid_strategy in {'utrsi', 'utbb'} and symbol:
            latch = self._remember_ut_hybrid_timing_signal(symbol, hybrid_strategy, timing_sig, timing_detail)
            latch_available = self._is_ut_hybrid_timing_latch_available(symbol, hybrid_strategy)

        ut_state = ut_sig or ut_detail.get('bias_side')
        effective_timing_sig = timing_sig
        timing_match_source = 'fresh' if timing_sig in {'long', 'short'} else None
        if hybrid_strategy in {'utrsi', 'utbb'} and ut_state in {'long', 'short'}:
            if not (timing_sig == ut_state):
                if latch_available and latch and latch.get('signal') == ut_state:
                    effective_timing_sig = ut_state
                    timing_match_source = 'latched'
                elif timing_sig not in {'long', 'short'}:
                    effective_timing_sig = None
                    timing_match_source = None

        detail = {
            'ut_state': ut_state,
            'ut_signal': ut_sig,
            'ut_reason': ut_reason,
            'ut_signal_ts': ut_detail.get('signal_ts'),
            'timing_label': timing_label,
            'timing_signal': timing_sig,
            'effective_timing_signal': effective_timing_sig,
            'timing_match_source': timing_match_source,
            'timing_reason': timing_reason,
            'timing_signal_ts': timing_detail.get('signal_ts'),
            'latched_timing_signal': latch.get('signal') if latch else None,
            'latched_timing_signal_ts': latch.get('signal_ts') if latch else None,
            'latched_timing_available': latch_available,
            'timing_detail': timing_detail,
            'ut_detail': ut_detail
        }

        if ut_state not in {'long', 'short'}:
            return None, f"{strategy_label} 대기: UT 상태 계산 대기", detail

        if effective_timing_sig == ut_state:
            if timing_match_source == 'latched':
                reason = (
                    f"{strategy_label} {ut_state.upper()}: 저장된 {timing_label} {ut_state.upper()} "
                    f"신호 + 현재 UT {ut_state.upper()} 상태 일치"
                )
            else:
                reason = (
                    f"{strategy_label} {ut_state.upper()}: UT {ut_state.upper()} 상태 + "
                    f"{timing_label} {ut_state.upper()} 타이밍 일치"
                )
            return ut_state, reason, detail

        if timing_sig and timing_sig != ut_state:
            reason = (
                f"{strategy_label} 대기: UT {ut_state.upper()} 상태, "
                f"{timing_label} {timing_sig.upper()} 신호는 방향 불일치"
            )
            return None, reason, detail

        if hybrid_strategy in {'utrsi', 'utbb'} and latch_available and latch and latch.get('signal') != ut_state:
            reason = (
                f"{strategy_label} 대기: UT {ut_state.upper()} 상태, "
                f"저장된 {timing_label} {str(latch.get('signal')).upper()} 신호와 방향 불일치"
            )
            return None, reason, detail

        reason = (
            f"{strategy_label} 대기: UT {ut_state.upper()} 상태, "
            f"{timing_label} {ut_state.upper()} 타이밍 신호 대기"
        )
        return None, reason, detail

    def _calculate_utrsibb_signal(self, symbol, df, strategy_params):
        return self._calculate_ut_hybrid_signal(symbol, df, strategy_params, 'utrsibb')

    def _calculate_utrsi_signal(self, symbol, df, strategy_params):
        return self._calculate_ut_hybrid_signal(symbol, df, strategy_params, 'utrsi')

    def _calculate_utbb_signal(self, symbol, df, strategy_params):
        ut_sig, ut_reason, ut_detail = self._calculate_utbot_signal(df, strategy_params)
        ut_state = ut_sig or ut_detail.get('bias_side')
        bb_sig, bb_reason, bb_detail = self._calculate_bb_signal(df, strategy_params)
        bb_upper_sig, bb_upper_reason, bb_upper_detail = self._calculate_bb_upper_breakout_signal(df, strategy_params)
        bb_mid_sig, bb_mid_reason, bb_mid_detail = self._calculate_bb_mid_cross_down_signal(df, strategy_params)
        bb_mid_up_sig, bb_mid_up_reason, bb_mid_up_detail = self._calculate_bb_mid_cross_up_signal(df, strategy_params)
        bb_lower_sig, bb_lower_reason, bb_lower_detail = self._calculate_bb_lower_breakout_signal(df, strategy_params)
        bb_mid_rebound_sig, bb_mid_rebound_reason, bb_mid_rebound_detail = self._calculate_bb_mid_rebound_fail_short_signal(df, strategy_params)
        bb_mid_two_bear_sig, bb_mid_two_bear_reason, bb_mid_two_bear_detail = self._calculate_bb_mid_two_bearish_exit_signal(df, strategy_params)
        closed = df.iloc[:-1].copy().reset_index(drop=True)
        candle_ms = 0
        if len(closed) >= 2:
            try:
                candle_ms = max(1, int(closed.iloc[-1]['timestamp']) - int(closed.iloc[-2]['timestamp']))
            except (TypeError, ValueError, KeyError):
                candle_ms = 0

        ut_long_fresh = ut_sig == 'long'
        bb_long_fresh = bb_sig == 'long'
        current_closed_ts = max(
            int(ut_detail.get('signal_ts') or 0),
            int(bb_detail.get('signal_ts') or 0),
            int(bb_mid_detail.get('signal_ts') or 0)
        )

        long_ut_setup = None
        long_bb_setup = None
        if symbol:
            if ut_sig == 'short' or bb_sig == 'short':
                self._clear_utbb_long_setup(symbol)
            if ut_state == 'long' or bb_sig == 'long':
                self._clear_utbb_short_setup(symbol)
            if ut_long_fresh:
                long_ut_setup = self._remember_utbb_long_setup(symbol, 'ut', ut_detail.get('signal_ts'))
            if bb_long_fresh:
                long_bb_setup = self._remember_utbb_long_setup(symbol, 'bb', bb_detail.get('signal_ts'))
            self._expire_utbb_long_setup(symbol, current_closed_ts, candle_ms, max_bars=3)
            long_ut_setup = self._get_utbb_long_setup(symbol, 'ut')
            long_bb_setup = self._get_utbb_long_setup(symbol, 'bb')

        long_pair_window_ready = self._is_utbb_long_setup_ready(symbol, candle_ms, max_bars=3) if symbol else False
        long_pair_entry_ready = long_pair_window_ready and (ut_long_fresh or bb_long_fresh)
        special_long_entry_ready = bool(ut_state == 'long' and bb_upper_sig == 'exit_long')
        short_setup = self._remember_utbb_short_setup(symbol, bb_sig, bb_detail) if symbol else None
        ut_signal_ts = ut_detail.get('signal_ts')
        short_setup_available = self._is_utbb_short_setup_available(symbol, ut_signal_ts) if symbol else False
        short_mid_ready = bb_mid_sig == 'short'
        short_rebound_fail_ready = bool(ut_state == 'short' and bb_mid_rebound_sig == 'short')
        short_setup_entry_ready = bool(ut_sig == 'short' and short_setup_available)
        short_mid_entry_ready = bool(ut_state == 'short' and short_mid_ready)
        special_short_candidate = short_mid_entry_ready and bb_lower_sig == 'exit_short'

        entry_sig = None
        if long_pair_entry_ready or special_long_entry_ready:
            entry_sig = 'long'
        elif short_setup_entry_ready or short_mid_entry_ready or short_rebound_fail_ready:
            entry_sig = 'short'

        if symbol and entry_sig == 'short':
            self._clear_utbb_long_setup(symbol)
            long_ut_setup = None
            long_bb_setup = None
            long_pair_window_ready = False
            long_pair_entry_ready = False

        short_entry_reason = None
        if ut_state == 'short':
            if special_short_candidate:
                short_entry_reason = (
                    "BB 중간선 하향돌파 + BB 하단 하향돌파 + UT 숏신호 확인"
                    if ut_sig == 'short'
                    else "BB 중간선 하향돌파 + BB 하단 하향돌파 + UT 숏상태 확인"
                )
            elif short_rebound_fail_ready:
                rebound_bulls = int(bb_mid_rebound_detail.get('setup_bull_count') or 0)
                short_entry_reason = (
                    f"BB 중간선 아래 양봉 {rebound_bulls}개 후 반등 실패 + UT 숏신호 확인"
                    if ut_sig == 'short'
                    else f"BB 중간선 아래 양봉 {rebound_bulls}개 후 반등 실패 + UT 숏상태 확인"
                )
            elif short_mid_entry_ready:
                short_entry_reason = (
                    "BB 중간선 하향돌파 + UT 숏신호 확인"
                    if ut_sig == 'short'
                    else "BB 중간선 하향돌파 + UT 숏상태 확인"
                )
            elif short_setup_entry_ready:
                short_entry_reason = "BB 상단 재진입 셋업 이후 UT 숏신호 확인"

        detail = {
            'ut_state': ut_state,
            'ut_signal': ut_sig,
            'ut_reason': ut_reason,
            'ut_signal_ts': ut_signal_ts,
            'timing_label': 'BB',
            'timing_signal': bb_mid_rebound_sig or bb_sig or bb_mid_sig,
            'effective_timing_signal': 'short' if (short_setup_entry_ready or short_mid_entry_ready or short_rebound_fail_ready) else None,
            'timing_match_source': (
                'mid_rebound_fail'
                if short_rebound_fail_ready else (
                    'mid_cross' if short_mid_entry_ready else ('latched' if short_setup_entry_ready else None)
                )
            ),
            'timing_reason': short_entry_reason or (bb_mid_rebound_reason if bb_mid_rebound_sig == 'short' else bb_reason),
            'timing_signal_ts': (
                bb_mid_rebound_detail.get('signal_ts')
                if short_rebound_fail_ready else (
                    bb_mid_detail.get('signal_ts') if short_mid_entry_ready else bb_detail.get('signal_ts')
                )
            ),
            'latched_timing_signal': short_setup.get('signal') if short_setup else None,
            'latched_timing_signal_ts': short_setup.get('signal_ts') if short_setup else None,
            'latched_timing_available': short_setup_available,
            'bb_short_setup_ready': short_setup_available,
            'bb_short_setup_ts': short_setup.get('signal_ts') if short_setup else None,
            'bb_short_signal': bb_sig,
            'bb_short_reason': bb_reason,
            'bb_mid_short_ready': short_mid_ready,
            'bb_mid_reason': bb_mid_reason,
            'bb_mid_signal_ts': bb_mid_detail.get('signal_ts'),
            'bb_mid_reentry_up': bb_mid_up_sig == 'exit_short',
            'bb_mid_up_reason': bb_mid_up_reason,
            'bb_mid_up_signal_ts': bb_mid_up_detail.get('signal_ts'),
            'bb_mid_rebound_fail_ready': short_rebound_fail_ready,
            'bb_mid_rebound_fail_reason': bb_mid_rebound_reason,
            'bb_mid_rebound_fail_signal_ts': bb_mid_rebound_detail.get('signal_ts'),
            'bb_mid_rebound_fail_bull_count': bb_mid_rebound_detail.get('setup_bull_count'),
            'bb_upper_breakout': bb_upper_sig == 'exit_long',
            'bb_mid_two_bearish_exit': bb_mid_two_bear_sig == 'exit_long',
            'bb_mid_two_bearish_reason': bb_mid_two_bear_reason,
            'bb_mid_two_bearish_signal_ts': bb_mid_two_bear_detail.get('signal_ts'),
            'ut_long_fresh': ut_long_fresh,
            'bb_long_fresh': bb_long_fresh,
            'utbb_long_window_bars': 3,
            'utbb_long_pair_ready': long_pair_entry_ready,
            'utbb_long_pair_source': (
                'same_candle'
                if (ut_long_fresh and bb_long_fresh) else (
                    'bb_first'
                    if ut_long_fresh and long_bb_setup else (
                        'ut_first' if bb_long_fresh and long_ut_setup else None
                    )
                )
            ),
            'utbb_long_ut_setup_ts': long_ut_setup.get('signal_ts') if long_ut_setup else None,
            'utbb_long_bb_setup_ts': long_bb_setup.get('signal_ts') if long_bb_setup else None,
            'bb_reentry_up': bb_sig == 'long',
            'bb_reentry_down': bb_sig == 'short',
            'bb_upper_reason': bb_upper_reason,
            'bb_upper_signal_ts': bb_upper_detail.get('signal_ts'),
            'bb_lower_breakout': bb_lower_sig == 'exit_short',
            'bb_lower_reason': bb_lower_reason,
            'bb_lower_signal_ts': bb_lower_detail.get('signal_ts'),
            'bb_special_short_candidate': special_short_candidate,
            'ut_detail': ut_detail,
            'timing_detail': bb_detail
        }

        if entry_sig == 'long':
            if special_long_entry_ready:
                if ut_sig == 'long':
                    return 'long', "UTBB LONG: UT 롱신호 + BB 상단 상향돌파 동시 발생", detail
                return 'long', "UTBB LONG: UT 롱상태 유지 + BB 상단 상향돌파 확인", detail
            if ut_long_fresh and bb_long_fresh:
                return 'long', "UTBB LONG: UT 롱신호 + BB 롱신호 동시 확인", detail
            if ut_long_fresh:
                return 'long', "UTBB LONG: 저장된 BB 롱 + UT 롱신호 확인", detail
            return 'long', "UTBB LONG: 저장된 UT 롱 + BB 롱신호 확인", detail
        if entry_sig == 'short':
            return 'short', f"UTBB SHORT: {short_entry_reason}", detail

        if long_ut_setup and not long_bb_setup:
            return None, "UTBB 대기: 저장된 UT 롱신호, 3봉 내 BB 롱신호 대기", detail
        if long_bb_setup and not long_ut_setup:
            return None, "UTBB 대기: 저장된 BB 롱신호, 3봉 내 UT 롱신호 대기", detail
        if ut_state == 'short' and not (short_setup_entry_ready or short_mid_entry_ready):
            wait_label = "UT 숏신호 감지" if ut_sig == 'short' else "UT 숏상태 유지"
            return None, f"UTBB 대기: {wait_label}, BB 상단 재진입 또는 중간선 하락 돌파 숏 셋업 대기", detail
        if ut_state == 'long':
            wait_label = "UT 롱신호 감지" if ut_sig == 'long' else "UT 롱상태 유지"
            return None, f"UTBB 대기: {wait_label}, BB 롱신호 대기", detail
        return None, "UTBB 대기", detail

    def _calculate_cameron_signal(self, df, strategy_params):
        cfg = strategy_params.get('Cameron', {})
        rsi_period = int(cfg.get('rsi_period', 14) or 14)
        rsi_oversold = float(cfg.get('rsi_oversold', 30) or 30)
        rsi_overbought = float(cfg.get('rsi_overbought', 70) or 70)
        bb_length = int(cfg.get('bollinger_length', 20) or 20)
        bb_std = float(cfg.get('bollinger_std', 2.0) or 2.0)
        macd_fast = int(cfg.get('macd_fast', 12) or 12)
        macd_slow = int(cfg.get('macd_slow', 26) or 26)
        macd_signal = int(cfg.get('macd_signal', 9) or 9)
        extreme_lookback = int(cfg.get('extreme_lookback', 60) or 60)
        macd_confirm_lookback = int(cfg.get('macd_confirm_lookback', 3) or 3)
        band_buffer_pct = float(cfg.get('band_buffer_pct', 0.001) or 0.0)

        closed = df.iloc[:-1].copy().reset_index(drop=True)
        min_bars = max(rsi_period + 5, bb_length + 5, macd_slow + macd_signal + 5, 40)
        if len(closed) < min_bars:
            return None, "CAMERON 데이터 부족", {}

        closed['rsi'] = ta.rsi(closed['close'], length=rsi_period)
        bb = ta.bbands(closed['close'], length=bb_length, std=bb_std)
        macd = ta.macd(closed['close'], fast=macd_fast, slow=macd_slow, signal=macd_signal)

        if bb is None or bb.empty or macd is None or macd.empty:
            return None, "CAMERON 지표 계산 대기", {}

        bb_lower = self._get_indicator_column(bb, 'BBL_')
        bb_upper = self._get_indicator_column(bb, 'BBU_')
        macd_line = self._get_indicator_column(macd, 'MACD_', exclude_prefixes=('MACDh_', 'MACDs_'))
        signal_line = self._get_indicator_column(macd, 'MACDs_')
        macd_hist = self._get_indicator_column(macd, 'MACDh_')

        if any(series is None for series in (bb_lower, bb_upper, macd_line, signal_line)):
            return None, "CAMERON 지표 컬럼 대기", {}

        closed['bb_lower'] = bb_lower
        closed['bb_upper'] = bb_upper
        closed['macd'] = macd_line
        closed['macd_signal'] = signal_line
        closed['macd_hist'] = macd_hist if macd_hist is not None else (macd_line - signal_line)

        trigger_idx = len(closed) - 1
        prev_idx = trigger_idx - 1
        if prev_idx < 0:
            return None, "CAMERON 캔들 대기", {}

        required_cols = ['open', 'high', 'low', 'close', 'rsi', 'bb_lower', 'bb_upper', 'macd', 'macd_signal']
        if closed.loc[[prev_idx, trigger_idx], required_cols].isna().any().any():
            return None, "CAMERON 지표 확정 대기", {}

        trigger = closed.iloc[trigger_idx]
        prev = closed.iloc[prev_idx]
        reason = "CAMERON 조건 대기"

        bullish_engulf = self._is_bullish_engulfing(prev, trigger)
        if bullish_engulf and trigger['low'] >= trigger['bb_lower'] * (1 - band_buffer_pct):
            has_cross_up, cross_idx = self._has_recent_macd_cross(
                closed['macd'], closed['macd_signal'], trigger_idx, 'up', macd_confirm_lookback
            )
            if has_cross_up and trigger['macd'] > trigger['macd_signal']:
                start_idx = max(0, trigger_idx - extreme_lookback)
                for idx in range(trigger_idx - 2, start_idx - 1, -1):
                    first = closed.iloc[idx]
                    if pd.isna(first[['rsi', 'bb_lower']]).any():
                        continue
                    if first['rsi'] > rsi_oversold:
                        continue
                    if first['low'] > first['bb_lower'] * (1 + band_buffer_pct):
                        continue
                    if trigger['low'] >= first['low']:
                        continue
                    if trigger['rsi'] <= first['rsi']:
                        continue
                    if cross_idx is not None and cross_idx <= idx:
                        continue

                    entry_ref_price = (float(trigger['open']) + float(trigger['close'])) / 2.0
                    detail = {
                        'side': 'long',
                        'stop_price': float(trigger['low']),
                        'entry_ref_price': entry_ref_price,
                        'signal_ts': int(trigger['timestamp']),
                        'trigger_open': float(trigger['open']),
                        'trigger_high': float(trigger['high']),
                        'trigger_low': float(trigger['low']),
                        'trigger_close': float(trigger['close']),
                        'first_extreme_ts': int(first['timestamp']),
                        'first_extreme_price': float(first['low']),
                        'first_extreme_rsi': float(first['rsi']),
                        'trigger_rsi': float(trigger['rsi'])
                    }
                    reason = (
                        f"CAMERON LONG: BB하단 확장 -> 상승 다이버전스 -> "
                        f"MACD 골든크로스 -> 장악형 양봉 (기준가 {entry_ref_price:.4f})"
                    )
                    return 'long', reason, detail

        bearish_engulf = self._is_bearish_engulfing(prev, trigger)
        if bearish_engulf and trigger['high'] <= trigger['bb_upper'] * (1 + band_buffer_pct):
            has_cross_down, cross_idx = self._has_recent_macd_cross(
                closed['macd'], closed['macd_signal'], trigger_idx, 'down', macd_confirm_lookback
            )
            if has_cross_down and trigger['macd'] < trigger['macd_signal']:
                start_idx = max(0, trigger_idx - extreme_lookback)
                for idx in range(trigger_idx - 2, start_idx - 1, -1):
                    first = closed.iloc[idx]
                    if pd.isna(first[['rsi', 'bb_upper']]).any():
                        continue
                    if first['rsi'] < rsi_overbought:
                        continue
                    if first['high'] < first['bb_upper'] * (1 - band_buffer_pct):
                        continue
                    if trigger['high'] <= first['high']:
                        continue
                    if trigger['rsi'] >= first['rsi']:
                        continue
                    if cross_idx is not None and cross_idx <= idx:
                        continue

                    entry_ref_price = (float(trigger['open']) + float(trigger['close'])) / 2.0
                    detail = {
                        'side': 'short',
                        'stop_price': float(trigger['high']),
                        'entry_ref_price': entry_ref_price,
                        'signal_ts': int(trigger['timestamp']),
                        'trigger_open': float(trigger['open']),
                        'trigger_high': float(trigger['high']),
                        'trigger_low': float(trigger['low']),
                        'trigger_close': float(trigger['close']),
                        'first_extreme_ts': int(first['timestamp']),
                        'first_extreme_price': float(first['high']),
                        'first_extreme_rsi': float(first['rsi']),
                        'trigger_rsi': float(trigger['rsi'])
                    }
                    reason = (
                        f"CAMERON SHORT: BB상단 확장 -> 하락 다이버전스 -> "
                        f"MACD 데드크로스 -> 장악형 음봉 (기준가 {entry_ref_price:.4f})"
                    )
                    return 'short', reason, detail

        return None, reason, {}
