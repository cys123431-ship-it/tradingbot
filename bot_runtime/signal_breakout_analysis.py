"""UTBreakout configuration, scoring, eligibility, and auto-entry bridge."""

from __future__ import annotations


class SignalBreakoutAnalysisMixin:
    def _calculate_utbot_signal(self, df, strategy_params):
        cfg = strategy_params.get('UTBot', {})
        key_value = float(cfg.get('key_value', 1.0) or 1.0)
        atr_period = max(1, int(cfg.get('atr_period', 10) or 10))
        use_heikin_ashi = bool(cfg.get('use_heikin_ashi', False))

        closed = df.iloc[:-1].copy().reset_index(drop=True)
        min_bars = max(atr_period + 3, 20)
        if len(closed) < min_bars:
            return None, "UTBOT 데이터 부족", {}

        prev_close = closed['close'].shift(1)
        true_range = pd.concat([
            (closed['high'] - closed['low']).abs(),
            (closed['high'] - prev_close).abs(),
            (closed['low'] - prev_close).abs()
        ], axis=1).max(axis=1)

        atr_series = pd.Series(np.nan, index=closed.index, dtype=float)
        if len(true_range) >= atr_period:
            atr_series.iloc[atr_period - 1] = true_range.iloc[:atr_period].mean()
            for idx in range(atr_period, len(true_range)):
                atr_series.iloc[idx] = (
                    (atr_series.iloc[idx - 1] * (atr_period - 1)) + true_range.iloc[idx]
                ) / atr_period
        if atr_series.isna().all():
            return None, "UTBOT ATR 계산 대기", {}

        if use_heikin_ashi:
            src_series = (closed['open'] + closed['high'] + closed['low'] + closed['close']) / 4.0
        else:
            src_series = closed['close'].astype(float)

        valid = pd.DataFrame({
            'timestamp': closed['timestamp'],
            'open': closed['open'].astype(float),
            'high': closed['high'].astype(float),
            'low': closed['low'].astype(float),
            'close': closed['close'].astype(float),
            'src': src_series.astype(float),
            'atr': atr_series.astype(float)
        }).dropna().reset_index(drop=True)
        if len(valid) < 3:
            return None, "UTBOT 지표 확정 대기", {}

        valid['nloss'] = valid['atr'] * key_value
        trail = []
        for idx, row in valid.iterrows():
            src_val = float(row['src'])
            nloss_val = float(row['nloss'])
            if idx == 0:
                trail.append(src_val - nloss_val)
                continue

            prev_stop = float(trail[-1])
            prev_src = float(valid.iloc[idx - 1]['src'])
            if src_val > prev_stop and prev_src > prev_stop:
                next_stop = max(prev_stop, src_val - nloss_val)
            elif src_val < prev_stop and prev_src < prev_stop:
                next_stop = min(prev_stop, src_val + nloss_val)
            elif src_val > prev_stop:
                next_stop = src_val - nloss_val
            else:
                next_stop = src_val + nloss_val
            trail.append(next_stop)

        valid['trail_stop'] = trail

        signal_idx = None
        signal_side = None
        for idx in range(1, len(valid)):
            scan_prev_row = valid.iloc[idx - 1]
            scan_curr_row = valid.iloc[idx]
            scan_prev_src = float(scan_prev_row['src'])
            scan_curr_src = float(scan_curr_row['src'])
            scan_prev_stop = float(scan_prev_row['trail_stop'])
            scan_curr_stop = float(scan_curr_row['trail_stop'])
            if scan_curr_src > scan_curr_stop and scan_prev_src <= scan_prev_stop:
                signal_idx = idx
                signal_side = 'long'
            elif scan_curr_src < scan_curr_stop and scan_prev_src >= scan_prev_stop:
                signal_idx = idx
                signal_side = 'short'

        prev_row = valid.iloc[-2]
        curr_row = valid.iloc[-1]
        prev_src = float(prev_row['src'])
        curr_src = float(curr_row['src'])
        prev_stop = float(prev_row['trail_stop'])
        curr_stop = float(curr_row['trail_stop'])

        buy = curr_src > curr_stop and prev_src <= prev_stop
        sell = curr_src < curr_stop and prev_src >= prev_stop
        signal_row = valid.iloc[signal_idx] if signal_idx is not None else None
        detail = {
            'key_value': key_value,
            'atr_period': atr_period,
            'use_heikin_ashi': use_heikin_ashi,
            'curr_src': curr_src,
            'curr_stop': curr_stop,
            'curr_atr': float(curr_row['atr']),
            'signal_ts': int(signal_row['timestamp']) if signal_row is not None else None,
            'signal_open': float(signal_row['open']) if signal_row is not None else None,
            'signal_high': float(signal_row['high']) if signal_row is not None else None,
            'signal_low': float(signal_row['low']) if signal_row is not None else None,
            'signal_close': float(signal_row['close']) if signal_row is not None else None,
            'signal_side': signal_side,
            'bias_side': 'long' if curr_src > curr_stop else 'short' if curr_src < curr_stop else None
        }

        if buy:
            reason = (
                f"UTBOT LONG: src {curr_src:.4f} > stop {curr_stop:.4f} "
                f"(key={key_value:.2f}, ATR={atr_period}, HA={'ON' if use_heikin_ashi else 'OFF'})"
            )
            return 'long', reason, detail

        if sell:
            reason = (
                f"UTBOT SHORT: src {curr_src:.4f} < stop {curr_stop:.4f} "
                f"(key={key_value:.2f}, ATR={atr_period}, HA={'ON' if use_heikin_ashi else 'OFF'})"
            )
            return 'short', reason, detail

        reason = (
            f"UTBOT 상태 유지 ({detail['bias_side'].upper() if detail['bias_side'] else 'NONE'}): "
            f"src {curr_src:.4f} / stop {curr_stop:.4f} "
            f"(key={key_value:.2f}, ATR={atr_period}, HA={'ON' if use_heikin_ashi else 'OFF'})"
        )
        return None, reason, detail

    def _get_utbot_filtered_breakout_config(self, strategy_params=None):
        params = strategy_params if isinstance(strategy_params, dict) else self.get_runtime_strategy_params()
        raw = params.get('UTBotFilteredBreakoutV1', {}) if isinstance(params, dict) else {}
        cfg = build_default_utbot_filtered_breakout_config()
        raw_has_active_set_id = isinstance(raw, dict) and 'active_set_id' in raw
        if isinstance(raw, dict):
            for key in list(cfg.keys()):
                if key in raw:
                    cfg[key] = raw[key]

        def _float(key, default, min_value=None):
            try:
                value = float(cfg.get(key, default))
            except (TypeError, ValueError):
                value = float(default)
            if min_value is not None:
                value = max(float(min_value), value)
            cfg[key] = value

        def _int(key, default, min_value=1):
            try:
                value = int(cfg.get(key, default))
            except (TypeError, ValueError):
                value = int(default)
            cfg[key] = max(int(min_value), value)

        for key, default in {
            'utbot_key_value': 2.5,
            'legacy_utbot_key_value': 2.0,
            'rsi_threshold': 50.0,
            'rsi_long_extreme': 80.0,
            'rsi_short_extreme': 20.0,
            'adx_threshold': 22.0,
            'atr_min_percent': 0.12,
            'atr_max_percent': 1.20,
            'ema_near_percent': 0.20,
            'htf_ema_gap_min_percent': 0.15,
            'donchian_width_min_percent': 0.50,
            'stop_atr_multiplier': 1.5,
            'take_profit_r_multiple': 3.50,
            'min_risk_reward': 2.0,
            'risk_per_trade_percent': DEFAULT_RISK_PER_TRADE_PERCENT,
            'min_risk_per_trade_percent': DEFAULT_MIN_RISK_PER_TRADE_PERCENT,
            'max_risk_per_trade_percent': UTBREAKOUT_MAX_RISK_PER_TRADE_PERCENT,
            'sl_retry_delay_sec': 0.7,
            'max_risk_per_trade_usdt': 1.0,
            'daily_max_loss_usdt': 3.0,
            'daily_profit_target_usdt': 5.0,
            'opposite_set_exit_min_pnl_usdt': 0.0,
            'auto_min_score_margin_live': 0.5,
            'auto_min_adjusted_score_live': 40.0,
            'multiple_testing_max_score_penalty': 4.0,
            'set_selection_switch_margin': 3.0,
            'set_selection_min_score_gap': 2.0,
            'adaptive_timeframe_min_score': 38.0,
            'adaptive_timeframe_switch_margin': 10.0,
            'partial_take_profit_r_multiple': 1.00,
            'partial_take_profit_ratio': 0.20,
            'second_take_profit_r_multiple': 3.50,
            'second_take_profit_ratio': 0.40,
            'atr_trailing_multiplier': 3.50,
            'atr_trailing_activation_r': 1.60,
            'entry_relaxation_balanced_score_buffer': 1.5,
            'entry_relaxation_active_score_buffer': 3.0,
            'entry_relaxation_balanced_probability_buffer': 0.005,
            'entry_relaxation_active_probability_buffer': 0.010,
            'market_quality_min_risk_multiplier': 0.55,
            'market_quality_high_atr_pct': 1.5,
            'market_quality_extreme_atr_pct': 2.5,
            'market_quality_adverse_funding_soft': 0.0006,
            'market_quality_adverse_funding_hard': 0.0015,
            'market_quality_long_multi_adverse_max_multiplier': 0.35,
            'entry_quality_gate_min_final_risk_multiplier': 0.35,
            'entry_quality_gate_long_min_final_risk_multiplier': 0.40,
            'entry_quality_gate_short_min_final_risk_multiplier': 0.35,
            'entry_quality_gate_hard_market_multiplier_below': 0.25,
            'entry_quality_gate_min_ev_score': 58.0,
            'entry_quality_gate_min_ev_probability': 0.53,
            'entry_quality_gate_min_ev_net_expectancy_r': 0.18,
            'market_quality_regime_strong_move_pct': 1.5,
            'squeeze_volume_ratio_min': 1.20,
            'runner_structure_buffer_atr': 0.20,
            'runner_chandelier_multiplier': 3.80,
            'runner_chandelier_multiplier_min': 1.4,
            'runner_chandelier_multiplier_max': 4.50,
            'runner_mfe_tighten_r': 3.0,
            'runner_mfe_tighten_delta': 0.20,
            'trend_health_hard_block_below': 12.0,
            'trend_health_reduce_below': 35.0,
            'trend_health_full_score': 62.0,
            'trend_health_min_multiplier': 0.55,
            'strategy_quality_hard_block_below': 8.0,
            'strategy_quality_reduce_below': 25.0,
            'strategy_quality_full_score': 60.0,
            'strategy_quality_min_multiplier': 0.55,
            'adaptive_exit_partial_r_min': 1.0,
            'adaptive_exit_partial_r_max': 1.2,
            'adaptive_exit_ratio_min': 0.20,
            'adaptive_exit_ratio_max': 0.35,
            'adaptive_exit_trailing_multiplier_min': 3.0,
            'adaptive_exit_trailing_multiplier_max': 4.0,
            'adaptive_exit_activation_r_min': 1.4,
            'adaptive_exit_activation_r_max': 1.8,
            'volatility_target_atr_pct': 1.0,
            'volatility_target_min_multiplier': 0.25,
            'meta_labeling_min_multiplier': 0.5,
            'strategy_adaptive_min_risk_multiplier': 0.50,
            'set_filter_soft_fail_multiplier': 0.90,
            'set_filter_multi_soft_fail_multiplier': 0.80,
            'final_risk_multiplier_floor': 0.20,
            'set32_min_relative_volume': 1.15,
            'set32_min_taker_ratio_long': 0.97,
            'set32_max_taker_ratio_short': 1.03,
            'set32_max_spread_pct': 0.09,
            'short_partial_take_profit_r_delta': 0.20,
            'short_partial_take_profit_ratio_add': 0.10,
            'short_atr_trailing_multiplier_delta': 0.25,
            'short_atr_trailing_activation_r_delta': 0.20,
            'short_adx_threshold': 25.0,
            'short_dmi_min_gap': 4.0,
            'bias_continuation_risk_multiplier': 0.65,
            'bias_continuation_15m_risk_multiplier': 0.50,
            'bias_continuation_min_adx': 10.0,
            'bias_continuation_15m_min_adx': 11.0,
            'bias_continuation_min_volume_ratio': 0.40,
            'bias_continuation_15m_min_volume_ratio': 0.45,
            'bias_continuation_max_extension_atr': 1.60,
            'bias_continuation_15m_max_extension_atr': 1.50,
            'bias_continuation_min_adaptive_tf_score': 30.0,
            'bias_continuation_15m_min_adaptive_tf_score': 32.0,
            'utbreakout_no_position_retry_interval_sec': 20.0,
            'utbreakout_trace_watchdog_wait_sec': 90.0,
            'utbreakout_trace_watchdog_cooldown_sec': 180.0,
            'utbreakout_auto_entry_bridge_cooldown_sec': 60.0,
            'utbreakout_auto_entry_bridge_max_ready_age_sec': 180.0,
            'utbreakout_require_scanner_candidate_for_auto_entry': True,
            'quality_score_v2_block_below': 12.0,
            'quality_score_v2_reduce_below': 40.0,
            'quality_score_v2_full_score': 82.0,
            'quality_score_v2_min_risk_multiplier': 0.60,
            'quality_score_v2_15m_block_below': 12.0,
            'quality_score_v2_15m_reduce_below': 40.0,
            'quality_score_v2_long_block_below': 12.0,
            'quality_score_v2_long_reduce_below': 40.0,
            'quality_score_v2_long_15m_block_below': 12.0,
            'quality_score_v2_long_15m_reduce_below': 40.0,
            'quality_score_v2_short_block_below': 16.0,
            'quality_score_v2_short_reduce_below': 45.0,
            'quality_score_v2_short_15m_block_below': 16.0,
            'quality_score_v2_short_15m_reduce_below': 45.0,
            'dynamic_tp2_strong_score': 72.0,
            'dynamic_tp2_elite_score': 82.0,
            'dynamic_tp2_base_r_multiple': 3.20,
            'dynamic_tp2_strong_r_multiple': 5.00,
            'dynamic_tp2_elite_r_multiple': 7.00,
            'tp1_breakeven_trigger_r': 1.5,
            'tp1_breakeven_offset_r': 0.03,
            'tp1_breakeven_qty_tolerance': 0.08,
            'protection_audit_after_place_delay_sec': 0.5,
            'aggressive_growth_balance_sleeve_pct': 0.20,
            'aggressive_growth_max_leverage_for_margin_sleeve': 3.0,
            'aggressive_growth_max_trade_risk_pct': 0.015,
            'aggressive_growth_max_trade_risk_pct_strong': 0.025,
            'aggressive_growth_daily_loss_limit_pct': 0.04,
            'aggressive_growth_weekly_loss_limit_pct': 0.10,
            'aggressive_growth_max_symbol_exposure_pct': 0.10,
            'aggressive_growth_pyramid_trigger_r': 1.0,
            'aggressive_growth_pyramid_add_risk_fraction': 0.50,
            'aggressive_growth_trailing_atr_multiplier': 2.5,
            'aggressive_growth_runner_pct': 0.35,
            'aggressive_growth_tp1_pct': 0.35,
            'aggressive_growth_tp2_pct': 0.30,
            'aggressive_growth_atr_pct_low': 0.004,
            'aggressive_growth_atr_pct_normal': 0.012,
            'aggressive_growth_atr_pct_high': 0.025,
            'aggressive_growth_atr_pct_extreme': 0.040,
            'aggressive_growth_kelly_fraction': 0.25,
            'aggressive_growth_kelly_max_risk_multiplier': 1.25,
            'aggressive_growth_kelly_min_risk_multiplier': 0.25,
            'aggressive_growth_cppi_floor_pct': 0.90,
            'aggressive_growth_cppi_multiplier': 2.0,
            'aggressive_growth_cppi_min_sleeve_pct': 0.05,
            'aggressive_growth_cppi_max_sleeve_pct': 0.20,
        }.items():
            min_value = None if key == 'opposite_set_exit_min_pnl_usdt' else 0.0
            _float(key, default, min_value)

        for key, default in {
            'utbot_atr_period': 14,
            'legacy_utbot_atr_period': 10,
            'ema_fast': 50,
            'ema_slow': 200,
            'rsi_length': 14,
            'adx_length': 14,
            'atr_length': 14,
            'donchian_length': 20,
            'max_daily_trades': 5,
            'max_consecutive_losses': 5,
            'sl_place_max_retries': 3,
            'opposite_set_exit_min_hold_candles': 3,
            'multiple_testing_free_trials': 25,
            'market_quality_long_multi_adverse_min_reasons': 5,
            'entry_quality_gate_min_ev_mtf_votes': 2,
            'set32_orderflow_min_samples': 2,
            'adaptive_timeframe_min_hold_candles': 6,
            'shadow_triple_barrier_max_bars': 24,
            'shadow_runner_max_bars': 48,
            'runner_chandelier_lookback': 22,
            'runner_structure_lookback': 5,
            'trend_health_directional_lookback': 12,
            'trend_health_volatility_long_length': 50,
            'strategy_quality_regression_lookback': 24,
            'strategy_quality_hurst_lookback': 64,
            'strategy_quality_rebound_lookback': 12,
            'adaptive_exit_min_samples': 8,
            'adaptive_exit_lookback_days': 14,
            'meta_labeling_min_samples': 12,
            'bias_continuation_max_signal_age_candles': 10,
            'bias_continuation_15m_max_signal_age_candles': 10,
            'tp2_fallback_confirm_loops': 2,
            'aggressive_growth_max_open_positions': 2,
            'aggressive_growth_pyramid_max_adds': 2,
            'aggressive_growth_kelly_lookback_trades': 50,
            'safe_live_default_set_id': UTBREAKOUT_SAFE_LIVE_DEFAULT_SET_ID,
        }.items():
            min_value = 0 if key == 'opposite_set_exit_min_hold_candles' else 1
            _int(key, default, min_value)

        cfg['active_set_id'] = normalize_utbreakout_set_id(
            cfg.get('active_set_id') if raw_has_active_set_id else cfg.get('profile'),
            UTBREAKOUT_DEFAULT_SET_ID
        )
        cfg['profile'] = f"set{cfg['active_set_id']}"
        selection_mode = str(cfg.get('selection_mode') or '').strip().lower()
        auto_enabled = bool(cfg.get('auto_select_enabled', False))
        if selection_mode not in {'manual', 'auto'}:
            selection_mode = 'auto' if auto_enabled else 'manual'
        cfg['selection_mode'] = selection_mode
        cfg['auto_select_enabled'] = selection_mode == 'auto' or auto_enabled
        if cfg['auto_select_enabled']:
            cfg['selection_mode'] = 'auto'
        raw_timeframes = cfg.get('auto_timeframes', UTBREAKOUT_AUTO_TIMEFRAMES)
        if isinstance(raw_timeframes, str):
            auto_timeframes = [item.strip().lower() for item in raw_timeframes.split(',') if item.strip()]
        elif isinstance(raw_timeframes, (list, tuple, set)):
            auto_timeframes = [str(item).strip().lower() for item in raw_timeframes if str(item).strip()]
        else:
            auto_timeframes = list(UTBREAKOUT_AUTO_TIMEFRAMES)
        cfg['auto_timeframes'] = auto_timeframes or list(UTBREAKOUT_AUTO_TIMEFRAMES)
        cfg['live_auto_set_whitelist'] = normalize_utbreakout_set_id_list(
            cfg.get('live_auto_set_whitelist'),
            UTBREAKOUT_LIVE_AUTO_SET_WHITELIST,
        )
        raw_adaptive_timeframes = cfg.get('adaptive_timeframes', cfg['auto_timeframes'])
        if isinstance(raw_adaptive_timeframes, str):
            adaptive_timeframes = [item.strip().lower() for item in raw_adaptive_timeframes.split(',') if item.strip()]
        elif isinstance(raw_adaptive_timeframes, (list, tuple, set)):
            adaptive_timeframes = [str(item).strip().lower() for item in raw_adaptive_timeframes if str(item).strip()]
        else:
            adaptive_timeframes = list(cfg['auto_timeframes'])
        cfg['adaptive_timeframes'] = [
            tf for tf in (adaptive_timeframes or list(UTBREAKOUT_AUTO_TIMEFRAMES))
            if tf in UTBREAKOUT_AUTO_TIMEFRAMES and tf != '5m'
        ] or list(UTBREAKOUT_AUTO_TIMEFRAMES)
        cfg['entry_timeframe'] = str(cfg.get('entry_timeframe') or '15m').strip().lower() or '15m'
        cfg['exit_timeframe'] = str(cfg.get('exit_timeframe') or cfg['entry_timeframe']).strip().lower() or cfg['entry_timeframe']
        cfg['htf_timeframe'] = str(cfg.get('htf_timeframe') or '1h').strip().lower() or '1h'
        active_strategy = str(params.get('active_strategy', '') or '').lower() if isinstance(params, dict) else ''
        if active_strategy == UTBOT_ADAPTIVE_TIMEFRAME_STRATEGY:
            cfg['adaptive_timeframe_enabled'] = True
        entry_strategy_key = resolve_entry_strategy({'entry_strategy': cfg.get('entry_strategy')})
        if (
            active_strategy == ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND
            or (
                active_strategy not in {DUAL_ALPHA_STRATEGY, TRIPLE_ALPHA_STRATEGY, QUAD_ALPHA_STRATEGY}
                and entry_strategy_key == ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND
            )
        ):
            rsp_cfg = default_relative_strength_pullback_config()
            nested_rsp_cfg = cfg.get('relative_strength_pullback_trend')
            if isinstance(nested_rsp_cfg, dict):
                rsp_cfg.update(nested_rsp_cfg)
            cfg['entry_strategy'] = ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND
            cfg['entry_timeframe'] = str(rsp_cfg.get('signal_tf') or '4h').strip().lower() or '4h'
            cfg['htf_timeframe'] = str(rsp_cfg.get('trend_htf') or '1d').strip().lower() or '1d'
            cfg['adaptive_timeframe_enabled'] = False
        for key in (
            'use_heikin_ashi',
            'exclude_rsi_extreme',
            'daily_profit_target_enabled',
            'opposite_signal_exit_enabled',
            'opposite_set_exit_enabled',
            'opposite_set_exit_min_pnl_enabled',
            'ema_rsi_exit_enabled',
            'adx_donchian_exit_enabled',
            'adaptive_timeframe_enabled',
            'fixed_take_profit_enabled',
            'partial_take_profit_enabled',
            'second_take_profit_enabled',
            'atr_trailing_enabled',
            'atr_trailing_breakeven_enabled',
            'short_conservative_enabled',
            'market_quality_enabled',
            'market_quality_data_required',
            'market_quality_regime_enabled',
            'shadow_triple_barrier_enabled',
            'shadow_runner_exit_enabled',
            'relative_strength_pullback_trend_shadow_enabled',
            'relative_strength_pullback_trend_live_enabled',
            'relative_strength_pullback_trend_paper_enabled',
            'runner_exit_enabled',
            'runner_chandelier_enabled',
            'runner_dynamic_multiplier_enabled',
            'trend_health_enabled',
            'strategy_quality_enabled',
            'adaptive_exit_enabled',
            'volatility_targeting_enabled',
            'meta_labeling_enabled',
            'short_asymmetry_enabled',
            'short_require_htf_supertrend',
            'short_require_entry_ema_downtrend',
            'short_require_momentum_downtrend',
            'regime_filter_enabled',
            'meta_sizing_enabled',
            'portfolio_risk_enabled',
            'drawdown_brake_enabled',
            'adaptive_exit_v2_enabled',
            'walk_forward_report_enabled',
            'advanced_alpha_engine_enabled',
            'advanced_alpha_paper_testnet_default_enabled',
            'adaptive_ladder_tp_enabled',
            'macro_guard_enabled',
            'engine_kill_switch_enabled',
            'live_parity_signal_enabled',
            'bias_continuation_enabled',
            'quality_score_v2_enabled',
            'dynamic_take_profit_enabled',
            'tp1_breakeven_enabled',
            'tp1_breakeven_wait_for_partial',
            'enable_tp2_fallback_close',
            'tp2_fallback_use_market',
            'emergency_close_on_sl_fail',
            'aggressive_growth_enabled',
            'aggressive_growth_pyramiding_enabled',
            'aggressive_growth_move_sl_to_breakeven_before_add',
            'aggressive_growth_vol_target_enabled',
            'aggressive_growth_kelly_enabled',
            'aggressive_growth_cppi_enabled',
            'live_safety_guard_enabled',
            'live_auto_set_whitelist_enabled',
            'auto_block_on_weak_margin_live',
            'auto_multiple_testing_penalty_enabled',
            'set_selection_hysteresis_enabled',
            'set_complexity_penalty_enabled',
            'market_quality_long_hard_block_on_multi_adverse_enabled',
            'selected_set_core_filter_hard_block_enabled',
            'set_filter_soft_fail_enabled',
            'set32_require_direction_candle',
            'set32_require_ema50_side',
            'set32_require_orderflow_confirmation',
            'allow_ut_only_live_override',
            'emergency_mode',
            'allow_emergency_simple_set',
        ):
            cfg[key] = bool(cfg.get(key, False))
        if isinstance(raw, dict) and 'advanced_alpha_engine_enabled' not in raw:
            cfg['advanced_alpha_engine_enabled'] = (
                False
                if self._is_utbreakout_live_runtime(cfg)
                else bool(cfg.get('advanced_alpha_paper_testnet_default_enabled', True))
            )
        relaxation_mode = str(cfg.get('utbreak_entry_relaxation_mode') or 'balanced').strip().lower()
        if relaxation_mode not in {'strict', 'balanced', 'active'}:
            relaxation_mode = 'balanced'
        cfg['utbreak_entry_relaxation_mode'] = relaxation_mode
        sleeve_mode = str(cfg.get('aggressive_growth_sleeve_mode') or 'notional').strip().lower()
        cfg['aggressive_growth_sleeve_mode'] = sleeve_mode if sleeve_mode in {'notional', 'margin'} else 'notional'
        cfg['risk_per_trade_percent'] = normalize_risk_percent(
            {
                'risk_per_trade_percent': cfg.get('risk_per_trade_percent'),
                'min_risk_per_trade_percent': cfg.get('min_risk_per_trade_percent'),
                'max_risk_per_trade_percent': cfg.get('max_risk_per_trade_percent'),
            },
            max_default=UTBREAKOUT_MAX_RISK_PER_TRADE_PERCENT,
        )
        if bool(cfg.get('fixed_take_profit_enabled', True)):
            cfg['partial_take_profit_enabled'] = True
            cfg['second_take_profit_enabled'] = True

            # Keep opportunity-mode TP values. The final stable override remains
            # the single source of truth after selected Set parameters are merged.
            cfg['partial_take_profit_r_multiple'] = float(
                cfg.get('partial_take_profit_r_multiple', 1.00) or 1.00
            )
            cfg['partial_take_profit_ratio'] = float(
                cfg.get('partial_take_profit_ratio', 0.20) or 0.20
            )
            cfg['second_take_profit_r_multiple'] = float(
                cfg.get('second_take_profit_r_multiple', 3.50) or 3.50
            )
            cfg['second_take_profit_ratio'] = float(
                cfg.get('second_take_profit_ratio', 0.40) or 0.40
            )

            # Fixed TP ladder and runner/trailing operate together in the
            # profit-opportunity profile.
            cfg['atr_trailing_enabled'] = True
            cfg['shadow_runner_exit_enabled'] = True
            cfg['runner_exit_enabled'] = True
            cfg['runner_chandelier_enabled'] = True
        # A-option (opposite UT signal only) is intentionally rejected for UT Breakout.
        # Only the stricter opposite-set exit can be enabled from Telegram.
        cfg['opposite_signal_exit_enabled'] = False
        if cfg['atr_max_percent'] < cfg['atr_min_percent']:
            cfg['atr_max_percent'] = cfg['atr_min_percent']
        cfg = apply_stable_utbreak_final_overrides(cfg)
        cfg = apply_profit_opportunity_effective_overrides(cfg)
        if bool(cfg.get('dual_alpha_direction_filter_enabled', False)):
            direction_tf = str(cfg.get('dual_alpha_direction_filter_timeframe') or '4h').strip().lower() or '4h'
            direction_htf = str(cfg.get('dual_alpha_direction_filter_htf') or '1d').strip().lower() or '1d'
            cfg['entry_timeframe'] = direction_tf
            cfg['exit_timeframe'] = direction_tf
            cfg['htf_timeframe'] = direction_htf
            cfg['adaptive_timeframe_enabled'] = False
        if hasattr(self, 'get_effective_automatic_daily_trade_limit'):
            cfg['max_daily_trades'] = int(
                self.get_effective_automatic_daily_trade_limit()
            )
        return cfg

    def _get_utbot_filtered_breakout_ut_params(self, cfg):
        return {
            'UTBot': {
                'key_value': float(cfg.get('utbot_key_value', 2.5) or 2.5),
                'atr_period': int(cfg.get('utbot_atr_period', 14) or 14),
                'use_heikin_ashi': bool(cfg.get('use_heikin_ashi', False))
            }
        }

    def _get_utbreakout_set_info(self, cfg, set_id=None):
        resolved_id = normalize_utbreakout_set_id(
            set_id if set_id is not None else cfg.get('active_set_id'),
            UTBREAKOUT_DEFAULT_SET_ID
        )
        return get_utbreakout_set_definition(resolved_id)

    def _calculate_utbreakout_vortex(self, closed, length=14):
        if closed is None or len(closed) < length + 2:
            return None, None, "Vortex 데이터 부족"
        high = closed['high'].astype(float).reset_index(drop=True)
        low = closed['low'].astype(float).reset_index(drop=True)
        close = closed['close'].astype(float).reset_index(drop=True)
        prev_high = high.shift(1)
        prev_low = low.shift(1)
        prev_close = close.shift(1)
        true_range = pd.concat([
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ], axis=1).max(axis=1)
        vm_plus = (high - prev_low).abs()
        vm_minus = (low - prev_high).abs()
        tr_sum = true_range.rolling(length).sum().replace(0.0, np.nan)
        vi_plus = vm_plus.rolling(length).sum() / tr_sum
        vi_minus = vm_minus.rolling(length).sum() / tr_sum
        curr_plus = vi_plus.iloc[-1]
        curr_minus = vi_minus.iloc[-1]
        if pd.isna(curr_plus) or pd.isna(curr_minus):
            return None, None, "Vortex 계산 대기"
        return float(curr_plus), float(curr_minus), f"VI+ {float(curr_plus):.3f} | VI- {float(curr_minus):.3f}"

    def _calculate_utbreakout_aroon(self, closed, length=25):
        if closed is None or len(closed) < length + 1:
            return None, None, "Aroon 데이터 부족"
        high_window = closed['high'].astype(float).iloc[-length:]
        low_window = closed['low'].astype(float).iloc[-length:]
        if high_window.empty or low_window.empty:
            return None, None, "Aroon 계산 대기"
        high_pos = int(np.argmax(high_window.to_numpy(dtype=float)))
        low_pos = int(np.argmin(low_window.to_numpy(dtype=float)))
        periods_since_high = (length - 1) - high_pos
        periods_since_low = (length - 1) - low_pos
        aroon_up = 100.0 * (length - periods_since_high) / max(float(length), 1.0)
        aroon_down = 100.0 * (length - periods_since_low) / max(float(length), 1.0)
        return float(aroon_up), float(aroon_down), f"AroonUp {aroon_up:.2f} | AroonDown {aroon_down:.2f}"

    def _calculate_utbreakout_psar_direction(self, closed, step=0.02, max_step=0.20):
        if closed is None or len(closed) < 12:
            return None, None, "PSAR 데이터 부족"
        high = closed['high'].astype(float).reset_index(drop=True)
        low = closed['low'].astype(float).reset_index(drop=True)
        close = closed['close'].astype(float).reset_index(drop=True)
        bull = bool(close.iloc[1] >= close.iloc[0])
        psar = float(low.iloc[0] if bull else high.iloc[0])
        extreme = float(high.iloc[0] if bull else low.iloc[0])
        accel = float(step)
        for idx in range(1, len(closed)):
            psar = psar + accel * (extreme - psar)
            if bull:
                if idx >= 2:
                    psar = min(psar, float(low.iloc[idx - 1]), float(low.iloc[idx - 2]))
                if float(low.iloc[idx]) < psar:
                    bull = False
                    psar = extreme
                    extreme = float(low.iloc[idx])
                    accel = float(step)
                else:
                    if float(high.iloc[idx]) > extreme:
                        extreme = float(high.iloc[idx])
                        accel = min(float(max_step), accel + float(step))
            else:
                if idx >= 2:
                    psar = max(psar, float(high.iloc[idx - 1]), float(high.iloc[idx - 2]))
                if float(high.iloc[idx]) > psar:
                    bull = True
                    psar = extreme
                    extreme = float(high.iloc[idx])
                    accel = float(step)
                else:
                    if float(low.iloc[idx]) < extreme:
                        extreme = float(low.iloc[idx])
                        accel = min(float(max_step), accel + float(step))
        direction = 'long' if bull else 'short'
        return direction, float(psar), f"PSAR {direction.upper()} @ {psar:.4f}"

    def _calculate_utbreakout_timeframe_metrics(self, closed, cfg):
        metrics = {'ready': False, 'reason': '데이터 부족'}
        if closed is None or len(closed) < 30:
            return metrics
        local = closed.copy().reset_index(drop=True)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            local[col] = pd.to_numeric(local[col], errors='coerce')
        local = local.dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
        if len(local) < 30:
            metrics['reason'] = f"유효 데이터 부족 {len(local)}/30"
            return metrics

        close = local['close'].astype(float)
        high = local['high'].astype(float)
        low = local['low'].astype(float)
        volume = local['volume'].astype(float) if 'volume' in local else pd.Series(0.0, index=local.index)
        curr_open = float(local['open'].astype(float).iloc[-1])
        curr_close = float(close.iloc[-1])
        prev_close = float(close.iloc[-2]) if len(close) >= 2 else curr_close
        ema_fast_len = int(cfg.get('ema_fast', 50) or 50)
        ema_slow_len = int(cfg.get('ema_slow', 200) or 200)
        ema_fast_series = close.ewm(span=ema_fast_len, adjust=False).mean() if len(close) >= ema_fast_len else pd.Series(np.nan, index=close.index)
        ema_slow_series = close.ewm(span=ema_slow_len, adjust=False).mean() if len(close) >= ema_slow_len else pd.Series(np.nan, index=close.index)
        ema_fast = float(ema_fast_series.iloc[-1]) if self._is_valid_number(ema_fast_series.iloc[-1]) else np.nan
        ema_fast_prev = float(ema_fast_series.iloc[-2]) if len(ema_fast_series) >= 2 and self._is_valid_number(ema_fast_series.iloc[-2]) else np.nan
        ema_slow = float(ema_slow_series.iloc[-1]) if self._is_valid_number(ema_slow_series.iloc[-1]) else np.nan
        ema_gap_pct = (
            abs(ema_fast - ema_slow) / max(abs(curr_close), 1e-9) * 100.0
            if self._is_valid_number(ema_fast) and self._is_valid_number(ema_slow)
            else np.nan
        )
        ema_bias = 'neutral'
        if self._is_valid_number(ema_fast) and self._is_valid_number(ema_slow):
            if curr_close > ema_slow and ema_fast > ema_slow:
                ema_bias = 'long'
            elif curr_close < ema_slow and ema_fast < ema_slow:
                ema_bias = 'short'

        rsi_series = self._calculate_wilder_rsi_series(close, int(cfg.get('rsi_length', 14) or 14))
        rsi_value = float(rsi_series.iloc[-1]) if self._is_valid_number(rsi_series.iloc[-1]) else np.nan
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        macd_signal = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist = macd_line - macd_signal
        macd_hist_value = float(macd_hist.iloc[-1]) if self._is_valid_number(macd_hist.iloc[-1]) else np.nan
        macd_hist_prev = float(macd_hist.iloc[-2]) if len(macd_hist) >= 2 and self._is_valid_number(macd_hist.iloc[-2]) else np.nan
        roc_len = 10
        roc_pct = (
            (curr_close / max(abs(float(close.iloc[-roc_len - 1])), 1e-9) - 1.0) * 100.0
            if len(close) > roc_len and self._is_valid_number(close.iloc[-roc_len - 1])
            else np.nan
        )
        typical_price = (high + low + close) / 3.0
        cci_len = 20
        tp_sma = typical_price.rolling(cci_len).mean()
        tp_mad = typical_price.rolling(cci_len).apply(
            lambda values: float(np.mean(np.abs(values - np.mean(values)))),
            raw=True
        )
        cci_series = (typical_price - tp_sma) / (0.015 * tp_mad.replace(0.0, np.nan))
        cci_value = float(cci_series.iloc[-1]) if self._is_valid_number(cci_series.iloc[-1]) else np.nan
        stoch_len = 14
        lowest_low = low.rolling(stoch_len).min()
        highest_high = high.rolling(stoch_len).max()
        stoch_k = 100.0 * ((close - lowest_low) / (highest_high - lowest_low).replace(0.0, np.nan))
        stoch_d = stoch_k.rolling(3).mean()
        stoch_k_value = float(stoch_k.iloc[-1]) if self._is_valid_number(stoch_k.iloc[-1]) else np.nan
        stoch_d_value = float(stoch_d.iloc[-1]) if self._is_valid_number(stoch_d.iloc[-1]) else np.nan
        adx_value, plus_di, minus_di, adx_reason = self._calculate_utbot_filter_pack_adx_dmi(
            local,
            int(cfg.get('adx_length', 14) or 14)
        )
        atr_series = self._calculate_wilder_atr_series(local, int(cfg.get('atr_length', 14) or 14))
        atr_value = float(atr_series.iloc[-1]) if self._is_valid_number(atr_series.iloc[-1]) else np.nan
        atr_pct = atr_value / max(abs(curr_close), 1e-9) * 100.0 if self._is_valid_number(atr_value) else np.nan
        atr20_series = self._calculate_wilder_atr_series(local, 20)
        atr20_value = float(atr20_series.iloc[-1]) if self._is_valid_number(atr20_series.iloc[-1]) else np.nan
        chop_value, chop_reason = self._calculate_utbot_filter_pack_chop(local, 14)
        vortex_plus, vortex_minus, vortex_reason = self._calculate_utbreakout_vortex(local, 14)
        aroon_up, aroon_down, aroon_reason = self._calculate_utbreakout_aroon(local, 25)
        vwap_value, vwap_slope, _, vwap_reason = self._calculate_utbot_filter_pack_vwap(local)

        don_len = int(cfg.get('donchian_length', 20) or 20)
        don_high_prev = np.nan
        don_low_prev = np.nan
        don_width_pct = np.nan
        don_ready = False
        donchian_prev = previous_donchian(
            high.astype(float).tolist(),
            low.astype(float).tolist(),
            don_len
        )
        if donchian_prev.get('ready'):
            don_high_prev = float(donchian_prev['high'])
            don_low_prev = float(donchian_prev['low'])
            don_width_pct = (don_high_prev - don_low_prev) / max(abs(curr_close), 1e-9) * 100.0
            don_ready = True

        vol_ma = volume.rolling(20).mean().iloc[-1] if len(volume) >= 20 else np.nan
        volume_ratio = float(volume.iloc[-1] / vol_ma) if self._is_valid_number(vol_ma) and float(vol_ma) > 0 else np.nan
        bb_width_pct = np.nan
        bb_width_prev_pct = np.nan
        bb_width_min_pct = np.nan
        bb_upper_value = np.nan
        bb_lower_value = np.nan
        bb_mid_value = np.nan
        if len(close) >= 20:
            bb_mid = close.rolling(20).mean()
            bb_std = close.rolling(20).std()
            bb_upper = bb_mid + (2.0 * bb_std)
            bb_lower = bb_mid - (2.0 * bb_std)
            if self._is_valid_number(bb_upper.iloc[-1]) and self._is_valid_number(bb_lower.iloc[-1]):
                bb_upper_value = float(bb_upper.iloc[-1])
                bb_lower_value = float(bb_lower.iloc[-1])
                bb_mid_value = float(bb_mid.iloc[-1])
                bb_width_pct = (float(bb_upper.iloc[-1]) - float(bb_lower.iloc[-1])) / max(abs(curr_close), 1e-9) * 100.0
                if len(bb_upper) >= 2 and self._is_valid_number(bb_upper.iloc[-2]) and self._is_valid_number(bb_lower.iloc[-2]):
                    bb_width_prev_pct = (float(bb_upper.iloc[-2]) - float(bb_lower.iloc[-2])) / max(abs(prev_close), 1e-9) * 100.0
                bb_width_series = ((bb_upper - bb_lower) / close.abs().replace(0.0, np.nan)) * 100.0
                bb_width_min = bb_width_series.rolling(min(120, len(bb_width_series)), min_periods=20).min().iloc[-1]
                bb_width_min_pct = float(bb_width_min) if self._is_valid_number(bb_width_min) else np.nan

        keltner_mid = close.ewm(span=20, adjust=False).mean()
        keltner_upper = keltner_mid + (2.0 * atr20_series)
        keltner_lower = keltner_mid - (2.0 * atr20_series)
        keltner_mid_value = float(keltner_mid.iloc[-1]) if self._is_valid_number(keltner_mid.iloc[-1]) else np.nan
        keltner_upper_value = float(keltner_upper.iloc[-1]) if self._is_valid_number(keltner_upper.iloc[-1]) else np.nan
        keltner_lower_value = float(keltner_lower.iloc[-1]) if self._is_valid_number(keltner_lower.iloc[-1]) else np.nan
        keltner_width_pct = (
            (keltner_upper_value - keltner_lower_value) / max(abs(curr_close), 1e-9) * 100.0
            if self._is_valid_number(keltner_upper_value) and self._is_valid_number(keltner_lower_value)
            else np.nan
        )
        keltner_width_prev_pct = (
            (float(keltner_upper.iloc[-2]) - float(keltner_lower.iloc[-2])) / max(abs(prev_close), 1e-9) * 100.0
            if len(keltner_upper) >= 2 and self._is_valid_number(keltner_upper.iloc[-2]) and self._is_valid_number(keltner_lower.iloc[-2])
            else np.nan
        )

        candle_range_pct = (float(high.iloc[-1]) - float(low.iloc[-1])) / max(abs(curr_close), 1e-9) * 100.0
        avg_range20_pct = ((high - low) / close.abs().replace(0.0, np.nan) * 100.0).rolling(20).mean().iloc[-1]
        avg_range50_pct = ((high - low) / close.abs().replace(0.0, np.nan) * 100.0).rolling(50).mean().iloc[-1]
        avg_range20_pct = float(avg_range20_pct) if self._is_valid_number(avg_range20_pct) else np.nan
        avg_range50_pct = float(avg_range50_pct) if self._is_valid_number(avg_range50_pct) else np.nan
        range_expansion_ratio = candle_range_pct / max(avg_range20_pct, 1e-9) if self._is_valid_number(avg_range20_pct) else np.nan
        range_compression_ratio = avg_range20_pct / max(avg_range50_pct, 1e-9) if self._is_valid_number(avg_range50_pct) else np.nan

        bb_width_percentile = np.nan
        keltner_squeeze_on = None
        keltner_squeeze_reason = None
        squeeze_release_state = 'pending'
        try:
            squeeze_lookback = max(2, int(cfg.get('squeeze_lookback', 80) or 80))
            bb_rank = bollinger_width_percentile(
                close.astype(float).tolist(),
                length=20,
                mult=2.0,
                lookback=squeeze_lookback,
            )
            if bb_rank.get('ready') and self._is_valid_number(bb_rank.get('percentile')):
                bb_width_percentile = float(bb_rank.get('percentile'))
            keltner_state = keltner_squeeze_state(
                high.astype(float).tolist(),
                low.astype(float).tolist(),
                close.astype(float).tolist(),
                bb_length=20,
                bb_mult=2.0,
                kc_length=20,
                kc_mult=1.5,
            )
            keltner_squeeze_reason = keltner_state.get('reason')
            if keltner_state.get('ready'):
                keltner_squeeze_on = bool(keltner_state.get('squeeze_on'))
            squeeze_threshold = float(cfg.get('squeeze_percentile_max', 25.0) or 25.0)
            release_threshold = float(cfg.get('squeeze_release_range_min', 1.05) or 1.05)
            squeeze_condition = (
                bool(keltner_squeeze_on)
                or (self._is_valid_number(bb_width_percentile) and float(bb_width_percentile) <= squeeze_threshold)
            )
            release_condition = self._is_valid_number(range_expansion_ratio) and float(range_expansion_ratio) >= release_threshold
            if squeeze_condition and release_condition:
                squeeze_release_state = 'released'
            elif squeeze_condition:
                squeeze_release_state = 'compressed'
            elif keltner_squeeze_on is None and not self._is_valid_number(bb_width_percentile):
                squeeze_release_state = 'pending'
            else:
                squeeze_release_state = 'inactive'
        except Exception as exc:
            keltner_squeeze_reason = str(exc)

        close_delta = close.diff().fillna(0.0)
        signed_volume = pd.Series(
            np.where(close_delta > 0, volume, np.where(close_delta < 0, -volume, 0.0)),
            index=close.index,
            dtype=float
        )
        obv = signed_volume.cumsum()
        obv_slope = float(obv.iloc[-1] - obv.iloc[-6]) if len(obv) >= 6 else np.nan
        avg_volume20 = volume.rolling(20).mean().iloc[-1] if len(volume) >= 20 else np.nan
        obv_slope_ratio = obv_slope / max(abs(float(avg_volume20)), 1e-9) if self._is_valid_number(avg_volume20) else np.nan

        money_flow = typical_price * volume
        tp_delta = typical_price.diff().fillna(0.0)
        positive_flow = money_flow.where(tp_delta > 0, 0.0).rolling(14).sum()
        negative_flow = money_flow.where(tp_delta < 0, 0.0).abs().rolling(14).sum().replace(0.0, np.nan)
        mfi = 100.0 - (100.0 / (1.0 + (positive_flow / negative_flow)))
        mfi_value = float(mfi.iloc[-1]) if self._is_valid_number(mfi.iloc[-1]) else np.nan

        psar_direction, psar_value, psar_reason = self._calculate_utbreakout_psar_direction(local)
        tenkan = (high.rolling(9).max() + low.rolling(9).min()) / 2.0
        kijun = (high.rolling(26).max() + low.rolling(26).min()) / 2.0
        span_a = (tenkan + kijun) / 2.0
        span_b = (high.rolling(52).max() + low.rolling(52).min()) / 2.0
        ichimoku_a = float(span_a.iloc[-1]) if self._is_valid_number(span_a.iloc[-1]) else np.nan
        ichimoku_b = float(span_b.iloc[-1]) if self._is_valid_number(span_b.iloc[-1]) else np.nan
        ichimoku_top = max(ichimoku_a, ichimoku_b) if self._is_valid_number(ichimoku_a) and self._is_valid_number(ichimoku_b) else np.nan
        ichimoku_bottom = min(ichimoku_a, ichimoku_b) if self._is_valid_number(ichimoku_a) and self._is_valid_number(ichimoku_b) else np.nan
        ichimoku_bias = (
            'long' if self._is_valid_number(ichimoku_top) and curr_close > ichimoku_top
            else 'short' if self._is_valid_number(ichimoku_bottom) and curr_close < ichimoku_bottom
            else 'neutral'
        )
        session_hour_kst = None
        try:
            session_hour_kst = datetime.fromtimestamp(
                int(local.iloc[-1].get('timestamp') or 0) / 1000,
                timezone.utc
            ).astimezone(timezone(timedelta(hours=9))).hour
        except Exception:
            session_hour_kst = None

        metrics.update({
            'ready': True,
            'reason': 'OK',
            'timestamp': int(local.iloc[-1].get('timestamp') or 0),
            'open': curr_open,
            'close': curr_close,
            'prev_close': prev_close,
            'ema_fast': ema_fast,
            'ema_fast_prev': ema_fast_prev,
            'ema_slow': ema_slow,
            'ema_gap_pct': ema_gap_pct,
            'ema_bias': ema_bias,
            'rsi': rsi_value,
            'macd_hist': macd_hist_value,
            'macd_hist_prev': macd_hist_prev,
            'roc_pct': roc_pct,
            'cci': cci_value,
            'stoch_k': stoch_k_value,
            'stoch_d': stoch_d_value,
            'adx': adx_value,
            'plus_di': plus_di,
            'minus_di': minus_di,
            'adx_reason': adx_reason,
            'atr': atr_value,
            'atr_pct': atr_pct,
            'chop': chop_value,
            'chop_reason': chop_reason,
            'vortex_plus': vortex_plus,
            'vortex_minus': vortex_minus,
            'vortex_reason': vortex_reason,
            'aroon_up': aroon_up,
            'aroon_down': aroon_down,
            'aroon_reason': aroon_reason,
            'vwap': vwap_value,
            'vwap_slope': vwap_slope,
            'vwap_reason': vwap_reason,
            'donchian_high_prev': don_high_prev,
            'donchian_low_prev': don_low_prev,
            'donchian_width_pct': don_width_pct,
            'donchian_ready': don_ready,
            'bb_upper': bb_upper_value,
            'bb_lower': bb_lower_value,
            'bb_mid': bb_mid_value,
            'bb_width_pct': bb_width_pct,
            'bb_width_prev_pct': bb_width_prev_pct,
            'bb_width_min_pct': bb_width_min_pct,
            'bb_width_percentile': bb_width_percentile,
            'keltner_upper': keltner_upper_value,
            'keltner_lower': keltner_lower_value,
            'keltner_mid': keltner_mid_value,
            'keltner_width_pct': keltner_width_pct,
            'keltner_width_prev_pct': keltner_width_prev_pct,
            'keltner_squeeze_on': keltner_squeeze_on,
            'keltner_squeeze_reason': keltner_squeeze_reason,
            'candle_range_pct': candle_range_pct,
            'avg_range20_pct': avg_range20_pct,
            'avg_range50_pct': avg_range50_pct,
            'range_expansion_ratio': range_expansion_ratio,
            'range_compression_ratio': range_compression_ratio,
            'squeeze_release_state': squeeze_release_state,
            'volume_ratio': volume_ratio,
            'obv_slope': obv_slope,
            'obv_slope_ratio': obv_slope_ratio,
            'mfi': mfi_value,
            'psar_direction': psar_direction,
            'psar': psar_value,
            'psar_reason': psar_reason,
            'ichimoku_a': ichimoku_a,
            'ichimoku_b': ichimoku_b,
            'ichimoku_top': ichimoku_top,
            'ichimoku_bottom': ichimoku_bottom,
            'ichimoku_bias': ichimoku_bias,
            'session_hour_kst': session_hour_kst,
        })
        return metrics

    def _utbreakout_score_from_metrics(self, tf_metrics):
        ready_metrics = [m for m in tf_metrics.values() if isinstance(m, dict) and m.get('ready')]

        def _valid_values(key):
            vals = []
            for item in ready_metrics:
                value = item.get(key)
                if self._is_valid_number(value):
                    vals.append(float(value))
            return vals

        def _avg(values, default=0.0):
            return float(sum(values) / len(values)) if values else float(default)

        adx_vals = _valid_values('adx')
        gap_vals = _valid_values('ema_gap_pct')
        chop_vals = _valid_values('chop')
        atr_vals = _valid_values('atr_pct')
        rsi_vals = _valid_values('rsi')
        don_width_vals = _valid_values('donchian_width_pct')
        volume_vals = _valid_values('volume_ratio')
        bb_width_vals = _valid_values('bb_width_pct')
        bb_width_prev_vals = _valid_values('bb_width_prev_pct')
        bb_width_min_vals = _valid_values('bb_width_min_pct')
        bb_width_percentile_vals = _valid_values('bb_width_percentile')
        keltner_width_vals = _valid_values('keltner_width_pct')
        keltner_width_prev_vals = _valid_values('keltner_width_prev_pct')
        range_expansion_vals = _valid_values('range_expansion_ratio')
        range_compression_vals = _valid_values('range_compression_ratio')
        roc_vals = _valid_values('roc_pct')
        cci_vals = _valid_values('cci')
        stoch_k_vals = _valid_values('stoch_k')
        stoch_d_vals = _valid_values('stoch_d')
        obv_vals = _valid_values('obv_slope_ratio')
        mfi_vals = _valid_values('mfi')

        def _scale(value, low, high):
            try:
                if not np.isfinite(float(value)) or high <= low:
                    return 0.0
                return min(100.0, max(0.0, (float(value) - float(low)) / (float(high) - float(low)) * 100.0))
            except (TypeError, ValueError):
                return 0.0

        def _atr_normal_score(value):
            try:
                value = float(value)
            except (TypeError, ValueError):
                return 0.0
            if not np.isfinite(value):
                return 0.0
            if value < 0.12:
                return _scale(value, 0.04, 0.12) * 0.55
            if value <= 0.45:
                return 58.0 + _scale(value, 0.12, 0.45) * 34.0 / 100.0
            if value <= 1.20:
                return 92.0 - _scale(value, 0.45, 1.20) * 24.0 / 100.0
            return max(0.0, 68.0 - _scale(value, 1.20, 2.20) * 68.0 / 100.0)

        adx_score = _avg([min(100.0, max(0.0, (v - 10.0) / 25.0 * 100.0)) for v in adx_vals], 35.0)
        gap_score = _avg([min(100.0, max(0.0, v / 0.80 * 100.0)) for v in gap_vals], 35.0)
        chop_trend_score = _avg([min(100.0, max(0.0, (62.0 - v) / 24.0 * 100.0)) for v in chop_vals], 45.0)
        chop_score = _avg([min(100.0, max(0.0, (v - 38.0) / 24.0 * 100.0)) for v in chop_vals], 45.0)
        volatility_score = _avg([_atr_normal_score(v) for v in atr_vals], 55.0)
        low_vol_score = _avg([max(0.0, 100.0 - _scale(v, 0.08, 0.35)) for v in atr_vals], 35.0)
        high_vol_score = _avg([_scale(v, 0.75, 1.60) for v in atr_vals], 30.0)
        rsi_momentum_score = _avg([min(100.0, abs(v - 50.0) * 4.0) for v in rsi_vals], 40.0)
        roc_score = _avg([min(100.0, abs(v) / 1.20 * 100.0) for v in roc_vals], 35.0)
        cci_score = _avg([min(100.0, abs(v) / 160.0 * 100.0) for v in cci_vals], 35.0)
        stoch_scores = []
        for item in ready_metrics:
            stoch_k = item.get('stoch_k')
            stoch_d = item.get('stoch_d')
            if self._is_valid_number(stoch_k) and self._is_valid_number(stoch_d):
                stoch_scores.append(min(100.0, abs(float(stoch_k) - float(stoch_d)) / 30.0 * 100.0))
        stoch_score = _avg(stoch_scores, 35.0)
        macd_scores = []
        for item in ready_metrics:
            macd_hist = item.get('macd_hist')
            close_value = item.get('close')
            if self._is_valid_number(macd_hist) and self._is_valid_number(close_value):
                macd_pct = abs(float(macd_hist)) / max(abs(float(close_value)), 1e-9) * 100.0
                macd_scores.append(min(100.0, macd_pct / 0.08 * 100.0))
        macd_score = _avg(macd_scores, 35.0)
        momentum_score = (rsi_momentum_score * 0.32) + (macd_score * 0.24) + (roc_score * 0.20) + (cci_score * 0.14) + (stoch_score * 0.10)
        breakout_score = _avg([min(100.0, max(0.0, v / 1.0 * 100.0)) for v in don_width_vals], 40.0)
        if bb_width_vals:
            breakout_score = (breakout_score + _avg([min(100.0, max(0.0, v / 1.5 * 100.0)) for v in bb_width_vals])) / 2.0
        flow_score = _avg([min(100.0, max(0.0, (v - 0.7) / 1.3 * 100.0)) for v in volume_vals], 40.0)
        volume_spike_score = _avg([_scale(v, 1.20, 2.40) for v in volume_vals], 30.0)
        relative_volume_score = _avg([_scale(v, 0.80, 1.80) for v in volume_vals], 40.0)
        obv_score = _avg([min(100.0, abs(v) / 3.0 * 100.0) for v in obv_vals], 35.0)
        mfi_score = _avg([min(100.0, abs(v - 50.0) * 4.0) for v in mfi_vals], 35.0)
        vwap_scores = []
        pullback_scores = []
        psar_votes = {'long': 0, 'short': 0}
        ichimoku_votes = {'long': 0, 'short': 0}
        session_scores = []
        for item in ready_metrics:
            close_value = item.get('close')
            vwap_slope = item.get('vwap_slope')
            if self._is_valid_number(vwap_slope) and self._is_valid_number(close_value):
                slope_pct = abs(float(vwap_slope)) / max(abs(float(close_value)), 1e-9) * 100.0
                vwap_scores.append(min(100.0, slope_pct / 0.03 * 100.0))
            distances = []
            for key, max_dist in [('vwap', 0.55), ('ema_fast', 0.80), ('bb_mid', 0.65)]:
                ref = item.get(key)
                if self._is_valid_number(ref) and self._is_valid_number(close_value):
                    dist_pct = abs(float(close_value) - float(ref)) / max(abs(float(close_value)), 1e-9) * 100.0
                    distances.append(max(0.0, 100.0 - min(100.0, dist_pct / max_dist * 100.0)))
            if distances:
                pullback_scores.append(max(distances))
            psar_direction = str(item.get('psar_direction') or '').lower()
            if psar_direction in psar_votes:
                psar_votes[psar_direction] += 1
            ichimoku_bias = str(item.get('ichimoku_bias') or '').lower()
            if ichimoku_bias in ichimoku_votes:
                ichimoku_votes[ichimoku_bias] += 1
            hour = item.get('session_hour_kst')
            atr_pct = item.get('atr_pct')
            volume_ratio = item.get('volume_ratio')
            if hour is not None:
                active_session = int(hour) in {0, 1, 2, 8, 9, 10, 16, 17, 18, 19, 20, 21, 22, 23}
                atr_ok = self._is_valid_number(atr_pct) and float(atr_pct) >= 0.12
                vol_ok = self._is_valid_number(volume_ratio) and float(volume_ratio) >= 0.9
                session_scores.append(85.0 if active_session and (atr_ok or vol_ok) else 35.0 if active_session else 15.0)
        vwap_score = _avg(vwap_scores, 35.0)
        pullback_score = _avg(pullback_scores, 35.0)
        psar_total = psar_votes['long'] + psar_votes['short']
        psar_score = abs(psar_votes['long'] - psar_votes['short']) / max(psar_total, 1) * 100.0 if psar_total else 35.0
        ichimoku_total = ichimoku_votes['long'] + ichimoku_votes['short']
        ichimoku_score = abs(ichimoku_votes['long'] - ichimoku_votes['short']) / max(ichimoku_total, 1) * 100.0 if ichimoku_total else 35.0
        session_score = _avg(session_scores, 35.0)

        bb_expansion_scores = []
        squeeze_release_scores = []
        squeeze_release_auto_scores = []
        squeeze_avoid_scores = []
        for item in ready_metrics:
            width = item.get('bb_width_pct')
            prev = item.get('bb_width_prev_pct')
            min_width = item.get('bb_width_min_pct')
            if self._is_valid_number(width) and self._is_valid_number(prev):
                bb_expansion_scores.append(
                    50.0 + min(50.0, max(-50.0, ((float(width) - float(prev)) / max(abs(float(prev)), 1e-9)) * 160.0))
                )
            if self._is_valid_number(width) and self._is_valid_number(min_width):
                ratio = float(width) / max(float(min_width), 1e-9)
                squeeze_release_scores.append(_scale(ratio, 1.05, 1.90))
                squeeze_avoid_scores.append(_scale(ratio, 1.05, 1.80))
            percentile = item.get('bb_width_percentile')
            range_ratio = item.get('range_expansion_ratio')
            keltner_squeeze = item.get('keltner_squeeze_on')
            compression_score = None
            if self._is_valid_number(percentile):
                compression_score = max(0.0, min(100.0, (35.0 - float(percentile)) / 35.0 * 100.0))
            elif keltner_squeeze is True:
                compression_score = 80.0
            if compression_score is not None and self._is_valid_number(range_ratio):
                squeeze_release_auto_scores.append((compression_score * 0.55) + (_scale(float(range_ratio), 1.05, 1.90) * 0.45))
        bb_expansion_score = _avg(bb_expansion_scores, 40.0)
        if squeeze_release_scores:
            squeeze_release = _avg(squeeze_release_scores, 40.0)
            bb_expansion_score = (bb_expansion_score + squeeze_release) / 2.0
        squeeze_release_score = _avg(squeeze_release_auto_scores, _avg([100.0 - min(100.0, v) for v in bb_width_percentile_vals], 40.0))

        keltner_expansion_scores = []
        for item in ready_metrics:
            width = item.get('keltner_width_pct')
            prev = item.get('keltner_width_prev_pct')
            if self._is_valid_number(width) and self._is_valid_number(prev):
                keltner_expansion_scores.append(
                    50.0 + min(50.0, max(-50.0, ((float(width) - float(prev)) / max(abs(float(prev)), 1e-9)) * 160.0))
                )
        keltner_expansion_score = _avg(keltner_expansion_scores, 40.0)
        range_expansion_score = _avg([_scale(v, 0.95, 2.20) for v in range_expansion_vals], 40.0)
        squeeze_avoid_score = _avg(squeeze_avoid_scores, 45.0)
        compression_avoid_score = _avg([_scale(v, 0.65, 1.10) for v in range_compression_vals], 45.0)

        long_votes = 0
        short_votes = 0
        for item in ready_metrics:
            if item.get('ema_bias') == 'long':
                long_votes += 1
            elif item.get('ema_bias') == 'short':
                short_votes += 1
            rsi = item.get('rsi')
            if self._is_valid_number(rsi):
                if float(rsi) > 52:
                    long_votes += 1
                elif float(rsi) < 48:
                    short_votes += 1
            plus_di = item.get('plus_di')
            minus_di = item.get('minus_di')
            if self._is_valid_number(plus_di) and self._is_valid_number(minus_di):
                if float(plus_di) > float(minus_di):
                    long_votes += 1
                elif float(minus_di) > float(plus_di):
                    short_votes += 1
        total_votes = long_votes + short_votes
        alignment_score = abs(long_votes - short_votes) / max(total_votes, 1) * 100.0
        dominant_side = 'long' if long_votes > short_votes else 'short' if short_votes > long_votes else 'neutral'
        trend_score = (adx_score * 0.45) + (gap_score * 0.25) + (chop_trend_score * 0.30)
        mtf_momentum_score = (momentum_score * 0.60) + (alignment_score * 0.40)
        mtf_volatility_score = (volatility_score * 0.70) + (min(100.0, len(ready_metrics) / max(len(tf_metrics), 1) * 100.0) * 0.30)
        clarity_score = max(trend_score, breakout_score, momentum_score, alignment_score)
        fallback_score = max(0.0, 100.0 - clarity_score)

        return {
            'trend_score': round(trend_score, 2),
            'chop_score': round(chop_score, 2),
            'volatility_score': round(volatility_score, 2),
            'low_vol_score': round(low_vol_score, 2),
            'high_vol_score': round(high_vol_score, 2),
            'breakout_score': round(breakout_score, 2),
            'momentum_score': round(momentum_score, 2),
            'rsi_momentum_score': round(rsi_momentum_score, 2),
            'macd_score': round(macd_score, 2),
            'roc_score': round(roc_score, 2),
            'cci_score': round(cci_score, 2),
            'stoch_score': round(stoch_score, 2),
            'bb_expansion_score': round(bb_expansion_score, 2),
            'keltner_expansion_score': round(keltner_expansion_score, 2),
            'range_expansion_score': round(range_expansion_score, 2),
            'squeeze_release_score': round(squeeze_release_score, 2),
            'pullback_score': round(pullback_score, 2),
            'flow_score': round(flow_score, 2),
            'volume_spike_score': round(volume_spike_score, 2),
            'relative_volume_score': round(relative_volume_score, 2),
            'obv_score': round(obv_score, 2),
            'mfi_score': round(mfi_score, 2),
            'vwap_score': round(vwap_score, 2),
            'squeeze_avoid_score': round(squeeze_avoid_score, 2),
            'compression_avoid_score': round(compression_avoid_score, 2),
            'alignment_score': round(alignment_score, 2),
            'mtf_momentum_score': round(mtf_momentum_score, 2),
            'mtf_volatility_score': round(mtf_volatility_score, 2),
            'psar_score': round(psar_score, 2),
            'ichimoku_score': round(ichimoku_score, 2),
            'session_score': round(session_score, 2),
            'fallback_score': round(fallback_score, 2),
            'dominant_side': dominant_side,
            'ready_timeframes': len(ready_metrics),
        }

    async def _build_utbreakout_auto_analysis(self, symbol, base_df, cfg):
        timeframes = list(cfg.get('auto_timeframes') or UTBREAKOUT_AUTO_TIMEFRAMES)
        if bool(cfg.get('adaptive_timeframe_enabled', False)):
            for tf in list(cfg.get('adaptive_timeframes') or UTBREAKOUT_AUTO_TIMEFRAMES):
                if tf not in timeframes:
                    timeframes.append(tf)
        entry_tf = str(cfg.get('entry_timeframe', '15m') or '15m').lower()
        timeframe_metrics = {}
        errors = {}
        for tf in timeframes:
            tf = str(tf or '').strip().lower()
            if not tf:
                continue
            try:
                if tf == entry_tf and base_df is not None and len(base_df) > 0:
                    tf_df = base_df.copy()
                else:
                    ohlcv = await asyncio.to_thread(
                        self.market_data_exchange.fetch_ohlcv,
                        symbol,
                        tf,
                        limit=300
                    )
                    tf_df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    tf_df[col] = pd.to_numeric(tf_df[col], errors='coerce')
                tf_closed = tf_df.iloc[:-1].dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
                timeframe_metrics[tf] = self._calculate_utbreakout_timeframe_metrics(tf_closed, cfg)
            except Exception as exc:
                errors[tf] = str(exc)
                timeframe_metrics[tf] = {'ready': False, 'reason': str(exc)}
        scores = self._utbreakout_score_from_metrics(timeframe_metrics)
        futures_context = {}
        try:
            futures_context = await self._fetch_utbreakout_futures_context(symbol)
            if isinstance(futures_context, dict):
                futures_context = dict(futures_context)
                futures_context['volatility_forecast_score'] = scores.get('volatility_score')
            futures_scores = self._score_prediction_futures_context(futures_context)
            scores.update(futures_scores)
        except Exception as exc:
            errors['futures_context'] = str(exc)
        return {
            'timeframes': timeframe_metrics,
            'scores': scores,
            'futures_context': futures_context,
            'errors': errors,
        }

    async def _resolve_utbreakout_adaptive_timeframe(self, symbol, base_df, cfg, pos=None):
        if not bool(cfg.get('adaptive_timeframe_enabled', False)):
            return cfg, base_df, None
        analysis = await self._build_utbreakout_auto_analysis(symbol, base_df, cfg)
        state = dict(self.utbreakout_adaptive_tf_state.get(symbol, {}) or {})
        position_side = str((pos or {}).get('side') or 'none').lower() if isinstance(pos, dict) else 'none'
        decision = select_adaptive_timeframe(
            analysis.get('timeframes', {}),
            cfg,
            state=state,
            position_side=position_side,
        )
        selected_tf = decision.get('selected_tf')
        previous_tf = decision.get('previous_tf')
        if selected_tf:
            next_state = dict(state)
            next_state['selected_tf'] = selected_tf
            next_state['selected_score'] = decision.get('selected_score')
            next_state['last_reason'] = decision.get('reason')
            next_state['last_decision'] = decision.get('decision')
            next_state['last_decision_ts'] = decision.get('selected_timestamp')
            if selected_tf != previous_tf:
                next_state['last_switch_ts'] = decision.get('selected_timestamp') or int(time.time() * 1000)
            elif 'last_switch_ts' not in next_state:
                next_state['last_switch_ts'] = decision.get('selected_timestamp') or int(time.time() * 1000)
            self.utbreakout_adaptive_tf_state[symbol] = next_state

        effective_cfg = dict(cfg)
        effective_cfg['_auto_analysis_override'] = analysis
        effective_cfg['_adaptive_timeframe_decision'] = decision
        if not selected_tf:
            return effective_cfg, base_df, decision

        effective_cfg['entry_timeframe'] = selected_tf
        effective_cfg['exit_timeframe'] = selected_tf
        effective_cfg['htf_timeframe'] = decision.get('htf_timeframe') or UTBREAKOUT_HTF_MAP.get(selected_tf, '1h')
        selected_df = base_df
        base_tf = str(cfg.get('entry_timeframe', '15m') or '15m').lower()
        if selected_tf != base_tf:
            ohlcv = await asyncio.to_thread(
                self.market_data_exchange.fetch_ohlcv,
                symbol,
                selected_tf,
                limit=300
            )
            selected_df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            for col in ['open', 'high', 'low', 'close', 'volume']:
                selected_df[col] = pd.to_numeric(selected_df[col], errors='coerce')
        return effective_cfg, selected_df, decision

    def _format_adaptive_timeframe_summary(self, decision):
        if not isinstance(decision, dict):
            return "Adaptive TF 분석 기록 없음"
        top3 = decision.get('top3') or []
        top_text = ", ".join(
            f"{item.get('tf')}:{float(item.get('score', 0) or 0):.1f}"
            for item in top3[:3]
        ) or "n/a"
        selected = decision.get('selected_tf') or "NO_TRADE"
        return f"{selected} ({decision.get('decision', 'WAIT')}) | {decision.get('reason', '')} | top {top_text}"

    def _select_utbreakout_auto_set(self, analysis, cfg, previous_set_id=None):
        raw_scores = (analysis or {}).get('scores') if isinstance(analysis, dict) else None
        scores = dict(raw_scores or {})
        if bool((cfg or {}).get('ev_adaptive_enabled', False)):
            if isinstance(raw_scores, dict):
                raw_scores.update({
                    'auto_candidate_scores_raw': {'Set64': 100.0},
                    'auto_candidate_scores_adjusted': {'Set64': 100.0},
                    'auto_candidate_scores': {'Set64': 100.0},
                    'auto_selected_score': 100.0,
                    'auto_selected_raw_score': 100.0,
                    'auto_score_margin': 100.0,
                    'auto_confidence': 'ev_adaptive',
                    'auto_final_set_id': 64,
                })
            return 64, f"EV Adaptive {EV_ADAPTIVE_PROFILE_VERSION.replace('ev_adaptive_', '').upper()} single live router (legacy Sets research-only)"
        trend = float(scores.get('trend_score', 0.0) or 0.0)
        chop = float(scores.get('chop_score', 0.0) or 0.0)
        volatility = float(scores.get('volatility_score', 0.0) or 0.0)
        low_vol = float(scores.get('low_vol_score', 0.0) or 0.0)
        high_vol = float(scores.get('high_vol_score', 0.0) or 0.0)
        breakout = float(scores.get('breakout_score', 0.0) or 0.0)
        momentum = float(scores.get('momentum_score', 0.0) or 0.0)
        rsi_momentum = float(scores.get('rsi_momentum_score', momentum) or 0.0)
        macd = float(scores.get('macd_score', momentum) or 0.0)
        roc = float(scores.get('roc_score', momentum) or 0.0)
        cci = float(scores.get('cci_score', momentum) or 0.0)
        stoch = float(scores.get('stoch_score', momentum) or 0.0)
        bb_expansion = float(scores.get('bb_expansion_score', breakout) or 0.0)
        keltner_expansion = float(scores.get('keltner_expansion_score', breakout) or 0.0)
        range_expansion = float(scores.get('range_expansion_score', breakout) or 0.0)
        squeeze_release = float(scores.get('squeeze_release_score', bb_expansion) or 0.0)
        pullback = float(scores.get('pullback_score', 0.0) or 0.0)
        flow = float(scores.get('flow_score', 0.0) or 0.0)
        volume_spike = float(scores.get('volume_spike_score', flow) or 0.0)
        relative_volume = float(scores.get('relative_volume_score', flow) or 0.0)
        obv = float(scores.get('obv_score', flow) or 0.0)
        mfi = float(scores.get('mfi_score', flow) or 0.0)
        vwap = float(scores.get('vwap_score', flow) or 0.0)
        squeeze_avoid = float(scores.get('squeeze_avoid_score', 0.0) or 0.0)
        compression_avoid = float(scores.get('compression_avoid_score', 0.0) or 0.0)
        alignment = float(scores.get('alignment_score', 0.0) or 0.0)
        mtf_momentum = float(scores.get('mtf_momentum_score', 0.0) or 0.0)
        mtf_volatility = float(scores.get('mtf_volatility_score', volatility) or 0.0)
        psar = float(scores.get('psar_score', trend) or 0.0)
        ichimoku = float(scores.get('ichimoku_score', trend) or 0.0)
        session = float(scores.get('session_score', 0.0) or 0.0)
        fallback = float(scores.get('fallback_score', 0.0) or 0.0)
        prediction_orderflow = float(scores.get('prediction_orderflow_score', 0.0) or 0.0)
        prediction_depth = float(scores.get('prediction_depth_score', 0.0) or 0.0)
        prediction_oi_funding = float(scores.get('prediction_oi_funding_score', 0.0) or 0.0)
        prediction_liquidation = float(scores.get('prediction_liquidation_score', 0.0) or 0.0)
        prediction_odds = float(scores.get('prediction_odds_score', 0.0) or 0.0)
        prediction_event_guard = float(scores.get('prediction_event_guard_score', 0.0) or 0.0)
        prediction_volatility = float(scores.get('prediction_volatility_score', 0.0) or 0.0)
        prediction_basis = float(scores.get('prediction_basis_score', 0.0) or 0.0)
        prediction_cross_market = float(scores.get('prediction_cross_market_score', 0.0) or 0.0)
        rolling_orderflow = float(scores.get('rolling_orderflow_score', prediction_orderflow) or 0.0)
        oi_funding_squeeze = float(scores.get('oi_funding_squeeze_score', prediction_oi_funding) or 0.0)
        ready = int(scores.get('ready_timeframes', 0) or 0)

        if ready <= 0:
            emergency_allowed = (
                _parse_bool_like(cfg.get('emergency_mode', False), False)
                or _parse_bool_like(cfg.get('allow_emergency_simple_set', False), False)
            )
            fallback_id = 50 if emergency_allowed else normalize_utbreakout_set_id(
                cfg.get('safe_live_default_set_id', UTBREAKOUT_SAFE_LIVE_DEFAULT_SET_ID),
                UTBREAKOUT_SAFE_LIVE_DEFAULT_SET_ID
            )
            if fallback_id in {1, 50} and not emergency_allowed:
                fallback_id = UTBREAKOUT_SAFE_LIVE_DEFAULT_SET_ID
            return fallback_id, f"AUTO 분석 데이터 부족: Set{fallback_id} safety fallback"

        clarity = max(trend, breakout, momentum, alignment)
        uncertainty = 100.0 - clarity
        trendability = 100.0 - chop
        volatility_balance = 100.0 - abs(volatility - 72.0)
        candidate_scores = {
            1: 34.0 + (uncertainty * 0.20) + (chop * 0.08) - (trend * 0.04) - (breakout * 0.03),
            2: 33.0 + (volatility_balance * 0.18) + (uncertainty * 0.10) + (low_vol * 0.04) + (high_vol * 0.03),
            3: 31.0 + (alignment * 0.31) + (trend * 0.16) + (trendability * 0.05),
            4: 31.0 + (momentum * 0.26) + (trend * 0.10) + (vwap * 0.05),
            5: 28.0 + (trend * 0.24) + (alignment * 0.22) + (trendability * 0.10),
            6: 33.0 + (trend * 0.26) + (volatility * 0.07) + (uncertainty * 0.04),
            7: 26.0 + (trend * 0.20) + (alignment * 0.18) + (momentum * 0.10) + (trendability * 0.05),
            8: 30.0 + (trendability * 0.30) + (trend * 0.14) + (compression_avoid * 0.04),
            9: 30.0 + (momentum * 0.18) + (flow * 0.15) + (trend * 0.10) + (alignment * 0.06),
            10: 30.0 + (breakout * 0.28) + (momentum * 0.13) + (trend * 0.08) + (range_expansion * 0.05),
            11: 30.0 + (rsi_momentum * 0.33) + (alignment * 0.10) + (volatility * 0.05),
            12: 30.0 + (macd * 0.34) + (momentum * 0.10) + (trend * 0.05),
            13: 31.0 + (roc * 0.34) + (range_expansion * 0.08) + (relative_volume * 0.05),
            14: 29.0 + (cci * 0.34) + (momentum * 0.09) + (trendability * 0.05),
            15: 31.0 + (stoch * 0.32) + (uncertainty * 0.08) + (relative_volume * 0.05),
            16: 31.0 + (volatility_balance * 0.28) + (flow * 0.08) + (uncertainty * 0.04),
            17: 28.0 + (low_vol * 0.34) + (squeeze_avoid * 0.10) + (relative_volume * 0.06),
            18: 27.0 + (high_vol * 0.34) + (volatility * 0.08) + (trend * 0.06),
            19: 29.0 + (bb_expansion * 0.34) + (range_expansion * 0.10) + (momentum * 0.05),
            20: 29.0 + (keltner_expansion * 0.34) + (volatility * 0.08) + (trend * 0.06),
            21: 28.0 + (breakout * 0.26) + (range_expansion * 0.14) + (relative_volume * 0.06),
            22: 27.0 + (breakout * 0.30) + (trend * 0.12) + (alignment * 0.05),
            23: 28.0 + (bb_expansion * 0.28) + (breakout * 0.14) + (volume_spike * 0.05),
            24: 28.0 + (keltner_expansion * 0.28) + (breakout * 0.12) + (trend * 0.07),
            25: 29.0 + (range_expansion * 0.34) + (volume_spike * 0.12) + (momentum * 0.04),
            26: 29.0 + (pullback * 0.30) + (vwap * 0.16) + (trendability * 0.05),
            27: 29.0 + (pullback * 0.30) + (trend * 0.14) + (volatility * 0.05),
            28: 28.0 + (pullback * 0.28) + (bb_expansion * 0.10) + (momentum * 0.08),
            29: 29.0 + (pullback * 0.26) + (rsi_momentum * 0.16) + (uncertainty * 0.05),
            30: 28.0 + (trend * 0.24) + (volatility * 0.16) + (psar * 0.06),
            31: 28.0 + (volume_spike * 0.36) + (range_expansion * 0.08) + (momentum * 0.05),
            32: 30.0 + (relative_volume * 0.34) + (flow * 0.12) + (volatility * 0.04),
            33: 29.0 + (obv * 0.34) + (flow * 0.10) + (alignment * 0.06),
            34: 29.0 + (mfi * 0.34) + (flow * 0.10) + (momentum * 0.05),
            35: 29.0 + (vwap * 0.34) + (pullback * 0.08) + (trend * 0.05),
            36: 29.0 + (trendability * 0.26) + (compression_avoid * 0.12) + (squeeze_avoid * 0.05),
            37: 28.0 + (breakout * 0.20) + (compression_avoid * 0.18) + (uncertainty * 0.04),
            38: 29.0 + (trend * 0.20) + (alignment * 0.16) + (trendability * 0.12),
            39: 28.0 + (squeeze_avoid * 0.32) + (bb_expansion * 0.10) + (uncertainty * 0.04),
            40: 28.0 + (compression_avoid * 0.34) + (trendability * 0.08) + (volatility * 0.05),
            41: 29.0 + (alignment * 0.24) + (mtf_momentum * 0.16) + (momentum * 0.05),
            42: 29.0 + (alignment * 0.27) + (trend * 0.18) + (mtf_momentum * 0.06),
            43: 28.0 + (alignment * 0.30) + (trend * 0.18) + (ichimoku * 0.05),
            44: 29.0 + (mtf_momentum * 0.34) + (alignment * 0.08) + (momentum * 0.05),
            45: 29.0 + (mtf_volatility * 0.34) + (volatility_balance * 0.08) + (ready * 1.2),
            46: 29.0 + (psar * 0.34) + (trend * 0.08) + (volatility * 0.05),
            47: 28.0 + (ichimoku * 0.34) + (alignment * 0.12) + (trend * 0.06),
            48: 29.0 + (session * 0.34) + (relative_volume * 0.08) + (volatility * 0.05),
            49: 31.0 + (fallback * 0.32) + (chop * 0.08) + (uncertainty * 0.08),
            50: 20.0 + (max(0.0, 3.0 - ready) * 8.0) + (fallback * 0.22) + (uncertainty * 0.06),
            51: 24.0 + (prediction_orderflow * 0.38) + (prediction_depth * 0.12) + (flow * 0.05),
            52: 25.0 + (prediction_oi_funding * 0.40) + (trend * 0.08) + (volatility * 0.04),
            53: 22.0 + (prediction_liquidation * 0.42) + (range_expansion * 0.12) + (high_vol * 0.04),
            54: 21.0 + (prediction_odds * 0.44) + (alignment * 0.08) + (momentum * 0.04),
            55: 25.0 + (prediction_event_guard * 0.40) + (volatility_balance * 0.08) + (session * 0.04),
            56: 24.0 + (prediction_volatility * 0.36) + (bb_expansion * 0.08) + (keltner_expansion * 0.08),
            57: 24.0 + (prediction_depth * 0.42) + (relative_volume * 0.06) + (volatility * 0.04),
            58: 24.0 + (prediction_basis * 0.40) + (prediction_oi_funding * 0.08) + (trend * 0.04),
            59: 20.0 + (prediction_odds * 0.42) + (prediction_cross_market * 0.12) + (momentum * 0.04),
            60: 20.0 + (prediction_cross_market * 0.44) + (alignment * 0.08) + (trend * 0.04),
            61: 23.0 + (rolling_orderflow * 0.40) + (prediction_depth * 0.12) + (flow * 0.05),
            62: 23.0 + (oi_funding_squeeze * 0.42) + (prediction_basis * 0.08) + (trend * 0.05),
            63: 24.0 + (squeeze_release * 0.38) + (range_expansion * 0.12) + (volatility * 0.05),
        }
        raw_candidate_scores = dict(candidate_scores)
        is_live_runtime = self._is_utbreakout_live_runtime(cfg)

        if isinstance(raw_scores, dict):
            raw_scores['auto_candidate_scores_raw'] = {
                f"Set{set_id}": round(float(score), 2)
                for set_id, score in sorted(raw_candidate_scores.items())
            }

        if is_live_runtime and bool(cfg.get('live_auto_set_whitelist_enabled', True)):
            whitelist = set(normalize_utbreakout_set_id_list(
                cfg.get('live_auto_set_whitelist'),
                UTBREAKOUT_LIVE_AUTO_SET_WHITELIST,
            ))
            candidate_scores = {
                set_id: score
                for set_id, score in candidate_scores.items()
                if set_id in whitelist
            }
            if isinstance(raw_scores, dict):
                raw_scores['auto_live_whitelist'] = sorted(int(x) for x in whitelist)

            if not candidate_scores:
                fallback_id = normalize_utbreakout_set_id(
                    cfg.get('safe_live_default_set_id', UTBREAKOUT_SAFE_LIVE_DEFAULT_SET_ID),
                    UTBREAKOUT_SAFE_LIVE_DEFAULT_SET_ID,
                )
                if fallback_id in {1, 50}:
                    fallback_id = UTBREAKOUT_SAFE_LIVE_DEFAULT_SET_ID
                if isinstance(raw_scores, dict):
                    raw_scores['auto_blocked_by_whitelist'] = True
                    raw_scores['auto_block_reason'] = 'live AUTO whitelist removed all candidates'
                    raw_scores['auto_final_set_id'] = int(fallback_id)
                return fallback_id, f"AUTO live whitelist 후보 없음 -> Set{fallback_id} safety fallback"

        multiple_testing_penalty_points = 0.0
        if is_live_runtime and bool(cfg.get('auto_multiple_testing_penalty_enabled', True)):
            trial_count = max(1, len(raw_candidate_scores))
            free_trials = int(cfg.get('multiple_testing_free_trials', 10) or 10)
            if trial_count > free_trials:
                try:
                    multiple_testing_penalty_points = min(
                        float(cfg.get('multiple_testing_max_score_penalty', 12.0) or 12.0),
                        2.0 * float(np.log(max(trial_count, 1))),
                    )
                except Exception:
                    multiple_testing_penalty_points = 0.0

            if multiple_testing_penalty_points > 0:
                candidate_scores = {
                    set_id: score - multiple_testing_penalty_points
                    for set_id, score in candidate_scores.items()
                }

        if isinstance(raw_scores, dict):
            raw_scores['auto_multiple_testing_penalty_points'] = round(float(multiple_testing_penalty_points), 2)
            raw_scores['auto_candidate_scores_adjusted'] = {
                f"Set{set_id}": round(float(score), 2)
                for set_id, score in sorted(candidate_scores.items())
            }

        candidate_rows = []
        for set_id, score in candidate_scores.items():
            set_info = get_utbreakout_set_definition(set_id)
            candidate_rows.append({
                'set_id': int(set_id),
                'score': float(score),
                'name': set_info.get('name'),
                'description': set_info.get('description'),
                'filters': list(set_info.get('entry_filters') or []),
            })

        if bool(cfg.get('set_selection_hysteresis_enabled', True)):
            ranked_sets = _rank_utbreakout_sets_stably(
                candidate_rows,
                previous_set_id=previous_set_id,
                cfg=cfg,
            )
        else:
            ranked_sets = [
                {
                    'item': item,
                    'set_id': item['set_id'],
                    'raw_score': item['score'],
                    'adjusted_score': item['score'],
                    'complexity_penalty': 0.0,
                }
                for item in sorted(candidate_rows, key=lambda row: row['score'], reverse=True)
            ]
        if not ranked_sets:
            fallback_id = normalize_utbreakout_set_id(
                cfg.get('safe_live_default_set_id', UTBREAKOUT_SAFE_LIVE_DEFAULT_SET_ID),
                UTBREAKOUT_SAFE_LIVE_DEFAULT_SET_ID,
            )
            return fallback_id, f"AUTO Set ranking empty -> Set{fallback_id} safety fallback"

        selected_row = ranked_sets[0]
        selected_id = int(selected_row['set_id'])
        selected_score = float(selected_row['adjusted_score'])
        selected_raw_score = float(selected_row['raw_score'])
        top3_rows = ranked_sets[:3]
        top3 = [
            (int(row['set_id']), float(row['adjusted_score']))
            for row in top3_rows
        ]
        top3_text = ", ".join(f"Set{set_id}:{score:.1f}" for set_id, score in top3)
        second_score = top3[1][1] if len(top3) > 1 else 0.0
        score_margin = selected_score - second_score
        stable_detail = (
            f"raw={selected_raw_score:.1f}, adjusted={selected_score:.1f}, "
            f"complexity_penalty={float(selected_row['complexity_penalty']):.1f}, "
            f"prev={_safe_set_id(previous_set_id)}"
        )
        if isinstance(raw_scores, dict):
            raw_scores['auto_candidate_scores'] = {
                f"Set{set_id}": round(float(score), 2)
                for set_id, score in sorted(candidate_scores.items())
            }
            raw_scores['auto_top3'] = [
                {
                    'set_id': int(row['set_id']),
                    'score': round(float(row['adjusted_score']), 2),
                    'raw_score': round(float(row['raw_score']), 2),
                    'complexity_penalty': round(float(row['complexity_penalty']), 2),
                }
                for row in top3_rows
            ]
            raw_scores['auto_selected_score'] = round(float(selected_score), 2)
            raw_scores['auto_selected_raw_score'] = round(float(selected_raw_score), 2)
            raw_scores['auto_score_margin'] = round(float(score_margin), 2)
            raw_scores['auto_stable_selection_detail'] = stable_detail
            raw_scores['auto_previous_set_id'] = _safe_set_id(previous_set_id)
            min_score_gap = float(cfg.get('set_selection_min_score_gap', 2.0) or 2.0)
            raw_scores['auto_confidence'] = (
                'weak'
                if selected_score < 50.0 or score_margin < min_score_gap
                else 'normal'
            )
            min_live_score = float(cfg.get('auto_min_adjusted_score_live', 50.0) or 50.0)
            min_live_margin = float(cfg.get('auto_min_score_margin_live', 3.0) or 3.0)
            weak_live_margin = bool(is_live_runtime and score_margin < min_live_margin)
            weak_live_score = bool(is_live_runtime and selected_score < min_live_score)
            raw_scores['auto_live_score_threshold'] = round(float(min_live_score), 2)
            raw_scores['auto_live_margin_threshold'] = round(float(min_live_margin), 2)

            if (
                bool(cfg.get('auto_block_on_weak_margin_live', True))
                and (weak_live_margin or weak_live_score)
            ):
                raw_scores['auto_blocked_by_margin'] = True
                raw_scores['auto_block_reason'] = (
                    f"live AUTO weak edge: score {selected_score:.1f}/{min_live_score:.1f}, "
                    f"margin {score_margin:.1f}/{min_live_margin:.1f}, top {top3_text}"
                )
        if selected_score < 50.0:
            selected_id = 2 if volatility < 45.0 else 49 if fallback >= 55.0 else normalize_utbreakout_set_id(
                cfg.get('safe_live_default_set_id', UTBREAKOUT_SAFE_LIVE_DEFAULT_SET_ID),
                UTBREAKOUT_SAFE_LIVE_DEFAULT_SET_ID
            )
            if selected_id in {1, 50}:
                selected_id = UTBREAKOUT_SAFE_LIVE_DEFAULT_SET_ID
            if isinstance(raw_scores, dict):
                raw_scores['auto_fallback_set_id'] = int(selected_id)
            reason = (
                f"점수 우위 약함 -> Set{selected_id} fallback "
                f"(trend {trend:.1f}, chop {chop:.1f}, vol {volatility:.1f}, momentum {momentum:.1f}, top {top3_text})"
            )
        else:
            reason = (
                f"trend {trend:.1f}, chop {chop:.1f}, vol {volatility:.1f}, "
                f"breakout {breakout:.1f}, momentum {momentum:.1f}, flow {flow:.1f}, align {alignment:.1f}, "
                f"bbExp {bb_expansion:.1f}, keltner {keltner_expansion:.1f}, pullback {pullback:.1f} "
                f"-> Set{selected_id} stable score {selected_score:.1f} "
                f"(penalty {multiple_testing_penalty_points:.1f}, margin {score_margin:.1f}, top {top3_text})"
            )
        if isinstance(raw_scores, dict):
            raw_scores['auto_final_set_id'] = int(selected_id)
        return selected_id, reason

    def _is_utbreakout_live_runtime(self, cfg):
        if isinstance(cfg, dict):
            if _parse_bool_like(cfg.get('live_trading'), False):
                return True
            if is_utbreakout_live_mode_value(cfg.get('mode')) or is_utbreakout_live_mode_value(cfg.get('exchange_mode')):
                return True
        ctrl = getattr(self, 'ctrl', None)
        if ctrl is not None and hasattr(ctrl, 'get_exchange_mode'):
            try:
                return is_utbreakout_live_mode_value(ctrl.get_exchange_mode())
            except Exception:
                return False
        return False

    async def _resolve_utbreakout_selected_set(self, symbol, df, cfg):
        analysis = None
        auto_reason = None
        selected_id = normalize_utbreakout_set_id(cfg.get('active_set_id'), UTBREAKOUT_DEFAULT_SET_ID)
        if bool(cfg.get('auto_select_enabled', False)) or str(cfg.get('selection_mode', '')).lower() == 'auto':
            try:
                analysis = cfg.get('_auto_analysis_override') if isinstance(cfg.get('_auto_analysis_override'), dict) else None
                if analysis is None:
                    analysis = await self._build_utbreakout_auto_analysis(symbol, df, cfg)
                previous_set_id = None
                last_selected = getattr(self, 'utbreakout_last_selected_set_ids', {})
                if isinstance(last_selected, dict):
                    previous_set_id = last_selected.get(symbol)
                selected_id, auto_reason = self._select_utbreakout_auto_set(
                    analysis,
                    cfg,
                    previous_set_id=previous_set_id,
                )
            except Exception as exc:
                auto_reason = f"AUTO 분석 실패: {exc}. 수동 Set{selected_id} 유지"
        validated_id, safety_reason = validate_utbreakout_runtime_set_id(
            cfg,
            selected_id,
            is_live=self._is_utbreakout_live_runtime(cfg)
        )
        if validated_id != selected_id:
            selected_id = validated_id
            auto_reason = f"{auto_reason} | {safety_reason}" if auto_reason else safety_reason
            if isinstance(analysis, dict):
                scores = analysis.setdefault('scores', {})
                if isinstance(scores, dict):
                    scores['auto_safety_fallback_set_id'] = int(validated_id)
                    scores['auto_safety_reason'] = safety_reason
        selected_info = dict(self._get_utbreakout_set_info(cfg, selected_id))
        scores = analysis.get('scores') if isinstance(analysis, dict) else None
        if isinstance(scores, dict):
            selected_info['_stable_selection_detail'] = scores.get('auto_stable_selection_detail')
        return selected_info, analysis, auto_reason

    def _evaluate_utbreakout_set_filter_items(self, side, set_info, cfg, values):
        side = str(side or '').lower()
        filters = list((set_info or {}).get('entry_filters') or [])
        items = []

        def _fmt(value, digits=2):
            try:
                if value is None or not np.isfinite(float(value)):
                    return "n/a"
                return f"{float(value):.{digits}f}"
            except (TypeError, ValueError):
                return "n/a"

        def _add(name, state, detail, code):
            items.append({
                'name': name,
                'state': state,
                'detail': detail,
                'code': code,
            })

        def _mtf_bias(tf):
            metrics = values.get('mtf_metrics') or {}
            item = metrics.get(tf) if isinstance(metrics, dict) else None
            if not isinstance(item, dict) or not item.get('ready'):
                return None
            ema_bias = str(item.get('ema_bias') or 'neutral').lower()
            rsi = item.get('rsi')
            if ema_bias in {'long', 'short'}:
                return ema_bias
            if self._is_valid_number(rsi):
                return 'long' if float(rsi) > 52.0 else 'short' if float(rsi) < 48.0 else 'neutral'
            return 'neutral'

        def _mtf_pair_state(tf_a, tf_b):
            bias_a = _mtf_bias(tf_a)
            bias_b = _mtf_bias(tf_b)
            if bias_a is None or bias_b is None:
                return None, f"{tf_a}/{tf_b} 계산 대기"
            ok = bias_a == side and bias_b == side
            return ok, f"{tf_a}={str(bias_a).upper()} / {tf_b}={str(bias_b).upper()}"

        for filter_name in filters:
            if filter_name == 'atr_guard':
                atr_pct = values.get('atr_pct')
                atr_min = float(cfg.get('atr_min_percent', 0.12) or 0.12)
                atr_max = float(cfg.get('atr_max_percent', 1.20) or 1.20)
                if not self._is_valid_number(atr_pct):
                    _add('ATR% 변동성', None, 'ATR 계산 대기', 'REJECTED_ATR_TOO_LOW')
                elif float(atr_pct) < atr_min:
                    _add('ATR% 변동성', False, f"ATR% {_fmt(atr_pct, 3)} < {atr_min:.2f}", 'REJECTED_ATR_TOO_LOW')
                elif float(atr_pct) > atr_max:
                    _add('ATR% 변동성', False, f"ATR% {_fmt(atr_pct, 3)} > {atr_max:.2f}", 'REJECTED_ATR_TOO_HIGH')
                else:
                    _add('ATR% 변동성', True, f"ATR% {_fmt(atr_pct, 3)} in {atr_min:.2f}~{atr_max:.2f}", 'ACCEPTED_ENTRY')
            elif filter_name == 'htf_trend':
                if not values.get('htf_ready'):
                    _add('HTF EMA 추세', None, values.get('htf_error') or 'HTF 계산 대기', 'REJECTED_HTF_TREND')
                else:
                    htf_close = values.get('htf_close')
                    htf_fast = values.get('htf_ema_fast')
                    htf_slow = values.get('htf_ema_slow')
                    if side == 'long':
                        ok = htf_close > htf_slow and htf_fast > htf_slow
                        cond = 'close>EMA200, EMA50>EMA200'
                    else:
                        ok = htf_close < htf_slow and htf_fast < htf_slow
                        cond = 'close<EMA200, EMA50<EMA200'
                    _add(
                        'HTF EMA 추세',
                        ok,
                        f"{cond} | close {_fmt(htf_close, 4)}, EMA50 {_fmt(htf_fast, 4)}, EMA200 {_fmt(htf_slow, 4)}",
                        'REJECTED_HTF_TREND'
                    )
            elif filter_name == 'ema_slope':
                ema_fast = values.get('ema50')
                ema_fast_prev = values.get('ema50_prev')
                close_value = values.get('entry_price')
                if not (self._is_valid_number(ema_fast) and self._is_valid_number(ema_fast_prev) and self._is_valid_number(close_value)):
                    _add('15M EMA 기울기', None, 'EMA 계산 대기', 'REJECTED_EMA_CHOP_ZONE')
                elif side == 'long':
                    ok = float(close_value) > float(ema_fast) and float(ema_fast) > float(ema_fast_prev)
                    _add('15M EMA 기울기', ok, f"close {_fmt(close_value, 4)} > EMA50 {_fmt(ema_fast, 4)}, EMA50 상승", 'REJECTED_EMA_CHOP_ZONE')
                else:
                    ok = float(close_value) < float(ema_fast) and float(ema_fast) < float(ema_fast_prev)
                    _add('15M EMA 기울기', ok, f"close {_fmt(close_value, 4)} < EMA50 {_fmt(ema_fast, 4)}, EMA50 하락", 'REJECTED_EMA_CHOP_ZONE')
            elif filter_name == 'htf_supertrend':
                direction = values.get('htf_supertrend_direction')
                reason = values.get('htf_supertrend_reason') or 'HTF Supertrend 계산 대기'
                state = (direction == side) if direction in {'long', 'short'} else None
                _add('HTF Supertrend', state, reason, 'REJECTED_HTF_TREND')
            elif filter_name == 'adx_loose':
                adx_value = values.get('adx')
                threshold = float(cfg.get('adx_threshold', 20.0) or 20.0)
                state = float(adx_value) >= threshold if self._is_valid_number(adx_value) else None
                _add('ADX 추세강도', state, f"ADX {_fmt(adx_value, 2)} / 조건 >= {threshold:.2f}", 'REJECTED_ADX_LOW')
            elif filter_name == 'adx_dmi':
                adx_value = values.get('adx')
                plus_di = values.get('plus_di')
                minus_di = values.get('minus_di')
                threshold = float(cfg.get('adx_threshold', 22.0) or 22.0)
                if not (self._is_valid_number(adx_value) and self._is_valid_number(plus_di) and self._is_valid_number(minus_di)):
                    _add('ADX+DMI', None, values.get('adx_reason') or 'ADX/DMI 계산 대기', 'REJECTED_ADX_LOW')
                else:
                    dmi_ok = float(plus_di) > float(minus_di) if side == 'long' else float(minus_di) > float(plus_di)
                    ok = float(adx_value) >= threshold and dmi_ok
                    _add('ADX+DMI', ok, f"ADX {_fmt(adx_value, 2)} >= {threshold:.2f}, +DI {_fmt(plus_di, 2)}, -DI {_fmt(minus_di, 2)}", 'REJECTED_ADX_LOW')
            elif filter_name == 'chop_trend':
                chop_value = values.get('chop')
                threshold = 55.0
                state = float(chop_value) <= threshold if self._is_valid_number(chop_value) else None
                _add('CHOP 추세성', state, f"CHOP {_fmt(chop_value, 2)} / 조건 <= {threshold:.2f}", 'REJECTED_DONCHIAN_WIDTH_TOO_NARROW')
            elif filter_name == 'vortex':
                vi_plus = values.get('vortex_plus')
                vi_minus = values.get('vortex_minus')
                if not (self._is_valid_number(vi_plus) and self._is_valid_number(vi_minus)):
                    _add('Vortex 방향', None, values.get('vortex_reason') or 'Vortex 계산 대기', 'REJECTED_RSI_MOMENTUM')
                else:
                    ok = float(vi_plus) > float(vi_minus) if side == 'long' else float(vi_minus) > float(vi_plus)
                    _add('Vortex 방향', ok, f"VI+ {_fmt(vi_plus, 3)} / VI- {_fmt(vi_minus, 3)}", 'REJECTED_RSI_MOMENTUM')
            elif filter_name == 'aroon':
                aroon_up = values.get('aroon_up')
                aroon_down = values.get('aroon_down')
                if not (self._is_valid_number(aroon_up) and self._is_valid_number(aroon_down)):
                    _add('Aroon 방향', None, values.get('aroon_reason') or 'Aroon 계산 대기', 'REJECTED_RSI_MOMENTUM')
                else:
                    ok = (
                        float(aroon_up) > float(aroon_down) and float(aroon_up) >= 50.0
                        if side == 'long'
                        else float(aroon_down) > float(aroon_up) and float(aroon_down) >= 50.0
                    )
                    _add('Aroon 방향', ok, f"Up {_fmt(aroon_up, 2)} / Down {_fmt(aroon_down, 2)}", 'REJECTED_RSI_MOMENTUM')
            elif filter_name == 'rsi_momentum':
                rsi_value = values.get('rsi')
                threshold = float(cfg.get('rsi_threshold', 50.0) or 50.0)
                if not self._is_valid_number(rsi_value):
                    _add('RSI50 모멘텀', None, 'RSI 계산 대기', 'REJECTED_RSI_MOMENTUM')
                else:
                    ok = float(rsi_value) > threshold if side == 'long' else float(rsi_value) < threshold
                    _add('RSI50 모멘텀', ok, f"RSI {_fmt(rsi_value, 2)} / 기준 {threshold:.1f}", 'REJECTED_RSI_MOMENTUM')
            elif filter_name == 'macd_histogram':
                macd_hist = values.get('macd_hist')
                macd_prev = values.get('macd_hist_prev')
                if not self._is_valid_number(macd_hist):
                    _add('MACD Histogram', None, 'MACD 계산 대기', 'REJECTED_RSI_MOMENTUM')
                else:
                    ok = float(macd_hist) > 0 if side == 'long' else float(macd_hist) < 0
                    if self._is_valid_number(macd_prev):
                        ok = ok and (float(macd_hist) >= float(macd_prev) if side == 'long' else float(macd_hist) <= float(macd_prev))
                    _add('MACD Histogram', ok, f"hist {_fmt(macd_hist, 6)} / prev {_fmt(macd_prev, 6)}", 'REJECTED_RSI_MOMENTUM')
            elif filter_name == 'roc_momentum':
                roc_pct = values.get('roc_pct')
                if not self._is_valid_number(roc_pct):
                    _add('ROC 모멘텀', None, 'ROC 계산 대기', 'REJECTED_RSI_MOMENTUM')
                else:
                    ok = float(roc_pct) > 0 if side == 'long' else float(roc_pct) < 0
                    _add('ROC 모멘텀', ok, f"ROC10 {_fmt(roc_pct, 3)}%", 'REJECTED_RSI_MOMENTUM')
            elif filter_name == 'cci_direction':
                cci_value = values.get('cci')
                if not self._is_valid_number(cci_value):
                    _add('CCI 방향', None, 'CCI 계산 대기', 'REJECTED_RSI_MOMENTUM')
                else:
                    ok = float(cci_value) > 0 if side == 'long' else float(cci_value) < 0
                    _add('CCI 방향', ok, f"CCI {_fmt(cci_value, 2)}", 'REJECTED_RSI_MOMENTUM')
            elif filter_name == 'stoch_direction':
                stoch_k = values.get('stoch_k')
                stoch_d = values.get('stoch_d')
                if not (self._is_valid_number(stoch_k) and self._is_valid_number(stoch_d)):
                    _add('Stochastic 방향', None, 'Stochastic 계산 대기', 'REJECTED_RSI_MOMENTUM')
                else:
                    ok = float(stoch_k) > float(stoch_d) if side == 'long' else float(stoch_k) < float(stoch_d)
                    _add('Stochastic 방향', ok, f"K {_fmt(stoch_k, 2)} / D {_fmt(stoch_d, 2)}", 'REJECTED_RSI_MOMENTUM')
            elif filter_name == 'atr_low_vol_caution':
                atr_pct = values.get('atr_pct')
                threshold = max(float(cfg.get('atr_min_percent', 0.12) or 0.12) * 1.5, 0.18)
                if not self._is_valid_number(atr_pct):
                    _add('ATR 저변동 회피', None, 'ATR 계산 대기', 'REJECTED_ATR_TOO_LOW')
                else:
                    ok = float(atr_pct) >= threshold
                    _add('ATR 저변동 회피', ok, f"ATR% {_fmt(atr_pct, 3)} / 조건 >= {threshold:.3f}", 'REJECTED_ATR_TOO_LOW')
            elif filter_name == 'atr_high_vol_reduce':
                atr_pct = values.get('atr_pct')
                threshold = min(float(cfg.get('atr_max_percent', 1.20) or 1.20), 1.00)
                if not self._is_valid_number(atr_pct):
                    _add('ATR 고변동 회피', None, 'ATR 계산 대기', 'REJECTED_ATR_TOO_HIGH')
                else:
                    ok = float(atr_pct) <= threshold
                    _add('ATR 고변동 회피', ok, f"ATR% {_fmt(atr_pct, 3)} / 조건 <= {threshold:.3f}", 'REJECTED_ATR_TOO_HIGH')
            elif filter_name == 'bb_width_expansion':
                width = values.get('bb_width_pct')
                prev_width = values.get('bb_width_prev_pct')
                min_width = values.get('bb_width_min_pct')
                if not (self._is_valid_number(width) and self._is_valid_number(prev_width)):
                    _add('BB Width 확장', None, 'BB Width 계산 대기', 'REJECTED_DONCHIAN_WIDTH_TOO_NARROW')
                else:
                    ok = float(width) > float(prev_width)
                    if self._is_valid_number(min_width):
                        ok = ok and float(width) >= float(min_width) * 1.05
                    _add('BB Width 확장', ok, f"width {_fmt(width, 3)}% / prev {_fmt(prev_width, 3)}%", 'REJECTED_DONCHIAN_WIDTH_TOO_NARROW')
            elif filter_name == 'keltner_expansion':
                width = values.get('keltner_width_pct')
                prev_width = values.get('keltner_width_prev_pct')
                if not (self._is_valid_number(width) and self._is_valid_number(prev_width)):
                    _add('Keltner 확장', None, 'Keltner 계산 대기', 'REJECTED_ATR_TOO_LOW')
                else:
                    ok = float(width) > float(prev_width)
                    _add('Keltner 확장', ok, f"width {_fmt(width, 3)}% / prev {_fmt(prev_width, 3)}%", 'REJECTED_ATR_TOO_LOW')
            elif filter_name == 'donchian_breakout':
                close_value = values.get('entry_price')
                high_prev = values.get('donchian_high_prev')
                low_prev = values.get('donchian_low_prev')
                if not (self._is_valid_number(close_value) and self._is_valid_number(high_prev) and self._is_valid_number(low_prev)):
                    _add('Donchian 돌파', None, 'Donchian 계산 대기', 'REJECTED_DONCHIAN_NO_BREAKOUT')
                elif side == 'long':
                    ok = float(close_value) > float(high_prev)
                    _add('Donchian 돌파', ok, f"close {_fmt(close_value, 4)} > prev high {_fmt(high_prev, 4)}", 'REJECTED_DONCHIAN_NO_BREAKOUT')
                else:
                    ok = float(close_value) < float(low_prev)
                    _add('Donchian 돌파', ok, f"close {_fmt(close_value, 4)} < prev low {_fmt(low_prev, 4)}", 'REJECTED_DONCHIAN_NO_BREAKOUT')
            elif filter_name == 'bb_band_breakout':
                close_value = values.get('entry_price')
                upper = values.get('bb_upper')
                lower = values.get('bb_lower')
                if not (self._is_valid_number(close_value) and self._is_valid_number(upper) and self._is_valid_number(lower)):
                    _add('BB 밴드 돌파', None, 'Bollinger Band 계산 대기', 'REJECTED_DONCHIAN_NO_BREAKOUT')
                elif side == 'long':
                    ok = float(close_value) > float(upper)
                    _add('BB 밴드 돌파', ok, f"close {_fmt(close_value, 4)} > upper {_fmt(upper, 4)}", 'REJECTED_DONCHIAN_NO_BREAKOUT')
                else:
                    ok = float(close_value) < float(lower)
                    _add('BB 밴드 돌파', ok, f"close {_fmt(close_value, 4)} < lower {_fmt(lower, 4)}", 'REJECTED_DONCHIAN_NO_BREAKOUT')
            elif filter_name == 'keltner_breakout':
                close_value = values.get('entry_price')
                upper = values.get('keltner_upper')
                lower = values.get('keltner_lower')
                if not (self._is_valid_number(close_value) and self._is_valid_number(upper) and self._is_valid_number(lower)):
                    _add('Keltner 돌파', None, 'Keltner 계산 대기', 'REJECTED_DONCHIAN_NO_BREAKOUT')
                elif side == 'long':
                    ok = float(close_value) > float(upper)
                    _add('Keltner 돌파', ok, f"close {_fmt(close_value, 4)} > upper {_fmt(upper, 4)}", 'REJECTED_DONCHIAN_NO_BREAKOUT')
                else:
                    ok = float(close_value) < float(lower)
                    _add('Keltner 돌파', ok, f"close {_fmt(close_value, 4)} < lower {_fmt(lower, 4)}", 'REJECTED_DONCHIAN_NO_BREAKOUT')
            elif filter_name == 'range_expansion':
                ratio = values.get('range_expansion_ratio')
                open_value = values.get('open')
                close_value = values.get('entry_price')
                if not (self._is_valid_number(ratio) and self._is_valid_number(open_value) and self._is_valid_number(close_value)):
                    _add('Range 확장봉', None, 'Range 계산 대기', 'REJECTED_DONCHIAN_NO_BREAKOUT')
                else:
                    candle_ok = float(close_value) > float(open_value) if side == 'long' else float(close_value) < float(open_value)
                    ok = float(ratio) >= 1.25 and candle_ok
                    _add('Range 확장봉', ok, f"range/avg {_fmt(ratio, 2)} / 방향봉 {'OK' if candle_ok else 'NO'}", 'REJECTED_DONCHIAN_NO_BREAKOUT')
            elif filter_name == 'vwap_pullback':
                close_value = values.get('entry_price')
                vwap = values.get('vwap')
                if not (self._is_valid_number(close_value) and self._is_valid_number(vwap)):
                    _add('VWAP 눌림 지속', None, values.get('vwap_reason') or 'VWAP 계산 대기', 'REJECTED_RSI_MOMENTUM')
                else:
                    dist_pct = abs(float(close_value) - float(vwap)) / max(abs(float(close_value)), 1e-9) * 100.0
                    side_ok = float(close_value) >= float(vwap) if side == 'long' else float(close_value) <= float(vwap)
                    ok = side_ok and dist_pct <= 0.60
                    _add('VWAP 눌림 지속', ok, f"dist {_fmt(dist_pct, 3)}% / close {_fmt(close_value, 4)} / VWAP {_fmt(vwap, 4)}", 'REJECTED_RSI_MOMENTUM')
            elif filter_name == 'ema_pullback':
                close_value = values.get('entry_price')
                ema_fast = values.get('ema50')
                if not (self._is_valid_number(close_value) and self._is_valid_number(ema_fast)):
                    _add('EMA 눌림 지속', None, 'EMA 계산 대기', 'REJECTED_EMA_CHOP_ZONE')
                else:
                    dist_pct = abs(float(close_value) - float(ema_fast)) / max(abs(float(close_value)), 1e-9) * 100.0
                    side_ok = float(close_value) >= float(ema_fast) if side == 'long' else float(close_value) <= float(ema_fast)
                    ok = side_ok and dist_pct <= 0.80
                    _add('EMA 눌림 지속', ok, f"dist {_fmt(dist_pct, 3)}% / EMA50 {_fmt(ema_fast, 4)}", 'REJECTED_EMA_CHOP_ZONE')
            elif filter_name == 'bb_midline_reclaim':
                close_value = values.get('entry_price')
                bb_mid = values.get('bb_mid')
                if not (self._is_valid_number(close_value) and self._is_valid_number(bb_mid)):
                    _add('BB 중심선 회복', None, 'BB 중심선 계산 대기', 'REJECTED_RSI_MOMENTUM')
                else:
                    ok = float(close_value) > float(bb_mid) if side == 'long' else float(close_value) < float(bb_mid)
                    _add('BB 중심선 회복', ok, f"close {_fmt(close_value, 4)} / mid {_fmt(bb_mid, 4)}", 'REJECTED_RSI_MOMENTUM')
            elif filter_name == 'rsi_pullback':
                rsi_value = values.get('rsi')
                if not self._is_valid_number(rsi_value):
                    _add('RSI 눌림 재개', None, 'RSI 계산 대기', 'REJECTED_RSI_MOMENTUM')
                elif side == 'long':
                    ok = 50.0 <= float(rsi_value) <= 70.0
                    _add('RSI 눌림 재개', ok, f"RSI {_fmt(rsi_value, 2)} / 조건 50~70", 'REJECTED_RSI_MOMENTUM')
                else:
                    ok = 30.0 <= float(rsi_value) <= 50.0
                    _add('RSI 눌림 재개', ok, f"RSI {_fmt(rsi_value, 2)} / 조건 30~50", 'REJECTED_RSI_MOMENTUM')
            elif filter_name == 'atr_trail_continuation':
                close_value = values.get('entry_price')
                ema_fast = values.get('ema50')
                atr_pct = values.get('atr_pct')
                if not (self._is_valid_number(close_value) and self._is_valid_number(ema_fast) and self._is_valid_number(atr_pct)):
                    _add('ATR 지속 추세', None, 'ATR/EMA 계산 대기', 'REJECTED_ATR_TOO_LOW')
                else:
                    atr_ok = float(cfg.get('atr_min_percent', 0.12) or 0.12) <= float(atr_pct) <= float(cfg.get('atr_max_percent', 1.20) or 1.20)
                    side_ok = float(close_value) > float(ema_fast) if side == 'long' else float(close_value) < float(ema_fast)
                    ok = atr_ok and side_ok
                    _add('ATR 지속 추세', ok, f"ATR% {_fmt(atr_pct, 3)} / EMA50 {_fmt(ema_fast, 4)}", 'REJECTED_ATR_TOO_LOW')
            elif filter_name == 'volume_spike':
                volume_ratio = values.get('volume_ratio')
                open_value = values.get('open')
                close_value = values.get('entry_price')
                if not (self._is_valid_number(volume_ratio) and self._is_valid_number(open_value) and self._is_valid_number(close_value)):
                    _add('거래량 급증', None, '거래량 계산 대기', 'REJECTED_RSI_MOMENTUM')
                else:
                    candle_ok = float(close_value) > float(open_value) if side == 'long' else float(close_value) < float(open_value)
                    ok = float(volume_ratio) >= 1.80 and candle_ok
                    _add('거래량 급증', ok, f"relVol {_fmt(volume_ratio, 2)} / 방향봉 {'OK' if candle_ok else 'NO'}", 'REJECTED_RSI_MOMENTUM')
            elif filter_name == 'relative_volume':
                volume_ratio = values.get('volume_ratio')
                open_value = values.get('open')
                close_value = values.get('entry_price')
                ema_fast = values.get('ema50')
                taker_ratio = values.get('taker_buy_sell_ratio')
                spread = values.get('futures_spread_pct')
                rolling_samples = int(values.get('rolling_ofi_samples') or 0)

                min_volume = float(cfg.get('set32_min_relative_volume', 1.50) or 1.50)
                require_candle = bool(cfg.get('set32_require_direction_candle', True))
                require_ema = bool(cfg.get('set32_require_ema50_side', True))
                require_flow = bool(cfg.get('set32_require_orderflow_confirmation', True))
                min_samples = int(cfg.get('set32_orderflow_min_samples', 3) or 3)
                long_taker_min = float(cfg.get('set32_min_taker_ratio_long', 1.00) or 1.00)
                short_taker_max = float(cfg.get('set32_max_taker_ratio_short', 1.00) or 1.00)
                spread_max = float(cfg.get('set32_max_spread_pct', 0.05) or 0.05)

                if not self._is_valid_number(volume_ratio):
                    _add('상대 거래량', None, '상대 거래량 계산 대기', 'REJECTED_RELATIVE_VOLUME_CORE')
                else:
                    relvol_ok = float(volume_ratio) >= min_volume

                    candle_ok = True
                    if require_candle:
                        candle_ok = (
                            self._is_valid_number(open_value)
                            and self._is_valid_number(close_value)
                            and (
                                float(close_value) > float(open_value)
                                if side == 'long'
                                else float(close_value) < float(open_value)
                            )
                        )

                    ema_ok = True
                    if require_ema:
                        ema_ok = (
                            self._is_valid_number(close_value)
                            and self._is_valid_number(ema_fast)
                            and (
                                float(close_value) >= float(ema_fast)
                                if side == 'long'
                                else float(close_value) <= float(ema_fast)
                            )
                        )

                    flow_ok = True
                    flow_detail = 'flow OFF'
                    if require_flow:
                        spread_ok = (not self._is_valid_number(spread)) or float(spread) <= spread_max
                        if side == 'long':
                            taker_ok = self._is_valid_number(taker_ratio) and float(taker_ratio) >= long_taker_min
                            flow_cond = f"taker>={long_taker_min:.2f}"
                        else:
                            taker_ok = self._is_valid_number(taker_ratio) and float(taker_ratio) <= short_taker_max
                            flow_cond = f"taker<={short_taker_max:.2f}"

                        sample_ok = rolling_samples >= min_samples
                        flow_ok = bool(sample_ok and taker_ok and spread_ok)
                        flow_detail = (
                            f"samples {rolling_samples}/{min_samples}, "
                            f"{flow_cond}, taker {_fmt(taker_ratio, 3)}, "
                            f"spread {_fmt(spread, 4)}%<= {spread_max:.3f}%"
                        )

                    ok = bool(relvol_ok and candle_ok and ema_ok and flow_ok)
                    _add(
                        '상대 거래량',
                        ok,
                        (
                            f"relVol {_fmt(volume_ratio, 2)} / 조건 >= {min_volume:.2f}; "
                            f"방향봉 {'OK' if candle_ok else 'NO'}; "
                            f"EMA50 {'OK' if ema_ok else 'NO'}; "
                            f"{flow_detail}"
                        ),
                        'REJECTED_RELATIVE_VOLUME_CORE'
                    )
            elif filter_name == 'obv_slope':
                obv_slope_ratio = values.get('obv_slope_ratio')
                if not self._is_valid_number(obv_slope_ratio):
                    _add('OBV 기울기', None, 'OBV 계산 대기', 'REJECTED_RSI_MOMENTUM')
                else:
                    ok = float(obv_slope_ratio) > 0 if side == 'long' else float(obv_slope_ratio) < 0
                    _add('OBV 기울기', ok, f"OBV slope/vol {_fmt(obv_slope_ratio, 3)}", 'REJECTED_RSI_MOMENTUM')
            elif filter_name == 'mfi_flow':
                mfi_value = values.get('mfi')
                if not self._is_valid_number(mfi_value):
                    _add('MFI 자금흐름', None, 'MFI 계산 대기', 'REJECTED_RSI_MOMENTUM')
                else:
                    ok = float(mfi_value) > 50.0 if side == 'long' else float(mfi_value) < 50.0
                    _add('MFI 자금흐름', ok, f"MFI {_fmt(mfi_value, 2)} / 기준 50", 'REJECTED_RSI_MOMENTUM')
            elif filter_name == 'vwap_slope':
                vwap_slope = values.get('vwap_slope')
                if not self._is_valid_number(vwap_slope):
                    _add('VWAP 기울기', None, values.get('vwap_reason') or 'VWAP 계산 대기', 'REJECTED_RSI_MOMENTUM')
                else:
                    ok = float(vwap_slope) > 0 if side == 'long' else float(vwap_slope) < 0
                    _add('VWAP 기울기', ok, f"slope {_fmt(vwap_slope, 6)}", 'REJECTED_RSI_MOMENTUM')
            elif filter_name == 'chop_avoid':
                chop_value = values.get('chop')
                threshold = 61.8
                if not self._is_valid_number(chop_value):
                    _add('CHOP 횡보회피', None, values.get('chop_reason') or 'CHOP 계산 대기', 'REJECTED_DONCHIAN_WIDTH_TOO_NARROW')
                else:
                    ok = float(chop_value) <= threshold
                    _add('CHOP 횡보회피', ok, f"CHOP {_fmt(chop_value, 2)} / 조건 <= {threshold:.1f}", 'REJECTED_DONCHIAN_WIDTH_TOO_NARROW')
            elif filter_name == 'donchian_width':
                width = values.get('donchian_width_pct')
                threshold = float(cfg.get('donchian_width_min_percent', 0.50) or 0.50)
                if not self._is_valid_number(width):
                    _add('Donchian Width', None, 'Donchian Width 계산 대기', 'REJECTED_DONCHIAN_WIDTH_TOO_NARROW')
                else:
                    ok = float(width) >= threshold
                    _add('Donchian Width', ok, f"width {_fmt(width, 3)}% / 조건 >= {threshold:.2f}%", 'REJECTED_DONCHIAN_WIDTH_TOO_NARROW')
            elif filter_name == 'htf_ema_gap':
                gap = values.get('htf_gap_pct')
                threshold = float(cfg.get('htf_ema_gap_min_percent', 0.15) or 0.15)
                if not self._is_valid_number(gap):
                    _add('HTF EMA Gap', None, values.get('htf_error') or 'HTF EMA gap 계산 대기', 'REJECTED_HTF_TREND')
                else:
                    ok = float(gap) >= threshold
                    _add('HTF EMA Gap', ok, f"gap {_fmt(gap, 3)}% / 조건 >= {threshold:.3f}%", 'REJECTED_HTF_TREND')
            elif filter_name == 'bb_squeeze_avoid':
                width = values.get('bb_width_pct')
                min_width = values.get('bb_width_min_pct')
                if not (self._is_valid_number(width) and self._is_valid_number(min_width)):
                    _add('BB Squeeze 회피', None, 'BB squeeze 계산 대기', 'REJECTED_DONCHIAN_WIDTH_TOO_NARROW')
                else:
                    ok = float(width) >= float(min_width) * 1.08
                    _add('BB Squeeze 회피', ok, f"width {_fmt(width, 3)}% / min {_fmt(min_width, 3)}%", 'REJECTED_DONCHIAN_WIDTH_TOO_NARROW')
            elif filter_name == 'range_compression_avoid':
                ratio = values.get('range_compression_ratio')
                if not self._is_valid_number(ratio):
                    _add('Range 압축회피', None, 'Range compression 계산 대기', 'REJECTED_DONCHIAN_WIDTH_TOO_NARROW')
                else:
                    ok = float(ratio) >= 0.70
                    _add('Range 압축회피', ok, f"range20/range50 {_fmt(ratio, 2)} / 조건 >= 0.70", 'REJECTED_DONCHIAN_WIDTH_TOO_NARROW')
            elif filter_name == 'mtf_15_30':
                ok, detail = _mtf_pair_state('15m', '30m')
                _add('MTF 15m/30m 정렬', ok, detail, 'REJECTED_HTF_TREND')
            elif filter_name == 'mtf_30_1h':
                ok, detail = _mtf_pair_state('30m', '1h')
                _add('MTF 30m/1h 정렬', ok, detail, 'REJECTED_HTF_TREND')
            elif filter_name == 'mtf_1h_4h':
                ok, detail = _mtf_pair_state('1h', '4h')
                _add('MTF 1h/4h 정렬', ok, detail, 'REJECTED_HTF_TREND')
            elif filter_name == 'mtf_momentum_score':
                scores = values.get('auto_scores') if isinstance(values.get('auto_scores'), dict) else {}
                momentum_score = scores.get('momentum_score')
                dominant_side = str(scores.get('dominant_side') or 'neutral').lower()
                if not self._is_valid_number(momentum_score):
                    _add('MTF 모멘텀 점수', None, 'MTF 모멘텀 계산 대기', 'REJECTED_RSI_MOMENTUM')
                else:
                    ok = float(momentum_score) >= 55.0 and dominant_side in {side, 'neutral'}
                    _add('MTF 모멘텀 점수', ok, f"momentum {float(momentum_score):.1f} / dominant {dominant_side.upper()}", 'REJECTED_RSI_MOMENTUM')
            elif filter_name == 'mtf_volatility_score':
                scores = values.get('auto_scores') if isinstance(values.get('auto_scores'), dict) else {}
                volatility_score = scores.get('volatility_score')
                if not self._is_valid_number(volatility_score):
                    _add('MTF 변동성 점수', None, 'MTF 변동성 계산 대기', 'REJECTED_ATR_TOO_LOW')
                else:
                    ok = float(volatility_score) >= 55.0
                    _add('MTF 변동성 점수', ok, f"volatility {float(volatility_score):.1f} / 조건 >= 55", 'REJECTED_ATR_TOO_LOW')
            elif filter_name == 'psar_direction':
                psar_direction = str(values.get('psar_direction') or '').lower()
                psar = values.get('psar')
                if psar_direction not in {'long', 'short'}:
                    _add('Parabolic SAR 방향', None, values.get('psar_reason') or 'PSAR 계산 대기', 'REJECTED_RSI_MOMENTUM')
                else:
                    ok = psar_direction == side
                    _add('Parabolic SAR 방향', ok, f"{psar_direction.upper()} @ {_fmt(psar, 4)}", 'REJECTED_RSI_MOMENTUM')
            elif filter_name == 'ichimoku_cloud':
                bias = str(values.get('ichimoku_bias') or 'neutral').lower()
                top = values.get('ichimoku_top')
                bottom = values.get('ichimoku_bottom')
                if bias not in {'long', 'short', 'neutral'} or not (self._is_valid_number(top) and self._is_valid_number(bottom)):
                    _add('Ichimoku Cloud', None, 'Ichimoku 계산 대기', 'REJECTED_HTF_TREND')
                else:
                    ok = bias == side
                    _add('Ichimoku Cloud', ok, f"bias {bias.upper()} / cloud {_fmt(bottom, 4)}~{_fmt(top, 4)}", 'REJECTED_HTF_TREND')
            elif filter_name == 'session_volatility':
                hour = values.get('session_hour_kst')
                atr_pct = values.get('atr_pct')
                volume_ratio = values.get('volume_ratio')
                if hour is None or not self._is_valid_number(atr_pct):
                    _add('세션 변동성', None, '세션/ATR 계산 대기', 'REJECTED_ATR_TOO_LOW')
                else:
                    active_session = int(hour) in {0, 1, 2, 8, 9, 10, 16, 17, 18, 19, 20, 21, 22, 23}
                    volume_ok = self._is_valid_number(volume_ratio) and float(volume_ratio) >= 0.9
                    ok = active_session and (float(atr_pct) >= float(cfg.get('atr_min_percent', 0.12) or 0.12) or volume_ok)
                    _add('세션 변동성', ok, f"KST {int(hour):02d}h / ATR% {_fmt(atr_pct, 3)} / relVol {_fmt(volume_ratio, 2)}", 'REJECTED_ATR_TOO_LOW')
            elif filter_name == 'regime_fallback':
                scores = values.get('auto_scores') if isinstance(values.get('auto_scores'), dict) else {}
                core = [
                    float(scores.get(key, 0.0) or 0.0)
                    for key in ('trend_score', 'breakout_score', 'momentum_score', 'flow_score', 'alignment_score')
                ]
                max_core = max(core) if core else 0.0
                ok = max_core < 65.0
                _add('Regime fallback', ok, f"max core score {max_core:.1f} / 조건 < 65", 'REJECTED_RISK_REWARD_LOW')
            elif filter_name == 'rolling_orderflow_imbalance':
                imbalance = values.get('rolling_orderbook_imbalance_pct')
                delta = values.get('rolling_orderbook_imbalance_delta')
                ofi_score = values.get('rolling_ofi_score')
                samples = int(float(values.get('rolling_ofi_samples', 0) or 0))
                taker_ratio = values.get('taker_buy_sell_ratio')
                spread = values.get('futures_spread_pct')
                min_abs = float(cfg.get('rolling_ofi_min_abs', 3.0) or 3.0)
                long_min = float(cfg.get('rolling_ofi_taker_long_min', 1.03) or 1.03)
                short_max = float(cfg.get('rolling_ofi_taker_short_max', 0.97) or 0.97)
                spread_max = float(cfg.get('rolling_ofi_spread_max_pct', 0.05) or 0.05)
                detail = (
                    f"imb {_fmt(imbalance, 2)}%, Δ {_fmt(delta, 2)}, OFI {_fmt(ofi_score, 2)}, "
                    f"taker {_fmt(taker_ratio, 3)}, spread {_fmt(spread, 4)}%, samples {samples}"
                )
                if samples < 3 or not (
                    self._is_valid_number(imbalance)
                    and self._is_valid_number(taker_ratio)
                    and self._is_valid_number(spread)
                ):
                    _add('Rolling OFI 확인', None, f"{detail} / rolling OFI 데이터 대기", 'REJECTED_ROLLING_OFI')
                else:
                    if side == 'long':
                        ok = float(imbalance) >= min_abs and float(taker_ratio) >= long_min and float(spread) <= spread_max
                        cond = f"long imb>={min_abs:.1f}, taker>={long_min:.2f}, spread<={spread_max:.3f}%"
                    else:
                        ok = float(imbalance) <= -min_abs and float(taker_ratio) <= short_max and float(spread) <= spread_max
                        cond = f"short imb<=-{min_abs:.1f}, taker<={short_max:.2f}, spread<={spread_max:.3f}%"
                    _add('Rolling OFI 확인', ok, f"{detail} / {cond}", 'REJECTED_ROLLING_OFI')
            elif filter_name == 'oi_funding_squeeze':
                oi_z = values.get('open_interest_delta_z')
                acceleration = values.get('open_interest_acceleration')
                funding = values.get('funding_rate')
                long_short = values.get('long_short_ratio')
                basis = values.get('basis_pct')
                oi_z_min = float(cfg.get('oi_z_min', 0.75) or 0.75)
                acc_min = float(cfg.get('oi_acceleration_min', 0.0) or 0.0)
                funding_long_max = float(cfg.get('funding_long_max', 0.0008) or 0.0008)
                funding_short_min = float(cfg.get('funding_short_min', -0.0008) or -0.0008)
                long_short_long_max = float(cfg.get('long_short_long_max', 1.85) or 1.85)
                long_short_short_min = float(cfg.get('long_short_short_min', 0.55) or 0.55)
                basis_long_max = float(cfg.get('basis_long_max_pct', 0.20) or 0.20)
                basis_short_min = float(cfg.get('basis_short_min_pct', -0.20) or -0.20)
                detail = (
                    f"OI z {_fmt(oi_z, 2)}, accel {_fmt(acceleration, 3)}, funding {_fmt(funding, 6)}, "
                    f"L/S {_fmt(long_short, 3)}, basis {_fmt(basis, 4)}%"
                )
                if not (
                    self._is_valid_number(oi_z)
                    and self._is_valid_number(acceleration)
                    and self._is_valid_number(funding)
                    and self._is_valid_number(long_short)
                ):
                    _add('OI/Funding Squeeze', None, f"{detail} / OI history/funding 데이터 대기", 'REJECTED_OI_FUNDING_SQUEEZE')
                else:
                    basis_ok = True
                    if self._is_valid_number(basis):
                        basis_ok = float(basis) <= basis_long_max if side == 'long' else float(basis) >= basis_short_min
                    if side == 'long':
                        ok = (
                            float(oi_z) >= oi_z_min
                            and float(acceleration) >= acc_min
                            and float(funding) <= funding_long_max
                            and float(long_short) <= long_short_long_max
                            and basis_ok
                        )
                        cond = f"long z>={oi_z_min:.2f}, accel>={acc_min:.2f}, funding<={funding_long_max:.4g}, L/S<={long_short_long_max:.2f}"
                    else:
                        ok = (
                            float(oi_z) >= oi_z_min
                            and float(acceleration) >= acc_min
                            and float(funding) >= funding_short_min
                            and float(long_short) >= long_short_short_min
                            and basis_ok
                        )
                        cond = f"short z>={oi_z_min:.2f}, accel>={acc_min:.2f}, funding>={funding_short_min:.4g}, L/S>={long_short_short_min:.2f}"
                    _add('OI/Funding Squeeze', ok, f"{detail} / {cond}", 'REJECTED_OI_FUNDING_SQUEEZE')
            elif filter_name == 'squeeze_release_breakout':
                bb_pct = values.get('bb_width_percentile')
                keltner_squeeze = values.get('keltner_squeeze_on')
                range_ratio = values.get('range_expansion_ratio')
                volume_ratio = values.get('volume_ratio')
                close_value = values.get('entry_price')
                high_prev = values.get('donchian_high_prev')
                low_prev = values.get('donchian_low_prev')
                pct_max = float(cfg.get('squeeze_percentile_max', 25.0) or 25.0)
                range_min = float(cfg.get('squeeze_release_range_min', 1.05) or 1.05)
                volume_min = float(cfg.get('squeeze_volume_ratio_min', 1.20) or 1.20)
                require_keltner = bool(cfg.get('squeeze_require_keltner', False))
                level = high_prev if side == 'long' else low_prev
                detail = (
                    f"BB pct {_fmt(bb_pct, 2)}, Keltner {'ON' if keltner_squeeze is True else 'OFF' if keltner_squeeze is False else 'n/a'}, "
                    f"range {_fmt(range_ratio, 2)}, vol {_fmt(volume_ratio, 2)}, level {_fmt(level, 4)}"
                )
                if not (
                    self._is_valid_number(range_ratio)
                    and self._is_valid_number(volume_ratio)
                    and self._is_valid_number(close_value)
                    and self._is_valid_number(high_prev)
                    and self._is_valid_number(low_prev)
                    and (self._is_valid_number(bb_pct) or keltner_squeeze is not None)
                ):
                    _add('Squeeze Release 돌파', None, f"{detail} / squeeze 데이터 대기", 'REJECTED_SQUEEZE_RELEASE')
                else:
                    squeeze_ok = keltner_squeeze is True if require_keltner else (
                        keltner_squeeze is True or (self._is_valid_number(bb_pct) and float(bb_pct) <= pct_max)
                    )
                    range_ok = float(range_ratio) >= range_min
                    volume_ok = float(volume_ratio) >= volume_min
                    breakout_ok = float(close_value) > float(high_prev) if side == 'long' else float(close_value) < float(low_prev)
                    ok = squeeze_ok and range_ok and volume_ok and breakout_ok
                    _add('Squeeze Release 돌파', ok, f"{detail} / squeeze {squeeze_ok} range>={range_min:.2f} vol>={volume_min:.2f} breakout {breakout_ok}", 'REJECTED_SQUEEZE_RELEASE')
            elif filter_name == 'prediction_orderflow_imbalance':
                imbalance = values.get('orderbook_imbalance_pct')
                taker_ratio = values.get('taker_buy_sell_ratio')
                spread = values.get('futures_spread_pct')
                if not (self._is_valid_number(imbalance) and self._is_valid_number(taker_ratio)):
                    _add('Futures 수급 불균형', None, 'orderbook/taker flow 데이터 대기', 'REJECTED_PREDICTION_FLOW')
                else:
                    flow_ok = (
                        float(imbalance) >= 5.0 and float(taker_ratio) >= 1.05
                        if side == 'long'
                        else float(imbalance) <= -5.0 and float(taker_ratio) <= 0.95
                    )
                    spread_ok = (not self._is_valid_number(spread)) or float(spread) <= 0.05
                    _add('Futures 수급 불균형', flow_ok and spread_ok, f"imbalance {_fmt(imbalance, 2)}%, takerRatio {_fmt(taker_ratio, 3)}, spread {_fmt(spread, 4)}%", 'REJECTED_PREDICTION_FLOW')
            elif filter_name == 'prediction_oi_funding_crowding':
                funding = values.get('funding_rate')
                long_short = values.get('long_short_ratio')
                oi_delta = values.get('open_interest_delta_pct')
                if not (self._is_valid_number(funding) or self._is_valid_number(long_short) or self._is_valid_number(oi_delta)):
                    _add('OI/Funding 과열회피', None, 'funding/OI 데이터 대기', 'REJECTED_PREDICTION_CROWDING')
                else:
                    funding_value = float(funding) if self._is_valid_number(funding) else 0.0
                    ratio_value = float(long_short) if self._is_valid_number(long_short) else 1.0
                    crowd_ok = (
                        funding_value <= 0.0008 and ratio_value <= 1.85
                        if side == 'long'
                        else funding_value >= -0.0008 and ratio_value >= 0.55
                    )
                    _add('OI/Funding 과열회피', crowd_ok, f"funding {_fmt(funding, 6)}, L/S {_fmt(long_short, 3)}, OIΔ {_fmt(oi_delta, 2)}%", 'REJECTED_PREDICTION_CROWDING')
            elif filter_name == 'prediction_liquidation_cascade':
                oi_delta = values.get('open_interest_delta_pct')
                taker_ratio = values.get('taker_buy_sell_ratio')
                range_ratio = values.get('range_expansion_ratio')
                if not (self._is_valid_number(oi_delta) and self._is_valid_number(taker_ratio) and self._is_valid_number(range_ratio)):
                    _add('청산 연쇄 Proxy', None, 'OIΔ/taker/range 데이터 대기', 'REJECTED_PREDICTION_LIQUIDATION')
                else:
                    flow_ok = float(taker_ratio) >= 1.08 if side == 'long' else float(taker_ratio) <= 0.92
                    ok = float(oi_delta) <= -0.35 and flow_ok and float(range_ratio) >= 1.10
                    _add('청산 연쇄 Proxy', ok, f"OIΔ {_fmt(oi_delta, 2)}%, takerRatio {_fmt(taker_ratio, 3)}, range {_fmt(range_ratio, 2)}", 'REJECTED_PREDICTION_LIQUIDATION')
            elif filter_name == 'prediction_odds_divergence':
                probability = values.get('prediction_up_probability')
                edge = values.get('prediction_edge')
                title = values.get('prediction_title') or 'prediction scan'
                if not self._is_valid_number(probability):
                    _add('Prediction odds 괴리', None, 'Prediction odds 데이터 대기', 'REJECTED_PREDICTION_ODDS')
                else:
                    ok = float(probability) >= 0.52 if side == 'long' else float(probability) <= 0.48
                    _add('Prediction odds 괴리', ok, f"upProb {_fmt(probability, 3)}, edge {_fmt(edge, 3)} / {title}", 'REJECTED_PREDICTION_ODDS')
            elif filter_name == 'prediction_macro_event_guard':
                funding = values.get('funding_rate')
                next_funding_time = values.get('next_funding_time')
                minutes_to_funding = None
                if next_funding_time:
                    minutes_to_funding = (int(next_funding_time) / 1000.0 - time.time()) / 60.0
                if self._is_valid_number(funding) and minutes_to_funding is not None:
                    ok = not (0.0 <= minutes_to_funding <= 20.0 and abs(float(funding)) >= 0.0007)
                    _add('Event/Funding Guard', ok, f"funding {_fmt(funding, 6)}, next {minutes_to_funding:.1f}m", 'REJECTED_PREDICTION_EVENT_GUARD')
                else:
                    _add('Event/Funding Guard', None, 'funding window 데이터 대기', 'REJECTED_PREDICTION_EVENT_GUARD')
            elif filter_name == 'prediction_volatility_forecast':
                scores = values.get('auto_scores') if isinstance(values.get('auto_scores'), dict) else {}
                vol_score = scores.get('prediction_volatility_score') or scores.get('volatility_score')
                high_vol = scores.get('high_vol_score')
                if not self._is_valid_number(vol_score):
                    _add('변동성 예측', None, 'volatility forecast 계산 대기', 'REJECTED_ATR_TOO_HIGH')
                else:
                    ok = float(vol_score) >= 48.0 and (not self._is_valid_number(high_vol) or float(high_vol) <= 85.0)
                    _add('변동성 예측', ok, f"volScore {_fmt(vol_score, 1)}, highVol {_fmt(high_vol, 1)}", 'REJECTED_ATR_TOO_HIGH')
            elif filter_name == 'prediction_spread_depth_guard':
                spread = values.get('futures_spread_pct')
                bid_depth = values.get('bid_depth_usdt')
                ask_depth = values.get('ask_depth_usdt')
                min_depth = float(cfg.get('prediction_min_depth_usdt', 50000.0) or 50000.0)
                if not (self._is_valid_number(spread) and self._is_valid_number(bid_depth) and self._is_valid_number(ask_depth)):
                    _add('Spread/Depth 비용', None, 'depth 데이터 대기', 'REJECTED_PREDICTION_LIQUIDITY')
                else:
                    min_side_depth = min(float(bid_depth), float(ask_depth))
                    ok = float(spread) <= 0.05 and min_side_depth >= min_depth
                    _add('Spread/Depth 비용', ok, f"spread {_fmt(spread, 4)}%, depth {_fmt(min_side_depth, 0)} >= {min_depth:.0f}", 'REJECTED_PREDICTION_LIQUIDITY')
            elif filter_name == 'prediction_basis_divergence':
                basis = values.get('basis_pct')
                if not self._is_valid_number(basis):
                    _add('Basis 과열회피', None, 'basis 데이터 대기', 'REJECTED_PREDICTION_BASIS')
                else:
                    ok = float(basis) <= 0.10 if side == 'long' else float(basis) >= -0.10
                    _add('Basis 과열회피', ok, f"mark-index basis {_fmt(basis, 4)}%", 'REJECTED_PREDICTION_BASIS')
            elif filter_name == 'prediction_probability_trail':
                probability = values.get('prediction_up_probability')
                if not self._is_valid_number(probability):
                    _add('Probability Trail Guard', None, 'Prediction probability 데이터 대기', 'REJECTED_PREDICTION_ODDS')
                else:
                    ok = float(probability) >= 0.55 if side == 'long' else float(probability) <= 0.45
                    _add('Probability Trail Guard', ok, f"upProb {_fmt(probability, 3)} / long>=0.55 short<=0.45", 'REJECTED_PREDICTION_ODDS')
            elif filter_name == 'prediction_cross_market_confirmation':
                probability = values.get('prediction_up_probability')
                imbalance = values.get('orderbook_imbalance_pct')
                taker_ratio = values.get('taker_buy_sell_ratio')
                basis = values.get('basis_pct')
                funding = values.get('funding_rate')
                votes = []
                if self._is_valid_number(probability):
                    votes.append(float(probability) >= 0.52 if side == 'long' else float(probability) <= 0.48)
                if self._is_valid_number(imbalance) and self._is_valid_number(taker_ratio):
                    votes.append(float(imbalance) >= 3.0 and float(taker_ratio) >= 1.03 if side == 'long' else float(imbalance) <= -3.0 and float(taker_ratio) <= 0.97)
                if self._is_valid_number(basis):
                    votes.append(float(basis) <= 0.10 if side == 'long' else float(basis) >= -0.10)
                if self._is_valid_number(funding):
                    votes.append(float(funding) <= 0.0008 if side == 'long' else float(funding) >= -0.0008)
                if len(votes) < 2:
                    _add('Cross-Market 확인', None, '크로스마켓 데이터 2개 미만', 'REJECTED_PREDICTION_CROSS_MARKET')
                else:
                    passed = sum(1 for vote in votes if vote)
                    _add('Cross-Market 확인', passed >= 2, f"{passed}/{len(votes)} votes pass", 'REJECTED_PREDICTION_CROSS_MARKET')
            else:
                _add(filter_name, None, 'planned 필터: 아직 실거래 연결 안 됨', 'REJECTED_RISK_REWARD_LOW')
        selected_id = int((set_info or {}).get('id') or 0)
        if selected_id in {61, 62, 63} and bool(cfg.get('feature_score_enabled', True)):
            feature = values.get('feature_score')
            if not isinstance(feature, dict):
                feature = self._calculate_utbreakout_feature_score(side, cfg, values)
            score = feature.get('score') if isinstance(feature, dict) else None
            block_below = float(cfg.get('feature_score_block_below', 55.0) or 55.0)
            reduce_below = float(cfg.get('feature_score_reduce_below', 65.0) or 65.0)
            if not self._is_valid_number(score):
                _add('Feature Score', None, 'feature score 계산 대기', 'REJECTED_FEATURE_SCORE')
            else:
                score_value = float(score)
                mode = 'risk-reduce' if score_value < reduce_below else 'normal'
                _add(
                    'Feature Score',
                    score_value >= block_below,
                    f"score {score_value:.1f} / block<{block_below:.1f}, reduce<{reduce_below:.1f} / {feature.get('reason', mode)}",
                    'REJECTED_FEATURE_SCORE',
                )
        return items

    def _calculate_wilder_atr_series(self, closed, length):
        if closed is None or len(closed) < length:
            return pd.Series(np.nan, index=range(0 if closed is None else len(closed)), dtype=float)
        high = closed['high'].astype(float).reset_index(drop=True)
        low = closed['low'].astype(float).reset_index(drop=True)
        close = closed['close'].astype(float).reset_index(drop=True)
        prev_close = close.shift(1)
        true_range = pd.concat([
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ], axis=1).max(axis=1).fillna(0.0)
        return self._calculate_rma_series(true_range, int(length or 14))

    def _calculate_wilder_rsi_series(self, close, length):
        close = pd.Series(close, dtype=float).reset_index(drop=True)
        if len(close) < length + 1:
            return pd.Series(np.nan, index=close.index, dtype=float)
        delta = close.diff().fillna(0.0)
        gain = delta.clip(lower=0.0)
        loss = (-delta).clip(lower=0.0)
        avg_gain = self._calculate_rma_series(gain, int(length or 14))
        avg_loss = self._calculate_rma_series(loss, int(length or 14))
        rs = avg_gain / avg_loss.replace(0.0, np.nan)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        rsi = rsi.where(avg_loss != 0.0, 100.0)
        rsi = rsi.where(~((avg_gain == 0.0) & (avg_loss == 0.0)), 50.0)
        return rsi

    def _is_valid_number(self, value):
        try:
            return np.isfinite(float(value))
        except (TypeError, ValueError):
            return False

    async def fetch_ohlcv_async(self, symbol, timeframe, limit=300):
        """Fetch market data without blocking the live scanner event loop."""
        return await asyncio.to_thread(
            self.market_data_exchange.fetch_ohlcv,
            symbol,
            timeframe,
            limit=limit,
        )

    def _canonical_futures_symbol(self, symbol, quote='USDT'):
        """Return one ccxt USD-M perpetual symbol for common symbol aliases."""
        return canonical_futures_symbol(symbol, quote)

    def _canonicalize_utbreakout_symbol_for_use(self, symbol, source='unknown'):
        canonical = self._canonical_futures_symbol(symbol)
        if str(symbol or '') != str(canonical):
            # Position snapshots can legitimately use the shorter CCXT alias
            # (for example RAVE/USDT). Normalizing that alias on every poll is
            # expected runtime behavior, not an operator warning.
            log_normalization = (
                logger.debug
                if str(source or '').startswith('poll_tick_')
                else logger.warning
            )
            log_normalization(
                "[UTBREAK_SYMBOL_NORMALIZED] source=%s raw=%s canonical=%s",
                source,
                symbol,
                canonical,
            )
        return canonical

    def _get_exchange_markets_safe(self):
        """Return loaded exchange markets without raising."""
        for exchange_name in ('exchange', 'market_data_exchange'):
            try:
                exchange = getattr(self, exchange_name, None)
                markets = getattr(exchange, 'markets', None)
                if isinstance(markets, dict) and markets:
                    return markets
            except Exception:
                continue
        return {}

    def _is_valid_exchange_market_symbol(self, symbol):
        """Validate a canonical symbol when ccxt markets are already loaded."""
        try:
            canonical = self._canonical_futures_symbol(symbol)
        except Exception:
            canonical = str(symbol or '')
        markets = self._get_exchange_markets_safe()
        if not markets:
            return True
        raw = str(symbol or '').strip()
        return canonical in markets or raw in markets

    def _record_invalid_exchange_market_symbol(
        self,
        symbol,
        source='unknown',
    ):
        """Record a nonexistent exchange market before data or order calls."""
        try:
            canonical = self._canonical_futures_symbol(symbol)
        except Exception:
            canonical = str(symbol or '')
        markets = self._get_exchange_markets_safe()
        market_count = len(markets) if isinstance(markets, dict) else 0
        reason = (
            f"exchange.markets does not contain canonical symbol {canonical}"
        )
        try:
            self._utbreakout_trace_event(
                canonical,
                'SYMBOL_INVALID_MARKET',
                'BLOCK',
                source=source,
                raw_symbol=str(symbol),
                canonical_symbol=canonical,
                market_count=market_count,
                reason=reason,
            )
        except Exception:
            logger.debug("invalid market trace skipped", exc_info=True)
        logger.warning(
            "[UTBREAK_INVALID_MARKET] source=%s raw=%s canonical=%s "
            "market_count=%s",
            source,
            symbol,
            canonical,
            market_count,
        )
        return reason

    def _utbreakout_restricted_symbol_bases(self):
        """Account-region exclusions supplied by the user; never scan or trade."""
        return {'SKHYNIX', 'HYUNDAI', 'SAMSUNG'}

    def _utbreakout_restricted_symbol_exceptions(self):
        """Explicitly allowed Binance contracts that must not match legacy names."""
        # SKHY is the Nasdaq ADR-linked Binance TradFi perpetual. It is a
        # different contract from the legacy SKHYNIX synthetic ticker.
        return {'SKHY'}

    def _utbreakout_symbol_base(self, symbol):
        raw = str(symbol or '').upper().strip()
        if not raw:
            return ''
        try:
            canonical = self._canonical_futures_symbol(raw)
        except Exception:
            canonical = raw
        text = str(canonical or raw).upper().strip()
        if '/' in text:
            return (
                text.split('/', 1)[0]
                .replace('-', '')
                .replace('_', '')
                .strip()
            )
        for quote in ('USDT', 'USDC', 'BUSD'):
            if text.endswith(quote) and len(text) > len(quote):
                return (
                    text[:-len(quote)]
                    .replace('-', '')
                    .replace('_', '')
                    .strip()
                )
        return (
            text.replace(':USDT', '')
            .replace(':USDC', '')
            .replace(':BUSD', '')
            .replace('/', '')
            .replace('-', '')
            .replace('_', '')
            .strip()
        )

    def _is_utbreakout_restricted_symbol(self, symbol):
        base = self._utbreakout_symbol_base(symbol)
        if base in self._utbreakout_restricted_symbol_exceptions():
            return False
        return base in self._utbreakout_restricted_symbol_bases()

    def _utbreakout_restricted_symbol_reason(self, symbol):
        base = self._utbreakout_symbol_base(symbol)
        return (
            f"{base} is excluded because this user's Korean Binance account "
            "cannot trade this restricted ticker."
        )

    def _record_utbreakout_restricted_symbol(self, symbol, source='unknown'):
        canonical = self._canonical_futures_symbol(symbol)
        reason = self._utbreakout_restricted_symbol_reason(canonical)
        try:
            self._utbreakout_trace_event(
                canonical,
                'SYMBOL_BLOCKED_REGION_RESTRICTED',
                'BLOCK',
                source=source,
                raw_symbol=str(symbol),
                canonical_symbol=canonical,
                base=self._utbreakout_symbol_base(canonical),
                reason=reason,
            )
        except Exception:
            logger.debug("restricted symbol trace skipped", exc_info=True)
        logger.warning(
            "[UTBREAK_RESTRICTED_SYMBOL] source=%s raw=%s canonical=%s reason=%s",
            source,
            symbol,
            canonical,
            reason,
        )
        return reason

    def _ensure_valid_utbreakout_market_symbol(
        self,
        symbol,
        source='unknown',
    ):
        """Return a canonical, account-allowed, loaded futures market."""
        try:
            canonical = self._canonical_futures_symbol(symbol)
        except Exception:
            canonical = str(symbol or '')

        try:
            if self._is_utbreakout_restricted_symbol(canonical):
                reason = self._record_utbreakout_restricted_symbol(
                    canonical,
                    source=source,
                )
                return (
                    False,
                    canonical,
                    'REJECTED_REGION_RESTRICTED_SYMBOL: ' + reason,
                )
        except Exception:
            logger.debug(
                "restricted symbol validation skipped",
                exc_info=True,
            )

        if not self._is_valid_exchange_market_symbol(canonical):
            reason = self._record_invalid_exchange_market_symbol(
                canonical,
                source=source,
            )
            return (
                False,
                canonical,
                'REJECTED_INVALID_MARKET_SYMBOL: ' + reason,
            )

        # The selector's first pass touches every loaded Binance market. Recording
        # thousands of successful validations every refresh adds no diagnostic
        # value; selected/entry-path validations remain fully traced below.
        if str(source or '') != 'coin_selector_candidate_filter':
            try:
                self._utbreakout_trace_event(
                    canonical,
                    'MARKET_VALIDATED',
                    'OK',
                    source=source,
                    raw_symbol=str(symbol),
                    canonical_symbol=canonical,
                    market_count=len(self._get_exchange_markets_safe()),
                )
            except Exception:
                logger.debug("market validated trace skipped", exc_info=True)
        return True, canonical, ''

    def _utbreakout_plan_symbol_keys(self, symbol):
        raw = str(symbol or '').strip()
        if not raw:
            return []
        canonical = self._canonical_futures_symbol(raw)
        variants = [
            canonical,
            canonical.replace(':USDT', ''),
            canonical.replace(':USDC', ''),
            canonical.replace(':BUSD', ''),
            raw,
        ]

        try:
            variants.append('__key__:' + self._futures_symbol_key(canonical))
        except Exception:
            variants.append('__key__:' + self._utbreakout_trace_key(canonical))

        out = []
        seen = set()
        for item in variants:
            item = str(item or '').strip()
            if item and item not in seen:
                seen.add(item)
                out.append(item)
        return out

    def _set_utbot_filtered_breakout_entry_plan(self, symbol, plan):
        if not isinstance(plan, dict):
            return
        stored = dict(plan)
        if not bool(stored.get('strategy_allocator_applied')):
            stored = self._apply_strategy_allocator_to_plan(stored)
        canonical = self._canonical_futures_symbol(
            stored.get('plan_symbol') or symbol
        )
        stored['plan_symbol'] = canonical
        for key in self._utbreakout_plan_symbol_keys(canonical):
            self.utbot_filtered_breakout_entry_plans[key] = stored
        self._utbreakout_trace_event(
            canonical,
            'PLAN_CREATED',
            'OK',
            side=stored.get('side'),
            entry_price=stored.get('entry_price'),
            qty=stored.get('qty'),
            margin=stored.get('planned_margin', stored.get('margin_to_use')),
            notional=stored.get('planned_notional', stored.get('target_notional')),
            risk=stored.get('risk_usdt', stored.get('risk_amount')),
            plan_symbol=stored.get('plan_symbol'),
        )

    def _clear_utbot_filtered_breakout_entry_plan(self, symbol):
        plans = getattr(self, 'utbot_filtered_breakout_entry_plans', None)
        if not isinstance(plans, dict):
            self.utbot_filtered_breakout_entry_plans = {}
            return
        for key in self._utbreakout_plan_symbol_keys(symbol):
            plans.pop(key, None)
        target_key = self._utbreakout_trace_key(symbol)
        for stored_key, plan in list(plans.items()):
            if not isinstance(plan, dict):
                continue
            plan_symbol = plan.get('plan_symbol') or plan.get('symbol') or stored_key
            plan_key = self._utbreakout_trace_key(plan_symbol)
            if plan_key == target_key:
                plans.pop(stored_key, None)

    def _get_utbot_filtered_breakout_entry_plan(self, symbol, side=None):
        for key in self._utbreakout_plan_symbol_keys(symbol):
            plan = self.utbot_filtered_breakout_entry_plans.get(key)
            if isinstance(plan, dict):
                if side and str(plan.get('side', '')).lower() != str(side).lower():
                    continue
                return plan

        # Last-resort fallback: match by normalized futures key.
        target_key = self._utbreakout_trace_key(symbol)
        for stored_key, plan in list(self.utbot_filtered_breakout_entry_plans.items()):
            if not isinstance(plan, dict):
                continue
            plan_symbol = plan.get('plan_symbol') or plan.get('symbol') or stored_key
            plan_key = self._utbreakout_trace_key(plan_symbol)
            if plan_key != target_key:
                continue
            if side and str(plan.get('side', '')).lower() != str(side).lower():
                continue
            return plan

        return None

    def _utbreakout_trace_key(self, symbol):
        raw = str(symbol or '').strip()
        if not raw:
            return 'UNKNOWN'
        canonical = self._canonical_futures_symbol(raw)
        try:
            return self._futures_symbol_key(canonical)
        except Exception:
            text = str(canonical or raw).upper()
            if '/' in text:
                base, rest = text.split('/', 1)
                quote_part = rest.split(':', 1)[0]
                return (
                    (base + quote_part)
                    .replace('-', '')
                    .replace('_', '')
                )
            return (
                text.replace(':USDT', '')
                .replace(':USDC', '')
                .replace(':BUSD', '')
                .replace('/', '')
                .replace('-', '')
                .replace('_', '')
            )

    def _ensure_utbreakout_trace_state(self):
        for name in (
            'utbreakout_entry_trace',
            'utbreakout_last_ready_ts',
            'utbreakout_last_ready_side',
            'utbreakout_last_order_attempt_ts',
            'utbreakout_last_watchdog_report_ts',
        ):
            if not isinstance(getattr(self, name, None), dict):
                setattr(self, name, {})
        if not hasattr(self, 'utbreakout_trace_watchdog_enabled'):
            self.utbreakout_trace_watchdog_enabled = False
        for name in (
            'utbreakout_last_status_symbol',
            'utbreakout_last_ready_symbol',
            'current_utbreakout_candidate_symbol',
            'utbreakout_status_symbol_source',
            'utbreakout_status_symbol_detail',
        ):
            if not hasattr(self, name):
                setattr(self, name, None)

    def _resolve_utbreakout_trace_symbol(self, symbol_arg=None):
        raw = str(symbol_arg or '').strip()
        if raw:
            if raw.lower() in {'all', '*'}:
                return None
            return self._canonical_futures_symbol(raw)
        for attr in (
            'utbreakout_last_status_symbol',
            'utbreakout_last_ready_symbol',
            'current_utbreakout_candidate_symbol',
            'current_coin_selector_symbol',
            'symbol',
        ):
            value = getattr(self, attr, None)
            if value:
                return self._canonical_futures_symbol(value)
        return None

    def _utbreakout_trace_event(self, symbol, stage, status='INFO', **data):
        """Record a bounded, best-effort UTBreakout entry-pipeline breadcrumb."""
        try:
            self._ensure_utbreakout_trace_state()
            canonical_symbol = self._canonical_futures_symbol(symbol)
            key = self._utbreakout_trace_key(canonical_symbol)
            events = self.utbreakout_entry_trace.setdefault(key, [])
            safe_data = {}
            for data_key, value in (data or {}).items():
                try:
                    if isinstance(value, float):
                        safe_data[data_key] = round(value, 8)
                    elif isinstance(value, (str, int, bool)) or value is None:
                        safe_data[data_key] = value
                    else:
                        safe_data[data_key] = str(value)
                except Exception:
                    safe_data[data_key] = '<unserializable>'
            now_ts = time.time()
            stage_text = str(stage or '').upper()
            rec = {
                'ts': now_ts,
                'time': datetime.now(timezone(timedelta(hours=9))).strftime(
                    '%m-%d %H:%M:%S KST'
                ),
                'symbol': canonical_symbol,
                'stage': stage_text,
                'status': str(status or 'INFO'),
                'data': safe_data,
            }
            events.append(rec)
            if len(events) > 200:
                del events[:len(events) - 200]

            stage_upper = stage_text
            if stage_upper == 'STATUS_EVALUATED':
                self.utbreakout_last_status_symbol = canonical_symbol or None
            elif stage_upper == 'STATUS_READY':
                self.utbreakout_last_ready_symbol = canonical_symbol or None
                ready_side = str(safe_data.get('side') or '').lower()
                prior_ready = float(self.utbreakout_last_ready_ts.get(key, 0.0) or 0.0)
                prior_side = str(self.utbreakout_last_ready_side.get(key) or '').lower()
                prior_order = float(
                    self.utbreakout_last_order_attempt_ts.get(key, 0.0) or 0.0
                )
                if prior_ready <= 0 or prior_order >= prior_ready or ready_side != prior_side:
                    self.utbreakout_last_ready_ts[key] = now_ts
                self.utbreakout_last_ready_side[key] = ready_side
            elif stage_upper == 'STATUS_NOT_READY':
                if str(safe_data.get('source') or '').lower() != 'manual_status':
                    self.utbreakout_last_ready_ts[key] = 0.0
                    self.utbreakout_last_ready_side.pop(key, None)
            elif stage_upper == 'ORDER_ATTEMPT':
                self.utbreakout_last_order_attempt_ts[key] = now_ts
            elif stage_upper == 'COIN_SELECTED':
                self.current_utbreakout_candidate_symbol = canonical_symbol or None

            logger.info(
                "[UTBREAK_TRACE] %s %s %s %s",
                key,
                stage,
                status,
                safe_data,
            )
        except Exception:
            logger.debug("UTBreakout trace event failed", exc_info=True)

    def _utbreakout_recent_trace_events(self, symbol=None, limit=60):
        try:
            self._ensure_utbreakout_trace_state()
            if symbol:
                key = self._utbreakout_trace_key(symbol)
                return list(self.utbreakout_entry_trace.get(key, []))[-limit:]
            all_events = []
            for events in self.utbreakout_entry_trace.values():
                all_events.extend(events)
            all_events.sort(key=lambda item: float(item.get('ts', 0.0) or 0.0))
            return all_events[-limit:]
        except Exception:
            return []

    def _format_utbreakout_entry_diagnostics_lines(self, symbol=None):
        events = self._utbreakout_recent_trace_events(symbol, limit=50)
        if not events:
            return [
                "UTBreakout Entry Diagnostics",
                "last 50 candidates: no trace events yet",
            ]
        category_counts = Counter()
        reason_counts = Counter()
        last_no_entry = None
        for event in events:
            stage = str(event.get('stage') or '').upper()
            status = str(event.get('status') or '').upper()
            data = event.get('data') if isinstance(event.get('data'), dict) else {}
            reason = str(data.get('reason') or status or stage or 'unknown')
            if stage == 'ORDER_ATTEMPT':
                category = 'ordered'
            elif stage in {'ENTRY_BLOCKED', 'AUTO_ENTRY_BRIDGE_BLOCKED'}:
                if any(token in status for token in {'PREFLIGHT', 'POSITION', 'ORDER', 'PROTECTION'}):
                    category = 'selected_but_preflight_blocked'
                elif any(token in status for token in {'RISK', 'QTY', 'NOTIONAL', 'BALANCE'}):
                    category = 'selected_but_risk_blocked'
                elif any(token in status for token in {'EXCHANGE', 'FAILED', 'ERROR'}):
                    category = 'selected_but_exchange_blocked'
                else:
                    category = 'strategy_blocked'
                last_no_entry = (event, category, reason)
            elif stage in {'STATUS_NOT_READY', 'STATUS_EVALUATED'} and ('REJECT' in status or 'BLOCK' in status):
                category = 'strategy_blocked'
                last_no_entry = (event, category, reason)
            else:
                category = 'strategy_blocked'
            category_counts[category] += 1
            if category != 'ordered':
                reason_counts[reason[:90]] += 1

        lines = ["UTBreakout Entry Diagnostics", "last 50 candidates:"]
        for label in (
            'strategy_blocked',
            'selected_but_risk_blocked',
            'selected_but_preflight_blocked',
            'selected_but_exchange_blocked',
            'ordered',
        ):
            if category_counts.get(label):
                lines.append(f"- {label}: {category_counts[label]}")
        if reason_counts:
            lines.append("top block reasons:")
            for reason, count in reason_counts.most_common(5):
                lines.append(f"- {reason}: {count}")
        if last_no_entry:
            event, category, reason = last_no_entry
            data = event.get('data') if isinstance(event.get('data'), dict) else {}
            lines.append(
                "last selected no-entry: "
                f"symbol={event.get('symbol')} category={category} "
                f"side={data.get('side') or '-'} reason={reason}"
            )
        return lines

    async def _notify_long_text(self, text, chunk_size=3400):
        text = str(text or '')
        if not text:
            return
        chunks = [text[index:index + chunk_size] for index in range(0, len(text), chunk_size)]
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            prefix = f"[{index}/{total}]\n" if total > 1 else ""
            try:
                notify_plain = getattr(self.ctrl, 'notify_plain', None)
                if callable(notify_plain):
                    await notify_plain(prefix + chunk)
                else:
                    await self.ctrl.notify(prefix + chunk)
            except Exception:
                logger.exception(
                    "Failed to send long Telegram text chunk %s/%s",
                    index,
                    total,
                )
                break

    def _format_utbreakout_trace_report(self, symbol=None, full=False):
        self._ensure_utbreakout_trace_state()
        raw_report_symbol = str(symbol or '').strip()
        if symbol:
            symbol = self._canonical_futures_symbol(symbol)
        available_keys = sorted(self.utbreakout_entry_trace.keys())
        events = self._utbreakout_recent_trace_events(
            symbol,
            limit=120 if full else 40,
        )
        if symbol:
            key = self._utbreakout_trace_key(symbol)
            report_symbol = str(symbol)
            report_scope = 'symbol'
        elif events:
            report_symbol = str(events[-1].get('symbol') or '')
            key = self._utbreakout_trace_key(report_symbol)
            report_scope = 'all'
        else:
            report_symbol = 'UNKNOWN'
            key = 'UNKNOWN'
            report_scope = 'all'

        try:
            cfg = self._get_utbot_filtered_breakout_config(
                self.get_runtime_strategy_params()
            )
        except Exception:
            cfg = {}
        try:
            diag = self._utbreakout_diag_for_symbol(report_symbol)
        except Exception:
            diag = {}
        plan = {}
        try:
            plan = self._get_utbot_filtered_breakout_entry_plan(report_symbol) or {}
        except Exception:
            plan = {}

        stage_last = {}
        for event in events:
            stage_last[str(event.get('stage', '')).upper()] = event

        ready_event = stage_last.get('STATUS_READY')
        evaluated_event = stage_last.get('STATUS_EVALUATED')
        status_data = (
            (ready_event or {}).get('data')
            or (evaluated_event or {}).get('data')
            or {}
        )
        status_ready_side = (
            status_data.get('side')
            or status_data.get('ready_side')
            or diag.get('accepted_side')
            or 'none'
        )
        status_has_plan = bool(plan) or bool(status_data.get('qty'))
        ready_ts = float(
            self.utbreakout_last_ready_ts.get(key, 0.0)
            or (ready_event or {}).get('ts', 0.0)
            or 0.0
        )

        def _last(stage):
            return stage_last.get(stage.upper())

        def _after_ready(stage):
            event = _last(stage)
            if not event:
                return None
            if ready_ts > 0 and float(event.get('ts', 0.0) or 0.0) < ready_ts:
                return None
            return event

        important_stages = [
            'STATUS_EVALUATED', 'STATUS_DIAGNOSTIC_READY', 'STATUS_READY', 'COIN_SELECTED',
            'SCANNER_SEEN', 'MARKET_VALIDATED', 'SYMBOL_INVALID_MARKET',
            'POLL_TICK',
            'POLL_ALREADY_PROCESSED', 'NO_POSITION_RETRY',
            'PROCESS_PRIMARY_START', 'SIGNAL_CALCULATED', 'PLAN_CREATED',
            'AUTO_ENTRY_BRIDGE', 'AUTO_ENTRY_BRIDGE_BLOCKED',
            'ENTRY_CALL', 'ENTRY_ENTERED', 'PLAN_LOOKUP', 'BALANCE_CHECK',
            'ENTRY_BLOCKED', 'ORDER_ATTEMPT', 'ORDER_SUCCESS', 'ORDER_FAILED',
            'POSITION_CONFIRMED', 'NO_POSITION_AFTER_ENTRY',
            'SYMBOL_BLOCKED_REGION_RESTRICTED', 'WATCHDOG',
        ]
        lines = [
            "🧪 UTBreakout Entry Trace Report",
            f"symbol_key: {key}",
            f"현재 후보 심볼: {report_symbol}",
            f"report scope: {report_scope}",
            f"full: {bool(full)}",
            "report_time: "
            + datetime.now(timezone(timedelta(hours=9))).strftime(
                '%Y-%m-%d %H:%M:%S KST'
            ),
            f"Effective Profile: {cfg.get('effective_profile_version', 'UNKNOWN')}",
            f"조건 스테이터스 ready 여부: {bool(ready_event)}",
            f"ready side: {status_ready_side}",
            f"accepted_side: {diag.get('accepted_side') or status_ready_side}",
            f"entry plan 생성/보관 여부: {status_has_plan}",
            f"available trace keys: {available_keys[:30]}",
            f"utbreakout_last_status_symbol: {getattr(self, 'utbreakout_last_status_symbol', None)}",
            f"utbreakout_last_ready_symbol: {getattr(self, 'utbreakout_last_ready_symbol', None)}",
            f"current_utbreakout_candidate_symbol: {getattr(self, 'current_utbreakout_candidate_symbol', None)}",
            f"current_coin_selector_symbol: {getattr(self, 'current_coin_selector_symbol', None)}",
            f"default symbol: {getattr(self, 'symbol', None)}",
            "",
            "== Last Stage Summary ==",
        ]
        raw_upper = raw_report_symbol.upper()
        normalized_input = bool(
            raw_report_symbol
            and raw_report_symbol != report_symbol
        )
        suspicious_input = (
            'USDT/USDT' in raw_upper
            or raw_upper.endswith('USDT/USDT')
            or str(key or '').endswith('USDTUSDT')
        )
        if normalized_input or suspicious_input:
            lines[0:0] = [
                "🚨 Symbol Normalization Notice",
                f"input symbol: {raw_report_symbol}",
                f"canonical symbol: {report_symbol}",
                f"canonical key: {key}",
                "",
            ]
        try:
            if report_symbol and not self._is_valid_exchange_market_symbol(
                report_symbol
            ):
                lines.extend([
                    "",
                    "🚨 Invalid Market Warning",
                    "현재 심볼은 exchange.markets에 없습니다: "
                    f"{report_symbol}",
                    "이 심볼은 스캔/상태평가/진입 대상에서 제외되어야 합니다.",
                ])
        except Exception:
            pass
        if 'ALUSDT' in available_keys and 'ALLOUSDT' in available_keys:
            lines.extend([
                "",
                "ℹ️ AL/ALLO Symbol Note",
                "ALUSDT와 ALLOUSDT가 모두 trace keys에 있습니다.",
                "ALUSDT가 exchange.markets에 없다면 "
                "SYMBOL_INVALID_MARKET으로 제외되어야 합니다.",
                "ALLOUSDT는 ALLO/USDT:USDT로 유지되어야 하며 "
                "AL/USDT:USDT로 축약되면 안 됩니다.",
            ])
        for stage in important_stages:
            event = _last(stage)
            if not event:
                lines.append(f"- {stage}: 없음")
                continue
            compact = ', '.join(
                f"{data_key}={value}"
                for data_key, value in (event.get('data') or {}).items()
            )
            lines.append(
                f"- {stage}: {event.get('time')} | {event.get('status')} | {compact}"
            )

        has_ready = ready_event is not None
        has_process = _after_ready('PROCESS_PRIMARY_START') is not None
        has_signal = _after_ready('SIGNAL_CALCULATED') is not None
        plan_lookup_event = _after_ready('PLAN_LOOKUP')
        has_plan = bool(plan) or (
            _after_ready('PLAN_CREATED') is not None
            or (
                plan_lookup_event is not None
                and str(plan_lookup_event.get('status', '')).upper() == 'FOUND'
            )
        )
        has_entry_call = _after_ready('ENTRY_CALL') is not None
        has_entry_entered = _after_ready('ENTRY_ENTERED') is not None
        bridge_event = _after_ready('AUTO_ENTRY_BRIDGE')
        bridge_blocked = _after_ready('AUTO_ENTRY_BRIDGE_BLOCKED')
        has_order_attempt = _after_ready('ORDER_ATTEMPT') is not None
        has_order_success = _after_ready('ORDER_SUCCESS') is not None
        order_failed = _after_ready('ORDER_FAILED')
        has_position = _after_ready('POSITION_CONFIRMED') is not None
        entry_blocked = _after_ready('ENTRY_BLOCKED')
        invalid_market_event = _last('SYMBOL_INVALID_MARKET')

        lines.extend(["", "== Diagnosis =="])
        if invalid_market_event is not None:
            diagnosis = "존재하지 않는 Binance Futures 심볼 차단"
            meaning = (
                "exchange.markets에 없는 심볼입니다. data="
                f"{invalid_market_event.get('data')}"
            )
        elif has_ready and bridge_event is None and bridge_blocked is None and not has_entry_call:
            diagnosis = "STATUS_READY 이후 AUTO_ENTRY_BRIDGE/ENTRY_CALL 전에서 끊김"
            meaning = "조건창은 진입 가능인데 ready plan을 실제 주문 실행 브리지로 넘기지 못함"
        elif bridge_blocked is not None and not has_entry_call:
            diagnosis = "AUTO_ENTRY_BRIDGE guard에서 차단"
            meaning = str(
                (bridge_blocked.get('data') or {}).get('reason')
                or bridge_blocked.get('status')
                or 'bridge 차단 상세 확인 필요'
            )
        elif bridge_event is not None and not has_entry_call:
            diagnosis = "AUTO_ENTRY_BRIDGE 이후 entry() 호출 전에서 끊김"
            meaning = "bridge는 작동했지만 entry call 직전 오류 또는 guard에서 중단됨"
        elif has_process and not has_signal:
            diagnosis = "process_primary_candle 내부 signal 계산 전/중단"
            meaning = "_calculate_utbot_filtered_breakout_signal 결과가 trace에 남지 않음"
        elif has_signal and not has_entry_call:
            diagnosis = "signal 계산 후 entry() 호출 전에서 끊김"
            meaning = "sig 또는 live entry 분기 전달 상태 확인 필요"
        elif has_entry_call and not has_entry_entered:
            diagnosis = "entry() 호출 기록 후 함수 진입 기록 없음"
            meaning = "await/call 경로 또는 호출 직전 예외 확인 필요"
        elif has_entry_entered and not has_plan:
            diagnosis = "entry() 내부 plan 조회 전/실패"
            meaning = "entry plan alias 또는 생성/조회 경로 확인 필요"
        elif entry_blocked and not has_order_attempt:
            diagnosis = "entry() 내부 주문 전 차단"
            meaning = str((entry_blocked.get('data') or {}).get('reason') or '차단 상세 확인 필요')
        elif has_plan and not has_order_attempt:
            diagnosis = "plan 확인 후 create_order 전에서 차단"
            meaning = "잔고·수량·최소주문금액·live flag·symbol 설정 확인 필요"
        elif order_failed:
            diagnosis = "거래소 주문 실패"
            meaning = str((order_failed.get('data') or {}).get('error') or 'unknown')
        elif has_order_attempt and not has_order_success:
            diagnosis = "ORDER_ATTEMPT 이후 결과 기록 없음"
            meaning = "create_order 응답 또는 비동기 중단 확인 필요"
        elif has_order_success and not has_position:
            diagnosis = "주문 성공 후 포지션 확인 실패"
            meaning = "체결 지연·심볼 불일치·position fetch 확인 필요"
        elif has_position:
            diagnosis = "포지션 확인까지 완료됨"
            meaning = "정상 완료"
        elif not has_ready:
            if _last('STATUS_EVALUATED'):
                diagnosis = "STATUS_EVALUATED 완료, STATUS_READY 없음"
                meaning = "상태 평가는 실행됐지만 현재 LONG/SHORT 진입 가능 판정은 아님"
            else:
                diagnosis = "최근 trace에 STATUS_EVALUATED/STATUS_READY 없음"
                meaning = "status hook 또는 선택한 report symbol 확인 필요"
        else:
            diagnosis = "명확한 중단 지점 없음"
            meaning = "tracefull Recent Events 확인 필요"
        balance_event = _after_ready('BALANCE_CHECK') or _last('BALANCE_CHECK')
        balance_data = (balance_event or {}).get('data') or {}
        order_failed_data = (order_failed or {}).get('data') or {}
        plan_created = _after_ready('PLAN_CREATED') is not None
        plan_lookup = _after_ready('PLAN_LOOKUP') is not None
        lines.extend([
            f"suspected_break_stage: {diagnosis}",
            f"의미: {meaning}",
            "",
            "== Pipeline Flags ==",
            f"scanner_seen 여부: {_after_ready('SCANNER_SEEN') is not None}",
            f"poll_tick 여부: {_after_ready('POLL_TICK') is not None}",
            f"already_processed 여부: {_after_ready('POLL_ALREADY_PROCESSED') is not None}",
            f"no_position_retry 여부: {_after_ready('NO_POSITION_RETRY') is not None}",
            f"process_primary_candle 호출 여부: {has_process}",
            f"signal 계산 결과: {((_after_ready('SIGNAL_CALCULATED') or {}).get('data') or {}).get('sig', '없음')}",
            f"entry plan 생성 여부: {plan_created}",
            f"entry plan 조회 여부: {plan_lookup}",
            f"auto entry bridge 여부: {bridge_event is not None}",
            f"auto entry bridge blocked 여부: {bridge_blocked is not None}",
            f"entry() 호출 여부: {has_entry_call}",
            f"entry() 함수 진입 여부: {has_entry_entered}",
            f"잔고 확인 여부: {balance_event is not None}",
            f"order_attempt 여부: {has_order_attempt}",
            f"order_success 여부: {has_order_success}",
            f"order_failed 여부: {order_failed is not None}",
            f"주문 실패 에러: {order_failed_data.get('error', '없음')}",
            f"position_confirmed 여부: {has_position}",
            f"no_position_after_entry 여부: {_after_ready('NO_POSITION_AFTER_ENTRY') is not None}",
            "",
            "== Key Runtime Values ==",
            f"qty: {balance_data.get('qty', plan.get('qty', status_data.get('qty', 'n/a')))}",
            f"entry_price: {plan.get('entry_price', status_data.get('entry_price', 'n/a'))}",
            f"target_notional: {balance_data.get('target_notional', plan.get('planned_notional', plan.get('target_notional', status_data.get('notional', 'n/a'))))}",
            f"margin_to_use: {balance_data.get('margin_to_use', plan.get('planned_margin', plan.get('margin_to_use', status_data.get('margin', 'n/a'))))}",
            f"free balance: {balance_data.get('free_balance', balance_data.get('free', 'n/a'))}",
            f"min_notional: {balance_data.get('min_notional', 'n/a')}",
            f"risk_usdt: {plan.get('risk_usdt', status_data.get('risk_usdt', 'n/a'))}",
            "",
            "== Recent Events ==",
        ])
        if not events:
            lines.append("no trace events")
            lines.append(
                "available trace keys가 비어 있으면 trace hook이 실제 "
                "status/poll/process/entry 경로에 연결되지 않은 것입니다."
            )
            lines.append(
                "available trace keys에 다른 심볼 키가 있으면 trace 명령의 "
                "기본 심볼 선택 또는 명시 심볼을 확인하세요."
            )
        else:
            for event in events[-(80 if full else 25):]:
                compact = ', '.join(
                    f"{data_key}={value}"
                    for data_key, value in (event.get('data') or {}).items()
                )
                lines.append(
                    f"{event.get('time')} | {event.get('symbol')} | "
                    f"{event.get('stage')} | {event.get('status')} | {compact}"
                )
        lines.extend([
            "",
            "== Copy Guide ==",
            "이 보고서를 그대로 ChatGPT에게 붙여넣으면 주문 파이프라인 중단 지점을 분석할 수 있습니다.",
        ])
        return "\n".join(lines)

    async def _utbreakout_entry_watchdog_check(
        self,
        symbol,
        ready_side=None,
        source='unknown',
    ):
        """Record ready-without-order diagnostics without Telegram delivery."""
        try:
            self._ensure_utbreakout_trace_state()
            if not bool(self.utbreakout_trace_watchdog_enabled):
                return False
            key = self._utbreakout_trace_key(symbol)
            now_ts = time.time()
            ready_ts = float(self.utbreakout_last_ready_ts.get(key, 0.0) or 0.0)
            order_ts = float(
                self.utbreakout_last_order_attempt_ts.get(key, 0.0) or 0.0
            )
            report_ts = float(
                self.utbreakout_last_watchdog_report_ts.get(key, 0.0) or 0.0
            )
            if ready_ts <= 0:
                return False
            try:
                cfg = self._get_utbot_filtered_breakout_config(
                    self.get_runtime_strategy_params()
                )
            except Exception:
                cfg = {}
            wait_sec = float(
                cfg.get('utbreakout_trace_watchdog_wait_sec', 90.0) or 90.0
            )
            cooldown_sec = float(
                cfg.get('utbreakout_trace_watchdog_cooldown_sec', 180.0)
                or 180.0
            )
            if now_ts - ready_ts < wait_sec or order_ts >= ready_ts:
                return False
            if now_ts - report_ts < cooldown_sec:
                return False
            self.utbreakout_last_watchdog_report_ts[key] = now_ts
            self._utbreakout_trace_event(
                symbol,
                'WATCHDOG',
                'READY_BUT_NO_ORDER_ATTEMPT',
                ready_side=ready_side or self.utbreakout_last_ready_side.get(key) or '',
                source=source,
                seconds_since_ready=round(now_ts - ready_ts, 1),
                seconds_since_order_attempt=(
                    round(now_ts - order_ts, 1) if order_ts > 0 else 'never'
                ),
            )
            logger.warning(
                "UTBreakout watchdog recorded ready-without-order without Telegram delivery: "
                "symbol=%s side=%s source=%s",
                symbol,
                ready_side or self.utbreakout_last_ready_side.get(key) or 'unknown',
                source,
            )
            return True
        except Exception:
            logger.exception("UTBreakout entry watchdog failed")
            return False

    def _evaluate_utbreakout_profit_alpha(
        self,
        *,
        side,
        cfg,
        values,
        ev_decision=None,
        ev_net=None,
    ):
        try:
            alpha_cfg = default_profit_alpha_config()
            if isinstance(cfg, dict):
                alpha_cfg.update(cfg)
            return evaluate_profit_alpha(
                side=side,
                values=values if isinstance(values, dict) else {},
                config=alpha_cfg,
                ev_decision=ev_decision,
                ev_net=ev_net,
                meta_stats=self._profit_alpha_meta_snapshot(),
            )
        except Exception as exc:
            logger.exception("UTBreakout profit alpha evaluation failed")
            return ProfitAlphaDecision(
                allowed=False,
                side=str(side or '').lower(),
                engine='ERROR',
                score=0.0,
                probability=0.0,
                risk_multiplier=0.0,
                exit_profile='NONE',
                follow_through_bars=0,
                follow_through_min_mfe_r=0.0,
                early_exit_max_mae_r=0.0,
                blockers=(f"profit alpha error: {exc}",),
            )

    def _profit_alpha_status_payload(self, decision):
        if not isinstance(decision, ProfitAlphaDecision):
            return None
        return {
            'allowed': decision.allowed,
            'engine': decision.engine,
            'score': decision.score,
            'probability': decision.probability,
            'risk_multiplier': decision.risk_multiplier,
            'exit_profile': decision.exit_profile,
            'entry_type': decision.entry_type,
            'direction_score': decision.direction_score,
            'exit_policy': decision.exit_policy,
            'follow_through_bars': decision.follow_through_bars,
            'follow_through_min_mfe_r': decision.follow_through_min_mfe_r,
            'early_exit_max_mae_r': decision.early_exit_max_mae_r,
            'meta_key': decision.meta_key,
            'meta_sample_count': decision.meta_sample_count,
            'meta_expectancy_r': decision.meta_expectancy_r,
            'components': dict(decision.components or {}),
            'reasons': list(decision.reasons),
            'blockers': list(decision.blockers),
            'summary': decision.summary,
        }

    def _entry_edge_status_payload(self, decision):
        if not isinstance(decision, EntryEdgeDecision):
            return None
        return {
            'allowed': decision.allowed,
            'engine': decision.engine,
            'score': decision.score,
            'probability': decision.probability,
            'net_expectancy_r': decision.net_expectancy_r,
            'risk_multiplier': decision.risk_multiplier,
            'exit_profile': decision.exit_profile,
            'entry_type': decision.entry_type,
            'direction_score': decision.direction_score,
            'exit_policy': decision.exit_policy,
            'ev_mode': decision.ev_mode,
            'ev_score': decision.ev_score,
            'alpha_score': decision.alpha_score,
            'mtf_alignment': decision.mtf_alignment,
            'components': dict(decision.components or {}),
            'reasons': list(decision.reasons),
            'blockers': list(decision.blockers),
            'summary': decision.summary,
        }

    def _append_entry_edge_status_item(
        self,
        core_items,
        *,
        side,
        cfg,
        filter_values,
        market_regime_context,
        selector_quality,
        quality_score_v2,
        adaptation,
        ev_status_decision=None,
        ev_status_net=None,
        ev_status_exit=None,
    ):
        if not bool((cfg or {}).get('entry_edge_enabled', True)):
            return None
        adaptation = adaptation if isinstance(adaptation, dict) else {}
        trend_health = (
            adaptation.get('trend_health')
            if isinstance(adaptation.get('trend_health'), dict)
            else {}
        )
        strategy_quality = (
            adaptation.get('strategy_quality')
            if isinstance(adaptation.get('strategy_quality'), dict)
            else {}
        )
        filter_values.update({
            'trend_health_score': trend_health.get('score'),
            'strategy_quality_score': strategy_quality.get('score'),
            'quality_score_v2_score': (quality_score_v2 or {}).get('score'),
            'coin_selector_score': (selector_quality or {}).get('score'),
            'market_regime_context': market_regime_context,
            'ev_adaptive_mode': ev_status_decision.mode if ev_status_decision is not None else None,
            'ev_win_probability': (
                ev_status_decision.win_probability
                if ev_status_decision is not None
                else None
            ),
            'ev_leadership_score': (
                ev_status_decision.leadership_score
                if ev_status_decision is not None
                else None
            ),
            'ev_reacceleration': (
                ev_status_decision.reacceleration
                if ev_status_decision is not None
                else False
            ),
        })
        alpha_decision = self._evaluate_utbreakout_profit_alpha(
            side=side,
            cfg=cfg,
            values=filter_values,
            ev_decision=ev_status_decision,
            ev_net=ev_status_net,
        )
        decision = build_entry_edge_decision(
            side=side,
            ev_decision=ev_status_decision,
            alpha_decision=alpha_decision,
            ev_net=ev_status_net,
            ev_exit=ev_status_exit,
            config=cfg,
        )
        detail = decision.summary
        if not decision.allowed:
            detail += ": " + "; ".join(decision.blockers[:4])
        core_items.append(("Entry Edge", bool(decision.allowed), detail))
        return decision

    def _evaluate_utbreakout_entry_quality_gate(
        self,
        side,
        cfg,
        *,
        final_risk_multiplier,
        market_quality=None,
        ev_decision=None,
        ev_net=None,
        ev_exit=None,
        alpha_decision=None,
        entry_edge_decision=None,
    ):
        """Execution-only quality gate for already-selected UTBreakout candidates."""
        cfg = cfg if isinstance(cfg, dict) else {}
        if not bool(cfg.get('entry_quality_gate_enabled', True)):
            return True, "OFF"

        side = str(side or '').lower()
        if side not in {'long', 'short'}:
            return False, "BLOCK: invalid side"

        def _cfg_float(key, default):
            try:
                return float(cfg.get(key, default))
            except (TypeError, ValueError):
                return float(default)

        def _cfg_int(key, default):
            try:
                return int(float(cfg.get(key, default)))
            except (TypeError, ValueError):
                return int(default)

        def _num(value, default=None):
            try:
                value = float(value)
            except (TypeError, ValueError):
                return default
            if not np.isfinite(value):
                return default
            return value

        def _attr(obj, name, default=None):
            if obj is None:
                return default
            if isinstance(obj, dict):
                return obj.get(name, default)
            return getattr(obj, name, default)

        def _mtf_votes(value):
            text = str(value or '').strip()
            match = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", text)
            if not match:
                return None, None
            return int(match.group(1)), int(match.group(2))

        final_multiplier = max(0.0, min(1.0, _num(final_risk_multiplier, 0.0)))
        base_min = _cfg_float('entry_quality_gate_min_final_risk_multiplier', 0.35)
        min_final = _cfg_float(
            f'entry_quality_gate_{side}_min_final_risk_multiplier',
            base_min,
        )
        min_final = max(0.0, min(1.0, min_final))

        blockers = []
        if final_multiplier + 1e-12 < min_final:
            blockers.append(
                f"final risk multiplier x{final_multiplier:.3f}<x{min_final:.2f}"
            )

        market_quality = market_quality if isinstance(market_quality, dict) else {}
        market_multiplier = max(
            0.0,
            min(1.0, _num(market_quality.get('risk_multiplier'), 1.0)),
        )
        market_floor = max(
            0.0,
            min(1.0, _cfg_float('entry_quality_gate_hard_market_multiplier_below', 0.25)),
        )
        if market_quality.get('hard_block') or market_quality.get('state') is False:
            blockers.append("market quality hard block")
        elif market_multiplier + 1e-12 < market_floor:
            blockers.append(
                f"market quality x{market_multiplier:.2f}<x{market_floor:.2f}"
            )

        edge_parts = []
        if entry_edge_decision is not None:
            edge_allowed = bool(_attr(entry_edge_decision, 'allowed', False))
            edge_engine = str(_attr(entry_edge_decision, 'engine', 'ENTRY_EDGE') or 'ENTRY_EDGE')
            edge_score = _num(_attr(entry_edge_decision, 'score'), 0.0)
            edge_probability = _num(_attr(entry_edge_decision, 'probability'), 0.0)
            edge_net_r = _num(_attr(entry_edge_decision, 'net_expectancy_r'), None)
            edge_risk = _num(_attr(entry_edge_decision, 'risk_multiplier'), 0.0)
            edge_blockers = _attr(entry_edge_decision, 'blockers', ()) or ()
            if not edge_allowed:
                blocker_text = '; '.join(str(item) for item in list(edge_blockers)[:3])
                blockers.append(
                    f"Entry Edge {edge_engine} not allowed"
                    + (f": {blocker_text}" if blocker_text else "")
                )
            edge_parts.extend([
                f"{edge_engine} score {edge_score:.1f}",
                f"p {edge_probability:.3f}",
                f"risk x{edge_risk:.2f}",
            ])
            if edge_net_r is not None:
                edge_parts.append(f"net {edge_net_r:.3f}R")

        ev_parts = []
        if entry_edge_decision is None and ev_decision is not None:
            ev_allowed = bool(_attr(ev_decision, 'allowed', False))
            ev_mode = str(_attr(ev_decision, 'mode', 'EV') or 'EV')
            ev_score = _num(_attr(ev_decision, 'score'), 0.0)
            ev_probability = _num(_attr(ev_decision, 'win_probability'), 0.0)
            ev_net_r = _num(_attr(ev_net, 'expected_net_r'), None)
            mtf_alignment = _attr(ev_decision, 'mtf_alignment', 'n/a')
            mtf_votes, mtf_total = _mtf_votes(mtf_alignment)

            if not ev_allowed:
                blockers.append(f"EV {ev_mode} not allowed")

            min_ev_score = _cfg_float('entry_quality_gate_min_ev_score', 58.0)
            if ev_score + 1e-12 < min_ev_score:
                blockers.append(f"EV score {ev_score:.1f}<{min_ev_score:.1f}")
            ev_parts.append(f"score {ev_score:.1f}")

            min_ev_probability = _cfg_float(
                'entry_quality_gate_min_ev_probability',
                0.53,
            )
            if ev_probability + 1e-12 < min_ev_probability:
                blockers.append(
                    f"EV p {ev_probability:.2f}<{min_ev_probability:.2f}"
                )
            ev_parts.append(f"p {ev_probability:.2f}")

            min_ev_net = _cfg_float(
                'entry_quality_gate_min_ev_net_expectancy_r',
                0.18,
            )
            if ev_net_r is not None:
                if ev_net_r + 1e-12 < min_ev_net:
                    blockers.append(f"EV net {ev_net_r:.3f}R<{min_ev_net:.2f}R")
                ev_parts.append(f"net {ev_net_r:.3f}R")

            if ev_exit is not None and not bool(_attr(ev_exit, 'executable', True)):
                blockers.append("exit ladder not executable")

            min_mtf_votes = max(0, _cfg_int('entry_quality_gate_min_ev_mtf_votes', 2))
            if mtf_total and mtf_votes is not None:
                if mtf_votes < min_mtf_votes:
                    blockers.append(
                        f"MTF {mtf_votes}/{mtf_total}<{min_mtf_votes}/{mtf_total}"
                    )
                ev_parts.append(f"MTF {mtf_votes}/{mtf_total}")

        alpha_parts = []
        if entry_edge_decision is None and alpha_decision is not None:
            alpha_allowed = bool(_attr(alpha_decision, 'allowed', False))
            alpha_engine = str(_attr(alpha_decision, 'engine', 'ALPHA') or 'ALPHA')
            alpha_score = _num(_attr(alpha_decision, 'score'), 0.0)
            alpha_probability = _num(_attr(alpha_decision, 'probability'), 0.0)
            alpha_risk = _num(_attr(alpha_decision, 'risk_multiplier'), 0.0)
            alpha_blockers = _attr(alpha_decision, 'blockers', ()) or ()
            if not alpha_allowed:
                blocker_text = '; '.join(str(item) for item in list(alpha_blockers)[:3])
                blockers.append(
                    f"Profit Alpha {alpha_engine} not allowed"
                    + (f": {blocker_text}" if blocker_text else "")
                )

            min_alpha_score = _cfg_float(
                'entry_quality_gate_min_profit_alpha_score',
                _cfg_float('profit_alpha_min_score', 68.0),
            )
            if alpha_score + 1e-12 < min_alpha_score:
                blockers.append(
                    f"Profit Alpha score {alpha_score:.1f}<{min_alpha_score:.1f}"
                )
            alpha_parts.append(f"{alpha_engine} score {alpha_score:.1f}")

            min_alpha_probability = _cfg_float(
                'entry_quality_gate_min_profit_alpha_probability',
                _cfg_float('profit_alpha_min_probability', 0.555),
            )
            if alpha_probability + 1e-12 < min_alpha_probability:
                blockers.append(
                    f"Profit Alpha p {alpha_probability:.3f}<{min_alpha_probability:.3f}"
                )
            alpha_parts.append(f"p {alpha_probability:.3f}")
            alpha_parts.append(f"risk x{alpha_risk:.2f}")

        if blockers:
            return False, "BLOCK: " + "; ".join(blockers[:5])

        state = 'reduced' if final_multiplier < 0.999 else True
        detail_parts = [
            f"PASS final x{final_multiplier:.3f}>=x{min_final:.2f}",
            f"market x{market_multiplier:.2f}",
        ]
        if ev_parts:
            detail_parts.append("EV " + ", ".join(ev_parts[:4]))
        if alpha_parts:
            detail_parts.append("Alpha " + ", ".join(alpha_parts[:4]))
        if edge_parts:
            detail_parts.append("Entry Edge " + ", ".join(edge_parts[:4]))
        return state, "; ".join(detail_parts)

    def _build_utbreakout_execution_eligibility(
        self,
        *,
        symbol: str,
        side: str,
        candidate_side: str | None,
        candidate_type: str | None,
        side_condition_ok: bool,
        risk_ok: bool,
        planned_qty: float | None,
        risk_usdt: float | None,
        entry_plan_detail: str,
        cooldown_reasons: list[str],
        has_open_position: bool,
        has_other_position: bool,
        auto_entry_enabled: bool,
        daily_risk_ok: bool,
        plan_lookup_ready: bool,
        cfg: dict,
        scanner_source: str | None = None,
        is_live_scanner_context: bool = False,
        is_current_scanner_candidate: bool = False,
        is_coinselector_top_candidate: bool | None = None,
        next_scan_symbol: str | None = None,
        evaluated_symbol: str | None = None,
        manual_status_only: bool = False,
    ):
        cfg = cfg if isinstance(cfg, dict) else {}
        blockers = []
        warnings = []
        strategy_params = self.get_runtime_strategy_params()
        active_strategy = str(strategy_params.get('active_strategy', '') or '').lower()

        if active_strategy not in UTBREAKOUT_STRATEGIES:
            blockers.append("active strategy not UTBreakout")
        try:
            daily_sl_locked, daily_sl_reason = self._is_utbreakout_daily_sl_locked(symbol)
        except Exception as exc:
            daily_sl_locked = False
            daily_sl_reason = ""
            logger.warning(
                "UTBreakout daily SL lockout check failed for %s: %s",
                symbol,
                exc,
            )
        if daily_sl_locked:
            blockers.append(daily_sl_reason or "daily SL lockout active")
        try:
            recent_loss_locked, recent_loss_reason = (
                self._is_utbreakout_recent_loss_cooldown_active(symbol, cfg)
            )
        except Exception as exc:
            recent_loss_locked = False
            recent_loss_reason = ""
            logger.warning(
                "UTBreakout recent loss cooldown check failed for %s: %s",
                symbol,
                exc,
            )
        if recent_loss_locked:
            blockers.append(recent_loss_reason or "recent loss cooldown active")
        if side in {'long', 'short'} and not self.is_trade_direction_allowed(side):
            blockers.append(self.format_trade_direction_block_reason(side))
        if not candidate_side or candidate_side != side:
            blockers.append("direction mismatch")
        if not side_condition_ok:
            blockers.append("side condition failed")

        evaluated_symbol = evaluated_symbol or symbol
        evaluated_key = self._utbreakout_status_symbol_key(evaluated_symbol)
        candidate_symbol = (
            next_scan_symbol
            or getattr(self, 'current_utbreakout_candidate_symbol', None)
            or getattr(self, 'current_coin_selector_symbol', None)
            or None
        )
        candidate_key = self._utbreakout_status_symbol_key(candidate_symbol)
        require_scanner_candidate = bool(
            cfg.get('utbreakout_require_scanner_candidate_for_auto_entry', True)
        )

        if manual_status_only:
            if candidate_key and evaluated_key and candidate_key == evaluated_key:
                blockers.append(
                    "status screen only; live scanner candidate, scanner loop must emit STATUS_READY"
                )
            else:
                blockers.append(
                    "status screen only; no live scanner candidate for this symbol"
                )
        elif not is_live_scanner_context:
            blockers.append("not live scanner context")

        if require_scanner_candidate:
            if is_live_scanner_context and not is_current_scanner_candidate:
                blockers.append("not current scanner candidate")
            elif candidate_key and evaluated_key and candidate_key != evaluated_key:
                blockers.append("not current scanner candidate")

        if is_coinselector_top_candidate is None:
            scores = getattr(self, 'coin_selector_symbol_scores', {})
            if isinstance(scores, dict) and scores:
                aliases = {
                    symbol,
                    evaluated_symbol,
                    str(symbol or '').replace(':USDT', ''),
                    str(evaluated_symbol or '').replace(':USDT', ''),
                }
                wanted = {
                    self._utbreakout_status_symbol_key(item)
                    for item in aliases
                    if item
                }
                is_coinselector_top_candidate = any(
                    self._utbreakout_status_symbol_key(score_key) in wanted
                    for score_key in scores.keys()
                )

        coin_selector_enabled = False
        try:
            coin_selector_enabled = bool(
                self._get_coin_selector_config().get('enabled', False)
            )
        except Exception:
            coin_selector_enabled = False
        if (
            coin_selector_enabled
            and is_coinselector_top_candidate is None
            and not is_live_scanner_context
        ):
            is_coinselector_top_candidate = False
        if coin_selector_enabled and is_coinselector_top_candidate is False:
            if require_scanner_candidate:
                blockers.append("CoinSelector not selected/top candidate")
            else:
                warnings.append(
                    "CoinSelector not top candidate; selector used as risk reducer only"
                )

        if cooldown_reasons:
            for r in cooldown_reasons:
                cooldown_text = str(r or '').strip()
                if cooldown_text.lower().startswith('candidate cooldown'):
                    warnings.append(f"candidate cooldown advisory: {cooldown_text}")
                else:
                    blockers.append(f"cooldown: {cooldown_text}")
        if not risk_ok:
            blockers.append("risk plan blocked")
        if planned_qty is None or not np.isfinite(planned_qty) or planned_qty <= 0:
            blockers.append("planned quantity zero")
        if risk_usdt is None or not np.isfinite(risk_usdt) or risk_usdt <= 0:
            blockers.append("planned risk zero")
        if not daily_risk_ok:
            blockers.append("daily risk limit reached")
        if not plan_lookup_ready:
            blockers.append("ready entry plan missing")
        elif bool(cfg.get('protection_health_execution_gate_enabled', False)):
            try:
                plan = self._get_utbot_filtered_breakout_entry_plan(symbol, side)
                protection_cfg = dict(cfg)
                protection_cfg['protection_health_require_plan_fields'] = True
                protection_decision = evaluate_protection_health_engine(
                    values=plan if isinstance(plan, dict) else {},
                    config=protection_cfg,
                )
                if not protection_decision.allowed:
                    blockers.append(
                        "protection health blocked: "
                        + "; ".join(protection_decision.blockers[:3])
                    )
                elif protection_decision.risk_multiplier < 1.0:
                    warnings.append(protection_decision.summary)
            except Exception as exc:
                logger.warning(
                    "UTBreakout protection health gate failed for %s %s: %s",
                    symbol,
                    side,
                    exc,
                )
        allow_continuation = bool(cfg.get('utbreakout_allow_continuation_auto_entry', True))
        if candidate_type == 'bias_state' and not allow_continuation:
            blockers.append("continuation entry disabled")
        if has_open_position:
            blockers.append("position already exists")
        if has_other_position:
            blockers.append("other symbol position exists")
        if not auto_entry_enabled:
            blockers.append("bridge disabled")
        if getattr(getattr(self, 'ctrl', None), 'is_paused', False):
            blockers.append("bot is paused")

        can_attempt = len(blockers) == 0

        expected_trace = []
        if can_attempt:
            expected_trace = ["STATUS_READY", "AUTO_ENTRY_BRIDGE", "ENTRY_CALL", "PLAN_LOOKUP", "ORDER_ATTEMPT"]

        return {
            'side': side,
            'symbol': symbol,
            'condition_ok': side_condition_ok,
            'risk_ok': risk_ok,
            'can_attempt': can_attempt,
            'blockers': blockers,
            'warnings': warnings,
            'candidate_type': candidate_type,
            'scanner_source': scanner_source,
            'is_live_scanner_context': bool(is_live_scanner_context),
            'is_current_scanner_candidate': bool(is_current_scanner_candidate),
            'is_coinselector_top_candidate': is_coinselector_top_candidate,
            'expected_trace': expected_trace
        }

    def _record_utbreakout_live_ready_from_diag(
        self,
        symbol: str,
        *,
        source: str,
        scan_tf: str | None = None,
    ) -> str | None:
        """Record real live readiness only from scanner-side accepted diagnostics."""
        try:
            self._ensure_utbreakout_trace_state()
            symbol = self._canonicalize_utbreakout_symbol_for_use(
                symbol,
                source=f'live_ready:{source}',
            )
            diag = self._utbreakout_diag_for_symbol(symbol)
            side = str((diag or {}).get('accepted_side') or '').lower()
            if side not in {'long', 'short'}:
                return None

            plan = self._get_utbot_filtered_breakout_entry_plan(symbol, side)
            if not isinstance(plan, dict):
                self._utbreakout_trace_event(
                    symbol,
                    'STATUS_NOT_READY',
                    'MISSING_PLAN',
                    source=source,
                    side=side,
                    reason='accepted_side exists but entry plan missing',
                )
                return None

            try:
                strategy_params = self.get_runtime_strategy_params()
                cfg = self._get_utbot_filtered_breakout_config(strategy_params)
                entry_tf = str(
                    diag.get('entry_timeframe')
                    or scan_tf
                    or cfg.get('entry_timeframe', '15m')
                    or '15m'
                ).lower()
                decision_ts = _safe_float_or_none(diag.get('decision_candle_ts'))
                if decision_ts is not None and decision_ts > 0:
                    decision_ms = decision_ts * 1000.0 if decision_ts < 10_000_000_000 else decision_ts
                    tf_ms = self._timeframe_to_ms(entry_tf) or (15 * 60 * 1000)
                    max_age_bars = float(
                        cfg.get('utbreakout_status_ready_max_decision_age_bars', 2.0)
                        or 2.0
                    )
                    max_age_ms = max(tf_ms, tf_ms * max_age_bars) + 60_000
                    decision_age_ms = (time.time() * 1000.0) - decision_ms
                    if decision_age_ms > max_age_ms:
                        self._utbreakout_trace_event(
                            symbol,
                            'STATUS_NOT_READY',
                            'STALE_DIAGNOSTIC',
                            source=source,
                            side=side,
                            decision_candle_ts=diag.get('decision_candle_ts'),
                            entry_tf=entry_tf,
                            decision_age_sec=round(decision_age_ms / 1000.0, 1),
                            max_age_sec=round(max_age_ms / 1000.0, 1),
                            reason='accepted diagnostic is too old to synthesize STATUS_READY',
                        )
                        return None
            except Exception as stale_check_exc:
                logger.debug(
                    "UTBreakout live ready stale check skipped for %s: %s",
                    symbol,
                    stale_check_exc,
                )

            self._utbreakout_trace_event(
                symbol,
                'STATUS_READY',
                'READY',
                source=source,
                side=side,
                candidate_type=diag.get('candidate_type'),
                decision_candle_ts=diag.get('decision_candle_ts'),
                entry_tf=diag.get('entry_timeframe') or scan_tf,
                effective_profile=diag.get('effective_profile_version'),
                selected_set=(
                    diag.get('auto_selected_set_name')
                    or diag.get('auto_selected_set_id')
                ),
                qty=plan.get('qty'),
                entry_price=plan.get('entry_price'),
                margin=plan.get('planned_margin', plan.get('margin_to_use')),
                notional=plan.get('planned_notional', plan.get('target_notional')),
                risk_usdt=plan.get('risk_usdt', plan.get('risk_amount')),
            )
            return side
        except Exception:
            logger.exception("UTBreakout live ready recording failed")
            return None

    def _ensure_utbreakout_auto_entry_bridge_state(self):
        if not isinstance(
            getattr(self, 'utbreakout_auto_entry_bridge_last_attempt_ts', None),
            dict,
        ):
            self.utbreakout_auto_entry_bridge_last_attempt_ts = {}
        if not hasattr(self, 'utbreakout_auto_entry_bridge_enabled'):
            self.utbreakout_auto_entry_bridge_enabled = True
        if not isinstance(
            getattr(self, 'utbreakout_daily_sl_symbol_lockouts', None),
            dict,
        ):
            self.utbreakout_daily_sl_symbol_lockouts = {}
        if not isinstance(
            getattr(self, 'utbreakout_recent_loss_symbol_cooldowns', None),
            dict,
        ):
            self.utbreakout_recent_loss_symbol_cooldowns = {}
        if not isinstance(
            getattr(self, 'utbreakout_consumed_decision_ts', None),
            dict,
        ):
            self.utbreakout_consumed_decision_ts = {}

    def _utbreakout_decision_timestamp(self, *sources):
        return resolve_signal_decision_timestamp(*sources)

    def _record_utbreakout_consumed_decision(self, symbol, decision_ts):
        parsed = _timestamp_ms_or_none(decision_ts)
        if parsed is None:
            return None
        self._ensure_utbreakout_auto_entry_bridge_state()
        key = self._utbreakout_trace_key(symbol)
        current = _timestamp_ms_or_none(
            self.utbreakout_consumed_decision_ts.get(key)
        ) or 0.0
        normalized = int(max(float(parsed), float(current)))
        self.utbreakout_consumed_decision_ts[key] = normalized
        return normalized

    def _utbreakout_decision_already_consumed(self, symbol, decision_ts):
        parsed = _timestamp_ms_or_none(decision_ts)
        if parsed is None:
            return False
        target = int(parsed)
        self._ensure_utbreakout_auto_entry_bridge_state()
        key = self._utbreakout_trace_key(symbol)
        remembered = _timestamp_ms_or_none(
            self.utbreakout_consumed_decision_ts.get(key)
        )
        if remembered is not None and target <= int(remembered):
            return True

        store = getattr(self, 'trading_state_store', None)
        if store is None:
            return False
        try:
            records = list(store.records_for_symbol(symbol) or [])
        except Exception:
            return False
        if records_contain_consumed_decision(records, target):
            self._record_utbreakout_consumed_decision(symbol, target)
            return True
        return False

    async def _build_utbreakout_bridge_execution_eligibility(
        self,
        *,
        symbol,
        side,
        plan,
        cfg,
        source,
        ready_data=None,
    ):
        """Recheck execution safety without rerunning an already accepted alpha plan."""
        plan = plan if isinstance(plan, dict) else {}
        ready_data = ready_data if isinstance(ready_data, dict) else {}
        live_sources = {'scanner', 'scanner_seen', 'coin_selector', 'high_volume_scanner'}
        is_live_source = str(source or '') in live_sources
        extra_blockers = []

        daily_risk_ok = True
        db = getattr(self, 'db', None)
        if db is not None:
            try:
                _, daily_pnl = db.get_daily_stats()
                daily_entries = self.get_automatic_daily_entry_count()
                daily_limit = float(cfg.get('daily_max_loss_usdt', 0) or 0)
                trade_limit = int(cfg.get('max_daily_trades', 0) or 0)
                if daily_limit > 0 and float(daily_pnl or 0) <= -daily_limit:
                    daily_risk_ok = False
                if trade_limit > 0 and int(daily_entries or 0) >= trade_limit:
                    daily_risk_ok = False
            except Exception as exc:
                extra_blockers.append(f"daily risk check failed: {exc}")

        active_positions = []
        if getattr(self, 'exchange', None) is not None and hasattr(
            self, 'get_active_position_symbols'
        ):
            try:
                active_positions = await self.get_active_position_symbols(use_cache=False)
            except Exception as exc:
                extra_blockers.append(f"position check failed: {exc}")
        evaluated_key = self._utbreakout_status_symbol_key(symbol)
        active_keys = {
            self._utbreakout_status_symbol_key(item)
            for item in (active_positions or [])
            if item
        }
        has_open_position = evaluated_key in active_keys
        has_other_position = any(key != evaluated_key for key in active_keys)

        planned_qty = _safe_float_or_none(plan.get('qty'))
        risk_usdt = _safe_float_or_none(
            plan.get('risk_usdt', plan.get('risk_amount'))
        )
        risk_ok = bool(
            planned_qty is not None
            and planned_qty > 0
            and risk_usdt is not None
            and risk_usdt > 0
        )
        eligibility = self._build_utbreakout_execution_eligibility(
            symbol=symbol,
            side=side,
            candidate_side=side,
            candidate_type='accepted_plan',
            side_condition_ok=True,
            risk_ok=risk_ok,
            planned_qty=planned_qty,
            risk_usdt=risk_usdt,
            entry_plan_detail='accepted plan execution safety recheck',
            cooldown_reasons=[],
            has_open_position=has_open_position,
            has_other_position=has_other_position,
            auto_entry_enabled=bool(self.utbreakout_auto_entry_bridge_enabled),
            daily_risk_ok=daily_risk_ok,
            plan_lookup_ready=bool(plan),
            cfg=cfg,
            scanner_source=source,
            is_live_scanner_context=is_live_source,
            is_current_scanner_candidate=is_live_source,
            is_coinselector_top_candidate=True,
            next_scan_symbol=symbol,
            evaluated_symbol=symbol,
            manual_status_only=False,
        )
        if extra_blockers:
            eligibility['blockers'] = [
                *list(eligibility.get('blockers') or []),
                *extra_blockers,
            ]
            eligibility['can_attempt'] = False
        return eligibility

    async def _maybe_run_utbreakout_auto_entry_bridge(
        self,
        symbol,
        source='scanner',
    ):
        """Connect a recent ready plan to entry(), only from a live loop."""
        try:
            self._ensure_utbreakout_trace_state()
            self._ensure_utbreakout_auto_entry_bridge_state()
            symbol = self._canonicalize_utbreakout_symbol_for_use(
                symbol,
                source=f'auto_entry_bridge:{source}',
            )

            if not bool(self.utbreakout_auto_entry_bridge_enabled):
                self._utbreakout_trace_event(
                    symbol,
                    'AUTO_ENTRY_BRIDGE_BLOCKED',
                    'DISABLED',
                    source=source,
                    reason='bridge disabled',
                )
                return False

            strategy_params = self.get_runtime_strategy_params()
            active_strategy = str(
                strategy_params.get('active_strategy', '') or ''
            ).lower()
            if active_strategy not in UTBREAKOUT_STRATEGIES:
                self._utbreakout_trace_event(
                    symbol,
                    'AUTO_ENTRY_BRIDGE_BLOCKED',
                    'NOT_UTBREAKOUT',
                    source=source,
                    active_strategy=active_strategy,
                    reason='active strategy is not UTBreakout',
                )
                return False

            ok_market, symbol, invalid_reason = (
                self._ensure_valid_utbreakout_market_symbol(
                    symbol,
                    source='auto_entry_bridge',
                )
            )
            if not ok_market:
                self._utbreakout_trace_event(
                    symbol,
                    'AUTO_ENTRY_BRIDGE_BLOCKED',
                    (
                        'RESTRICTED_SYMBOL'
                        if invalid_reason.startswith(
                            'REJECTED_REGION_RESTRICTED_SYMBOL:'
                        )
                        else 'INVALID_MARKET'
                    ),
                    source=source,
                    reason=invalid_reason,
                )
                return False

            cfg = self._get_utbot_filtered_breakout_config(strategy_params)
            key = self._utbreakout_trace_key(symbol)
            now_ts = time.time()
            cooldown_sec = float(
                cfg.get('utbreakout_auto_entry_bridge_cooldown_sec', 60.0)
                or 60.0
            )
            last_attempt = float(
                self.utbreakout_auto_entry_bridge_last_attempt_ts.get(key, 0.0)
                or 0.0
            )
            if now_ts - last_attempt < cooldown_sec:
                self._utbreakout_trace_event(
                    symbol,
                    'AUTO_ENTRY_BRIDGE_BLOCKED',
                    'COOLDOWN',
                    source=source,
                    reason='duplicate bridge attempt cooldown',
                    cooldown_sec=cooldown_sec,
                    elapsed=round(now_ts - last_attempt, 1),
                )
                return False

            ready_ts = float(
                self.utbreakout_last_ready_ts.get(key, 0.0) or 0.0
            )
            if ready_ts <= 0:
                live_ready_sources = {
                    'scanner',
                    'scanner_seen',
                    'coin_selector',
                    'high_volume_scanner',
                }
                if str(source) in live_ready_sources:
                    synthesized_side = self._record_utbreakout_live_ready_from_diag(
                        symbol,
                        source=f"{source}:synthesized_ready",
                    )
                    if synthesized_side in {'long', 'short'}:
                        ready_ts = float(
                            self.utbreakout_last_ready_ts.get(key, 0.0) or 0.0
                        )
                    else:
                        self._utbreakout_trace_event(
                            symbol,
                            'AUTO_ENTRY_BRIDGE_BLOCKED',
                            'NO_STATUS_READY',
                            source=source,
                            reason='no live STATUS_READY and no accepted diagnostic/plan',
                        )
                        return False
                else:
                    self._utbreakout_trace_event(
                        symbol,
                        'AUTO_ENTRY_BRIDGE_BLOCKED',
                        'NO_STATUS_READY',
                        source=source,
                        reason='no STATUS_READY event for symbol',
                    )
                    return False

            ready_diag = self._utbreakout_diag_for_symbol(symbol)
            ready_entry_tf = (
                ready_diag.get('entry_timeframe')
                if isinstance(ready_diag, dict)
                else None
            )
            max_ready_age_sec = resolve_utbreakout_bridge_ready_age_sec(
                cfg,
                ready_entry_tf,
            )
            ready_age_sec = now_ts - ready_ts
            if ready_age_sec > max_ready_age_sec:
                self._utbreakout_trace_event(
                    symbol,
                    'AUTO_ENTRY_BRIDGE_BLOCKED',
                    'READY_TOO_OLD',
                    source=source,
                    reason='STATUS_READY event is too old',
                    ready_age_sec=round(ready_age_sec, 1),
                    max_ready_age_sec=max_ready_age_sec,
                    entry_timeframe=ready_entry_tf or cfg.get('entry_timeframe') or '15m',
                )
                return False

            ready_events = [
                event
                for event in self._utbreakout_recent_trace_events(
                    symbol,
                    limit=80,
                )
                if str(event.get('stage', '')).upper() == 'STATUS_READY'
                and float(event.get('ts', 0.0) or 0.0) >= ready_ts
            ]
            ready_event = ready_events[-1] if ready_events else {}
            ready_data = (
                ready_event.get('data')
                if isinstance(ready_event, dict)
                and isinstance(ready_event.get('data'), dict)
                else {}
            )
            side = str(
                ready_data.get('side')
                or self.utbreakout_last_ready_side.get(key)
                or ''
            ).lower()
            if side not in {'long', 'short'}:
                self._utbreakout_trace_event(
                    symbol,
                    'AUTO_ENTRY_BRIDGE_BLOCKED',
                    'NO_READY_SIDE',
                    source=source,
                    reason='STATUS_READY side is missing',
                    side=side,
                )
                return False

            plan = self._get_utbot_filtered_breakout_entry_plan(symbol, side)
            if not isinstance(plan, dict):
                self._utbreakout_trace_event(
                    symbol,
                    'AUTO_ENTRY_BRIDGE_BLOCKED',
                    'MISSING_PLAN',
                    source=source,
                    reason='ready entry plan is missing',
                    side=side,
                )
                return False
            decision_ts = self._utbreakout_decision_timestamp(
                plan,
                ready_data,
                ready_diag,
            )
            if self._utbreakout_decision_already_consumed(
                symbol,
                decision_ts,
            ):
                self._utbreakout_trace_event(
                    symbol,
                    'AUTO_ENTRY_BRIDGE_BLOCKED',
                    'DECISION_ALREADY_CONSUMED',
                    source=source,
                    reason='this closed-candle decision already entered once',
                    side=side,
                    decision_candle_ts=decision_ts,
                )
                self._clear_utbot_filtered_breakout_entry_plan(symbol)
                return False
            daily_sl_locked, daily_sl_reason = self._is_utbreakout_daily_sl_locked(symbol)
            if daily_sl_locked:
                self._utbreakout_trace_event(
                    symbol,
                    'AUTO_ENTRY_BRIDGE_BLOCKED',
                    'DAILY_SL_LOCKOUT',
                    source=source,
                    reason=daily_sl_reason,
                    side=side,
                )
                return False
            recent_loss_locked, recent_loss_reason = (
                self._is_utbreakout_recent_loss_cooldown_active(symbol, cfg)
            )
            if recent_loss_locked:
                self._utbreakout_trace_event(
                    symbol,
                    'AUTO_ENTRY_BRIDGE_BLOCKED',
                    'RECENT_LOSS_COOLDOWN',
                    source=source,
                    reason=recent_loss_reason,
                    side=side,
                )
                return False
            eligibility = await self._build_utbreakout_bridge_execution_eligibility(
                symbol=symbol,
                side=side,
                plan=plan,
                cfg=cfg,
                source=source,
                ready_data=ready_data,
            )
            self._utbreakout_trace_event(
                symbol,
                'EXECUTION_SAFETY_GATE',
                'PASS' if eligibility.get('can_attempt') else 'BLOCKED',
                source=source,
                side=side,
                accepted_plan=True,
                blockers=list(eligibility.get('blockers') or []),
            )
            if not eligibility.get('can_attempt'):
                block_reason = "; ".join(eligibility.get('blockers') or []) or "execution blocked"
                self._utbreakout_trace_event(
                    symbol,
                    'AUTO_ENTRY_BRIDGE_BLOCKED',
                    'EXECUTION_SAFETY_REJECTED',
                    source=source,
                    reason=block_reason,
                    side=side,
                    blockers=list(eligibility.get('blockers') or []),
                )
                return False

            entry_price = float(
                plan.get('entry_price')
                or ready_data.get('entry_price')
                or 0.0
            )
            if entry_price <= 0:
                self._utbreakout_trace_event(
                    symbol,
                    'AUTO_ENTRY_BRIDGE_BLOCKED',
                    'INVALID_ENTRY_PRICE',
                    source=source,
                    reason='entry plan has no valid entry price',
                    side=side,
                    entry_price=entry_price,
                )
                return False

            self.utbreakout_auto_entry_bridge_last_attempt_ts[key] = now_ts
            self._utbreakout_trace_event(
                symbol,
                'AUTO_ENTRY_BRIDGE',
                'CALL_ENTRY',
                source=source,
                side=side,
                entry_price=entry_price,
                qty=plan.get('qty'),
                margin=plan.get(
                    'planned_margin',
                    plan.get('margin_to_use'),
                ),
                notional=plan.get(
                    'planned_notional',
                    plan.get('target_notional'),
                ),
                risk=plan.get('risk_usdt', plan.get('risk_amount')),
                ready_age_sec=round(ready_age_sec, 1),
            )
            try:
                await self.ctrl.notify(
                    "🟡 UTBreakout Auto Entry Bridge\n"
                    f"symbol={symbol}\n"
                    f"side={side.upper()}\n"
                    f"source={source}\n"
                    f"qty={plan.get('qty')}\n"
                    f"notional={plan.get('planned_notional', plan.get('target_notional'))}\n"
                    f"margin={plan.get('planned_margin', plan.get('margin_to_use'))}\n"
                    "entry() 호출을 시도합니다."
                )
            except Exception:
                logger.debug("bridge notify skipped", exc_info=True)

            self._utbreakout_trace_event(
                symbol,
                'ENTRY_CALL',
                'CALLING',
                side=side,
                entry_ref_price=entry_price,
                source=f'auto_entry_bridge:{source}',
            )
            try:
                order_path_status = self.resolve_live_order_path_status(
                    self.get_runtime_common_settings(),
                    selected=f"{side.upper()} {symbol}",
                )
                logger.info(
                    "LIVE_ENTRY_DISPATCH bridge_enabled=%s live_order_enabled=%s "
                    "selected=%s symbol=%s",
                    order_path_status.get('bridge_enabled'),
                    order_path_status.get('live_order_enabled'),
                    f"{side.upper()}",
                    symbol,
                )
            except Exception:
                logger.debug("LIVE_ENTRY_DISPATCH status log skipped", exc_info=True)
            await self.entry(symbol, side, entry_price)
            return True
        except Exception as exc:
            try:
                self._utbreakout_trace_event(
                    symbol,
                    'AUTO_ENTRY_BRIDGE_BLOCKED',
                    'ERROR',
                    source=source,
                    reason='bridge raised an exception',
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            except Exception:
                pass
            logger.exception(
                "UTBreakout auto entry bridge failed for %s: %s",
                symbol,
                exc,
            )
            return False
