"""UTBreakout signal evaluation, status rendering, and opposite-set exits."""

from __future__ import annotations


class SignalBreakoutStatusMixin:
    async def _calculate_utbot_filtered_breakout_signal(
        self,
        symbol,
        df,
        strategy_params,
        *,
        force_reprocess=False,
    ):
        cfg = self._get_utbot_filtered_breakout_config(strategy_params)
        self._clear_utbot_filtered_breakout_entry_plan(symbol)
        active_strategy = str((strategy_params or {}).get('active_strategy', UTBOT_FILTERED_BREAKOUT_STRATEGY) or UTBOT_FILTERED_BREAKOUT_STRATEGY).lower()
        strategy_label = STRATEGY_DISPLAY_NAMES.get(active_strategy, 'UTBOT_FILTERED_BREAKOUT_V1')
        status = {
            'strategy': strategy_label,
            'effective_profile_version': cfg.get('effective_profile_version'),
            'stage': 'evaluate',
            'entry_timeframe': cfg.get('entry_timeframe', '15m'),
            'htf_timeframe': cfg.get('htf_timeframe', '1h'),
            'adaptive_timeframe_enabled': bool(cfg.get('adaptive_timeframe_enabled', False)),
            'utbot_key_value': cfg.get('utbot_key_value'),
            'utbot_atr_period': cfg.get('utbot_atr_period'),
            'use_heikin_ashi': cfg.get('use_heikin_ashi', False)
        }
        if force_reprocess:
            status['force_reprocess'] = True

        def _finish(sig, reason, code=None, *, record_failure=False, side=None):
            status['reason'] = reason
            if code:
                status['reject_code'] = code
                status['stage'] = 'entry_rejected'
            else:
                status['stage'] = 'entry_ready' if sig else 'waiting'
            if sig:
                status['accepted_code'] = 'ACCEPTED_ENTRY'
                status['accepted_side'] = sig
            self._store_utbot_filtered_breakout_status(symbol, status)
            self.last_entry_reason[symbol] = reason
            self._record_utbreakout_diagnostic_event(symbol, status)
            if record_failure and side and not bool(cfg.get('dual_alpha_direction_filter_enabled', False)):
                self._record_utbot_filtered_breakout_failure(
                    symbol,
                    side,
                    status.get('decision_candle_ts'),
                    code or reason
                )
            return sig, reason, status

        if self.is_upbit_mode():
            return _finish(None, "REJECTED_HTF_TREND: Upbit spot mode is not supported", 'REJECTED_HTF_TREND')

        if df is None or len(df) < 5:
            return _finish(None, "UTBOT_FILTERED_BREAKOUT_V1 데이터 부족", None)

        if bool(cfg.get('adaptive_timeframe_enabled', False)):
            try:
                cfg, df, adaptive_decision = await self._resolve_utbreakout_adaptive_timeframe(symbol, df, cfg)
                cfg = apply_profit_opportunity_effective_overrides(cfg)
                status.update({
                    'entry_timeframe': cfg.get('entry_timeframe', '15m'),
                    'exit_timeframe': cfg.get('exit_timeframe', cfg.get('entry_timeframe', '15m')),
                    'htf_timeframe': cfg.get('htf_timeframe', '1h'),
                    'adaptive_timeframe_enabled': True,
                    'adaptive_timeframe_decision': adaptive_decision,
                    'adaptive_timeframe_summary': self._format_adaptive_timeframe_summary(adaptive_decision),
                    'adaptive_selected_tf': (adaptive_decision or {}).get('selected_tf') if isinstance(adaptive_decision, dict) else None,
                    'adaptive_previous_tf': (adaptive_decision or {}).get('previous_tf') if isinstance(adaptive_decision, dict) else None,
                    'adaptive_top3': (adaptive_decision or {}).get('top3') if isinstance(adaptive_decision, dict) else None,
                })
                if not (adaptive_decision or {}).get('selected_tf'):
                    return _finish(
                        None,
                        f"REJECTED_TIMEFRAME_NO_TRADE: {self._format_adaptive_timeframe_summary(adaptive_decision)}",
                        'REJECTED_TIMEFRAME_NO_TRADE'
                    )
            except Exception as exc:
                return _finish(
                    None,
                    f"REJECTED_TIMEFRAME_NO_TRADE: Adaptive TF 분석 실패 ({exc})",
                    'REJECTED_TIMEFRAME_NO_TRADE'
                )

        closed = df.iloc[:-1].copy().reset_index(drop=True)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            closed[col] = pd.to_numeric(closed[col], errors='coerce')
        closed = closed.dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)

        selected_set, auto_analysis, auto_reason = await self._resolve_utbreakout_selected_set(symbol, df, cfg)
        set_params = selected_set.get('params', {}) if isinstance(selected_set, dict) else {}
        effective_cfg = dict(cfg)
        effective_cfg.update(set_params)
        effective_cfg['active_set_id'] = int(selected_set.get('id', cfg.get('active_set_id', UTBREAKOUT_DEFAULT_SET_ID)))
        effective_cfg['profile'] = f"set{effective_cfg['active_set_id']}"
        cfg = apply_stable_utbreak_final_overrides(effective_cfg)
        cfg = apply_profit_opportunity_effective_overrides(cfg)
        if bool(cfg.get('auto_select_enabled', False)):
            if not isinstance(getattr(self, 'utbreakout_last_selected_set_ids', None), dict):
                self.utbreakout_last_selected_set_ids = {}
            self.utbreakout_last_selected_set_ids[symbol] = int(selected_set.get('id'))
        self._update_utbreakout_shadow_triple_barrier(symbol, closed, cfg)
        selected_filters = set(selected_set.get('entry_filters') or [])
        status.update({
            'selection_mode': 'auto' if cfg.get('auto_select_enabled') else 'manual',
            'auto_select_enabled': bool(cfg.get('auto_select_enabled', False)),
            'auto_selected_set_id': selected_set.get('id'),
            'auto_selected_set_name': selected_set.get('name'),
            'auto_stable_selection_detail': selected_set.get('_stable_selection_detail'),
            'auto_selected_set_family': selected_set.get('family'),
            'auto_selected_set_status': selected_set.get('status'),
            'auto_selection_reason': auto_reason,
            'auto_scores': (auto_analysis or {}).get('scores') if isinstance(auto_analysis, dict) else None,
            'set_filters': list(selected_set.get('entry_filters') or []),
            'utbot_key_value': cfg.get('utbot_key_value'),
            'utbot_atr_period': cfg.get('utbot_atr_period'),
        })

        auto_scores = (auto_analysis or {}).get('scores') if isinstance(auto_analysis, dict) else {}
        if (
            bool(cfg.get('auto_select_enabled', False))
            and isinstance(auto_scores, dict)
            and auto_scores.get('auto_blocked_by_margin')
        ):
            return _finish(
                None,
                f"REJECTED_AUTO_WEAK_MARGIN: {auto_scores.get('auto_block_reason')}",
                'REJECTED_AUTO_WEAK_MARGIN',
                record_failure=True,
            )

        if selected_set.get('status') != 'active':
            return _finish(
                None,
                f"REJECTED_RISK_REWARD_LOW: Set{selected_set.get('id')} is planned only and not connected to orders",
                'REJECTED_RISK_REWARD_LOW'
            )

        min_candidates = [
            int(cfg.get('utbot_atr_period', 14) or 14) + 5,
            int(cfg.get('atr_length', 14) or 14) + 5,
            30,
        ]
        if 'ema_slope' in selected_filters:
            min_candidates.append(int(cfg.get('ema_fast', 50) or 50) + 5)
        if 'rsi_momentum' in selected_filters:
            min_candidates.append(int(cfg.get('rsi_length', 14) or 14) + 5)
        if 'adx_loose' in selected_filters or 'adx_dmi' in selected_filters:
            min_candidates.append(int(cfg.get('adx_length', 14) or 14) * 2 + 5)
        if 'donchian_breakout' in selected_filters or 'donchian_width' in selected_filters:
            min_candidates.append(int(cfg.get('donchian_length', 20) or 20) + 2)
        min_bars = max(min_candidates)
        if len(closed) < min_bars:
            return _finish(None, f"UTBOT_FILTERED_BREAKOUT_V1 유효 데이터 부족 ({len(closed)}/{min_bars})", None)

        decision_row = closed.iloc[-1]
        decision_ts = int(decision_row.get('timestamp') or 0)
        entry_price = float(decision_row['close'])
        status['decision_candle_ts'] = decision_ts
        status['feed_last_ts'] = int(df.iloc[-1]['timestamp']) if len(df) else decision_ts
        status['entry_price'] = entry_price
        if bool(cfg.get('adaptive_timeframe_enabled', False)):
            tf_key = str(cfg.get('entry_timeframe', '15m') or '15m')
            skip_decision, last_adaptive_ts = self._utbreakout_should_skip_adaptive_decision(
                symbol,
                tf_key,
                decision_ts,
                force_reprocess=force_reprocess,
            )
            if skip_decision:
                return _finish(
                    None,
                    f"ADAPTIVE_TF 대기: {tf_key} 마감봉 {decision_ts} 이미 평가 완료",
                    None
                )
            if force_reprocess and decision_ts <= last_adaptive_ts:
                logger.warning(
                    "[UTBOT_FILTERED_BREAKOUT_V1] force reprocessing adaptive decision: "
                    "symbol=%s tf=%s candle=%s last=%s",
                    symbol,
                    tf_key,
                    decision_ts,
                    last_adaptive_ts,
                )

        ut_sig, ut_reason, ut_detail = self._calculate_utbot_signal(
            df,
            self._get_utbot_filtered_breakout_ut_params(cfg)
        )
        ut_detail = ut_detail or {}
        ut_bias_side = str(ut_detail.get('bias_side') or '').lower()
        candidate_side = ut_sig if ut_sig in {'long', 'short'} else ut_bias_side if ut_bias_side in {'long', 'short'} else None
        candidate_type = 'fresh_signal' if ut_sig in {'long', 'short'} else 'bias_state' if candidate_side else None
        status.update({
            'fresh_signal': ut_sig,
            'candidate_signal': candidate_side,
            'candidate_side': candidate_side,
            'candidate_type': candidate_type,
            'ut_bias_side': ut_bias_side,
            'ut_reason': ut_reason,
            'ut_curr_src': ut_detail.get('curr_src'),
            'ut_curr_stop': ut_detail.get('curr_stop'),
            'ut_curr_atr': ut_detail.get('curr_atr'),
            'ut_signal_ts': ut_detail.get('signal_ts')
        })

        if candidate_side not in {'long', 'short'}:
            return _finish(None, "UTBOT_FILTERED_BREAKOUT_V1 후보 신호 대기", None)

        side = candidate_side
        if self._micro_auto_enabled():
            micro_cfg = self._get_micro_auto_config()
            cfg['daily_max_loss_usdt'] = micro_cfg.get('daily_loss_limit_usdt', cfg.get('daily_max_loss_usdt'))
            cfg['max_daily_trades'] = micro_cfg.get('max_daily_trades', cfg.get('max_daily_trades'))
            cfg['max_consecutive_losses'] = micro_cfg.get('max_consecutive_losses', cfg.get('max_consecutive_losses'))
            cfg['risk_per_trade_percent'] = micro_cfg.get('risk_per_trade_pct', cfg.get('risk_per_trade_percent'))
            cfg['max_risk_per_trade_usdt'] = micro_cfg.get('max_risk_usdt', cfg.get('max_risk_per_trade_usdt'))
        daily_count, daily_pnl = self.db.get_daily_stats()
        daily_entries = self.db.get_daily_entry_count()
        status['daily_pnl'] = daily_pnl
        status['daily_entries'] = daily_entries
        if float(cfg.get('daily_max_loss_usdt', 0) or 0) > 0 and float(daily_pnl or 0) <= -float(cfg['daily_max_loss_usdt']):
            return _finish(
                None,
                f"REJECTED_DAILY_LOSS_LIMIT: daily pnl {daily_pnl:.2f} <= -{float(cfg['daily_max_loss_usdt']):.2f}",
                'REJECTED_DAILY_LOSS_LIMIT',
                record_failure=False,
                side=side
            )
        if int(cfg.get('max_daily_trades', 0) or 0) > 0 and daily_entries >= int(cfg['max_daily_trades']):
            return _finish(
                None,
                f"REJECTED_DAILY_LOSS_LIMIT: daily trade count {daily_entries} >= {int(cfg['max_daily_trades'])}",
                'REJECTED_DAILY_LOSS_LIMIT',
                record_failure=False,
                side=side
            )
        if bool(cfg.get('daily_profit_target_enabled', False)) and float(daily_pnl or 0) >= float(cfg.get('daily_profit_target_usdt', 0) or 0):
            return _finish(
                None,
                f"REJECTED_DAILY_LOSS_LIMIT: daily target reached {daily_pnl:.2f}",
                'REJECTED_DAILY_LOSS_LIMIT',
                record_failure=False,
                side=side
            )

        max_losses = int(cfg.get('max_consecutive_losses', 3) or 3)
        utbreak_strategy_family = {
            ENTRY_STRATEGY_UT_BREAKOUT,
            UTBOT_FILTERED_BREAKOUT_STRATEGY,
            UTBOT_ADAPTIVE_TIMEFRAME_STRATEGY,
        }
        recent_pnls = self._get_recent_strategy_closed_trade_pnls(
            utbreak_strategy_family,
            max_losses,
            today_only=True,
        )
        status['recent_closed_pnls'] = recent_pnls
        status['recent_closed_pnl_scope'] = 'utbreak_strategy_family'
        if len(recent_pnls) >= max_losses and all(float(pnl) < 0 for pnl in recent_pnls[:max_losses]):
            return _finish(
                None,
                f"REJECTED_CONSECUTIVE_LOSSES: last {max_losses} closed trades are losses",
                'REJECTED_CONSECUTIVE_LOSSES',
                record_failure=False,
                side=side
            )

        ema_fast_len = int(cfg['ema_fast'])
        ema_slow_len = int(cfg['ema_slow'])
        htf_closed = None
        htf_ready = False
        htf_error = None
        htf_curr_close = htf_ema_fast = htf_ema_slow = htf_gap_pct = np.nan
        htf_supertrend_direction = None
        htf_supertrend_reason = None
        try:
            htf_ohlcv = await asyncio.to_thread(
                self.market_data_exchange.fetch_ohlcv,
                symbol,
                cfg.get('htf_timeframe', '1h'),
                limit=300
            )
            htf_df = pd.DataFrame(htf_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            for col in ['open', 'high', 'low', 'close', 'volume']:
                htf_df[col] = pd.to_numeric(htf_df[col], errors='coerce')
            htf_closed = htf_df.iloc[:-1].dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
            if len(htf_closed) >= ema_slow_len + 2:
                htf_close = htf_closed['close'].astype(float)
                htf_ema_fast = float(htf_close.ewm(span=ema_fast_len, adjust=False).mean().iloc[-1])
                htf_ema_slow = float(htf_close.ewm(span=ema_slow_len, adjust=False).mean().iloc[-1])
                htf_curr_close = float(htf_close.iloc[-1])
                htf_gap_pct = abs(htf_ema_fast - htf_ema_slow) / max(abs(htf_curr_close), 1e-9) * 100.0
                htf_ready = True
            else:
                htf_error = f"HTF 데이터 부족 ({len(htf_closed)}/{ema_slow_len + 2})"
            if htf_closed is not None:
                htf_supertrend_direction, htf_supertrend_band, htf_supertrend_reason = self._calculate_utbot_filter_pack_supertrend_direction(
                    htf_closed,
                    length=10,
                    multiplier=3.0
                )
                status['htf_supertrend_band'] = htf_supertrend_band
        except Exception as e:
            htf_error = f"HTF fetch failed ({e})"
        status['htf_summary'] = (
            f"{cfg.get('htf_timeframe')} close={htf_curr_close:.4f}, "
            f"EMA{ema_fast_len}={float(htf_ema_fast):.4f}, EMA{ema_slow_len}={float(htf_ema_slow):.4f}, "
            f"gap={htf_gap_pct:.3f}%"
            if htf_ready else (htf_error or "HTF 계산 대기")
        )

        close_series = closed['close'].astype(float)
        ema200 = close_series.ewm(span=ema_slow_len, adjust=False).mean().iloc[-1] if len(close_series) >= ema_slow_len else np.nan
        ema50_series = close_series.ewm(span=ema_fast_len, adjust=False).mean()
        ema50 = ema50_series.iloc[-1]
        ema50_prev = ema50_series.iloc[-2] if len(ema50_series) >= 2 else np.nan
        rsi_series = self._calculate_wilder_rsi_series(close_series, int(cfg['rsi_length']))
        rsi_value = float(rsi_series.iloc[-1]) if self._is_valid_number(rsi_series.iloc[-1]) else np.nan
        adx_value, plus_di, minus_di, adx_reason = self._calculate_utbot_filter_pack_adx_dmi(closed, int(cfg['adx_length']))
        atr_series = self._calculate_wilder_atr_series(closed, int(cfg['atr_length']))
        atr_value = float(atr_series.iloc[-1]) if self._is_valid_number(atr_series.iloc[-1]) else np.nan
        atr_pct = atr_value / max(abs(entry_price), 1e-9) * 100.0 if self._is_valid_number(atr_value) else np.nan
        chop_value, chop_reason = self._calculate_utbot_filter_pack_chop(closed, 14)
        vortex_plus, vortex_minus, vortex_reason = self._calculate_utbreakout_vortex(closed, 14)
        aroon_up, aroon_down, aroon_reason = self._calculate_utbreakout_aroon(closed, 25)
        donchian_len = int(cfg['donchian_length'])
        donchian_prev = previous_donchian(
            closed['high'].astype(float).tolist(),
            closed['low'].astype(float).tolist(),
            donchian_len
        )
        if not donchian_prev.get('ready'):
            don_high_prev = np.nan
            don_low_prev = np.nan
            don_width_pct = np.nan
        else:
            don_high_prev = float(donchian_prev['high'])
            don_low_prev = float(donchian_prev['low'])
            don_width_pct = (don_high_prev - don_low_prev) / max(abs(entry_price), 1e-9) * 100.0
        ema_near_pct = abs(entry_price - float(ema200)) / max(abs(entry_price), 1e-9) * 100.0 if self._is_valid_number(ema200) else np.nan
        entry_metrics = self._calculate_utbreakout_timeframe_metrics(closed, cfg)

        status.update({
            'rsi': rsi_value,
            'adx': adx_value,
            'plus_di': plus_di,
            'minus_di': minus_di,
            'chop': chop_value,
            'vortex_plus': vortex_plus,
            'vortex_minus': vortex_minus,
            'aroon_up': aroon_up,
            'aroon_down': aroon_down,
            'atr': atr_value,
            'atr_pct': atr_pct,
            'ema50': float(ema50),
            'ema50_prev': float(ema50_prev) if self._is_valid_number(ema50_prev) else None,
            'ema200': float(ema200),
            'ema_near_pct': ema_near_pct,
            'donchian_high_prev': don_high_prev,
            'donchian_low_prev': don_low_prev,
            'donchian_width_pct': don_width_pct,
            'bb_width_percentile': entry_metrics.get('bb_width_percentile'),
            'keltner_squeeze_on': entry_metrics.get('keltner_squeeze_on'),
            'squeeze_release_state': entry_metrics.get('squeeze_release_state'),
            'htf_ready': htf_ready,
            'htf_close': htf_curr_close,
            'htf_ema_fast': htf_ema_fast,
            'htf_ema_slow': htf_ema_slow,
            'htf_gap_pct': htf_gap_pct,
            'htf_supertrend_direction': htf_supertrend_direction,
            'htf_supertrend_reason': htf_supertrend_reason,
            'metric_summary': (
                f"RSI={rsi_value:.2f}, ADX={float(adx_value or 0):.2f}, ATR%={atr_pct:.3f}, "
                f"EMA200 dist={ema_near_pct:.3f}%, Donchian width={don_width_pct:.3f}%"
            )
        })
        futures_context = {}
        try:
            futures_context = (
                (auto_analysis or {}).get('futures_context')
                if isinstance(auto_analysis, dict) and isinstance((auto_analysis or {}).get('futures_context'), dict)
                else None
            )
            if futures_context is None:
                futures_context = await self._fetch_utbreakout_futures_context(symbol)
            if futures_context:
                status.update(futures_context)
        except Exception as e:
            status['futures_context_error'] = str(e)

        l2_gate = await self._evaluate_shared_l2_gate(symbol, cfg, force_refresh=force_reprocess)
        status['l2_gate'] = l2_gate
        status['l2_state'] = l2_gate.get('state')
        status['l2_risk_multiplier'] = l2_gate.get('risk_multiplier')
        if not l2_gate.get('allowed', False):
            return _finish(
                None,
                f"REJECTED_L2_STRESSED: {l2_gate.get('reason')}",
                'REJECTED_L2_STRESSED',
                record_failure=True,
                side=side,
            )
        qh_confirmation = await self._qh_flow_confirmation(
            symbol,
            side,
            cfg,
            force_reprocess=force_reprocess,
        )
        status['qh_confirmation'] = qh_confirmation
        status['qh_confirmation_state'] = qh_confirmation.get('state')
        status['qh_confirmation_risk_multiplier'] = qh_confirmation.get('risk_multiplier')
        if not qh_confirmation.get('allowed', True):
            return _finish(
                None,
                f"{qh_confirmation.get('reject_code') or 'REJECTED_QH_CONFIRMATION'}: {qh_confirmation.get('reason')}",
                qh_confirmation.get('reject_code') or 'REJECTED_QH_CONFIRMATION',
                record_failure=False,
                side=side,
            )
        market_regime_context = {}
        try:
            market_regime_context = await self._fetch_utbreakout_market_regime_context(cfg)
            if isinstance(market_regime_context, dict) and market_regime_context:
                status['market_regime_context'] = market_regime_context
                status['market_regime_summary'] = market_regime_context.get('summary')
        except Exception as e:
            status['market_regime_error'] = str(e)

        filter_values = {
            'entry_price': entry_price,
            'entry_timeframe': cfg.get('entry_timeframe', '15m'),
            'open': entry_metrics.get('open'),
            'rsi': rsi_value,
            'macd_hist': entry_metrics.get('macd_hist'),
            'macd_hist_prev': entry_metrics.get('macd_hist_prev'),
            'roc_pct': entry_metrics.get('roc_pct'),
            'cci': entry_metrics.get('cci'),
            'stoch_k': entry_metrics.get('stoch_k'),
            'stoch_d': entry_metrics.get('stoch_d'),
            'adx': adx_value,
            'plus_di': plus_di,
            'minus_di': minus_di,
            'adx_reason': adx_reason,
            'atr_pct': atr_pct,
            'ema50': ema50,
            'ema50_prev': ema50_prev,
            'ema200': ema200,
            'ema_near_pct': ema_near_pct,
            'donchian_high_prev': don_high_prev,
            'donchian_low_prev': don_low_prev,
            'donchian_width_pct': don_width_pct,
            'bb_upper': entry_metrics.get('bb_upper'),
            'bb_lower': entry_metrics.get('bb_lower'),
            'bb_mid': entry_metrics.get('bb_mid'),
            'bb_width_pct': entry_metrics.get('bb_width_pct'),
            'bb_width_prev_pct': entry_metrics.get('bb_width_prev_pct'),
            'bb_width_min_pct': entry_metrics.get('bb_width_min_pct'),
            'bb_width_percentile': entry_metrics.get('bb_width_percentile'),
            'keltner_upper': entry_metrics.get('keltner_upper'),
            'keltner_lower': entry_metrics.get('keltner_lower'),
            'keltner_mid': entry_metrics.get('keltner_mid'),
            'keltner_width_pct': entry_metrics.get('keltner_width_pct'),
            'keltner_width_prev_pct': entry_metrics.get('keltner_width_prev_pct'),
            'keltner_squeeze_on': entry_metrics.get('keltner_squeeze_on'),
            'range_expansion_ratio': entry_metrics.get('range_expansion_ratio'),
            'range_compression_ratio': entry_metrics.get('range_compression_ratio'),
            'squeeze_release_state': entry_metrics.get('squeeze_release_state'),
            'vwap': entry_metrics.get('vwap'),
            'vwap_slope': entry_metrics.get('vwap_slope'),
            'vwap_reason': entry_metrics.get('vwap_reason'),
            'volume_ratio': entry_metrics.get('volume_ratio'),
            'obv_slope_ratio': entry_metrics.get('obv_slope_ratio'),
            'mfi': entry_metrics.get('mfi'),
            'psar_direction': entry_metrics.get('psar_direction'),
            'psar': entry_metrics.get('psar'),
            'psar_reason': entry_metrics.get('psar_reason'),
            'ichimoku_bias': entry_metrics.get('ichimoku_bias'),
            'ichimoku_top': entry_metrics.get('ichimoku_top'),
            'ichimoku_bottom': entry_metrics.get('ichimoku_bottom'),
            'session_hour_kst': entry_metrics.get('session_hour_kst'),
            'auto_scores': (auto_analysis or {}).get('scores') if isinstance(auto_analysis, dict) else {},
            'mtf_metrics': (auto_analysis or {}).get('timeframes') if isinstance(auto_analysis, dict) else {},
            'chop': chop_value,
            'chop_reason': chop_reason,
            'vortex_plus': vortex_plus,
            'vortex_minus': vortex_minus,
            'vortex_reason': vortex_reason,
            'aroon_up': aroon_up,
            'aroon_down': aroon_down,
            'aroon_reason': aroon_reason,
            'htf_ready': htf_ready,
            'htf_error': htf_error,
            'htf_close': htf_curr_close,
            'htf_ema_fast': htf_ema_fast,
            'htf_ema_slow': htf_ema_slow,
            'htf_gap_pct': htf_gap_pct,
            'htf_supertrend_direction': htf_supertrend_direction,
            'htf_supertrend_reason': htf_supertrend_reason,
            'funding_rate': status.get('funding_rate'),
            'next_funding_time': status.get('next_funding_time'),
            'open_interest_delta_pct': status.get('open_interest_delta_pct'),
            'open_interest_delta_z': status.get('open_interest_delta_z'),
            'open_interest_acceleration': status.get('open_interest_acceleration'),
            'open_interest_hist_samples': status.get('open_interest_hist_samples'),
            'taker_buy_sell_ratio': status.get('taker_buy_sell_ratio'),
            'long_short_ratio': status.get('long_short_ratio'),
            'orderbook_imbalance_pct': status.get('orderbook_imbalance_pct'),
            'rolling_orderbook_imbalance_pct': status.get('rolling_orderbook_imbalance_pct'),
            'rolling_orderbook_imbalance_delta': status.get('rolling_orderbook_imbalance_delta'),
            'rolling_ofi_score': status.get('rolling_ofi_score'),
            'rolling_ofi_samples': status.get('rolling_ofi_samples'),
            'futures_spread_pct': status.get('futures_spread_pct'),
            'bid_depth_usdt': status.get('bid_depth_usdt'),
            'ask_depth_usdt': status.get('ask_depth_usdt'),
            'basis_pct': status.get('basis_pct'),
            'prediction_up_probability': status.get('prediction_up_probability'),
            'prediction_edge': status.get('prediction_edge'),
            'prediction_score': status.get('prediction_score'),
            'prediction_title': status.get('prediction_title'),
            'market_regime_context': market_regime_context,
        }
        try:
            structure_lookback = max(
                3,
                int(cfg.get('structure_stop_lookback_bars', cfg.get('runner_structure_lookback', 5)) or 5),
            )
            recent_structure = closed.tail(structure_lookback)
            filter_values['high'] = float(closed.iloc[-1]['high'])
            filter_values['low'] = float(closed.iloc[-1]['low'])
            filter_values['close'] = float(closed.iloc[-1]['close'])
            filter_values['recent_swing_low'] = float(recent_structure['low'].astype(float).min())
            filter_values['recent_swing_high'] = float(recent_structure['high'].astype(float).max())
        except Exception:
            logger.debug("UTBreakout structure values unavailable for %s", symbol, exc_info=True)
        filter_values = self._enrich_utbreakout_trend_health_values(filter_values, closed, cfg, atr_series)
        filter_values = self._enrich_utbreakout_strategy_quality_values(filter_values, closed, cfg)
        try:
            signal_ts = float(ut_detail.get('signal_ts') or 0.0)
            timeframe_ms = float(
                self._timeframe_to_ms(str(cfg.get('entry_timeframe', '15m') or '15m'))
                or (15 * 60 * 1000)
            )
            if signal_ts > 0 and decision_ts >= signal_ts:
                filter_values['signal_age_candles'] = (
                    float(decision_ts) - signal_ts
                ) / max(timeframe_ms, 1.0)
            elif candidate_type == 'fresh_signal':
                filter_values['signal_age_candles'] = 0.0
        except (TypeError, ValueError):
            if candidate_type == 'fresh_signal':
                filter_values['signal_age_candles'] = 0.0
        feature_score = self._calculate_utbreakout_feature_score(side, cfg, filter_values)
        filter_values['feature_score'] = feature_score
        status['feature_score'] = feature_score
        status['feature_score_value'] = feature_score.get('score')
        status['feature_score_components'] = feature_score.get('components')
        status['feature_score_reason'] = feature_score.get('reason')
        bias_continuation_multiplier = 1.0
        if candidate_type == 'bias_state' and not bool(cfg.get('ev_adaptive_enabled', False)):
            bias_continuation = self._evaluate_utbreakout_bias_continuation(
                side,
                cfg,
                status,
                filter_values,
                selected_set,
            )
            bias_continuation_multiplier = min(
                1.0,
                max(0.0, float(bias_continuation.get('risk_multiplier', 1.0) or 0.0))
            )
            status['bias_continuation'] = bias_continuation
            status['bias_continuation_summary'] = bias_continuation.get('summary')
            status['bias_continuation_risk_multiplier'] = bias_continuation_multiplier
            status['bias_continuation_signal_age_candles'] = bias_continuation.get('signal_age_candles')
            status['bias_continuation_extension_atr'] = bias_continuation.get('extension_atr')
            status['bias_continuation_selected_tf_score'] = bias_continuation.get('selected_tf_score')
            if bias_continuation.get('state') is False:
                status['bias_continuation_blocked'] = True
                status['bias_continuation_block_reason'] = bias_continuation.get('summary')
                if not bool(cfg.get('trend_continuation_entry_enabled', True)):
                    return _finish(
                        None,
                        f"REJECTED_BIAS_CONTINUATION: {bias_continuation.get('summary')}",
                        'REJECTED_BIAS_CONTINUATION',
                        record_failure=True,
                        side=side
                    )

                # Do not reject immediately. Trend continuation entry will decide later.
                # Apply reduced size if continuation is finally accepted.
                bias_continuation_multiplier = min(bias_continuation_multiplier, 0.50)
                status['bias_continuation_risk_multiplier'] = bias_continuation_multiplier
            if bias_continuation.get('enabled', True):
                candidate_type = 'bias_continuation'
                status['candidate_type'] = candidate_type
        filter_items = self._evaluate_utbreakout_set_filter_items(side, selected_set, cfg, filter_values)
        status['set_filter_items'] = filter_items

        hard_filter_names = {
            'ATR% 변동성',
            '손익비',
            '스프레드',
            '유동성',
        }

        failed_filter_items = [item for item in filter_items if item.get('state') is not True]
        hard_filter_failures = []
        soft_filter_failures = []
        set_filter_item_multipliers = []
        orderflow_filter_names = {
            '상대 거래량',
            'Rolling OFI 확인',
            'Futures 수급 불균형',
            'Spread/Depth 비용',
        }

        for item in failed_filter_items:
            name = item.get('name') or ''
            detail = item.get('detail') or ''
            code = item.get('code') or 'SET_FILTER_SOFT_FAIL'
            is_core_set_failure = (
                bool(cfg.get('selected_set_core_filter_hard_block_enabled', True))
                and (
                    name in UTBREAKOUT_CORE_SET_FILTER_HARD_NAMES
                    or code in UTBREAKOUT_CORE_SET_FILTER_HARD_CODES
                    or bool(item.get('core_set_hard_block'))
                )
            )
            if is_core_set_failure or name in hard_filter_names or code == 'REJECTED_RISK_REWARD_LOW':
                classified_state = False
                classified_multiplier = 0.0
                classified_detail = detail
            elif name in orderflow_filter_names:
                classified_state, classified_multiplier, classified_detail = _classify_set_filter_result(
                    side,
                    False,
                    detail,
                    metrics=filter_values,
                    cfg=cfg,
                )
            else:
                classified_state = 'reduced'
                classified_multiplier = (
                    0.65 if item.get('state') is None else 0.50
                )
                classified_detail = f"{detail}; set confirmation soft fail"

            classified = dict(item)
            classified.update({
                'state': classified_state,
                'detail': classified_detail,
                'risk_multiplier': classified_multiplier,
            })
            if classified_state is False:
                hard_filter_failures.append(classified)
            else:
                soft_filter_failures.append(classified)
                set_filter_item_multipliers.append(float(classified_multiplier))

        status['set_filter_hard_failures'] = hard_filter_failures
        status['set_filter_soft_failures'] = soft_filter_failures
        status['set_filter_soft_fail_count'] = len(soft_filter_failures)
        status['set_filter_state'] = (
            False if hard_filter_failures else
            'reduced' if soft_filter_failures else
            True
        )

        if hard_filter_failures:
            item = hard_filter_failures[0]
            code = item.get('code') or 'REJECTED_SET_FILTER_HARD'
            return _finish(
                None,
                f"{code}: Set{selected_set.get('id')} {item.get('name')} - {item.get('detail')}",
                code,
                record_failure=True,
                side=side
            )

        set_filter_multiplier = 1.0
        if soft_filter_failures and bool(cfg.get('set_filter_soft_fail_enabled', True)):
            configured_multiplier = (
                float(cfg.get('set_filter_multi_soft_fail_multiplier', 0.50) or 0.50)
                if len(soft_filter_failures) >= 2
                else float(cfg.get('set_filter_soft_fail_multiplier', 0.70) or 0.70)
            )
            set_filter_multiplier = min(
                [configured_multiplier, *set_filter_item_multipliers]
            )
            status['set_filter_risk_multiplier'] = set_filter_multiplier
            status['set_filter_soft_fail_summary'] = '; '.join(
                f"{item.get('name')}: {item.get('detail')}"
                for item in soft_filter_failures[:4]
            )
        elif soft_filter_failures:
            item = soft_filter_failures[0]
            code = item.get('code') or 'REJECTED_SET_FILTER_SOFT'
            return _finish(
                None,
                f"{code}: Set{selected_set.get('id')} {item.get('name')} - {item.get('detail')}",
                code,
                record_failure=True,
                side=side
            )
        else:
            status['set_filter_risk_multiplier'] = 1.0

        if side == 'short' and not bool(cfg.get('ev_adaptive_enabled', False)):
            short_ok, short_reason = self._utbreakout_short_guard_passes(cfg, filter_values)
            status['short_guard_enabled'] = bool(cfg.get('short_conservative_enabled', True))
            status['short_guard_summary'] = short_reason
            if not short_ok:
                return _finish(
                    None,
                    f"REJECTED_SHORT_GUARD: {short_reason}",
                    'REJECTED_SHORT_GUARD',
                    record_failure=True,
                    side=side
                )

        market_quality = self._evaluate_utbreakout_market_quality(side, cfg, filter_values)
        market_quality_multiplier = min(1.0, max(0.0, float(market_quality.get('risk_multiplier', 1.0) or 0.0)))
        status['market_quality'] = market_quality
        status['market_quality_summary'] = market_quality.get('summary')
        status['market_quality_risk_multiplier'] = market_quality_multiplier
        if market_quality.get('hard_block') or market_quality.get('state') is False:
            return _finish(
                None,
                f"REJECTED_MARKET_QUALITY: {market_quality.get('summary')}",
                'REJECTED_MARKET_QUALITY',
                record_failure=True,
                side=side
            )

        selector_quality = self._build_utbreakout_selector_quality(symbol)
        selector_quality_multiplier = min(1.0, max(0.0, float(selector_quality.get('risk_multiplier', 1.0) or 1.0)))
        selector_candidate = (
            selector_quality.get('candidate')
            if isinstance(selector_quality.get('candidate'), dict)
            else {}
        )
        for source_key, target_key in (
            ('rolling_sharpe', 'selector_rolling_sharpe'),
            ('return_lookback_pct', 'selector_return_lookback_pct'),
            ('momentum_consistency', 'selector_momentum_consistency'),
            ('directional_efficiency', 'selector_directional_efficiency'),
            ('cross_sectional_rank_pct', 'cross_sectional_rank_pct'),
        ):
            if selector_candidate.get(source_key) is not None:
                filter_values[target_key] = selector_candidate.get(source_key)
        status['selector_quality'] = selector_quality
        status['selector_quality_score'] = selector_quality.get('score')
        status['selector_quality_summary'] = selector_quality.get('summary')
        status['selector_quality_risk_multiplier'] = selector_quality_multiplier

        feature_score_multiplier = 1.0
        selected_set_id = int(selected_set.get('id') or 0) if isinstance(selected_set, dict) else 0
        if selected_set_id in {61, 62, 63} and bool(cfg.get('feature_score_enabled', True)):
            score_value = _safe_float_or_none((feature_score or {}).get('score'))
            reduce_below = float(cfg.get('feature_score_reduce_below', 65.0) or 65.0)
            min_multiplier = min(1.0, max(0.0, float(cfg.get('feature_score_min_risk_multiplier', 0.60) or 0.60)))
            if score_value is not None and score_value < reduce_below:
                feature_score_multiplier = max(min_multiplier, min(1.0, score_value / max(reduce_below, 1e-9)))
            status['feature_score_risk_multiplier'] = feature_score_multiplier

        strategy_adaptation = self._build_utbreakout_strategy_adaptation(symbol, side, cfg, selected_set, filter_values)
        strategy_risk_multiplier = min(1.0, max(0.0, float(strategy_adaptation.get('risk_multiplier', 1.0) or 0.0)))
        exit_overlay = strategy_adaptation.get('exit_overlay') if isinstance(strategy_adaptation.get('exit_overlay'), dict) else {}
        fixed_take_profit = bool(cfg.get('fixed_take_profit_enabled', True))
        if exit_overlay and not fixed_take_profit:
            cfg = dict(cfg)
            for key in (
                'partial_take_profit_r_multiple',
                'partial_take_profit_ratio',
                'atr_trailing_multiplier',
                'atr_trailing_activation_r',
            ):
                if key in exit_overlay:
                    cfg[key] = exit_overlay[key]
        status['strategy_adaptation'] = strategy_adaptation
        status['strategy_adaptation_summary'] = strategy_adaptation.get('summary')
        status['strategy_adaptive_risk_multiplier'] = strategy_risk_multiplier
        status['volatility_risk_multiplier'] = strategy_adaptation.get('volatility_risk_multiplier')
        status['meta_label_risk_multiplier'] = strategy_adaptation.get('meta_label_risk_multiplier')
        status['trend_health_risk_multiplier'] = strategy_adaptation.get('trend_health_risk_multiplier')
        trend_health = strategy_adaptation.get('trend_health') if isinstance(strategy_adaptation.get('trend_health'), dict) else {}
        status['trend_health_score'] = trend_health.get('score')
        status['trend_health_state'] = trend_health.get('state')
        status['trend_health_summary'] = trend_health.get('summary')
        strategy_quality = strategy_adaptation.get('strategy_quality') if isinstance(strategy_adaptation.get('strategy_quality'), dict) else {}
        status['strategy_quality_score'] = strategy_quality.get('score')
        status['strategy_quality_state'] = strategy_quality.get('state')
        status['strategy_quality_summary'] = strategy_quality.get('summary')
        status['strategy_quality_risk_multiplier'] = strategy_adaptation.get('strategy_quality_risk_multiplier')
        quality_score_v2 = self._build_utbreakout_quality_score_v2(
            side,
            cfg,
            status,
            filter_values,
            trend_health=trend_health,
            strategy_quality=strategy_quality,
            market_quality=market_quality,
            selector_quality=selector_quality,
        )
        quality_score_v2_multiplier = min(1.0, max(0.0, float(quality_score_v2.get('risk_multiplier', 1.0) or 0.0)))
        status['quality_score_v2'] = quality_score_v2
        status['quality_score_v2_score'] = quality_score_v2.get('score')
        status['quality_score_v2_state'] = quality_score_v2.get('state')
        status['quality_score_v2_summary'] = quality_score_v2.get('summary')
        status['quality_score_v2_risk_multiplier'] = quality_score_v2_multiplier
        ev_decision = None
        if bool(cfg.get('ev_adaptive_enabled', False)):
            filter_values.update({
                'trend_health_score': trend_health.get('score'),
                'strategy_quality_score': strategy_quality.get('score'),
                'quality_score_v2_score': quality_score_v2.get('score'),
                'coin_selector_score': selector_quality.get('score'),
            })
            ev_decision = evaluate_ev_adaptive_entry(
                side=side,
                candidate_type=candidate_type,
                values=filter_values,
                config=_ev_adaptive_runtime_config(cfg),
            )
            status['ev_adaptive'] = {
                'allowed': ev_decision.allowed,
                'mode': ev_decision.mode,
                'score': ev_decision.score,
                'win_probability': ev_decision.win_probability,
                'gross_win_r': ev_decision.gross_win_r,
                'expected_r_before_cost': ev_decision.expected_r_before_cost,
                'risk_multiplier': ev_decision.risk_multiplier,
                'signal_age_candles': ev_decision.signal_age_candles,
                'mtf_alignment': ev_decision.mtf_alignment,
                'momentum_alignment': ev_decision.momentum_alignment,
                'leadership_score': ev_decision.leadership_score,
                'reacceleration': ev_decision.reacceleration,
                'extension_atr': ev_decision.extension_atr,
                'reasons': list(ev_decision.reasons),
                'blockers': list(ev_decision.blockers),
            }
            status['ev_adaptive_mode'] = ev_decision.mode
            status['ev_adaptive_score'] = ev_decision.score
            status['ev_adaptive_summary'] = (
                f"{ev_decision.mode} score {ev_decision.score:.1f} "
                f"p={ev_decision.win_probability:.2f} "
                f"gross={ev_decision.gross_win_r:.2f}R "
                f"MTF={ev_decision.mtf_alignment} "
                f"leader={ev_decision.leadership_score:.0f}"
            )
            if ev_decision.allowed:
                cfg = _apply_ev_exit_profile(cfg, ev_decision.exit_profile)

        profit_alpha_decision = None
        if bool(cfg.get('profit_alpha_enabled', True)):
            filter_values.update({
                'trend_health_score': trend_health.get('score'),
                'strategy_quality_score': strategy_quality.get('score'),
                'quality_score_v2_score': quality_score_v2.get('score'),
                'coin_selector_score': selector_quality.get('score'),
                'market_regime_context': market_regime_context,
                'ev_adaptive_mode': ev_decision.mode if ev_decision is not None else None,
                'ev_win_probability': (
                    ev_decision.win_probability if ev_decision is not None else None
                ),
                'ev_leadership_score': (
                    ev_decision.leadership_score if ev_decision is not None else None
                ),
                'ev_reacceleration': (
                    ev_decision.reacceleration if ev_decision is not None else False
                ),
            })
            profit_alpha_decision = self._evaluate_utbreakout_profit_alpha(
                side=side,
                cfg=cfg,
                values=filter_values,
                ev_decision=ev_decision,
                ev_net=None,
            )
            status['profit_alpha'] = self._profit_alpha_status_payload(
                profit_alpha_decision
            )
            status['profit_alpha_summary'] = profit_alpha_decision.summary
            status['profit_alpha_engine'] = profit_alpha_decision.engine
            status['profit_alpha_score'] = profit_alpha_decision.score
            status['profit_alpha_probability'] = profit_alpha_decision.probability
            status['profit_alpha_risk_multiplier'] = profit_alpha_decision.risk_multiplier
            if profit_alpha_decision.allowed:
                cfg = apply_profit_alpha_exit_overrides(cfg, profit_alpha_decision)

        entry_edge_decision = build_entry_edge_decision(
            side=side,
            ev_decision=ev_decision,
            alpha_decision=profit_alpha_decision,
            config=cfg,
        )
        status['entry_edge'] = self._entry_edge_status_payload(entry_edge_decision)
        status['entry_edge_summary'] = entry_edge_decision.summary
        status['entry_edge_engine'] = entry_edge_decision.engine
        status['entry_edge_score'] = entry_edge_decision.score
        status['entry_edge_probability'] = entry_edge_decision.probability
        status['entry_edge_risk_multiplier'] = entry_edge_decision.risk_multiplier
        status['entry_edge_entry_type'] = entry_edge_decision.entry_type
        status['entry_edge_direction_score'] = entry_edge_decision.direction_score
        status['entry_edge_exit_policy'] = entry_edge_decision.exit_policy
        if not entry_edge_decision.allowed:
            return _finish(
                None,
                "REJECTED_ENTRY_EDGE: " + "; ".join(entry_edge_decision.blockers[:6]),
                'REJECTED_ENTRY_EDGE',
                record_failure=True,
                side=side,
            )

        dynamic_tp2 = self._build_utbreakout_dynamic_tp2(
            side,
            cfg,
            quality_score_v2,
            trend_health=trend_health,
            strategy_quality=strategy_quality,
        )
        if (
            not bool(cfg.get('ev_adaptive_enabled', False))
            and dynamic_tp2.get('enabled')
            and bool(cfg.get('fixed_take_profit_enabled', True))
        ):
            cfg = dict(cfg)
            cfg['second_take_profit_r_multiple'] = max(
                float(cfg.get('second_take_profit_r_multiple', 3.50) or 3.50),
                float(dynamic_tp2.get('second_take_profit_r_multiple', 3.20) or 3.20),
            )
            cfg['take_profit_r_multiple'] = max(
                float(cfg.get('take_profit_r_multiple', 3.50) or 3.50),
                float(cfg.get('second_take_profit_r_multiple', 3.50) or 3.50),
            )
        status['dynamic_take_profit'] = dynamic_tp2
        status['dynamic_take_profit_summary'] = dynamic_tp2.get('summary')
        status['dynamic_tp2_r_multiple'] = dynamic_tp2.get('second_take_profit_r_multiple')
        cfg = apply_profit_opportunity_effective_overrides(cfg)
        if ev_decision is not None:
            cfg = _apply_ev_exit_profile(cfg, ev_decision.exit_profile)
        if profit_alpha_decision is not None and profit_alpha_decision.allowed:
            cfg = apply_profit_alpha_exit_overrides(cfg, profit_alpha_decision)
        elif dynamic_tp2.get('enabled'):
            cfg['second_take_profit_r_multiple'] = max(
                float(cfg.get('second_take_profit_r_multiple', 3.50) or 3.50),
                float(dynamic_tp2.get('second_take_profit_r_multiple', 3.20) or 3.20),
            )
            cfg['take_profit_r_multiple'] = max(
                float(cfg.get('take_profit_r_multiple', 3.50) or 3.50),
                float(cfg.get('second_take_profit_r_multiple', 3.50) or 3.50),
            )

        # The legacy direction/continuation stack remains available for research
        # profiles. EV Adaptive already incorporates those inputs once.
        dir_decision = None
        try:
            if bool(cfg.get('ev_adaptive_enabled', False)):
                raise RuntimeError("EV_ADAPTIVE_SKIP_LEGACY_DIRECTION")
            from utbreakout.direction_filter import decide_direction

            async def _fetch_direction_ohlcv_with_fallback(candidates, timeframe, limit=60):
                last_error = None
                for candidate_symbol in candidates:
                    try:
                        raw = await self.fetch_ohlcv_async(candidate_symbol, timeframe, limit=limit)
                        if raw is None:
                            continue
                        if hasattr(raw, "empty") and raw.empty:
                            continue
                        if hasattr(raw, "__len__") and len(raw) == 0:
                            continue
                        return raw, candidate_symbol
                    except Exception as exc:
                        last_error = exc
                        continue
                if last_error is not None:
                    logger.warning(
                        "direction filter OHLCV fallback failed for %s %s: %s",
                        candidates,
                        timeframe,
                        last_error,
                    )
                return None, None

            btc_candidates = (
                ["BTC/USDT:USDT", "BTC/USDT"]
                if ":USDT" in str(symbol)
                else ["BTC/USDT", "BTC/USDT:USDT"]
            )

            btc_4h_raw, btc_4h_symbol = await _fetch_direction_ohlcv_with_fallback(btc_candidates, "4h", limit=60)
            btc_1d_raw, btc_1d_symbol = await _fetch_direction_ohlcv_with_fallback(btc_candidates, "1d", limit=60)
            sym_1h_raw, sym_1h_symbol = await _fetch_direction_ohlcv_with_fallback([symbol], "1h", limit=60)

            status["direction_btc_4h_symbol"] = btc_4h_symbol
            status["direction_btc_1d_symbol"] = btc_1d_symbol
            status["direction_symbol_1h_symbol"] = sym_1h_symbol

            btc_4h = self._build_direction_metrics_dict(btc_4h_raw)
            btc_1d = self._build_direction_metrics_dict(btc_1d_raw)
            sym_1h = self._build_direction_metrics_dict(sym_1h_raw)

            dir_decision = decide_direction(
                btc_4h=btc_4h,
                btc_1d=btc_1d,
                symbol_1h=sym_1h,
                entry_15m=filter_values or {},
                side_hint=side,
            )

            allowed_by_direction = (
                dir_decision.long_allowed if str(side).lower() == "long"
                else dir_decision.short_allowed
            )

            status["direction_decision"] = {
                "allowed": allowed_by_direction,
                "regime": dir_decision.regime,
                "size_multiplier": dir_decision.size_multiplier,
                "reason": dir_decision.reason,
            }

            if not allowed_by_direction:
                return _finish(
                    None,
                    f"REJECTED_DIRECTION_FILTER: {dir_decision.reason}",
                    "REJECTED_DIRECTION_FILTER",
                    record_failure=True,
                    side=side,
                )
        except RuntimeError as exc:
            if str(exc) == "EV_ADAPTIVE_SKIP_LEGACY_DIRECTION":
                status["direction_decision"] = {
                    "allowed": True,
                    "regime": ev_decision.mode if ev_decision is not None else "EV_ADAPTIVE",
                    "size_multiplier": ev_decision.risk_multiplier if ev_decision is not None else 1.0,
                    "reason": "EV Adaptive integrated direction gate",
                }
            else:
                raise
        except Exception as exc:
            status["direction_decision_error"] = str(exc)
            logger.exception("decide_direction overlay failed")

        continuation_decision = None
        try:
            if bool(cfg.get('ev_adaptive_enabled', False)):
                raise RuntimeError("EV_ADAPTIVE_SKIP_LEGACY_CONTINUATION")
            continuation_decision = evaluate_trend_continuation_entry(
                side=side,
                candidate_type=candidate_type,
                cfg=cfg,
                values=filter_values,
                status=status,
                direction_decision=dir_decision,
                quality_score_v2=quality_score_v2,
                trend_health=trend_health,
                strategy_quality=strategy_quality,
            )
            status['trend_continuation_entry'] = {
                'enabled': continuation_decision.enabled,
                'accepted': continuation_decision.accepted,
                'side': continuation_decision.side,
                'mode': continuation_decision.mode,
                'risk_multiplier': continuation_decision.risk_multiplier,
                'setup': continuation_decision.setup,
                'reason': continuation_decision.reason,
                'blockers': continuation_decision.blockers,
                'positives': continuation_decision.positives,
            }

            if candidate_type in {'bias_state', 'bias_continuation'}:
                if continuation_decision.accepted:
                    candidate_type = 'trend_continuation'
                    status['candidate_type'] = candidate_type
                    status['trend_continuation_accepted'] = True
                    status['trend_continuation_summary'] = continuation_decision.reason
                else:
                    return _finish(
                        None,
                        f"REJECTED_TREND_CONTINUATION: {continuation_decision.reason}",
                        'REJECTED_TREND_CONTINUATION',
                        record_failure=True,
                        side=side
                    )
        except RuntimeError as exc:
            if str(exc) == "EV_ADAPTIVE_SKIP_LEGACY_CONTINUATION":
                if candidate_type in {'bias_state', 'bias_continuation'}:
                    candidate_type = 'trend_continuation'
                    status['candidate_type'] = candidate_type
                status['trend_continuation_entry'] = {
                    'enabled': False,
                    'accepted': True,
                    'mode': ev_decision.mode if ev_decision is not None else 'EV_ADAPTIVE',
                    'risk_multiplier': ev_decision.risk_multiplier if ev_decision is not None else 1.0,
                    'reason': 'EV Adaptive integrated continuation gate',
                }
            else:
                raise
        except Exception as exc:
            logger.error("trend continuation evaluation failed: %s", exc)
            status['trend_continuation_error'] = str(exc)
            if candidate_type in {'bias_state', 'bias_continuation'}:
                return _finish(
                    None,
                    f"REJECTED_TREND_CONTINUATION_ERROR: {exc}",
                    'REJECTED_TREND_CONTINUATION_ERROR',
                    record_failure=True,
                    side=side
                )

        if ev_decision is not None:
            status['adaptive_exit_summary'] = (
                f"EV {ev_decision.exit_profile.name}: "
                f"TP1 {ev_decision.exit_profile.tp1_r:.2f}R"
                f"({ev_decision.exit_profile.tp1_ratio:.0%}) / "
                f"TP2 {ev_decision.exit_profile.tp2_r:.2f}R"
                f"({ev_decision.exit_profile.tp2_ratio:.0%}) / "
                f"runner {ev_decision.exit_profile.runner_ratio:.0%}, "
                f"trail {ev_decision.exit_profile.trailing_atr_multiplier:.2f}ATR"
            )
        elif fixed_take_profit:
            status['adaptive_exit_summary'] = (
                f"fixed TP ladder "
                f"{float(cfg.get('partial_take_profit_ratio', 0.20) or 0.20):.0%}@"
                f"{float(cfg.get('partial_take_profit_r_multiple', 1.00) or 1.00):.1f}R + "
                f"{float(cfg.get('second_take_profit_ratio', 0.40) or 0.40):.0%}@"
                f"{float(cfg.get('second_take_profit_r_multiple', 3.50) or 3.50):.1f}R; "
                f"dynamic={dynamic_tp2.get('summary')}; "
                f"runner/chandelier policy active if enabled; "
                f"TP1 BE {'ON' if cfg.get('tp1_breakeven_enabled', True) else 'OFF'}"
            )
        else:
            status['adaptive_exit_summary'] = exit_overlay.get('summary')

        if entry_edge_decision is not None and entry_edge_decision.allowed:
            status['adaptive_exit_summary'] = (
                f"Entry Edge {entry_edge_decision.engine}/{entry_edge_decision.entry_type}: "
                f"TP1 {float(cfg.get('partial_take_profit_r_multiple', 1.0) or 1.0):.2f}R"
                f"({float(cfg.get('partial_take_profit_ratio', 0.0) or 0.0):.0%}) / "
                f"TP2 {float(cfg.get('second_take_profit_r_multiple', cfg.get('take_profit_r_multiple', 2.4)) or 2.4):.2f}R"
                f"({float(cfg.get('second_take_profit_ratio', 0.0) or 0.0):.0%}) / "
                f"runner {float(cfg.get('runner_pct', 0.0) or 0.0):.0%}; "
                f"score {entry_edge_decision.score:.1f} "
                f"dir {entry_edge_decision.direction_score:.1f} "
                f"p={entry_edge_decision.probability:.3f}"
            )

        shadow_stats = strategy_adaptation.get('shadow_stats') if isinstance(strategy_adaptation.get('shadow_stats'), dict) else {}
        runner_stats = strategy_adaptation.get('runner_stats') if isinstance(strategy_adaptation.get('runner_stats'), dict) else {}
        status['shadow_sample_count'] = shadow_stats.get('sample_count')
        status['shadow_win_rate'] = shadow_stats.get('tp_rate')
        status['shadow_avg_pnl_r'] = shadow_stats.get('avg_pnl_r')
        status['runner_sample_count'] = runner_stats.get('sample_count')
        status['runner_avg_mfe_capture_ratio'] = runner_stats.get('avg_mfe_capture_ratio')
        status['runner_avg_pnl_r'] = runner_stats.get('avg_pnl_r')

        short_risk_multiplier = 1.0
        if side == 'short' and bool(cfg.get('short_conservative_enabled', True)):
            short_risk_multiplier = min(
                1.0,
                max(0.0, float(cfg.get('short_risk_multiplier', 0.60) or 0.60)),
            )
        direction_multiplier = (
            min(1.0, max(0.0, float(dir_decision.size_multiplier)))
            if dir_decision is not None
            else 1.0
        )
        continuation_multiplier = (
            min(1.0, max(0.0, float(continuation_decision.risk_multiplier)))
            if continuation_decision is not None
            else 1.0
        )
        l2_multiplier = max(0.0, min(1.0, float(l2_gate.get('risk_multiplier', 1.0) or 0.0)))
        qh_confirmation_multiplier = max(
            0.0,
            min(1.0, float(qh_confirmation.get('risk_multiplier', 1.0) or 0.0)),
        )
        if ev_decision is not None:
            volatility_multiplier = min(
                1.0,
                max(
                    0.0,
                    float(strategy_adaptation.get('volatility_risk_multiplier', 1.0) or 0.0),
                ),
            )
            raw_final_risk_multiplier = min(
                float(entry_edge_decision.risk_multiplier),
                market_quality_multiplier,
                volatility_multiplier,
                l2_multiplier,
                qh_confirmation_multiplier,
            )
        else:
            raw_final_risk_multiplier = (
                short_risk_multiplier
                * bias_continuation_multiplier
                * market_quality_multiplier
                * l2_multiplier
                * qh_confirmation_multiplier
                * strategy_risk_multiplier
                * quality_score_v2_multiplier
                * selector_quality_multiplier
                * feature_score_multiplier
                * set_filter_multiplier
                * direction_multiplier
                * continuation_multiplier
                * (
                    min(
                        1.0,
                        max(0.0, float(entry_edge_decision.risk_multiplier)),
                    )
                    if entry_edge_decision is not None
                    else 1.0
                )
            )
        raw_final_risk_multiplier_before_position_sizing = max(
            0.0,
            min(1.0, float(raw_final_risk_multiplier)),
        )
        position_sizing = {
            'risk_multiplier': 1.0,
            'blocked': False,
            'components': {},
            'reasons': [],
            'kelly_reason': 'disabled',
        }
        strategy_allocator_cfg = self._strategy_allocator_runtime_config(cfg)
        performance_delegated_to_allocator = bool(
            strategy_allocator_cfg.get('enabled', True)
        )
        status['position_sizing_performance_delegated_to_allocator'] = (
            performance_delegated_to_allocator
        )
        if bool(cfg.get('position_sizing_engine_enabled', True)):
            try:
                portfolio_sizing_context = {
                    'open_positions': 0,
                    'same_direction_positions': 0,
                    'total_open_risk_pct': 0.0,
                }
                try:
                    if hasattr(getattr(self, 'exchange', None), 'fetch_positions'):
                        sizing_positions = await asyncio.to_thread(self.exchange.fetch_positions)
                        portfolio_sizing_context = (
                            self._build_utbreakout_position_sizing_portfolio_context(
                                symbol=symbol,
                                side=side,
                                cfg=cfg,
                                positions=sizing_positions,
                            )
                        )
                except Exception as portfolio_exc:
                    logger.debug(
                        "UTBreakout position sizing portfolio context unavailable for %s: %s",
                        symbol,
                        portfolio_exc,
                    )
                position_sizing = build_position_risk_multiplier(
                    {
                        'atr_pct': atr_pct,
                        # The Entry Edge probability is already applied by the
                        # dedicated edge-probability component below.  Feeding
                        # the same number into the independent meta bucket cut
                        # otherwise-strong trades twice.
                        'meta_probability': 0.65,
                        'entry_edge_probability': (
                            entry_edge_decision.probability
                            if entry_edge_decision is not None
                            else (
                                ev_decision.win_probability
                                if ev_decision is not None
                                else None
                            )
                        ),
                        'entry_edge_score': (
                            entry_edge_decision.score
                            if entry_edge_decision is not None
                            else None
                        ),
                        'direction_score': (
                            entry_edge_decision.direction_score
                            if entry_edge_decision is not None
                            else None
                        ),
                        'recent_avg_pnl_r': (
                            profit_alpha_decision.meta_expectancy_r
                            if (
                                profit_alpha_decision is not None
                                and not performance_delegated_to_allocator
                            )
                            else None
                        ),
                        'meta_sample_count': (
                            profit_alpha_decision.meta_sample_count
                            if (
                                profit_alpha_decision is not None
                                and not performance_delegated_to_allocator
                            )
                            else 0
                        ),
                        # Finalized strategy performance is applied once by the
                        # strategy allocator when the plan is stored. Feeding
                        # the same loss streak into this engine compounded the
                        # same evidence twice.
                        'recent_closed_pnls': (
                            None
                            if performance_delegated_to_allocator
                            else recent_pnls
                        ),
                        'daily_loss_limit_hit': False,
                        'total_open_risk_pct': portfolio_sizing_context.get('total_open_risk_pct'),
                        'same_direction_positions': portfolio_sizing_context.get('same_direction_positions'),
                        'liquidity_ok': market_quality.get('state') is not False,
                        'spread_ok': market_quality.get('hard_block') is not True,
                    },
                    cfg,
                )
                status['position_sizing_portfolio_context'] = portfolio_sizing_context
                position_multiplier = min(
                    1.0,
                    max(0.0, float(position_sizing.get('risk_multiplier', 1.0) or 0.0)),
                )
                raw_final_risk_multiplier = min(
                    raw_final_risk_multiplier_before_position_sizing,
                    position_multiplier,
                )
            except Exception as exc:
                logger.warning("UTBreakout position sizing engine failed for %s: %s", symbol, exc)
                position_sizing = {
                    'risk_multiplier': 1.0,
                    'blocked': False,
                    'components': {},
                    'reasons': [f'position sizing unavailable: {exc}'],
                    'kelly_reason': 'error',
                }
                raw_final_risk_multiplier = raw_final_risk_multiplier_before_position_sizing
        status['position_sizing'] = position_sizing
        status['position_sizing_risk_multiplier'] = position_sizing.get('risk_multiplier')
        status['position_sizing_components'] = position_sizing.get('components')
        status['position_sizing_reasons'] = position_sizing.get('reasons')
        status['raw_final_risk_multiplier_before_position_sizing'] = raw_final_risk_multiplier_before_position_sizing
        if bool(position_sizing.get('blocked')):
            return _finish(
                None,
                "REJECTED_POSITION_SIZING: " + "; ".join(position_sizing.get('reasons') or ['position sizing blocked']),
                'REJECTED_POSITION_SIZING',
                record_failure=True,
                side=side,
            )
        final_risk_multiplier = _apply_utbreakout_risk_multiplier_floor(
            raw_final_risk_multiplier,
            cfg,
        )
        status['raw_final_risk_multiplier'] = max(
            0.0,
            min(1.0, float(raw_final_risk_multiplier)),
        )
        status['final_risk_multiplier'] = final_risk_multiplier
        status['decision_trace'] = {
            'runtime_profile': cfg.get('runtime_profile'),
            'entry_timeframe': cfg.get('entry_timeframe'),
            'htf_timeframe': cfg.get('htf_timeframe'),
            'adaptive_timeframes': cfg.get('adaptive_timeframes'),
            'selected_set': selected_set.get('id') if isinstance(selected_set, dict) else None,
            'selected_set_detail': (
                selected_set.get('_stable_selection_detail')
                if isinstance(selected_set, dict)
                else None
            ),
            'set_filter_state': status.get('set_filter_state'),
            'set_filter_multiplier': set_filter_multiplier,
            'trend_health_state': trend_health.get('state'),
            'strategy_quality_state': strategy_quality.get('state'),
            'quality_score_v2_state': quality_score_v2.get('state'),
            'ev_adaptive_mode': ev_decision.mode if ev_decision is not None else None,
            'ev_adaptive_score': ev_decision.score if ev_decision is not None else None,
            'profit_alpha_engine': (
                profit_alpha_decision.engine if profit_alpha_decision is not None else None
            ),
            'profit_alpha_score': (
                profit_alpha_decision.score if profit_alpha_decision is not None else None
            ),
            'profit_alpha_probability': (
                profit_alpha_decision.probability if profit_alpha_decision is not None else None
            ),
            'entry_edge_engine': entry_edge_decision.engine if entry_edge_decision is not None else None,
            'entry_edge_score': entry_edge_decision.score if entry_edge_decision is not None else None,
            'entry_edge_probability': (
                entry_edge_decision.probability if entry_edge_decision is not None else None
            ),
            'entry_edge_entry_type': (
                entry_edge_decision.entry_type if entry_edge_decision is not None else None
            ),
            'entry_edge_direction_score': (
                entry_edge_decision.direction_score if entry_edge_decision is not None else None
            ),
            'entry_edge_exit_policy': (
                entry_edge_decision.exit_policy if entry_edge_decision is not None else None
            ),
            'position_sizing_risk_multiplier': position_sizing.get('risk_multiplier'),
            'position_sizing_reasons': list(position_sizing.get('reasons') or []),
            'raw_final_risk_multiplier_before_position_sizing': (
                status.get('raw_final_risk_multiplier_before_position_sizing')
            ),
            'raw_final_risk_multiplier': status.get('raw_final_risk_multiplier'),
            'final_risk_multiplier': final_risk_multiplier,
        }

        # Only configured extreme states remain hard blocks.
        trend_score_val = trend_health.get('score')
        trend_hard = float(cfg.get('trend_health_hard_block_below', 20.0) or 20.0)
        if ev_decision is None and (trend_health.get('state') is False or (
            trend_score_val is not None and float(trend_score_val) < trend_hard
        )):
            return _finish(
                None,
                f"REJECTED_TREND_HEALTH: trend health score {float(trend_score_val or 0.0):.2f} < {trend_hard:.1f}",
                'REJECTED_TREND_HEALTH',
                record_failure=True,
                side=side
            )

        strategy_score_val = strategy_quality.get('score')
        strategy_hard = float(cfg.get('strategy_quality_hard_block_below', 12.0) or 12.0)
        if ev_decision is None and (strategy_quality.get('state') is False or (
            strategy_score_val is not None and float(strategy_score_val) < strategy_hard
        )):
            return _finish(
                None,
                f"REJECTED_STRATEGY_QUALITY: strategy quality score {float(strategy_score_val or 0.0):.2f} < {strategy_hard:.1f}",
                'REJECTED_STRATEGY_QUALITY',
                record_failure=True,
                side=side
            )

        q2_score_val = quality_score_v2.get('score')
        q2_hard = float(
            quality_score_v2.get(
                'block_below',
                cfg.get(
                    'quality_score_v2_short_block_below' if side == 'short' else 'quality_score_v2_long_block_below',
                    25.0 if side == 'short' else 20.0,
                ),
            )
            or (25.0 if side == 'short' else 20.0)
        )
        if ev_decision is None and (quality_score_v2.get('state') is False or (
            q2_score_val is not None and float(q2_score_val) < q2_hard
        )):
            return _finish(
                None,
                f"REJECTED_QUALITY_SCORE_V2: quality score v2 {float(q2_score_val or 0.0):.2f} < {q2_hard:.1f}",
                'REJECTED_QUALITY_SCORE_V2',
                record_failure=True,
                side=side
            )

        if not self._is_valid_number(atr_value) or float(atr_value) <= 0:
            return _finish(None, "REJECTED_ATR_TOO_LOW: ATR risk distance calculation pending", 'REJECTED_ATR_TOO_LOW', record_failure=True, side=side)

        total_balance, free_balance, _ = await self.get_balance_info()
        balance_for_risk = total_balance if total_balance > 0 else free_balance
        common_cfg = self.get_runtime_common_settings()
        leverage = int(max(1.0, float(common_cfg.get('leverage', 5) or 5)))
        risk_budget = resolve_utbreakout_risk_budget(
            balance_for_risk,
            cfg,
            daily_pnl_usdt=daily_pnl,
        )
        risk_per_trade_percent = risk_budget['risk_per_trade_percent']
        max_risk_per_trade_usdt = risk_budget['max_risk_per_trade_usdt']
        if side == 'short' and bool(cfg.get('short_conservative_enabled', True)):
            status['short_risk_multiplier'] = short_risk_multiplier
        if set_filter_multiplier < 0.999:
            status['set_filter_risk_multiplier'] = set_filter_multiplier
        if continuation_decision and continuation_decision.risk_multiplier < 0.999:
            status['trend_continuation_risk_multiplier'] = continuation_decision.risk_multiplier

        risk_per_trade_percent *= final_risk_multiplier
        max_risk_per_trade_usdt *= final_risk_multiplier
        structure_stop = (
            filter_values.get('recent_swing_low')
            if side == 'long'
            else filter_values.get('recent_swing_high')
        )
        try:
            plan = calculate_risk_plan(
                side=side,
                entry_price=entry_price,
                atr_value=atr_value,
                stop_atr_multiplier=cfg.get('stop_atr_multiplier', 1.5),
                ut_stop=ut_detail.get('curr_stop'),
                structure_stop=structure_stop,
                structure_buffer_atr=cfg.get('structure_stop_buffer_atr', 0.28),
                take_profit_r_multiple=cfg.get('take_profit_r_multiple', 3.50),
                take_profit_front_run_atr=cfg.get('take_profit_front_run_atr', 0.14),
                take_profit_front_run_pct=cfg.get('take_profit_front_run_pct', 0.055),
                min_risk_reward=cfg.get('min_risk_reward', 2.0),
                balance_usdt=balance_for_risk,
                risk_per_trade_percent=risk_per_trade_percent,
                max_risk_per_trade_usdt=max_risk_per_trade_usdt,
                leverage=leverage,
            )
            plan = cap_utbreakout_risk_plan_to_margin(
                plan,
                free_balance=free_balance,
                leverage=leverage,
                entry_price=entry_price,
            )
        except ValueError as e:
            reason = str(e)
            if 'risk budget unavailable' in reason:
                reject_code = 'REJECTED_RISK_BUDGET_UNAVAILABLE'
                human_reason = "리스크 예산 없음: 최종 risk budget이 0이라 주문 수량 계산 불가"
            else:
                reject_code = 'REJECTED_RISK_REWARD_LOW'
                human_reason = reason
            return _finish(None, f"{reject_code}: {human_reason}", reject_code, record_failure=True, side=side)
        growth_overlay = {
            'enabled': bool(cfg.get('aggressive_growth_enabled', False)),
            'accepted': False,
            'reason': 'disabled',
        }
        if bool(cfg.get('aggressive_growth_enabled', False)) and side == 'long':
            weekly_pnl = 0.0
            try:
                _, weekly_pnl = self.db.get_weekly_stats()
            except Exception:
                weekly_pnl = 0.0
            exposure = {
                'open_positions': int(cfg.get('aggressive_growth_max_open_positions', 2) or 2),
                'total_notional': 0.0,
                'symbol_notional': 0.0,
            }
            try:
                positions = await asyncio.to_thread(self.exchange.fetch_positions)
                exposure = self._calculate_aggressive_growth_exposure(positions, symbol=symbol)
            except Exception as exc:
                logger.warning(f"Aggressive growth overlay position scan failed for {symbol}: {exc}")
            high_watermark = self._update_aggressive_growth_high_watermark(balance_for_risk)

            min_depth = float(cfg.get('prediction_min_depth_usdt', 50000.0) or 50000.0)
            bid_depth = _safe_float_or_none(filter_values.get('bid_depth_usdt'))
            ask_depth = _safe_float_or_none(filter_values.get('ask_depth_usdt'))
            liquidity_ok = True
            if bid_depth is not None and ask_depth is not None:
                liquidity_ok = bid_depth >= min_depth and ask_depth >= min_depth
            spread = _safe_float_or_none(filter_values.get('futures_spread_pct'))
            spread_ok = True if spread is None else spread <= float(cfg.get('rolling_ofi_spread_max_pct', 0.05) or 0.05)
            long_htf_bullish = (
                bool(filter_values.get('htf_ready'))
                and _safe_float_or_none(filter_values.get('htf_close')) is not None
                and _safe_float_or_none(filter_values.get('htf_ema_slow')) is not None
                and float(filter_values.get('htf_close')) > float(filter_values.get('htf_ema_slow'))
            ) or str(filter_values.get('htf_supertrend_direction') or '').lower() == 'long'
            adx_value = _safe_float_or_none(filter_values.get('adx'))
            volume_value = _safe_float_or_none(filter_values.get('volume_ratio'))
            funding_value = _safe_float_or_none(filter_values.get('funding_rate'))
            extreme_atr = float(cfg.get('market_quality_extreme_atr_pct', 2.5) or 2.5)
            symbol_trend_bullish = is_aggressive_symbol_trend_bullish(trend_health)
            growth_overlay = build_aggressive_growth_overlay_plan(
                plan,
                cfg,
                {
                    'side': side,
                    'entry_price': entry_price,
                    'stop_loss_price': plan.get('stop_loss'),
                    'risk_distance': plan.get('risk_distance'),
                    'account_equity': balance_for_risk,
                    'aggressive_growth_high_watermark': high_watermark,
                    'daily_pnl_usdt': daily_pnl,
                    'weekly_pnl_usdt': weekly_pnl,
                    'open_positions': exposure.get('open_positions', 0),
                    'symbol_exposure_notional': exposure.get('symbol_notional', 0.0),
                    'total_aggressive_exposure_notional': exposure.get('total_notional', 0.0),
                    'leverage': leverage,
                    'liquidity_ok': liquidity_ok,
                    'spread_ok': spread_ok,
                    'htf_trend_bullish': long_htf_bullish,
                    'symbol_trend_bullish': symbol_trend_bullish,
                    'volume_ok': volume_value is not None and volume_value >= float(cfg.get('bias_continuation_min_volume_ratio', 0.50) or 0.50),
                    'adx_ok': adx_value is not None and adx_value >= float(cfg.get('bias_continuation_min_adx', 18.0) or 18.0),
                    'quality_ok': float(quality_score_v2.get('score', 0.0) or 0.0) >= float(cfg.get('quality_score_v2_long_reduce_below', 60.0) or 60.0),
                    'funding_not_overheated': funding_value is None or funding_value <= float(cfg.get('funding_long_max', 0.0008) or 0.0008),
                    'volatility_safe': atr_pct is None or float(atr_pct) <= extreme_atr,
                    'atr_pct': atr_pct,
                    'funding_rate': filter_values.get('funding_rate'),
                    'funding_percentile_7d': filter_values.get('funding_percentile_7d'),
                    'funding_percentile_30d': filter_values.get('funding_percentile_30d'),
                    'open_interest_change_1h': filter_values.get('open_interest_change_1h', filter_values.get('open_interest_delta_pct')),
                    'open_interest_change_4h': filter_values.get('open_interest_change_4h'),
                    'price_change_1h': filter_values.get('price_change_1h', filter_values.get('momentum_6_pct')),
                    'price_change_4h': filter_values.get('price_change_4h', filter_values.get('momentum_12_pct')),
                    'long_short_ratio': filter_values.get('long_short_ratio'),
                    'taker_buy_sell_ratio': filter_values.get('taker_buy_sell_ratio'),
                    'liquidation_imbalance': filter_values.get('liquidation_imbalance'),
                }
            )
            status['aggressive_growth_overlay'] = growth_overlay
            status['aggressive_growth_summary'] = growth_overlay.get('reason')
            if growth_overlay.get('accepted') and isinstance(growth_overlay.get('plan'), dict):
                plan = growth_overlay['plan']
                cfg = dict(cfg)
                for key in (
                    'partial_take_profit_enabled',
                    'partial_take_profit_r_multiple',
                    'partial_take_profit_ratio',
                    'second_take_profit_enabled',
                    'second_take_profit_r_multiple',
                    'second_take_profit_ratio',
                    'runner_pct',
                    'atr_trailing_enabled',
                    'atr_trailing_multiplier',
                    'runner_exit_enabled',
                    'runner_chandelier_enabled',
                    'tp1_breakeven_enabled',
                ):
                    if key in plan:
                        cfg[key] = plan[key]
            elif growth_overlay.get('enabled'):
                logger.info(
                    f"Aggressive growth overlay not applied for {symbol}: {growth_overlay.get('reason')}"
                )
                try:
                    await self.ctrl.notify(
                        f"⚠️ Aggressive Growth 차단: {self.ctrl.format_symbol_for_display(symbol)} "
                        f"{growth_overlay.get('reason')}. 일반 UTBreakout 수량으로 진행합니다."
                    )
                except Exception:
                    logger.debug("Aggressive growth blocked notice skipped", exc_info=True)
        cfg = apply_profit_opportunity_effective_overrides(cfg)
        if ev_decision is not None:
            cfg = _apply_ev_exit_profile(cfg, ev_decision.exit_profile)
        if profit_alpha_decision is not None and profit_alpha_decision.allowed:
            cfg = apply_profit_alpha_exit_overrides(cfg, profit_alpha_decision)
        elif dynamic_tp2.get('enabled'):
            cfg['second_take_profit_r_multiple'] = max(
                float(cfg.get('second_take_profit_r_multiple', 3.50) or 3.50),
                float(dynamic_tp2.get('second_take_profit_r_multiple', 3.20) or 3.20),
            )
            cfg['take_profit_r_multiple'] = max(
                float(cfg.get('take_profit_r_multiple', 3.50) or 3.50),
                float(cfg.get('second_take_profit_r_multiple', 3.50) or 3.50),
            )
        plan.update({
            'strategy': active_strategy if active_strategy in UTBREAKOUT_STRATEGIES else UTBOT_FILTERED_BREAKOUT_STRATEGY,
            'effective_profile_version': cfg.get('effective_profile_version'),
            'entry_timeframe': cfg.get('entry_timeframe', '15m'),
            'exit_timeframe': cfg.get('exit_timeframe', cfg.get('entry_timeframe', '15m')),
            'htf_timeframe': cfg.get('htf_timeframe', '1h'),
            'auto_selected_set_id': selected_set.get('id'),
            'auto_selected_set_name': selected_set.get('name'),
            'adaptive_timeframe_enabled': bool(cfg.get('adaptive_timeframe_enabled', False)),
            'atr': atr_value,
            'atr_pct': atr_pct,
            'decision_candle_ts': decision_ts,
            'fixed_take_profit_enabled': bool(cfg.get('fixed_take_profit_enabled', True)),
            'partial_take_profit_enabled': bool(cfg.get('partial_take_profit_enabled', True)),
            'partial_take_profit_r_multiple': float(cfg.get('partial_take_profit_r_multiple', 1.00) or 1.00),
            'partial_take_profit_ratio': float(cfg.get('partial_take_profit_ratio', 0.20) or 0.20),
            'second_take_profit_enabled': bool(cfg.get('second_take_profit_enabled', True)),
            'second_take_profit_r_multiple': float(cfg.get('second_take_profit_r_multiple', cfg.get('take_profit_r_multiple', 3.50)) or 3.50),
            'second_take_profit_ratio': float(cfg.get('second_take_profit_ratio', 0.40) or 0.40),
            'atr_trailing_enabled': bool(cfg.get('atr_trailing_enabled', True)),
            'atr_trailing_multiplier': float(cfg.get('atr_trailing_multiplier', 3.50) or 3.50),
            'atr_trailing_activation_r': float(cfg.get('atr_trailing_activation_r', 1.60) or 1.60),
            'hard_stop_loss': plan.get('hard_stop_loss', plan.get('stop_loss')),
            'soft_stop_loss': plan.get('soft_stop_loss'),
            'structure_stop': plan.get('structure_stop'),
            'structure_stop_with_buffer': plan.get('structure_stop_with_buffer'),
            'structure_anchor_distance': plan.get('structure_anchor_distance'),
            'structure_stop_buffer_atr': float(cfg.get('structure_stop_buffer_atr', 0.28) or 0.28),
            'take_profit_front_run_atr': float(cfg.get('take_profit_front_run_atr', 0.14) or 0.14),
            'take_profit_front_run_pct': float(cfg.get('take_profit_front_run_pct', 0.055) or 0.055),
            'tp_front_run_min_r_multiple': float(cfg.get('tp_front_run_min_r_multiple', 0.0) or 0.0),
            'soft_stop_enabled': bool(cfg.get('soft_stop_enabled', True)),
            'soft_stop_confirm_bars': int(cfg.get('soft_stop_confirm_bars', 2) or 2),
            'near_miss_tp_enabled': bool(cfg.get('near_miss_tp_enabled', True)),
            'near_miss_tp_arm_ratio': float(cfg.get('near_miss_tp_arm_ratio', 0.86) or 0.86),
            'near_miss_tp_lock_r': float(cfg.get('near_miss_tp_lock_r', 0.28) or 0.28),
            'near_miss_tp_rejection_atr': float(cfg.get('near_miss_tp_rejection_atr', 0.12) or 0.12),
            'profit_alpha_stall_exit_max_current_r': float(cfg.get('profit_alpha_stall_exit_max_current_r', 0.0) or 0.0),
            'ev_time_stop_max_current_r': float(cfg.get('ev_time_stop_max_current_r', 0.0) or 0.0),
            'short_conservative_enabled': bool(cfg.get('short_conservative_enabled', True)),
            'short_risk_multiplier': float(cfg.get('short_risk_multiplier', 0.5) or 0.5),
            'bias_continuation_summary': status.get('bias_continuation_summary'),
            'bias_continuation_risk_multiplier': bias_continuation_multiplier,
            'bias_continuation_signal_age_candles': status.get('bias_continuation_signal_age_candles'),
            'bias_continuation_extension_atr': status.get('bias_continuation_extension_atr'),
            'bias_continuation_selected_tf_score': status.get('bias_continuation_selected_tf_score'),
            'market_quality_enabled': bool(cfg.get('market_quality_enabled', True)),
            'market_quality_risk_multiplier': market_quality_multiplier,
            'market_quality_summary': market_quality.get('summary'),
            'l2_gate': l2_gate,
            'l2_state': l2_gate.get('state'),
            'l2_risk_multiplier': l2_multiplier,
            'qh_confirmation': qh_confirmation,
            'qh_confirmation_state': qh_confirmation.get('state'),
            'qh_confirmation_risk_multiplier': qh_confirmation_multiplier,
            'selector_quality_risk_multiplier': selector_quality_multiplier,
            'selector_quality_score': selector_quality.get('score'),
            'selector_quality_summary': selector_quality.get('summary'),
            'feature_score_value': feature_score.get('score'),
            'feature_score_components': feature_score.get('components'),
            'feature_score_reason': feature_score.get('reason'),
            'feature_score_risk_multiplier': feature_score_multiplier,
            'strategy_adaptive_risk_multiplier': strategy_risk_multiplier,
            'strategy_adaptation_summary': strategy_adaptation.get('summary'),
            'volatility_risk_multiplier': strategy_adaptation.get('volatility_risk_multiplier'),
            'meta_label_risk_multiplier': strategy_adaptation.get('meta_label_risk_multiplier'),
            'trend_health_risk_multiplier': strategy_adaptation.get('trend_health_risk_multiplier'),
            'trend_health_score': trend_health.get('score'),
            'trend_health_summary': trend_health.get('summary'),
            'strategy_quality_risk_multiplier': strategy_adaptation.get('strategy_quality_risk_multiplier'),
            'strategy_quality_score': strategy_quality.get('score'),
            'strategy_quality_summary': strategy_quality.get('summary'),
            'quality_score_v2_score': quality_score_v2.get('score'),
            'quality_score_v2_state': quality_score_v2.get('state'),
            'quality_score_v2_summary': quality_score_v2.get('summary'),
            'quality_score_v2_risk_multiplier': quality_score_v2_multiplier,
            'position_sizing_enabled': bool(cfg.get('position_sizing_engine_enabled', True)),
            'position_sizing_risk_multiplier': status.get('position_sizing_risk_multiplier'),
            'position_sizing_components': status.get('position_sizing_components'),
            'position_sizing_reasons': status.get('position_sizing_reasons'),
            'position_sizing_performance_delegated_to_allocator': status.get(
                'position_sizing_performance_delegated_to_allocator'
            ),
            'raw_final_risk_multiplier_before_position_sizing': status.get(
                'raw_final_risk_multiplier_before_position_sizing'
            ),
            'profit_alpha_enabled': profit_alpha_decision is not None,
            'profit_alpha_engine': (
                profit_alpha_decision.engine if profit_alpha_decision is not None else None
            ),
            'profit_alpha_score': (
                profit_alpha_decision.score if profit_alpha_decision is not None else None
            ),
            'profit_alpha_probability': (
                profit_alpha_decision.probability if profit_alpha_decision is not None else None
            ),
            'profit_alpha_risk_multiplier': (
                profit_alpha_decision.risk_multiplier if profit_alpha_decision is not None else None
            ),
            'profit_alpha_entry_type': (
                profit_alpha_decision.entry_type if profit_alpha_decision is not None else None
            ),
            'profit_alpha_direction_score': (
                profit_alpha_decision.direction_score if profit_alpha_decision is not None else None
            ),
            'profit_alpha_exit_policy': (
                profit_alpha_decision.exit_policy if profit_alpha_decision is not None else None
            ),
            'profit_alpha_meta_key': (
                profit_alpha_decision.meta_key if profit_alpha_decision is not None else None
            ),
            'profit_alpha_meta_sample_count': (
                profit_alpha_decision.meta_sample_count if profit_alpha_decision is not None else None
            ),
            'profit_alpha_summary': status.get('profit_alpha_summary'),
            'profit_alpha_components': (
                dict(profit_alpha_decision.components or {})
                if profit_alpha_decision is not None
                else None
            ),
            'profit_alpha_follow_through_enabled': bool(
                cfg.get('profit_alpha_follow_through_enabled', True)
            ),
            'profit_alpha_follow_through_bars': int(
                cfg.get('profit_alpha_follow_through_bars', 3) or 3
            ),
            'profit_alpha_follow_through_min_mfe_r': float(
                cfg.get('profit_alpha_follow_through_min_mfe_r', 0.35) or 0.35
            ),
            'profit_alpha_early_exit_max_mae_r': float(
                cfg.get('profit_alpha_early_exit_max_mae_r', 0.75) or 0.75
            ),
            'dynamic_take_profit_enabled': bool(cfg.get('dynamic_take_profit_enabled', True)),
            'dynamic_take_profit_summary': dynamic_tp2.get('summary'),
            'dynamic_tp2_r_multiple': dynamic_tp2.get('second_take_profit_r_multiple'),
            'aggressive_growth_enabled': bool(cfg.get('aggressive_growth_enabled', False)),
            'aggressive_growth_overlay': bool(plan.get('aggressive_growth_overlay', False)),
            'aggressive_growth_score': plan.get('aggressive_growth_score'),
            'aggressive_growth_risk_pct': plan.get('aggressive_growth_risk_pct'),
            'runner_pct': plan.get('runner_pct', cfg.get('runner_pct')),
            'tp1_breakeven_enabled': bool(cfg.get('tp1_breakeven_enabled', True)),
            'tp1_breakeven_trigger_r': float(cfg.get('tp1_breakeven_trigger_r', cfg.get('partial_take_profit_r_multiple', 1.00)) or 1.00),
            'tp1_breakeven_offset_r': float(cfg.get('tp1_breakeven_offset_r', 0.03) or 0.03),
            'tp1_breakeven_wait_for_partial': bool(cfg.get('tp1_breakeven_wait_for_partial', True)),
            'tp1_breakeven_qty_tolerance': float(cfg.get('tp1_breakeven_qty_tolerance', 0.08) or 0.08),
            'adaptive_exit_summary': status.get('adaptive_exit_summary'),
            'trend_continuation_entry': status.get('trend_continuation_entry'),
            'trend_continuation_accepted': status.get('trend_continuation_accepted'),
            'trend_continuation_summary': status.get('trend_continuation_summary'),
            'trend_continuation_risk_multiplier': status.get('trend_continuation_risk_multiplier'),
            'set_filter_risk_multiplier': status.get('set_filter_risk_multiplier'),
            'set_filter_soft_fail_summary': status.get('set_filter_soft_fail_summary'),
            'set_filter_soft_fail_count': status.get('set_filter_soft_fail_count'),
            'set_filter_state': status.get('set_filter_state'),
            'raw_final_risk_multiplier': status.get('raw_final_risk_multiplier'),
            'final_risk_multiplier': status.get('final_risk_multiplier'),
            'decision_trace': status.get('decision_trace'),
            'direction_decision': status.get('direction_decision'),
            'direction_btc_4h_symbol': status.get('direction_btc_4h_symbol'),
            'direction_btc_1d_symbol': status.get('direction_btc_1d_symbol'),
            'direction_symbol_1h_symbol': status.get('direction_symbol_1h_symbol'),
            'shadow_sample_count': shadow_stats.get('sample_count'),
            'shadow_win_rate': shadow_stats.get('tp_rate'),
            'shadow_avg_pnl_r': shadow_stats.get('avg_pnl_r'),
            'runner_sample_count': runner_stats.get('sample_count'),
            'runner_avg_mfe_capture_ratio': runner_stats.get('avg_mfe_capture_ratio'),
            'runner_avg_pnl_r': runner_stats.get('avg_pnl_r'),
            'ev_adaptive_enabled': ev_decision is not None,
            'ev_adaptive_mode': ev_decision.mode if ev_decision is not None else None,
            'ev_adaptive_score': ev_decision.score if ev_decision is not None else None,
            'ev_win_probability': ev_decision.win_probability if ev_decision is not None else None,
            'ev_signal_age_candles': (
                ev_decision.signal_age_candles if ev_decision is not None else None
            ),
            'ev_mtf_alignment': ev_decision.mtf_alignment if ev_decision is not None else None,
            'ev_momentum_alignment': (
                ev_decision.momentum_alignment if ev_decision is not None else None
            ),
            'ev_leadership_score': (
                ev_decision.leadership_score if ev_decision is not None else None
            ),
            'ev_reacceleration': (
                ev_decision.reacceleration if ev_decision is not None else False
            ),
            'ev_extension_atr': (
                ev_decision.extension_atr if ev_decision is not None else None
            ),
            'ev_gross_win_r': ev_decision.gross_win_r if ev_decision is not None else None,
            'ev_expected_r_before_cost': (
                ev_decision.expected_r_before_cost if ev_decision is not None else None
            ),
            'ev_exit_profile_name': cfg.get('ev_exit_profile_name'),
            'ev_time_stop_enabled': bool(cfg.get('ev_time_stop_enabled', False)),
            'ev_time_stop_bars': int(cfg.get('ev_time_stop_bars', 8) or 8),
            'ev_time_stop_min_mfe_r': float(cfg.get('ev_time_stop_min_mfe_r', 0.45) or 0.45),
            'ev_mfe_profit_lock_enabled': bool(cfg.get('ev_mfe_profit_lock_enabled', True)),
            'ev_mfe_lock_trigger_1_r': float(cfg.get('ev_mfe_lock_trigger_1_r', 1.50) or 1.50),
            'ev_mfe_lock_trigger_2_r': float(cfg.get('ev_mfe_lock_trigger_2_r', 2.20) or 2.20),
            'ev_mfe_lock_trigger_3_r': float(cfg.get('ev_mfe_lock_trigger_3_r', 3.20) or 3.20),
        })
        self._register_utbreakout_shadow_candidate(symbol, side, status, plan, cfg, selected_set)
        plan, micro_reject = await self._apply_micro_auto_to_utbreakout_plan(
            symbol=symbol,
            side=side,
            base_plan=plan,
            selected_set=selected_set,
            auto_analysis=auto_analysis,
            cfg=cfg,
            entry_price=entry_price,
            atr_value=atr_value,
            ut_stop=ut_detail.get('curr_stop'),
            total_balance=total_balance,
            free_balance=free_balance,
            market_quality_risk_multiplier=final_risk_multiplier,
        )
        if micro_reject:
            code = micro_reject.get('reject_code') or 'REJECTED_MICRO_AUTO'
            reason = micro_reject.get('reason') or 'Micro Auto V1 rejected candidate'
            status['micro_auto'] = micro_reject
            status['micro_auto_summary'] = f"{code}: {reason}"
            return _finish(None, f"{code}: {reason}", code, record_failure=True, side=side)

        if plan.get('micro_auto'):
            status['micro_auto'] = dict(plan)
            status['micro_auto_summary'] = (
                f"Micro Auto V1 {'DRY-RUN' if plan.get('dry_run') else 'LIVE'}: "
                f"{plan.get('planned_notional', 0):.2f} notional / "
                f"{plan.get('planned_margin', 0):.2f} margin / "
                f"{plan.get('leverage', 0)}x / fee burden {plan.get('fee_burden_pct', 0):.1f}%"
            )

        if ev_decision is not None:
            exit_feasibility = adapt_exit_for_quantity(
                ev_decision.exit_profile,
                total_qty=plan.get('qty'),
                min_amount=self._get_min_amount_for_symbol(symbol),
            )
            status['ev_exit_feasibility'] = {
                'executable': exit_feasibility.executable,
                'mode': exit_feasibility.mode,
                'reason': exit_feasibility.reason,
            }

            cfg = _apply_ev_exit_profile(cfg, exit_feasibility.profile)
            if profit_alpha_decision is not None and profit_alpha_decision.allowed:
                cfg = apply_profit_alpha_exit_overrides(cfg, profit_alpha_decision)
            target_r = float(cfg.get('take_profit_r_multiple', 0.0) or 0.0)
            risk_distance = float(plan.get('risk_distance', 0.0) or 0.0)
            effective_tp_distance = self._utbreakout_tp_distance_after_front_run(
                risk_distance,
                target_r,
                cfg,
                plan.get('atr') or atr_value,
            ) or (risk_distance * target_r)
            plan.update({
                'fixed_take_profit_enabled': True,
                'partial_take_profit_enabled': bool(cfg.get('partial_take_profit_enabled', True)),
                'partial_take_profit_r_multiple': float(cfg.get('partial_take_profit_r_multiple', 1.0) or 1.0),
                'partial_take_profit_ratio': float(cfg.get('partial_take_profit_ratio', 0.0) or 0.0),
                'second_take_profit_enabled': bool(cfg.get('second_take_profit_enabled', False)),
                'second_take_profit_r_multiple': float(cfg.get('second_take_profit_r_multiple', target_r) or target_r),
                'second_take_profit_ratio': float(cfg.get('second_take_profit_ratio', 0.0) or 0.0),
                'runner_pct': float(cfg.get('runner_pct', 0.0) or 0.0),
                'atr_trailing_enabled': bool(cfg.get('atr_trailing_enabled', False)),
                'atr_trailing_activation_r': float(cfg.get('atr_trailing_activation_r', target_r) or target_r),
                'atr_trailing_multiplier': float(cfg.get('atr_trailing_multiplier', 2.7) or 2.7),
                'runner_exit_enabled': bool(cfg.get('runner_exit_enabled', False)),
                'runner_chandelier_enabled': bool(cfg.get('runner_chandelier_enabled', False)),
                'tp1_breakeven_enabled': bool(cfg.get('tp1_breakeven_enabled', True)),
                'tp1_breakeven_trigger_r': float(cfg.get('tp1_breakeven_trigger_r', 1.0) or 1.0),
                'ev_exit_profile_name': exit_feasibility.profile.name,
                'ev_exit_mode': exit_feasibility.mode,
                'ev_exit_feasibility_reason': exit_feasibility.reason,
                'ev_time_stop_enabled': bool(cfg.get('ev_time_stop_enabled', True)),
                'ev_time_stop_bars': int(cfg.get('ev_time_stop_bars', 8) or 8),
                'ev_time_stop_min_mfe_r': float(cfg.get('ev_time_stop_min_mfe_r', 0.45) or 0.45),
                'rr_multiple': target_r,
                'effective_rr_multiple': effective_tp_distance / max(risk_distance, 1e-9),
                'take_profit_distance': effective_tp_distance,
                'take_profit_front_run_atr': float(cfg.get('take_profit_front_run_atr', 0.14) or 0.14),
                'take_profit_front_run_pct': float(cfg.get('take_profit_front_run_pct', 0.055) or 0.055),
                'take_profit': (
                    entry_price + effective_tp_distance
                    if side == 'long'
                    else entry_price - effective_tp_distance
                ),
            })
            net_edge = evaluate_net_edge(
                risk_usdt=plan.get('risk_usdt'),
                planned_notional=plan.get('planned_notional'),
                win_probability=entry_edge_decision.probability,
                gross_win_r=profile_gross_win_r(exit_feasibility.profile),
                config=_ev_adaptive_runtime_config(cfg),
            )
            status['ev_net_edge'] = {
                'allowed': net_edge.allowed,
                'expected_net_r': net_edge.expected_net_r,
                'expected_net_usdt': net_edge.expected_net_usdt,
                'roundtrip_cost_usdt': net_edge.roundtrip_cost_usdt,
                'cost_r': net_edge.cost_r,
                'reason': net_edge.reason,
            }
            entry_edge_decision = build_entry_edge_decision(
                side=side,
                ev_decision=ev_decision,
                alpha_decision=profit_alpha_decision,
                ev_net=net_edge,
                ev_exit=exit_feasibility,
                config=cfg,
            )
            status['entry_edge'] = self._entry_edge_status_payload(entry_edge_decision)
            status['entry_edge_summary'] = entry_edge_decision.summary
            status['entry_edge_engine'] = entry_edge_decision.engine
            status['entry_edge_score'] = entry_edge_decision.score
            status['entry_edge_probability'] = entry_edge_decision.probability
            status['entry_edge_net_expectancy_r'] = entry_edge_decision.net_expectancy_r
            status['entry_edge_risk_multiplier'] = entry_edge_decision.risk_multiplier
            status['entry_edge_entry_type'] = entry_edge_decision.entry_type
            status['entry_edge_direction_score'] = entry_edge_decision.direction_score
            status['entry_edge_exit_policy'] = entry_edge_decision.exit_policy
            if not entry_edge_decision.allowed:
                return _finish(
                    None,
                    "REJECTED_ENTRY_EDGE: " + "; ".join(entry_edge_decision.blockers[:6]),
                    'REJECTED_ENTRY_EDGE',
                    record_failure=True,
                    side=side,
                )
            plan.update({
                'ev_net_edge_r': net_edge.expected_net_r,
                'ev_expected_net_usdt': net_edge.expected_net_usdt,
                'ev_roundtrip_cost_usdt': net_edge.roundtrip_cost_usdt,
                'ev_cost_r': net_edge.cost_r,
                'ev_net_edge_reason': net_edge.reason,
            })
            status['adaptive_exit_summary'] = (
                f"EV {exit_feasibility.profile.name}: "
                f"TP1 {exit_feasibility.profile.tp1_r:.2f}R"
                f"({exit_feasibility.profile.tp1_ratio:.0%}) / "
                f"TP2 {exit_feasibility.profile.tp2_r:.2f}R"
                f"({exit_feasibility.profile.tp2_ratio:.0%}) / "
                f"runner {exit_feasibility.profile.runner_ratio:.0%}; "
                f"net {net_edge.expected_net_r:.3f}R"
            )
            if entry_edge_decision is not None and entry_edge_decision.allowed:
                status['adaptive_exit_summary'] = (
                    f"Entry Edge {entry_edge_decision.engine}/{entry_edge_decision.entry_type}: "
                    f"TP1 {float(cfg.get('partial_take_profit_r_multiple', 1.0) or 1.0):.2f}R"
                    f"({float(cfg.get('partial_take_profit_ratio', 0.0) or 0.0):.0%}) / "
                    f"TP2 {float(cfg.get('second_take_profit_r_multiple', target_r) or target_r):.2f}R"
                    f"({float(cfg.get('second_take_profit_ratio', 0.0) or 0.0):.0%}) / "
                    f"runner {float(cfg.get('runner_pct', 0.0) or 0.0):.0%}; "
                    f"score {entry_edge_decision.score:.1f}, "
                    f"dir {entry_edge_decision.direction_score:.1f}, "
                    f"p={entry_edge_decision.probability:.3f}, "
                    f"net {net_edge.expected_net_r:.3f}R"
                )
            plan['adaptive_exit_summary'] = status['adaptive_exit_summary']
            plan.update({
                'entry_edge_engine': entry_edge_decision.engine,
                'entry_edge_score': entry_edge_decision.score,
                'entry_edge_probability': entry_edge_decision.probability,
                'entry_edge_net_expectancy_r': entry_edge_decision.net_expectancy_r,
                'entry_edge_risk_multiplier': entry_edge_decision.risk_multiplier,
                'entry_edge_summary': entry_edge_decision.summary,
                'entry_edge_entry_type': entry_edge_decision.entry_type,
                'entry_edge_direction_score': entry_edge_decision.direction_score,
                'entry_edge_exit_policy': entry_edge_decision.exit_policy,
            })

        self._set_utbot_filtered_breakout_entry_plan(symbol, plan)
        status['risk_summary'] = (
            f"risk={plan['risk_usdt']:.4f} USDT, distance={plan['risk_distance']:.4f}, "
            f"SL={plan['stop_loss']:.4f}, TP={plan['take_profit']:.4f}, "
            f"qty={plan['qty']:.8f}, margin={plan['planned_margin']:.2f}, "
            f"notional={plan['planned_notional']:.2f}, RR={plan['rr_multiple']:.2f}"
        )
        status['entry_plan'] = dict(plan)
        return _finish(
            side,
            f"ACCEPTED_ENTRY: {side.upper()} Set{selected_set.get('id')} {selected_set.get('name')} confirmed ({candidate_type})",
            None
        )

    def _utbreakout_status_symbol_key(self, symbol):
        return normalize_futures_market_id(symbol)

    def _utbreakout_diag_for_symbol(self, symbol):
        statuses = getattr(self, 'last_utbot_filtered_breakout_status', {})
        if not isinstance(statuses, dict):
            return {}
        direct = statuses.get(symbol)
        if isinstance(direct, dict):
            return direct
        target_key = self._utbreakout_status_symbol_key(symbol)
        for status_symbol, status in statuses.items():
            if self._utbreakout_status_symbol_key(status_symbol) == target_key and isinstance(status, dict):
                return status
        return {}

    def _utbreakout_should_skip_adaptive_decision(
        self,
        symbol,
        timeframe,
        decision_ts,
        *,
        force_reprocess=False,
    ):
        decisions = self._ensure_runtime_state_container(
            'utbreakout_adaptive_last_decision_ts'
        )
        symbol_decisions = decisions.setdefault(symbol, {})
        tf_key = str(timeframe or '15m')
        current_ts = int(decision_ts or 0)
        last_ts = int(symbol_decisions.get(tf_key, 0) or 0)
        if current_ts <= last_ts and not force_reprocess:
            return True, last_ts
        symbol_decisions[tf_key] = max(last_ts, current_ts)
        return False, last_ts

    def _recover_utbreakout_accepted_side(self, symbol, decision_ts=None):
        diag = self._utbreakout_diag_for_symbol(symbol)
        accepted_side = str((diag or {}).get('accepted_side') or '').lower()
        if accepted_side not in {'long', 'short'}:
            return None
        if decision_ts is not None:
            try:
                diag_ts = int((diag or {}).get('decision_candle_ts') or 0)
                if diag_ts <= 0 or diag_ts != int(decision_ts):
                    return None
            except (TypeError, ValueError):
                return None
        plan = self._get_utbot_filtered_breakout_entry_plan(symbol, accepted_side)
        if not isinstance(plan, dict):
            return None
        if not self.is_trade_direction_allowed(accepted_side):
            return None
        return accepted_side

    def _utbreakout_status_row_for_symbol(self, symbol):
        status_data = getattr(self.ctrl, 'status_data', {}) if getattr(self, 'ctrl', None) else {}
        if not isinstance(status_data, dict):
            return {}
        direct = status_data.get(symbol)
        if isinstance(direct, dict):
            return direct
        target_key = self._utbreakout_status_symbol_key(symbol)
        for status_symbol, row in status_data.items():
            if self._utbreakout_status_symbol_key(status_symbol) == target_key and isinstance(row, dict):
                return row
        return {}

    def _format_utbreakout_diag_reason(self, diag, *, entry=False):
        if not isinstance(diag, dict) or not diag:
            return "아직 기록 없음"
        if entry:
            return (
                diag.get('reason')
                or diag.get('accepted_code')
                or diag.get('reject_code')
                or "진입 기록 대기"
            )
        selected_set = diag.get('auto_selected_set_id')
        selected_name = diag.get('auto_selected_set_name') or ''
        reason = diag.get('auto_selection_reason') or diag.get('selector_quality_summary') or diag.get('strategy_adaptation_summary')
        if selected_set and reason:
            return f"Set{selected_set} {selected_name}: {reason}"
        if selected_set:
            return f"Set{selected_set} {selected_name}"
        return reason or "수동 선택 또는 AUTO 기록 대기"

    def _format_coin_selector_candidate_reason(self, item):
        if not isinstance(item, dict):
            return "후보 정보 없음"
        components = item.get('component_scores') if isinstance(item.get('component_scores'), dict) else {}
        reason_parts = [
            f"점수 {float(item.get('score', 0) or 0):.1f}",
            f"상태 {item.get('selection_state') or 'WATCH'}",
            f"Set{item.get('auto_set_id') or '?'} {item.get('auto_set_name') or ''}".strip(),
            f"TF {item.get('adaptive_tf') or 'n/a'}",
        ]
        auto_reason = item.get('auto_selection_reason') or item.get('adaptive_reason')
        if auto_reason:
            reason_parts.append(str(auto_reason))
        elif components:
            reason_parts.append(
                "구성 "
                f"liq {components.get('liquidity_cost', 0):.1f}, "
                f"ut {components.get('utbreakout_regime', 0):.1f}, "
                f"set {components.get('auto_set', 0):.1f}, "
                f"tf {components.get('adaptive_tf', 0):.1f}, "
                f"qual {components.get('selection_quality', 0):.1f}"
            )
        warnings = ", ".join(str(w) for w in (item.get('soft_warnings') or [])[:3])
        if warnings:
            reason_parts.append(f"주의 {warnings}")
        return " / ".join(part for part in reason_parts if part)

    async def _resolve_next_utbreakout_scan_candidate(self, excluded_symbols=None):
        """Return the next actionable CoinSelector candidate.

        This is shared by:
        - _resolve_utbreakout_status_symbol()
        - _build_utbreakout_position_scan_context_lines()

        It prevents the status header from showing one symbol while the condition
        body evaluates another.
        """
        excluded_symbols = excluded_symbols or set()
        excluded_keys = {
            self._utbreakout_status_symbol_key(item)
            for item in excluded_symbols
            if item
        }

        next_candidate = None
        report = self.coin_selector_last_result if isinstance(getattr(self, 'coin_selector_last_result', None), dict) else {}
        selected = list(report.get('selected') or [])
        coin_cfg = self._get_coin_selector_config()

        if not selected and bool(coin_cfg.get('enabled', False)):
            try:
                if self._micro_auto_enabled():
                    report = await self.evaluate_micro_auto_candidates(force=False)
                else:
                    report = await self.evaluate_coin_selector(force=False)
                selected = list(report.get('selected') or [])
            except Exception as exc:
                logger.warning(f"UTBreak next scan candidate lookup failed: {exc}")
                selected = []

        now = time.time()
        for item in selected:
            if not isinstance(item, dict):
                continue

            symbol = item.get('exchange_symbol') or item.get('normalized_symbol') or item.get('symbol')
            if not symbol:
                continue

            symbol_key = self._utbreakout_status_symbol_key(symbol)
            if symbol_key in excluded_keys:
                continue

            ok_market, _, _ = self._ensure_valid_utbreakout_market_symbol(
                symbol,
                source='resolve_next_scan_candidate',
            )
            if not ok_market:
                continue

            next_candidate = item
            break

        if not next_candidate:
            return None, None

        next_symbol = (
            next_candidate.get('exchange_symbol')
            or next_candidate.get('normalized_symbol')
            or next_candidate.get('symbol')
        )

        return next_symbol, next_candidate

    async def _build_utbreakout_position_scan_context_lines(self, evaluated_symbol):
        lines = []
        position_symbols = []
        try:
            position_symbols = sorted(
                await self.get_active_position_symbols(use_cache=False),
                key=self._utbreakout_status_symbol_key
            )
        except Exception as exc:
            lines.append(f"현재 포지션: 조회 실패 ({exc})")

        if position_symbols:
            for pos_symbol in position_symbols[:3]:
                pos = await self.get_server_position(pos_symbol, use_cache=False)
                diag = self._utbreakout_diag_for_symbol(pos_symbol)
                status_row = self._utbreakout_status_row_for_symbol(pos_symbol)
                side = str((pos or {}).get('side') or status_row.get('pos_side') or 'UNKNOWN').upper()
                contracts = float((pos or {}).get('contracts') or status_row.get('coin_amt') or 0.0)
                entry_price = float((pos or {}).get('entryPrice') or status_row.get('entry_price') or 0.0)
                pnl = float((pos or {}).get('unrealizedPnl') or status_row.get('pnl_usdt') or 0.0)
                lines.extend([
                    f"현재 포지션: {pos_symbol} `{side}` qty `{contracts:.6g}` entry `{entry_price:.6g}` PnL `{pnl:+.2f}`",
                    f"포지션 코인 선택 이유: {self._format_utbreakout_diag_reason(diag)}",
                    f"포지션 진입 이유: {self._format_utbreakout_diag_reason(diag, entry=True)}",
                ])
            if len(position_symbols) > 3:
                lines.append(f"현재 포지션 추가: {len(position_symbols) - 3}개")
        else:
            lines.append("현재 포지션: 없음")

        status_source = str(getattr(self, 'utbreakout_status_symbol_source', '') or '')
        status_detail = getattr(self, 'utbreakout_status_symbol_detail', None)
        if status_source in {'no_live_candidate', 'watchlist_fallback_no_live_candidate'}:
            lines.append("현재 live 후보: 없음")
            if status_source == 'watchlist_fallback_no_live_candidate' and evaluated_symbol:
                lines.append(
                    f"상태 화면 참고 심볼: {evaluated_symbol} "
                    "(live 후보가 없어 watchlist 첫 항목을 참고 평가)"
                )
            elif not evaluated_symbol:
                lines.append("조건 평가 심볼: 없음 (live 후보 발생 전에는 참고 심볼 조건표를 표시하지 않음)")
        elif status_source == 'live_candidate':
            lines.append(f"현재 live 후보: {evaluated_symbol}")
        elif status_source == 'scanner_lock':
            lines.append(f"현재 live 후보: {evaluated_symbol} (scanner lock)")
        elif status_source == 'position':
            lines.append(f"현재 live 후보: 포지션 보유 심볼 {evaluated_symbol}")
        elif status_source == 'status_data_position':
            lines.append(f"현재 live 후보: 최근 포지션 상태 {evaluated_symbol}")
        elif status_detail:
            lines.append(f"상태 심볼 출처: {status_detail}")

        scanner_symbol = getattr(self, 'scanner_active_symbol', None)
        if scanner_symbol:
            lines.append(f"현재 scanner lock: {scanner_symbol}")
        try:
            common_cfg = self.get_runtime_common_settings()
            selector_cfg = self._get_coin_selector_config()
            scanner_interval = self._get_scanner_scan_interval_seconds(common_cfg)
            selector_interval = float(
                selector_cfg.get('refresh_interval_seconds', 300.0) or 300.0
            )
            lines.append(
                f"스캐너 주기: 후보 확인 {scanner_interval:.0f}초 / "
                f"CoinSelector 새로고침 {selector_interval:.0f}초"
            )
        except Exception:
            pass

        next_symbol, next_candidate = await self._resolve_next_utbreakout_scan_candidate(
            excluded_symbols=position_symbols
        )
        if next_symbol:
            ok_market, next_symbol, _ = (
                self._ensure_valid_utbreakout_market_symbol(
                    next_symbol,
                    source='status_next_candidate_filter',
                )
            )
        else:
            ok_market = True
        if not ok_market:
            next_symbol = None
            next_candidate = None

        coin_cfg = self._get_coin_selector_config()
        if next_candidate:
            next_symbol = self._canonicalize_utbreakout_symbol_for_use(
                next_symbol,
            )
            self.current_utbreakout_candidate_symbol = next_symbol
            self._utbreakout_trace_event(
                next_symbol,
                'COIN_SELECTED',
                'SELECTED',
                score=next_candidate.get('score'),
                set_name=(
                    next_candidate.get('auto_set_name')
                    or next_candidate.get('auto_set_id')
                ),
                timeframe=next_candidate.get('adaptive_tf'),
                reason=self._format_coin_selector_candidate_reason(next_candidate),
                source='status_scan_context',
            )
            lines.append(f"다음 스캔 후보: {next_symbol}")
            lines.append(f"다음 후보 선택 이유: {self._format_coin_selector_candidate_reason(next_candidate)}")
        elif bool(coin_cfg.get('enabled', False)):
            lines.append("다음 스캔 후보: 없음 또는 후보 쿨다운/대기")
            report = (
                self.coin_selector_last_result
                if isinstance(getattr(self, 'coin_selector_last_result', None), dict)
                else {}
            )
            watch_only = list(report.get('watch_only') or [])
            if watch_only:
                preview_parts = []
                for item in watch_only[:3]:
                    preview_symbol = (
                        item.get('normalized_symbol')
                        or item.get('exchange_symbol')
                        or item.get('symbol')
                    )
                    ev_reason = str(item.get('ev_reason') or '').replace('\n', ' ')
                    if len(ev_reason) > 45:
                        ev_reason = ev_reason[:42] + "..."
                    preview_parts.append(
                        f"{preview_symbol} {float(item.get('score', 0) or 0):.1f}점 "
                        f"{item.get('selection_state') or 'WATCH_ONLY'}"
                        + (f"({ev_reason})" if ev_reason else "")
                    )
                lines.append(
                    "상위 감시 후보(진입 차단): "
                    + " / ".join(preview_parts)
                )
            reason_counts = report.get('watch_only_reason_counts') if isinstance(report, dict) else {}
            if reason_counts:
                reason_preview = ", ".join(
                    f"{reason} {count}"
                    for reason, count in sorted(
                        reason_counts.items(),
                        key=lambda item: item[1],
                        reverse=True,
                    )[:3]
                )
                lines.append(f"후보 차단 요약: {reason_preview}")
        else:
            lines.append("다음 스캔 후보: CoinSelector OFF")

        if evaluated_symbol:
            lines.append(f"조건 평가 심볼: {evaluated_symbol}")

            if next_symbol and self._utbreakout_status_symbol_key(next_symbol) != self._utbreakout_status_symbol_key(evaluated_symbol):
                lines.append(
                    f"⚠️ 상태 경고: 다음 후보({next_symbol})와 조건 평가 심볼({evaluated_symbol})이 다릅니다."
                )
        return lines

    async def _build_direction_decision_for_status(self, symbol, side, filter_values):
        try:
            from utbreakout.direction_filter import decide_direction

            btc_candidates = (
                ["BTC/USDT:USDT", "BTC/USDT"]
                if ":USDT" in str(symbol)
                else ["BTC/USDT", "BTC/USDT:USDT"]
            )

            btc_4h_raw = None
            btc_1d_raw = None
            sym_1h_raw = None

            for btc_symbol in btc_candidates:
                try:
                    btc_4h_raw = await self.fetch_ohlcv_async(btc_symbol, "4h", limit=60)
                    if btc_4h_raw is not None and len(btc_4h_raw) > 0:
                        break
                except Exception:
                    pass

            for btc_symbol in btc_candidates:
                try:
                    btc_1d_raw = await self.fetch_ohlcv_async(btc_symbol, "1d", limit=60)
                    if btc_1d_raw is not None and len(btc_1d_raw) > 0:
                        break
                except Exception:
                    pass

            try:
                sym_1h_raw = await self.fetch_ohlcv_async(symbol, "1h", limit=60)
            except Exception:
                sym_1h_raw = None

            btc_4h = self._build_direction_metrics_dict(btc_4h_raw)
            btc_1d = self._build_direction_metrics_dict(btc_1d_raw)
            sym_1h = self._build_direction_metrics_dict(sym_1h_raw)

            decision = decide_direction(
                btc_4h=btc_4h,
                btc_1d=btc_1d,
                symbol_1h=sym_1h,
                entry_15m=filter_values or {},
                side_hint=side,
            )

            allowed = decision.long_allowed if side == "long" else decision.short_allowed
            state = True if allowed else False
            return state, (
                f"{'PASS' if allowed else 'BLOCK'}: regime {decision.regime}, "
                f"{decision.reason}"
            ), decision
        except Exception as exc:
            logger.exception("direction filter status evaluation failed")
            return 'reduced', f"방향 필터 평가 오류: {exc}", None

    async def force_utbreakout_entry_from_status(self, symbol):
        symbol = self._canonicalize_utbreakout_symbol_for_use(
            symbol,
            source='forceentry',
        )
        strategy_params = self.get_runtime_strategy_params()
        active_strategy = str(
            strategy_params.get('active_strategy', '') or ''
        ).lower()
        if active_strategy not in UTBREAKOUT_STRATEGIES:
            message = f"⚠️ forceentry 실패: 현재 전략이 UTBreakout이 아닙니다 ({active_strategy or 'unknown'})"
            await self.ctrl.notify(message)
            return {'status': 'BLOCKED', 'reason': message}
        if bool(getattr(self.ctrl, 'is_paused', False)):
            message = "⚠️ forceentry 실패: 현재 거래가 PAUSE 상태입니다"
            await self.ctrl.notify(message)
            return {'status': 'PAUSED', 'reason': message}

        existing = await self.get_server_position(symbol, use_cache=False)
        if existing:
            message = (
                f"⚠️ forceentry 중단: {symbol} 포지션이 이미 있습니다 "
                f"({str(existing.get('side') or 'unknown').upper()})"
            )
            await self.ctrl.notify(message)
            return {'status': 'POSITION_EXISTS', 'reason': message}

        trade_cfg = self.get_runtime_trade_config()
        primary_tf = self._get_primary_poll_timeframe(symbol, trade_cfg)
        ohlcv = await asyncio.to_thread(
            self.market_data_exchange.fetch_ohlcv,
            symbol,
            primary_tf,
            limit=300,
        )
        if not ohlcv or len(ohlcv) < 5:
            message = f"⚠️ forceentry 실패: {symbol} {primary_tf} 데이터 부족"
            await self.ctrl.notify(message)
            return {'status': 'NO_DATA', 'reason': message}

        df = pd.DataFrame(
            ohlcv,
            columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'],
        )
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        sig, reason, status = await self._calculate_utbot_filtered_breakout_signal(
            symbol,
            df,
            strategy_params,
            force_reprocess=True,
        )
        if sig not in {'long', 'short'}:
            message = (
                f"⚠️ forceentry 실패: 현재 accepted_side 없음 / "
                f"{symbol} / {reason or (status or {}).get('reason') or 'unknown'}"
            )
            await self.ctrl.notify(message)
            return {'status': 'NO_ACCEPTED_SIDE', 'reason': message}
        if not self.is_trade_direction_allowed(sig):
            message = f"⚠️ forceentry 실패: {symbol} {self.format_trade_direction_block_reason(sig)}"
            await self.ctrl.notify(message)
            return {'status': 'DIRECTION_BLOCKED', 'reason': message}

        plan = self._get_utbot_filtered_breakout_entry_plan(symbol, sig) or {}
        price = float(plan.get('entry_price') or df.iloc[-2]['close'])
        self._utbreakout_trace_event(
            symbol,
            'ENTRY_CALL',
            'CALLING',
            side=sig,
            entry_ref_price=price,
            source='force_utbreakout_entry_from_status',
        )
        await self.ctrl.notify(
            f"🟡 UTBreakout forceentry 시도: {symbol} {sig.upper()} price={price}"
        )
        await self.entry(symbol, sig, price)

        post_pos = await self.get_server_position(symbol, use_cache=False)
        if not post_pos:
            entry_reason = self.last_entry_reason.get(symbol) or 'unknown'
            message = (
                f"⚠️ UTBreakout forceentry 후 포지션 미생성: "
                f"{symbol} {sig.upper()} / reason={entry_reason}"
            )
            await self.ctrl.notify(message)
            return {'status': 'NO_POSITION', 'reason': message, 'side': sig}
        return {'status': 'POSITION_OPENED', 'side': sig, 'position': post_pos}

    def _compact_side_gate_summary(
        self,
        side: str,
        ok: bool,
        side_lines: list[str],
        execution_eligibility: dict | None = None
    ) -> str:
        # 1. Side label
        side_label = "LONG" if side == "long" else "SHORT"

        # 2. Entry status
        if execution_eligibility is not None:
            if execution_eligibility.get('can_attempt'):
                entry_status = "진입 가능 / 주문시도 대상"
            elif ok:
                entry_status = "조건통과 / 주문 안함"
            else:
                entry_status = "진입 안함"
        else:
            entry_status = "진입 가능" if ok else "진입 안함"

        # Extract "필수 게이트" slice
        required_start = -1
        required_end = len(side_lines)
        for i, line in enumerate(side_lines):
            if "필수 게이트" in line:
                required_start = i + 1
            elif "참고/감액" in line and required_start != -1:
                required_end = i
                break

        required_lines = []
        if required_start != -1 and required_start < required_end:
            required_lines = side_lines[required_start:required_end]

        # 3. Gate icons
        icons = []
        for line in required_lines:
            if "🔴" in line or "불만족" in line or "차단" in line or "REJECTED" in line:
                icons.append("🔴")
            elif "🟢" in line or "만족" in line:
                icons.append("🟢")
            elif "🟡" in line or "축소" in line or "대기" in line or "감액" in line:
                icons.append("🟡")
            else:
                icons.append("⚪")

        # Pad or truncate to 7
        if len(icons) < 7:
            icons.extend(["⚪"] * (7 - len(icons)))
        else:
            icons = icons[:7]
        icon_str = "".join(icons)

        # 4. Score extraction
        score_val = "n/a"
        # Search all lines
        for line in side_lines:
            m = re.search(r"score\s+([0-9]+(?:\.[0-9]+)?)", line, re.IGNORECASE)
            if m:
                score_val = m.group(1)
                break
        if score_val == "n/a":
            for line in side_lines:
                m = re.search(r"Feature Score:\s*([0-9]+(?:\.[0-9]+)?)", line)
                if m:
                    score_val = m.group(1)
                    break
        score_str = f"점수 {score_val}"

        # 5. Reason extraction
        raw_reason = ""
        # Find blocker lines in required_lines
        blocker_candidates = []
        for line in required_lines:
            if "🔴" in line:
                blocker_candidates.append((0, line))
            elif "불만족" in line:
                blocker_candidates.append((1, line))
            elif "REJECTED" in line:
                blocker_candidates.append((2, line))
            elif "Entry Edge" in line:
                blocker_candidates.append((3, line))
            elif "EV Adaptive 기대값" in line:
                blocker_candidates.append((3, line))

        if blocker_candidates:
            # sort by priority
            blocker_candidates.sort(key=lambda x: x[0])
            raw_reason = blocker_candidates[0][1]

        # Clean the reason
        reason_str = ""
        if raw_reason:
            # Remove prefix like 🔴 불만족 5. or 🟢 만족 1.
            cleaned = re.sub(r"^[🟢🔴🟡⚪]\s*(?:만족|불만족|축소|대기)?\s*[0-9]+\.\s*", "", raw_reason)
            # Replace multiple spaces with single space
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            if len(cleaned) > 90:
                cleaned = cleaned[:87] + "..."
            reason_str = cleaned
        elif execution_eligibility is not None and not execution_eligibility.get('can_attempt') and ok:
            # Filter passed but execution is blocked
            blockers = self._format_utbreakout_execution_blockers_for_display(
                side,
                side_lines,
                execution_eligibility,
            )
            cleaned = "; ".join(blockers)
            if len(cleaned) > 90:
                cleaned = cleaned[:87] + "..."
            reason_str = cleaned
        else:
            reason_str = "조건 충족" if ok else "대기 조건"

        return f"{side_label}: {icon_str} | {score_str} | {entry_status} | 이유: {reason_str}"

    @staticmethod
    def _required_gate_lines_for_preview(side_lines: list[str], max_required_gate_lines: int = 7) -> list[str]:
        lines = [str(line) for line in (side_lines or [])]
        if not lines:
            return []
        required_start = -1
        for index, line in enumerate(lines):
            if "필수 게이트" in line:
                required_start = index
                break
        if required_start < 0:
            return lines[:max_required_gate_lines + 2]

        preview_lines = list(lines[:required_start + 1])
        required_count = 0
        for line in lines[required_start + 1:]:
            if "참고/감액" in line:
                break
            preview_lines.append(line)
            required_count += 1
            if required_count >= max_required_gate_lines:
                break
        return preview_lines

    @staticmethod
    def _clean_utbreakout_status_gate_line(line: str) -> str:
        cleaned = re.sub(
            r"^[🟢🔴🟡⚪]\s*(?:만족|불만족|축소|대기)?\s*[0-9]+\.\s*",
            "",
            str(line or ""),
        )
        return re.sub(r"\s+", " ", cleaned).strip()

    def _format_utbreakout_execution_blockers_for_display(
        self,
        side: str,
        side_lines: list[str],
        execution_eligibility: dict | None,
        limit: int = 3,
    ) -> list[str]:
        if not execution_eligibility or execution_eligibility.get('can_attempt'):
            return []
        raw_blockers = [
            str(blocker)
            for blocker in (execution_eligibility.get('blockers') or [])
            if str(blocker or "").strip()
        ]
        required_lines = self._required_gate_lines_for_preview(side_lines)
        display_blockers = []

        def _add(reason):
            reason = re.sub(r"\s+", " ", str(reason or "")).strip()
            if reason and reason not in display_blockers:
                display_blockers.append(reason)

        def _raw_has(*needles):
            lowered_needles = [needle.lower() for needle in needles]
            return any(
                any(needle in blocker.lower() for needle in lowered_needles)
                for blocker in raw_blockers
            )

        direction_line = next(
            (
                line for line in required_lines
                if "UTBot 방향" in line
                and ("🔴" in line or "불만족" in line or "불일치" in line)
            ),
            None,
        )
        has_direction_root_blocker = bool(direction_line or _raw_has("direction mismatch"))
        if has_direction_root_blocker:
            cleaned = self._clean_utbreakout_status_gate_line(direction_line or "")
            detail = cleaned.split(":", 1)[1].strip() if ":" in cleaned else ""
            _add(f"UTBot 방향 불일치: {detail}" if detail else "UTBot 방향 불일치")

        trade_direction_blocker = next(
            (
                blocker
                for blocker in raw_blockers
                if "방향 필터 차단" in blocker
                or "direction filter" in blocker.lower()
            ),
            None,
        )
        if trade_direction_blocker:
            _add(trade_direction_blocker)

        edge_line = next(
            (
                line for line in required_lines
                if "Entry Edge" in line
                and (
                    "?뵶" in line
                    or "BLOCK" in line.upper()
                    or "NO_TRADE" in line.upper()
                    or "<" in line
                )
            ),
            None,
        )
        has_entry_edge_root_blocker = bool(edge_line)
        if edge_line:
            cleaned = self._clean_utbreakout_status_gate_line(edge_line)
            detail = cleaned.split(":", 1)[1].strip() if ":" in cleaned else cleaned
            _add(f"Entry Edge: {detail}")

        ev_line = next(
            (
                line for line in required_lines
                if "EV Adaptive 기대값" in line
                and (
                    "🔴" in line
                    or "불만족" in line
                    or "NO_TRADE" in line.upper()
                    or "<" in line
                    or "stale" in line.lower()
                )
            ),
            None,
        )
        has_ev_root_blocker = bool(ev_line)
        if ev_line:
            cleaned = self._clean_utbreakout_status_gate_line(ev_line)
            detail = cleaned.split(":", 1)[1].strip() if ":" in cleaned else cleaned
            upper_detail = detail.upper()
            if upper_detail.startswith("NO_TRADE"):
                rest = detail[len("NO_TRADE"):].lstrip(" :")
                _add(f"EV Adaptive NO_TRADE: {rest}" if rest else "EV Adaptive NO_TRADE")
            elif "score" in detail.lower() and "<" in detail:
                _add(f"EV Adaptive 점수 미달: {detail}")
            else:
                _add(f"EV Adaptive 기대값: {detail}")

        if _raw_has("not current scanner candidate"):
            _add("현재 scanner/CoinSelector 후보 아님")
        elif _raw_has("coinselector not selected/top candidate"):
            _add("CoinSelector top 후보 아님")

        for blocker in raw_blockers:
            lowered = blocker.lower()
            if "cooldown" in lowered:
                _add(f"쿨다운: {blocker.split(':', 1)[1].strip() if ':' in blocker else blocker}")
        if _raw_has("continuation entry disabled"):
            _add("continuation entry disabled")

        if _raw_has("status screen only; live scanner candidate"):
            _add("상태조회 진단용: 실제 주문은 live scanner 루프가 시도")
        elif _raw_has(
            "manual status only",
            "status screen only; no live scanner candidate",
        ):
            _add("상태조회 진단용: 이 심볼은 현재 live 후보 아님")
        elif _raw_has("not live scanner context"):
            _add("live scanner context 아님")

        if _raw_has("risk plan blocked"):
            _add("risk plan blocked")
        if _raw_has("daily risk limit reached"):
            _add("daily risk limit reached")
        has_side_root_blocker = (
            has_direction_root_blocker
            or has_entry_edge_root_blocker
            or has_ev_root_blocker
        )
        if not has_side_root_blocker:
            if _raw_has("planned quantity zero", "planned risk zero"):
                _add("계획 수량/리스크 0")
            if _raw_has("ready entry plan missing"):
                _add("ready entry plan missing")
        if _raw_has("position already exists"):
            _add("open position guard: same symbol position exists")
        if _raw_has("other symbol position exists"):
            _add("open position guard: other symbol position exists")
        if _raw_has("bridge disabled"):
            _add("bridge disabled")
        if _raw_has("bot is paused"):
            _add("bot paused")
        if _raw_has("exchange", "testnet", "connection", "api"):
            _add("exchange/testnet connection problem")
        if not display_blockers and _raw_has("side condition failed"):
            _add("필수 조건 불만족")
        if not display_blockers:
            display_blockers.extend(raw_blockers[:limit])
        return display_blockers[:limit]

    def _build_utbreakout_active_side_preview_lines(
        self,
        candidate_side: str | None,
        long_lines: list[str],
        short_lines: list[str],
        compact_long: str,
        compact_short: str,
    ) -> list[str]:
        candidate_side = str(candidate_side or "").lower()
        if candidate_side not in {'long', 'short'}:
            return []

        active_lines = short_lines if candidate_side == 'short' else long_lines
        inactive_compact = compact_long if candidate_side == 'short' else compact_short
        return [
            f"활성 후보 상세: {candidate_side.upper()}",
            *self._required_gate_lines_for_preview(active_lines),
            "",
            "비활성 방향 요약",
            inactive_compact,
        ]

    @staticmethod
    def _ordered_utbreakout_side_detail_lines(
        candidate_side: str | None,
        long_lines: list[str],
        short_lines: list[str],
    ) -> list[str]:
        candidate_side = str(candidate_side or "").lower()
        ordered_sections = (
            (short_lines, long_lines)
            if candidate_side == 'short'
            else (long_lines, short_lines)
        )
        detail_lines = []
        for section in ordered_sections:
            if detail_lines:
                detail_lines.append("")
            detail_lines.extend(section or [])
        return detail_lines

    async def _evaluate_utbreakout_eligibility_context(
        self,
        symbol,
        *,
        scanner_source: str | None = None,
        is_live_scanner_context: bool = False,
        is_current_scanner_candidate: bool = False,
        is_coinselector_top_candidate: bool | None = None,
        next_scan_symbol: str | None = None,
        evaluated_symbol: str | None = None,
        manual_status_only: bool = False,
    ):
        if not self.is_upbit_mode():
            symbol = self._canonicalize_utbreakout_symbol_for_use(
                symbol,
                source='status',
            )
        status_micro_result = None
        cfg = self._get_utbot_filtered_breakout_config(self.get_runtime_strategy_params())
        cfg = apply_stable_utbreak_final_overrides(cfg)
        cfg = apply_profit_opportunity_effective_overrides(cfg)
        self.utbreakout_last_status_symbol = symbol
        common_cfg = self.get_runtime_common_settings()
        lev = int(max(1.0, float(common_cfg.get('leverage', 5) or 5)))
        entry_tf = cfg.get('entry_timeframe', '15m')
        htf_tf = cfg.get('htf_timeframe', '1h')

        def _record_status_evaluated(status='OK', **data):
            self._utbreakout_trace_event(
                symbol,
                'STATUS_EVALUATED',
                status,
                effective_profile=cfg.get('effective_profile_version'),
                entry_tf=cfg.get('entry_timeframe', entry_tf),
                htf=cfg.get('htf_timeframe', htf_tf),
                **data,
            )

        _record_status_evaluated(
            'START',
            phase='status_build_started',
        )

        if self.is_upbit_mode():
            ok_market = True
            validation_reason = ''
        else:
            ok_market, symbol, validation_reason = (
                self._ensure_valid_utbreakout_market_symbol(
                    symbol,
                    source='status',
                )
            )
        self.utbreakout_last_status_symbol = symbol
        if not ok_market:
            restricted_symbol = validation_reason.startswith(
                'REJECTED_REGION_RESTRICTED_SYMBOL:'
            )
            _record_status_evaluated(
                'BLOCKED' if restricted_symbol else 'INVALID_MARKET',
                reason=validation_reason,
            )
            return {
                'ok_market': False,
                'restricted_symbol': restricted_symbol,
                'validation_reason': validation_reason,
                'symbol': symbol,
                'cfg': cfg
            }

        def _icon(state):
            if state is True:
                return "🟢"
            if state is False:
                return "🔴"
            return "🟡"

        def _state_label(state):
            if state is True:
                return "만족"
            if state is False:
                return "불만족"
            if state == 'reduced':
                return "축소"
            return "대기"

        def _fmt(value, digits=2):
            try:
                if value is None or not np.isfinite(float(value)):
                    return "n/a"
                return f"{float(value):.{digits}f}"
            except (TypeError, ValueError):
                return "n/a"

        def _fmt_ts(ms):
            try:
                ts = int(ms or 0)
                if ts <= 0:
                    return "n/a"
                return datetime.fromtimestamp(ts / 1000, timezone.utc).astimezone(
                    timezone(timedelta(hours=9))
                ).strftime('%m-%d %H:%M KST')
            except Exception:
                return "n/a"

        def _line(idx, label, state, detail):
            return f"{_icon(state)} {_state_label(state)} {idx}. {label}: {detail}"

        if self.is_upbit_mode():
            _record_status_evaluated(
                'UNSUPPORTED_MODE',
                reason='Upbit spot mode does not use UTBreakout',
            )
            return {
                'ok_market': False,
                'unsupported_mode': True,
                'cfg': cfg
            }

        exchange = getattr(self, 'market_data_exchange', None)
        if exchange and hasattr(exchange, 'fetch_ohlcv'):
            ohlcv = await asyncio.to_thread(
                exchange.fetch_ohlcv,
                symbol,
                entry_tf,
                limit=300
            )
        else:
            now = int(time.time() * 1000)
            ohlcv = []
            for i in range(20):
                ohlcv.append([
                    now - (20 - i) * 60000,
                    100.0, 101.0, 99.0, 100.0, 1000.0
                ])
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        closed = df.iloc[:-1].copy().dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
        if len(closed) < 5:
            _record_status_evaluated(
                'INSUFFICIENT_VALID_DATA',
                rows=len(closed),
            )
            return {
                'ok_market': False,
                'insufficient_data': True,
                'cfg': cfg,
                'reason': f"{symbol} {entry_tf} 유효 데이터 부족"
            }

        adaptive_decision = None
        if bool(cfg.get('adaptive_timeframe_enabled', False)):
            try:
                cfg, df, adaptive_decision = await self._resolve_utbreakout_adaptive_timeframe(symbol, df, cfg)
                cfg = apply_profit_opportunity_effective_overrides(cfg)
                entry_tf = cfg.get('entry_timeframe', entry_tf)
                htf_tf = cfg.get('htf_timeframe', htf_tf)
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
                closed = df.iloc[:-1].copy().dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
                if len(closed) < 5:
                    _record_status_evaluated(
                        'INSUFFICIENT_ADAPTIVE_DATA',
                        rows=len(closed),
                    )
                    return {
                        'ok_market': False,
                        'insufficient_data': True,
                        'cfg': cfg,
                        'reason': f"{symbol} {entry_tf} 유효 데이터 부족"
                    }
            except Exception as e:
                adaptive_decision = {'selected_tf': None, 'decision': 'ERROR', 'reason': str(e), 'top3': []}

        selected_set, auto_analysis, auto_reason = await self._resolve_utbreakout_selected_set(symbol, df, cfg)
        effective_cfg = dict(cfg)
        effective_cfg.update(selected_set.get('params', {}) if isinstance(selected_set, dict) else {})
        effective_cfg['active_set_id'] = selected_set.get('id', cfg.get('active_set_id', UTBREAKOUT_DEFAULT_SET_ID))
        effective_cfg['profile'] = f"set{effective_cfg['active_set_id']}"
        cfg = effective_cfg
        cfg = apply_stable_utbreak_final_overrides(cfg)
        cfg = apply_profit_opportunity_effective_overrides(cfg)
        entry_tf = cfg.get('entry_timeframe', '15m')
        htf_tf = cfg.get('htf_timeframe', '1h')

        decision_ts = int(closed.iloc[-1].get('timestamp') or 0) if len(closed) else 0
        entry_price = float(closed.iloc[-1]['close']) if len(closed) else np.nan

        ut_sig, ut_reason, ut_detail = self._calculate_utbot_signal(
            df,
            self._get_utbot_filtered_breakout_ut_params(cfg)
        )
        ut_detail = ut_detail or {}
        ut_bias_side = str(ut_detail.get('bias_side') or '').lower()
        key = self._utbreakout_trace_key(symbol)
        last_ready_side = self.utbreakout_last_ready_side.get(key) if (getattr(self, 'utbreakout_last_ready_side', None) and isinstance(self.utbreakout_last_ready_side, dict)) else None
        candidate_side = ut_sig if ut_sig in {'long', 'short'} else ut_bias_side if ut_bias_side in {'long', 'short'} else last_ready_side if last_ready_side in {'long', 'short'} else None
        candidate_type = 'fresh_signal' if ut_sig in {'long', 'short'} else 'bias_state' if (ut_bias_side in {'long', 'short'}) else 'ready_fallback' if candidate_side else 'waiting'

        metrics = self._calculate_utbreakout_timeframe_metrics(closed, cfg)
        ema_fast_len = int(cfg.get('ema_fast', 50) or 50)
        ema_slow_len = int(cfg.get('ema_slow', 200) or 200)
        htf_ready = False
        htf_error = None
        htf_close = htf_ema_fast = htf_ema_slow = htf_gap_pct = np.nan
        htf_supertrend_direction = None
        htf_supertrend_reason = None
        try:
            exchange = getattr(self, 'market_data_exchange', None)
            if exchange and hasattr(exchange, 'fetch_ohlcv'):
                htf_ohlcv = await asyncio.to_thread(
                    exchange.fetch_ohlcv,
                    symbol,
                    htf_tf,
                    limit=300
                )
            else:
                now = int(time.time() * 1000)
                htf_ohlcv = []
                for i in range(250):
                    htf_ohlcv.append([
                        now - (250 - i) * 3600000,
                        100.0, 101.0, 99.0, 100.0, 1000.0
                    ])
            htf_df = pd.DataFrame(htf_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            for col in ['open', 'high', 'low', 'close', 'volume']:
                htf_df[col] = pd.to_numeric(htf_df[col], errors='coerce')
            htf_closed = htf_df.iloc[:-1].dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
            if len(htf_closed) >= ema_slow_len + 2:
                htf_close_series = htf_closed['close'].astype(float)
                htf_ema_fast = float(htf_close_series.ewm(span=ema_fast_len, adjust=False).mean().iloc[-1])
                htf_ema_slow = float(htf_close_series.ewm(span=ema_slow_len, adjust=False).mean().iloc[-1])
                htf_close = float(htf_close_series.iloc[-1])
                htf_gap_pct = abs(htf_ema_fast - htf_ema_slow) / max(abs(htf_close), 1e-9) * 100.0
                htf_ready = True
            else:
                htf_error = f"데이터 부족 {len(htf_closed)}/{ema_slow_len + 2}"
            htf_supertrend_direction, _, htf_supertrend_reason = self._calculate_utbot_filter_pack_supertrend_direction(
                htf_closed,
                length=10,
                multiplier=3.0
            )
        except Exception as e:
            htf_error = str(e)

        futures_context = {}
        try:
            futures_context = (
                (auto_analysis or {}).get('futures_context')
                if isinstance(auto_analysis, dict) and isinstance((auto_analysis or {}).get('futures_context'), dict)
                else None
            )
            if futures_context is None:
                futures_context = await self._fetch_utbreakout_futures_context(symbol)
            futures_context = dict(futures_context or {})
        except Exception as e:
            futures_context = {'futures_context_error': str(e)}
        market_regime_context = {}
        try:
            market_regime_context = await self._fetch_utbreakout_market_regime_context(cfg)
            market_regime_context = dict(market_regime_context or {})
        except Exception as e:
            market_regime_context = {'error': str(e)}

        try:
            status_atr_series = self._calculate_wilder_atr_series(closed, int(cfg.get('atr_length', 14) or 14))
        except Exception:
            status_atr_series = None

        def _market_quality_filter_values():
            def _candidate_signal_age_candles():
                try:
                    signal_ts = float(ut_detail.get('signal_ts') or 0.0)
                    timeframe_ms = float(
                        self._timeframe_to_ms(entry_tf) or (15 * 60 * 1000)
                    )
                    if signal_ts > 0 and decision_ts >= signal_ts:
                        return (float(decision_ts) - signal_ts) / max(timeframe_ms, 1.0)
                    if candidate_type == 'fresh_signal':
                        return 0.0
                except (TypeError, ValueError):
                    if candidate_type == 'fresh_signal':
                        return 0.0
                return None

            values = {
                'entry_price': entry_price,
                'open': metrics.get('open'),
                'high': metrics.get('high'),
                'low': metrics.get('low'),
                'close': metrics.get('close'),
                'rsi': metrics.get('rsi'),
                'macd_hist': metrics.get('macd_hist'),
                'macd_hist_prev': metrics.get('macd_hist_prev'),
                'roc_pct': metrics.get('roc_pct'),
                'cci': metrics.get('cci'),
                'stoch_k': metrics.get('stoch_k'),
                'stoch_d': metrics.get('stoch_d'),
                'atr': metrics.get('atr'),
                'atr_pct': metrics.get('atr_pct'),
                'adx': metrics.get('adx'),
                'plus_di': metrics.get('plus_di'),
                'minus_di': metrics.get('minus_di'),
                'adx_reason': metrics.get('adx_reason'),
                'chop': metrics.get('chop'),
                'chop_reason': metrics.get('chop_reason'),
                'ema50': metrics.get('ema_fast'),
                'ema50_prev': metrics.get('ema_fast_prev'),
                'ema200': metrics.get('ema_slow'),
                'donchian_high_prev': metrics.get('donchian_high_prev'),
                'donchian_low_prev': metrics.get('donchian_low_prev'),
                'donchian_width_pct': metrics.get('donchian_width_pct'),
                'bb_upper': metrics.get('bb_upper'),
                'bb_lower': metrics.get('bb_lower'),
                'bb_mid': metrics.get('bb_mid'),
                'bb_width_pct': metrics.get('bb_width_pct'),
                'bb_width_prev_pct': metrics.get('bb_width_prev_pct'),
                'bb_width_min_pct': metrics.get('bb_width_min_pct'),
                'bb_width_percentile': metrics.get('bb_width_percentile'),
                'keltner_upper': metrics.get('keltner_upper'),
                'keltner_lower': metrics.get('keltner_lower'),
                'keltner_mid': metrics.get('keltner_mid'),
                'keltner_width_pct': metrics.get('keltner_width_pct'),
                'keltner_width_prev_pct': metrics.get('keltner_width_prev_pct'),
                'keltner_squeeze_on': metrics.get('keltner_squeeze_on'),
                'range_expansion_ratio': metrics.get('range_expansion_ratio'),
                'range_compression_ratio': metrics.get('range_compression_ratio'),
                'squeeze_release_state': metrics.get('squeeze_release_state'),
                'volume_ratio': metrics.get('volume_ratio'),
                'obv_slope_ratio': metrics.get('obv_slope_ratio'),
                'mfi': metrics.get('mfi'),
                'vwap': metrics.get('vwap'),
                'vwap_slope': metrics.get('vwap_slope'),
                'vwap_reason': metrics.get('vwap_reason'),
                'psar_direction': metrics.get('psar_direction'),
                'psar': metrics.get('psar'),
                'psar_reason': metrics.get('psar_reason'),
                'ichimoku_bias': metrics.get('ichimoku_bias'),
                'ichimoku_top': metrics.get('ichimoku_top'),
                'ichimoku_bottom': metrics.get('ichimoku_bottom'),
                'vortex_plus': metrics.get('vortex_plus'),
                'vortex_minus': metrics.get('vortex_minus'),
                'vortex_reason': metrics.get('vortex_reason'),
                'aroon_up': metrics.get('aroon_up'),
                'aroon_down': metrics.get('aroon_down'),
                'aroon_reason': metrics.get('aroon_reason'),
                'session_hour_kst': metrics.get('session_hour_kst'),
                'auto_scores': (auto_analysis or {}).get('scores') if isinstance(auto_analysis, dict) else {},
                'mtf_metrics': (auto_analysis or {}).get('timeframes') if isinstance(auto_analysis, dict) else {},
                'htf_ready': htf_ready,
                'htf_error': htf_error,
                'htf_close': htf_close,
                'htf_ema_fast': htf_ema_fast,
                'htf_ema_slow': htf_ema_slow,
                'htf_gap_pct': htf_gap_pct,
                'htf_supertrend_direction': htf_supertrend_direction,
                'htf_supertrend_reason': htf_supertrend_reason,
                'funding_rate': futures_context.get('funding_rate'),
                'next_funding_time': futures_context.get('next_funding_time'),
                'open_interest_delta_pct': futures_context.get('open_interest_delta_pct'),
                'open_interest_delta_z': futures_context.get('open_interest_delta_z'),
                'open_interest_acceleration': futures_context.get('open_interest_acceleration'),
                'open_interest_hist_samples': futures_context.get('open_interest_hist_samples'),
                'taker_buy_sell_ratio': futures_context.get('taker_buy_sell_ratio'),
                'long_short_ratio': futures_context.get('long_short_ratio'),
                'orderbook_imbalance_pct': futures_context.get('orderbook_imbalance_pct'),
                'rolling_orderbook_imbalance_pct': futures_context.get('rolling_orderbook_imbalance_pct'),
                'rolling_orderbook_imbalance_delta': futures_context.get('rolling_orderbook_imbalance_delta'),
                'rolling_ofi_score': futures_context.get('rolling_ofi_score'),
                'rolling_ofi_samples': futures_context.get('rolling_ofi_samples'),
                'futures_spread_pct': futures_context.get('futures_spread_pct'),
                'bid_depth_usdt': futures_context.get('bid_depth_usdt'),
                'ask_depth_usdt': futures_context.get('ask_depth_usdt'),
                'basis_pct': futures_context.get('basis_pct'),
                'market_regime_context': market_regime_context,
            }
            signal_age = _candidate_signal_age_candles()
            if signal_age is not None:
                values['signal_age_candles'] = signal_age
            values = self._enrich_utbreakout_trend_health_values(values, closed, cfg, status_atr_series)
            return self._enrich_utbreakout_strategy_quality_values(values, closed, cfg)

        db = getattr(self, 'db', None)
        if db and hasattr(db, 'get_daily_stats'):
            daily_count, daily_pnl = db.get_daily_stats()
            daily_entries = db.get_daily_entry_count()
            max_losses = int(cfg.get('max_consecutive_losses', 3) or 3)
            recent_pnls = self._get_recent_strategy_closed_trade_pnls(
                {
                    ENTRY_STRATEGY_UT_BREAKOUT,
                    UTBOT_FILTERED_BREAKOUT_STRATEGY,
                    UTBOT_ADAPTIVE_TIMEFRAME_STRATEGY,
                },
                max_losses,
                today_only=True,
            )
        else:
            daily_count, daily_pnl = 0, 0.0
            daily_entries = 0
            max_losses = int(cfg.get('max_consecutive_losses', 3) or 3)
            recent_pnls = []
        daily_ok = True
        daily_detail = f"PnL {_fmt(daily_pnl, 2)} / trades {daily_entries}/{int(cfg['max_daily_trades'])}"
        if float(cfg.get('daily_max_loss_usdt', 0) or 0) > 0 and float(daily_pnl or 0) <= -float(cfg['daily_max_loss_usdt']):
            daily_ok = False
            daily_detail = f"일손실 한도 도달 PnL {_fmt(daily_pnl, 2)}"
        elif int(cfg.get('max_daily_trades', 0) or 0) > 0 and daily_entries >= int(cfg['max_daily_trades']):
            daily_ok = False
            daily_detail = f"일일 거래수 한도 {daily_entries}/{int(cfg['max_daily_trades'])}"
        elif bool(cfg.get('daily_profit_target_enabled', False)) and float(daily_pnl or 0) >= float(cfg.get('daily_profit_target_usdt', 0) or 0):
            daily_ok = False
            daily_detail = f"일 목표수익 도달 {_fmt(daily_pnl, 2)}"
        elif len(recent_pnls) >= max_losses and all(float(pnl) < 0 for pnl in recent_pnls[:max_losses]):
            daily_ok = False
            daily_detail = f"연속 손절 {max_losses}회"

        balance_detail = "잔고 조회 대기"
        entry_plan_detail = "진입 계획: ATR/잔고 계산 대기"
        take_profit_detail = "익절 계획: ATR/잔고 계산 대기"
        risk_ok = None
        risk_distance = np.nan
        risk_distance_pct = np.nan
        risk_usdt = np.nan
        planned_qty = np.nan
        planned_notional = np.nan
        planned_margin = np.nan
        take_profit_distance = np.nan
        take_profit_pct = np.nan
        expected_profit_usdt = np.nan
        status_micro_result = None
        atr_value = metrics.get('atr')
        atr_pct = metrics.get('atr_pct')
        if self._is_valid_number(atr_value):
            try:
                total_balance, free_balance, _ = await self.get_balance_info()
                balance_for_risk = total_balance if total_balance > 0 else free_balance
                side_for_plan = candidate_side if candidate_side in {'long', 'short'} else 'long'
                risk_budget = resolve_utbreakout_risk_budget(
                    balance_for_risk,
                    cfg,
                    daily_pnl_usdt=daily_pnl,
                )
                risk_per_trade_percent = risk_budget['risk_per_trade_percent']
                max_risk_per_trade_usdt = risk_budget['max_risk_per_trade_usdt']
                risk_note = ""
                ev_plan_enabled = bool(cfg.get('ev_adaptive_enabled', False))
                if (
                    not ev_plan_enabled
                    and side_for_plan == 'short'
                    and bool(cfg.get('short_conservative_enabled', True))
                ):
                    short_risk_multiplier = min(1.0, max(0.0, float(cfg.get('short_risk_multiplier', 0.5) or 0.5)))
                    risk_per_trade_percent *= short_risk_multiplier
                    max_risk_per_trade_usdt *= short_risk_multiplier
                    risk_note = f" / 숏 리스크 x{short_risk_multiplier:.2f}"
                market_quality_for_plan = self._evaluate_utbreakout_market_quality(
                    side_for_plan,
                    cfg,
                    _market_quality_filter_values()
                )
                market_quality_multiplier = min(
                    1.0,
                    max(0.0, float(market_quality_for_plan.get('risk_multiplier', 1.0) or 0.0))
                )
                if market_quality_for_plan.get('state') is False:
                    risk_note += " / 시장품질 BLOCK"
                elif not ev_plan_enabled and market_quality_multiplier < 0.999:
                    risk_per_trade_percent *= market_quality_multiplier
                    max_risk_per_trade_usdt *= market_quality_multiplier
                    risk_note += f" / 시장품질 x{market_quality_multiplier:.2f}"
                elif market_quality_multiplier < 0.999:
                    risk_note += f" / 시장품질 x{market_quality_multiplier:.2f}"
                adaptation_for_plan = self._build_utbreakout_strategy_adaptation(
                    symbol,
                    side_for_plan,
                    cfg,
                    selected_set,
                    _market_quality_filter_values()
                )
                adaptation_multiplier = min(
                    1.0,
                    max(0.0, float(adaptation_for_plan.get('risk_multiplier', 1.0) or 0.0))
                )
                trend_health_for_plan = adaptation_for_plan.get('trend_health') if isinstance(adaptation_for_plan.get('trend_health'), dict) else {}
                if not ev_plan_enabled and adaptation_multiplier < 0.999:
                    risk_per_trade_percent *= adaptation_multiplier
                    max_risk_per_trade_usdt *= adaptation_multiplier
                    risk_note += f" / 적응 x{adaptation_multiplier:.2f}"
                if trend_health_for_plan.get('state') is False:
                    risk_note += " / 추세건강 BLOCK"
                plan_cfg = dict(cfg)
                if ev_plan_enabled:
                    ev_plan_values = _market_quality_filter_values()
                    ev_plan_decision = evaluate_ev_adaptive_entry(
                        side=side_for_plan,
                        candidate_type=(
                            candidate_type
                            if candidate_side == side_for_plan
                            else 'waiting'
                        ),
                        values=ev_plan_values,
                        config=_ev_adaptive_runtime_config(cfg),
                    )
                    plan_cfg = _apply_ev_exit_profile(
                        plan_cfg,
                        ev_plan_decision.exit_profile,
                    )
                    volatility_multiplier = min(
                        1.0,
                        max(
                            0.0,
                            float(
                                adaptation_for_plan.get(
                                    'volatility_risk_multiplier',
                                    1.0,
                                )
                                or 0.0
                            ),
                        ),
                    )
                    ev_plan_multiplier = min(
                        float(ev_plan_decision.risk_multiplier),
                        market_quality_multiplier,
                        volatility_multiplier,
                    )
                    risk_per_trade_percent *= ev_plan_multiplier
                    max_risk_per_trade_usdt *= ev_plan_multiplier

                    if ev_plan_multiplier <= 0:
                        ev_plan_blockers = "; ".join(ev_plan_decision.blockers[:3]) or "unknown EV plan blocker"
                        risk_note += (
                            f" / EV {ev_plan_decision.mode} x0.00 "
                            f"BLOCK: {ev_plan_blockers}"
                        )
                    else:
                        risk_note += (
                            f" / EV {ev_plan_decision.mode} "
                            f"x{ev_plan_multiplier:.2f}"
                        )
                plan_cfg['take_profit_r_multiple'] = max(
                    float(plan_cfg.get('take_profit_r_multiple', 2.40) or 2.40),
                    float(plan_cfg.get('second_take_profit_r_multiple', 2.40) or 2.40),
                )
                plan = calculate_risk_plan(
                    side=side_for_plan,
                    entry_price=entry_price,
                    atr_value=atr_value,
                    stop_atr_multiplier=plan_cfg.get('stop_atr_multiplier', 1.5),
                    ut_stop=ut_detail.get('curr_stop'),
                    take_profit_r_multiple=plan_cfg.get('take_profit_r_multiple', 2.40),
                    min_risk_reward=plan_cfg.get('min_risk_reward', 2.0),
                    balance_usdt=balance_for_risk,
                    risk_per_trade_percent=risk_per_trade_percent,
                    max_risk_per_trade_usdt=max_risk_per_trade_usdt,
                    leverage=lev,
                )
                plan = cap_utbreakout_risk_plan_to_margin(
                    plan,
                    free_balance=free_balance,
                    leverage=lev,
                    entry_price=entry_price,
                )
                if self._micro_auto_enabled():
                    status_micro_cfg = self._get_micro_auto_config()
                    if ev_plan_enabled:
                        status_micro_cfg = dict(status_micro_cfg)
                        status_micro_cfg['risk_per_trade_pct'] = (
                            float(status_micro_cfg.get('risk_per_trade_pct', 0.0) or 0.0)
                            * ev_plan_multiplier
                        )
                        status_micro_cfg['max_risk_usdt'] = (
                            float(status_micro_cfg.get('max_risk_usdt', 0.0) or 0.0)
                            * ev_plan_multiplier
                        )
                    status_micro_result = build_micro_entry_plan(
                        side=side_for_plan,
                        entry_price=entry_price,
                        atr_value=atr_value,
                        ut_stop=ut_detail.get('curr_stop'),
                        base_plan=dict(
                            plan,
                            stop_atr_multiplier=plan_cfg.get('stop_atr_multiplier', 1.5),
                        ),
                        cfg=status_micro_cfg,
                        selected_set=selected_set,
                        auto_scores=(
                            (auto_analysis or {}).get('scores')
                            if isinstance(auto_analysis, dict)
                            else {}
                        ),
                        selected_timeframe=entry_tf,
                        total_equity_usdt=total_balance,
                        free_usdt=free_balance,
                        min_notional_usdt=await self._get_symbol_min_notional(symbol),
                        max_symbol_leverage=status_micro_cfg.get('max_leverage', 10),
                    )
                    if status_micro_result.get('accepted'):
                        plan = dict(plan)
                        plan.update(status_micro_result)
                        risk_note += (
                            f" / Micro {int(status_micro_result.get('leverage', lev) or lev)}x"
                        )
                    else:
                        risk_note += (
                            f" / Micro BLOCK {status_micro_result.get('reject_code')}"
                        )
                risk_distance = plan['risk_distance']
                risk_distance_pct = plan['risk_distance_pct']
                rr_multiple = plan['rr_multiple']
                take_profit_distance = plan['take_profit_distance']
                take_profit_pct = plan['take_profit_pct']
                risk_usdt = plan['risk_usdt']
                planned_qty = plan['qty']
                planned_notional = plan['planned_notional']
                planned_margin = plan['planned_margin']
                expected_profit_usdt = plan['expected_profit_usdt']
                risk_ok = (
                    bool(
                        status_micro_result.get('accepted')
                        and not status_micro_result.get('dry_run', True)
                        and status_micro_result.get('live_enabled', False)
                    )
                    if isinstance(status_micro_result, dict)
                    else True
                )
                balance_detail = (
                    f"손실한도 {_fmt(risk_usdt, 2)} USDT / 손절거리 {_fmt(risk_distance, 4)} "
                    f"({_fmt(risk_distance_pct, 3)}%) / qty {_fmt(planned_qty, 6)}{risk_note}"
                )
                entry_plan_detail = (
                    f"진입 계획: 증거금 {_fmt(planned_margin, 2)} USDT / "
                    f"포지션 {_fmt(planned_notional, 2)} USDT / 레버리지 {lev}x / "
                    f"손절시 손실 {_fmt(risk_usdt, 2)} USDT"
                )
                tp1_r = float(plan_cfg.get('partial_take_profit_r_multiple', 1.00) or 1.00)
                tp2_r = float(plan_cfg.get('second_take_profit_r_multiple', 2.40) or 2.40)
                tp1_ratio = float(plan_cfg.get('partial_take_profit_ratio', 0.30) or 0.30)
                tp2_ratio = float(plan_cfg.get('second_take_profit_ratio', 0.40) or 0.40)
                take_profit_detail = (
                    f"익절 계획: TP1 {tp1_r:.2f}R({tp1_ratio:.0%}) / "
                    f"TP2 {tp2_r:.2f}R({tp2_ratio:.0%}) / "
                    f"익절거리 {take_profit_distance:.4f} ({take_profit_pct:.3f}%) / "
                    f"예상수익 {expected_profit_usdt:.2f} USDT"
                )
            except ValueError as e:
                reason = str(e)
                if "risk budget unavailable" in reason:
                    multiplier_note = ""
                    if ev_plan_enabled:
                        multiplier_note = f" (effective x{ev_plan_multiplier:.3f})"
                    entry_plan_detail = f"진입 계획: 일손실 한도 초과로 진입 제한{multiplier_note}"
                    risk_ok = False
                else:
                    entry_plan_detail = f"진입 계획: 수량 계산 실패 ({reason})"
                    risk_ok = False
        else:
            balance_detail = "진입 계획: ATR 지표 없음"
            risk_ok = False

        opposite_set_exit_detail = (
            f"반대Set청산: {'ON' if cfg.get('opposite_set_exit_enabled') else 'OFF'} | "
            f"조건: 반대 UT 신규신호 + 선택 Set 조건 통과 + 최소 "
            f"{int(float(cfg.get('opposite_set_exit_min_hold_candles', 3) or 0))}봉 보유 + "
            f"{'PnL≥$' + format(float(cfg.get('opposite_set_exit_min_pnl_usdt', 0.0) or 0.0), '.2f') if cfg.get('opposite_set_exit_min_pnl_enabled') else 'PnL조건 OFF'} | "
            "동작: 현재 포지션 청산만, 반대 신규진입 없음"
        )

        # side conditions
        async def _side_conditions(side):
            side_status_summary = {}
            side_upper = side.upper()
            if candidate_side == side:
                ut_state = True
                ut_detail_text = f"{side_upper} {candidate_type}"
            else:
                ut_state = False if candidate_side in {'long', 'short'} else None
                ut_detail_text = f"현재 {str(candidate_side or 'none').upper()} / bias {str(ut_bias_side or 'none').upper()}"

            filter_values = _market_quality_filter_values()
            feature_score = self._calculate_utbreakout_feature_score(side, cfg, filter_values)
            filter_values['feature_score'] = feature_score
            selected_items = self._evaluate_utbreakout_set_filter_items(side, selected_set, cfg, filter_values)
            failed_set_items = [item for item in selected_items if item.get('state') is not True]
            set_hard_failures = []
            set_soft_failures = []
            set_item_multipliers = []
            for item in failed_set_items:
                name = item.get('name') or ''
                detail = item.get('detail') or ''
                code = item.get('code') or ''
                is_core_set_failure = (
                    bool(cfg.get('selected_set_core_filter_hard_block_enabled', True))
                    and (
                        name in UTBREAKOUT_CORE_SET_FILTER_HARD_NAMES
                        or code in UTBREAKOUT_CORE_SET_FILTER_HARD_CODES
                        or bool(item.get('core_set_hard_block'))
                    )
                )
                if is_core_set_failure or name in {'ATR% 변동성', '손익비', '스프레드', '유동성'} or code == 'REJECTED_RISK_REWARD_LOW':
                    state, multiplier, classified_detail = False, 0.0, detail
                elif name in {'상대 거래량', 'Rolling OFI 확인', 'Futures 수급 불균형', 'Spread/Depth 비용'}:
                    state, multiplier, classified_detail = _classify_set_filter_result(
                        side,
                        False,
                        detail,
                        metrics=filter_values,
                        cfg=cfg,
                    )
                else:
                    state = 'reduced'
                    multiplier = 0.65 if item.get('state') is None else 0.50
                    classified_detail = f"{detail}; set confirmation soft fail"
                classified = dict(item)
                classified.update({
                    'state': state,
                    'risk_multiplier': multiplier,
                    'detail': classified_detail,
                })
                if state is False:
                    set_hard_failures.append(classified)
                else:
                    set_soft_failures.append(classified)
                    set_item_multipliers.append(float(multiplier))

            if set_hard_failures:
                set_state = False
                set_detail = "; ".join(
                    f"{item.get('name')}: {item.get('detail')}"
                    for item in set_hard_failures[:3]
                )
            elif set_soft_failures:
                set_state = 'reduced'
                set_detail = "; ".join(
                    f"{item.get('name')}: {item.get('detail')}"
                    for item in set_soft_failures[:3]
                )
            elif selected_items:
                set_state = True
                set_detail = "모든 필수 필터 만족"
            else:
                set_state = None
                set_detail = "필터 미등록 또는 조회 실패"

            db = getattr(self, 'db', None)
            if db and hasattr(db, 'get_coin_selector_symbol_quality'):
                selector_quality = db.get_coin_selector_symbol_quality(symbol)
            else:
                selector_quality = {}
            selector_quality = dict(selector_quality or {})
            selector_state = selector_quality.get('state')
            if selector_state is None:
                selector_state = True

            adaptation = self._build_utbreakout_strategy_adaptation(
                symbol,
                side,
                cfg,
                selected_set,
                filter_values
            )
            adaptation_state = adaptation.get('state')

            market_quality = self._evaluate_utbreakout_market_quality(side, cfg, filter_values)
            market_multiplier = min(1.0, max(0.0, float(market_quality.get('risk_multiplier', 1.0) or 1.0)))

            quality_score_v2 = self._build_utbreakout_quality_score_v2(
                side,
                cfg,
                selected_set,
                filter_values,
                market_quality=market_quality,
                selector_quality=selector_quality
            )

            bias_multiplier_status = 1.0
            selector_multiplier = min(1.0, max(0.0, float(selector_quality.get('risk_multiplier', 1.0) or 1.0)))
            adaptation_multiplier = min(1.0, max(0.0, float(adaptation.get('risk_multiplier', 1.0) or 1.0)))
            q2_multiplier = min(1.0, max(0.0, float(quality_score_v2.get('risk_multiplier', 1.0) or 1.0)))

            core_items = [
                ("UTBot 시그널 방향", ut_state, ut_detail_text),
                ("Set 필터 세부조건", set_state, set_detail),
            ]

            ev_status_decision = None
            ev_status_net = None
            ev_status_exit = None
            if bool(cfg.get('ev_adaptive_enabled', False)):
                ev_status_decision = evaluate_ev_adaptive_entry(
                    side=side,
                    candidate_type=(
                        candidate_type
                        if candidate_side == side
                        else 'waiting'
                    ),
                    values=filter_values,
                    config=_ev_adaptive_runtime_config(cfg),
                )
                if not ev_status_decision.allowed:
                    ev_state = False
                    ev_detail = (
                        f"{ev_status_decision.mode} score {ev_status_decision.score:.1f}: "
                        f"{'; '.join(ev_status_decision.blockers[:4])}"
                    )
                elif (
                    self._is_valid_number(planned_qty)
                    and self._is_valid_number(risk_usdt)
                    and self._is_valid_number(planned_notional)
                ):
                    ev_status_exit = adapt_exit_for_quantity(
                        ev_status_decision.exit_profile,
                        total_qty=planned_qty,
                        min_amount=self._get_min_amount_for_symbol(symbol),
                    )
                    ev_status_net = evaluate_net_edge(
                        risk_usdt=risk_usdt,
                        planned_notional=planned_notional,
                        win_probability=ev_status_decision.win_probability,
                        gross_win_r=profile_gross_win_r(ev_status_exit.profile),
                        config=_ev_adaptive_runtime_config(cfg),
                    )
                    ev_state = bool(
                        ev_status_exit.executable and ev_status_net.allowed
                    )
                    ev_detail = (
                        f"{ev_status_decision.mode} score {ev_status_decision.score:.1f}, "
                        f"p={ev_status_decision.win_probability:.2f}, "
                        f"MTF={ev_status_decision.mtf_alignment}, "
                        f"leader={ev_status_decision.leadership_score:.0f}, "
                        f"exit={ev_status_exit.mode}, "
                        f"net={ev_status_net.expected_net_r:.3f}R "
                        f"(cost {ev_status_net.cost_r:.3f}R)"
                    )
                else:
                    ev_state = None
                    ev_detail = (
                        f"{ev_status_decision.mode} score {ev_status_decision.score:.1f}; "
                        "수량/비용 계산 대기"
                    )
                side_status_summary['ev_adaptive_status_summary'] = ev_detail

            entry_edge_status_decision = self._append_entry_edge_status_item(
                core_items,
                side=side,
                cfg=cfg,
                filter_values=filter_values,
                market_regime_context=market_regime_context,
                selector_quality=selector_quality,
                quality_score_v2=quality_score_v2,
                adaptation=adaptation,
                ev_status_decision=ev_status_decision,
                ev_status_net=ev_status_net,
                ev_status_exit=ev_status_exit,
            )
            if entry_edge_status_decision is not None:
                side_status_summary['entry_edge_status_summary'] = entry_edge_status_decision.summary

            set_filter_multiplier_status = 1.0
            if set_hard_failures:
                set_filter_multiplier_status = 0.0
            elif set_soft_failures and bool(cfg.get('set_filter_soft_fail_enabled', True)):
                configured_set_multiplier = (
                    float(cfg.get('set_filter_multi_soft_fail_multiplier', 0.50) or 0.50)
                    if len(set_soft_failures) >= 2
                    else float(cfg.get('set_filter_soft_fail_multiplier', 0.70) or 0.70)
                )
                set_filter_multiplier_status = min(
                    [configured_set_multiplier, *set_item_multipliers]
                )
            elif set_soft_failures:
                set_filter_multiplier_status = 0.0

            if ev_status_decision is not None:
                volatility_multiplier = min(
                    1.0,
                    max(
                        0.0,
                        float(adaptation.get('volatility_risk_multiplier', 1.0) or 0.0),
                    ),
                )
                raw_final_risk_multiplier_status = min(
                    (
                        min(
                            1.0,
                            max(0.0, float(entry_edge_status_decision.risk_multiplier)),
                        )
                        if entry_edge_status_decision is not None
                        else float(ev_status_decision.risk_multiplier)
                    ),
                    market_multiplier,
                    volatility_multiplier,
                )
            else:
                raw_final_risk_multiplier_status = (
                    bias_multiplier_status
                    * market_multiplier
                    * selector_multiplier
                    * adaptation_multiplier
                    * q2_multiplier
                    * set_filter_multiplier_status
                    * (
                        min(
                            1.0,
                            max(0.0, float(entry_edge_status_decision.risk_multiplier)),
                        )
                        if entry_edge_status_decision is not None
                        else 1.0
                    )
                )
            raw_before_position_sizing_status = max(
                0.0,
                min(1.0, float(raw_final_risk_multiplier_status)),
            )
            position_sizing_status = {
                'risk_multiplier': 1.0,
                'blocked': False,
                'components': {},
                'reasons': [],
                'kelly_reason': 'disabled',
            }
            if bool(cfg.get('position_sizing_engine_enabled', True)):
                try:
                    position_sizing_status = build_position_risk_multiplier(
                        {
                            'atr_pct': filter_values.get('atr_pct'),
                            'meta_probability': 0.65,
                            'entry_edge_probability': (
                                entry_edge_status_decision.probability
                                if entry_edge_status_decision is not None
                                else (
                                    ev_status_decision.win_probability
                                    if ev_status_decision is not None
                                    else None
                                )
                            ),
                            'entry_edge_score': (
                                entry_edge_status_decision.score
                                if entry_edge_status_decision is not None
                                else None
                            ),
                            'direction_score': (
                                entry_edge_status_decision.direction_score
                                if entry_edge_status_decision is not None
                                else None
                            ),
                            'recent_closed_pnls': recent_pnls,
                            'daily_loss_limit_hit': daily_ok is False,
                            'liquidity_ok': market_quality.get('state') is not False,
                            'spread_ok': market_quality.get('hard_block') is not True,
                        },
                        cfg,
                    )
                    raw_final_risk_multiplier_status = min(
                        raw_before_position_sizing_status,
                        min(1.0, max(0.0, float(position_sizing_status.get('risk_multiplier', 1.0) or 0.0))),
                    )
                except Exception as exc:
                    logger.warning("UTBreakout status position sizing failed for %s: %s", symbol, exc)
                    raw_final_risk_multiplier_status = raw_before_position_sizing_status
            final_risk_multiplier_status = (
                0.0
                if bool(position_sizing_status.get('blocked'))
                else _apply_utbreakout_risk_multiplier_floor(raw_final_risk_multiplier_status, cfg)
            )

            base_max_risk = float(cfg.get('max_risk_per_trade_usdt', 1.0) or 1.0)
            effective_max_risk = base_max_risk * final_risk_multiplier_status
            entry_quality_state, entry_quality_detail = (
                self._evaluate_utbreakout_entry_quality_gate(
                    side,
                    cfg,
                    final_risk_multiplier=final_risk_multiplier_status,
                    market_quality=market_quality,
                    ev_decision=ev_status_decision,
                    ev_net=ev_status_net,
                    ev_exit=ev_status_exit,
                    entry_edge_decision=entry_edge_status_decision,
                )
            )
            core_items.append(("Entry Quality Gate", entry_quality_state, entry_quality_detail))
            core_items.extend([
                ("일일 리스크", daily_ok, daily_detail),
                ("ATR 손절/RR/수량", risk_ok, balance_detail),
            ])
            advisory_items = [
                (
                    "최종 누적 리스크",
                    'reduced' if final_risk_multiplier_status < 0.999 else True,
                    (
                        f"x{final_risk_multiplier_status:.3f} "
                        f"(base ${base_max_risk:.2f} -> effective ${effective_max_risk:.2f}; "
                        f"raw x{float(raw_before_position_sizing_status):.3f}, "
                        f"sizing x{float(position_sizing_status.get('risk_multiplier', 1.0) or 0.0):.2f}, "
                        f"bias x{bias_multiplier_status:.2f}, market x{market_multiplier:.2f}, "
                        f"selector x{selector_multiplier:.2f}, "
                        f"strategy x{adaptation_multiplier:.2f}, qscore x{q2_multiplier:.2f}, "
                        f"set x{set_filter_multiplier_status:.2f})"
                    ),
                ),
                ("코인 선택 품질", selector_state, selector_quality.get('summary')),
                ("Feature Score", True, f"{feature_score.get('score', 0):.1f} / {feature_score.get('reason')}"),
                (
                    "전략 적응 요약",
                    'reduced' if adaptation_multiplier < 0.999 else True,
                    adaptation.get('summary'),
                ),
                (
                    "레거시 통합 품질(참고)",
                    (
                        False
                        if adaptation_state is False or quality_score_v2.get('state') is False
                        else 'reduced'
                        if adaptation_state == 'reduced' or quality_score_v2.get('state') == 'reduced'
                        else True
                    ),
                    (
                        f"trend/strategy={adaptation.get('summary')}; "
                        f"qscore={quality_score_v2.get('summary')}"
                    ),
                ),
            ]
            ok = all(item[1] is True or item[1] == 'reduced' for item in core_items)
            lines = [
                f"{side_upper}: {'진입 가능' if ok else '대기'}",
                "필수 게이트",
            ]
            lines.extend(_line(idx, label, state, detail) for idx, (label, state, detail) in enumerate(core_items, 1))
            lines.append("참고/감액")
            lines.extend(
                f"{_icon(state)} {_state_label(state)} - {label}: {detail}"
                for label, state, detail in advisory_items
            )
            return ok, lines

        long_ok, long_lines = await _side_conditions('long')
        short_ok, short_lines = await _side_conditions('short')

        sl_lockout_active, sl_lockout_reason = self._is_utbreakout_daily_sl_locked(symbol)
        if sl_lockout_active:
            long_ok = False
            short_ok = False
            sl_lockout_reason = sl_lockout_reason or "daily SL lockout active"
            lockout_msg = "🔴 [Lockout] Stop Loss 일일 거래 제한 활성화 중 (24시간 차단)"
            lockout_msg = (
                f"당일 재진입 차단: {symbol} - {sl_lockout_reason}; "
                "same-day symbol re-entry blocked for both LONG and SHORT"
            )
            long_lines.append(lockout_msg)
            short_lines.append(lockout_msg)

        def _side_has_red_gate(side_lines):
            return any("🔴" in line or "불만족" in line for line in side_lines if "필수 게이트" not in line)

        candidate_lines = short_lines if candidate_side == 'short' else long_lines if candidate_side == 'long' else []
        if candidate_side in {'long', 'short'} and not _side_has_red_gate(candidate_lines):
            if not self._is_valid_number(planned_qty) or not self._is_valid_number(risk_usdt) or float(risk_usdt or 0) <= 0:
                entry_plan_detail += " / 상태진단: 후보 EV는 통과처럼 보이나 수량계산 risk=0"

        # Gather eligibility details
        cooldown_reasons = []
        now_ts = time.time()
        cooldown_remaining, cooldown_state = self._coin_selector_cooldown_remaining(
            symbol,
            cfg,
            now=now_ts
        )
        if cooldown_remaining > 0:
            last_r = "unknown"
            if isinstance(cooldown_state, dict):
                last_r = cooldown_state.get('last_reason') or "unknown"
            cooldown_reasons.append(f"candidate cooldown {cooldown_remaining / 60.0:.1f}m ({last_r})")

        # Bridge cooldown
        key = self._utbreakout_trace_key(symbol)
        last_attempt = float(self.utbreakout_auto_entry_bridge_last_attempt_ts.get(key, 0.0) or 0.0)
        cooldown_sec = float(cfg.get('utbreakout_auto_entry_bridge_cooldown_sec', 60.0) or 60.0)
        if now_ts - last_attempt < cooldown_sec:
            cooldown_reasons.append(f"bridge cooldown (elapsed {now_ts - last_attempt:.1f}s)")

        pos = await self.get_server_position(symbol, use_cache=False)
        has_open_position = pos is not None

        active_positions = []
        try:
            active_positions = await self.get_active_position_symbols(use_cache=False)
        except Exception:
            pass
        has_other_position = any(
            self._utbreakout_status_symbol_key(p) != self._utbreakout_status_symbol_key(symbol)
            for p in active_positions
        )

        auto_entry_enabled = bool(getattr(self, 'utbreakout_auto_entry_bridge_enabled', True))
        evaluated_symbol = evaluated_symbol or symbol
        gate_next_symbol = next_scan_symbol
        if not gate_next_symbol:
            gate_next_symbol = (
                getattr(self, 'current_utbreakout_candidate_symbol', None)
                or getattr(self, 'current_coin_selector_symbol', None)
                or None
            )
        if is_live_scanner_context and not gate_next_symbol:
            gate_next_symbol = symbol
        if (
            not is_current_scanner_candidate
            and gate_next_symbol
            and self._utbreakout_status_symbol_key(gate_next_symbol)
            == self._utbreakout_status_symbol_key(evaluated_symbol)
        ):
            is_current_scanner_candidate = True

        long_plan = self._get_utbot_filtered_breakout_entry_plan(symbol, 'long')
        long_eligibility = self._build_utbreakout_execution_eligibility(
            symbol=symbol,
            side='long',
            candidate_side=candidate_side,
            candidate_type=candidate_type,
            side_condition_ok=long_ok,
            risk_ok=bool(risk_ok),
            planned_qty=planned_qty,
            risk_usdt=risk_usdt,
            entry_plan_detail=entry_plan_detail,
            cooldown_reasons=cooldown_reasons,
            has_open_position=has_open_position,
            has_other_position=has_other_position,
            auto_entry_enabled=auto_entry_enabled,
            daily_risk_ok=bool(daily_ok),
            plan_lookup_ready=isinstance(long_plan, dict),
            cfg=cfg,
            scanner_source=scanner_source,
            is_live_scanner_context=is_live_scanner_context,
            is_current_scanner_candidate=is_current_scanner_candidate,
            is_coinselector_top_candidate=is_coinselector_top_candidate,
            next_scan_symbol=gate_next_symbol,
            evaluated_symbol=evaluated_symbol,
            manual_status_only=manual_status_only,
        )

        short_plan = self._get_utbot_filtered_breakout_entry_plan(symbol, 'short')
        short_eligibility = self._build_utbreakout_execution_eligibility(
            symbol=symbol,
            side='short',
            candidate_side=candidate_side,
            candidate_type=candidate_type,
            side_condition_ok=short_ok,
            risk_ok=bool(risk_ok),
            planned_qty=planned_qty,
            risk_usdt=risk_usdt,
            entry_plan_detail=entry_plan_detail,
            cooldown_reasons=cooldown_reasons,
            has_open_position=has_open_position,
            has_other_position=has_other_position,
            auto_entry_enabled=auto_entry_enabled,
            daily_risk_ok=bool(daily_ok),
            plan_lookup_ready=isinstance(short_plan, dict),
            cfg=cfg,
            scanner_source=scanner_source,
            is_live_scanner_context=is_live_scanner_context,
            is_current_scanner_candidate=is_current_scanner_candidate,
            is_coinselector_top_candidate=is_coinselector_top_candidate,
            next_scan_symbol=gate_next_symbol,
            evaluated_symbol=evaluated_symbol,
            manual_status_only=manual_status_only,
        )

        ready_side = (
            candidate_side
            if candidate_side == 'long' and long_ok
            else candidate_side
            if candidate_side == 'short' and short_ok
            else 'long'
            if long_ok
            else 'short'
            if short_ok
            else None
        )
        _record_status_evaluated(
            'OK',
            candidate_side=candidate_side,
            candidate_type=candidate_type,
            long_ok=long_ok,
            short_ok=short_ok,
            ready_side=ready_side,
            final=(
                f"LONG {'주문시도 대상' if long_eligibility['can_attempt'] else '조건통과 / 주문 안함' if long_ok else '대기'} / "
                f"SHORT {'주문시도 대상' if short_eligibility['can_attempt'] else '조건통과 / 주문 안함' if short_ok else '대기'}"
            ),
            selected_set=(
                f"Set{selected_set.get('id')} {selected_set.get('name')}"
                if isinstance(selected_set, dict) else ''
            ),
            current_position='see position scan context',
            decision_candle_ts=decision_ts,
            qty=planned_qty,
            entry_price=entry_price,
            margin=planned_margin,
            notional=planned_notional,
            risk_usdt=risk_usdt,
        )

        status_side = candidate_side if candidate_side in {'long', 'short'} else 'long'
        status_filter_values = _market_quality_filter_values()
        status_feature_score = self._calculate_utbreakout_feature_score(status_side, cfg, status_filter_values)

        micro_cfg = self._get_micro_auto_config() if hasattr(self, '_get_micro_auto_config') else {}
        micro_plan = self.micro_auto_last_plan.get(symbol) if (getattr(self, 'micro_auto_last_plan', None) and isinstance(self.micro_auto_last_plan, dict)) else None
        micro_reject = self.micro_auto_last_rejects.get(symbol) if (getattr(self, 'micro_auto_last_rejects', None) and isinstance(self.micro_auto_last_rejects, dict)) else None
        if micro_cfg.get('enabled'):
            if isinstance(status_micro_result, dict) and status_micro_result.get('accepted'):
                micro_line = (
                    f"Micro Auto: ON / {'DRY-RUN' if status_micro_result.get('dry_run') else 'LIVE'} / "
                    f"{float(status_micro_result.get('planned_margin', 0) or 0):.2f} margin / "
                    f"{float(status_micro_result.get('planned_notional', 0) or 0):.2f} notional / "
                    f"{int(float(status_micro_result.get('leverage', 0) or 0))}x"
                )
            elif isinstance(status_micro_result, dict):
                micro_line = (
                    f"Micro Auto: ON / 현재 거절 {status_micro_result.get('reject_code')}: "
                    f"{status_micro_result.get('reason')}"
                )
            elif isinstance(micro_plan, dict) and micro_plan.get('micro_auto'):
                micro_line = (
                    f"Micro Auto: ON / {'DRY-RUN' if micro_plan.get('dry_run') else 'LIVE'} / "
                    f"{float(micro_plan.get('planned_margin', 0) or 0):.2f} margin / "
                    f"{float(micro_plan.get('planned_notional', 0) or 0):.2f} notional / "
                    f"{int(float(micro_plan.get('leverage', 0) or 0))}x"
                )
            elif isinstance(micro_reject, dict):
                micro_line = f"Micro Auto: ON / 최근 거절 {micro_reject.get('reject_code')}: {micro_reject.get('reason')}"
            else:
                micro_line = "Micro Auto: ON / 아직 계획 없음"
        else:
            micro_line = "Micro Auto: OFF"
        order_path_line = format_utbreakout_order_path_summary(
            build_utbreakout_order_path_summary(
                cfg,
                exchange_mode=self.ctrl.get_exchange_mode() if getattr(self, 'ctrl', None) else None,
                micro_cfg=micro_cfg,
            )
        )

        return {
            'ok_market': True,
            'symbol': symbol,
            'cfg': cfg,
            'daily_entries': daily_entries,
            'entry_tf': entry_tf,
            'htf_tf': htf_tf,
            'adaptive_decision': adaptive_decision,
            'decision_ts': decision_ts,
            'entry_price': entry_price,
            'ut_label': f"{str(candidate_side or 'none').upper()} ({candidate_type})",
            'mode_label': 'AUTO' if cfg.get('auto_select_enabled') else 'MANUAL',
            'selected_set': selected_set,
            'set_status': '실거래 연결' if selected_set.get('status') == 'active' else 'planned only',
            'auto_reason': auto_reason,
            'entry_plan_detail': entry_plan_detail,
            'micro_line': micro_line or 'Micro Auto: OFF',
            'order_path_line': order_path_line,
            'opposite_set_exit_detail': opposite_set_exit_detail,
            'market_regime_context': market_regime_context,
            'score_line': (
                f"trend {auto_analysis.get('scores', {}).get('trend_score', 0):.1f} / chop {auto_analysis.get('scores', {}).get('chop_score', 0):.1f} / "
                f"vol {auto_analysis.get('scores', {}).get('volatility_score', 0):.1f} / breakout {auto_analysis.get('scores', {}).get('breakout_score', 0):.1f} / "
                f"momentum {auto_analysis.get('scores', {}).get('momentum_score', 0):.1f} / flow {auto_analysis.get('scores', {}).get('flow_score', 0):.1f}"
                if isinstance(auto_analysis, dict) and auto_analysis.get('scores') else "AUTO OFF 또는 분석 대기"
            ),
            'orderflow_line': (
                f"Orderflow: imb {_fmt(futures_context.get('rolling_orderbook_imbalance_pct'), 2)}% / "
                f"Δ {_fmt(futures_context.get('rolling_orderbook_imbalance_delta'), 2)} / "
                f"OFI {_fmt(futures_context.get('rolling_ofi_score'), 2)} / "
                f"samples {int(float(futures_context.get('rolling_ofi_samples') or 0))}"
            ),
            'oi_line': (
                f"OI/Funding: OI z {_fmt(futures_context.get('open_interest_delta_z'), 2)} / "
                f"1h {_fmt(futures_context.get('open_interest_change_1h'), 2)}% / "
                f"4h {_fmt(futures_context.get('open_interest_change_4h'), 2)}% / "
                f"accel {_fmt(futures_context.get('open_interest_acceleration'), 3)} / "
                f"funding {_fmt(futures_context.get('funding_rate'), 6)} / "
                f"fpct {_fmt(futures_context.get('funding_percentile_7d'), 0)}/{_fmt(futures_context.get('funding_percentile_30d'), 0)} / "
                f"L/S {_fmt(futures_context.get('long_short_ratio'), 3)} / "
                f"liq {_fmt(futures_context.get('liquidation_imbalance'), 2)}"
            ),
            'squeeze_line': (
                f"Squeeze: BB pct {_fmt(metrics.get('bb_width_percentile'), 2)} / "
                f"Keltner {'ON' if metrics.get('keltner_squeeze_on') is True else 'OFF' if metrics.get('keltner_squeeze_on') is False else 'n/a'} / "
                f"range {_fmt(metrics.get('range_expansion_ratio'), 2)} / state {metrics.get('squeeze_release_state') or 'n/a'}"
            ),
            'feature_line': (
                f"Feature Score: {_fmt(status_feature_score.get('score'), 1)} / "
                f"{status_feature_score.get('reason') or 'n/a'}"
            ),
            'ut_reason': ut_reason,
            'long_ok': long_ok,
            'long_lines': long_lines,
            'short_ok': short_ok,
            'short_lines': short_lines,
            'long_eligibility': long_eligibility,
            'short_eligibility': short_eligibility,
        }

    async def build_utbreakout_no_live_candidate_status_text(self):
        cfg = self._get_utbot_filtered_breakout_config(self.get_runtime_strategy_params())
        cfg = apply_stable_utbreak_final_overrides(cfg)
        cfg = apply_profit_opportunity_effective_overrides(cfg)
        self.utbreakout_last_status_symbol = None

        lines = [
            "🚦 UT Breakout 조건 스테이터스",
            "현재 live 후보: 없음",
            "조건 평가 심볼: 없음 (참고 심볼 조건표 표시 안 함)",
            "최종: live 후보 대기 / 주문 안함",
        ]
        try:
            context_lines = await self._build_utbreakout_position_scan_context_lines(None)
        except Exception as exc:
            context_lines = [f"스캐너 상태 조회 실패: {exc}"]

        for line in context_lines:
            if line and line not in lines:
                lines.append(line)
        try:
            micro_cfg = self._get_micro_auto_config() if hasattr(self, '_get_micro_auto_config') else {}
            lines.append(format_utbreakout_order_path_summary(
                build_utbreakout_order_path_summary(
                    cfg,
                    exchange_mode=self.ctrl.get_exchange_mode() if getattr(self, 'ctrl', None) else None,
                    micro_cfg=micro_cfg,
                )
            ))
            lines.extend(self._format_utbreakout_entry_diagnostics_lines(None))
        except Exception as exc:
            lines.append(f"Order Path: unavailable ({exc})")

        lines.extend([
            "",
            "실제 주문 규칙:",
            "live scanner 후보가 선택되고 해당 방향 필수 게이트가 모두 만족되면 STATUS_READY -> AUTO_ENTRY_BRIDGE -> entry() 순서로 주문을 시도합니다.",
            "상태 조회 화면은 참고 심볼을 만들어 주문 판단처럼 표시하지 않습니다.",
        ])
        return enforce_utbreakout_effective_status_contract("\n".join(lines), cfg)

    async def build_utbreakout_condition_status_text(self, symbol):
        if not self.is_upbit_mode():
            symbol = self._canonicalize_utbreakout_symbol_for_use(
                symbol,
                source='status',
            )
        cfg = self._get_utbot_filtered_breakout_config(self.get_runtime_strategy_params())
        cfg = apply_stable_utbreak_final_overrides(cfg)
        cfg = apply_profit_opportunity_effective_overrides(cfg)
        self.utbreakout_last_status_symbol = symbol
        common_cfg = self.get_runtime_common_settings()
        lev = int(max(1.0, float(common_cfg.get('leverage', 5) or 5)))
        entry_tf = cfg.get('entry_timeframe', '15m')
        htf_tf = cfg.get('htf_timeframe', '1h')

        def _record_status_evaluated(status='OK', **data):
            self._utbreakout_trace_event(
                symbol,
                'STATUS_EVALUATED',
                status,
                effective_profile=cfg.get('effective_profile_version'),
                entry_tf=cfg.get('entry_timeframe', entry_tf),
                htf=cfg.get('htf_timeframe', htf_tf),
                **data,
            )

        _record_status_evaluated(
            'START',
            phase='status_build_started',
        )

        if self.is_upbit_mode():
            ok_market = True
            validation_reason = ''
        else:
            ok_market, symbol, validation_reason = (
                self._ensure_valid_utbreakout_market_symbol(
                    symbol,
                    source='status',
                )
            )
        self.utbreakout_last_status_symbol = symbol
        if not ok_market:
            restricted_symbol = validation_reason.startswith(
                'REJECTED_REGION_RESTRICTED_SYMBOL:'
            )
            _record_status_evaluated(
                'BLOCKED' if restricted_symbol else 'INVALID_MARKET',
                reason=validation_reason,
            )
            if restricted_symbol:
                return enforce_utbreakout_effective_status_contract(
                    "\n".join([
                        "🚦 UT Breakout 조건 스테이터스",
                        f"조건 평가 심볼: {symbol}",
                        "최종: 진입 차단",
                        f"🔴 제한 티커 제외: {validation_reason}",
                        "이 심볼은 한국계정 거래 제한으로 "
                        "스캔/진입 대상에서 제외됩니다.",
                    ]),
                    cfg,
                )
            return enforce_utbreakout_effective_status_contract(
                "\n".join([
                    "🚦 UT Breakout 조건 스테이터스",
                    f"조건 평가 심볼: {symbol}",
                    "최종: 진입 차단",
                    "🔴 유효하지 않은 Binance Futures 심볼: "
                    f"{validation_reason}",
                    "이 심볼은 exchange.markets에 없으므로 "
                    "스캔/진입 대상에서 제외됩니다.",
                ]),
                cfg,
            )

        def _icon(state):
            if state is True:
                return "🟢"
            if state is False:
                return "🔴"
            return "🟡"

        def _state_label(state):
            if state is True:
                return "만족"
            if state is False:
                return "불만족"
            if state == 'reduced':
                return "축소"
            return "대기"

        def _fmt(value, digits=2):
            try:
                if value is None or not np.isfinite(float(value)):
                    return "n/a"
                return f"{float(value):.{digits}f}"
            except (TypeError, ValueError):
                return "n/a"

        def _fmt_ts(ms):
            try:
                ts = int(ms or 0)
                if ts <= 0:
                    return "n/a"
                return datetime.fromtimestamp(ts / 1000, timezone.utc).astimezone(
                    timezone(timedelta(hours=9))
                ).strftime('%m-%d %H:%M KST')
            except Exception:
                return "n/a"

        def _line(idx, label, state, detail):
            return f"{_icon(state)} {_state_label(state)} {idx}. {label}: {detail}"

        if self.is_upbit_mode():
            _record_status_evaluated(
                'UNSUPPORTED_MODE',
                reason='Upbit spot mode does not use UTBreakout',
            )
            return enforce_utbreakout_effective_status_contract(
                "🚦 UT Breakout 조건 스테이터스\n\n업비트 현물 모드에서는 UTBOT_FILTERED_BREAKOUT_V1을 사용하지 않습니다.",
                cfg,
            )

        try:
            ohlcv = await asyncio.to_thread(
                self.market_data_exchange.fetch_ohlcv,
                symbol,
                entry_tf,
                limit=300
            )
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        except Exception as e:
            _record_status_evaluated(
                'DATA_ERROR',
                reason=str(e),
            )
            return enforce_utbreakout_effective_status_contract(
                f"🚦 UT Breakout 조건 스테이터스\n\n{symbol} {entry_tf} 데이터 조회 실패: {e}",
                cfg,
            )

        if df is None or len(df) < 5:
            _record_status_evaluated(
                'INSUFFICIENT_DATA',
                rows=0 if df is None else len(df),
            )
            return enforce_utbreakout_effective_status_contract(
                f"🚦 UT Breakout 조건 스테이터스\n\n{symbol} {entry_tf} 데이터 부족",
                cfg,
            )

        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        closed = df.iloc[:-1].copy().dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
        if len(closed) < 5:
            _record_status_evaluated(
                'INSUFFICIENT_VALID_DATA',
                rows=len(closed),
            )
            return enforce_utbreakout_effective_status_contract(
                f"🚦 UT Breakout 조건 스테이터스\n\n{symbol} {entry_tf} 유효 데이터 부족",
                cfg,
            )

        adaptive_decision = None
        if bool(cfg.get('adaptive_timeframe_enabled', False)):
            try:
                cfg, df, adaptive_decision = await self._resolve_utbreakout_adaptive_timeframe(symbol, df, cfg)
                cfg = apply_profit_opportunity_effective_overrides(cfg)
                entry_tf = cfg.get('entry_timeframe', entry_tf)
                htf_tf = cfg.get('htf_timeframe', htf_tf)
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
                closed = df.iloc[:-1].copy().dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
                if len(closed) < 5:
                    _record_status_evaluated(
                        'INSUFFICIENT_ADAPTIVE_DATA',
                        rows=len(closed),
                    )
                    return enforce_utbreakout_effective_status_contract(
                        f"🚦 UT Breakout 조건 스테이터스\n\n{symbol} {entry_tf} 유효 데이터 부족",
                        cfg,
                    )
            except Exception as e:
                adaptive_decision = {'selected_tf': None, 'decision': 'ERROR', 'reason': str(e), 'top3': []}

        selected_set, auto_analysis, auto_reason = await self._resolve_utbreakout_selected_set(symbol, df, cfg)
        effective_cfg = dict(cfg)
        effective_cfg.update(selected_set.get('params', {}) if isinstance(selected_set, dict) else {})
        effective_cfg['active_set_id'] = selected_set.get('id', cfg.get('active_set_id', UTBREAKOUT_DEFAULT_SET_ID))
        effective_cfg['profile'] = f"set{effective_cfg['active_set_id']}"
        cfg = effective_cfg
        cfg = apply_stable_utbreak_final_overrides(cfg)
        cfg = apply_profit_opportunity_effective_overrides(cfg)
        entry_tf = cfg.get('entry_timeframe', '15m')
        htf_tf = cfg.get('htf_timeframe', '1h')

        decision_ts = int(closed.iloc[-1].get('timestamp') or 0) if len(closed) else 0
        entry_price = float(closed.iloc[-1]['close']) if len(closed) else np.nan

        ut_sig, ut_reason, ut_detail = self._calculate_utbot_signal(
            df,
            self._get_utbot_filtered_breakout_ut_params(cfg)
        )
        ut_detail = ut_detail or {}
        ut_bias_side = str(ut_detail.get('bias_side') or '').lower()
        candidate_side = ut_sig if ut_sig in {'long', 'short'} else ut_bias_side if ut_bias_side in {'long', 'short'} else None
        candidate_type = 'fresh_signal' if ut_sig in {'long', 'short'} else 'bias_state' if candidate_side else 'waiting'

        metrics = self._calculate_utbreakout_timeframe_metrics(closed, cfg)
        ema_fast_len = int(cfg.get('ema_fast', 50) or 50)
        ema_slow_len = int(cfg.get('ema_slow', 200) or 200)
        htf_ready = False
        htf_error = None
        htf_close = htf_ema_fast = htf_ema_slow = htf_gap_pct = np.nan
        htf_supertrend_direction = None
        htf_supertrend_reason = None
        try:
            htf_ohlcv = await asyncio.to_thread(
                self.market_data_exchange.fetch_ohlcv,
                symbol,
                htf_tf,
                limit=300
            )
            htf_df = pd.DataFrame(htf_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            for col in ['open', 'high', 'low', 'close', 'volume']:
                htf_df[col] = pd.to_numeric(htf_df[col], errors='coerce')
            htf_closed = htf_df.iloc[:-1].dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
            if len(htf_closed) >= ema_slow_len + 2:
                htf_close_series = htf_closed['close'].astype(float)
                htf_ema_fast = float(htf_close_series.ewm(span=ema_fast_len, adjust=False).mean().iloc[-1])
                htf_ema_slow = float(htf_close_series.ewm(span=ema_slow_len, adjust=False).mean().iloc[-1])
                htf_close = float(htf_close_series.iloc[-1])
                htf_gap_pct = abs(htf_ema_fast - htf_ema_slow) / max(abs(htf_close), 1e-9) * 100.0
                htf_ready = True
            else:
                htf_error = f"데이터 부족 {len(htf_closed)}/{ema_slow_len + 2}"
            htf_supertrend_direction, _, htf_supertrend_reason = self._calculate_utbot_filter_pack_supertrend_direction(
                htf_closed,
                length=10,
                multiplier=3.0
            )
        except Exception as e:
            htf_error = str(e)

        futures_context = {}
        try:
            futures_context = (
                (auto_analysis or {}).get('futures_context')
                if isinstance(auto_analysis, dict) and isinstance((auto_analysis or {}).get('futures_context'), dict)
                else None
            )
            if futures_context is None:
                futures_context = await self._fetch_utbreakout_futures_context(symbol)
            futures_context = dict(futures_context or {})
        except Exception as e:
            futures_context = {'futures_context_error': str(e)}
        market_regime_context = {}
        try:
            market_regime_context = await self._fetch_utbreakout_market_regime_context(cfg)
            market_regime_context = dict(market_regime_context or {})
        except Exception as e:
            market_regime_context = {'error': str(e)}

        try:
            status_atr_series = self._calculate_wilder_atr_series(closed, int(cfg.get('atr_length', 14) or 14))
        except Exception:
            status_atr_series = None

        def _market_quality_filter_values():
            def _candidate_signal_age_candles():
                try:
                    signal_ts = float(ut_detail.get('signal_ts') or 0.0)
                    timeframe_ms = float(
                        self._timeframe_to_ms(entry_tf) or (15 * 60 * 1000)
                    )
                    if signal_ts > 0 and decision_ts >= signal_ts:
                        return (float(decision_ts) - signal_ts) / max(timeframe_ms, 1.0)
                    if candidate_type == 'fresh_signal':
                        return 0.0
                except (TypeError, ValueError):
                    if candidate_type == 'fresh_signal':
                        return 0.0
                return None

            values = {
                'entry_price': entry_price,
                'open': metrics.get('open'),
                'high': metrics.get('high'),
                'low': metrics.get('low'),
                'close': metrics.get('close'),
                'rsi': metrics.get('rsi'),
                'macd_hist': metrics.get('macd_hist'),
                'macd_hist_prev': metrics.get('macd_hist_prev'),
                'roc_pct': metrics.get('roc_pct'),
                'cci': metrics.get('cci'),
                'stoch_k': metrics.get('stoch_k'),
                'stoch_d': metrics.get('stoch_d'),
                'atr': metrics.get('atr'),
                'atr_pct': metrics.get('atr_pct'),
                'adx': metrics.get('adx'),
                'plus_di': metrics.get('plus_di'),
                'minus_di': metrics.get('minus_di'),
                'adx_reason': metrics.get('adx_reason'),
                'chop': metrics.get('chop'),
                'chop_reason': metrics.get('chop_reason'),
                'ema50': metrics.get('ema_fast'),
                'ema50_prev': metrics.get('ema_fast_prev'),
                'ema200': metrics.get('ema_slow'),
                'donchian_high_prev': metrics.get('donchian_high_prev'),
                'donchian_low_prev': metrics.get('donchian_low_prev'),
                'donchian_width_pct': metrics.get('donchian_width_pct'),
                'bb_upper': metrics.get('bb_upper'),
                'bb_lower': metrics.get('bb_lower'),
                'bb_mid': metrics.get('bb_mid'),
                'bb_width_pct': metrics.get('bb_width_pct'),
                'bb_width_prev_pct': metrics.get('bb_width_prev_pct'),
                'bb_width_min_pct': metrics.get('bb_width_min_pct'),
                'bb_width_percentile': metrics.get('bb_width_percentile'),
                'keltner_upper': metrics.get('keltner_upper'),
                'keltner_lower': metrics.get('keltner_lower'),
                'keltner_mid': metrics.get('keltner_mid'),
                'keltner_width_pct': metrics.get('keltner_width_pct'),
                'keltner_width_prev_pct': metrics.get('keltner_width_prev_pct'),
                'keltner_squeeze_on': metrics.get('keltner_squeeze_on'),
                'range_expansion_ratio': metrics.get('range_expansion_ratio'),
                'range_compression_ratio': metrics.get('range_compression_ratio'),
                'squeeze_release_state': metrics.get('squeeze_release_state'),
                'volume_ratio': metrics.get('volume_ratio'),
                'obv_slope_ratio': metrics.get('obv_slope_ratio'),
                'mfi': metrics.get('mfi'),
                'vwap': metrics.get('vwap'),
                'vwap_slope': metrics.get('vwap_slope'),
                'vwap_reason': metrics.get('vwap_reason'),
                'psar_direction': metrics.get('psar_direction'),
                'psar': metrics.get('psar'),
                'psar_reason': metrics.get('psar_reason'),
                'ichimoku_bias': metrics.get('ichimoku_bias'),
                'ichimoku_top': metrics.get('ichimoku_top'),
                'ichimoku_bottom': metrics.get('ichimoku_bottom'),
                'vortex_plus': metrics.get('vortex_plus'),
                'vortex_minus': metrics.get('vortex_minus'),
                'vortex_reason': metrics.get('vortex_reason'),
                'aroon_up': metrics.get('aroon_up'),
                'aroon_down': metrics.get('aroon_down'),
                'aroon_reason': metrics.get('aroon_reason'),
                'session_hour_kst': metrics.get('session_hour_kst'),
                'auto_scores': (auto_analysis or {}).get('scores') if isinstance(auto_analysis, dict) else {},
                'mtf_metrics': (auto_analysis or {}).get('timeframes') if isinstance(auto_analysis, dict) else {},
                'htf_ready': htf_ready,
                'htf_error': htf_error,
                'htf_close': htf_close,
                'htf_ema_fast': htf_ema_fast,
                'htf_ema_slow': htf_ema_slow,
                'htf_gap_pct': htf_gap_pct,
                'htf_supertrend_direction': htf_supertrend_direction,
                'htf_supertrend_reason': htf_supertrend_reason,
                'funding_rate': futures_context.get('funding_rate'),
                'next_funding_time': futures_context.get('next_funding_time'),
                'open_interest_delta_pct': futures_context.get('open_interest_delta_pct'),
                'open_interest_delta_z': futures_context.get('open_interest_delta_z'),
                'open_interest_acceleration': futures_context.get('open_interest_acceleration'),
                'open_interest_hist_samples': futures_context.get('open_interest_hist_samples'),
                'taker_buy_sell_ratio': futures_context.get('taker_buy_sell_ratio'),
                'long_short_ratio': futures_context.get('long_short_ratio'),
                'orderbook_imbalance_pct': futures_context.get('orderbook_imbalance_pct'),
                'rolling_orderbook_imbalance_pct': futures_context.get('rolling_orderbook_imbalance_pct'),
                'rolling_orderbook_imbalance_delta': futures_context.get('rolling_orderbook_imbalance_delta'),
                'rolling_ofi_score': futures_context.get('rolling_ofi_score'),
                'rolling_ofi_samples': futures_context.get('rolling_ofi_samples'),
                'futures_spread_pct': futures_context.get('futures_spread_pct'),
                'bid_depth_usdt': futures_context.get('bid_depth_usdt'),
                'ask_depth_usdt': futures_context.get('ask_depth_usdt'),
                'basis_pct': futures_context.get('basis_pct'),
                'market_regime_context': market_regime_context,
            }
            signal_age = _candidate_signal_age_candles()
            if signal_age is not None:
                values['signal_age_candles'] = signal_age
            values = self._enrich_utbreakout_trend_health_values(values, closed, cfg, status_atr_series)
            return self._enrich_utbreakout_strategy_quality_values(values, closed, cfg)

        daily_count, daily_pnl = self.db.get_daily_stats()
        daily_entries = self.db.get_daily_entry_count()
        max_losses = int(cfg.get('max_consecutive_losses', 3) or 3)
        recent_pnls = self._get_recent_strategy_closed_trade_pnls(
            {
                ENTRY_STRATEGY_UT_BREAKOUT,
                UTBOT_FILTERED_BREAKOUT_STRATEGY,
                UTBOT_ADAPTIVE_TIMEFRAME_STRATEGY,
            },
            max_losses,
            today_only=True,
        )
        daily_ok = True
        daily_detail = f"PnL {_fmt(daily_pnl, 2)} / trades {daily_entries}/{int(cfg['max_daily_trades'])}"
        if float(cfg.get('daily_max_loss_usdt', 0) or 0) > 0 and float(daily_pnl or 0) <= -float(cfg['daily_max_loss_usdt']):
            daily_ok = False
            daily_detail = f"일손실 한도 도달 PnL {_fmt(daily_pnl, 2)}"
        elif int(cfg.get('max_daily_trades', 0) or 0) > 0 and daily_entries >= int(cfg['max_daily_trades']):
            daily_ok = False
            daily_detail = f"일일 거래수 한도 {daily_entries}/{int(cfg['max_daily_trades'])}"
        elif bool(cfg.get('daily_profit_target_enabled', False)) and float(daily_pnl or 0) >= float(cfg.get('daily_profit_target_usdt', 0) or 0):
            daily_ok = False
            daily_detail = f"일 목표수익 도달 {_fmt(daily_pnl, 2)}"
        elif len(recent_pnls) >= max_losses and all(float(pnl) < 0 for pnl in recent_pnls[:max_losses]):
            daily_ok = False
            daily_detail = f"연속 손절 {max_losses}회"

        balance_detail = "잔고 조회 대기"
        entry_plan_detail = "진입 계획: ATR/잔고 계산 대기"
        take_profit_detail = "익절 계획: ATR/잔고 계산 대기"
        risk_ok = None
        risk_distance = np.nan
        risk_distance_pct = np.nan
        risk_usdt = np.nan
        planned_qty = np.nan
        planned_notional = np.nan
        planned_margin = np.nan
        take_profit_distance = np.nan
        take_profit_pct = np.nan
        expected_profit_usdt = np.nan
        status_micro_result = None
        atr_value = metrics.get('atr')
        atr_pct = metrics.get('atr_pct')
        if self._is_valid_number(atr_value):
            try:
                total_balance, free_balance, _ = await self.get_balance_info()
                balance_for_risk = total_balance if total_balance > 0 else free_balance
                side_for_plan = candidate_side if candidate_side in {'long', 'short'} else 'long'
                risk_budget = resolve_utbreakout_risk_budget(
                    balance_for_risk,
                    cfg,
                    daily_pnl_usdt=daily_pnl,
                )
                risk_per_trade_percent = risk_budget['risk_per_trade_percent']
                max_risk_per_trade_usdt = risk_budget['max_risk_per_trade_usdt']
                risk_note = ""
                ev_plan_enabled = bool(cfg.get('ev_adaptive_enabled', False))
                if (
                    not ev_plan_enabled
                    and side_for_plan == 'short'
                    and bool(cfg.get('short_conservative_enabled', True))
                ):
                    short_risk_multiplier = min(1.0, max(0.0, float(cfg.get('short_risk_multiplier', 0.5) or 0.5)))
                    risk_per_trade_percent *= short_risk_multiplier
                    max_risk_per_trade_usdt *= short_risk_multiplier
                    risk_note = f" / 숏 리스크 x{short_risk_multiplier:.2f}"
                market_quality_for_plan = self._evaluate_utbreakout_market_quality(
                    side_for_plan,
                    cfg,
                    _market_quality_filter_values()
                )
                market_quality_multiplier = min(
                    1.0,
                    max(0.0, float(market_quality_for_plan.get('risk_multiplier', 1.0) or 0.0))
                )
                if market_quality_for_plan.get('state') is False:
                    risk_note += " / 시장품질 BLOCK"
                elif not ev_plan_enabled and market_quality_multiplier < 0.999:
                    risk_per_trade_percent *= market_quality_multiplier
                    max_risk_per_trade_usdt *= market_quality_multiplier
                    risk_note += f" / 시장품질 x{market_quality_multiplier:.2f}"
                elif market_quality_multiplier < 0.999:
                    risk_note += f" / 시장품질 x{market_quality_multiplier:.2f}"
                adaptation_for_plan = self._build_utbreakout_strategy_adaptation(
                    symbol,
                    side_for_plan,
                    cfg,
                    selected_set,
                    _market_quality_filter_values()
                )
                adaptation_multiplier = min(
                    1.0,
                    max(0.0, float(adaptation_for_plan.get('risk_multiplier', 1.0) or 0.0))
                )
                trend_health_for_plan = adaptation_for_plan.get('trend_health') if isinstance(adaptation_for_plan.get('trend_health'), dict) else {}
                if not ev_plan_enabled and adaptation_multiplier < 0.999:
                    risk_per_trade_percent *= adaptation_multiplier
                    max_risk_per_trade_usdt *= adaptation_multiplier
                    risk_note += f" / 적응 x{adaptation_multiplier:.2f}"
                if trend_health_for_plan.get('state') is False:
                    risk_note += " / 추세건강 BLOCK"
                plan_cfg = dict(cfg)
                if ev_plan_enabled:
                    ev_plan_values = _market_quality_filter_values()
                    ev_plan_decision = evaluate_ev_adaptive_entry(
                        side=side_for_plan,
                        candidate_type=(
                            candidate_type
                            if candidate_side == side_for_plan
                            else 'waiting'
                        ),
                        values=ev_plan_values,
                        config=_ev_adaptive_runtime_config(cfg),
                    )
                    plan_cfg = _apply_ev_exit_profile(
                        plan_cfg,
                        ev_plan_decision.exit_profile,
                    )
                    volatility_multiplier = min(
                        1.0,
                        max(
                            0.0,
                            float(
                                adaptation_for_plan.get(
                                    'volatility_risk_multiplier',
                                    1.0,
                                )
                                or 0.0
                            ),
                        ),
                    )
                    ev_plan_multiplier = min(
                        float(ev_plan_decision.risk_multiplier),
                        market_quality_multiplier,
                        volatility_multiplier,
                    )
                    risk_per_trade_percent *= ev_plan_multiplier
                    max_risk_per_trade_usdt *= ev_plan_multiplier

                    if ev_plan_multiplier <= 0:
                        ev_plan_blockers = "; ".join(ev_plan_decision.blockers[:3]) or "unknown EV plan blocker"
                        risk_note += (
                            f" / EV {ev_plan_decision.mode} x0.00 "
                            f"BLOCK: {ev_plan_blockers}"
                        )
                    else:
                        risk_note += (
                            f" / EV {ev_plan_decision.mode} "
                            f"x{ev_plan_multiplier:.2f}"
                        )
                plan_cfg['take_profit_r_multiple'] = max(
                    float(plan_cfg.get('take_profit_r_multiple', 2.40) or 2.40),
                    float(plan_cfg.get('second_take_profit_r_multiple', 2.40) or 2.40),
                )
                plan = calculate_risk_plan(
                    side=side_for_plan,
                    entry_price=entry_price,
                    atr_value=atr_value,
                    stop_atr_multiplier=plan_cfg.get('stop_atr_multiplier', 1.5),
                    ut_stop=ut_detail.get('curr_stop'),
                    take_profit_r_multiple=plan_cfg.get('take_profit_r_multiple', 2.40),
                    min_risk_reward=plan_cfg.get('min_risk_reward', 2.0),
                    balance_usdt=balance_for_risk,
                    risk_per_trade_percent=risk_per_trade_percent,
                    max_risk_per_trade_usdt=max_risk_per_trade_usdt,
                    leverage=lev,
                )
                plan = cap_utbreakout_risk_plan_to_margin(
                    plan,
                    free_balance=free_balance,
                    leverage=lev,
                    entry_price=entry_price,
                )
                if self._micro_auto_enabled():
                    status_micro_cfg = self._get_micro_auto_config()
                    if ev_plan_enabled:
                        status_micro_cfg = dict(status_micro_cfg)
                        status_micro_cfg['risk_per_trade_pct'] = (
                            float(status_micro_cfg.get('risk_per_trade_pct', 0.0) or 0.0)
                            * ev_plan_multiplier
                        )
                        status_micro_cfg['max_risk_usdt'] = (
                            float(status_micro_cfg.get('max_risk_usdt', 0.0) or 0.0)
                            * ev_plan_multiplier
                        )
                    status_micro_result = build_micro_entry_plan(
                        side=side_for_plan,
                        entry_price=entry_price,
                        atr_value=atr_value,
                        ut_stop=ut_detail.get('curr_stop'),
                        base_plan=dict(
                            plan,
                            stop_atr_multiplier=plan_cfg.get('stop_atr_multiplier', 1.5),
                        ),
                        cfg=status_micro_cfg,
                        selected_set=selected_set,
                        auto_scores=(
                            (auto_analysis or {}).get('scores')
                            if isinstance(auto_analysis, dict)
                            else {}
                        ),
                        selected_timeframe=entry_tf,
                        total_equity_usdt=total_balance,
                        free_usdt=free_balance,
                        min_notional_usdt=await self._get_symbol_min_notional(symbol),
                        max_symbol_leverage=status_micro_cfg.get('max_leverage', 10),
                    )
                    if status_micro_result.get('accepted'):
                        plan = dict(plan)
                        plan.update(status_micro_result)
                        risk_note += (
                            f" / Micro {int(status_micro_result.get('leverage', lev) or lev)}x"
                        )
                    else:
                        risk_note += (
                            f" / Micro BLOCK {status_micro_result.get('reject_code')}"
                        )
                risk_distance = plan['risk_distance']
                risk_distance_pct = plan['risk_distance_pct']
                rr_multiple = plan['rr_multiple']
                take_profit_distance = plan['take_profit_distance']
                take_profit_pct = plan['take_profit_pct']
                risk_usdt = plan['risk_usdt']
                planned_qty = plan['qty']
                planned_notional = plan['planned_notional']
                planned_margin = plan['planned_margin']
                expected_profit_usdt = plan['expected_profit_usdt']
                risk_ok = (
                    bool(
                        status_micro_result.get('accepted')
                        and not status_micro_result.get('dry_run', True)
                        and status_micro_result.get('live_enabled', False)
                    )
                    if isinstance(status_micro_result, dict)
                    else True
                )
                balance_detail = (
                    f"손실한도 {_fmt(risk_usdt, 2)} USDT / 손절거리 {_fmt(risk_distance, 4)} "
                    f"({_fmt(risk_distance_pct, 3)}%) / qty {_fmt(planned_qty, 6)}{risk_note}"
                )
                entry_plan_detail = (
                    f"진입 계획: 증거금 {_fmt(planned_margin, 2)} USDT / "
                    f"포지션 {_fmt(planned_notional, 2)} USDT / 레버리지 {lev}x / "
                    f"손절시 손실 {_fmt(risk_usdt, 2)} USDT"
                )
                tp1_r = float(plan_cfg.get('partial_take_profit_r_multiple', 1.00) or 1.00)
                tp2_r = float(plan_cfg.get('second_take_profit_r_multiple', 2.40) or 2.40)
                tp1_ratio = float(plan_cfg.get('partial_take_profit_ratio', 0.30) or 0.30)
                tp2_ratio = float(plan_cfg.get('second_take_profit_ratio', 0.40) or 0.40)
                take_profit_detail = (
                    f"익절 계획: TP1 {tp1_r:.2f}R({tp1_ratio:.0%}) / "
                    f"TP2 {tp2_r:.2f}R({tp2_ratio:.0%}) / "
                    f"익절거리 {take_profit_distance:.4f} ({take_profit_pct:.3f}%) / "
                    f"예상수익 {expected_profit_usdt:.2f} USDT"
                )
            except ValueError as e:
                reason = str(e)
                if "risk budget unavailable" in reason:
                    multiplier_note = ""
                    if ev_plan_enabled:
                        multiplier_note = f" (최종 리스크 배율: {ev_plan_multiplier:.2f})"
                    budget_detail = (
                        f"리스크 예산 없음: total {_fmt(total_balance, 2)} / "
                        f"free {_fmt(free_balance, 2)} USDT, "
                        f"risk_pct {_fmt(risk_per_trade_percent, 4)}%, "
                        f"max_risk {_fmt(max_risk_per_trade_usdt, 2)} USDT{multiplier_note}"
                    )
                    balance_detail = budget_detail
                    entry_plan_detail = f"진입 계획: 리스크 예산 없음 ({budget_detail})"
                    take_profit_detail = f"익절 계획: 리스크 예산 없음 ({budget_detail})"
                else:
                    balance_detail = f"Risk plan calculation failed: {reason}"
                    entry_plan_detail = f"Entry plan blocked: {reason}"
                    take_profit_detail = f"TP plan blocked: {reason}"
            except Exception as e:
                reason = str(e)
                balance_detail = f"Balance or entry-plan calculation failed: {reason}"
                entry_plan_detail = f"Entry plan calculation failed: {reason}"
                take_profit_detail = f"TP plan calculation failed: {reason}"
        else:
            balance_detail = "ATR 기반 손절폭 계산 대기"
            entry_plan_detail = "진입 계획: ATR 기반 손절폭 계산 대기"
            take_profit_detail = "익절 계획: ATR 기반 손절폭 계산 대기"

        opposite_set_exit_detail = (
            f"반대Set청산: {'ON' if cfg.get('opposite_set_exit_enabled') else 'OFF'} | "
            f"조건: 반대 UT 신규신호 + 선택 Set 조건 통과 + 최소 "
            f"{int(float(cfg.get('opposite_set_exit_min_hold_candles', 3) or 0))}봉 보유 + "
            f"{'PnL≥$' + format(float(cfg.get('opposite_set_exit_min_pnl_usdt', 0.0) or 0.0), '.2f') if cfg.get('opposite_set_exit_min_pnl_enabled') else 'PnL조건 OFF'} | "
            "동작: 현재 포지션 청산만, 반대 신규진입 없음"
        )

        async def _side_conditions(side):
            side_upper = side.upper()
            if candidate_side == side:
                ut_state = True
                ut_detail_text = f"{side_upper} {candidate_type}"
            else:
                ut_state = False if candidate_side in {'long', 'short'} else None
                ut_detail_text = f"현재 {str(candidate_side or 'none').upper()} / bias {str(ut_bias_side or 'none').upper()}"

            filter_values = _market_quality_filter_values()
            feature_score = self._calculate_utbreakout_feature_score(side, cfg, filter_values)
            filter_values['feature_score'] = feature_score
            selected_items = self._evaluate_utbreakout_set_filter_items(side, selected_set, cfg, filter_values)
            failed_set_items = [item for item in selected_items if item.get('state') is not True]
            set_hard_failures = []
            set_soft_failures = []
            set_item_multipliers = []
            for item in failed_set_items:
                name = item.get('name') or ''
                detail = item.get('detail') or ''
                code = item.get('code') or ''
                is_core_set_failure = (
                    bool(cfg.get('selected_set_core_filter_hard_block_enabled', True))
                    and (
                        name in UTBREAKOUT_CORE_SET_FILTER_HARD_NAMES
                        or code in UTBREAKOUT_CORE_SET_FILTER_HARD_CODES
                        or bool(item.get('core_set_hard_block'))
                    )
                )
                if is_core_set_failure or name in {'ATR% 변동성', '손익비', '스프레드', '유동성'} or code == 'REJECTED_RISK_REWARD_LOW':
                    state, multiplier, classified_detail = False, 0.0, detail
                elif name in {'상대 거래량', 'Rolling OFI 확인', 'Futures 수급 불균형', 'Spread/Depth 비용'}:
                    state, multiplier, classified_detail = _classify_set_filter_result(
                        side,
                        False,
                        detail,
                        metrics=filter_values,
                        cfg=cfg,
                    )
                else:
                    state = 'reduced'
                    multiplier = 0.65 if item.get('state') is None else 0.50
                    classified_detail = f"{detail}; set confirmation soft fail"
                classified = dict(item)
                classified.update({
                    'state': state,
                    'risk_multiplier': multiplier,
                    'detail': classified_detail,
                })
                if state is False:
                    set_hard_failures.append(classified)
                else:
                    set_soft_failures.append(classified)
                    set_item_multipliers.append(float(multiplier))

            if set_hard_failures:
                set_state = False
                set_detail = "; ".join(
                    f"{item.get('name')}: {item.get('detail')}"
                    for item in set_hard_failures[:3]
                )
            elif set_soft_failures:
                set_state = 'reduced'
                set_detail = "; ".join(
                    f"{item.get('name')}: {item.get('detail')}"
                    for item in set_soft_failures[:3]
                )
            elif selected_items:
                set_state = True
                set_detail = f"{len(selected_items)}개 통과: " + ", ".join(
                    str(item.get('name') or '필터') for item in selected_items[:3]
                )
            else:
                set_state = True
                set_detail = "선택 Set 추가 필터 없음"

            if bool(cfg.get('ev_adaptive_enabled', False)):
                direction_state = True
                direction_detail = "EV Adaptive 통합 방향 판정 사용"
                direction_decision = None
            else:
                direction_state, direction_detail, direction_decision = (
                    await self._build_direction_decision_for_status(symbol, side, filter_values)
                )

            core_items = [
                ("UTBot 방향", ut_state, ut_detail_text),
            ]
            core_items.insert(1, ("방향 필터", direction_state, direction_detail))
            bias_multiplier_status = 1.0
            if (
                candidate_side == side
                and candidate_type == 'bias_state'
                and not bool(cfg.get('ev_adaptive_enabled', False))
            ):
                bias_status = {
                    'candidate_type': candidate_type,
                    'decision_candle_ts': decision_ts,
                    'ut_signal_ts': ut_detail.get('signal_ts'),
                    'adaptive_timeframe_decision': adaptive_decision,
                    'auto_selected_set_id': selected_set.get('id') if isinstance(selected_set, dict) else None,
                    'entry_timeframe': entry_tf,
                }
                bias_continuation_status = self._evaluate_utbreakout_bias_continuation(
                    side,
                    cfg,
                    bias_status,
                    filter_values,
                    selected_set,
                )
                bias_multiplier_status = min(
                    1.0,
                    max(
                        0.0,
                        float(bias_continuation_status.get('risk_multiplier', 1.0) or 0.0),
                    ),
                )
                core_items.append((
                    "Bias continuation",
                    bias_continuation_status.get('state'),
                    bias_continuation_status.get('summary'),
                ))
            core_items.append(("선택 Set 필터", set_state, set_detail))
            if side == 'short' and not bool(cfg.get('ev_adaptive_enabled', False)):
                core_items.append(self._build_utbreakout_short_guard_status_item(cfg, filter_values))
            market_quality = self._evaluate_utbreakout_market_quality(side, cfg, filter_values)
            core_items.append(("시장 품질", market_quality.get('state'), market_quality.get('summary')))
            selector_quality = self._build_utbreakout_selector_quality(symbol)
            selector_candidate = (
                selector_quality.get('candidate')
                if isinstance(selector_quality.get('candidate'), dict)
                else {}
            )
            for source_key, target_key in (
                ('rolling_sharpe', 'selector_rolling_sharpe'),
                ('return_lookback_pct', 'selector_return_lookback_pct'),
                ('momentum_consistency', 'selector_momentum_consistency'),
                ('directional_efficiency', 'selector_directional_efficiency'),
                ('cross_sectional_rank_pct', 'cross_sectional_rank_pct'),
            ):
                if selector_candidate.get(source_key) is not None:
                    filter_values[target_key] = selector_candidate.get(source_key)
            selector_multiplier = float(selector_quality.get('risk_multiplier', 1.0) or 1.0)
            selector_state = True if selector_multiplier >= 0.999 else 'reduced'
            adaptation = self._build_utbreakout_strategy_adaptation(symbol, side, cfg, selected_set, filter_values)
            adaptation_health = adaptation.get('trend_health') if isinstance(adaptation.get('trend_health'), dict) else {}
            adaptation_quality = adaptation.get('strategy_quality') if isinstance(adaptation.get('strategy_quality'), dict) else {}
            try:
                adaptation_multiplier = min(1.0, max(0.0, float(adaptation.get('risk_multiplier', 1.0) or 1.0)))
            except (TypeError, ValueError):
                adaptation_multiplier = 1.0
            if adaptation_health.get('state') is False or adaptation_quality.get('state') is False:
                adaptation_state = False
            elif (
                adaptation_health.get('state') == 'reduced'
                or adaptation_quality.get('state') == 'reduced'
                or adaptation_multiplier < 0.999
            ):
                adaptation_state = 'reduced'
            else:
                adaptation_state = True
            if not bool(cfg.get('ev_adaptive_enabled', False)):
                core_items.append((
                    "추세/전략 품질",
                    adaptation_state,
                    (
                        f"trend {adaptation_health.get('summary') or 'neutral'}; "
                        f"strategy {adaptation_quality.get('summary') or 'neutral'}"
                    ),
                ))
            q_status = {
                'candidate_type': candidate_type if candidate_side == side else 'waiting',
                'adaptive_timeframe_decision': adaptive_decision,
                'entry_timeframe': entry_tf,
            }
            quality_score_v2 = self._build_utbreakout_quality_score_v2(
                side,
                cfg,
                q_status,
                filter_values,
                trend_health=adaptation_health,
                strategy_quality=adaptation_quality,
                market_quality=market_quality,
                selector_quality=selector_quality,
            )
            if not bool(cfg.get('ev_adaptive_enabled', False)):
                core_items.append(("통합 품질 점수", quality_score_v2.get('state'), quality_score_v2.get('summary')))
            q2_multiplier = min(1.0, max(0.0, float(quality_score_v2.get('risk_multiplier', 1.0) or 1.0)))
            market_multiplier = min(1.0, max(0.0, float(market_quality.get('risk_multiplier', 1.0) or 1.0)))

            ev_status_decision = None
            ev_status_net = None
            ev_status_exit = None
            if bool(cfg.get('ev_adaptive_enabled', False)):
                filter_values.update({
                    'trend_health_score': adaptation_health.get('score'),
                    'strategy_quality_score': adaptation_quality.get('score'),
                    'quality_score_v2_score': quality_score_v2.get('score'),
                    'coin_selector_score': selector_quality.get('score'),
                })
                ev_status_decision = evaluate_ev_adaptive_entry(
                    side=side,
                    candidate_type=(
                        candidate_type if candidate_side == side else 'waiting'
                    ),
                    values=filter_values,
                    config=_ev_adaptive_runtime_config(cfg),
                )
                if candidate_side != side:
                    ev_state = None
                    ev_detail = (
                        f"현재 후보 {str(candidate_side or 'none').upper()}; "
                        f"{side.upper()} EV 평가는 대기"
                    )
                elif not ev_status_decision.allowed:
                    ev_state = False
                    ev_detail = (
                        f"{ev_status_decision.mode} score {ev_status_decision.score:.1f}: "
                        f"{'; '.join(ev_status_decision.blockers[:4])}"
                    )
                elif (
                    self._is_valid_number(planned_qty)
                    and self._is_valid_number(risk_usdt)
                    and self._is_valid_number(planned_notional)
                ):
                    ev_status_exit = adapt_exit_for_quantity(
                        ev_status_decision.exit_profile,
                        total_qty=planned_qty,
                        min_amount=self._get_min_amount_for_symbol(symbol),
                    )
                    ev_status_net = evaluate_net_edge(
                        risk_usdt=risk_usdt,
                        planned_notional=planned_notional,
                        win_probability=ev_status_decision.win_probability,
                        gross_win_r=profile_gross_win_r(ev_status_exit.profile),
                        config=_ev_adaptive_runtime_config(cfg),
                    )
                    ev_state = bool(
                        ev_status_exit.executable and ev_status_net.allowed
                    )
                    ev_detail = (
                        f"{ev_status_decision.mode} score {ev_status_decision.score:.1f}, "
                        f"p={ev_status_decision.win_probability:.2f}, "
                        f"MTF={ev_status_decision.mtf_alignment}, "
                        f"leader={ev_status_decision.leadership_score:.0f}, "
                        f"exit={ev_status_exit.mode}, "
                        f"net={ev_status_net.expected_net_r:.3f}R "
                        f"(cost {ev_status_net.cost_r:.3f}R)"
                    )
                else:
                    ev_state = None
                    ev_detail = (
                        f"{ev_status_decision.mode} score {ev_status_decision.score:.1f}; "
                        "수량/비용 계산 대기"
                    )
                q_status['ev_adaptive_status_summary'] = ev_detail

            entry_edge_status_decision = self._append_entry_edge_status_item(
                core_items,
                side=side,
                cfg=cfg,
                filter_values=filter_values,
                market_regime_context=market_regime_context,
                selector_quality=selector_quality,
                quality_score_v2=quality_score_v2,
                adaptation=adaptation,
                ev_status_decision=ev_status_decision,
                ev_status_net=ev_status_net,
                ev_status_exit=ev_status_exit,
            )
            if entry_edge_status_decision is not None:
                q_status['entry_edge_status_summary'] = entry_edge_status_decision.summary

            set_filter_multiplier_status = 1.0
            if set_hard_failures:
                set_filter_multiplier_status = 0.0
            elif set_soft_failures and bool(cfg.get('set_filter_soft_fail_enabled', True)):
                configured_set_multiplier = (
                    float(cfg.get('set_filter_multi_soft_fail_multiplier', 0.50) or 0.50)
                    if len(set_soft_failures) >= 2
                    else float(cfg.get('set_filter_soft_fail_multiplier', 0.70) or 0.70)
                )
                set_filter_multiplier_status = min(
                    [configured_set_multiplier, *set_item_multipliers]
                )
            elif set_soft_failures:
                set_filter_multiplier_status = 0.0

            if ev_status_decision is not None:
                volatility_multiplier = min(
                    1.0,
                    max(
                        0.0,
                        float(adaptation.get('volatility_risk_multiplier', 1.0) or 0.0),
                    ),
                )
                raw_final_risk_multiplier_status = min(
                    (
                        min(
                            1.0,
                            max(0.0, float(entry_edge_status_decision.risk_multiplier)),
                        )
                        if entry_edge_status_decision is not None
                        else float(ev_status_decision.risk_multiplier)
                    ),
                    market_multiplier,
                    volatility_multiplier,
                )
            else:
                raw_final_risk_multiplier_status = (
                    bias_multiplier_status
                    * market_multiplier
                    * selector_multiplier
                    * adaptation_multiplier
                    * q2_multiplier
                    * set_filter_multiplier_status
                    * (
                        min(
                            1.0,
                            max(0.0, float(entry_edge_status_decision.risk_multiplier)),
                        )
                        if entry_edge_status_decision is not None
                        else 1.0
                    )
                )
            raw_before_position_sizing_status = max(
                0.0,
                min(1.0, float(raw_final_risk_multiplier_status)),
            )
            position_sizing_status = {
                'risk_multiplier': 1.0,
                'blocked': False,
                'components': {},
                'reasons': [],
                'kelly_reason': 'disabled',
            }
            if bool(cfg.get('position_sizing_engine_enabled', True)):
                try:
                    position_sizing_status = build_position_risk_multiplier(
                        {
                            'atr_pct': filter_values.get('atr_pct'),
                            'meta_probability': 0.65,
                            'entry_edge_probability': (
                                entry_edge_status_decision.probability
                                if entry_edge_status_decision is not None
                                else (
                                    ev_status_decision.win_probability
                                    if ev_status_decision is not None
                                    else None
                                )
                            ),
                            'entry_edge_score': (
                                entry_edge_status_decision.score
                                if entry_edge_status_decision is not None
                                else None
                            ),
                            'direction_score': (
                                entry_edge_status_decision.direction_score
                                if entry_edge_status_decision is not None
                                else None
                            ),
                            'recent_closed_pnls': recent_pnls,
                            'daily_loss_limit_hit': daily_ok is False,
                            'liquidity_ok': market_quality.get('state') is not False,
                            'spread_ok': market_quality.get('hard_block') is not True,
                        },
                        cfg,
                    )
                    raw_final_risk_multiplier_status = min(
                        raw_before_position_sizing_status,
                        min(1.0, max(0.0, float(position_sizing_status.get('risk_multiplier', 1.0) or 0.0))),
                    )
                except Exception as exc:
                    logger.warning("UTBreakout status position sizing failed for %s: %s", symbol, exc)
                    raw_final_risk_multiplier_status = raw_before_position_sizing_status
            final_risk_multiplier_status = (
                0.0
                if bool(position_sizing_status.get('blocked'))
                else _apply_utbreakout_risk_multiplier_floor(raw_final_risk_multiplier_status, cfg)
            )

            base_max_risk = float(cfg.get('max_risk_per_trade_usdt', 1.0) or 1.0)
            effective_max_risk = base_max_risk * final_risk_multiplier_status
            entry_quality_state, entry_quality_detail = (
                self._evaluate_utbreakout_entry_quality_gate(
                    side,
                    cfg,
                    final_risk_multiplier=final_risk_multiplier_status,
                    market_quality=market_quality,
                    ev_decision=ev_status_decision,
                    ev_net=ev_status_net,
                    ev_exit=ev_status_exit,
                    entry_edge_decision=entry_edge_status_decision,
                )
            )
            core_items.append(("Entry Quality Gate", entry_quality_state, entry_quality_detail))
            core_items.extend([
                ("일일 리스크", daily_ok, daily_detail),
                ("ATR 손절/RR/수량", risk_ok, balance_detail),
            ])
            advisory_items = [
                (
                    "최종 누적 리스크",
                    'reduced' if final_risk_multiplier_status < 0.999 else True,
                    (
                        f"x{final_risk_multiplier_status:.3f} "
                        f"(base ${base_max_risk:.2f} -> effective ${effective_max_risk:.2f}; "
                        f"raw x{float(raw_before_position_sizing_status):.3f}, "
                        f"sizing x{float(position_sizing_status.get('risk_multiplier', 1.0) or 0.0):.2f}, "
                        f"bias x{bias_multiplier_status:.2f}, market x{market_multiplier:.2f}, "
                        f"selector x{selector_multiplier:.2f}, "
                        f"strategy x{adaptation_multiplier:.2f}, qscore x{q2_multiplier:.2f}, "
                        f"set x{set_filter_multiplier_status:.2f})"
                    ),
                ),
                ("코인 선택 품질", selector_state, selector_quality.get('summary')),
                ("Feature Score", True, f"{feature_score.get('score', 0):.1f} / {feature_score.get('reason')}"),
                (
                    "전략 적응 요약",
                    'reduced' if adaptation_multiplier < 0.999 else True,
                    adaptation.get('summary'),
                ),
                (
                    "레거시 통합 품질(참고)",
                    (
                        False
                        if adaptation_state is False or quality_score_v2.get('state') is False
                        else 'reduced'
                        if adaptation_state == 'reduced' or quality_score_v2.get('state') == 'reduced'
                        else True
                    ),
                    (
                        f"trend/strategy={adaptation.get('summary')}; "
                        f"qscore={quality_score_v2.get('summary')}"
                    ),
                ),
            ]
            ok = all(item[1] is True or item[1] == 'reduced' for item in core_items)
            lines = [
                f"{side_upper}: {'진입 가능' if ok else '대기'}",
                "필수 게이트",
            ]
            lines.extend(_line(idx, label, state, detail) for idx, (label, state, detail) in enumerate(core_items, 1))
            lines.append("참고/감액")
            lines.extend(
                f"{_icon(state)} {_state_label(state)} - {label}: {detail}"
                for label, state, detail in advisory_items
            )
            return ok, lines

        long_ok, long_lines = await _side_conditions('long')
        short_ok, short_lines = await _side_conditions('short')

        def _side_has_red_gate(side_lines):
            return any("🔴" in line or "불만족" in line for line in side_lines if "필수 게이트" not in line)

        candidate_lines = short_lines if candidate_side == 'short' else long_lines if candidate_side == 'long' else []
        if candidate_side in {'long', 'short'} and not _side_has_red_gate(candidate_lines):
            if not self._is_valid_number(planned_qty) or not self._is_valid_number(risk_usdt) or float(risk_usdt or 0) <= 0:
                entry_plan_detail += " / 상태진단: 후보 EV는 통과처럼 보이나 수량계산 risk=0"

        # Gather eligibility details
        cooldown_reasons = []
        now_ts = time.time()
        cooldown_remaining, cooldown_state = self._coin_selector_cooldown_remaining(
            symbol,
            cfg,
            now=now_ts
        )
        if cooldown_remaining > 0:
            last_r = "unknown"
            if isinstance(cooldown_state, dict):
                last_r = cooldown_state.get('last_reason') or "unknown"
            cooldown_reasons.append(f"candidate cooldown {cooldown_remaining / 60.0:.1f}m ({last_r})")

        # Bridge cooldown
        key = self._utbreakout_trace_key(symbol)
        last_attempt = float(self.utbreakout_auto_entry_bridge_last_attempt_ts.get(key, 0.0) or 0.0)
        cooldown_sec = float(cfg.get('utbreakout_auto_entry_bridge_cooldown_sec', 60.0) or 60.0)
        if now_ts - last_attempt < cooldown_sec:
            cooldown_reasons.append(f"bridge cooldown (elapsed {now_ts - last_attempt:.1f}s)")

        pos = await self.get_server_position(symbol, use_cache=False)
        has_open_position = pos is not None

        active_positions = []
        try:
            active_positions = await self.get_active_position_symbols(use_cache=False)
        except Exception:
            pass
        has_other_position = any(
            self._utbreakout_status_symbol_key(p) != self._utbreakout_status_symbol_key(symbol)
            for p in active_positions
        )

        auto_entry_enabled = bool(getattr(self, 'utbreakout_auto_entry_bridge_enabled', True))
        gate_next_symbol = (
            getattr(self, 'current_utbreakout_candidate_symbol', None)
            or getattr(self, 'current_coin_selector_symbol', None)
            or None
        )

        long_plan = self._get_utbot_filtered_breakout_entry_plan(symbol, 'long')
        long_eligibility = self._build_utbreakout_execution_eligibility(
            symbol=symbol,
            side='long',
            candidate_side=candidate_side,
            candidate_type=candidate_type,
            side_condition_ok=long_ok,
            risk_ok=bool(risk_ok),
            planned_qty=planned_qty,
            risk_usdt=risk_usdt,
            entry_plan_detail=entry_plan_detail,
            cooldown_reasons=cooldown_reasons,
            has_open_position=has_open_position,
            has_other_position=has_other_position,
            auto_entry_enabled=auto_entry_enabled,
            daily_risk_ok=bool(daily_ok),
            plan_lookup_ready=isinstance(long_plan, dict),
            cfg=cfg,
            scanner_source='manual_status',
            is_live_scanner_context=False,
            is_current_scanner_candidate=False,
            is_coinselector_top_candidate=None,
            next_scan_symbol=gate_next_symbol,
            evaluated_symbol=symbol,
            manual_status_only=True,
        )

        short_plan = self._get_utbot_filtered_breakout_entry_plan(symbol, 'short')
        short_eligibility = self._build_utbreakout_execution_eligibility(
            symbol=symbol,
            side='short',
            candidate_side=candidate_side,
            candidate_type=candidate_type,
            side_condition_ok=short_ok,
            risk_ok=bool(risk_ok),
            planned_qty=planned_qty,
            risk_usdt=risk_usdt,
            entry_plan_detail=entry_plan_detail,
            cooldown_reasons=cooldown_reasons,
            has_open_position=has_open_position,
            has_other_position=has_other_position,
            auto_entry_enabled=auto_entry_enabled,
            daily_risk_ok=bool(daily_ok),
            plan_lookup_ready=isinstance(short_plan, dict),
            cfg=cfg,
            scanner_source='manual_status',
            is_live_scanner_context=False,
            is_current_scanner_candidate=False,
            is_coinselector_top_candidate=None,
            next_scan_symbol=gate_next_symbol,
            evaluated_symbol=symbol,
            manual_status_only=True,
        )

        ready_side = (
            candidate_side
            if candidate_side == 'long' and long_ok
            else candidate_side
            if candidate_side == 'short' and short_ok
            else 'long'
            if long_ok
            else 'short'
            if short_ok
            else None
        )
        _record_status_evaluated(
            'OK',
            candidate_side=candidate_side,
            candidate_type=candidate_type,
            long_ok=long_ok,
            short_ok=short_ok,
            ready_side=ready_side,
            final=(
                f"LONG {'주문시도 대상' if long_eligibility['can_attempt'] else '조건통과 / 주문 안함' if long_ok else '대기'} / "
                f"SHORT {'주문시도 대상' if short_eligibility['can_attempt'] else '조건통과 / 주문 안함' if short_ok else '대기'}"
            ),
            selected_set=(
                f"Set{selected_set.get('id')} {selected_set.get('name')}"
                if isinstance(selected_set, dict) else ''
            ),
            current_position='see position scan context',
            decision_candle_ts=decision_ts,
            qty=planned_qty,
            entry_price=entry_price,
            margin=planned_margin,
            notional=planned_notional,
            risk_usdt=risk_usdt,
        )
        diagnostic_ready_side = (
            candidate_side
            if candidate_side == 'long' and long_ok
            else candidate_side
            if candidate_side == 'short' and short_ok
            else None
        )
        if diagnostic_ready_side:
            self._utbreakout_trace_event(
                symbol,
                'STATUS_DIAGNOSTIC_READY',
                'DIAGNOSTIC_ONLY',
                source='manual_status',
                side=diagnostic_ready_side,
                entry_tf=cfg.get('entry_timeframe'),
                htf=cfg.get('htf_timeframe'),
                effective_profile=cfg.get('effective_profile_version'),
                selected_set=(
                    f"Set{selected_set.get('id')} {selected_set.get('name')}"
                    if isinstance(selected_set, dict) else ''
                ),
                candidate_type=candidate_type,
                decision_candle_ts=decision_ts,
                qty=planned_qty,
                entry_price=entry_price,
                margin=planned_margin,
                notional=planned_notional,
                risk_usdt=risk_usdt,
                reason='manual /utbreak status is diagnostic only; live scanner must emit STATUS_READY',
            )
        else:
            self._utbreakout_trace_event(
                symbol,
                'STATUS_NOT_READY',
                'WAIT',
                source='manual_status',
                candidate_side=candidate_side,
                candidate_type=candidate_type,
                long_ok=long_ok,
                short_ok=short_ok,
                decision_candle_ts=decision_ts,
            )
        ut_label = f"{str(candidate_side or 'none').upper()} ({candidate_type})"
        scores = (auto_analysis or {}).get('scores') if isinstance(auto_analysis, dict) else {}
        if scores:
            score_line = (
                f"trend {scores.get('trend_score', 0):.1f} / chop {scores.get('chop_score', 0):.1f} / "
                f"vol {scores.get('volatility_score', 0):.1f} / breakout {scores.get('breakout_score', 0):.1f} / "
                f"momentum {scores.get('momentum_score', 0):.1f} / flow {scores.get('flow_score', 0):.1f}"
            )
        else:
            score_line = "AUTO OFF 또는 분석 대기"
        mode_label = 'AUTO' if cfg.get('auto_select_enabled') else 'MANUAL'
        set_status = '실거래 연결' if selected_set.get('status') == 'active' else 'planned only'
        position_scan_context = await self._build_utbreakout_position_scan_context_lines(symbol)
        micro_cfg = self._get_micro_auto_config()
        micro_plan = self.micro_auto_last_plan.get(symbol) if isinstance(self.micro_auto_last_plan, dict) else None
        micro_reject = self.micro_auto_last_rejects.get(symbol) if isinstance(self.micro_auto_last_rejects, dict) else None
        if micro_cfg.get('enabled'):
            if isinstance(status_micro_result, dict) and status_micro_result.get('accepted'):
                micro_line = (
                    f"Micro Auto: ON / {'DRY-RUN' if status_micro_result.get('dry_run') else 'LIVE'} / "
                    f"{float(status_micro_result.get('planned_margin', 0) or 0):.2f} margin / "
                    f"{float(status_micro_result.get('planned_notional', 0) or 0):.2f} notional / "
                    f"{int(float(status_micro_result.get('leverage', 0) or 0))}x"
                )
            elif isinstance(status_micro_result, dict):
                micro_line = (
                    f"Micro Auto: ON / 현재 거절 {status_micro_result.get('reject_code')}: "
                    f"{status_micro_result.get('reason')}"
                )
            elif isinstance(micro_plan, dict) and micro_plan.get('micro_auto'):
                micro_line = (
                    f"Micro Auto: ON / {'DRY-RUN' if micro_plan.get('dry_run') else 'LIVE'} / "
                    f"{float(micro_plan.get('planned_margin', 0) or 0):.2f} margin / "
                    f"{float(micro_plan.get('planned_notional', 0) or 0):.2f} notional / "
                    f"{int(float(micro_plan.get('leverage', 0) or 0))}x"
                )
            elif isinstance(micro_reject, dict):
                micro_line = f"Micro Auto: ON / 최근 거절 {micro_reject.get('reject_code')}: {micro_reject.get('reason')}"
            else:
                micro_line = "Micro Auto: ON / 아직 계획 없음"
        else:
            micro_line = "Micro Auto: OFF"
        order_path_line = format_utbreakout_order_path_summary(
            build_utbreakout_order_path_summary(
                cfg,
                exchange_mode=self.ctrl.get_exchange_mode() if getattr(self, 'ctrl', None) else None,
                micro_cfg=micro_cfg,
            )
        )
        status_side = candidate_side if candidate_side in {'long', 'short'} else 'long'
        status_filter_values = _market_quality_filter_values()
        status_feature_score = self._calculate_utbreakout_feature_score(status_side, cfg, status_filter_values)
        orderflow_line = (
            f"Orderflow: imb {_fmt(futures_context.get('rolling_orderbook_imbalance_pct'), 2)}% / "
            f"Δ {_fmt(futures_context.get('rolling_orderbook_imbalance_delta'), 2)} / "
            f"OFI {_fmt(futures_context.get('rolling_ofi_score'), 2)} / "
            f"samples {int(float(futures_context.get('rolling_ofi_samples') or 0))}"
        )
        oi_line = (
            f"OI/Funding: OI z {_fmt(futures_context.get('open_interest_delta_z'), 2)} / "
            f"1h {_fmt(futures_context.get('open_interest_change_1h'), 2)}% / "
            f"4h {_fmt(futures_context.get('open_interest_change_4h'), 2)}% / "
            f"accel {_fmt(futures_context.get('open_interest_acceleration'), 3)} / "
            f"funding {_fmt(futures_context.get('funding_rate'), 6)} / "
            f"fpct {_fmt(futures_context.get('funding_percentile_7d'), 0)}/{_fmt(futures_context.get('funding_percentile_30d'), 0)} / "
            f"L/S {_fmt(futures_context.get('long_short_ratio'), 3)} / "
            f"liq {_fmt(futures_context.get('liquidation_imbalance'), 2)}"
        )
        squeeze_line = (
            f"Squeeze: BB pct {_fmt(metrics.get('bb_width_percentile'), 2)} / "
            f"Keltner {'ON' if metrics.get('keltner_squeeze_on') is True else 'OFF' if metrics.get('keltner_squeeze_on') is False else 'n/a'} / "
            f"range {_fmt(metrics.get('range_expansion_ratio'), 2)} / state {metrics.get('squeeze_release_state') or 'n/a'}"
        )
        feature_line = (
            f"Feature Score: {_fmt(status_feature_score.get('score'), 1)} / "
            f"{status_feature_score.get('reason') or 'n/a'}"
        )
        cfg = apply_profit_opportunity_effective_overrides(cfg)
        adaptive_tfs = cfg.get("adaptive_timeframes", ["15m", "30m", "1h"])
        tp2 = float(cfg.get("second_take_profit_r_multiple", 3.50) or 3.50)
        vol_base = float(cfg.get("bias_continuation_min_volume_ratio", 0.40) or 0.40)
        vol_15m = float(cfg.get("bias_continuation_15m_min_volume_ratio", 0.45) or 0.45)

        compact_long = self._compact_side_gate_summary("long", long_ok, long_lines, execution_eligibility=long_eligibility)
        compact_short = self._compact_side_gate_summary("short", short_ok, short_lines, execution_eligibility=short_eligibility)

        long_gate_blockers = self._format_utbreakout_execution_blockers_for_display(
            "long",
            long_lines,
            long_eligibility,
        )
        short_gate_blockers = self._format_utbreakout_execution_blockers_for_display(
            "short",
            short_lines,
            short_eligibility,
        )

        long_gate_lbl = "주문시도 대상 - STATUS_READY → AUTO_ENTRY_BRIDGE → ENTRY_CALL 예상" if long_eligibility['can_attempt'] else f"차단 - {'; '.join(long_gate_blockers)}" if long_gate_blockers else "대기"
        short_gate_lbl = "주문시도 대상 - STATUS_READY → AUTO_ENTRY_BRIDGE → ENTRY_CALL 예상" if short_eligibility['can_attempt'] else f"차단 - {'; '.join(short_gate_blockers)}" if short_gate_blockers else "대기"

        long_final_lbl = "주문시도 대상" if long_eligibility['can_attempt'] else "조건통과 / 주문 안함" if long_ok else "대기"
        short_final_lbl = "주문시도 대상" if short_eligibility['can_attempt'] else "조건통과 / 주문 안함" if short_ok else "대기"

        active_side_preview_lines = self._build_utbreakout_active_side_preview_lines(
            candidate_side,
            long_lines,
            short_lines,
            compact_long,
            compact_short,
        )
        full_side_detail_lines = self._ordered_utbreakout_side_detail_lines(
            candidate_side,
            long_lines,
            short_lines,
        )

        text_lines = [
            "🚦 UT Breakout 조건 스테이터스",
            *build_utbreakout_effective_status_contract(cfg, daily_entries),
            *position_scan_context,
            *_crypto_safety_status_lines(self),
            "실행 게이트",
            f"LONG: {long_gate_lbl}",
            f"SHORT: {short_gate_lbl}",
            "",
            f"Runtime Profile: {cfg.get('runtime_profile', UTBREAKOUT_RUNTIME_PROFILE)}",
            f"Effective TF: entry {entry_tf} / htf {htf_tf} / adaptive {', '.join(map(str, adaptive_tfs))}",
            f"TF: 진입 {entry_tf} / HTF {htf_tf}",
            f"Adaptive TF: {'ON' if cfg.get('adaptive_timeframe_enabled') else 'OFF'} / {self._format_adaptive_timeframe_summary(adaptive_decision) if adaptive_decision else '고정 시간봉'}",
            f"마지막 마감봉: {_fmt_ts(decision_ts)} / close {_fmt(entry_price, 4)}",
            f"현재 UTBot 방향: {ut_label}",
            f"선택모드: {mode_label}",
            f"선택 Set: Set{selected_set.get('id')} {selected_set.get('name')} ({set_status})",
            f"선택 이유: {auto_reason or '수동 선택'}",
            entry_plan_detail,
            micro_line,
            order_path_line,
            *self._format_utbreakout_entry_diagnostics_lines(symbol),
            opposite_set_exit_detail,
            f"시장 레짐: {market_regime_context.get('summary') or '데이터 대기'}",
            f"AUTO 점수: {score_line}",
            orderflow_line,
            oi_line,
            squeeze_line,
            feature_line,
            self.get_coin_selector_symbol_summary(symbol),
            "주의: 빨간 필수 게이트만 진입 차단입니다. 노란 항목은 진입 차단이 아니라 수량/리스크 축소입니다.",
            f"최종: LONG {long_final_lbl} / SHORT {short_final_lbl}",
            "",
            "요약 신호등",
            compact_long,
            compact_short,
            "",
            *active_side_preview_lines,
            "",
            "전체 방향 상세",
            *full_side_detail_lines,
            "",
            f"UT 사유: {ut_reason}"
        ]
        return enforce_utbreakout_effective_status_contract(
            "\n".join(text_lines),
            cfg,
            daily_entries,
        )

    async def build_utbreakout_entry_analysis_text(self, symbol):
        """Read-only UT Breakout preflight report for Telegram diagnostics."""
        strategy_params = self.get_runtime_strategy_params()
        common_cfg = self.get_runtime_common_settings()
        active_strategy = str(strategy_params.get('active_strategy', 'utbot') or 'utbot').lower()
        cfg = self._get_utbot_filtered_breakout_config(strategy_params)
        cfg = apply_stable_utbreak_final_overrides(cfg)
        cfg = apply_profit_opportunity_effective_overrides(cfg)
        entry_tf = str(cfg.get('entry_timeframe', '15m') or '15m').lower()
        htf_tf = str(cfg.get('htf_timeframe', '1h') or '1h').lower()
        display_symbol = self.ctrl.format_symbol_for_display(symbol)

        def _fmt(value, digits=2):
            try:
                if value is None or not np.isfinite(float(value)):
                    return "n/a"
                return f"{float(value):.{digits}f}"
            except (TypeError, ValueError):
                return "n/a"

        def _fmt_ts(ms):
            try:
                ts = int(ms or 0)
                if ts <= 0:
                    return "n/a"
                return datetime.fromtimestamp(ts / 1000, timezone.utc).astimezone(
                    timezone(timedelta(hours=9))
                ).strftime('%m-%d %H:%M KST')
            except Exception:
                return "n/a"

        def _symbol_key(value):
            return str(value or '').upper().replace(':USDT', '').replace('/', '').strip()

        def _ok_text(state):
            if state is True:
                return "OK"
            if state == 'reduced':
                return "REDUCE"
            if state is False:
                return "BLOCK"
            return "WAIT"

        blockers = []
        waits = []
        notes = []
        symbol_key = _symbol_key(symbol)

        if self.is_upbit_mode():
            return "UT Breakout 진입 분석\n\n업비트 현물 모드에서는 UTBOT_FILTERED_BREAKOUT_V1을 사용하지 않습니다."

        if self.cfg.get('system_settings', {}).get('active_engine', CORE_ENGINE) != CORE_ENGINE:
            blockers.append("Signal 엔진이 활성 엔진이 아님")
        if not self.running:
            waits.append("SignalEngine running=False")
        if self.ctrl.is_paused:
            blockers.append("봇 일시정지 상태")
        if active_strategy not in UTBREAKOUT_STRATEGIES:
            blockers.append(f"현재 전략이 UT Breakout이 아님: {active_strategy.upper()}")

        scanner_enabled = bool(common_cfg.get('scanner_enabled', True))
        watchlist = list(self.get_runtime_watchlist() or [])
        active_symbols = set(self.active_symbols or set())
        active_keys = {_symbol_key(item) for item in active_symbols}
        watch_keys = {_symbol_key(item) for item in watchlist}
        scanner_active = self.scanner_active_symbol
        scanner_active_key = _symbol_key(scanner_active)
        coin_cfg = self._get_coin_selector_config()
        custom_symbols = normalize_coin_selector_custom_symbols(coin_cfg.get('custom_symbols'))
        custom_keys = {_symbol_key(item) for item in custom_symbols}

        all_positions = []
        position_fetch_error = None
        symbol_position = None
        other_positions = []
        try:
            all_positions = await asyncio.to_thread(self.exchange.fetch_positions)
            if not isinstance(all_positions, list):
                all_positions = []
            for pos in all_positions:
                try:
                    contracts = abs(float(pos.get('contracts', 0) or 0))
                except (TypeError, ValueError):
                    contracts = 0.0
                if contracts <= 0:
                    continue
                pos_symbol = pos.get('symbol') or ''
                if _symbol_key(pos_symbol) == symbol_key:
                    symbol_position = pos
                else:
                    other_positions.append(pos)
        except Exception as exc:
            position_fetch_error = str(exc)
            blockers.append(f"포지션 조회 실패: 실제 entry()도 안전상 중단 가능 ({exc})")

        position_keys = {_symbol_key(pos.get('symbol')) for pos in [symbol_position] if isinstance(pos, dict)}
        target_keys = active_keys | watch_keys | position_keys
        if scanner_enabled:
            if scanner_active_key and scanner_active_key == symbol_key:
                notes.append("scanner ON: 현재 scanner_active_symbol이 이 심볼")
            elif symbol_position:
                notes.append("scanner ON: 기존 포지션 안전관리 대상으로 포함")
            else:
                blockers.append("scanner ON: EWY가 scanner_active_symbol으로 lock-in되지 않으면 직접 진입 루프가 돌지 않음")
        else:
            if symbol_key not in target_keys:
                blockers.append("scanner OFF지만 watchlist/active_symbols/포지션 대상에 심볼이 없음")

        if symbol_key in custom_keys and not coin_cfg.get('custom_universe_enabled'):
            notes.append("custom_symbols에는 있으나 custom universe는 OFF")

        if symbol_position:
            blockers.append(f"동일 심볼 포지션 보유 중: {symbol_position.get('side')}")
        if other_positions:
            held = ", ".join(str(pos.get('symbol') or '?') for pos in other_positions[:3])
            blockers.append(f"단일 포지션 제한: 다른 포지션 보유 중 ({held})")

        try:
            ohlcv = await asyncio.to_thread(
                self.market_data_exchange.fetch_ohlcv,
                symbol,
                entry_tf,
                limit=300
            )
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        except Exception as exc:
            return (
                "UT Breakout 진입 분석\n\n"
                f"심볼: {display_symbol}\n"
                f"TF: {entry_tf}\n"
                f"OHLCV 조회 실패: {exc}\n"
                "이 경우 조건 계산과 실제 진입 모두 진행되지 않습니다."
            )

        if df is None or len(df) < 5:
            return f"UT Breakout 진입 분석\n\n심볼: {display_symbol}\nTF: {entry_tf}\n데이터 부족: {0 if df is None else len(df)}/5"

        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        closed = df.iloc[:-1].copy().dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
        if len(closed) < 5:
            return f"UT Breakout 진입 분석\n\n심볼: {display_symbol}\nTF: {entry_tf}\n유효 마감봉 데이터 부족"

        adaptive_state = {}
        if cfg.get('adaptive_timeframe_enabled'):
            adaptive_state = self.utbreakout_adaptive_tf_state.get(symbol, {}) or {}
            selected_tf = adaptive_state.get('selected_tf')
            if selected_tf and selected_tf != entry_tf:
                notes.append(f"Adaptive TF ON: 최근 선택 TF {selected_tf}. 이 분석은 config 진입 TF {entry_tf} 기준")
            else:
                notes.append("Adaptive TF ON: 실제 루프는 판단 시점에 TF를 다시 고를 수 있음")

        selected_set, auto_analysis, auto_reason = await self._resolve_utbreakout_selected_set(symbol, df, cfg)
        effective_cfg = dict(cfg)
        effective_cfg.update(selected_set.get('params', {}) if isinstance(selected_set, dict) else {})
        effective_cfg['active_set_id'] = selected_set.get('id', cfg.get('active_set_id', UTBREAKOUT_DEFAULT_SET_ID))
        effective_cfg['profile'] = f"set{effective_cfg['active_set_id']}"
        cfg = effective_cfg
        cfg = apply_stable_utbreak_final_overrides(cfg)
        cfg = apply_profit_opportunity_effective_overrides(cfg)
        entry_tf = str(cfg.get('entry_timeframe', entry_tf) or entry_tf).lower()
        htf_tf = str(cfg.get('htf_timeframe', htf_tf) or htf_tf).lower()

        decision_ts = int(closed.iloc[-1].get('timestamp') or 0)
        entry_price = float(closed.iloc[-1]['close'])
        last_processed_ts = int(self.last_processed_candle_ts.get(symbol, 0) or 0)
        if last_processed_ts >= decision_ts:
            waits.append("현재 마감봉은 이미 처리/프라임됨. 다음 마감봉까지 신규 진입 재시도 없음")

        ut_sig, ut_reason, ut_detail = self._calculate_utbot_signal(
            df,
            self._get_utbot_filtered_breakout_ut_params(cfg)
        )
        ut_detail = ut_detail or {}
        ut_bias_side = str(ut_detail.get('bias_side') or '').lower()
        candidate_side = ut_sig if ut_sig in {'long', 'short'} else ut_bias_side if ut_bias_side in {'long', 'short'} else None
        candidate_type = 'fresh_signal' if ut_sig in {'long', 'short'} else 'bias_state' if candidate_side else 'waiting'

        metrics = self._calculate_utbreakout_timeframe_metrics(closed, cfg)
        ema_fast_len = int(cfg.get('ema_fast', 50) or 50)
        ema_slow_len = int(cfg.get('ema_slow', 200) or 200)
        htf_ready = False
        htf_error = None
        htf_close = htf_ema_fast = htf_ema_slow = htf_gap_pct = np.nan
        htf_supertrend_direction = None
        htf_supertrend_reason = None
        try:
            htf_ohlcv = await asyncio.to_thread(
                self.market_data_exchange.fetch_ohlcv,
                symbol,
                htf_tf,
                limit=300
            )
            htf_df = pd.DataFrame(htf_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            for col in ['open', 'high', 'low', 'close', 'volume']:
                htf_df[col] = pd.to_numeric(htf_df[col], errors='coerce')
            htf_closed = htf_df.iloc[:-1].dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
            if len(htf_closed) >= ema_slow_len + 2:
                htf_close_series = htf_closed['close'].astype(float)
                htf_ema_fast = float(htf_close_series.ewm(span=ema_fast_len, adjust=False).mean().iloc[-1])
                htf_ema_slow = float(htf_close_series.ewm(span=ema_slow_len, adjust=False).mean().iloc[-1])
                htf_close = float(htf_close_series.iloc[-1])
                htf_gap_pct = abs(htf_ema_fast - htf_ema_slow) / max(abs(htf_close), 1e-9) * 100.0
                htf_ready = True
            else:
                htf_error = f"HTF 데이터 부족 {len(htf_closed)}/{ema_slow_len + 2}"
            htf_supertrend_direction, _, htf_supertrend_reason = self._calculate_utbot_filter_pack_supertrend_direction(
                htf_closed,
                length=10,
                multiplier=3.0
            )
        except Exception as exc:
            htf_error = str(exc)

        futures_context = {}
        try:
            futures_context = (
                (auto_analysis or {}).get('futures_context')
                if isinstance(auto_analysis, dict) and isinstance((auto_analysis or {}).get('futures_context'), dict)
                else None
            )
            if futures_context is None:
                futures_context = await self._fetch_utbreakout_futures_context(symbol)
            futures_context = dict(futures_context or {})
        except Exception as exc:
            futures_context = {'futures_context_error': str(exc)}
        market_regime_context = {}
        try:
            market_regime_context = await self._fetch_utbreakout_market_regime_context(cfg)
            market_regime_context = dict(market_regime_context or {})
        except Exception as exc:
            market_regime_context = {'error': str(exc)}

        daily_entries = self.db.get_daily_entry_count()
        _, daily_pnl = self.db.get_daily_stats()
        max_losses = int(cfg.get('max_consecutive_losses', 3) or 3)
        recent_pnls = self._get_recent_strategy_closed_trade_pnls(
            {
                ENTRY_STRATEGY_UT_BREAKOUT,
                UTBOT_FILTERED_BREAKOUT_STRATEGY,
                UTBOT_ADAPTIVE_TIMEFRAME_STRATEGY,
            },
            max_losses,
            today_only=True,
        )
        daily_ok = True
        daily_detail = f"PnL {_fmt(daily_pnl, 2)} / trades {daily_entries}/{int(cfg.get('max_daily_trades', 0) or 0)}"
        if float(cfg.get('daily_max_loss_usdt', 0) or 0) > 0 and float(daily_pnl or 0) <= -float(cfg['daily_max_loss_usdt']):
            daily_ok = False
            daily_detail = f"일손실 한도 도달 PnL {_fmt(daily_pnl, 2)}"
        elif int(cfg.get('max_daily_trades', 0) or 0) > 0 and daily_entries >= int(cfg['max_daily_trades']):
            daily_ok = False
            daily_detail = f"일일 거래수 한도 {daily_entries}/{int(cfg['max_daily_trades'])}"
        elif bool(cfg.get('daily_profit_target_enabled', False)) and float(daily_pnl or 0) >= float(cfg.get('daily_profit_target_usdt', 0) or 0):
            daily_ok = False
            daily_detail = f"일 목표수익 도달 {_fmt(daily_pnl, 2)}"
        elif len(recent_pnls) >= max_losses and all(float(pnl) < 0 for pnl in recent_pnls[:max_losses]):
            daily_ok = False
            daily_detail = f"연속 손절 {max_losses}회"
        if not daily_ok:
            blockers.append(daily_detail)

        filter_values = {
            'entry_price': entry_price,
            'open': metrics.get('open'),
            'high': metrics.get('high'),
            'low': metrics.get('low'),
            'close': metrics.get('close'),
            'rsi': metrics.get('rsi'),
            'macd_hist': metrics.get('macd_hist'),
            'macd_hist_prev': metrics.get('macd_hist_prev'),
            'roc_pct': metrics.get('roc_pct'),
            'cci': metrics.get('cci'),
            'stoch_k': metrics.get('stoch_k'),
            'stoch_d': metrics.get('stoch_d'),
            'adx': metrics.get('adx'),
            'plus_di': metrics.get('plus_di'),
            'minus_di': metrics.get('minus_di'),
            'adx_reason': metrics.get('adx_reason'),
            'atr_pct': metrics.get('atr_pct'),
            'ema50': metrics.get('ema_fast'),
            'ema50_prev': metrics.get('ema_fast_prev'),
            'ema200': metrics.get('ema_slow'),
            'donchian_high_prev': metrics.get('donchian_high_prev'),
            'donchian_low_prev': metrics.get('donchian_low_prev'),
            'donchian_width_pct': metrics.get('donchian_width_pct'),
            'bb_upper': metrics.get('bb_upper'),
            'bb_lower': metrics.get('bb_lower'),
            'bb_mid': metrics.get('bb_mid'),
            'bb_width_pct': metrics.get('bb_width_pct'),
            'bb_width_prev_pct': metrics.get('bb_width_prev_pct'),
            'bb_width_min_pct': metrics.get('bb_width_min_pct'),
            'bb_width_percentile': metrics.get('bb_width_percentile'),
            'keltner_upper': metrics.get('keltner_upper'),
            'keltner_lower': metrics.get('keltner_lower'),
            'keltner_mid': metrics.get('keltner_mid'),
            'keltner_width_pct': metrics.get('keltner_width_pct'),
            'keltner_width_prev_pct': metrics.get('keltner_width_prev_pct'),
            'keltner_squeeze_on': metrics.get('keltner_squeeze_on'),
            'range_expansion_ratio': metrics.get('range_expansion_ratio'),
            'range_compression_ratio': metrics.get('range_compression_ratio'),
            'squeeze_release_state': metrics.get('squeeze_release_state'),
            'vwap': metrics.get('vwap'),
            'vwap_slope': metrics.get('vwap_slope'),
            'vwap_reason': metrics.get('vwap_reason'),
            'volume_ratio': metrics.get('volume_ratio'),
            'obv_slope_ratio': metrics.get('obv_slope_ratio'),
            'mfi': metrics.get('mfi'),
            'psar_direction': metrics.get('psar_direction'),
            'psar': metrics.get('psar'),
            'psar_reason': metrics.get('psar_reason'),
            'ichimoku_bias': metrics.get('ichimoku_bias'),
            'ichimoku_top': metrics.get('ichimoku_top'),
            'ichimoku_bottom': metrics.get('ichimoku_bottom'),
            'session_hour_kst': metrics.get('session_hour_kst'),
            'auto_scores': (auto_analysis or {}).get('scores') if isinstance(auto_analysis, dict) else {},
            'mtf_metrics': (auto_analysis or {}).get('timeframes') if isinstance(auto_analysis, dict) else {},
            'chop': metrics.get('chop'),
            'chop_reason': metrics.get('chop_reason'),
            'vortex_plus': metrics.get('vortex_plus'),
            'vortex_minus': metrics.get('vortex_minus'),
            'vortex_reason': metrics.get('vortex_reason'),
            'aroon_up': metrics.get('aroon_up'),
            'aroon_down': metrics.get('aroon_down'),
            'aroon_reason': metrics.get('aroon_reason'),
            'htf_ready': htf_ready,
            'htf_error': htf_error,
            'htf_close': htf_close,
            'htf_ema_fast': htf_ema_fast,
            'htf_ema_slow': htf_ema_slow,
            'htf_gap_pct': htf_gap_pct,
            'htf_supertrend_direction': htf_supertrend_direction,
            'htf_supertrend_reason': htf_supertrend_reason,
            'funding_rate': futures_context.get('funding_rate'),
            'next_funding_time': futures_context.get('next_funding_time'),
            'open_interest_delta_pct': futures_context.get('open_interest_delta_pct'),
            'open_interest_delta_z': futures_context.get('open_interest_delta_z'),
            'open_interest_acceleration': futures_context.get('open_interest_acceleration'),
            'open_interest_hist_samples': futures_context.get('open_interest_hist_samples'),
            'taker_buy_sell_ratio': futures_context.get('taker_buy_sell_ratio'),
            'long_short_ratio': futures_context.get('long_short_ratio'),
            'orderbook_imbalance_pct': futures_context.get('orderbook_imbalance_pct'),
            'rolling_orderbook_imbalance_pct': futures_context.get('rolling_orderbook_imbalance_pct'),
            'rolling_orderbook_imbalance_delta': futures_context.get('rolling_orderbook_imbalance_delta'),
            'rolling_ofi_score': futures_context.get('rolling_ofi_score'),
            'rolling_ofi_samples': futures_context.get('rolling_ofi_samples'),
            'futures_spread_pct': futures_context.get('futures_spread_pct'),
            'bid_depth_usdt': futures_context.get('bid_depth_usdt'),
            'ask_depth_usdt': futures_context.get('ask_depth_usdt'),
            'basis_pct': futures_context.get('basis_pct'),
            'prediction_up_probability': futures_context.get('prediction_up_probability'),
            'prediction_edge': futures_context.get('prediction_edge'),
            'prediction_score': futures_context.get('prediction_score'),
            'prediction_title': futures_context.get('prediction_title'),
            'market_regime_context': market_regime_context,
        }
        filter_values = self._enrich_utbreakout_trend_health_values(filter_values, closed, cfg, None)
        filter_values = self._enrich_utbreakout_strategy_quality_values(filter_values, closed, cfg)
        feature_score_analysis = self._calculate_utbreakout_feature_score('long', cfg, filter_values)
        filter_values['feature_score'] = feature_score_analysis
        long_filter_items = self._evaluate_utbreakout_set_filter_items('long', selected_set, cfg, filter_values)
        bias_continuation_ok = True
        bias_continuation_summary = "fresh/waiting"
        if candidate_side == 'long' and candidate_type == 'bias_state':
            bias_status = {
                'candidate_type': candidate_type,
                'decision_candle_ts': decision_ts,
                'ut_signal_ts': ut_detail.get('signal_ts'),
                'auto_selected_set_id': selected_set.get('id') if isinstance(selected_set, dict) else None,
                'entry_timeframe': entry_tf,
                'adaptive_timeframe_decision': {
                    'selected_score': adaptive_state.get('selected_score'),
                    'selected_tf': adaptive_state.get('selected_tf'),
                    'decision': adaptive_state.get('last_decision'),
                },
            }
            bias_continuation = self._evaluate_utbreakout_bias_continuation(
                'long',
                cfg,
                bias_status,
                filter_values,
                selected_set,
            )
            bias_continuation_ok = bias_continuation.get('state') is not False
            bias_continuation_summary = bias_continuation.get('summary') or "n/a"
            if not bias_continuation_ok:
                blockers.append(f"Bias continuation 미통과: {bias_continuation_summary}")
        market_quality_analysis = self._evaluate_utbreakout_market_quality('long', cfg, filter_values)
        selector_quality_analysis = self._build_utbreakout_selector_quality(symbol)
        adaptation_analysis = self._build_utbreakout_strategy_adaptation(
            symbol,
            'long',
            cfg,
            selected_set,
            filter_values,
        )
        trend_health_analysis = adaptation_analysis.get('trend_health') if isinstance(adaptation_analysis.get('trend_health'), dict) else {}
        strategy_quality_analysis = adaptation_analysis.get('strategy_quality') if isinstance(adaptation_analysis.get('strategy_quality'), dict) else {}
        quality_score_v2_analysis = self._build_utbreakout_quality_score_v2(
            'long',
            cfg,
            {
                'candidate_type': candidate_type if candidate_side == 'long' else 'waiting',
                'entry_timeframe': entry_tf,
                'adaptive_timeframe_decision': {
                    'selected_score': adaptive_state.get('selected_score'),
                    'selected_tf': adaptive_state.get('selected_tf'),
                    'decision': adaptive_state.get('last_decision'),
                },
            },
            filter_values,
            trend_health=trend_health_analysis,
            strategy_quality=strategy_quality_analysis,
            market_quality=market_quality_analysis,
            selector_quality=selector_quality_analysis,
        )
        if quality_score_v2_analysis.get('state') is False:
            blockers.append(f"통합 품질 점수 미통과: {quality_score_v2_analysis.get('summary')}")
        classified_long_filter_items = []
        hard_failed_filters = []
        soft_failed_filters = []
        for item in long_filter_items:
            if item.get('state') is True:
                classified_long_filter_items.append(item)
                continue
            name = item.get('name') or ''
            detail = item.get('detail') or ''
            code = item.get('code') or ''
            is_core_set_failure = (
                bool(cfg.get('selected_set_core_filter_hard_block_enabled', True))
                and (
                    name in UTBREAKOUT_CORE_SET_FILTER_HARD_NAMES
                    or code in UTBREAKOUT_CORE_SET_FILTER_HARD_CODES
                    or bool(item.get('core_set_hard_block'))
                )
            )
            if is_core_set_failure or name in {'ATR% 변동성', '손익비', '스프레드', '유동성'} or code == 'REJECTED_RISK_REWARD_LOW':
                state, multiplier, classified_detail = False, 0.0, detail
            elif name in {'상대 거래량', 'Rolling OFI 확인', 'Futures 수급 불균형', 'Spread/Depth 비용'}:
                state, multiplier, classified_detail = _classify_set_filter_result(
                    'long',
                    False,
                    detail,
                    metrics=filter_values,
                    cfg=cfg,
                )
            else:
                state = 'reduced'
                multiplier = 0.65 if item.get('state') is None else 0.50
                classified_detail = f"{detail}; set confirmation soft fail"
            classified = dict(item)
            classified.update({
                'state': state,
                'risk_multiplier': multiplier,
                'detail': classified_detail,
            })
            classified_long_filter_items.append(classified)
            if state is False:
                hard_failed_filters.append(classified)
            else:
                soft_failed_filters.append(classified)
        failed_filters = hard_failed_filters
        set_filter_analysis_state = (
            False if hard_failed_filters else
            'reduced' if soft_failed_filters else
            True
        )
        if selected_set.get('status') != 'active':
            blockers.append(f"Set{selected_set.get('id')}은 실거래 연결 상태가 아님")
        if candidate_side != 'long':
            blockers.append(f"UTBot LONG 후보 아님: 현재 {str(candidate_side or 'none').upper()} ({candidate_type})")
        if hard_failed_filters:
            top_failed = "; ".join(
                f"{item.get('name')}={item.get('detail')}"
                for item in hard_failed_filters[:4]
            )
            blockers.append(f"선택 Set LONG 필터 미통과: {top_failed}")
        if soft_failed_filters:
            notes.append(
                "선택 Set 보조 확인 감액: "
                + "; ".join(
                    f"{item.get('name')} x{float(item.get('risk_multiplier', 1.0)):.2f}"
                    for item in soft_failed_filters[:4]
                )
            )

        if not self.is_trade_direction_allowed('long'):
            blockers.append(self.format_trade_direction_block_reason('long'))

        balance_error = None
        total_balance = free_balance = mmr = 0.0
        try:
            total_balance, free_balance, mmr = await self.get_balance_info()
            if float(free_balance or 0) <= 0:
                blockers.append(f"잔고 부족: free {_fmt(free_balance, 2)} USDT")
        except Exception as exc:
            balance_error = str(exc)
            blockers.append(f"잔고 조회 실패: {exc}")

        lev = int(max(1.0, float(common_cfg.get('leverage', 5) or 5)))
        risk_plan = None
        risk_error = None
        atr_value = metrics.get('atr')
        try:
            if not self._is_valid_number(atr_value) or float(atr_value) <= 0:
                raise ValueError("ATR 값 없음")
            balance_for_risk = total_balance if total_balance > 0 else free_balance
            risk_budget = resolve_utbreakout_risk_budget(balance_for_risk, cfg)
            risk_plan = calculate_risk_plan(
                side='long',
                entry_price=entry_price,
                atr_value=atr_value,
                stop_atr_multiplier=cfg.get('stop_atr_multiplier', 1.5),
                ut_stop=ut_detail.get('curr_stop'),
                take_profit_r_multiple=cfg.get('take_profit_r_multiple', 3.50),
                min_risk_reward=cfg.get('min_risk_reward', 2.0),
                balance_usdt=balance_for_risk,
                risk_per_trade_percent=risk_budget['risk_per_trade_percent'],
                max_risk_per_trade_usdt=risk_budget['max_risk_per_trade_usdt'],
                leverage=lev,
            )
            risk_plan = cap_utbreakout_risk_plan_to_margin(
                risk_plan,
                free_balance=free_balance,
                leverage=lev,
                entry_price=entry_price,
            )
        except Exception as exc:
            risk_error = str(exc)
            if "risk budget unavailable" in risk_error:
                blockers.append("리스크 예산 없음: 최종 리스크 배율 또는 risk budget이 0이라 수량 계산 대기")
            else:
                blockers.append(f"리스크 계획 생성 실패: {exc}")

        min_notional = 0.0
        min_notional_source = "unknown"
        markets = {}
        market = {}
        try:
            markets = await asyncio.to_thread(self.exchange.load_markets)
            market = markets.get(symbol) or markets.get(f"{symbol}:USDT") or {}
            limits = market.get('limits', {}) if isinstance(market, dict) else {}
            info = market.get('info', {}) if isinstance(market, dict) else {}
            filters = info.get('filters', []) if isinstance(info, dict) else []
            candidates = []
            if isinstance(limits.get('cost', {}), dict):
                candidates.append(limits.get('cost', {}).get('min'))
            candidates.extend([info.get('notional'), info.get('minNotional')])
            if isinstance(filters, list):
                for item in filters:
                    if isinstance(item, dict) and item.get('filterType') in ('MIN_NOTIONAL', 'NOTIONAL'):
                        candidates.extend([item.get('notional'), item.get('minNotional')])
            parsed = []
            for item in candidates:
                try:
                    value = float(item)
                    if value > 0:
                        parsed.append(value)
                except (TypeError, ValueError):
                    continue
            if parsed:
                min_notional = min(parsed)
                min_notional_source = "exchange"
        except Exception as exc:
            notes.append(f"min notional 조회 실패: {exc}")
        if min_notional <= 0 and getattr(self.exchange, 'id', '') == 'binance':
            default_type = str(getattr(self.exchange, 'options', {}).get('defaultType', '')).lower()
            if default_type in ('future', 'futures'):
                min_notional = 100.0
                min_notional_source = "binance fallback"

        effective_plan = risk_plan
        micro_cfg = self._get_micro_auto_config()
        micro_line = "Micro Auto: OFF"
        if micro_cfg.get('enabled') and risk_plan:
            micro_plan = build_micro_entry_plan(
                side='long',
                entry_price=entry_price,
                atr_value=atr_value,
                ut_stop=ut_detail.get('curr_stop'),
                base_plan=dict(risk_plan, stop_atr_multiplier=cfg.get('stop_atr_multiplier', 1.5)),
                cfg=micro_cfg,
                selected_set=selected_set,
                auto_scores=(auto_analysis or {}).get('scores') if isinstance(auto_analysis, dict) else {},
                selected_timeframe=entry_tf,
                total_equity_usdt=total_balance,
                free_usdt=free_balance,
                min_notional_usdt=min_notional,
            )
            if micro_plan.get('accepted'):
                effective_plan = micro_plan
                lev = int(max(1, float(micro_plan.get('leverage', lev) or lev)))
                micro_line = (
                    f"Micro Auto: ON / {'DRY-RUN' if micro_plan.get('dry_run') else 'LIVE'} / "
                    f"notional {_fmt(micro_plan.get('planned_notional'), 2)} / "
                    f"margin {_fmt(micro_plan.get('planned_margin'), 2)} / {lev}x"
                )
                if micro_plan.get('dry_run', True) or not micro_plan.get('live_enabled', False):
                    blockers.append("Micro Auto dry-run/live-lock: 실제 주문 전송 안 함")
            else:
                micro_line = f"Micro Auto: ON / 거절 {micro_plan.get('reject_code')}: {micro_plan.get('reason')}"
                blockers.append(micro_line)

        qty = None
        qty_notional = 0.0
        target_notional = 0.0
        max_notional = float(free_balance or 0.0) * float(lev) * 0.98
        if effective_plan:
            try:
                target_notional = float(effective_plan.get('qty', 0) or 0) * entry_price
                if min_notional > 0 and target_notional < min_notional:
                    blockers.append(
                        f"최소 주문금액 미달: 계획 {_fmt(target_notional, 2)} < 필요 {_fmt(min_notional, 2)} USDT"
                    )
                if target_notional > max_notional:
                    blockers.append(
                        f"증거금 부족: 계획 {_fmt(target_notional, 2)} > 가능 {_fmt(max_notional, 2)} USDT"
                    )
                qty = self.safe_amount(symbol, target_notional / max(entry_price, 1e-9))
                qty_notional = float(qty) * entry_price
                if min_notional > 0 and qty_notional < min_notional:
                    blockers.append(
                        f"수량 정밀도 후 최소금액 미달: {_fmt(qty_notional, 2)} < {_fmt(min_notional, 2)} USDT"
                    )
                if float(qty) <= 0:
                    blockers.append(f"주문 수량 계산 오류: {qty}")
            except Exception as exc:
                blockers.append(f"주문 수량/정밀도 분석 실패: {exc}")

        last_status = self.last_utbot_filtered_breakout_status.get(symbol, {}) or {}
        last_status_code = last_status.get('reject_code') or last_status.get('accepted_code') or last_status.get('reason') or '없음'
        last_status_ts = int(last_status.get('decision_candle_ts') or 0)
        if last_status_ts and last_status_ts != decision_ts:
            notes.append(f"최근 엔진 판단은 현재 마감봉과 다름: {_fmt_ts(last_status_ts)} / 현재 {_fmt_ts(decision_ts)}")

        recent_events = []
        try:
            events = read_utbreakout_diagnostic_events(days=2)
            recent_events = [
                event for event in events
                if _symbol_key(event.get('symbol')) == symbol_key
            ][-5:]
        except Exception as exc:
            notes.append(f"진단 로그 읽기 실패: {exc}")

        if self.ctrl._telegram_event_alerts_only():
            notes.append("Telegram event-only 모드: 진입 차단/최소금액/증거금 알림 일부가 숨겨질 수 있음")

        core_ok = (
            candidate_side == 'long'
            and selected_set.get('status') == 'active'
            and bias_continuation_ok
            and quality_score_v2_analysis.get('state') is not False
            and not hard_failed_filters
            and daily_ok
            and risk_plan is not None
        )
        blocker_lines = list(dict.fromkeys(str(item) for item in blockers if str(item).strip()))
        wait_lines = list(dict.fromkeys(str(item) for item in waits if str(item).strip()))
        note_lines = list(dict.fromkeys(str(item) for item in notes if str(item).strip()))
        if blocker_lines:
            verdict = "차단/대기 원인 있음"
        elif wait_lines:
            verdict = "조건은 대체로 충족, 다음 봉 또는 다음 루프 대기"
        elif core_ok:
            verdict = "현재 15분 기준으로는 주문 시도 가능 상태"
        else:
            verdict = "조건 미충족"

        filter_lines = [
            f"- {item.get('name')}: {_ok_text(item.get('state'))} / {item.get('detail')}"
            for item in classified_long_filter_items[:12]
        ] or ["- 선택 Set 필터 없음"]
        event_lines = []
        for event in recent_events:
            dt = event.get('_dt')
            dt_text = dt.astimezone(timezone(timedelta(hours=9))).strftime('%m-%d %H:%M') if dt else 'unknown'
            code = event.get('code') or event.get('event') or 'UNKNOWN'
            reason = str(event.get('reason') or '').replace('\n', ' ')
            if len(reason) > 110:
                reason = reason[:107] + "..."
            event_lines.append(f"- {dt_text} {code}: {reason}")
        if not event_lines:
            event_lines = ["- 최근 2일 EWY/해당 심볼 진단 로그 없음"]

        summary_blockers = blocker_lines or ["없음"]
        summary_waits = wait_lines or ["없음"]
        summary_notes = note_lines[:6] or ["없음"]
        plan_summary = (
            f"risk {_fmt(effective_plan.get('risk_usdt'), 4)} / "
            f"qty {qty if qty is not None else _fmt(effective_plan.get('qty'), 6)} / "
            f"notional {_fmt(target_notional, 2)} / margin {_fmt(effective_plan.get('planned_margin'), 2)} / "
            f"SL {_fmt(effective_plan.get('stop_loss'), 4)} / TP {_fmt(effective_plan.get('take_profit'), 4)}"
            if effective_plan else
            f"없음 ({risk_error or balance_error or '계산 대기'})"
        )
        active_summary = (
            f"scanner {'ON' if scanner_enabled else 'OFF'} / "
            f"scanner_active {scanner_active or 'none'} / "
            f"watchlist {', '.join(watchlist[:4]) if watchlist else 'none'} / "
            f"active_symbols {', '.join(sorted(active_symbols)[:4]) if active_symbols else 'none'}"
        )

        lines = [
            "UT Breakout 진입 분석",
            *build_utbreakout_effective_status_contract(cfg, daily_entries),
            f"심볼: {display_symbol}",
            f"결론: {verdict}",
            "",
            "1) 실행 경로",
            f"- 전략: {active_strategy.upper()}",
            f"- {active_summary}",
            f"- customcoins: {'ON' if coin_cfg.get('custom_universe_enabled') else 'OFF'} / {', '.join(custom_symbols) if custom_symbols else 'none'}",
            f"- 일시정지: {'YES' if self.ctrl.is_paused else 'NO'} / running: {'YES' if self.running else 'NO'}",
            "",
            "2) 15분봉 조건",
            f"- TF: entry {entry_tf} / HTF {htf_tf}",
            f"- 마감봉: {_fmt_ts(decision_ts)} / close {_fmt(entry_price, 4)}",
            f"- UT 후보: {str(candidate_side or 'none').upper()} ({candidate_type}) / fresh {str(ut_sig or 'none').upper()} / bias {str(ut_bias_side or 'none').upper()}",
            f"- 선택 Set: Set{selected_set.get('id')} {selected_set.get('name')} ({selected_set.get('status')})",
            f"- AUTO 이유: {auto_reason or '수동 선택'}",
            f"- LONG 핵심: UT {_ok_text(candidate_side == 'long')} / BiasCont {_ok_text(bias_continuation_ok)} / QScore {_ok_text(quality_score_v2_analysis.get('state'))} / Set필터 {_ok_text(set_filter_analysis_state)} / 일일리스크 {_ok_text(daily_ok)} / 리스크계획 {_ok_text(risk_plan is not None)}",
            f"- Bias continuation: {bias_continuation_summary}",
            f"- 통합 품질 점수: {quality_score_v2_analysis.get('summary')}",
            f"- Feature Score: {_fmt(feature_score_analysis.get('score'), 1)} / {feature_score_analysis.get('reason')}",
            f"- Orderflow: imb {_fmt(futures_context.get('rolling_orderbook_imbalance_pct'), 2)}% / Δ {_fmt(futures_context.get('rolling_orderbook_imbalance_delta'), 2)} / OFI {_fmt(futures_context.get('rolling_ofi_score'), 2)} / samples {int(float(futures_context.get('rolling_ofi_samples') or 0))}",
            f"- OI/Funding: OI z {_fmt(futures_context.get('open_interest_delta_z'), 2)} / 1h {_fmt(futures_context.get('open_interest_change_1h'), 2)}% / 4h {_fmt(futures_context.get('open_interest_change_4h'), 2)}% / accel {_fmt(futures_context.get('open_interest_acceleration'), 3)} / funding {_fmt(futures_context.get('funding_rate'), 6)} / fpct {_fmt(futures_context.get('funding_percentile_7d'), 0)}/{_fmt(futures_context.get('funding_percentile_30d'), 0)} / L/S {_fmt(futures_context.get('long_short_ratio'), 3)} / liq {_fmt(futures_context.get('liquidation_imbalance'), 2)}",
            f"- Squeeze: BB pct {_fmt(metrics.get('bb_width_percentile'), 2)} / Keltner {'ON' if metrics.get('keltner_squeeze_on') is True else 'OFF' if metrics.get('keltner_squeeze_on') is False else 'n/a'} / range {_fmt(metrics.get('range_expansion_ratio'), 2)} / state {metrics.get('squeeze_release_state') or 'n/a'}",
            f"- 일일리스크 상세: {daily_detail}",
            "",
            "3) LONG 필터 상세",
            *filter_lines,
            "",
            "4) 주문 전 체크",
            f"- 방향필터 LONG: {_ok_text(self.is_trade_direction_allowed('long'))}",
            f"- 잔고: total {_fmt(total_balance, 2)} / free {_fmt(free_balance, 2)} / MMR {_fmt(mmr, 2)}",
            f"- minNotional: {_fmt(min_notional, 2)} USDT ({min_notional_source})",
            f"- 계획: {plan_summary}",
            f"- 가능 notional: {_fmt(max_notional, 2)} USDT @ {lev}x",
            f"- {micro_line}",
            "",
            "5) 차단 원인",
            *[f"- {item}" for item in summary_blockers[:10]],
            "",
            "6) 대기/주의",
            *[f"- {item}" for item in summary_waits[:6]],
            *[f"- {item}" for item in summary_notes],
            "",
            "7) 최근 엔진/로그",
            f"- last_status: {last_status_code}",
            f"- last_status_candle: {_fmt_ts(last_status_ts)}",
            *event_lines,
            "",
            "복붙용 요약",
            f"symbol={symbol} verdict={verdict} tf={entry_tf} candle={_fmt_ts(decision_ts)} candidate={candidate_side}/{candidate_type} set=Set{selected_set.get('id')} blockers={' | '.join(summary_blockers[:5])}",
        ]
        return "\n".join(lines)

    async def _evaluate_utbreakout_opposite_set_exit(self, symbol, df, fb_cfg, current_side, pos):
        if not bool(fb_cfg.get('opposite_set_exit_enabled', False)):
            return False, None

        current_side = str(current_side or '').lower()
        if current_side not in {'long', 'short'}:
            return False, "Opposite set exit skipped: unknown current side"
        opposite_side = 'short' if current_side == 'long' else 'long'

        exit_sig, ut_exit_reason, _ = self._calculate_utbot_signal(
            df,
            self._get_utbot_filtered_breakout_ut_params(fb_cfg)
        )
        if exit_sig != opposite_side:
            return False, f"Opposite set exit wait: no fresh UT {opposite_side.upper()} signal"

        min_hold_candles = int(fb_cfg.get('opposite_set_exit_min_hold_candles', 3) or 0)
        hold_candles = None
        if min_hold_candles > 0:
            open_trade = self.db.get_latest_open_trade(symbol)
            entry_time_text = (open_trade or {}).get('entry_time')
            if not entry_time_text:
                return False, "Opposite set exit blocked: entry time unknown"
            try:
                entry_dt = datetime.fromisoformat(str(entry_time_text).replace('Z', '+00:00'))
                if entry_dt.tzinfo is None:
                    entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                elapsed_ms = (datetime.now(timezone.utc) - entry_dt.astimezone(timezone.utc)).total_seconds() * 1000.0
                candle_ms = self._timeframe_to_ms(fb_cfg.get('entry_timeframe', '15m'))
                hold_candles = elapsed_ms / max(float(candle_ms), 1.0)
            except Exception as exc:
                return False, f"Opposite set exit blocked: entry time parse failed {exc}"
            if hold_candles < min_hold_candles:
                return False, f"Opposite set exit wait: hold {hold_candles:.1f}/{min_hold_candles} candles"

        min_pnl_enabled = bool(fb_cfg.get('opposite_set_exit_min_pnl_enabled', False))
        min_pnl_usdt = float(fb_cfg.get('opposite_set_exit_min_pnl_usdt', 0.0) or 0.0)
        try:
            current_pnl = float((pos or {}).get('unrealizedPnl', 0.0) or 0.0)
        except (TypeError, ValueError):
            current_pnl = 0.0
        if min_pnl_enabled and current_pnl < min_pnl_usdt:
            return False, f"Opposite set exit wait: PnL {current_pnl:.2f} < min {min_pnl_usdt:.2f} USDT"

        closed = df.iloc[:-1].copy().reset_index(drop=True)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            closed[col] = pd.to_numeric(closed[col], errors='coerce')
        closed = closed.dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
        if len(closed) < 30:
            return False, f"Opposite set exit wait: data shortage {len(closed)}/30"

        selected_set, auto_analysis, auto_reason = await self._resolve_utbreakout_selected_set(symbol, df, fb_cfg)
        selected_filters = list(selected_set.get('entry_filters') or [])
        if not selected_filters:
            return False, f"Opposite set exit blocked: Set{selected_set.get('id')} has no confirmation filters"

        metrics = self._calculate_utbreakout_timeframe_metrics(closed, fb_cfg)
        ema_fast_len = int(fb_cfg.get('ema_fast', 50) or 50)
        ema_slow_len = int(fb_cfg.get('ema_slow', 200) or 200)
        htf_tf = str(fb_cfg.get('htf_timeframe', '1h') or '1h')
        htf_ready = False
        htf_error = None
        htf_close = htf_ema_fast = htf_ema_slow = htf_gap_pct = np.nan
        htf_supertrend_direction = None
        htf_supertrend_reason = None
        try:
            htf_ohlcv = await asyncio.to_thread(self.market_data_exchange.fetch_ohlcv, symbol, htf_tf, limit=300)
            htf_df = pd.DataFrame(htf_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            for col in ['open', 'high', 'low', 'close', 'volume']:
                htf_df[col] = pd.to_numeric(htf_df[col], errors='coerce')
            htf_closed = htf_df.iloc[:-1].dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
            if len(htf_closed) >= ema_slow_len + 2:
                htf_close_series = htf_closed['close'].astype(float)
                htf_ema_fast = float(htf_close_series.ewm(span=ema_fast_len, adjust=False).mean().iloc[-1])
                htf_ema_slow = float(htf_close_series.ewm(span=ema_slow_len, adjust=False).mean().iloc[-1])
                htf_close = float(htf_close_series.iloc[-1])
                htf_gap_pct = abs(htf_ema_fast - htf_ema_slow) / max(abs(htf_close), 1e-9) * 100.0
                htf_ready = True
            else:
                htf_error = f"HTF data shortage {len(htf_closed)}/{ema_slow_len + 2}"
            htf_supertrend_direction, _, htf_supertrend_reason = self._calculate_utbot_filter_pack_supertrend_direction(
                htf_closed,
                length=10,
                multiplier=3.0
            )
        except Exception as exc:
            htf_error = str(exc)

        futures_context = {}
        try:
            futures_context = (
                (auto_analysis or {}).get('futures_context')
                if isinstance(auto_analysis, dict) and isinstance((auto_analysis or {}).get('futures_context'), dict)
                else None
            )
            if futures_context is None:
                futures_context = await self._fetch_utbreakout_futures_context(symbol)
            futures_context = dict(futures_context or {})
        except Exception:
            futures_context = {}
        market_regime_context = {}
        try:
            market_regime_context = await self._fetch_utbreakout_market_regime_context(fb_cfg)
            market_regime_context = dict(market_regime_context or {})
        except Exception:
            market_regime_context = {}

        entry_price = float(metrics.get('close') or closed['close'].astype(float).iloc[-1])
        filter_values = {
            'entry_price': entry_price,
            'open': metrics.get('open'),
            'rsi': metrics.get('rsi'),
            'macd_hist': metrics.get('macd_hist'),
            'macd_hist_prev': metrics.get('macd_hist_prev'),
            'roc_pct': metrics.get('roc_pct'),
            'cci': metrics.get('cci'),
            'stoch_k': metrics.get('stoch_k'),
            'stoch_d': metrics.get('stoch_d'),
            'adx': metrics.get('adx'),
            'plus_di': metrics.get('plus_di'),
            'minus_di': metrics.get('minus_di'),
            'adx_reason': metrics.get('adx_reason'),
            'atr_pct': metrics.get('atr_pct'),
            'ema50': metrics.get('ema_fast'),
            'ema50_prev': metrics.get('ema_fast_prev'),
            'ema200': metrics.get('ema_slow'),
            'donchian_high_prev': metrics.get('donchian_high_prev'),
            'donchian_low_prev': metrics.get('donchian_low_prev'),
            'donchian_width_pct': metrics.get('donchian_width_pct'),
            'bb_upper': metrics.get('bb_upper'),
            'bb_lower': metrics.get('bb_lower'),
            'bb_mid': metrics.get('bb_mid'),
            'bb_width_pct': metrics.get('bb_width_pct'),
            'bb_width_prev_pct': metrics.get('bb_width_prev_pct'),
            'bb_width_min_pct': metrics.get('bb_width_min_pct'),
            'bb_width_percentile': metrics.get('bb_width_percentile'),
            'keltner_upper': metrics.get('keltner_upper'),
            'keltner_lower': metrics.get('keltner_lower'),
            'keltner_mid': metrics.get('keltner_mid'),
            'keltner_width_pct': metrics.get('keltner_width_pct'),
            'keltner_width_prev_pct': metrics.get('keltner_width_prev_pct'),
            'keltner_squeeze_on': metrics.get('keltner_squeeze_on'),
            'range_expansion_ratio': metrics.get('range_expansion_ratio'),
            'range_compression_ratio': metrics.get('range_compression_ratio'),
            'squeeze_release_state': metrics.get('squeeze_release_state'),
            'vwap': metrics.get('vwap'),
            'vwap_slope': metrics.get('vwap_slope'),
            'vwap_reason': metrics.get('vwap_reason'),
            'volume_ratio': metrics.get('volume_ratio'),
            'obv_slope_ratio': metrics.get('obv_slope_ratio'),
            'mfi': metrics.get('mfi'),
            'psar_direction': metrics.get('psar_direction'),
            'psar': metrics.get('psar'),
            'psar_reason': metrics.get('psar_reason'),
            'ichimoku_bias': metrics.get('ichimoku_bias'),
            'ichimoku_top': metrics.get('ichimoku_top'),
            'ichimoku_bottom': metrics.get('ichimoku_bottom'),
            'session_hour_kst': metrics.get('session_hour_kst'),
            'auto_scores': (auto_analysis or {}).get('scores') if isinstance(auto_analysis, dict) else {},
            'mtf_metrics': (auto_analysis or {}).get('timeframes') if isinstance(auto_analysis, dict) else {},
            'chop': metrics.get('chop'),
            'chop_reason': metrics.get('chop_reason'),
            'vortex_plus': metrics.get('vortex_plus'),
            'vortex_minus': metrics.get('vortex_minus'),
            'vortex_reason': metrics.get('vortex_reason'),
            'aroon_up': metrics.get('aroon_up'),
            'aroon_down': metrics.get('aroon_down'),
            'aroon_reason': metrics.get('aroon_reason'),
            'htf_ready': htf_ready,
            'htf_error': htf_error,
            'htf_close': htf_close,
            'htf_ema_fast': htf_ema_fast,
            'htf_ema_slow': htf_ema_slow,
            'htf_gap_pct': htf_gap_pct,
            'htf_supertrend_direction': htf_supertrend_direction,
            'htf_supertrend_reason': htf_supertrend_reason,
            'funding_rate': futures_context.get('funding_rate'),
            'next_funding_time': futures_context.get('next_funding_time'),
            'open_interest_delta_pct': futures_context.get('open_interest_delta_pct'),
            'open_interest_delta_z': futures_context.get('open_interest_delta_z'),
            'open_interest_acceleration': futures_context.get('open_interest_acceleration'),
            'open_interest_hist_samples': futures_context.get('open_interest_hist_samples'),
            'taker_buy_sell_ratio': futures_context.get('taker_buy_sell_ratio'),
            'long_short_ratio': futures_context.get('long_short_ratio'),
            'orderbook_imbalance_pct': futures_context.get('orderbook_imbalance_pct'),
            'rolling_orderbook_imbalance_pct': futures_context.get('rolling_orderbook_imbalance_pct'),
            'rolling_orderbook_imbalance_delta': futures_context.get('rolling_orderbook_imbalance_delta'),
            'rolling_ofi_score': futures_context.get('rolling_ofi_score'),
            'rolling_ofi_samples': futures_context.get('rolling_ofi_samples'),
            'futures_spread_pct': futures_context.get('futures_spread_pct'),
            'bid_depth_usdt': futures_context.get('bid_depth_usdt'),
            'ask_depth_usdt': futures_context.get('ask_depth_usdt'),
            'basis_pct': futures_context.get('basis_pct'),
            'market_regime_context': market_regime_context,
        }
        filter_values = self._enrich_utbreakout_trend_health_values(filter_values, closed, fb_cfg, None)
        filter_values = self._enrich_utbreakout_strategy_quality_values(filter_values, closed, fb_cfg)
        filter_values['feature_score'] = self._calculate_utbreakout_feature_score(opposite_side, fb_cfg, filter_values)
        filter_items = self._evaluate_utbreakout_set_filter_items(opposite_side, selected_set, fb_cfg, filter_values)
        failed_items = [item for item in filter_items if item.get('state') is not True]
        if failed_items:
            first = failed_items[0]
            return False, (
                f"Opposite set exit wait: Set{selected_set.get('id')} {first.get('name')} "
                f"{first.get('detail')}"
            )

        hold_text = f"{hold_candles:.1f}/{min_hold_candles}" if hold_candles is not None else f"0/{min_hold_candles}"
        pnl_text = (
            f"PnL {current_pnl:.2f} >= {min_pnl_usdt:.2f} USDT"
            if min_pnl_enabled else
            f"PnL filter OFF current {current_pnl:.2f} USDT"
        )
        reason = (
            f"OppositeSetExit {current_side.upper()} -> flat: fresh UT {opposite_side.upper()} "
            f"({ut_exit_reason}) + Set{selected_set.get('id')} {selected_set.get('name')} PASS "
            f"| hold {hold_text} candles | {pnl_text}"
        )
        if auto_reason:
            reason += f" | AUTO {auto_reason}"
        return True, reason
