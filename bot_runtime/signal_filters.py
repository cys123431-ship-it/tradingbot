"""UT filters, indicators, timing latches, and pending-entry state."""

from __future__ import annotations


class SignalFilterMixin:
    def _collect_primary_strategy_context(self, symbol, df, strategy_params, active_strategy):
        context = {
            'raw_strategy_sig': None,
            'raw_state_sig': None,
            'raw_ut_detail': {},
            'raw_hybrid_detail': {},
            'precomputed': {}
        }

        if active_strategy == 'utbot':
            utbot_result = self._calculate_utbot_signal(df, strategy_params)
            raw_strategy_sig, _, ut_detail = utbot_result
            context['raw_strategy_sig'] = raw_strategy_sig
            context['raw_state_sig'] = raw_strategy_sig or ut_detail.get('bias_side')
            context['raw_ut_detail'] = ut_detail or {}
            context['precomputed'][active_strategy] = utbot_result
        elif active_strategy == 'utsmc':
            utsmc_result = self._calculate_utsmc_signal(df, strategy_params)
            _, _, utsmc_detail = utsmc_result
            context['raw_strategy_sig'] = utsmc_detail.get('ut_signal')
            context['raw_state_sig'] = utsmc_detail.get('ut_state')
            context['raw_ut_detail'] = utsmc_detail.get('ut_detail') or {}
            context['raw_hybrid_detail'] = utsmc_detail or {}
            context['precomputed'][active_strategy] = utsmc_result
            self.last_utsmc_candidate_filter_status[symbol] = {
                'mode': utsmc_detail.get('candidate_filter_mode', 'off'),
                'mode_label': utsmc_detail.get('candidate_filter_mode_label', 'OFF'),
                'apply_to_persistent': bool(utsmc_detail.get('candidate_filter_apply_to_persistent', True)),
                'candidate1_long_pass': bool(utsmc_detail.get('candidate1_long_pass', False)),
                'candidate1_short_pass': bool(utsmc_detail.get('candidate1_short_pass', False)),
                'candidate2_long_pass': bool(utsmc_detail.get('candidate2_long_pass', False)),
                'candidate2_short_pass': bool(utsmc_detail.get('candidate2_short_pass', False)),
                'candidate_final_long_pass': bool(utsmc_detail.get('candidate_final_long_pass', True)),
                'candidate_final_short_pass': bool(utsmc_detail.get('candidate_final_short_pass', True)),
                'candidate_reason_long': utsmc_detail.get('candidate_reason_long'),
                'candidate_reason_short': utsmc_detail.get('candidate_reason_short')
            }
        elif active_strategy == 'utbb':
            utbb_result = self._calculate_utbb_signal(symbol, df, strategy_params)
            _, _, hybrid_detail = utbb_result
            context['raw_strategy_sig'] = hybrid_detail.get('ut_signal')
            context['raw_state_sig'] = hybrid_detail.get('ut_state')
            context['raw_ut_detail'] = hybrid_detail.get('ut_detail') or {}
            context['raw_hybrid_detail'] = hybrid_detail or {}
            context['precomputed'][active_strategy] = utbb_result
        elif active_strategy in UT_HYBRID_STRATEGIES:
            hybrid_calc = {
                'utrsibb': self._calculate_utrsibb_signal,
                'utrsi': self._calculate_utrsi_signal
            }.get(active_strategy)
            hybrid_result = hybrid_calc(symbol, df, strategy_params)
            _, _, hybrid_detail = hybrid_result
            context['raw_strategy_sig'] = hybrid_detail.get('ut_state')
            context['raw_state_sig'] = context['raw_strategy_sig']
            context['raw_ut_detail'] = hybrid_detail.get('ut_detail') or {}
            context['raw_hybrid_detail'] = hybrid_detail or {}
            context['precomputed'][active_strategy] = hybrid_result
        elif active_strategy == 'rsibb':
            rsibb_result = self._calculate_rsibb_signal(df, strategy_params)
            context['raw_strategy_sig'] = rsibb_result[0]
            context['raw_state_sig'] = rsibb_result[0]
            context['precomputed'][active_strategy] = rsibb_result

        return context

    def _get_exit_timeframe(self, symbol=None):
        """泥?궛????꾪봽?덉엫 (User Defined)
           醫낅ぉ???ㅼ틦?덉뿉 ?섑빐 ?≫엺 寃쎌슦 ?꾩슜 ??꾪봽?덉엫 諛섑솚
        """
        cfg = self.get_runtime_common_settings()
        strategy_params = self.get_runtime_strategy_params()
        active_strategy = str(strategy_params.get('active_strategy', 'utbot') or 'utbot').lower()
        if active_strategy in UTBREAKOUT_STRATEGIES:
            fb_cfg = self._get_utbot_filtered_breakout_config(strategy_params)
            if bool(fb_cfg.get('adaptive_timeframe_enabled', False)) and symbol:
                selected_tf = (self.utbreakout_adaptive_tf_state.get(symbol, {}) or {}).get('selected_tf')
                if selected_tf:
                    return selected_tf
            return fb_cfg.get('exit_timeframe', fb_cfg.get('entry_timeframe', '15m'))
        if symbol and symbol == self.scanner_active_symbol:
            return cfg.get('scanner_exit_timeframe', '1h')
        return cfg.get('exit_timeframe', '4h')

    def _get_utbot_filter_pack(self, strategy_params=None):
        params = strategy_params if isinstance(strategy_params, dict) else self.get_runtime_strategy_params()
        utbot_cfg = params.get('UTBot', {}) if isinstance(params, dict) else {}
        return normalize_utbot_filter_pack_config(utbot_cfg.get('filter_pack', {}))

    def _get_utbot_rsi_momentum_filter_config(self, strategy_params=None):
        params = strategy_params if isinstance(strategy_params, dict) else self.get_runtime_strategy_params()
        utbot_cfg = params.get('UTBot', {}) if isinstance(params, dict) else {}
        rsi_cfg = params.get('RSIMomentumTrend', {}) if isinstance(params, dict) else {}
        return {
            'enabled': bool(utbot_cfg.get('rsi_momentum_filter_enabled', False)),
            'rsi_length': max(1, int(rsi_cfg.get('rsi_length', 14) or 14)),
            'positive_above': float(rsi_cfg.get('positive_above', 65) or 65),
            'negative_below': float(rsi_cfg.get('negative_below', 32) or 32),
            'ema_period': max(1, int(rsi_cfg.get('ema_period', 5) or 5))
        }

    def _get_utbot_filter_pack_htf(self, tf):
        tf_text = str(tf or '').strip()
        if not tf_text:
            return '1d'
        if tf_text.endswith('M'):
            return '1d'
        mapping = {
            '1m': '15m',
            '2m': '15m',
            '3m': '15m',
            '4m': '15m',
            '5m': '30m',
            '15m': '1h',
            '30m': '4h',
            '1h': '1d',
            '2h': '1d',
            '4h': '1d',
            '6h': '1d',
            '8h': '1d',
            '12h': '1d',
            '1d': '1d'
        }
        return mapping.get(tf_text.lower(), '1d')

    def _combine_utbot_filter_flags(self, values, logic, default_value):
        flags = [bool(value) for value in values]
        if not flags:
            return default_value
        return all(flags) if normalize_utbot_filter_pack_logic(logic) == 'and' else any(flags)

    def _store_utbot_filter_pack_status(self, symbol, phase, evaluation, strategy_params=None):
        current = dict(self.last_utbot_filter_pack_status.get(symbol, {}) or {})
        current['config'] = self._get_utbot_filter_pack(strategy_params)
        current[phase] = evaluation or {}
        self.last_utbot_filter_pack_status[symbol] = current
        if isinstance(self.ctrl.status_data, dict):
            symbol_status = self.ctrl.status_data.get(symbol)
            if isinstance(symbol_status, dict):
                symbol_status['utbot_filter_pack'] = current

    def _store_utbot_rsi_momentum_filter_status(self, symbol, phase, evaluation, strategy_params=None):
        current = dict(self.last_utbot_rsi_momentum_filter_status.get(symbol, {}) or {})
        current['config'] = self._get_utbot_rsi_momentum_filter_config(strategy_params)
        current[phase] = evaluation or {}
        self.last_utbot_rsi_momentum_filter_status[symbol] = current
        if isinstance(self.ctrl.status_data, dict):
            symbol_status = self.ctrl.status_data.get(symbol)
            if isinstance(symbol_status, dict):
                symbol_status['utbot_rsi_momentum_filter'] = current

    def _build_utbot_filter_pack_side_text(self, evaluation, side):
        if side not in {'long', 'short'}:
            return 'UTBot filter pack off'
        if not isinstance(evaluation, dict) or not evaluation.get('selected'):
            return 'UTBot filter pack off'

        pass_key = 'long_pass' if side == 'long' else 'short_pass'
        reason_key = 'reason_long' if side == 'long' else 'reason_short'
        parts = []
        for filter_id in evaluation.get('selected', []):
            detail = (evaluation.get('filters') or {}).get(str(filter_id), {})
            status_text = 'PASS' if detail.get(pass_key, False) else 'FAIL'
            parts.append(
                f"{filter_id}:{detail.get('label', get_utbot_filter_pack_label(filter_id))} "
                f"{status_text} ({detail.get(reason_key, '-')})"
            )
        logic_text = format_utbot_filter_pack_logic(evaluation.get('logic', 'and'))
        return f"{logic_text} | " + " | ".join(parts) if parts else 'UTBot filter pack off'

    def _utbot_filter_pack_allows_entry(self, evaluation, side):
        if side not in {'long', 'short'}:
            return True, 'UTBot filter pack off'
        if not isinstance(evaluation, dict) or not evaluation.get('selected'):
            return True, 'UTBot filter pack off'
        pass_key = 'long_pass' if side == 'long' else 'short_pass'
        allowed = bool(evaluation.get(pass_key, False))
        return allowed, self._build_utbot_filter_pack_side_text(evaluation, side)

    def _build_utbot_rsi_momentum_filter_side_text(self, evaluation, side):
        if side not in {'long', 'short'}:
            return 'RSI Momentum Trend filter off'
        if not isinstance(evaluation, dict) or not evaluation.get('enabled', False):
            return 'RSI Momentum Trend filter off'
        pass_key = 'long_pass' if side == 'long' else 'short_pass'
        reason_key = 'reason_long' if side == 'long' else 'reason_short'
        status_text = 'PASS' if evaluation.get(pass_key, False) else 'FAIL'
        return f"RSI Momentum Trend {status_text} ({evaluation.get(reason_key, '-')})"

    def _utbot_rsi_momentum_filter_allows(self, evaluation, side):
        if side not in {'long', 'short'}:
            return True, 'RSI Momentum Trend filter off'
        if not isinstance(evaluation, dict) or not evaluation.get('enabled', False):
            return True, 'RSI Momentum Trend filter off'
        pass_key = 'long_pass' if side == 'long' else 'short_pass'
        allowed = bool(evaluation.get(pass_key, False))
        return allowed, self._build_utbot_rsi_momentum_filter_side_text(evaluation, side)

    def _evaluate_utbot_rsi_momentum_filter(self, df, strategy_params, phase='entry'):
        config = self._get_utbot_rsi_momentum_filter_config(strategy_params)
        evaluation = {
            'phase': phase,
            'enabled': bool(config.get('enabled', False)),
            'long_pass': True,
            'short_pass': True,
            'reason_long': 'RSI Momentum Trend filter off',
            'reason_short': 'RSI Momentum Trend filter off',
            'detail': {}
        }
        if not evaluation['enabled']:
            return evaluation

        signal_side, reason, detail = self._calculate_rsi_momentum_trend_signal(df, strategy_params)
        state_side = str(detail.get('state_side') or '').lower()
        base_reason = reason or 'RSI Momentum Trend 상태 대기'

        evaluation['detail'] = detail or {}
        evaluation['signal_side'] = signal_side
        evaluation['state_side'] = state_side or None

        if phase == 'exit':
            evaluation['long_pass'] = state_side == 'short'
            evaluation['short_pass'] = state_side == 'long'
            evaluation['reason_long'] = (
                f"{base_reason} | SHORT 반전 확인"
                if evaluation['long_pass'] else
                f"{base_reason} | SHORT 반전 미확인"
            )
            evaluation['reason_short'] = (
                f"{base_reason} | LONG 반전 확인"
                if evaluation['short_pass'] else
                f"{base_reason} | LONG 반전 미확인"
            )
        else:
            evaluation['long_pass'] = state_side == 'long'
            evaluation['short_pass'] = state_side == 'short'
            evaluation['reason_long'] = (
                f"{base_reason} | LONG 상태 일치"
                if evaluation['long_pass'] else
                f"{base_reason} | LONG 상태 대기"
            )
            evaluation['reason_short'] = (
                f"{base_reason} | SHORT 상태 일치"
                if evaluation['short_pass'] else
                f"{base_reason} | SHORT 상태 대기"
            )

        return evaluation

    def _calculate_utbot_filter_pack_chop(self, closed, length=14):
        if closed is None or len(closed) < (length + 1):
            return None, "CHOP 데이터 부족"

        high = closed['high'].astype(float)
        low = closed['low'].astype(float)
        close = closed['close'].astype(float)
        prev_close = close.shift(1)
        true_range = pd.concat([
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ], axis=1).max(axis=1)
        tr_sum = true_range.rolling(length).sum()
        highest_high = high.rolling(length).max()
        lowest_low = low.rolling(length).min()
        price_span = (highest_high - lowest_low).replace(0, np.nan)
        chop = 100.0 * np.log10(tr_sum / price_span) / np.log10(float(length))
        curr_chop = chop.iloc[-1]
        if pd.isna(curr_chop) or np.isinf(curr_chop):
            return None, "CHOP 계산 대기"
        return float(curr_chop), f"CHOP {float(curr_chop):.2f}"

    def _calculate_utbot_filter_pack_adx_dmi(self, closed, length=14):
        if closed is None or len(closed) < max((length * 2), (length + 5)):
            return None, None, None, "ADX 데이터 부족"

        high = closed['high'].astype(float).reset_index(drop=True)
        low = closed['low'].astype(float).reset_index(drop=True)
        close = closed['close'].astype(float).reset_index(drop=True)
        prev_high = high.shift(1)
        prev_low = low.shift(1)
        prev_close = close.shift(1)

        up_move = high - prev_high
        down_move = prev_low - low
        plus_dm = pd.Series(
            np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
            index=high.index,
            dtype=float
        )
        minus_dm = pd.Series(
            np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
            index=high.index,
            dtype=float
        )
        true_range = pd.concat([
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ], axis=1).max(axis=1).fillna(0.0)

        atr = self._calculate_rma_series(true_range, length)
        plus_dm_rma = self._calculate_rma_series(plus_dm, length)
        minus_dm_rma = self._calculate_rma_series(minus_dm, length)

        atr_safe = atr.replace(0, np.nan)
        plus_di = 100.0 * (plus_dm_rma / atr_safe)
        minus_di = 100.0 * (minus_dm_rma / atr_safe)
        dx_den = (plus_di + minus_di).replace(0, np.nan)
        dx = 100.0 * ((plus_di - minus_di).abs() / dx_den)
        adx = self._calculate_rma_series(dx.fillna(0.0), length)

        curr_adx = adx.iloc[-1]
        curr_plus = plus_di.iloc[-1]
        curr_minus = minus_di.iloc[-1]
        if pd.isna(curr_adx) or pd.isna(curr_plus) or pd.isna(curr_minus):
            return None, None, None, "ADX 계산 대기"

        return (
            float(curr_adx),
            float(curr_plus),
            float(curr_minus),
            f"ADX {float(curr_adx):.2f} | +DI {float(curr_plus):.2f} | -DI {float(curr_minus):.2f}"
        )

    def _calculate_utbot_filter_pack_vwap(self, closed):
        if closed is None or len(closed) < 2:
            return None, None, None, "VWAP 데이터 부족"

        high = closed['high'].astype(float)
        low = closed['low'].astype(float)
        close = closed['close'].astype(float)
        volume = closed['volume'].astype(float)
        session_key = (closed['timestamp'].astype('int64') // 86400000).astype('int64')
        typical_price = (high + low + close) / 3.0
        pv = typical_price * volume
        cum_pv = pv.groupby(session_key).cumsum()
        cum_vol = volume.groupby(session_key).cumsum().replace(0, np.nan)
        session_vwap = cum_pv / cum_vol
        curr_vwap = session_vwap.iloc[-1]
        prev_vwap = session_vwap.iloc[-2]
        curr_close = close.iloc[-1]
        if pd.isna(curr_vwap) or pd.isna(prev_vwap) or pd.isna(curr_close):
            return None, None, None, "VWAP 계산 대기"
        slope = float(curr_vwap - prev_vwap)
        return (
            float(curr_vwap),
            slope,
            float(curr_close),
            f"close {float(curr_close):.2f} | VWAP {float(curr_vwap):.2f} | slope {slope:+.4f}"
        )

    def _calculate_utbot_filter_pack_supertrend_direction(self, closed, length=10, multiplier=3.0):
        if closed is None or len(closed) < max((length + 5), 20):
            return None, None, "HTF Supertrend 데이터 부족"

        high = closed['high'].astype(float).reset_index(drop=True)
        low = closed['low'].astype(float).reset_index(drop=True)
        close = closed['close'].astype(float).reset_index(drop=True)
        prev_close = close.shift(1)
        true_range = pd.concat([
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ], axis=1).max(axis=1).fillna(0.0)
        atr = self._calculate_rma_series(true_range, length)
        if atr.isna().all():
            return None, None, "HTF Supertrend ATR 대기"

        hl2 = (high + low) / 2.0
        upper_band = hl2 + (multiplier * atr)
        lower_band = hl2 - (multiplier * atr)
        final_upper = pd.Series(np.nan, index=closed.index, dtype=float)
        final_lower = pd.Series(np.nan, index=closed.index, dtype=float)
        supertrend = pd.Series(np.nan, index=closed.index, dtype=float)
        direction = pd.Series(np.nan, index=closed.index, dtype=float)

        valid_indices = [idx for idx, value in enumerate(atr.tolist()) if not pd.isna(value)]
        if not valid_indices:
            return None, None, "HTF Supertrend ATR 대기"
        start_idx = valid_indices[0]
        final_upper.iloc[start_idx] = float(upper_band.iloc[start_idx])
        final_lower.iloc[start_idx] = float(lower_band.iloc[start_idx])
        direction.iloc[start_idx] = 1.0 if float(close.iloc[start_idx]) >= float(hl2.iloc[start_idx]) else -1.0
        supertrend.iloc[start_idx] = (
            float(final_lower.iloc[start_idx])
            if direction.iloc[start_idx] == 1.0
            else float(final_upper.iloc[start_idx])
        )

        for idx in range(start_idx + 1, len(closed)):
            if pd.isna(atr.iloc[idx]):
                continue

            prev_final_upper = float(final_upper.iloc[idx - 1])
            prev_final_lower = float(final_lower.iloc[idx - 1])
            prev_supertrend = float(supertrend.iloc[idx - 1])
            prev_close_val = float(close.iloc[idx - 1])
            curr_close_val = float(close.iloc[idx])

            curr_upper = float(upper_band.iloc[idx])
            curr_lower = float(lower_band.iloc[idx])
            final_upper.iloc[idx] = (
                curr_upper if (curr_upper < prev_final_upper or prev_close_val > prev_final_upper) else prev_final_upper
            )
            final_lower.iloc[idx] = (
                curr_lower if (curr_lower > prev_final_lower or prev_close_val < prev_final_lower) else prev_final_lower
            )

            if prev_supertrend == prev_final_upper:
                if curr_close_val <= float(final_upper.iloc[idx]):
                    supertrend.iloc[idx] = float(final_upper.iloc[idx])
                    direction.iloc[idx] = -1.0
                else:
                    supertrend.iloc[idx] = float(final_lower.iloc[idx])
                    direction.iloc[idx] = 1.0
            else:
                if curr_close_val >= float(final_lower.iloc[idx]):
                    supertrend.iloc[idx] = float(final_lower.iloc[idx])
                    direction.iloc[idx] = 1.0
                else:
                    supertrend.iloc[idx] = float(final_upper.iloc[idx])
                    direction.iloc[idx] = -1.0

        curr_direction = direction.dropna()
        curr_supertrend = supertrend.dropna()
        if curr_direction.empty or curr_supertrend.empty:
            return None, None, "HTF Supertrend 계산 대기"

        final_direction = 'long' if float(curr_direction.iloc[-1]) > 0 else 'short'
        final_band = float(curr_supertrend.iloc[-1])
        return final_direction, final_band, f"HTF Supertrend {final_direction.upper()} @ {final_band:.2f}"

    async def _evaluate_utbot_filter_pack(self, symbol, df, strategy_params, tf, phase):
        filter_pack = self._get_utbot_filter_pack(strategy_params)
        phase_cfg = filter_pack.get(phase, {})
        selected = normalize_utbot_filter_pack_selected(phase_cfg.get('selected', []))
        logic = normalize_utbot_filter_pack_logic(phase_cfg.get('logic', 'and'))
        mode_by_filter = dict(filter_pack.get('exit', {}).get('mode_by_filter', {})) if phase == 'exit' else {}
        evaluation = {
            'phase': phase,
            'selected': selected,
            'logic': logic,
            'mode_by_filter': mode_by_filter,
            'filters': {}
        }

        if not selected:
            if phase == 'entry':
                evaluation['long_pass'] = True
                evaluation['short_pass'] = True
            else:
                evaluation['confirm_selected'] = []
                evaluation['signal_selected'] = []
                evaluation['confirm_long_pass'] = True
                evaluation['confirm_short_pass'] = True
                evaluation['signal_long_trigger'] = False
                evaluation['signal_short_trigger'] = False
            return evaluation

        closed = df.iloc[:-1].copy().reset_index(drop=True)
        if len(closed) < 2:
            for filter_id in selected:
                evaluation['filters'][str(filter_id)] = {
                    'label': get_utbot_filter_pack_label(filter_id),
                    'long_pass': False,
                    'short_pass': False,
                    'reason_long': '확정봉 부족',
                    'reason_short': '확정봉 부족'
                }
            if phase == 'entry':
                evaluation['long_pass'] = False
                evaluation['short_pass'] = False
            else:
                evaluation['confirm_selected'] = [fid for fid in selected if mode_by_filter.get(str(fid), 'confirm') == 'confirm']
                evaluation['signal_selected'] = [fid for fid in selected if mode_by_filter.get(str(fid), 'confirm') == 'signal']
                evaluation['confirm_long_pass'] = False
                evaluation['confirm_short_pass'] = False
                evaluation['signal_long_trigger'] = False
                evaluation['signal_short_trigger'] = False
            return evaluation

        htf_tf = None
        htf_direction = None
        htf_band = None
        htf_reason = None
        if 4 in selected:
            htf_tf = self._get_utbot_filter_pack_htf(tf)
            try:
                htf_ohlcv = await asyncio.to_thread(self.market_data_exchange.fetch_ohlcv, symbol, htf_tf, limit=300)
                htf_df = pd.DataFrame(htf_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    htf_df[col] = pd.to_numeric(htf_df[col], errors='coerce')
                htf_closed = htf_df.iloc[:-1].copy().reset_index(drop=True)
                htf_direction, htf_band, htf_reason = self._calculate_utbot_filter_pack_supertrend_direction(
                    htf_closed,
                    length=10,
                    multiplier=3.0
                )
            except Exception as exc:
                htf_reason = f"HTF Supertrend fetch error: {exc}"

        smc_sig = None
        smc_reason = None
        smc_detail = {}
        if 5 in selected:
            smc_sig, smc_reason, smc_detail = self._calculate_smc_structure_signal(df, strategy_params)

        curr_chop = None
        adx_val = plus_di = minus_di = None
        curr_vwap = vwap_slope = curr_close = None
        chop_reason = adx_reason = vwap_reason = None
        if 1 in selected:
            curr_chop, chop_reason = self._calculate_utbot_filter_pack_chop(closed, length=14)
        if 2 in selected:
            adx_val, plus_di, minus_di, adx_reason = self._calculate_utbot_filter_pack_adx_dmi(closed, length=14)
        if 3 in selected:
            curr_vwap, vwap_slope, curr_close, vwap_reason = self._calculate_utbot_filter_pack_vwap(closed)

        for filter_id in selected:
            detail = {
                'label': get_utbot_filter_pack_label(filter_id),
                'long_pass': False,
                'short_pass': False,
                'reason_long': '-',
                'reason_short': '-'
            }

            if filter_id == 1:
                if curr_chop is None:
                    detail['reason_long'] = chop_reason or 'CHOP 계산 대기'
                    detail['reason_short'] = detail['reason_long']
                elif phase == 'entry':
                    detail['long_pass'] = curr_chop <= 50.0
                    detail['short_pass'] = curr_chop <= 50.0
                    detail['reason_long'] = f"{chop_reason} <= 50.00"
                    detail['reason_short'] = detail['reason_long']
                else:
                    detail['long_pass'] = curr_chop >= 61.8
                    detail['short_pass'] = curr_chop >= 61.8
                    detail['reason_long'] = f"{chop_reason} >= 61.80"
                    detail['reason_short'] = detail['reason_long']

            elif filter_id == 2:
                if adx_val is None or plus_di is None or minus_di is None:
                    detail['reason_long'] = adx_reason or 'ADX 계산 대기'
                    detail['reason_short'] = detail['reason_long']
                elif phase == 'entry':
                    detail['long_pass'] = adx_val >= 20.0 and plus_di > minus_di
                    detail['short_pass'] = adx_val >= 20.0 and minus_di > plus_di
                    detail['reason_long'] = f"{adx_reason} | +DI > -DI"
                    detail['reason_short'] = f"{adx_reason} | -DI > +DI"
                else:
                    detail['long_pass'] = adx_val >= 20.0 and minus_di > plus_di
                    detail['short_pass'] = adx_val >= 20.0 and plus_di > minus_di
                    detail['reason_long'] = f"{adx_reason} | -DI > +DI"
                    detail['reason_short'] = f"{adx_reason} | +DI > -DI"

            elif filter_id == 3:
                if curr_vwap is None or vwap_slope is None or curr_close is None:
                    detail['reason_long'] = vwap_reason or 'VWAP 계산 대기'
                    detail['reason_short'] = detail['reason_long']
                elif phase == 'entry':
                    detail['long_pass'] = curr_close > curr_vwap and vwap_slope > 0
                    detail['short_pass'] = curr_close < curr_vwap and vwap_slope < 0
                    detail['reason_long'] = f"{vwap_reason} | close > VWAP & slope > 0"
                    detail['reason_short'] = f"{vwap_reason} | close < VWAP & slope < 0"
                else:
                    detail['long_pass'] = curr_close < curr_vwap and vwap_slope < 0
                    detail['short_pass'] = curr_close > curr_vwap and vwap_slope > 0
                    detail['reason_long'] = f"{vwap_reason} | close < VWAP & slope < 0"
                    detail['reason_short'] = f"{vwap_reason} | close > VWAP & slope > 0"

            elif filter_id == 4:
                reason_text = htf_reason or 'HTF Supertrend 계산 대기'
                if htf_direction is None:
                    detail['reason_long'] = reason_text
                    detail['reason_short'] = reason_text
                elif phase == 'entry':
                    detail['long_pass'] = htf_direction == 'long'
                    detail['short_pass'] = htf_direction == 'short'
                    detail['reason_long'] = f"{htf_tf} | {reason_text} | LONG 일치"
                    detail['reason_short'] = f"{htf_tf} | {reason_text} | SHORT 일치"
                else:
                    detail['long_pass'] = htf_direction == 'short'
                    detail['short_pass'] = htf_direction == 'long'
                    detail['reason_long'] = f"{htf_tf} | {reason_text} | SHORT 반전"
                    detail['reason_short'] = f"{htf_tf} | {reason_text} | LONG 반전"
                if htf_band is not None:
                    detail['htf_band'] = float(htf_band)
                detail['htf_tf'] = htf_tf

            elif filter_id == 5:
                bullish_structure = bool(smc_detail.get('bullish_structure', False))
                bearish_structure = bool(smc_detail.get('bearish_structure', False))
                bullish_types = list(smc_detail.get('bullish_structure_types', []) or [])
                bearish_types = list(smc_detail.get('bearish_structure_types', []) or [])
                bullish_reason = (
                    f"BULLISH {', '.join(bullish_types)}"
                    if bullish_types else
                    "BULLISH BOS/CHoCH 대기"
                )
                bearish_reason = (
                    f"BEARISH {', '.join(bearish_types)}"
                    if bearish_types else
                    "BEARISH BOS/CHoCH 대기"
                )
                if phase == 'entry':
                    detail['long_pass'] = bullish_structure
                    detail['short_pass'] = bearish_structure
                    detail['reason_long'] = bullish_reason
                    detail['reason_short'] = bearish_reason
                else:
                    detail['long_pass'] = bearish_structure
                    detail['short_pass'] = bullish_structure
                    detail['reason_long'] = bearish_reason
                    detail['reason_short'] = bullish_reason

            evaluation['filters'][str(filter_id)] = detail

        if phase == 'entry':
            evaluation['long_pass'] = self._combine_utbot_filter_flags(
                [evaluation['filters'][str(filter_id)].get('long_pass', False) for filter_id in selected],
                logic,
                True
            )
            evaluation['short_pass'] = self._combine_utbot_filter_flags(
                [evaluation['filters'][str(filter_id)].get('short_pass', False) for filter_id in selected],
                logic,
                True
            )
        else:
            confirm_selected = [filter_id for filter_id in selected if mode_by_filter.get(str(filter_id), 'confirm') == 'confirm']
            signal_selected = [filter_id for filter_id in selected if mode_by_filter.get(str(filter_id), 'confirm') == 'signal']
            evaluation['confirm_selected'] = confirm_selected
            evaluation['signal_selected'] = signal_selected
            evaluation['confirm_long_pass'] = self._combine_utbot_filter_flags(
                [evaluation['filters'][str(filter_id)].get('long_pass', False) for filter_id in confirm_selected],
                logic,
                True
            )
            evaluation['confirm_short_pass'] = self._combine_utbot_filter_flags(
                [evaluation['filters'][str(filter_id)].get('short_pass', False) for filter_id in confirm_selected],
                logic,
                True
            )
            evaluation['signal_long_trigger'] = self._combine_utbot_filter_flags(
                [evaluation['filters'][str(filter_id)].get('long_pass', False) for filter_id in signal_selected],
                logic,
                False
            )
            evaluation['signal_short_trigger'] = self._combine_utbot_filter_flags(
                [evaluation['filters'][str(filter_id)].get('short_pass', False) for filter_id in signal_selected],
                logic,
                False
            )

        return evaluation

    async def prime_symbol_to_next_closed_candle(self, symbol):
        """When a symbol is newly selected, skip retroactive entry/exit on the latest closed candle."""
        try:
            common_cfg = self.get_runtime_common_settings()
            entry_tf = common_cfg.get('entry_timeframe', common_cfg.get('timeframe', '15m'))
            exit_tf = self._get_exit_timeframe(symbol)

            entry_ohlcv = await asyncio.to_thread(self.market_data_exchange.fetch_ohlcv, symbol, entry_tf, limit=5)
            if entry_ohlcv and len(entry_ohlcv) >= 3:
                entry_closed_ts = int(entry_ohlcv[-2][0])
                self.last_processed_candle_ts[symbol] = entry_closed_ts
                self.last_state_sync_candle_ts[symbol] = entry_closed_ts
                self.last_candle_time[symbol] = entry_closed_ts
                self.last_candle_success[symbol] = True
                self.last_stateful_retry_ts[symbol] = time.time()

            exit_ohlcv = await asyncio.to_thread(self.market_data_exchange.fetch_ohlcv, symbol, exit_tf, limit=5)
            if exit_ohlcv and len(exit_ohlcv) >= 3:
                exit_closed_ts = int(exit_ohlcv[-2][0])
                self.last_processed_exit_candle_ts[symbol] = exit_closed_ts

            logger.info(
                f"Primed symbol state for {symbol}: next action waits for future closed candle "
                f"(entry_tf={entry_tf}, exit_tf={exit_tf})"
            )
        except Exception as e:
            logger.warning(f"Prime symbol state failed for {symbol}: {e}")

    def _calculate_kalman_values(self, df, kalman_cfg):
        import numpy as np
        obs_cov = kalman_cfg.get('observation_covariance', 0.1)
        trans_cov = kalman_cfg.get('transition_covariance', 0.05)
        prices = df['close'].values

        kf = PyKalmanFilter(
            transition_matrices=np.array([[1, 1], [0, 1]]),
            observation_matrices=np.array([[1, 0]]),
            initial_state_mean=np.array([prices[0], 0]),
            initial_state_covariance=np.eye(2),
            observation_covariance=obs_cov * np.eye(1),
            transition_covariance=trans_cov * np.eye(2)
        )
        state_means, _ = kf.filter(prices)
        velocities = state_means[:, 1]
        c_vel = velocities[-2] # Completed candle
        return c_vel

    def _get_indicator_column(self, frame, prefix, exclude_prefixes=()):
        for col in frame.columns:
            name = str(col)
            if name.startswith(prefix) and not any(name.startswith(ex) for ex in exclude_prefixes):
                return frame[col]
        return None

    def _is_bullish_engulfing(self, prev_row, curr_row, tol=0.0):
        return (
            prev_row['close'] < prev_row['open']
            and curr_row['close'] > curr_row['open']
            and curr_row['open'] <= prev_row['close'] + tol
            and curr_row['close'] >= prev_row['open'] - tol
        )

    def _is_bearish_engulfing(self, prev_row, curr_row, tol=0.0):
        return (
            prev_row['close'] > prev_row['open']
            and curr_row['close'] < curr_row['open']
            and curr_row['open'] >= prev_row['close'] - tol
            and curr_row['close'] <= prev_row['open'] + tol
        )

    def _has_recent_macd_cross(self, macd_line, signal_line, end_idx, direction, lookback):
        start_idx = max(1, end_idx - max(1, int(lookback)) + 1)
        for idx in range(start_idx, end_idx + 1):
            prev_diff = macd_line.iloc[idx - 1] - signal_line.iloc[idx - 1]
            curr_diff = macd_line.iloc[idx] - signal_line.iloc[idx]
            if direction == 'up' and prev_diff <= 0 < curr_diff:
                return True, idx
            if direction == 'down' and prev_diff >= 0 > curr_diff:
                return True, idx
        return False, None

    def _get_ut_hybrid_latch_key(self, symbol, hybrid_strategy):
        return f"{str(hybrid_strategy).lower()}::{symbol}"

    def _remember_ut_hybrid_timing_signal(self, symbol, hybrid_strategy, timing_sig, timing_detail):
        if str(hybrid_strategy).lower() not in {'utrsi', 'utbb'}:
            return None
        if timing_sig not in {'long', 'short'}:
            return self.ut_hybrid_timing_latches.get(self._get_ut_hybrid_latch_key(symbol, hybrid_strategy))

        signal_ts = int(timing_detail.get('signal_ts') or 0)
        if signal_ts <= 0:
            return self.ut_hybrid_timing_latches.get(self._get_ut_hybrid_latch_key(symbol, hybrid_strategy))

        key = self._get_ut_hybrid_latch_key(symbol, hybrid_strategy)
        current = self.ut_hybrid_timing_latches.get(key, {})
        current_ts = int(current.get('signal_ts') or 0)
        if signal_ts >= current_ts:
            self.ut_hybrid_timing_latches[key] = {
                'signal': timing_sig,
                'signal_ts': signal_ts
            }
        return self.ut_hybrid_timing_latches.get(key)

    def _get_ut_hybrid_timing_latch(self, symbol, hybrid_strategy):
        return self.ut_hybrid_timing_latches.get(self._get_ut_hybrid_latch_key(symbol, hybrid_strategy))

    def _is_ut_hybrid_timing_latch_available(self, symbol, hybrid_strategy, side=None):
        latch = self._get_ut_hybrid_timing_latch(symbol, hybrid_strategy)
        if not latch:
            return False
        if side and latch.get('signal') != side:
            return False
        key = self._get_ut_hybrid_latch_key(symbol, hybrid_strategy)
        consumed_ts = int(self.ut_hybrid_timing_consumed_ts.get(key, 0) or 0)
        latch_ts = int(latch.get('signal_ts', 0) or 0)
        return latch_ts > consumed_ts

    def _consume_ut_hybrid_timing_latch(self, symbol, hybrid_strategy, side=None):
        if str(hybrid_strategy).lower() not in {'utrsi', 'utbb'}:
            return
        latch = self._get_ut_hybrid_timing_latch(symbol, hybrid_strategy)
        if not latch:
            return
        if side and latch.get('signal') != side:
            return
        key = self._get_ut_hybrid_latch_key(symbol, hybrid_strategy)
        self.ut_hybrid_timing_consumed_ts[key] = int(latch.get('signal_ts', 0) or 0)

    def _remember_utbb_short_setup(self, symbol, bb_sig, bb_detail):
        if bb_sig != 'short':
            return self.ut_hybrid_timing_latches.get(self._get_ut_hybrid_latch_key(symbol, 'utbb_short_setup'))
        signal_ts = int(bb_detail.get('signal_ts') or 0)
        if signal_ts <= 0:
            return self.ut_hybrid_timing_latches.get(self._get_ut_hybrid_latch_key(symbol, 'utbb_short_setup'))
        key = self._get_ut_hybrid_latch_key(symbol, 'utbb_short_setup')
        self.ut_hybrid_timing_latches[key] = {
            'signal': 'short',
            'signal_ts': signal_ts
        }
        return self.ut_hybrid_timing_latches.get(key)

    def _get_utbb_short_setup(self, symbol):
        return self.ut_hybrid_timing_latches.get(self._get_ut_hybrid_latch_key(symbol, 'utbb_short_setup'))

    def _is_utbb_short_setup_available(self, symbol, ut_signal_ts=None):
        setup = self._get_utbb_short_setup(symbol)
        if not setup:
            return False
        key = self._get_ut_hybrid_latch_key(symbol, 'utbb_short_setup')
        consumed_ts = int(self.ut_hybrid_timing_consumed_ts.get(key, 0) or 0)
        setup_ts = int(setup.get('signal_ts', 0) or 0)
        if setup_ts <= consumed_ts:
            return False
        if ut_signal_ts is not None:
            try:
                return int(ut_signal_ts or 0) > setup_ts
            except (TypeError, ValueError):
                return False
        return True

    def _consume_utbb_short_setup(self, symbol):
        setup = self._get_utbb_short_setup(symbol)
        if not setup:
            return
        key = self._get_ut_hybrid_latch_key(symbol, 'utbb_short_setup')
        self.ut_hybrid_timing_consumed_ts[key] = int(setup.get('signal_ts', 0) or 0)

    def _clear_utbb_short_setup(self, symbol):
        key = self._get_ut_hybrid_latch_key(symbol, 'utbb_short_setup')
        self.ut_hybrid_timing_latches.pop(key, None)
        self.ut_hybrid_timing_consumed_ts.pop(key, None)

    def _get_utbb_long_setup_key(self, symbol, source):
        return self._get_ut_hybrid_latch_key(symbol, f'utbb_long_{source}')

    def _remember_utbb_long_setup(self, symbol, source, signal_ts=None):
        source = str(source or '').lower()
        if source not in {'ut', 'bb'}:
            return None
        signal_ts = int(signal_ts or 0)
        if signal_ts <= 0:
            return self.ut_hybrid_timing_latches.get(self._get_utbb_long_setup_key(symbol, source))
        key = self._get_utbb_long_setup_key(symbol, source)
        current = self.ut_hybrid_timing_latches.get(key, {})
        current_ts = int(current.get('signal_ts') or 0)
        if signal_ts >= current_ts:
            self.ut_hybrid_timing_latches[key] = {
                'signal': 'long',
                'signal_ts': signal_ts
            }
        return self.ut_hybrid_timing_latches.get(key)

    def _get_utbb_long_setup(self, symbol, source):
        return self.ut_hybrid_timing_latches.get(self._get_utbb_long_setup_key(symbol, source))

    def _clear_utbb_long_setup(self, symbol, source=None):
        if source is None:
            self.ut_hybrid_timing_latches.pop(self._get_utbb_long_setup_key(symbol, 'ut'), None)
            self.ut_hybrid_timing_latches.pop(self._get_utbb_long_setup_key(symbol, 'bb'), None)
            return
        self.ut_hybrid_timing_latches.pop(self._get_utbb_long_setup_key(symbol, source), None)

    def _get_ut_strategy_signal_state_key(self, symbol, strategy_key):
        return f"{str(strategy_key or '').lower()}::{symbol}"

    def _remember_ut_strategy_signal_state(self, symbol, strategy_key, current_sig):
        state_key = self._get_ut_strategy_signal_state_key(symbol, strategy_key)
        parsed_sig = str(current_sig or '').lower()
        if parsed_sig not in {'long', 'short'}:
            self.ut_strategy_signal_state.pop(state_key, None)
            return None

        previous_sig = str(self.ut_strategy_signal_state.get(state_key) or '').lower()
        self.ut_strategy_signal_state[state_key] = parsed_sig
        return parsed_sig if parsed_sig != previous_sig else None

    def _clear_ut_strategy_signal_state(self, symbol, strategy_key=None):
        symbol_text = str(symbol or '').strip()
        if not symbol_text:
            return
        if strategy_key:
            self.ut_strategy_signal_state.pop(
                self._get_ut_strategy_signal_state_key(symbol_text, strategy_key),
                None
            )
            return

        suffix = f"::{symbol_text}"
        for state_key in list(self.ut_strategy_signal_state.keys()):
            if str(state_key).endswith(suffix):
                self.ut_strategy_signal_state.pop(state_key, None)

    def _set_ut_pending_entry(
        self,
        symbol,
        side,
        signal_ts,
        execute_ts,
        strategy_name=None,
        hybrid_strategy=None,
        source=None,
        expires_ts=None,
        signal_side=None,
        block_reason=None,
        retry_count=0,
        pending_state=None
    ):
        if side not in {'long', 'short'}:
            return
        self.ut_pending_entries[symbol] = {
            'side': side,
            'signal_ts': int(signal_ts or 0),
            'execute_ts': int(execute_ts or 0),
            'expires_ts': int(expires_ts or 0),
            'strategy_name': str(strategy_name or 'UT'),
            'hybrid_strategy': str(hybrid_strategy or '').lower() or None,
            'source': str(source or 'signal').strip().lower() or 'signal',
            'signal_side': str(signal_side or side).strip().lower() or side,
            'block_reason': str(block_reason or '').strip() or None,
            'retry_count': max(0, int(retry_count or 0)),
            'pending_state': str(pending_state or 'armed').strip().lower() or 'armed'
        }

    def _get_ut_pending_entry(self, symbol):
        return self.ut_pending_entries.get(symbol)

    def _touch_ut_pending_entry(self, symbol, **updates):
        pending = dict(self.ut_pending_entries.get(symbol) or {})
        if not pending:
            return None
        pending.update(updates)
        self.ut_pending_entries[symbol] = pending
        return pending

    def _get_ut_pending_source(self, pending):
        return str((pending or {}).get('source') or 'signal').strip().lower() or 'signal'

    def _is_ut_flip_pending(self, pending):
        return self._get_ut_pending_source(pending) == 'flip'

    def _clear_ut_pending_entry(self, symbol):
        self.ut_pending_entries.pop(symbol, None)
