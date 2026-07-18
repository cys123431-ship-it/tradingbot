"""Configuration loading, defaults, and persistence."""

from __future__ import annotations

class TradingConfig:
    def __init__(self, config_file='config.json'):
        self.config_file = config_file
        self.config = {}
        self.lock = asyncio.Lock()
        self.load_config_sync()

    def load_config_sync(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    self.config = json.load(f)
                # ?꾨씫???꾨뱶 ?먮룞 異붽?
                self._ensure_defaults()
                return True
            except Exception as e:
                logger.error(f"Config load error: {e}")
        self.create_default_config()
        return True

    def _ensure_defaults(self):
        """?꾨씫???꾨뱶 ?먮룞 異붽?"""
        defaults = {
            'api': {
                'exchange_mode': BINANCE_TESTNET,
                'mainnet': {'api_key': '', 'secret_key': ''},
                'testnet': {'api_key': '', 'secret_key': ''},
                'upbit': {'api_key': '', 'secret_key': ''}
            },
            'telegram': {
                'reporting': {
                    'event_alerts_only': True,
                    'periodic_reports_enabled': False,
                    'startup_notice_enabled': False,
                    'startup_keyboard_enabled': False,
                    'hourly_report_enabled': False,
                    'stateful_diag_enabled': False,
                    'monthly_trade_report_enabled': True,
                    'monthly_trade_report_timezone': 'Asia/Seoul',
                    'monthly_trade_report_hour': 9,
                    'alt_trend_alert_enabled': False,
                    'alt_trend_alert_timeframes': ['1d'],
                    'alt_trend_alert_scope': 'binance_futures_all',
                    'alt_trend_alert_stage_mode': 'setup_and_confirm',
                    'alt_trend_alert_profile': 'conservative',
                    'alt_trend_alert_oi_cvd_mode': 'required'
                }
            },
            'system_settings': {
                'active_engine': CORE_ENGINE,
                'trade_direction': 'both',
                'show_dashboard': True,
                'monitoring_interval_seconds': 3
            },
            'shannon_engine': {
                'leverage': 5,
                'daily_loss_limit': 5000,
                'target_symbol': 'BTC/USDT',
                'asset_allocation': {'target_ratio': 0.5, 'allowed_deviation_pct': 2.0},
                'trend_filter': {'enabled': True, 'ema_period': 200},
                'atr_settings': {'enabled': True, 'period': 14, 'grid_multiplier': 0.5},
                'grid_trading': {'enabled': False, 'grid_levels': 5, 'order_size_usdt': 20},
                'drawdown_protection': {'enabled': True, 'threshold_pct': 3.0, 'reduction_factor': 0.5}
            },
            'signal_engine': {
                'common_settings': {
                    'leverage': 10,
                    'timeframe': '15m',
                    'entry_timeframe': '8h',
                    'exit_timeframe': '4h',
                    'risk_per_trade_pct': DEFAULT_RISK_PER_TRADE_PERCENT,
                    'min_risk_per_trade_pct': DEFAULT_MIN_RISK_PER_TRADE_PERCENT,
                    'max_risk_per_trade_pct': DEFAULT_MAX_RISK_PER_TRADE_PERCENT,
                    'target_roe_pct': 20.0,
                    'stop_loss_pct': 10.0,
                    'daily_loss_limit': 5000.0,
                    'daily_loss_limit_pct': 5.0,
                    'tp_sl_enabled': True,
                    'take_profit_enabled': True,
                    'stop_loss_enabled': True,
                    'scanner_enabled': False,
                    'scanner_timeframe': '15m', # [New] Dedicated Scanner TF
                    'scanner_exit_timeframe': '1h', # [New] Dedicated Scanner Exit TF
                    'scanner_scan_interval_seconds': 60,
                    'scanner_min_rise_pct': 0.5,
                    'scanner_max_rise_pct': 8.0,
                    'r2_entry_enabled': True,
                    'r2_exit_enabled': True,
                    'r2_threshold': 0.25,
                    'chop_entry_enabled': True,
                    'chop_exit_enabled': True,
                    'chop_threshold': 50.0,
                    'cc_exit_enabled': False,
                    'cc_threshold': 0.70,
                    'cc_length': 14
                },
                'coin_selector': default_coin_selector_config(),
                'micro_auto': default_micro_auto_config(),
                'strategy_params': {
                    'active_strategy': 'utbot',
                    'entry_mode': 'cross',
                    'ut_entry_timing_mode': 'next_candle',
                    'Triple_SMA': {'fast_sma': 2, 'slow_sma': 10},
                    'HMA': {'fast_period': 9, 'slow_period': 21},
                    'UTBot': {
                        'key_value': 1.0,
                        'atr_period': 10,
                        'use_heikin_ashi': False,
                        'rsi_momentum_filter_enabled': False,
                        'filter_pack': build_default_utbot_filter_pack()
                    },
                    'UTBotFilteredBreakoutV1': build_default_utbot_filtered_breakout_config(),
                    'UTSMC': {
                        'internal_length': 5,
                        'swing_length': 50,
                        'use_confluence_filter': False,
                        'exit_candidate2_enabled': False,
                        'candidate_filter': {
                            'mode': 'off',
                            'apply_to_persistent': True,
                            'c1_release_window': 3,
                            'c2_breakout_window': 2
                        }
                    },
                    'RSIBB': {
                        'rsi_length': 6,
                        'bb_length': 200,
                        'bb_mult': 2.0,
                        'rsibb_enabled': False,
                        'rsibb_paper_only': True,
                        'rsibb_regime_guard_enabled': True
                    },
                    'RSIMomentumTrend': {
                        'rsi_length': 14,
                        'positive_above': 65,
                        'negative_below': 32,
                        'ema_period': 5
                    },
                    'Cameron': {
                        'rsi_period': 14,
                        'rsi_oversold': 30,
                        'rsi_overbought': 70,
                        'bollinger_length': 20,
                        'bollinger_std': 2.0,
                        'macd_fast': 12,
                        'macd_slow': 26,
                        'macd_signal': 9,
                        'extreme_lookback': 60,
                        'macd_confirm_lookback': 3,
                        'band_buffer_pct': 0.001,
                        'risk_reward_ratio': 2.0
                    },
                    'kalman_filter': {
                        'entry_enabled': False,
                        'exit_enabled': False,
                        'observation_covariance': 0.1,
                        'transition_covariance': 0.05
                    }
                }
            },
            'upbit': {
                'watchlist': ['BTC/KRW'],
                'common_settings': {
                    'leverage': 1,
                    'timeframe': '1h',
                    'entry_timeframe': '1h',
                    'exit_timeframe': '1h',
                    'risk_per_trade_pct': DEFAULT_RISK_PER_TRADE_PERCENT,
                    'min_risk_per_trade_pct': DEFAULT_MIN_RISK_PER_TRADE_PERCENT,
                    'max_risk_per_trade_pct': DEFAULT_MAX_RISK_PER_TRADE_PERCENT,
                    'daily_loss_limit': 50000.0,
                    'daily_loss_limit_pct': 5.0,
                    'min_order_krw': 5000.0,
                    'scanner_enabled': False,
                    'scanner_timeframe': '15m',
                    'scanner_exit_timeframe': '1h',
                    'scanner_min_rise_pct': 0.5,
                    'scanner_max_rise_pct': 8.0,
                    'r2_entry_enabled': False,
                    'r2_exit_enabled': False,
                    'r2_threshold': 0.25,
                    'chop_entry_enabled': False,
                    'chop_exit_enabled': False,
                    'chop_threshold': 50.0,
                    'cc_exit_enabled': False,
                    'cc_threshold': 0.70,
                    'cc_length': 14
                },
                'strategy_params': {
                    'active_strategy': 'utbot',
                    'entry_mode': 'position',
                    'UTBot': {
                        'key_value': 1.0,
                        'atr_period': 10,
                        'use_heikin_ashi': False
                    },
                    'kalman_filter': {
                        'entry_enabled': False,
                        'exit_enabled': False,
                        'observation_covariance': 0.1,
                        'transition_covariance': 0.05
                    }
                }
            },
            'dual_thrust_engine': {
                'target_symbol': 'BTC/USDT',
                'leverage': 5,
                'daily_loss_limit': 5000,
                'n_days': 4,
                'k1': 0.5,
                'k2': 0.5,
                'risk_per_trade_pct': DEFAULT_RISK_PER_TRADE_PERCENT,
                'min_risk_per_trade_pct': DEFAULT_MIN_RISK_PER_TRADE_PERCENT,
                'max_risk_per_trade_pct': DEFAULT_MAX_RISK_PER_TRADE_PERCENT
            },
            'dual_mode_engine': {
                'target_symbol': 'BTC/USDT',
                'leverage': 5,
                'mode': 'standard',  # 'scalping' or 'standard'
                'risk_per_trade_pct': DEFAULT_RISK_PER_TRADE_PERCENT,
                'min_risk_per_trade_pct': DEFAULT_MIN_RISK_PER_TRADE_PERCENT,
                'max_risk_per_trade_pct': DEFAULT_MAX_RISK_PER_TRADE_PERCENT,
                'scalping_tf': '5m',
                'standard_tf': '4h'
            },
            'tema_engine': {
                'target_symbol': 'BTC/USDT',
                'timeframe': '5m',
                'rsi_period': 14,
                'tema_period': 9,
                'bollinger_window': 20
            }
        }
        changed = False
        for section, fields in defaults.items():
            if section not in self.config:
                self.config[section] = {}
                changed = True

            for key, val in fields.items():
                if key not in self.config[section]:
                    self.config[section][key] = val
                    changed = True
                elif isinstance(val, dict) and isinstance(self.config[section].get(key), dict):
                    # Nested Dictionary Merge (1-level deep for common_settings etc)
                    for sub_k, sub_v in val.items():
                        if sub_k not in self.config[section][key]:
                            self.config[section][key][sub_k] = sub_v
                            changed = True

        # Enforce signal-only runtime policy while keeping legacy configs archived.
        api_cfg = self.config.setdefault('api', {})
        exchange_mode = str(api_cfg.get('exchange_mode', '')).lower()
        if exchange_mode not in SUPPORTED_EXCHANGE_MODES:
            exchange_mode = BINANCE_TESTNET if api_cfg.get('use_testnet', True) else BINANCE_MAINNET
            api_cfg['exchange_mode'] = exchange_mode
            changed = True
        if exchange_mode == UPBIT_MODE and api_cfg.get('use_testnet', False):
            api_cfg['use_testnet'] = False
            changed = True

        system_cfg = self.config.setdefault('system_settings', {})
        if system_cfg.get('active_engine') != CORE_ENGINE:
            system_cfg['active_engine'] = CORE_ENGINE
            changed = True

        signal_cfg = self.config.setdefault('signal_engine', {})
        strategy_params = signal_cfg.setdefault('strategy_params', {})
        strategy_defaults = {
            'Triple_SMA': {'fast_sma': 2, 'slow_sma': 10},
            'HMA': {'fast_period': 9, 'slow_period': 21},
            'UTBot': {
                'key_value': 1.0,
                'atr_period': 10,
                'use_heikin_ashi': False,
                'rsi_momentum_filter_enabled': False,
                'filter_pack': build_default_utbot_filter_pack()
            },
            'UTBotFilteredBreakoutV1': build_default_utbot_filtered_breakout_config(),
            'UTSMC': {
                'internal_length': 5,
                'swing_length': 50,
                'use_confluence_filter': False,
                'exit_candidate2_enabled': False,
                'candidate_filter': {
                    'mode': 'off',
                    'apply_to_persistent': True,
                    'c1_release_window': 3,
                    'c2_breakout_window': 2
                }
            },
            'ut_entry_timing_mode': 'next_candle',
            'RSIBB': {
                'rsi_length': 6,
                'bb_length': 200,
                'bb_mult': 2.0,
                'rsibb_enabled': False,
                'rsibb_paper_only': True,
                'rsibb_regime_guard_enabled': True
            },
            'RSIMomentumTrend': {
                'rsi_length': 14,
                'positive_above': 65,
                'negative_below': 32,
                'ema_period': 5
            },
            'Cameron': {
                'rsi_period': 14,
                'rsi_oversold': 30,
                'rsi_overbought': 70,
                'bollinger_length': 20,
                'bollinger_std': 2.0,
                'macd_fast': 12,
                'macd_slow': 26,
                'macd_signal': 9,
                'extreme_lookback': 60,
                'macd_confirm_lookback': 3,
                'band_buffer_pct': 0.001,
                'risk_reward_ratio': 2.0
            },
            'kalman_filter': {
                'entry_enabled': False,
                'exit_enabled': False,
                'observation_covariance': 0.1,
                'transition_covariance': 0.05
            }
        }
        for key, default_val in strategy_defaults.items():
            current_val = strategy_params.get(key)
            if not isinstance(default_val, dict):
                if key not in strategy_params:
                    strategy_params[key] = default_val
                    changed = True
                continue
            if not isinstance(current_val, dict):
                strategy_params[key] = dict(default_val)
                changed = True
                continue
            for sub_key, sub_val in default_val.items():
                if sub_key not in current_val:
                    current_val[sub_key] = sub_val
                    changed = True
        utbot_cfg = strategy_params.setdefault('UTBot', {})
        normalized_utbot_filter_pack = normalize_utbot_filter_pack_config(utbot_cfg.get('filter_pack', {}))
        if utbot_cfg.get('filter_pack') != normalized_utbot_filter_pack:
            utbot_cfg['filter_pack'] = normalized_utbot_filter_pack
            changed = True
        utsmc_cfg = strategy_params.setdefault('UTSMC', {})
        if 'exit_candidate2_enabled' not in utsmc_cfg:
            utsmc_cfg['exit_candidate2_enabled'] = False
            changed = True
        elif not isinstance(utsmc_cfg.get('exit_candidate2_enabled'), bool):
            utsmc_cfg['exit_candidate2_enabled'] = bool(utsmc_cfg.get('exit_candidate2_enabled'))
            changed = True
        utsmc_candidate_filter_cfg = utsmc_cfg.setdefault('candidate_filter', {})
        utsmc_candidate_filter_defaults = {
            'mode': 'off',
            'apply_to_persistent': True,
            'c1_release_window': 3,
            'c2_breakout_window': 2
        }
        for key, value in utsmc_candidate_filter_defaults.items():
            if key not in utsmc_candidate_filter_cfg:
                utsmc_candidate_filter_cfg[key] = value
                changed = True
        quad_cfg = strategy_params.setdefault('UTBotFilteredBreakoutV1', {})
        if not bool(quad_cfg.get('lxr_migration_v1_complete', False)):
            migrated_selection = normalize_quad_alpha_enabled_strategies(
                quad_cfg.get('quad_alpha_enabled_strategies')
            )
            if LXR_STRATEGY not in migrated_selection:
                migrated_selection.append(LXR_STRATEGY)
            migrated_selection = [
                key for key in QUAD_ALPHA_BRANCH_ORDER if key in set(migrated_selection)
            ]
            quad_cfg['quad_alpha_enabled_strategies'] = migrated_selection
            lxr_cfg = default_liquidation_exhaustion_reversal_config()
            if isinstance(quad_cfg.get('liquidation_exhaustion_reversal'), dict):
                lxr_cfg.update(quad_cfg.get('liquidation_exhaustion_reversal'))
            lxr_cfg['enabled'] = True
            lxr_cfg['live_enabled'] = True
            quad_cfg['liquidation_exhaustion_reversal'] = lxr_cfg
            quad_cfg['liquidation_exhaustion_reversal_live_enabled'] = True
            # The retired M-TREND branch is always fail-closed after migration.
            quad_cfg['multi_timeframe_trend_live_enabled'] = False
            if isinstance(quad_cfg.get('multi_timeframe_trend'), dict):
                quad_cfg['multi_timeframe_trend']['enabled'] = False
                quad_cfg['multi_timeframe_trend']['live_enabled'] = False
            if str(strategy_params.get('active_strategy') or '').lower() == 'multi_timeframe_trend_v1':
                strategy_params['active_strategy'] = LXR_STRATEGY
            quad_cfg['lxr_migration_v1_complete'] = True
            changed = True

        active_strategy = str(strategy_params.get('active_strategy', 'utbot')).lower()
        if active_strategy not in CORE_STRATEGIES:
            strategy_params['active_strategy'] = 'utbot'
            active_strategy = 'utbot'
            changed = True
        if active_strategy == QUAD_ALPHA_STRATEGY:
            selected = normalize_quad_alpha_enabled_strategies(
                quad_cfg.get('quad_alpha_enabled_strategies')
            )
            if quad_cfg.get('quad_alpha_enabled_strategies') != selected:
                quad_cfg['quad_alpha_enabled_strategies'] = list(selected)
                changed = True
            live_flags = quad_alpha_branch_live_flags(selected)
            top_level_live_flags = {
                'relative_strength_pullback_trend_live_enabled': live_flags[
                    ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND
                ],
                'qh_flow_live_enabled': live_flags[QH_FLOW_STRATEGY],
                'crowding_unwind_live_enabled': live_flags[CROWDING_UNWIND_STRATEGY],
                'liquidation_exhaustion_reversal_live_enabled': live_flags[LXR_STRATEGY],
            }
            for key, value in top_level_live_flags.items():
                if quad_cfg.get(key) is not value:
                    quad_cfg[key] = value
                    changed = True
            nested_live_flags = {
                'relative_strength_pullback_trend': {
                    'relative_strength_pullback_trend_live_enabled': live_flags[
                        ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND
                    ],
                },
                'qh_flow': {
                    'qh_flow_enabled': live_flags[QH_FLOW_STRATEGY],
                    'qh_flow_live_enabled': live_flags[QH_FLOW_STRATEGY],
                },
                'crowding_unwind': {
                    'enabled': live_flags[CROWDING_UNWIND_STRATEGY],
                    'live_enabled': live_flags[CROWDING_UNWIND_STRATEGY],
                },
                'liquidation_exhaustion_reversal': {
                    'enabled': live_flags[LXR_STRATEGY],
                    'live_enabled': live_flags[LXR_STRATEGY],
                },
            }
            for section, values in nested_live_flags.items():
                nested_cfg = quad_cfg.setdefault(section, {})
                if not isinstance(nested_cfg, dict):
                    nested_cfg = {}
                    quad_cfg[section] = nested_cfg
                    changed = True
                for key, value in values.items():
                    if nested_cfg.get(key) is not value:
                        nested_cfg[key] = value
                        changed = True

        entry_mode = str(strategy_params.get('entry_mode', 'cross')).lower()
        if entry_mode not in {'cross', 'position'}:
            strategy_params['entry_mode'] = 'cross'
            changed = True

        def _normalize_percent_risk_cfg(risk_cfg):
            nonlocal changed
            if not isinstance(risk_cfg, dict):
                return
            min_raw = risk_cfg.get('min_risk_per_trade_pct', DEFAULT_MIN_RISK_PER_TRADE_PERCENT)
            try:
                min_value = max(0.0, float(min_raw))
            except (TypeError, ValueError):
                min_value = DEFAULT_MIN_RISK_PER_TRADE_PERCENT
            max_raw = risk_cfg.get('max_risk_per_trade_pct', DEFAULT_MAX_RISK_PER_TRADE_PERCENT)
            try:
                max_value = float(max_raw)
            except (TypeError, ValueError):
                max_value = DEFAULT_MAX_RISK_PER_TRADE_PERCENT
            max_value = max(min_value, min(DEFAULT_MAX_RISK_PER_TRADE_PERCENT, max_value))
            for key, value in (
                ('min_risk_per_trade_pct', min_value),
                ('max_risk_per_trade_pct', max_value),
            ):
                if risk_cfg.get(key) != value:
                    risk_cfg[key] = value
                    changed = True
            normalized = normalize_risk_percent(risk_cfg)
            if risk_cfg.get('risk_per_trade_pct') != normalized:
                risk_cfg['risk_per_trade_pct'] = normalized
                changed = True

        common_cfg = signal_cfg.setdefault('common_settings', {})
        _normalize_percent_risk_cfg(common_cfg)

        tp_sl_master = bool(common_cfg.get('tp_sl_enabled', True))
        tp_enabled = bool(common_cfg.get('take_profit_enabled', True))
        sl_enabled = bool(common_cfg.get('stop_loss_enabled', True))
        if not tp_sl_master and (tp_enabled or sl_enabled):
            common_cfg['take_profit_enabled'] = False
            common_cfg['stop_loss_enabled'] = False
            tp_enabled = False
            sl_enabled = False
            changed = True
        desired_master = tp_enabled or sl_enabled
        if tp_sl_master != desired_master:
            common_cfg['tp_sl_enabled'] = desired_master
            changed = True

        # Hurst filter was removed from core strategy path.
        for removed_key in ('hurst_entry_enabled', 'hurst_exit_enabled', 'hurst_threshold'):
            if removed_key in common_cfg:
                common_cfg.pop(removed_key, None)
                changed = True

        daily_limit_pct = float(common_cfg.get('daily_loss_limit_pct', 5.0) or 0.0)
        if daily_limit_pct <= 0:
            common_cfg['daily_loss_limit_pct'] = 5.0
            changed = True

        scanner_max_rise = float(common_cfg.get('scanner_max_rise_pct', 8.0) or 8.0)
        if scanner_max_rise <= 0:
            common_cfg['scanner_max_rise_pct'] = 8.0
            changed = True
        scanner_min_rise = float(common_cfg.get('scanner_min_rise_pct', 0.5) or 0.0)
        if scanner_min_rise < 0:
            common_cfg['scanner_min_rise_pct'] = 0.5
            changed = True
        elif scanner_min_rise >= scanner_max_rise:
            common_cfg['scanner_min_rise_pct'] = max(0.1, scanner_max_rise * 0.25)
            changed = True
        try:
            scanner_scan_interval = float(common_cfg.get('scanner_scan_interval_seconds', 60.0) or 60.0)
        except (TypeError, ValueError):
            scanner_scan_interval = 60.0
        scanner_scan_interval = max(10.0, min(300.0, scanner_scan_interval))
        if common_cfg.get('scanner_scan_interval_seconds') != scanner_scan_interval:
            common_cfg['scanner_scan_interval_seconds'] = scanner_scan_interval
            changed = True

        coin_selector_cfg = signal_cfg.setdefault('coin_selector', {})
        if not isinstance(coin_selector_cfg, dict):
            coin_selector_cfg = default_coin_selector_config()
            signal_cfg['coin_selector'] = coin_selector_cfg
            changed = True
        coin_selector_defaults = default_coin_selector_config()
        for key, value in coin_selector_defaults.items():
            if key not in coin_selector_cfg:
                coin_selector_cfg[key] = value
                changed = True
        for list_key in ('excluded_sectors', 'blacklist', 'custom_symbols'):
            if not isinstance(coin_selector_cfg.get(list_key), list):
                coin_selector_cfg[list_key] = list(coin_selector_defaults[list_key])
                changed = True
        normalized_custom_symbols = normalize_coin_selector_custom_symbols(coin_selector_cfg.get('custom_symbols'))
        if coin_selector_cfg.get('custom_symbols') != normalized_custom_symbols:
            coin_selector_cfg['custom_symbols'] = normalized_custom_symbols
            changed = True
        for bool_key in ('enabled', 'auto_apply_watchlist', 'custom_universe_enabled', 'custom_relax_discovery', 'candidate_cooldown_enabled', 'selection_quality_enabled', 'include_tradifi_universe'):
            value = coin_selector_cfg.get(bool_key, coin_selector_defaults.get(bool_key, False))
            if isinstance(value, bool):
                normalized_bool = value
            else:
                text = str(value).strip().lower()
                if text in {'1', 'true', 'yes', 'on', 'enable', 'enabled'}:
                    normalized_bool = True
                elif text in {'0', 'false', 'no', 'off', 'disable', 'disabled'}:
                    normalized_bool = False
                else:
                    normalized_bool = bool(coin_selector_defaults.get(bool_key, False))
            if coin_selector_cfg.get(bool_key) != normalized_bool:
                coin_selector_cfg[bool_key] = normalized_bool
                changed = True
        if not isinstance(coin_selector_cfg.get('sector_overrides'), dict):
            coin_selector_cfg['sector_overrides'] = {}
            changed = True
        numeric_coin_selector_defaults = {
            'min_quote_volume_usdt': 100_000_000.0,
            'ideal_quote_volume_usdt': 1_000_000_000.0,
            'min_trade_count': 20_000,
            'ideal_trade_count': 500_000,
            'max_spread_pct': 0.08,
            'max_abs_price_change_pct': 18.0,
            'analysis_limit': 20,
            'top_n': 10,
            'min_final_score': 55.0,
            'refresh_interval_seconds': 300,
            'candidate_cooldown_misses': 3,
            'candidate_cooldown_seconds': 1800,
            'selection_return_lookback_bars': 96,
            'selection_target_realized_vol_pct': 0.65,
            'selection_max_drawdown_pct': 18.0,
            'selection_max_rebound_pct': 18.0,
            'selection_max_dispersion_pct': 9.0,
        }
        for key, default in numeric_coin_selector_defaults.items():
            try:
                value = float(coin_selector_cfg.get(key, default))
            except (TypeError, ValueError):
                value = float(default)
            if value <= 0:
                value = float(default)
            if key in {'analysis_limit', 'top_n', 'min_trade_count', 'ideal_trade_count', 'candidate_cooldown_misses'}:
                value = int(value)
            if coin_selector_cfg.get(key) != value:
                coin_selector_cfg[key] = value
                changed = True
        hard_min_quote_volume = max(
            COIN_SELECTOR_HARD_MIN_QUOTE_VOLUME_USDT,
            float(
                coin_selector_cfg.get(
                    'min_quote_volume_usdt',
                    COIN_SELECTOR_HARD_MIN_QUOTE_VOLUME_USDT,
                )
                or COIN_SELECTOR_HARD_MIN_QUOTE_VOLUME_USDT
            ),
        )
        if (
            float(coin_selector_cfg.get('min_quote_volume_usdt', 0) or 0)
            != hard_min_quote_volume
        ):
            coin_selector_cfg['min_quote_volume_usdt'] = (
                hard_min_quote_volume
            )
            changed = True
        if float(coin_selector_cfg.get('ideal_quote_volume_usdt', 0) or 0) < float(coin_selector_cfg.get('min_quote_volume_usdt', 0) or 0):
            coin_selector_cfg['ideal_quote_volume_usdt'] = max(
                float(coin_selector_cfg.get('min_quote_volume_usdt', 100_000_000.0) or 100_000_000.0) * 5.0,
                1_000_000_000.0
            )
            changed = True

        micro_auto_cfg = signal_cfg.setdefault('micro_auto', {})
        if not isinstance(micro_auto_cfg, dict):
            micro_auto_cfg = default_micro_auto_config()
            signal_cfg['micro_auto'] = micro_auto_cfg
            changed = True
        normalized_micro = normalize_micro_auto_config(micro_auto_cfg)
        for key, value in normalized_micro.items():
            if micro_auto_cfg.get(key) != value:
                micro_auto_cfg[key] = value
                changed = True

        upbit_cfg = self.config.setdefault('upbit', {})
        upbit_watchlist = upbit_cfg.get('watchlist')
        if not isinstance(upbit_watchlist, list) or not upbit_watchlist:
            upbit_cfg['watchlist'] = ['BTC/KRW']
            changed = True

        exchange_watchlists = self.config.get('exchange_watchlists')
        if not isinstance(exchange_watchlists, dict):
            exchange_watchlists = {}
            self.config['exchange_watchlists'] = exchange_watchlists
            changed = True
        legacy_signal_watchlist = signal_cfg.get('watchlist')
        legacy_upbit_watchlist = upbit_cfg.get('watchlist')
        for mode, default_watchlist in EXCHANGE_MODE_DEFAULT_WATCHLISTS.items():
            current_watchlist = exchange_watchlists.get(mode)
            if isinstance(current_watchlist, list) and current_watchlist:
                continue
            if mode == BINANCE_MAINNET and isinstance(legacy_signal_watchlist, list) and legacy_signal_watchlist:
                exchange_watchlists[mode] = list(legacy_signal_watchlist)
            elif mode == UPBIT_MODE and isinstance(legacy_upbit_watchlist, list) and legacy_upbit_watchlist:
                exchange_watchlists[mode] = list(legacy_upbit_watchlist)
            else:
                exchange_watchlists[mode] = list(default_watchlist)
            changed = True

        upbit_common = upbit_cfg.setdefault('common_settings', {})
        upbit_common_defaults = {
            'leverage': 1,
            'timeframe': '1h',
            'entry_timeframe': '1h',
            'exit_timeframe': '1h',
            'risk_per_trade_pct': DEFAULT_RISK_PER_TRADE_PERCENT,
            'min_risk_per_trade_pct': DEFAULT_MIN_RISK_PER_TRADE_PERCENT,
            'max_risk_per_trade_pct': DEFAULT_MAX_RISK_PER_TRADE_PERCENT,
            'daily_loss_limit': 50000.0,
            'daily_loss_limit_pct': 5.0,
            'min_order_krw': 5000.0,
            'scanner_enabled': False,
            'scanner_timeframe': '15m',
            'scanner_exit_timeframe': '1h',
            'scanner_min_rise_pct': 0.5,
            'scanner_max_rise_pct': 8.0,
            'r2_entry_enabled': False,
            'r2_exit_enabled': False,
            'r2_threshold': 0.25,
            'chop_entry_enabled': False,
            'chop_exit_enabled': False,
            'chop_threshold': 50.0,
            'cc_exit_enabled': False,
            'cc_threshold': 0.70,
            'cc_length': 14
        }
        for key, value in upbit_common_defaults.items():
            if key not in upbit_common:
                upbit_common[key] = value
                changed = True
        _normalize_percent_risk_cfg(upbit_common)
        for legacy_section in ('dual_thrust_engine', 'dual_mode_engine'):
            _normalize_percent_risk_cfg(self.config.setdefault(legacy_section, {}))
        if float(upbit_common.get('min_order_krw', 5000.0) or 0.0) < 5000.0:
            upbit_common['min_order_krw'] = 5000.0
            changed = True
        if int(upbit_common.get('leverage', 1) or 1) != 1:
            upbit_common['leverage'] = 1
            changed = True

        upbit_strategy = upbit_cfg.setdefault('strategy_params', {})
        if str(upbit_strategy.get('active_strategy', 'utbot')).lower() != 'utbot':
            upbit_strategy['active_strategy'] = 'utbot'
            changed = True
        if str(upbit_strategy.get('entry_mode', 'position')).lower() != 'position':
            upbit_strategy['entry_mode'] = 'position'
            changed = True
        upbit_strategy_defaults = {
            'UTBot': {
                'key_value': 1.0,
                'atr_period': 10,
                'use_heikin_ashi': False,
                'rsi_momentum_filter_enabled': False
            },
            'kalman_filter': {
                'entry_enabled': False,
                'exit_enabled': False,
                'observation_covariance': 0.1,
                'transition_covariance': 0.05
            }
        }
        for key, default_val in upbit_strategy_defaults.items():
            current_val = upbit_strategy.get(key)
            if not isinstance(current_val, dict):
                upbit_strategy[key] = dict(default_val)
                changed = True
                continue
            for sub_key, sub_val in default_val.items():
                if sub_key not in current_val:
                    current_val[sub_key] = sub_val
                    changed = True

        if changed:
            self.save_config_sync()

        reporting_cfg = self.config.setdefault('telegram', {}).setdefault('reporting', {})
        reporting_defaults = {
            'event_alerts_only': True,
            'periodic_reports_enabled': False,
            'startup_notice_enabled': False,
            'startup_keyboard_enabled': False,
            'hourly_report_enabled': False,
            'stateful_diag_enabled': False,
            'monthly_trade_report_enabled': True,
            'monthly_trade_report_timezone': 'Asia/Seoul',
            'monthly_trade_report_hour': 9,
            'alt_trend_alert_enabled': False
        }
        for key, default_value in reporting_defaults.items():
            if key not in reporting_cfg:
                reporting_cfg[key] = default_value
                changed = True
        normalized_timeframes = normalize_alt_trend_timeframes(
            reporting_cfg.get('alt_trend_alert_timeframes', ['1d'])
        ) or ['1d']
        if reporting_cfg.get('alt_trend_alert_timeframes') != normalized_timeframes:
            reporting_cfg['alt_trend_alert_timeframes'] = normalized_timeframes
            changed = True

        if str(reporting_cfg.get('alt_trend_alert_scope', 'binance_futures_all')).strip().lower() != 'binance_futures_all':
            reporting_cfg['alt_trend_alert_scope'] = 'binance_futures_all'
            changed = True
        if str(reporting_cfg.get('alt_trend_alert_stage_mode', 'setup_and_confirm')).strip().lower() != 'setup_and_confirm':
            reporting_cfg['alt_trend_alert_stage_mode'] = 'setup_and_confirm'
            changed = True
        if str(reporting_cfg.get('alt_trend_alert_profile', 'conservative')).strip().lower() != 'conservative':
            reporting_cfg['alt_trend_alert_profile'] = 'conservative'
            changed = True
        if str(reporting_cfg.get('alt_trend_alert_oi_cvd_mode', 'required')).strip().lower() != 'required':
            reporting_cfg['alt_trend_alert_oi_cvd_mode'] = 'required'
            changed = True

        if changed:
            self.save_config_sync()

    def create_default_config(self):
        self.config = {
            "api": {
                "use_testnet": True,
                "exchange_mode": BINANCE_TESTNET,
                "mainnet": {"api_key": "", "secret_key": ""},
                "testnet": {"api_key": "", "secret_key": ""},
                "upbit": {"api_key": "", "secret_key": ""}
            },
            "telegram": {
                "token": "",
                "chat_id": "",
                "reporting": {
                    "event_alerts_only": True,
                    "periodic_reports_enabled": False,
                    "startup_notice_enabled": False,
                    "startup_keyboard_enabled": False,
                    "hourly_report_enabled": False,
                    "stateful_diag_enabled": False,
                    "monthly_trade_report_enabled": True,
                    "monthly_trade_report_timezone": "Asia/Seoul",
                    "monthly_trade_report_hour": 9,
                    "alt_trend_alert_enabled": False,
                    "alt_trend_alert_timeframes": ["1d"],
                    "alt_trend_alert_scope": "binance_futures_all",
                    "alt_trend_alert_stage_mode": "setup_and_confirm",
                    "alt_trend_alert_profile": "conservative",
                    "alt_trend_alert_oi_cvd_mode": "required"
                }
            },
            "system_settings": {
                "active_engine": CORE_ENGINE,
                "trade_direction": "both",
                "show_dashboard": True,
                "monitoring_interval_seconds": 3
            },
            "exchange_watchlists": {
                BINANCE_TESTNET: list(EXCHANGE_MODE_DEFAULT_WATCHLISTS[BINANCE_TESTNET]),
                BINANCE_MAINNET: list(EXCHANGE_MODE_DEFAULT_WATCHLISTS[BINANCE_MAINNET]),
                UPBIT_MODE: list(EXCHANGE_MODE_DEFAULT_WATCHLISTS[UPBIT_MODE]),
            },
            "signal_engine": {
                "watchlist": ["BTC/USDT"],
                "common_settings": {
                    "leverage": 10, "timeframe": "15m",
                    "entry_timeframe": "15m",
                    "exit_timeframe": "15m",
                    "risk_per_trade_pct": DEFAULT_RISK_PER_TRADE_PERCENT,
                    "min_risk_per_trade_pct": DEFAULT_MIN_RISK_PER_TRADE_PERCENT,
                    "max_risk_per_trade_pct": DEFAULT_MAX_RISK_PER_TRADE_PERCENT,
                    "target_roe_pct": 20.0, "stop_loss_pct": 10.0,
                    "daily_loss_limit": 5000.0,
                    "daily_loss_limit_pct": 5.0,
                    "tp_sl_enabled": True,
                    "take_profit_enabled": True,
                    "stop_loss_enabled": True,
                    "scanner_enabled": False,
                    "scanner_timeframe": "15m",
                    "scanner_exit_timeframe": "1h",
                    "scanner_scan_interval_seconds": 60,
                    "scanner_min_rise_pct": 0.5,
                    "scanner_max_rise_pct": 8.0,
                    "r2_entry_enabled": True,
                    "r2_exit_enabled": True,
                    "chop_entry_enabled": True,
                    "chop_exit_enabled": True
                },
                "coin_selector": default_coin_selector_config(),
                "strategy_params": {
                    "active_strategy": "utbot",
                    "entry_mode": "cross",
                    "ut_entry_timing_mode": "next_candle",
                    "Triple_SMA": {"fast_sma": 2, "slow_sma": 10},
                    "HMA": {"fast_period": 9, "slow_period": 21},
                    "UTBot": {
                        "key_value": 1.0,
                        "atr_period": 10,
                        "use_heikin_ashi": False,
                        "rsi_momentum_filter_enabled": False,
                        "filter_pack": build_default_utbot_filter_pack()
                    },
                    "UTBotFilteredBreakoutV1": build_default_utbot_filtered_breakout_config(),
                    "UTSMC": {
                        "internal_length": 5,
                        "swing_length": 50,
                        "use_confluence_filter": False,
                        "exit_candidate2_enabled": False,
                        "candidate_filter": {
                            "mode": "off",
                            "apply_to_persistent": True,
                            "c1_release_window": 3,
                            "c2_breakout_window": 2
                        }
                    },
                    "RSIBB": {
                        "rsi_length": 6,
                        "bb_length": 200,
                        "bb_mult": 2.0,
                        "rsibb_enabled": False,
                        "rsibb_paper_only": True,
                        "rsibb_regime_guard_enabled": True
                    },
                    "RSIMomentumTrend": {
                        "rsi_length": 14,
                        "positive_above": 65,
                        "negative_below": 32,
                        "ema_period": 5
                    },
                    "Cameron": {
                        "rsi_period": 14,
                        "rsi_oversold": 30,
                        "rsi_overbought": 70,
                        "bollinger_length": 20,
                        "bollinger_std": 2.0,
                        "macd_fast": 12,
                        "macd_slow": 26,
                        "macd_signal": 9,
                        "extreme_lookback": 60,
                        "macd_confirm_lookback": 3,
                        "band_buffer_pct": 0.001,
                        "risk_reward_ratio": 2.0
                    },
                    "kalman_filter": {"entry_enabled": False, "exit_enabled": False, "observation_covariance": 0.1, "transition_covariance": 0.05}
                }
            },
            "upbit": {
                "watchlist": ["BTC/KRW"],
                "common_settings": {
                    "leverage": 1,
                    "timeframe": "1h",
                    "entry_timeframe": "1h",
                    "exit_timeframe": "1h",
                    "risk_per_trade_pct": DEFAULT_RISK_PER_TRADE_PERCENT,
                    "min_risk_per_trade_pct": DEFAULT_MIN_RISK_PER_TRADE_PERCENT,
                    "max_risk_per_trade_pct": DEFAULT_MAX_RISK_PER_TRADE_PERCENT,
                    "daily_loss_limit": 50000.0,
                    "daily_loss_limit_pct": 5.0,
                    "min_order_krw": 5000.0,
                    "scanner_enabled": False,
                    "scanner_timeframe": "15m",
                    "scanner_exit_timeframe": "1h",
                    "scanner_min_rise_pct": 0.5,
                    "scanner_max_rise_pct": 8.0,
                    "r2_entry_enabled": False,
                    "r2_exit_enabled": False,
                    "r2_threshold": 0.25,
                    "chop_entry_enabled": False,
                    "chop_exit_enabled": False,
                    "chop_threshold": 50.0,
                    "cc_exit_enabled": False,
                    "cc_threshold": 0.70,
                    "cc_length": 14
                },
                "strategy_params": {
                    "active_strategy": "utbot",
                    "entry_mode": "position",
                    "UTBot": {
                        "key_value": 1.0,
                        "atr_period": 10,
                        "use_heikin_ashi": False,
                        "rsi_momentum_filter_enabled": False
                    },
                    "kalman_filter": {
                        "entry_enabled": False,
                        "exit_enabled": False,
                        "observation_covariance": 0.1,
                        "transition_covariance": 0.05
                    }
                }
            },
            "shannon_engine": {
                "target_symbol": "BTC/USDT", "leverage": 5, "daily_loss_limit": 5000.0,
                "asset_allocation": {"target_ratio": 0.5, "allowed_deviation_pct": 2.0},
                "grid_trading": {"enabled": True, "grid_levels": 5, "grid_step_pct": 0.5, "order_size_usdt": 20},
                "risk_monitor": {"max_mmr_alert_pct": 25.0}
            },
            "dual_thrust_engine": {
                "target_symbol": "BTC/USDT",
                "leverage": 5,
                "daily_loss_limit": 5000,
                "n_days": 4,
                "k1": 0.5,
                "k2": 0.5,
                "risk_per_trade_pct": DEFAULT_RISK_PER_TRADE_PERCENT,
                "min_risk_per_trade_pct": DEFAULT_MIN_RISK_PER_TRADE_PERCENT,
                "max_risk_per_trade_pct": DEFAULT_MAX_RISK_PER_TRADE_PERCENT
            },
            "dual_mode_engine": {
                "target_symbol": "BTC/USDT",
                "leverage": 5,
                "mode": "standard",
                "risk_per_trade_pct": DEFAULT_RISK_PER_TRADE_PERCENT,
                "min_risk_per_trade_pct": DEFAULT_MIN_RISK_PER_TRADE_PERCENT,
                "max_risk_per_trade_pct": DEFAULT_MAX_RISK_PER_TRADE_PERCENT,
                "scalping_tf": "5m",
                "standard_tf": "4h"
            },
            "logging": {"db_path": "bot_database.db"}
        }
        self.save_config_sync()

    def save_config_sync(self):
        try:
            atomic_write_json(self.config_file, self.config, indent=4, ensure_ascii=False)
            return True
        except Exception as e:
            logger.error(f"Config save error: {e}")
            return False

    async def update_value(self, path, value):
        async with self.lock:
            ptr = self.config
            for key in path[:-1]:
                if key not in ptr:
                    ptr[key] = {}
                ptr = ptr[key]
            ptr[path[-1]] = value
            self.save_config_sync()

    def get(self, key, default=None):
        return self.config.get(key, default)

    def get_chat_id(self):
        """chat_id瑜??뺤닔濡??덉쟾?섍쾶 諛섑솚"""
        cid = self.config.get('telegram', {}).get('chat_id', '')
        try:
            return int(cid) if cid else 0
        except (ValueError, TypeError):
            logger.error(f"Invalid chat_id: {cid}")
            return 0

__all__ = (
    'TradingConfig',
)
