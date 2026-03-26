# emas_improved.py
# [?쒖닔 ?대쭅 踰꾩쟾]
# Version: 2025-12-25-Recovery (Emergency Access)
# 1. WebSocket ?쒓굅 ???덉젙?곸씤 ?대쭅 諛⑹떇?쇰줈 ?꾪솚
# 2. Signal/Shannon ?붿쭊 紐⑤몢 ?대쭅 吏??
# 3. 紐⑤뱺 Critical ?댁뒋 ?섏젙: chat_id ??? ?덉쇅 濡쒓퉭, async ?몃뱾??
# 4. 誘멸뎄??湲곕뒫 異붽?: Grid Trading, Daily Loss Limit, Hourly Report, MMR Alert

import logging
import threading
import sqlite3
import os
import json
import asyncio
import time
import sys
import traceback
import re
import atexit
import signal
import faulthandler
import ccxt
import pandas as pd
import pandas_ta as ta
import numpy as np
from pykalman import KalmanFilter as PyKalmanFilter
from datetime import datetime, timezone, timedelta
from collections import deque
try:
    from dual_mode_fractal_strategy import DualModeFractalStrategy
    DUAL_MODE_AVAILABLE = True
except ImportError:
    DUAL_MODE_AVAILABLE = False
    logging.warning("?좑툘 dual_mode_fractal_strategy.py ?뚯씪???놁뒿?덈떎. ?대떦 ?꾨왂???ъ슜?섎젮硫??뚯씪??蹂듦뎄?섏꽭??")

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.error import BadRequest, TimedOut, RetryAfter
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, 
    MessageHandler, filters, ConversationHandler, CallbackQueryHandler
)

# ---------------------------------------------------------
# 0. 濡쒓퉭 諛??좏떥由ы떚
# ---------------------------------------------------------
log_buffer = deque(maxlen=50)

class BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            log_buffer.append(self.format(record))
        except Exception:
            pass

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(), 
        BufferHandler(),
        logging.FileHandler('trading_bot.log', encoding='utf-8')  # 濡쒓렇 ?뚯씪 ???
    ]
)
logger = logging.getLogger(__name__)
faulthandler.enable()
CORE_ENGINE = 'signal'
MA_STRATEGIES = {'sma', 'hma'}
PATTERN_STRATEGIES = {'cameron', 'utbot', 'rsibb', 'utrsibb', 'utrsi', 'utbb'}
CORE_STRATEGIES = MA_STRATEGIES | PATTERN_STRATEGIES
UT_HYBRID_STRATEGIES = {'utrsibb', 'utrsi', 'utbb'}
STATEFUL_UT_STRATEGIES = {'utbot'} | UT_HYBRID_STRATEGIES
STRATEGY_DISPLAY_NAMES = {
    'sma': 'SMA',
    'hma': 'HMA',
    'cameron': 'CAMERON',
    'utbot': 'UTBOT',
    'rsibb': 'RSIBB',
    'utrsibb': 'UTRSIBB',
    'utrsi': 'UTRSI',
    'utbb': 'UTBB'
}
UT_HYBRID_TIMING_LABELS = {
    'utrsibb': 'RSI+BB',
    'utrsi': 'RSI',
    'utbb': 'BB'
}
BINANCE_TESTNET = 'binance_testnet'
BINANCE_MAINNET = 'binance_mainnet'
UPBIT_MODE = 'upbit'
SUPPORTED_EXCHANGE_MODES = {BINANCE_TESTNET, BINANCE_MAINNET, UPBIT_MODE}

# ??뷀삎 ?곹깭
SELECT, INPUT, SYMBOL_INPUT, DIRECTION_SELECT, ENGINE_SELECT = range(5)

# ---------------------------------------------------------
# 1. ?ㅼ젙 諛?DB 愿由?
# ---------------------------------------------------------
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
                    'risk_per_trade_pct': 10.0,
                    'max_risk_per_trade_pct': 100.0,
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
                'strategy_params': {
                    'active_strategy': 'sma',
                    'entry_mode': 'cross',
                    'Triple_SMA': {'fast_sma': 2, 'slow_sma': 10},
                    'HMA': {'fast_period': 9, 'slow_period': 21},
                    'UTBot': {
                        'key_value': 1.0,
                        'atr_period': 10,
                        'use_heikin_ashi': False
                    },
                    'RSIBB': {
                        'rsi_length': 6,
                        'bb_length': 200,
                        'bb_mult': 2.0
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
                    'risk_per_trade_pct': 10.0,
                    'max_risk_per_trade_pct': 100.0,
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
                'risk_per_trade_pct': 50.0
            },
            'dual_mode_engine': {
                'target_symbol': 'BTC/USDT',
                'leverage': 5,
                'mode': 'standard',  # 'scalping' or 'standard'
                'risk_per_trade_pct': 10.0,
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
                'use_heikin_ashi': False
            },
            'RSIBB': {
                'rsi_length': 6,
                'bb_length': 200,
                'bb_mult': 2.0
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
        active_strategy = str(strategy_params.get('active_strategy', 'sma')).lower()
        if active_strategy not in CORE_STRATEGIES:
            strategy_params['active_strategy'] = 'sma'
            changed = True

        entry_mode = str(strategy_params.get('entry_mode', 'cross')).lower()
        if entry_mode not in {'cross', 'position'}:
            strategy_params['entry_mode'] = 'cross'
            changed = True

        common_cfg = signal_cfg.setdefault('common_settings', {})
        max_risk_pct = float(common_cfg.get('max_risk_per_trade_pct', 100.0) or 100.0)
        if max_risk_pct < 1.0:
            max_risk_pct = 1.0
            common_cfg['max_risk_per_trade_pct'] = max_risk_pct
            changed = True
        if max_risk_pct > 100.0:
            max_risk_pct = 100.0
            common_cfg['max_risk_per_trade_pct'] = max_risk_pct
            changed = True
        # Keep global cap at 100% so Telegram setup can always allow up to 100.
        if max_risk_pct < 100.0:
            max_risk_pct = 100.0
            common_cfg['max_risk_per_trade_pct'] = max_risk_pct
            changed = True

        risk_pct = float(common_cfg.get('risk_per_trade_pct', 10.0) or 10.0)
        if risk_pct > max_risk_pct:
            common_cfg['risk_per_trade_pct'] = max_risk_pct
            changed = True
        elif risk_pct < 1.0:
            common_cfg['risk_per_trade_pct'] = 1.0
            changed = True

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

        upbit_cfg = self.config.setdefault('upbit', {})
        upbit_watchlist = upbit_cfg.get('watchlist')
        if not isinstance(upbit_watchlist, list) or not upbit_watchlist:
            upbit_cfg['watchlist'] = ['BTC/KRW']
            changed = True

        upbit_common = upbit_cfg.setdefault('common_settings', {})
        upbit_common_defaults = {
            'leverage': 1,
            'timeframe': '1h',
            'entry_timeframe': '1h',
            'exit_timeframe': '1h',
            'risk_per_trade_pct': 10.0,
            'max_risk_per_trade_pct': 100.0,
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
        if float(upbit_common.get('risk_per_trade_pct', 10.0) or 10.0) < 1.0:
            upbit_common['risk_per_trade_pct'] = 1.0
            changed = True
        if float(upbit_common.get('max_risk_per_trade_pct', 100.0) or 100.0) < 1.0:
            upbit_common['max_risk_per_trade_pct'] = 100.0
            changed = True
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
                'use_heikin_ashi': False
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

    def create_default_config(self):
        self.config = {
            "api": {
                "use_testnet": True,
                "exchange_mode": BINANCE_TESTNET,
                "mainnet": {"api_key": "", "secret_key": ""},
                "testnet": {"api_key": "", "secret_key": ""},
                "upbit": {"api_key": "", "secret_key": ""}
            },
            "telegram": {"token": "", "chat_id": ""},
            "system_settings": {
                "active_engine": CORE_ENGINE,
                "trade_direction": "both",
                "show_dashboard": True,
                "monitoring_interval_seconds": 3
            },
            "signal_engine": {
                "watchlist": ["BTC/USDT"],
                "common_settings": {
                    "leverage": 10, "timeframe": "15m",
                    "entry_timeframe": "15m",
                    "exit_timeframe": "15m",
                    "risk_per_trade_pct": 10.0,
                    "max_risk_per_trade_pct": 100.0,
                    "target_roe_pct": 20.0, "stop_loss_pct": 10.0,
                    "daily_loss_limit": 5000.0,
                    "daily_loss_limit_pct": 5.0,
                    "tp_sl_enabled": True,
                    "take_profit_enabled": True,
                    "stop_loss_enabled": True,
                    "scanner_enabled": False,
                    "scanner_timeframe": "15m",
                    "scanner_exit_timeframe": "1h",
                    "scanner_min_rise_pct": 0.5,
                    "scanner_max_rise_pct": 8.0,
                    "r2_entry_enabled": True,
                    "r2_exit_enabled": True,
                    "chop_entry_enabled": True,
                    "chop_exit_enabled": True
                },
                "strategy_params": {
                    "active_strategy": "sma",
                    "entry_mode": "cross",
                    "Triple_SMA": {"fast_sma": 2, "slow_sma": 10},
                    "HMA": {"fast_period": 9, "slow_period": 21},
                    "UTBot": {
                        "key_value": 1.0,
                        "atr_period": 10,
                        "use_heikin_ashi": False
                    },
                    "RSIBB": {
                        "rsi_length": 6,
                        "bb_length": 200,
                        "bb_mult": 2.0
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
                    "risk_per_trade_pct": 10.0,
                    "max_risk_per_trade_pct": 100.0,
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
                        "use_heikin_ashi": False
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
            "logging": {"db_path": "bot_database.db"}
        }
        self.save_config_sync()

    def save_config_sync(self):
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
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

class DBManager:
    def __init__(self, db_path='bot_database.db'):
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.lock = threading.Lock()
        self._init_tables()

    def _init_tables(self):
        with self.lock:
            self.conn.execute("""CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY, symbol TEXT, side TEXT, 
                entry_price REAL, exit_price REAL, quantity REAL, 
                pnl_usdt REAL, pnl_pct REAL, 
                entry_time TEXT, exit_time TEXT, exit_reason TEXT
            )""")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS shannon_log (
                id INTEGER PRIMARY KEY, timestamp TEXT, 
                total_equity REAL, action TEXT, 
                coin_price REAL, coin_amt REAL, usdt_amt REAL
            )""")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS grid_orders (
                id INTEGER PRIMARY KEY, symbol TEXT, side TEXT,
                price REAL, quantity REAL, order_id TEXT,
                status TEXT, created_at TEXT
            )""")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS status_history (
                id INTEGER PRIMARY KEY,
                created_at TEXT,
                snapshot_key TEXT,
                snapshot_text TEXT
            )""")
            self.conn.commit()

    def log_shannon(self, equity, action, price, coin, usdt):
        with self.lock:
            self.conn.execute(
                "INSERT INTO shannon_log (timestamp, total_equity, action, coin_price, coin_amt, usdt_amt) VALUES (?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), equity, action, price, coin, usdt)
            )
            self.conn.commit()

    def log_trade_entry(self, symbol, side, price, quantity=0):
        with self.lock:
            self.conn.execute(
                "INSERT INTO trades (symbol, side, entry_price, quantity, entry_time) VALUES (?,?,?,?,?)", 
                (symbol, side, price, quantity, datetime.now(timezone.utc).isoformat())
            )
            self.conn.commit()

    def log_trade_close(self, symbol, pnl, pnl_pct, exit_price, reason):
        with self.lock:
            self.conn.execute(
                """UPDATE trades SET exit_time=?, exit_price=?, pnl_usdt=?, pnl_pct=?, exit_reason=? 
                WHERE symbol=? AND exit_time IS NULL ORDER BY id DESC LIMIT 1""",
                (datetime.now(timezone.utc).isoformat(), exit_price, pnl, pnl_pct, reason, symbol)
            )
            self.conn.commit()

    def get_daily_stats(self):
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT COUNT(*), SUM(pnl_usdt) FROM trades WHERE exit_time LIKE ?", (f"{today}%",))
            res = cur.fetchone()
            return (res[0] if res and res[0] else 0), (res[1] if res and res[1] else 0.0)

    def get_weekly_stats(self):
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%Y-%m-%d')
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT COUNT(*), SUM(pnl_usdt) FROM trades WHERE exit_time >= ?", (week_ago,))
            res = cur.fetchone()
            return (res[0] if res and res[0] else 0), (res[1] if res and res[1] else 0.0)

    def log_status_snapshot(self, snapshot_key, snapshot_text, keep_rows=200):
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT snapshot_key FROM status_history ORDER BY id DESC LIMIT 1")
            latest = cur.fetchone()
            if latest and latest[0] == snapshot_key:
                return False

            cur.execute(
                "INSERT INTO status_history (created_at, snapshot_key, snapshot_text) VALUES (?,?,?)",
                (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), snapshot_key, snapshot_text)
            )
            keep_rows = max(1, int(keep_rows))
            self.conn.execute(
                f"DELETE FROM status_history WHERE id NOT IN (SELECT id FROM status_history ORDER BY id DESC LIMIT {keep_rows})"
            )
            self.conn.commit()
            return True

    def get_recent_status_history(self, limit=5, offset=0):
        limit = max(1, int(limit))
        offset = max(0, int(offset))
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT created_at, snapshot_text FROM status_history ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset)
            )
            return cur.fetchall()

# ---------------------------------------------------------
# 2. ?붿쭊 (Signal / Shannon)
# ---------------------------------------------------------
class BaseEngine:
    def __init__(self, controller):
        self.ctrl = controller
        self.cfg = controller.cfg
        self.db = controller.db
        self.exchange = controller.exchange
        self.market_data_exchange = getattr(controller, 'market_data_exchange', self.exchange)
        self.running = False
        self.position_cache = None
        self.position_cache_time = 0
        self.POSITION_CACHE_TTL = 2.0  # 2珥?罹먯떆
        self.all_positions_cache = None
        self.all_positions_cache_time = 0
        self.ALL_POSITIONS_CACHE_TTL = 15.0

    def start(self):
        self.running = True
        logger.info(f"?? {self.__class__.__name__} started")

    def stop(self):
        self.running = False
        logger.info(f"??{self.__class__.__name__} stopped")

    async def on_tick(self, data_type, data):
        pass

    def is_upbit_mode(self):
        return self.ctrl.is_upbit_mode()

    def get_runtime_trade_config(self):
        return self.ctrl.get_active_trade_config()

    def get_runtime_common_settings(self):
        return self.ctrl.get_active_common_settings()

    def get_runtime_strategy_params(self):
        return self.ctrl.get_active_strategy_params()

    def get_runtime_watchlist(self):
        return self.ctrl.get_active_watchlist()

    def get_quote_currency(self):
        return 'KRW' if self.is_upbit_mode() else 'USDT'

    def _to_float_safe(self, value):
        try:
            if value in (None, ''):
                return 0.0
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _get_upbit_min_order_krw(self):
        cfg = self.get_runtime_common_settings()
        return max(5000.0, float(cfg.get('min_order_krw', 5000.0) or 5000.0))

    def _extract_upbit_assets(self, balance_payload):
        assets = {}
        if not isinstance(balance_payload, dict):
            return assets

        info_rows = balance_payload.get('info', [])
        if isinstance(info_rows, list):
            for row in info_rows:
                if not isinstance(row, dict):
                    continue
                code = str(row.get('currency', '')).upper().strip()
                if not code:
                    continue
                free_amt = self._to_float_safe(row.get('balance'))
                locked_amt = self._to_float_safe(row.get('locked'))
                assets[code] = {
                    'currency': code,
                    'free': free_amt,
                    'locked': locked_amt,
                    'total': free_amt + locked_amt,
                    'avg_buy_price': self._to_float_safe(row.get('avg_buy_price')),
                    'unit_currency': str(row.get('unit_currency', '')).upper().strip(),
                }

        if assets:
            return assets

        total_map = balance_payload.get('total', {})
        free_map = balance_payload.get('free', {})
        used_map = balance_payload.get('used', {})
        if isinstance(total_map, dict):
            for code_raw, total_val in total_map.items():
                code = str(code_raw or '').upper().strip()
                if not code:
                    continue
                free_amt = self._to_float_safe(free_map.get(code))
                locked_amt = self._to_float_safe(used_map.get(code))
                total_amt = self._to_float_safe(total_val)
                if total_amt <= 0:
                    total_amt = free_amt + locked_amt
                assets[code] = {
                    'currency': code,
                    'free': free_amt,
                    'locked': locked_amt,
                    'total': total_amt,
                    'avg_buy_price': 0.0,
                    'unit_currency': 'KRW',
                }

        return assets

    def safe_amount(self, symbol, amount):
        try:
            return self.exchange.amount_to_precision(symbol, amount)
        except Exception as e:
            logger.error(f"Amount precision error: {e}")
            return str(round(amount, 6))

    def safe_price(self, symbol, price):
        try:
            return self.exchange.price_to_precision(symbol, price)
        except Exception as e:
            logger.error(f"Price precision error: {e}")
            return str(round(price, 2))

    async def ensure_market_settings(self, symbol, leverage=None):
        """留덉폆 ?ㅼ젙 媛뺤젣 ?곸슜 (寃⑸━ 紐⑤뱶 + ?덈쾭由ъ?)"""
        if self.is_upbit_mode():
            logger.info(f"{symbol} Settings: UPBIT KRW spot / leverage fixed at 1x")
            return

        # 1. Position Mode: One-way (Hedge Mode OFF)
        try:
            await asyncio.to_thread(self.exchange.set_position_mode, hedged=False, symbol=symbol)
        except Exception as e:
            # ?대? ?ㅼ젙?섏뼱 ?덇굅??吏?먰븯吏 ?딅뒗 寃쎌슦 臾댁떆 (濡쒓렇 ?앸왂 媛??
            pass
        
        # 2. Margin Mode: ISOLATED (媛뺤젣)
        try:
            await asyncio.to_thread(self.exchange.set_margin_mode, 'ISOLATED', symbol)
        except Exception as e:
            # ?대? 寃⑸━ 紐⑤뱶?????덉쓬
            pass
        
            # 3. Leverage Setting
        try:
            # ?몄옄濡??꾨떖???덈쾭由ъ?媛 ?놁쑝硫??ㅼ젙?먯꽌 議고쉶
            if leverage is None:
                eng = self.cfg.get('system_settings', {}).get('active_engine', CORE_ENGINE)
                if eng == 'shannon':
                    leverage = self.cfg.get('shannon_engine', {}).get('leverage', 5)
                elif eng == 'dualthrust':
                    leverage = self.cfg.get('dual_thrust_engine', {}).get('leverage', 5)
                elif eng == 'dualmode':
                    leverage = self.cfg.get('dual_mode_engine', {}).get('leverage', 5)
                else:
                    leverage = self.get_runtime_common_settings().get('leverage', 20)
            
            await asyncio.to_thread(self.exchange.set_leverage, leverage, symbol)
            logger.info(f"??{symbol} Settings: ISOLATED / {leverage}x")
        except Exception as e:
            logger.error(f"Leverage setting error: {e}")

    async def get_server_position(self, symbol, use_cache=True):
        """?ъ???議고쉶 (?щ낵蹂?罹먯떆 ?곸슜)"""
        now = time.time()
        
        # [Fix] Lazy Init for Dictionary Cache (Backward Compatibility)
        if not isinstance(self.position_cache, dict):
            self.position_cache = {}

        # Check Cache
        if use_cache:
            cache_entry = self.position_cache.get(symbol)
            if cache_entry:
                cached_pos, cached_ts = cache_entry
                if (now - cached_ts) < self.POSITION_CACHE_TTL:
                    return cached_pos
        
        try:
            if self.is_upbit_mode():
                bal = await asyncio.to_thread(self.exchange.fetch_balance)
                assets = self._extract_upbit_assets(bal)
                base = str(symbol).split('/')[0].upper()
                asset_info = assets.get(base, {})
                contracts = float(asset_info.get('total', 0) or 0)
                if contracts <= 0:
                    self.position_cache[symbol] = (None, now)
                    return None

                entry_price = float(asset_info.get('avg_buy_price') or 0.0)
                current_price = 0.0
                try:
                    ticker = await asyncio.to_thread(self.market_data_exchange.fetch_ticker, symbol)
                    current_price = float(ticker.get('last') or ticker.get('close') or 0.0)
                except Exception as ticker_e:
                    logger.warning(f"Upbit ticker fetch error for {symbol}: {ticker_e}")

                pnl = 0.0
                pnl_pct = 0.0
                if entry_price > 0 and current_price > 0:
                    pnl = (current_price - entry_price) * contracts
                    pnl_pct = ((current_price / entry_price) - 1.0) * 100.0

                found_pos = {
                    'symbol': symbol,
                    'side': 'long',
                    'contracts': contracts,
                    'entryPrice': entry_price,
                    'markPrice': current_price,
                    'unrealizedPnl': pnl,
                    'percentage': pnl_pct
                }
                self.position_cache[symbol] = (found_pos, now)
                return found_pos

            # fetch_positions??紐⑤뱺 ?ъ??섏쓣 媛?몄삱 ?섎룄 ?덇퀬, params濡??뱀젙???섎룄 ?덉쓬
            # exchange.fetch_positions([symbol]) ?ъ슜 沅뚯옣
            positions = await asyncio.to_thread(self.exchange.fetch_positions, [symbol])
            
            # ?щ낵 ?뺢퇋??(BTC/USDT -> BTCUSDT)
            base_symbol = symbol.replace('/', '')
            
            found_pos = None
            for p in positions:
                pos_symbol = p.get('symbol', '')
                # ?ㅼ뼇???뺤떇 留ㅼ묶
                pos_base = pos_symbol.replace('/', '').replace(':USDT', '')
                
                if pos_base == base_symbol or pos_symbol == symbol or pos_symbol == f"{symbol}:USDT":
                    # ?섎웾 0 ?댁긽??寃껊쭔 ?좏슚 ?ъ??섏쑝濡?媛꾩＜? 
                    # fetch_positions??蹂댄넻 ?대젮?덈뒗 寃껊쭔 二쇨굅?? 0??寃껊룄 以????덉쓬.
                    # ?ш린??contracts != 0 泥댄겕
                    if abs(float(p.get('contracts', 0))) > 0:
                        found_pos = p
                        logger.debug(f"Position found: {p['symbol']} contracts={p['contracts']}")
                        break
            
            # Update Cache (Key by Symbol)
            self.position_cache[symbol] = (found_pos, now)
            return found_pos

        except Exception as e:
            logger.error(f"Position fetch error: {e}")
            # ?먮윭 ??罹먯떆媛 ?덉쑝硫?諛섑솚, ?놁쑝硫?None
            cache_entry = self.position_cache.get(symbol)
            if cache_entry:
                return cache_entry[0]
            return None

    async def get_active_position_symbols(self, use_cache=True):
        now = time.time()

        if use_cache and isinstance(self.all_positions_cache, set):
            if (now - self.all_positions_cache_time) < self.ALL_POSITIONS_CACHE_TTL:
                return set(self.all_positions_cache)

        try:
            if self.is_upbit_mode():
                bal = await asyncio.to_thread(self.exchange.fetch_balance)
                assets = self._extract_upbit_assets(bal)
                active_symbols = set()
                min_order_krw = self._get_upbit_min_order_krw()
                for code, asset in assets.items():
                    if code == 'KRW':
                        continue
                    qty = float(asset.get('total', 0) or 0)
                    if qty <= 0:
                        continue
                    est_value = qty * float(asset.get('avg_buy_price', 0) or 0)
                    if est_value > 0 and est_value < min_order_krw:
                        continue
                    active_symbols.add(f"{code}/KRW")
                self.all_positions_cache = set(active_symbols)
                self.all_positions_cache_time = now
                return active_symbols

            positions = await asyncio.to_thread(self.exchange.fetch_positions)
            active_symbols = set()
            for p in positions:
                if abs(float(p.get('contracts', 0) or 0)) > 0:
                    sym = str(p.get('symbol', '')).replace(':USDT', '')
                    if sym:
                        active_symbols.add(sym)
            self.all_positions_cache = set(active_symbols)
            self.all_positions_cache_time = now
            return active_symbols
        except Exception as e:
            logger.warning(f"Active positions fetch error: {e}")
            if isinstance(self.all_positions_cache, set):
                return set(self.all_positions_cache)
            return set()

    async def get_balance_info(self):
        try:
            if self.is_upbit_mode():
                bal = await asyncio.to_thread(self.exchange.fetch_balance)
                assets = self._extract_upbit_assets(bal)
                krw_asset = assets.get('KRW', {})
                total_krw = self._to_float_safe(krw_asset.get('total'))
                free_krw = self._to_float_safe(krw_asset.get('free'))

                total_equity = total_krw
                for code, asset in assets.items():
                    if code == 'KRW':
                        continue
                    qty = self._to_float_safe(asset.get('total'))
                    if qty <= 0:
                        continue
                    try:
                        ticker = await asyncio.to_thread(self.market_data_exchange.fetch_ticker, f"{code}/KRW")
                        last_price = self._to_float_safe(ticker.get('last') or ticker.get('close'))
                        if last_price > 0:
                            total_equity += qty * last_price
                            continue
                    except Exception as ticker_e:
                        logger.debug(f"Upbit balance valuation skipped for {code}/KRW: {ticker_e}")

                    fallback_price = self._to_float_safe(asset.get('avg_buy_price'))
                    if fallback_price > 0:
                        total_equity += qty * fallback_price

                if total_equity <= 0 and free_krw > 0:
                    total_equity = free_krw

                return float(total_equity), float(free_krw), 0.0

            # Binance futures balance schemas vary by account mode (single/multi asset, PM, etc).
            # Parse multiple candidate fields to avoid showing 0 when funds exist.
            try:
                bal = await asyncio.to_thread(self.exchange.fetch_balance, {'type': 'future'})
            except Exception:
                bal = await asyncio.to_thread(self.exchange.fetch_balance)

            info = bal.get('info', {}) if isinstance(bal, dict) else {}
            usdt_bucket = bal.get('USDT', {}) if isinstance(bal, dict) else {}
            total_map = bal.get('total', {}) if isinstance(bal, dict) else {}
            free_map = bal.get('free', {}) if isinstance(bal, dict) else {}

            usdt_asset = {}
            assets = info.get('assets', []) if isinstance(info, dict) else []
            if isinstance(assets, list):
                for asset in assets:
                    if str(asset.get('asset', '')).upper() == 'USDT':
                        usdt_asset = asset
                        break

            def to_float_or_none(v):
                try:
                    if v is None or v == '':
                        return None
                    return float(v)
                except (TypeError, ValueError):
                    return None

            def pick_value(candidates):
                # Prefer positive values first, then first parseable numeric value.
                parsed = [to_float_or_none(v) for v in candidates]
                for v in parsed:
                    if v is not None and v > 0:
                        return v
                for v in parsed:
                    if v is not None:
                        return v
                return 0.0

            total = pick_value([
                info.get('totalMarginBalance'),
                info.get('totalWalletBalance'),
                info.get('totalCrossWalletBalance'),
                usdt_asset.get('marginBalance'),
                usdt_asset.get('walletBalance'),
                usdt_bucket.get('total'),
                total_map.get('USDT'),
                info.get('availableBalance'),
                usdt_asset.get('availableBalance'),
                usdt_bucket.get('free'),
                free_map.get('USDT'),
            ])

            free = pick_value([
                info.get('availableBalance'),
                usdt_asset.get('availableBalance'),
                usdt_bucket.get('free'),
                free_map.get('USDT'),
                usdt_bucket.get('total'),
                total_map.get('USDT'),
            ])

            if total <= 0 and free > 0:
                total = free

            maint_margin = pick_value([
                info.get('totalMaintMargin'),
                usdt_asset.get('maintMargin'),
            ])
            mmr = (maint_margin / total * 100) if total > 0 else 0.0

            return float(total), float(free), float(mmr)
        except Exception as e:
            logger.error(f"Balance fetch error: {e}")
            return 0.0, 0.0, 0.0

    async def check_daily_loss_limit(self):
        """?쇱씪 ?먯떎 ?쒕룄 泥댄겕 (誘몄떎???먯씡 ?ы븿)"""
        _, daily_pnl = self.db.get_daily_stats()
        eng = self.cfg.get('system_settings', {}).get('active_engine', CORE_ENGINE)

        # 誘몄떎???먯씡???ы븿 (硫???щ낵 ?곹깭 ?곗씠??吏??
        unrealized_pnl = 0.0
        open_symbols = []
        status_data = self.ctrl.status_data if isinstance(self.ctrl.status_data, dict) else {}

        status_rows = []
        if status_data.get('symbol') and status_data.get('pos_side') is not None:
            # Legacy single-symbol format
            status_rows = [status_data]
        else:
            status_rows = [v for v in status_data.values() if isinstance(v, dict)]

        active_symbols_on_exchange = set()
        try:
            active_symbols_on_exchange = await self.get_active_position_symbols(use_cache=True)
        except Exception as e:
            logger.warning(f"Daily loss check: fetch_positions failed, using status cache only ({e})")

        for row in status_rows:
            pos_side = str(row.get('pos_side', 'NONE')).upper()
            symbol = row.get('symbol')
            if symbol and pos_side != 'NONE':
                norm_symbol = str(symbol).replace(':USDT', '')
                if active_symbols_on_exchange and norm_symbol not in active_symbols_on_exchange:
                    continue
                unrealized_pnl += float(row.get('pnl_usdt', 0) or 0)
                open_symbols.append(symbol)

        total_daily_pnl = daily_pnl + unrealized_pnl
        total_equity = 0.0
        if status_rows:
            equities = [float(r.get('total_equity', 0) or 0) for r in status_rows]
            total_equity = max(equities) if equities else 0.0
        if total_equity <= 0:
            total_equity, _, _ = await self.get_balance_info()
        
        if eng == 'shannon':
            sh_cfg = self.cfg.get('shannon_engine', {})
            limit_abs = float(sh_cfg.get('daily_loss_limit', 5000) or 5000)
            limit_pct = float(sh_cfg.get('daily_loss_limit_pct', 0) or 0)
        else:
            sig_cfg = self.get_runtime_common_settings()
            limit_abs = float(sig_cfg.get('daily_loss_limit', 5000) or 5000)
            limit_pct = float(sig_cfg.get('daily_loss_limit_pct', 0) or 0)

        # Telegram setup currently exposes absolute daily limit, so prioritize absolute limit.
        if limit_abs > 0:
            effective_limit = limit_abs
        elif limit_pct > 0 and total_equity > 0:
            effective_limit = total_equity * (limit_pct / 100.0)
        else:
            effective_limit = 5000.0

        if total_daily_pnl < -effective_limit:
            logger.warning(
                f"?좑툘 Daily loss limit reached: {total_daily_pnl:.2f} "
                f"(realized: {daily_pnl:.2f}, unrealized: {unrealized_pnl:.2f}) / "
                f"Limit: -{effective_limit:.2f} (abs={limit_abs:.2f}, pct={limit_pct:.2f}%)"
            )
            # ?ъ??섏씠 ?덉쑝硫?泥?궛
            if open_symbols and hasattr(self, 'exit_position'):
                await self.ctrl.notify("⚠️ 일일 손실 한도 도달! 보유 포지션 정리를 시작합니다.")
                for symbol in sorted(set(open_symbols)):
                    try:
                        await self.exit_position(symbol, "DailyLossLimit")
                    except Exception as e:
                        logger.error(f"Daily loss limit forced exit failed for {symbol}: {e}")
            return True
        return False

    async def check_mmr_alert(self, mmr):
        """MMR 寃쎄퀬 泥댄겕 (荑⑤떎???곸슜)"""
        max_mmr = self.cfg.get('shannon_engine', {}).get('risk_monitor', {}).get('max_mmr_alert_pct', 25.0)
        
        # 荑⑤떎?? 5遺??숈븞 以묐났 ?뚮┝ 諛⑹?
        now = time.time()
        if not hasattr(self, '_last_mmr_alert_time'):
            self._last_mmr_alert_time = 0
        
        if mmr >= max_mmr:
            if now - self._last_mmr_alert_time > 300:  # 5遺?
                self._last_mmr_alert_time = now
                await self.ctrl.notify(f"⚠️ **MMR 경고!** 현재 {mmr:.2f}% (한도: {max_mmr}%)")
            return True
        return False


class TemaEngine(BaseEngine):
    def __init__(self, controller):
        super().__init__(controller)
        self.last_candle_time = 0
        self.consecutive_errors = 0
        
        # 湲곕낯 湲곗닠??吏??罹먯떆
        self.ema1 = None
        self.ema2 = None
        self.ema3 = None
    
    def start(self):
        super().start()
        # ?ъ떆?????곹깭 珥덇린?뷀븯??利됱떆 遺꾩꽍 媛?ν븯寃???
        self.last_candle_time = 0
        self.ema1 = None
        self.ema2 = None
        self.ema3 = None
        logger.info(f"?? [TEMA] Engine started")
        
    async def poll_tick(self):
        if not self.running: return
        
        try:
            # 1. ?ㅼ젙 濡쒕뱶 (怨듯넻 ?ㅼ젙 ?ъ슜)
            cfg = self.cfg.get('tema_engine', {})
            common_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
            
            symbol = cfg.get('target_symbol', 'BTC/USDT')
            tf = cfg.get('timeframe', '5m')
            
            # 2. 罹붾뱾 ?곗씠??議고쉶
            ohlcv = await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, tf, limit=100)
            if not ohlcv or len(ohlcv) < 50:
                return
                
            last_closed = ohlcv[-2]
            current_ts = int(last_closed[0])
            current_close = float(last_closed[4])
            
            # 3. ?덈줈??罹붾뱾 留덇컧 ??遺꾩꽍
            if current_ts > self.last_candle_time:
                logger.info(f"?빉截?[TEMA {tf}] {symbol} New Candle: close={current_close}")
                self.last_candle_time = current_ts
                
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                
                # 吏??怨꾩궛
                df = self._calculate_indicators(df, cfg)
                
                # ?좏샇 ?뺤씤
                signal, reason = self._check_entry_conditions(df, cfg)
                
                # ?ъ???議고쉶
                pos = await self.get_server_position(symbol, use_cache=False)
                if pos and abs(float(pos.get('contracts', 0) or 0)) > 0:
                    p_side = str(pos.get('side', '')).lower()
                    if p_side == 'long':
                        pos_side = 'long'
                    elif p_side == 'short':
                        pos_side = 'short'
                    else:
                        pos_side = 'none'
                else:
                    pos_side = 'none'
                
                # 吏꾩엯 媛먯?
                if signal and pos_side == 'none':
                    logger.info(f"?? TEMA Signal Detected: {signal.upper()} ({reason})")
                    current_price = float(ohlcv[-1][4])
                    await self.entry(symbol, signal, current_price, common_cfg)
                    
                # 泥?궛 媛먯? (?ъ??섏씠 ?덉쓣 ?뚮쭔)
                elif pos_side != 'none':
                     exit_signal, exit_reason = self._check_exit_conditions(df, pos_side, cfg)
                     if exit_signal:
                         logger.info(f"?몝 TEMA Exit Signal: {exit_reason}")
                         await self.exit_position(symbol, exit_reason)

        except Exception as e:
            self.consecutive_errors += 1
            if self.consecutive_errors % 10 == 0:
                logger.error(f"TemaEngine poll error: {e}")

    def _calculate_indicators(self, df, cfg):
        try:
            rsi_period = cfg.get('rsi_period', 14)
            tema_period = cfg.get('tema_period', 9)
            bb_window = cfg.get('bollinger_window', 20)
            
            # RSI
            df['rsi'] = ta.rsi(df['close'], length=rsi_period)
            
            # TEMA Calculation
            # TEMA = (3 * EMA1) - (3 * EMA2) + EMA3
            ema1 = ta.ema(df['close'], length=tema_period)
            ema2 = ta.ema(ema1, length=tema_period)
            ema3 = ta.ema(ema2, length=tema_period)
            df['tema'] = (3 * ema1) - (3 * ema2) + ema3
            
            # Bollinger Bands
            bb = ta.bbands(df['close'], length=bb_window, std=2.0)
            # pandas_ta bbands returns multiple columns. We need standard names.
            # Assuming default names: BBL_20_2.0, BBM_20_2.0, BBU_20_2.0
            # We map them to simpler names
            cols = bb.columns
            df['bb_lower'] = bb[cols[0]]
            df['bb_mid'] = bb[cols[1]]
            df['bb_upper'] = bb[cols[2]]
            
            return df
        except Exception as e:
            logger.error(f"Indicator calculation error: {e}")
            return df

    def _check_entry_conditions(self, df, cfg):
        # ?꾨왂: SampleStrategy.py 濡쒖쭅 援ы쁽
        # Long: RSI > 30 & TEMA < BB_Mid & TEMA Rising
        # Short: RSI > 70 & TEMA > BB_Mid & TEMA Falling
        
        try:
            last = df.iloc[-2] # 吏곸쟾 ?뺤젙 遊?
            prev = df.iloc[-3] # 洹???遊?(異붿꽭 ?뺤씤??
            
            # TEMA Rising/Falling check
            tema_rising = last['tema'] > prev['tema']
            tema_falling = last['tema'] < prev['tema']
            
            # Conditions
            # 1. Long
            if (last['rsi'] > 30 and 
                last['tema'] <= last['bb_mid'] and 
                tema_rising):
                return 'long', f"RSI({last['rsi']:.1f})>30 & TEMA<Mid & Rising"
                
            # 2. Short
            if (last['rsi'] > 70 and 
                last['tema'] >= last['bb_mid'] and 
                tema_falling):
                return 'short', f"RSI({last['rsi']:.1f})>70 & TEMA>Mid & Falling"
                
            return None, None
        except Exception:
            return None, None

    def _check_exit_conditions(self, df, pos_side, cfg):
        # ?꾨왂: SampleStrategy.py 濡쒖쭅 援ы쁽
        # Exit Long: RSI > 70 & TEMA > BB_Mid & TEMA Falling (怨쇰ℓ??+ 爰얠엫)
        # Exit Short: RSI < 30 & TEMA < BB_Mid & TEMA Rising (怨쇰ℓ??+ 諛섎벑)
        
        try:
            last = df.iloc[-2]
            prev = df.iloc[-3]
            
            tema_rising = last['tema'] > prev['tema']
            tema_falling = last['tema'] < prev['tema']
            
            if pos_side == 'long':
                if (last['rsi'] > 70 and 
                    last['tema'] > last['bb_mid'] and 
                    tema_falling):
                    return True, f"Long Exit: RSI({last['rsi']:.1f})>70 & TEMA>Mid & Falling"
            
            elif pos_side == 'short':
                if (last['rsi'] < 30 and 
                    last['tema'] < last['bb_mid'] and 
                    tema_rising):
                    return True, f"Short Exit: RSI({last['rsi']:.1f})<30 & TEMA<Mid & Rising"
                    
            return False, None
        except Exception:
            return False, None

    async def entry(self, symbol, side, price, common_cfg):
        try:
            # === [Single Position Enforcement] ===
            try:
                all_positions = await asyncio.to_thread(self.exchange.fetch_positions)
                for p in all_positions:
                    if float(p.get('contracts', 0)) > 0:
                        active_sym = p.get('symbol', '').replace(':USDT', '').replace('/', '')
                        target_sym = symbol.replace(':USDT', '').replace('/', '')
                        
                        if active_sym != target_sym:
                            logger.warning(f"?슟 [Single Limit] Entry blocked: Already holding {p['symbol']}")
                            await self.ctrl.notify(f"⚠️ **진입 차단**: 단일 포지션 제한 (보유 중: {p['symbol']})")
                            return
            except Exception as e:
                logger.error(f"Single position check failed: {e}")
                return

            # 1. ?먯궛 ?뺤씤
            total, free, _ = await self.get_balance_info()
            if total <= 0: return

            # 2. ?ъ옄 鍮꾩쨷 (Risk %) - 怨듯넻 ?ㅼ젙 ?ъ슜
            risk_pct = common_cfg.get('risk_per_trade_pct', 50.0)
            leverage = common_cfg.get('leverage', 5)
            
            # USDT ?ъ엯 湲덉븸 怨꾩궛
            invest_amount = (total * (risk_pct / 100.0)) * leverage
            
            # ?섎웾 怨꾩궛
            quantity = invest_amount / price
            amount_str = self.safe_amount(symbol, quantity)
            price_str = self.safe_price(symbol, price) # Limit 二쇰Ц??(?꾩옱媛)
            
            logger.info(f"?뮥 TEMA Entry: {side.upper()} {symbol} Qty={amount_str} Price={price_str} (Lev {leverage}x)")
            
            # 3. 二쇰Ц ?꾩넚
            # [Enforce] Market Settings
            await self.ensure_market_settings(symbol, leverage=leverage)
            
            params = {'leverage': leverage}
            
            if side == 'long':
                order = await asyncio.to_thread(self.exchange.create_market_buy_order, symbol, float(amount_str), params)
            else:
                order = await asyncio.to_thread(self.exchange.create_market_sell_order, symbol, float(amount_str), params)
            
            await self.ctrl.notify(f"✅ **TEMA 진입**: {symbol} {side.upper()}\n가격: {price}\n수량: {amount_str}")
            
            # 4. TP/SL ?ㅼ젙 (怨듯넻 ?ㅼ젙 ?ъ슜)
            tp_master_enabled = bool(common_cfg.get('tp_sl_enabled', False))
            tp_enabled = tp_master_enabled and bool(common_cfg.get('take_profit_enabled', True))
            sl_enabled = tp_master_enabled and bool(common_cfg.get('stop_loss_enabled', True))
            if tp_enabled or sl_enabled:
                roe_target = common_cfg.get('target_roe_pct', 20.0) / 100.0
                stop_loss = common_cfg.get('stop_loss_pct', 10.0) / 100.0
                
                # 二쇰Ц 泥닿껐媛 湲곗? TP/SL 怨꾩궛
                entry_price = float(order['average']) if order.get('average') else price
                
                if side == 'long':
                    tp_price = entry_price * (1 + roe_target/leverage)
                    sl_price = entry_price * (1 - stop_loss/leverage)
                else:
                    tp_price = entry_price * (1 - roe_target/leverage)
                    sl_price = entry_price * (1 + stop_loss/leverage)
                    
                # 諛붿씠?몄뒪 湲곗? TP/SL 二쇰Ц (STOP_MARKET / TAKE_PROFIT_MARKET)
                try:
                    if tp_enabled:
                        params_tp = {
                            'stopPrice': self.safe_price(symbol, tp_price),
                            'reduceOnly': True
                        }
                        if side == 'long':
                            await asyncio.to_thread(self.exchange.create_order, symbol, 'TAKE_PROFIT_MARKET', 'sell', amount_str, None, params_tp)
                        else:
                            await asyncio.to_thread(self.exchange.create_order, symbol, 'TAKE_PROFIT_MARKET', 'buy', amount_str, None, params_tp)
                    
                    if sl_enabled:
                        params_sl = {
                            'stopPrice': self.safe_price(symbol, sl_price),
                            'reduceOnly': True
                        }
                        if side == 'long':
                            await asyncio.to_thread(self.exchange.create_order, symbol, 'STOP_MARKET', 'sell', amount_str, None, params_sl)
                        else:
                            await asyncio.to_thread(self.exchange.create_order, symbol, 'STOP_MARKET', 'buy', amount_str, None, params_sl)
                    
                    logger.info(f"??Protective orders placed: TP={'ON' if tp_enabled else 'OFF'}, SL={'ON' if sl_enabled else 'OFF'}")
                except Exception as e:
                    logger.error(f"Failed to place TP/SL order: {e}")
                    await self.ctrl.notify(f"⚠️ TP/SL 주문 실패: {e}")

        except Exception as e:
            logger.error(f"TEMA entry failed: {e}")
            await self.ctrl.notify(f"❌ 진입 실패: {e}")

    async def exit_position(self, symbol, reason):
        try:
            pos = await self.get_server_position(symbol, use_cache=False)
            if not pos: return

            amount = abs(float(pos.get('contracts', 0) or 0))
            pos_side = str(pos.get('side', '')).lower()
            side = 'sell' if pos_side == 'long' else 'buy'
            
            if amount > 0:
                await asyncio.to_thread(self.exchange.create_market_order, symbol, side, amount)
                await self.ctrl.notify(f"🔄 **TEMA 청산**: {symbol} ({reason})")
        except Exception as e:
            logger.error(f"TEMA exit failed: {e}")


class SignalEngine(BaseEngine):
    def __init__(self, controller):
        super().__init__(controller)
        # Multi-symbol support
        self.active_symbols = set()  # Manually added + Scanned symbols
        
        # Symbol-specific states (Dict[symbol, value])
        self.last_candle_time = {}
        self.last_candle_success = {} 
        self.last_processed_candle_ts = {}
        self.last_state_sync_candle_ts = {}
        self.last_stateful_retry_ts = {}
        self.last_stateful_diag = {}
        self.last_stateful_diag_notice = {}
        self.last_processed_exit_candle_ts = {}
        self.pending_reentry = {} # {symbol: {'side': 'long'|'short', 'target_time': ts}}
        self.ut_hybrid_timing_latches = {}
        self.ut_hybrid_timing_consumed_ts = {}
        
        self.last_heartbeat = 0
        self.consecutive_errors = 0
        self.last_activity = time.time()
        self.last_volume_scan = 0
        
        # Scanner State
        self.scanner_active_symbol = None # ?꾩옱 ?ㅼ틦?덇? ?↔퀬 ?덈뒗 肄붿씤 (Serial Hunter Mode)
        
        # Kalman ?곹깭 罹먯떆 (??쒕낫???쒖떆??
        self.kalman_states = {} # {symbol: {'velocity': float, 'direction': str}}
        
        # Strategy states (Dict[symbol, value] or just cache keying)
        # MicroVBO ?곹깭 罹먯떆
        self.vbo_states = {} # {symbol: {entry_price, entry_atr, breakout_level}}
        
        # FractalFisher ?곹깭 罹먯떆
        self.fisher_states = {} # {symbol: {hurst, value, prev_value, entry_price, entry_atr, trailing_stop}}
        self.cameron_states = {} # {symbol: {side, stop_price, entry_ref_price, signal_ts, ...}}
        
        # [New] Filter Status Persistence (Dashboard)
        self.last_entry_filter_status = {} # symbol -> {r2_val, ...}
        self.last_exit_filter_status = {}  # symbol -> {r2_val, ...}
        self.last_entry_reason = {}        # symbol -> latest entry decision reason

    def start(self):
        super().start()
        self.last_activity = time.time()
        # [Fix] ?ш컻(RESUME) ???곹깭 珥덇린?뷀븯??利됱떆 ?ъ쭊??媛?ν븯?꾨줉 ?섏젙
        self.last_candle_time = {}
        self.last_candle_success = {}
        self.last_processed_candle_ts = {}
        self.last_state_sync_candle_ts = {}
        self.last_stateful_retry_ts = {}
        self.last_stateful_diag = {}
        self.last_stateful_diag_notice = {}
        self.last_processed_exit_candle_ts = {}
        self.cameron_states = {}
        self.ut_hybrid_timing_latches = {}
        self.ut_hybrid_timing_consumed_ts = {}
        
        # 珥덇린??
        self.active_symbols = set()
        config_watchlist = self.get_runtime_watchlist()
        for s in config_watchlist:
            self.active_symbols.add(s)
        logger.info(f"?? [Signal] Engine started (Multi-Symbol Mode). Watching: {self.active_symbols}")

    def _get_exit_timeframe(self, symbol=None):
        """泥?궛????꾪봽?덉엫 (User Defined)
           醫낅ぉ???ㅼ틦?덉뿉 ?섑빐 ?≫엺 寃쎌슦 ?꾩슜 ??꾪봽?덉엫 諛섑솚
        """
        cfg = self.get_runtime_common_settings()
        if symbol and symbol == self.scanner_active_symbol:
            return cfg.get('scanner_exit_timeframe', '1h')
        return cfg.get('exit_timeframe', '4h')

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

        prev_row = valid.iloc[-2]
        curr_row = valid.iloc[-1]
        prev_src = float(prev_row['src'])
        curr_src = float(curr_row['src'])
        prev_stop = float(prev_row['trail_stop'])
        curr_stop = float(curr_row['trail_stop'])

        buy = curr_src > curr_stop and prev_src <= prev_stop
        sell = curr_src < curr_stop and prev_src >= prev_stop
        detail = {
            'key_value': key_value,
            'atr_period': atr_period,
            'use_heikin_ashi': use_heikin_ashi,
            'curr_src': curr_src,
            'curr_stop': curr_stop,
            'curr_atr': float(curr_row['atr']),
            'signal_ts': int(curr_row['timestamp']),
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
        short_setup = self._remember_utbb_short_setup(symbol, bb_sig, bb_detail) if symbol else None
        ut_signal_ts = ut_detail.get('signal_ts')
        short_setup_available = self._is_utbb_short_setup_available(symbol, ut_signal_ts) if symbol else False

        entry_sig = None
        if ut_sig == 'long':
            entry_sig = 'long'
        elif ut_sig == 'short' and short_setup_available:
            entry_sig = 'short'

        detail = {
            'ut_state': ut_state,
            'ut_signal': ut_sig,
            'ut_reason': ut_reason,
            'ut_signal_ts': ut_signal_ts,
            'timing_label': 'BB',
            'timing_signal': bb_sig,
            'effective_timing_signal': 'short' if short_setup_available else None,
            'timing_match_source': 'latched' if short_setup_available else None,
            'timing_reason': bb_reason,
            'timing_signal_ts': bb_detail.get('signal_ts'),
            'latched_timing_signal': short_setup.get('signal') if short_setup else None,
            'latched_timing_signal_ts': short_setup.get('signal_ts') if short_setup else None,
            'latched_timing_available': short_setup_available,
            'bb_short_setup_ready': short_setup_available,
            'bb_short_setup_ts': short_setup.get('signal_ts') if short_setup else None,
            'bb_short_signal': bb_sig,
            'bb_short_reason': bb_reason,
            'bb_upper_breakout': bb_upper_sig == 'exit_long',
            'bb_upper_reason': bb_upper_reason,
            'bb_upper_signal_ts': bb_upper_detail.get('signal_ts'),
            'ut_detail': ut_detail,
            'timing_detail': bb_detail
        }

        if entry_sig == 'long':
            return 'long', "UTBB LONG: UT 롱 진입신호 확인", detail
        if entry_sig == 'short':
            return 'short', "UTBB SHORT: BB 상단 재진입 셋업 이후 UT 숏신호 확인", detail

        if ut_sig == 'short' and not short_setup_available:
            return None, "UTBB 대기: UT 숏신호 감지, BB 상단 재진입 숏 셋업 대기", detail
        if ut_sig == 'long':
            return None, "UTBB 대기: UT 롱신호 감지", detail
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

    async def poll_tick(self):
        """
        [?쒖닔 ?대쭅] 硫붿씤 ?대쭅 ?⑥닔 (Multi-Symbol)
        Mode 1: Scanner ON -> Serial Hunter (One at a time, ignoring Watchlist)
        Mode 2: Scanner OFF -> Watchlist + Manual Added
        """
        if not self.running:
            return
        
        try:
            self.last_activity = time.time()
            cfg = self.get_runtime_trade_config()
            common_cfg = self.get_runtime_common_settings()
            entry_tf = common_cfg.get('entry_timeframe', common_cfg.get('timeframe', '8h'))
            
            active_position_symbols = set()
            # Common: Fetch current positions (best effort).
            # If this call fails (e.g. permission/auth issue), do not stop status updates.
            try:
                active_position_symbols = await self.get_active_position_symbols(use_cache=True)
            except Exception as e:
                logger.warning(
                    f"Signal poll_tick: fetch_positions failed, continuing without server positions ({e})"
                )

            # Check Scanner Setting
            scanner_enabled = common_cfg.get('scanner_enabled', True)
            
            target_symbols = set()
            
            if scanner_enabled:
                # === [Mode 1: Serial Scanner] ===
                # 0. Add Existing Positions to Targets (Safety Net)
                for sym in active_position_symbols:
                    target_symbols.add(sym)

                # 1. 留뚯빟 ?대? ?↔퀬 ?덈뒗 ?ㅼ틦??肄붿씤???덈떎硫? -> 洹멸쾬留?愿由?
                if self.scanner_active_symbol:
                    # ?ъ??섏씠 ?꾩쭅 ?댁븘?덈뒗吏 ?뺤씤
                    pos = await self.get_server_position(self.scanner_active_symbol, use_cache=False)
                    if pos:
                        # ?ъ????좎? 以?-> ?대냸留?吏묒쨷 耳??(?ㅼ틪 以묒?)
                        target_symbols.add(self.scanner_active_symbol)
                    else:
                        # ?ъ????놁쓬 (泥?궛?? -> ?ㅼ틦??蹂??珥덇린??& ?ㅼ떆 ?ㅼ틪 紐⑤뱶 吏꾩엯
                        logger.info(f"?삼툘 Scanner trade completed for {self.scanner_active_symbol}. Resuming scan.")
                        self.scanner_active_symbol = None
                        # 諛붾줈 ?ㅼ틪 濡쒖쭅?쇰줈 ?섏뼱媛?
                
                # 2. ?↔퀬 ?덈뒗寃??녿떎硫? -> ?ㅼ틪 ?ㅽ뻾
                if not self.scanner_active_symbol:
                    # ?ㅼ틪 寃곌낵? 蹂꾧컻濡?湲곗〈 ?ъ??섏? ?대? target_symbols??異붽???
                    
                    # 荑⑦???濡쒖쭅: ?ъ???泥?궛 吏곹썑?쇰㈃ 諛붾줈 ?ㅼ틪?댁빞 ??
                    if time.time() - self.last_volume_scan > 60: # 1遺?荑⑦???
                        await self.scan_and_trade_high_volume()
                        self.last_volume_scan = time.time()
                    
                    # ?ㅼ틪 寃곌낵 吏꾩엯?덉쑝硫?target_symbols??異붽???
                    if self.scanner_active_symbol:
                        target_symbols.add(self.scanner_active_symbol)
            else:
                # === [Mode 2: Manual / Watchlist] ===
                # Config Watchlist
                config_symbols = set(self.get_runtime_watchlist())
                
                # Merge: Config + Chat Manual + Positions
                target_symbols = self.active_symbols | config_symbols | active_position_symbols
 
            if self.ctrl.is_paused:
                total, free, mmr = await self.get_balance_info()
                count, daily_pnl = self.db.get_daily_stats()
                paused_symbol = sorted(target_symbols)[0] if target_symbols else 'PAUSED'
                self.ctrl.status_data = {
                    'PAUSED': {
                        'engine': 'SIGNAL',
                        'symbol': paused_symbol,
                        'price': 0,
                        'pos_side': 'NONE',
                        'total_equity': total,
                        'free_usdt': free,
                        'mmr': mmr,
                        'daily_count': count,
                        'daily_pnl': daily_pnl,
                        'leverage': common_cfg.get('leverage', 20),
                        'margin_mode': 'SPOT' if self.is_upbit_mode() else 'ISOLATED',
                        'entry_tf': entry_tf,
                        'exit_tf': common_cfg.get('exit_timeframe', '4h'),
                        'entry_reason': '일시정지(PAUSE) 상태',
                        'protection_config': {'tp_enabled': False, 'sl_enabled': False},
                    }
                }
                return

            if 'PAUSED' in self.ctrl.status_data:
                del self.ctrl.status_data['PAUSED']

            if not target_symbols:
                if scanner_enabled:
                    # [Fix] Provide status feedback during scanning (Target empty)
                    total, free, mmr = await self.get_balance_info()
                    count, daily_pnl = self.db.get_daily_stats()
                    
                    self.ctrl.status_data['SCANNER'] = {
                        'engine': 'SIGNAL',
                        'symbol': 'Scanning... ?뱻',
                        'price': 0,
                        'pos_side': 'NONE',
                        'total_equity': total, 'free_usdt': free, 'mmr': mmr,
                        'daily_count': count, 'daily_pnl': daily_pnl,
                        'leverage': common_cfg.get('leverage', 20),
                        'margin_mode': 'SPOT' if self.is_upbit_mode() else 'ISOLATED',
                        'entry_tf': entry_tf,
                        'exit_tf': common_cfg.get('exit_timeframe', '4h')
                    }
                return

            # If targets exist, remove SCANNER placeholder to avoid duplicate display
            if 'SCANNER' in self.ctrl.status_data:
                del self.ctrl.status_data['SCANNER']

            active_status_keys = set(target_symbols) | {'PAUSED', 'SCANNER'}
            stale_keys = [
                key for key in list(self.ctrl.status_data.keys())
                if key not in active_status_keys
            ]
            for stale_key in stale_keys:
                self.ctrl.status_data.pop(stale_key, None)

            # Parallel Execution: Poll all symbols concurrently
            tasks = []
            for symbol in target_symbols:
                if self.ctrl.is_paused: break
                tasks.append(self.poll_symbol(symbol, entry_tf, cfg))
                
            if tasks:
                await asyncio.gather(*tasks)

        except Exception as e:
            self.consecutive_errors += 1
            logger.error(f"Signal poll_tick error ({self.consecutive_errors}x): {e}")
            if self.consecutive_errors > 10:
                self.consecutive_errors = 0

    async def poll_symbol(self, symbol, primary_tf, cfg):
        """媛쒕퀎 ?щ낵 ?대쭅 濡쒖쭅"""
        # Defensive Check: ?곹깭 蹂?섍? ?ㅼ뿼?섏뿀??寃쎌슦 蹂듦뎄
        if not isinstance(self.last_processed_candle_ts, dict):
            logger.error(f"?좑툘 State corrupted: last_processed_candle_ts is {type(self.last_processed_candle_ts)}, resetting.")
            self.last_processed_candle_ts = {}
        if not isinstance(self.last_state_sync_candle_ts, dict):
            logger.error(f"?좑툘 State corrupted: last_state_sync_candle_ts is {type(self.last_state_sync_candle_ts)}, resetting.")
            self.last_state_sync_candle_ts = {}
        if not isinstance(self.last_stateful_retry_ts, dict):
            logger.error(f"?좑툘 State corrupted: last_stateful_retry_ts is {type(self.last_stateful_retry_ts)}, resetting.")
            self.last_stateful_retry_ts = {}
        if not isinstance(self.last_processed_exit_candle_ts, dict):
            logger.error(f"?좑툘 State corrupted: last_processed_exit_candle_ts is {type(self.last_processed_exit_candle_ts)}, resetting.")
            self.last_processed_exit_candle_ts = {}
        if not isinstance(self.last_candle_time, dict):
            logger.error(f"?좑툘 State corrupted: last_candle_time is {type(self.last_candle_time)}, resetting.")
            self.last_candle_time = {}
        if not isinstance(self.fisher_states, dict):
            logger.error(f"?좑툘 State corrupted: fisher_states is {type(self.fisher_states)}, resetting.")
            self.fisher_states = {}
        if not isinstance(self.vbo_states, dict):
            logger.error(f"?좑툘 State corrupted: vbo_states is {type(self.vbo_states)}, resetting.")
            self.vbo_states = {}
        if not isinstance(self.cameron_states, dict):
            logger.error(f"?좑툘 State corrupted: cameron_states is {type(self.cameron_states)}, resetting.")
            self.cameron_states = {}
            
        try:
            # 1. OHLCV (Primary) - Basic monitoring
            ohlcv_p = await asyncio.to_thread(self.market_data_exchange.fetch_ohlcv, symbol, primary_tf, limit=5)
            if not ohlcv_p or len(ohlcv_p) < 3:
                return
            
            current_price = float(ohlcv_p[-1][4])
        
            # [Fix] Update Status and get local pos_side to avoid race condition
            pos_side = await self.check_status(symbol, current_price)
            
            # 2. Check Primary TF (Entry Logic)
            last_closed_p = ohlcv_p[-2]
            ts_p = last_closed_p[0]
            k_p = {
                't': ts_p, 'o': str(last_closed_p[1]), 'h': str(last_closed_p[2]),
                'l': str(last_closed_p[3]), 'c': str(last_closed_p[4]), 'v': str(last_closed_p[5])
            }
            
            # ?щ낵蹂??곹깭 珥덇린??
            if symbol not in self.last_processed_candle_ts:
                self.last_processed_candle_ts[symbol] = 0
            if symbol not in self.last_state_sync_candle_ts:
                self.last_state_sync_candle_ts[symbol] = 0
            if symbol not in self.last_stateful_retry_ts:
                self.last_stateful_retry_ts[symbol] = 0.0

            strategy_params = cfg.get('strategy_params', {})
            entry_mode = strategy_params.get('entry_mode', 'cross').lower()
            active_strategy = strategy_params.get('active_strategy', 'sma').lower()
            uses_stateful_primary_sync = active_strategy in STATEFUL_UT_STRATEGIES
            processed_primary_this_tick = False
            
            if ts_p > self.last_processed_candle_ts[symbol]:
                logger.info(f"?빉截?[Primary {primary_tf}] {symbol} New Candle: {ts_p} close={last_closed_p[4]}")
                await self.process_primary_candle(symbol, k_p)
                processed_primary_this_tick = True
                if self.last_candle_success.get(symbol, False):
                    self.last_processed_candle_ts[symbol] = ts_p
                    if uses_stateful_primary_sync:
                        self.last_state_sync_candle_ts[symbol] = ts_p
                    pos_side = await self.check_status(symbol, current_price)
                else:
                    logger.warning(f"Primary candle processing failed, will retry: {symbol} {ts_p}")
            elif uses_stateful_primary_sync and self.last_state_sync_candle_ts[symbol] < ts_p:
                logger.info(f"?봽 [Primary {primary_tf}] {symbol} State sync on closed candle: {ts_p} close={last_closed_p[4]}")
                await self.process_primary_candle(symbol, k_p, force=True)
                processed_primary_this_tick = True
                if self.last_candle_success.get(symbol, False):
                    self.last_state_sync_candle_ts[symbol] = ts_p
                    pos_side = await self.check_status(symbol, current_price)
            elif uses_stateful_primary_sync and pos_side != 'NONE':
                retry_interval = 15.0
                now_ts = time.time()
                if (not processed_primary_this_tick) and (now_ts - float(self.last_stateful_retry_ts.get(symbol, 0.0) or 0.0) >= retry_interval):
                    logger.info(
                        f"?봽 [Primary {primary_tf}] {symbol} Stateful reconcile retry: "
                        f"candle={ts_p} pos={pos_side} close={last_closed_p[4]}"
                    )
                    self.last_stateful_retry_ts[symbol] = now_ts
                    await self.process_primary_candle(symbol, k_p, force=True)
                    if self.last_candle_success.get(symbol, False):
                        self.last_state_sync_candle_ts[symbol] = ts_p
                    pos_side = await self.check_status(symbol, current_price)
                
            # 3. Check Exit TF (Exit Logic)
            # Cross/Position 紐⑤뱶?먯꽌留?Secondary TF 泥?궛 濡쒖쭅 ?ъ슜
            uses_secondary_exit = (
                pos_side != 'NONE'
                and (
                    (active_strategy in MA_STRATEGIES and entry_mode in ['cross', 'position'])
                    or active_strategy == 'cameron'
                    or (self.is_upbit_mode() and active_strategy == 'utbot')
                )
            )
            if uses_secondary_exit:
                exit_tf = self._get_exit_timeframe(symbol)
                
                ohlcv_e = await asyncio.to_thread(self.market_data_exchange.fetch_ohlcv, symbol, exit_tf, limit=5)
                if ohlcv_e and len(ohlcv_e) >= 3:
                    last_closed_e = ohlcv_e[-2]
                    ts_e = last_closed_e[0]
                    
                    if symbol not in self.last_processed_exit_candle_ts:
                        self.last_processed_exit_candle_ts[symbol] = 0

                    # [Initial Sync] 留뚯빟 遊??ъ떆???깆쑝濡??꾩쭅 怨꾩궛 湲곕줉???녾퀬 ?ъ??섏씠 ?덈떎硫?利됱떆 1??怨꾩궛
                    is_first_sync = (self.last_processed_exit_candle_ts[symbol] == 0)
                    
                    if ts_e > self.last_processed_exit_candle_ts[symbol] or is_first_sync:
                        if is_first_sync:
                            logger.info(f"?봽 [Initial Sync] {symbol} Position detected on restart, processing filters...")
                        else:
                            logger.info(f"?빉截?[Exit {exit_tf}] {symbol} New Candle: {ts_e} close={last_closed_e[4]}")
                            
                        exit_ok = await self.process_exit_candle(symbol, exit_tf, pos_side)
                        if exit_ok:
                            self.last_processed_exit_candle_ts[symbol] = ts_e
                        else:
                            logger.warning(f"Exit candle processing failed, will retry: {symbol} {ts_e}")

        except Exception as e:
            logger.error(f"Poll symbol {symbol} error: {e}")
            import traceback
            traceback.print_exc()

    async def scan_and_trade_high_volume(self):
        """[New] High Volume Scanner Logic (Refined)
        Rule: 200M+ Vol -> Top 5 Risers -> Select Max Vol from Top 5 -> Cross Strategy
        Serial Mode: Finds ONE target -> Enters -> Sets self.scanner_active_symbol
        """
        try:
            if self.is_upbit_mode():
                logger.info("Scanner skipped: Upbit mode uses dedicated KRW UTBOT watchlist only.")
                return

            logger.info("?뱻 Scanning high volume markets (>200M USDT)...")
            tickers = await asyncio.to_thread(self.market_data_exchange.fetch_tickers)
            
            # 1. 1李??꾪꽣: 嫄곕옒湲덉븸 200M ?댁긽
            candidates = []
            for symbol, data in tickers.items():
                # [Fix] 諛붿씠?몄뒪 ?좊Ъ ?щ낵留??꾪꽣 (?ㅽ뙚 ?쒖쇅: BTC/USDT ??X, BTC/USDT:USDT ??O)
                if '/USDT:USDT' in symbol:
                    quote_vol = float(data.get('quoteVolume', 0) or 0)
                    percentage = float(data.get('percentage', 0) or 0)
                    if quote_vol >= 200_000_000:
                        candidates.append({'symbol': symbol, 'vol': quote_vol, 'pct': percentage})
            
            if not candidates:
                logger.info("scanner: No coins > 200M vol found.")
                return

            # 2. 2李??꾪꽣: ?곸듅瑜??곸쐞 5媛?
            candidates.sort(key=lambda x: x['pct'], reverse=True)
            top_5_risers = candidates[:5]
            
            # Debug Log: Top 5 Risers
            log_msg = "?뱤 [Scanner Debug] Top 5 Risers:\n"
            for idx, c in enumerate(top_5_risers):
                log_msg += f"  {idx+1}. {c['symbol']}: Rise={c['pct']:.2f}%, Vol={c['vol']/1_000_000:.1f}M\n"
            logger.info(log_msg.strip())

            if not top_5_risers:
                 logger.info("scanner: No candidates after filtering.")
                 return

            # 3. 3李??꾪꽣: 洹?以?嫄곕옒?湲??쒖쑝濡??뺣젹?섏뿬 ?쒖감?곸쑝濡?泥댄겕
            top_5_risers.sort(key=lambda x: x['vol'], reverse=True)
            
            for target_coin in top_5_risers:
                symbol = target_coin['symbol']
                logger.info(f"?렞 Scanner Evaluating: {symbol} (Vol: {target_coin['vol']/1_000_000:.1f}M, Rise: {target_coin['pct']:.2f}%)")

                # 4. ?꾨왂 ?ㅽ뻾
                if self.ctrl.is_paused: return
                
                try:
                    cfg = self.get_runtime_trade_config()
                    common_cfg = self.get_runtime_common_settings()
                    scan_tf = common_cfg.get('scanner_timeframe', '15m')
                    min_rise_pct = float(common_cfg.get('scanner_min_rise_pct', 0.5) or 0.0)
                    max_rise_pct = float(common_cfg.get('scanner_max_rise_pct', 8.0) or 8.0)
                    strategy_params = cfg.get('strategy_params', {})

                    rise_pct = float(target_coin.get('pct', 0) or 0)
                    if rise_pct < min_rise_pct:
                        logger.info(f"?? Scanner Skip {symbol}: rise too weak ({rise_pct:.2f}% < {min_rise_pct:.2f}%)")
                        continue
                    if rise_pct > max_rise_pct:
                        logger.info(f"?? Scanner Skip {symbol}: rise too extended ({rise_pct:.2f}% > {max_rise_pct:.2f}%)")
                        continue
                    
                    # [MODIFIED] Always use entry_timeframe if scanner is evaluating for a position-like entry
                    scan_params = strategy_params.copy()
                    active_strategy = scan_params.get('active_strategy', 'sma').lower()
                    if active_strategy not in CORE_STRATEGIES:
                        active_strategy = 'sma'
                    
                    # Use scanner_timeframe if set, but ensure we are thinking about consistency
                    ohlcv = await asyncio.to_thread(self.market_data_exchange.fetch_ohlcv, symbol, scan_tf, limit=300)
                    if not ohlcv: continue
                    
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    
                    sig, _, _, _, _, _ = await self._calculate_strategy_signal(
                        symbol, df, scan_params, active_strategy, allow_utbot_stateful=False
                    )
                    
                    if sig:
                        # ?ъ????뺤씤 (?쒕쾭)
                        pos = await self.get_server_position(symbol, use_cache=False)
                        
                        if not pos:
                            logger.info(f"?? Scanner Locking In: {symbol} [{sig.upper()}] detected!")
                            current_price = float(ohlcv[-1][4])
                            await self.entry(symbol, sig, current_price)
                            
                            self.scanner_active_symbol = symbol
                            current_ts = int(ohlcv[-1][0])
                            self.last_processed_candle_ts[symbol] = current_ts
                            self.last_candle_time[symbol] = current_ts
                            self.last_candle_success[symbol] = True
                            break # Found a winner, exit loop
                        else:
                            logger.info(f"?? Scanner Checked {symbol}: Position exists ({pos['side']})")

                except Exception as e:
                    logger.error(f"Scanner strategy check failed for {symbol}: {e}")
                    continue
                
        except Exception as e:
            logger.error(f"Volume scanner error: {e}")

    def _timeframe_to_ms(self, tf):
        """??꾪봽?덉엫??諛由ъ큹濡?蹂??"""
        multipliers = {
            'm': 60 * 1000,
            'h': 60 * 60 * 1000,
            'd': 24 * 60 * 60 * 1000
        }
        try:
            unit = tf[-1]
            value = int(tf[:-1])
            return value * multipliers.get(unit, 0)
        except:
            return 0

    async def check_status(self, symbol, price):
        try:
            total, free, mmr = await self.get_balance_info()
            count, daily_pnl = self.db.get_daily_stats()
            pos = await self.get_server_position(symbol)
            
            # ?꾨왂 ?곹깭 媛?몄삤湲?
            strategy_params = self.get_runtime_strategy_params()
            comm_cfg = self.get_runtime_common_settings()
            kalman_cfg = strategy_params.get('kalman_filter', {})
            kalman_enabled = bool(kalman_cfg.get('entry_enabled', False) or kalman_cfg.get('exit_enabled', False))
            active_strategy = strategy_params.get('active_strategy', 'sma').upper()
            entry_mode = strategy_params.get('entry_mode', 'cross').upper()
            if active_strategy in {'UTBOT', 'RSIBB', 'UTRSIBB', 'UTRSI', 'UTBB'}:
                entry_mode = active_strategy
            
            # MicroVBO State
            vbo_state = self.vbo_states.get(symbol, {})
            # FractalFisher State
            fisher_state = self.fisher_states.get(symbol, {})
            
            # [Fix] Multi-symbol Status Data
            pos_side = pos['side'].upper() if pos else 'NONE'
            symbol_status = {
                'engine': 'Signal', 'symbol': symbol, 'price': price,
                'total_equity': total, 'free_usdt': free, 'mmr': mmr,
                'daily_count': count, 'daily_pnl': daily_pnl,
                'network': self.ctrl.get_network_status_label(),
                'exchange_id': self.ctrl.get_exchange_display_name(),
                'market_data_exchange_id': getattr(self.market_data_exchange, 'id', 'unknown'),
                'market_data_source': getattr(self.ctrl, 'market_data_source_label', self.ctrl.get_exchange_display_name()),
                'pos_side': pos_side,
                'entry_price': float(pos['entryPrice']) if pos else 0.0,
                'coin_amt': float(pos['contracts']) if pos else 0.0,
                'pnl_pct': float(pos['percentage']) if pos else 0.0,
                'pnl_usdt': float(pos['unrealizedPnl']) if pos else 0.0,
                'quote_currency': self.get_quote_currency(),
                'is_spot': self.is_upbit_mode(),
                # Signal ?꾩슜 ?꾨뱶
                'kalman_enabled': kalman_enabled,
                'kalman_velocity': self.kalman_states.get(symbol, {}).get('velocity', 0.0),
                'kalman_direction': self.kalman_states.get(symbol, {}).get('direction'),
                'active_strategy': active_strategy,
                'entry_mode': entry_mode,
                'entry_reason': self.last_entry_reason.get(symbol, '대기'),
                'stateful_diag': self.last_stateful_diag.get(symbol, {}),
                'runtime_diag': self.ctrl.get_runtime_diag(),
                # MicroVBO ?꾩슜 ?꾨뱶
                'vbo_breakout_level': vbo_state.get('breakout_level'),
                'vbo_entry_atr': vbo_state.get('entry_atr'),
                # FractalFisher ?꾩슜 ?꾨뱶
                'fisher_hurst': fisher_state.get('hurst'),
                'fisher_value': fisher_state.get('value'),
                'fisher_trailing_stop': fisher_state.get('trailing_stop'),
                'fisher_entry_atr': fisher_state.get('entry_atr')
            }
            
            # Merge Last Filter Status (Persistence)
            entry_status = self.last_entry_filter_status.get(symbol, {})
            exit_status = self.last_exit_filter_status.get(symbol, {})
            
            symbol_status['entry_filters'] = entry_status
            symbol_status['exit_filters'] = exit_status
            
            # Real-time Filter Config
            symbol_status['filter_config'] = {
                'r2': {
                    'en_entry': comm_cfg.get('r2_entry_enabled', True), 
                    'en_exit': comm_cfg.get('r2_exit_enabled', True),
                    'th': comm_cfg.get('r2_threshold', 0.25)
                },
                'chop': {
                    'en_entry': comm_cfg.get('chop_entry_enabled', True), 
                    'en_exit': comm_cfg.get('chop_exit_enabled', True),
                    'th': comm_cfg.get('chop_threshold', 50.0)
                },
                'cc': {
                    'en_exit': comm_cfg.get('cc_exit_enabled', False),
                    'th': comm_cfg.get('cc_threshold', 0.70)
                },
                'kalman': {
                    'en_entry': strategy_params.get('kalman_filter', {}).get('entry_enabled', False),
                    'en_exit': strategy_params.get('kalman_filter', {}).get('exit_enabled', False)
                }
            }
            tp_master_enabled = False if self.is_upbit_mode() else bool(comm_cfg.get('tp_sl_enabled', True))
            tp_enabled = tp_master_enabled and bool(comm_cfg.get('take_profit_enabled', True))
            sl_enabled = tp_master_enabled and bool(comm_cfg.get('stop_loss_enabled', True))
            symbol_status['protection_config'] = {
                'tp_enabled': tp_enabled,
                'sl_enabled': sl_enabled
            }
            
            # [New] Status Display Enhancement
            symbol_status['leverage'] = comm_cfg.get('leverage', 1 if self.is_upbit_mode() else 20)
            symbol_status['margin_mode'] = 'SPOT' if self.is_upbit_mode() else 'ISOLATED'
            symbol_status['entry_tf'] = comm_cfg.get('entry_timeframe', comm_cfg.get('timeframe', '8h'))
            symbol_status['exit_tf'] = comm_cfg.get('exit_timeframe', '4h')

            self.ctrl.status_data[symbol] = symbol_status
            
            # MMR 寃쎄퀬 泥댄겕
            await self.check_mmr_alert(mmr)
            
            return pos_side
                
        except Exception as e:
            logger.error(f"Signal check_status error: {e}")

    def _reset_vbo_state(self):
        """MicroVBO ?곹깭 珥덇린??"""
        self.vbo_entry_price = None
        self.vbo_entry_atr = None

    def _reset_fisher_state(self):
        """FractalFisher ?곹깭 珥덇린??"""
        self.fisher_entry_price = None
        self.fisher_entry_atr = None
        self.fisher_trailing_stop = None

    def _update_stateful_diag(self, symbol, **kwargs):
        current = dict(self.last_stateful_diag.get(symbol, {}))
        current.update(kwargs)
        self.last_stateful_diag[symbol] = current

    async def _notify_stateful_diag(self, symbol, force=False):
        info = self.last_stateful_diag.get(symbol)
        if not info:
            return

        payload = "|".join([
            str(info.get('stage', '')),
            str(info.get('strategy', '')),
            str(info.get('raw_state', '')),
            str(info.get('raw_signal', '')),
            str(info.get('entry_sig', '')),
            str(info.get('pos_side', '')),
            str(info.get('ut_key', '')),
            str(info.get('ut_atr', '')),
            str(info.get('ut_ha', '')),
            str(info.get('note', '')),
        ])
        now = time.time()
        prev = self.last_stateful_diag_notice.get(symbol, {})
        if not force and prev.get('payload') == payload and (now - float(prev.get('ts', 0.0) or 0.0)) < 180:
            return

        self.last_stateful_diag_notice[symbol] = {'payload': payload, 'ts': now}
        lines = [
            f"🧪 UT 진단 {symbol}",
            f"stage={info.get('stage', '?')} strategy={info.get('strategy', '?')}",
            f"raw_state={info.get('raw_state', 'none')} raw_signal={info.get('raw_signal', 'none')}",
            f"entry_sig={info.get('entry_sig', 'none')} pos={info.get('pos_side', 'NONE')}",
        ]
        if info.get('ut_key') is not None:
            lines.append(
                f"ut=K{info.get('ut_key')} ATR{info.get('ut_atr')} HA={info.get('ut_ha')}"
            )
        if info.get('src') is not None and info.get('stop') is not None:
            lines.append(
                f"src={float(info.get('src')):.4f} stop={float(info.get('stop')):.4f}"
            )
        if info.get('signal_ts_human') or info.get('feed_last_ts_human'):
            lines.append(
                f"tf={info.get('tf_used', '?')} signal_ts={info.get('signal_ts_human', '?')} feed_last={info.get('feed_last_ts_human', '?')}"
            )
        if info.get('closed_ohlc_text'):
            lines.append(f"closed={info.get('closed_ohlc_text')}")
        if info.get('live_ohlc_text'):
            lines.append(f"live={info.get('live_ohlc_text')}")
        note = info.get('note')
        if note:
            lines.append(f"note={note}")
        await self.ctrl.notify("\n".join(lines))

    async def process_primary_candle(self, symbol, k, force=False):
        candle_time = k['t']
        
        # ?щ낵蹂??곹깭 珥덇린??
        if symbol not in self.last_candle_time:
            self.last_candle_time[symbol] = 0
            self.last_candle_success[symbol] = True

        # 以묐났 罹붾뱾 諛⑹? (媛숈? 罹붾뱾 ?ъ쿂由?李⑤떒) - ?깃났??寃쎌슦?먮쭔 ?ㅽ궢
        if (not force) and candle_time <= self.last_candle_time[symbol] and self.last_candle_success[symbol]:
            logger.debug(f"??Skipping duplicate candle: {candle_time} <= {self.last_candle_time[symbol]}")
            return
        
        processing_candle_time = candle_time
        self.last_signal_check = time.time()
        self.last_candle_success[symbol] = False
        
        logger.info(f"?빉截?[Signal] Processing candle: {symbol} close={k['c']}")
        
        if await self.check_daily_loss_limit():
            logger.info("??Daily loss limit reached, skipping trade")
            self.last_candle_time[symbol] = processing_candle_time
            self.last_candle_success[symbol] = True
            return
        
        try:
            await self.check_status(symbol, float(k['c']))
            
            # [MODIFIED] Prioritize entry_timeframe for fetching entry OHLCV
            common_cfg = self.get_runtime_common_settings()
            tf = common_cfg.get('entry_timeframe', common_cfg.get('timeframe', '15m'))
            ohlcv = await asyncio.to_thread(self.market_data_exchange.fetch_ohlcv, symbol, tf, limit=300)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            # [CRITICAL] Ensure numeric types (Robust Loop)
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            
            # ===== ?꾨왂 ?ㅼ젙 濡쒕뱶 =====
            strategy_params = self.get_runtime_strategy_params()
            active_strategy = strategy_params.get('active_strategy', 'sma').lower()
            raw_strategy_sig = None
            raw_state_sig = None
            raw_ut_detail = {}
            raw_hybrid_detail = {}
            if active_strategy == 'utbot':
                raw_strategy_sig, _, ut_detail = self._calculate_utbot_signal(df, strategy_params)
                raw_state_sig = raw_strategy_sig or ut_detail.get('bias_side')
                raw_ut_detail = ut_detail or {}
            elif active_strategy == 'utbb':
                _, _, hybrid_detail = self._calculate_utbb_signal(symbol, df, strategy_params)
                raw_strategy_sig = hybrid_detail.get('ut_signal')
                raw_state_sig = hybrid_detail.get('ut_state')
                raw_ut_detail = hybrid_detail.get('ut_detail') or {}
                raw_hybrid_detail = hybrid_detail or {}
            elif active_strategy in UT_HYBRID_STRATEGIES:
                hybrid_calc = {
                    'utrsibb': self._calculate_utrsibb_signal,
                    'utrsi': self._calculate_utrsi_signal
                }.get(active_strategy)
                _, _, hybrid_detail = hybrid_calc(symbol, df, strategy_params)
                raw_strategy_sig = hybrid_detail.get('ut_state')
                raw_state_sig = raw_strategy_sig
                raw_ut_detail = hybrid_detail.get('ut_detail') or {}
                raw_hybrid_detail = hybrid_detail or {}
            elif active_strategy == 'rsibb':
                raw_strategy_sig, _, _ = self._calculate_rsibb_signal(df, strategy_params)
                raw_state_sig = raw_strategy_sig
            
            # ?꾨왂蹂??좏샇 怨꾩궛
            sig, is_bullish, is_bearish, strategy_name, entry_mode, kalman_enabled = await self._calculate_strategy_signal(symbol, df, strategy_params, active_strategy)
            strategy_sig = sig
            
            # 留ㅻℓ 諛⑺뼢 ?꾪꽣
            d_mode = self.ctrl.get_effective_trade_direction()
            if sig and ((d_mode == 'long' and sig == 'short') or (d_mode == 'short' and sig == 'long')):
                logger.info(f"??Signal {sig} blocked by direction filter: {d_mode}")
                self.last_entry_reason[symbol] = f"방향 필터 차단 ({sig.upper()} vs {d_mode.upper()})"
                sig = None

            # ?ъ????뺤씤
            self.position_cache = None
            self.position_cache_time = 0
            pos = await self.get_server_position(symbol, use_cache=False)
            
            current_side = pos['side'] if pos else 'NONE'
            logger.info(f"?뱧 Current position: {current_side}, Signal: {sig or 'NONE'}, Mode: {entry_mode}")
            if active_strategy in STATEFUL_UT_STRATEGIES:
                signal_ts = raw_ut_detail.get('signal_ts')
                timing_signal_ts = raw_hybrid_detail.get('timing_signal_ts')
                feed_last_ts = int(df.iloc[-1]['timestamp']) if len(df) >= 1 else None
                closed_row = df.iloc[-2] if len(df) >= 2 else None
                live_row = df.iloc[-1] if len(df) >= 1 else None

                def _fmt_ohlc(row):
                    if row is None:
                        return None
                    ts = int(row['timestamp'])
                    return (
                        f"{datetime.fromtimestamp(ts / 1000).strftime('%m-%d %H:%M')} "
                        f"O{float(row['open']):.2f} H{float(row['high']):.2f} "
                        f"L{float(row['low']):.2f} C{float(row['close']):.2f}"
                    )

                self._update_stateful_diag(
                    symbol,
                    stage='evaluate',
                    strategy=active_strategy.upper(),
                    raw_state=(raw_state_sig or 'none'),
                    raw_signal=(raw_strategy_sig or 'none'),
                    entry_sig=(sig or 'none'),
                    pos_side=str(current_side).upper(),
                    ut_key=raw_ut_detail.get('key_value'),
                    ut_atr=raw_ut_detail.get('atr_period'),
                    ut_ha='ON' if raw_ut_detail.get('use_heikin_ashi') else 'OFF',
                    src=raw_ut_detail.get('curr_src'),
                    stop=raw_ut_detail.get('curr_stop'),
                    tf_used=tf,
                    signal_ts=signal_ts,
                    signal_ts_human=datetime.fromtimestamp(signal_ts / 1000).strftime('%m-%d %H:%M') if signal_ts else None,
                    timing_label=raw_hybrid_detail.get('timing_label'),
                    timing_signal=(raw_hybrid_detail.get('timing_signal') or 'none') if raw_hybrid_detail.get('timing_label') else None,
                    effective_timing_signal=(raw_hybrid_detail.get('effective_timing_signal') or 'none') if raw_hybrid_detail.get('timing_label') else None,
                    timing_match_source=raw_hybrid_detail.get('timing_match_source'),
                    timing_signal_ts=timing_signal_ts,
                    timing_signal_ts_human=datetime.fromtimestamp(timing_signal_ts / 1000).strftime('%m-%d %H:%M') if timing_signal_ts else None,
                    timing_reason=raw_hybrid_detail.get('timing_reason'),
                    latched_timing_signal=raw_hybrid_detail.get('latched_timing_signal'),
                    latched_timing_available=raw_hybrid_detail.get('latched_timing_available'),
                    latched_timing_signal_ts=raw_hybrid_detail.get('latched_timing_signal_ts'),
                    latched_timing_signal_ts_human=datetime.fromtimestamp(raw_hybrid_detail.get('latched_timing_signal_ts') / 1000).strftime('%m-%d %H:%M') if raw_hybrid_detail.get('latched_timing_signal_ts') else None,
                    feed_last_ts=feed_last_ts,
                    feed_last_ts_human=datetime.fromtimestamp(feed_last_ts / 1000).strftime('%m-%d %H:%M') if feed_last_ts else None,
                    feed_last_close=float(df.iloc[-1]['close']) if len(df) >= 1 else None,
                    closed_ohlc_text=_fmt_ohlc(closed_row),
                    live_ohlc_text=_fmt_ohlc(live_row),
                    note=f"force={'Y' if force else 'N'} dir={d_mode}"
                )
            
            # 6.5 Pending Re-entry Check (吏??吏꾩엯)
            # ?댁쟾 罹붾뱾?먯꽌 泥?궛 ???덉빟??吏꾩엯???덈뒗吏 ?뺤씤
            if self.pending_reentry.get(symbol):
                reentry_data = self.pending_reentry[symbol]
                target_ts = reentry_data.get('target_time', 0)
                side = reentry_data.get('side')
                
                # ?寃?罹붾뱾 ?쒓컙?닿굅??洹??댄썑硫?吏꾩엯
                if candle_time >= target_ts:
                    logger.info(f"??Executing PENDING re-entry for {side.upper()} at {candle_time} (scheduled for {target_ts})")
                    self.pending_reentry.pop(symbol, None) # Reset first to avoid loop
                    
                    if not pos: # ?ъ??섏씠 鍮꾩뼱?덉뼱??吏꾩엯
                        await self.entry(symbol, side, float(k['c']))
                    else:
                        logger.warning(f"?좑툘 Pending entry skipped: Position not empty ({pos['side']})")
                else:
                    logger.info(f"??Waiting for pending re-entry: target={target_ts}, current={candle_time}")
                    # ?꾩쭅 ?쒓컙 ???먯쑝硫??대쾲 ??醫낅즺 (?좉퇋 ?좏샇 臾댁떆)
                    return 

            # ===== Kalman 諛⑺뼢 媛?몄삤湲?(李멸퀬?? =====
            kalman_direction = self.kalman_states.get(symbol, {}).get('direction')  # 'long' or 'short' or None
            
            # ?ㅼ쓬 罹붾뱾 ?쒓컙 怨꾩궛??
            next_candle_ts = candle_time + self._timeframe_to_ms(tf)

            # ===== entry_mode???곕Ⅸ 吏꾩엯 泥섎━ (Exit???쒖쇅) =====
            if active_strategy == 'utbot':
                target_sig = raw_state_sig
                entry_sig = sig
                if self.is_upbit_mode():
                    if pos and target_sig == 'short':
                        self.last_entry_reason[symbol] = (
                            f"{strategy_name} SELL 상태 감지, 청산은 exit TF `{self._get_exit_timeframe(symbol)}` 확인 후 실행"
                        )
                    elif not pos and entry_sig == 'long':
                        self.last_entry_reason[symbol] = f"{strategy_name} BUY 상태 -> 현물 매수"
                        logger.info(f"[{strategy_name}] Upbit spot entry LONG")
                        await self.entry(symbol, 'long', float(k['c']))
                    elif pos:
                        self.last_entry_reason[symbol] = (
                            f"현물 보유 중, 청산은 {strategy_name} exit TF `{self._get_exit_timeframe(symbol)}` 기준"
                        )
                    elif target_sig == 'short':
                        self.last_entry_reason[symbol] = f"{strategy_name} SELL 상태, 현물 대기"
                    else:
                        self.last_entry_reason[symbol] = f"{strategy_name} BUY 대기"
                else:
                    need_flip = pos and target_sig and (
                        (pos['side'] == 'long' and target_sig == 'short') or
                        (pos['side'] == 'short' and target_sig == 'long')
                    )

                    if need_flip:
                        flip_label = "반전 신호" if raw_strategy_sig == target_sig else "상태 동기화"
                        self._update_stateful_diag(
                            symbol,
                            stage='flip_detected',
                            strategy=strategy_name,
                            raw_state=(target_sig or 'none'),
                            raw_signal=(raw_strategy_sig or 'none'),
                            entry_sig=(entry_sig or 'none'),
                            pos_side=str(pos['side']).upper(),
                            note=flip_label
                        )
                        await self._notify_stateful_diag(symbol)
                        logger.info(f"[{strategy_name}] {flip_label}: {pos['side']} -> {target_sig}")
                        await self.exit_position(symbol, f"{strategy_name}_Flip")
                        await asyncio.sleep(1)
                        self.position_cache = None
                        check_pos = await self.get_server_position(symbol, use_cache=False)
                        if not check_pos:
                            self._update_stateful_diag(
                                symbol,
                                stage='flip_exit_done',
                                pos_side='NONE',
                                note=f"{flip_label} exit complete"
                            )
                            if entry_sig == target_sig:
                                self.last_entry_reason[symbol] = f"{strategy_name} {flip_label} -> {entry_sig.upper()} 재진입"
                                await self.entry(symbol, entry_sig, float(k['c']))
                                self._update_stateful_diag(
                                    symbol,
                                    stage='flip_reentered',
                                    entry_sig=entry_sig,
                                    pos_side=entry_sig.upper(),
                                    note='re-entry submitted'
                                )
                            else:
                                self.last_entry_reason[symbol] = f"{strategy_name} {flip_label} 청산 완료, 재진입은 필터 또는 방향 설정으로 차단"
                                self._update_stateful_diag(
                                    symbol,
                                    stage='flip_exit_only',
                                    note='re-entry blocked by filter or direction'
                                )
                                await self._notify_stateful_diag(symbol)
                        else:
                            self.last_entry_reason[symbol] = f"{strategy_name} {flip_label} 청산 미확인, 상태 재동기화 재시도 대기"
                            self.last_stateful_retry_ts[symbol] = 0.0
                            self._update_stateful_diag(
                                symbol,
                                stage='flip_still_open',
                                pos_side=str(check_pos['side']).upper(),
                                note='position still open after exit attempt'
                            )
                            await self._notify_stateful_diag(symbol, force=True)
                            logger.warning(f"[{strategy_name}] Flip re-entry skipped: position still open ({check_pos['side']})")
                    elif not pos and entry_sig:
                        self.last_entry_reason[symbol] = f"{strategy_name} 현재 상태 -> {entry_sig.upper()} 진입"
                        logger.info(f"[{strategy_name}] Stateful entry {entry_sig.upper()}")
                        await self.entry(symbol, entry_sig, float(k['c']))
                    elif not pos and target_sig:
                        self.last_entry_reason[symbol] = f"{strategy_name} 현재 상태는 {target_sig.upper()}지만 진입은 방향 설정 또는 필터로 차단"
                    elif pos:
                        self.last_entry_reason[symbol] = f"포지션 보유 중 ({pos['side'].upper()}), {strategy_name} 반대신호 대기"

            elif active_strategy == 'utbb':
                ut_signal = raw_hybrid_detail.get('ut_signal')
                bb_upper_breakout = bool(raw_hybrid_detail.get('bb_upper_breakout'))
                short_setup_ready = bool(raw_hybrid_detail.get('bb_short_setup_ready'))
                entry_sig = sig

                if pos and pos['side'] == 'long' and (ut_signal == 'short' or bb_upper_breakout):
                    exit_note = "UT short signal" if ut_signal == 'short' else "BB upper breakout"
                    self._update_stateful_diag(
                        symbol,
                        stage='flip_detected',
                        strategy=strategy_name,
                        raw_state=(raw_state_sig or 'none'),
                        raw_signal=(raw_strategy_sig or 'none'),
                        entry_sig=(entry_sig or 'none'),
                        pos_side=str(pos['side']).upper(),
                        note=f'long exit by {exit_note}'
                    )
                    await self._notify_stateful_diag(symbol)
                    logger.info(f"[{strategy_name}] LONG exit trigger: {exit_note}")
                    await self.exit_position(symbol, f"{strategy_name}_LongExit")
                    await asyncio.sleep(1)
                    self.position_cache = None
                    check_pos = await self.get_server_position(symbol, use_cache=False)
                    if not check_pos:
                        self._update_stateful_diag(
                            symbol,
                            stage='flip_exit_done',
                            pos_side='NONE',
                            note=f'long exit complete ({exit_note})'
                        )
                        if ut_signal == 'short' and short_setup_ready and entry_sig == 'short':
                            self.last_entry_reason[symbol] = f"{strategy_name} 롱 청산 후 UT 숏신호로 SHORT 재진입"
                            await self.entry(symbol, 'short', float(k['c']))
                            self._consume_utbb_short_setup(symbol)
                            self._update_stateful_diag(
                                symbol,
                                stage='flip_reentered',
                                entry_sig='short',
                                pos_side='SHORT',
                                note='re-entered short after long exit'
                            )
                        else:
                            self.last_entry_reason[symbol] = f"{strategy_name} 롱 청산 완료, 다음 진입 신호 대기"
                            await self._notify_stateful_diag(symbol)
                    else:
                        self.last_entry_reason[symbol] = f"{strategy_name} 롱 청산 미확인, 상태 재동기화 재시도 대기"
                        self.last_stateful_retry_ts[symbol] = 0.0
                        self._update_stateful_diag(
                            symbol,
                            stage='flip_still_open',
                            pos_side=str(check_pos['side']).upper(),
                            note='position still open after long exit attempt'
                        )
                        await self._notify_stateful_diag(symbol, force=True)

                elif pos and pos['side'] == 'short' and ut_signal == 'long':
                    self._update_stateful_diag(
                        symbol,
                        stage='flip_detected',
                        strategy=strategy_name,
                        raw_state=(raw_state_sig or 'none'),
                        raw_signal=(raw_strategy_sig or 'none'),
                        entry_sig=(entry_sig or 'none'),
                        pos_side=str(pos['side']).upper(),
                        note='short exit by UT long signal'
                    )
                    await self._notify_stateful_diag(symbol)
                    logger.info(f"[{strategy_name}] SHORT exit trigger: UT long signal")
                    await self.exit_position(symbol, f"{strategy_name}_ShortExit")
                    await asyncio.sleep(1)
                    self.position_cache = None
                    check_pos = await self.get_server_position(symbol, use_cache=False)
                    if not check_pos:
                        self._update_stateful_diag(
                            symbol,
                            stage='flip_exit_done',
                            pos_side='NONE',
                            note='short exit complete'
                        )
                        if entry_sig == 'long':
                            self.last_entry_reason[symbol] = f"{strategy_name} 숏 청산 후 LONG 재진입"
                            await self.entry(symbol, 'long', float(k['c']))
                            self._update_stateful_diag(
                                symbol,
                                stage='flip_reentered',
                                entry_sig='long',
                                pos_side='LONG',
                                note='re-entered long after short exit'
                            )
                        else:
                            self.last_entry_reason[symbol] = f"{strategy_name} 숏 청산 완료, LONG 신호 대기"
                            await self._notify_stateful_diag(symbol)
                    else:
                        self.last_entry_reason[symbol] = f"{strategy_name} 숏 청산 미확인, 상태 재동기화 재시도 대기"
                        self.last_stateful_retry_ts[symbol] = 0.0
                        self._update_stateful_diag(
                            symbol,
                            stage='flip_still_open',
                            pos_side=str(check_pos['side']).upper(),
                            note='position still open after short exit attempt'
                        )
                        await self._notify_stateful_diag(symbol, force=True)

                elif not pos and entry_sig == 'long':
                    self.last_entry_reason[symbol] = f"{strategy_name} UT 롱신호 -> LONG 진입"
                    logger.info(f"[{strategy_name}] New LONG entry")
                    await self.entry(symbol, 'long', float(k['c']))
                elif not pos and entry_sig == 'short':
                    self.last_entry_reason[symbol] = f"{strategy_name} BB 숏 셋업 + UT 숏신호 -> SHORT 진입"
                    logger.info(f"[{strategy_name}] New SHORT entry")
                    await self.entry(symbol, 'short', float(k['c']))
                    self._consume_utbb_short_setup(symbol)
                elif not pos and short_setup_ready:
                    self.last_entry_reason[symbol] = f"{strategy_name} BB 숏 셋업 저장 중, UT 숏신호 대기"
                elif pos:
                    self.last_entry_reason[symbol] = f"포지션 보유 중 ({pos['side'].upper()}), {strategy_name} 청산 조건 대기"

            elif active_strategy in UT_HYBRID_STRATEGIES:
                timing_label = raw_hybrid_detail.get('timing_label', UT_HYBRID_TIMING_LABELS.get(active_strategy, 'signal'))
                regime_sig = raw_state_sig
                entry_sig = sig
                need_flip = pos and regime_sig and (
                    (pos['side'] == 'long' and regime_sig == 'short') or
                    (pos['side'] == 'short' and regime_sig == 'long')
                )

                if need_flip:
                    self._update_stateful_diag(
                        symbol,
                        stage='flip_detected',
                        strategy=strategy_name,
                        raw_state=(regime_sig or 'none'),
                        raw_signal=(raw_strategy_sig or 'none'),
                        entry_sig=(entry_sig or 'none'),
                        pos_side=str(pos['side']).upper(),
                        note='UT regime flip'
                    )
                    await self._notify_stateful_diag(symbol)
                    logger.info(f"[{strategy_name}] UT regime flip: {pos['side']} -> {regime_sig}")
                    await self.exit_position(symbol, f"{strategy_name}_UTFlip")
                    await asyncio.sleep(1)
                    self.position_cache = None
                    check_pos = await self.get_server_position(symbol, use_cache=False)
                    if not check_pos:
                        self._update_stateful_diag(
                            symbol,
                            stage='flip_exit_done',
                            pos_side='NONE',
                            note='UT flip exit complete'
                        )
                        if entry_sig == regime_sig:
                            self.last_entry_reason[symbol] = (
                                f"{strategy_name} UT 반전 + {timing_label} 타이밍 -> {entry_sig.upper()} 재진입"
                            )
                            await self.entry(symbol, entry_sig, float(k['c']))
                            self._consume_ut_hybrid_timing_latch(symbol, active_strategy, entry_sig)
                            self._update_stateful_diag(
                                symbol,
                                stage='flip_reentered',
                                entry_sig=entry_sig,
                                pos_side=entry_sig.upper(),
                                note='re-entry submitted'
                            )
                        else:
                            self.last_entry_reason[symbol] = (
                                f"{strategy_name} UT 반전 청산 완료, {regime_sig.upper()} {timing_label} 타이밍 대기"
                            )
                            self._update_stateful_diag(
                                symbol,
                                stage='flip_exit_only',
                                note=f'waiting for {timing_label} timing'
                            )
                            await self._notify_stateful_diag(symbol)
                    else:
                        self.last_entry_reason[symbol] = f"{strategy_name} UT 반전 청산 미확인, 상태 재동기화 재시도 대기"
                        self.last_stateful_retry_ts[symbol] = 0.0
                        self._update_stateful_diag(
                            symbol,
                            stage='flip_still_open',
                            pos_side=str(check_pos['side']).upper(),
                            note='position still open after exit attempt'
                        )
                        await self._notify_stateful_diag(symbol, force=True)
                        logger.warning(f"[{strategy_name}] Hybrid re-entry skipped: position still open ({check_pos['side']})")
                elif not pos and entry_sig:
                    self.last_entry_reason[symbol] = f"{strategy_name} 조건 충족 -> {entry_sig.upper()} 진입"
                    logger.info(f"[{strategy_name}] New entry {entry_sig.upper()}")
                    await self.entry(symbol, entry_sig, float(k['c']))
                    self._consume_ut_hybrid_timing_latch(symbol, active_strategy, entry_sig)
                elif not pos and regime_sig:
                    self.last_entry_reason[symbol] = (
                        f"{strategy_name} UT {regime_sig.upper()} 상태, {timing_label} 타이밍 대기"
                    )
                elif pos:
                    self.last_entry_reason[symbol] = f"포지션 보유 중 ({pos['side'].upper()}), {strategy_name} UT 반전 대기"

            elif active_strategy == 'rsibb':
                if pos and raw_strategy_sig and (
                    (pos['side'] == 'long' and raw_strategy_sig == 'short') or
                    (pos['side'] == 'short' and raw_strategy_sig == 'long')
                ):
                    logger.info(f"[{strategy_name}] Flip trigger: {pos['side']} -> {raw_strategy_sig}")
                    await self.exit_position(symbol, f"{strategy_name}_Flip")
                    await asyncio.sleep(1)
                    self.position_cache = None
                    check_pos = await self.get_server_position(symbol, use_cache=False)
                    if not check_pos:
                        if sig == raw_strategy_sig:
                            self.last_entry_reason[symbol] = f"{strategy_name} 반전 신호 -> {sig.upper()} 재진입"
                            await self.entry(symbol, sig, float(k['c']))
                        else:
                            self.last_entry_reason[symbol] = f"{strategy_name} 반전 신호로 청산 완료, 재진입은 필터 또는 방향 설정으로 차단"
                    else:
                        logger.warning(f"[{strategy_name}] Flip re-entry skipped: position still open ({check_pos['side']})")
                elif not pos and sig:
                    self.last_entry_reason[symbol] = f"{strategy_name} 조건 충족 -> {sig.upper()} 진입"
                    logger.info(f"[{strategy_name}] New entry {sig.upper()}")
                    await self.entry(symbol, sig, float(k['c']))
                elif pos:
                    self.last_entry_reason[symbol] = f"포지션 보유 중 ({pos['side'].upper()}), {strategy_name} 반대신호 대기"

            elif (active_strategy in MA_STRATEGIES and entry_mode in ['cross', 'position']) or active_strategy == 'cameron':
                # Cross/Position 紐⑤뱶: Primary TF?먯꽌??"吏꾩엯(Entry)"留?泥섎━
                # 泥?궛(Exit)? Secondary TF candle (process_exit_candle)?먯꽌 泥섎━??
                
                if pos:
                    # ?대? ?ъ??섏씠 ?덉쑝硫?Entry 泥댄겕 ????(Wait for Exit TF signal)
                    # ?? Pending Re-entry???꾩뿉??泥섎━??
                    self.last_entry_reason[symbol] = f"포지션 보유 중 ({pos['side'].upper()}), 진입 대기"
                    logger.debug(f"ProcessPrimary: Position exists ({pos['side']}), waiting for Exit TF signal.")
                    
                elif not pos and sig:
                    # ?ъ????놁쓬 + 吏꾩엯 ?좏샇 -> 吏꾩엯
                    if active_strategy == 'cameron':
                        strategy_label = strategy_name
                    else:
                        strategy_label = "Cross" if entry_mode == 'cross' else "Position"
                    self.last_entry_reason[symbol] = f"{strategy_label} 조건 충족 -> {sig.upper()} 진입"
                    logger.info(f"?? {strategy_label} (Primary): New entry {sig.upper()}")
                    await self.entry(symbol, sig, float(k['c']))
            
            elif entry_mode in ['hurst_fisher', 'microvbo']:
                # FractalFisher / MicroVBO 紐⑤뱶: 湲곗〈 濡쒖쭅 ?좎? (?⑥씪 TF)
                if not pos and sig:
                    logger.info(f"?? New entry ({entry_mode.upper()}): {sig.upper()}")
                    await self.entry(symbol, sig, float(k['c']))
                elif pos and sig:
                    # ?ъ????덇퀬 諛섎? ?좏샇 ???뚮┰
                    if (pos['side'] == 'long' and sig == 'short') or (pos['side'] == 'short' and sig == 'long'):
                        logger.info(f"?봽 {entry_mode.upper()}: Flip position {pos['side']} ??{sig}")
                        await self.exit_position(symbol, f"{strategy_name}_Flip")
                        await asyncio.sleep(1)
                        self.position_cache = None
                        check_pos = await self.get_server_position(symbol, use_cache=False)
                        if not check_pos:
                            await self.entry(symbol, sig, float(k['c']))
            
            else:
                logger.debug(f"No action: pos={current_side}, sig={sig}, entry_mode={entry_mode}")
            
            self.last_candle_time[symbol] = processing_candle_time
            self.last_candle_success[symbol] = True
            logger.debug(f"??Candle processing completed successfully")
                
        except Exception as e:
            logger.error(f"Signal process_candle error: {e}")
            import traceback
            traceback.print_exc()

    async def process_exit_candle(self, symbol, tf, current_side):
        """[New] Process secondary timeframe candle for EXIT signals
           Applies EXIT filters (Kalman, R2, Chop) independently.
        """
        try:
            # Fetch history for Exit TF
            ohlcv = await asyncio.to_thread(self.market_data_exchange.fetch_ohlcv, symbol, tf, limit=300)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            # [CRITICAL] Ensure numeric types (Robust Loop)
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            
            # ===== 1. Calculate Raw Exit Signal =====
            strategy_params = self.get_runtime_strategy_params()
            active_strategy = strategy_params.get('active_strategy', 'sma').lower()
            raw_exit_long = False
            raw_exit_short = False
            c_f = c_s = 0.0

            if active_strategy == 'cameron':
                strategy_name = "CAMERON(Exit)"
                exit_sig, exit_reason, _ = self._calculate_cameron_signal(df, strategy_params)
                raw_exit_long = current_side.lower() == 'long' and exit_sig == 'short'
                raw_exit_short = current_side.lower() == 'short' and exit_sig == 'long'
                if raw_exit_long or raw_exit_short:
                    logger.info(f"[Exit Debug] {symbol} {strategy_name}: {exit_reason}")
            elif active_strategy == 'utbot':
                strategy_name = "UTBOT(Exit)"
                exit_sig, exit_reason, _ = self._calculate_utbot_signal(df, strategy_params)
                raw_exit_long = current_side.lower() == 'long' and exit_sig == 'short'
                raw_exit_short = current_side.lower() == 'short' and exit_sig == 'long'
                if raw_exit_long or raw_exit_short:
                    logger.info(f"[Exit Debug] {symbol} {strategy_name}: {exit_reason}")
            else:
                # Simple SMA/HMA Logic for Exit
                if active_strategy == 'hma':
                    p = strategy_params.get('HMA', {})
                    fast_period = p.get('fast_period', 9)
                    slow_period = p.get('slow_period', 21)
                    df['f'] = ta.hma(df['close'], length=fast_period)
                    df['s'] = ta.hma(df['close'], length=slow_period)
                    strategy_name = "HMA(Exit)"
                else:
                    p = strategy_params.get('Triple_SMA', {})
                    fast_period = p.get('fast_sma', 3)
                    slow_period = p.get('slow_sma', 10) # Fixed default to 10
                    df['f'] = ta.sma(df['close'], length=fast_period)
                    df['s'] = ta.sma(df['close'], length=slow_period)
                    strategy_name = "SMA(Exit)"

                c_f, c_s = df['f'].iloc[-2], df['s'].iloc[-2]
                p_f, p_s = df['f'].iloc[-3], df['s'].iloc[-3]

                # Check Cross (Standard Exit Trigger)
                # If Long -> Cross Down is exit signal
                if p_f > p_s and c_f < c_s:
                    raw_exit_long = True
                    logger.info(f"?뱣 [Exit Debug] {symbol} Dead Cross: {p_f:.2f}/{p_s:.2f} -> {c_f:.2f}/{c_s:.2f}")

                # If Short -> Cross Up is exit signal
                if p_f < p_s and c_f > c_s:
                    raw_exit_short = True
                    logger.info(f"?뱢 [Exit Debug] {symbol} Golden Cross: {p_f:.2f}/{p_s:.2f} -> {c_f:.2f}/{c_s:.2f}")

                if current_side.lower() == 'long':
                    if c_f < c_s: # Alignment becomes bearish
                        raw_exit_long = True
                        logger.info(f"?슟 [Exit Debug] {symbol} Bearish Alignment: {c_f:.2f} < {c_s:.2f}")
                    else:
                        logger.debug(f"?뵇 [Exit Debug] {symbol} Still Bullish: {c_f:.2f} > {c_s:.2f}")
                elif current_side.lower() == 'short':
                    if c_f > c_s: # Alignment becomes bullish
                        raw_exit_short = True
                        logger.info(f"??[Exit Debug] {symbol} Bullish Alignment: {c_f:.2f} > {c_s:.2f}")
                    else:
                        logger.debug(f"?뵇 [Exit Debug] {symbol} Still Bearish: {c_f:.2f} < {c_s:.2f}")
            
            # ?좏샇媛 ?녿뜑?쇰룄 ?꾪꽣 媛믪? 怨꾩궛?댁꽌 ??쒕낫?쒖뿉 ?낅뜲?댄듃 (??Pending 諛⑹?)
            await self._update_exit_filter_values(symbol, df, current_side)
            if not raw_exit_long and not raw_exit_short:
                return True
            
            # ===== 3. Exit Filter logic check =====
            can_exit = True
            block_reasons = []
            
            # Re-fetch the calculated values for checking
            st = self.last_exit_filter_status.get(symbol, {})
            kalman_vel = st.get('kalman_vel', 0.0)
            curr_r2 = st.get('r2_val', 0.0)
            curr_chop = st.get('chop_val', 50.0)
            
            kalman_cfg = strategy_params.get('kalman_filter', {})
            kalman_exit_enabled = kalman_cfg.get('exit_enabled', False)
            
            common_cfg = self.get_runtime_common_settings()
            r2_exit_enabled = common_cfg.get('r2_exit_enabled', True)
            r2_thresh = common_cfg.get('r2_threshold', 0.25)
            chop_exit_enabled = common_cfg.get('chop_exit_enabled', True)
            chop_thresh = common_cfg.get('chop_threshold', 50.0)

            # 1. Kalman Check
            if kalman_exit_enabled:
                if current_side.lower() == 'long':
                    # To Exit Long, Kalman must be Bearish (Velocity < 0)
                    if kalman_vel >= 0:
                        can_exit = False
                        block_reasons.append(f"Kalman(Vel={kalman_vel:.4f})>=0")
                elif current_side.lower() == 'short':
                    # To Exit Short, Kalman must be Bullish (Velocity > 0)
                    if kalman_vel <= 0:
                        can_exit = False
                        block_reasons.append(f"Kalman(Vel={kalman_vel:.4f})<=0")
            
            # 2. R2 Check (Trend Strength)
            if r2_exit_enabled:
                if curr_r2 < r2_thresh:
                    can_exit = False
                    block_reasons.append(f"R2({curr_r2:.2f})<{r2_thresh}")

            # 3. CHOP Check
            can_exit_by_chop = not chop_exit_enabled or (curr_chop <= chop_thresh)
            if chop_exit_enabled and not can_exit_by_chop:
                can_exit = False
                block_reasons.append(f"Chop({curr_chop:.1f})>{chop_thresh}")
            
            # 4. CC Check (Correlation)
            cc_exit_enabled = common_cfg.get('cc_exit_enabled', False)
            cc_thresh = common_cfg.get('cc_threshold', 0.70)
            curr_cc = st.get('cc_val', 0.0)
            can_exit_by_cc = (not cc_exit_enabled)
            if cc_exit_enabled:
                if current_side.lower() == 'long':
                    can_exit_by_cc = curr_cc < -cc_thresh
                elif current_side.lower() == 'short':
                    can_exit_by_cc = curr_cc > cc_thresh
                else:
                    can_exit_by_cc = False
                if not can_exit_by_cc:
                    can_exit = False
                    block_reasons.append(f"CC({curr_cc:.2f}) fail")
            
            # [New] Update Pass/Fail Status based on logic above
            st['kalman_pass'] = (not kalman_exit_enabled) or \
                              (current_side.lower() == 'long' and kalman_vel < 0) or \
                              (current_side.lower() == 'short' and kalman_vel > 0)
            st['r2_pass'] = (not r2_exit_enabled) or (curr_r2 >= r2_thresh)
            st['chop_pass'] = (not chop_exit_enabled) or (curr_chop <= chop_thresh)
            
            
            # ===== 5. Execute Exit =====
            # Enabled exit filters are all mandatory. If one fails, exit is blocked.

            if current_side.lower() == 'long':
                if raw_exit_long:
                    if can_exit:
                        logger.info(f"?뵒 [Exit {tf}] LONG Exit Triggered (Chop:{curr_chop:.1f}, CC:{curr_cc:.2f})")
                        await self.exit_position(symbol, f"{strategy_name}_Exit_L")
                    else:
                        why = ", ".join(block_reasons) if block_reasons else "Filter blocked"
                        logger.info(f"?썳截?[Exit {tf}] LONG Exit Blocked: {why} (Chop:{curr_chop:.1f}, CC:{curr_cc:.2f})")
                else:
                    logger.debug(f"??[Exit {tf}] LONG Exit Ignored: raw exit signal not confirmed")
            
            elif current_side.lower() == 'short':
                if raw_exit_short:
                    if can_exit:
                        logger.info(f"?뵒 [Exit {tf}] SHORT Exit Triggered (Chop:{curr_chop:.1f}, CC:{curr_cc:.2f})")
                        await self.exit_position(symbol, f"{strategy_name}_Exit_S")
                    else:
                        why = ", ".join(block_reasons) if block_reasons else "Filter blocked"
                        logger.info(f"?썳截?[Exit {tf}] SHORT Exit Blocked: {why} (Chop:{curr_chop:.1f}, CC:{curr_cc:.2f})")
                else:
                    logger.debug(f"??[Exit {tf}] SHORT Exit Ignored: raw exit signal not confirmed")
            return True
                
        except Exception as e:
            logger.error(f"Process exit candle error: {e}")
            import traceback
            traceback.print_exc()
            return False

    async def _calculate_strategy_signal(self, symbol, df, strategy_params, active_strategy, allow_utbot_stateful=True):
        """
        ?꾨왂蹂??좏샇 怨꾩궛
        
        ?ㅼ젙:
        - active_strategy: 'sma', 'hma', ?먮뒗 'microvbo'
        - entry_mode: 'cross' (援먯감) ?먮뒗 'position' (?꾩튂) - SMA/HMA??
        - kalman_filter.enabled: True/False - SMA/HMA??
        
        Returns: (signal, is_bullish, is_bearish, strategy_name, entry_mode, kalman_enabled)
        """
        sig = None
        is_bullish = False
        is_bearish = False
        entry_reason = "신호 대기"
        
        # Init state dicts if needed
        if symbol not in self.vbo_states: self.vbo_states[symbol] = {}
        if symbol not in self.fisher_states: self.fisher_states[symbol] = {}
        if symbol not in self.cameron_states: self.cameron_states[symbol] = {}
        if symbol not in self.kalman_states: self.kalman_states[symbol] = {'velocity': 0.0, 'direction': None}

        active_strategy = str(active_strategy).lower()
        if active_strategy not in CORE_STRATEGIES:
            logger.warning(f"Unsupported active_strategy '{active_strategy}' in core mode. Using SMA.")
            active_strategy = 'sma'

        entry_mode = str(strategy_params.get('entry_mode', 'cross')).lower()
        if entry_mode not in {'cross', 'position'}:
            entry_mode = 'cross'
        kalman_cfg = strategy_params.get('kalman_filter', {})
        # [MODIFIED] Separate Entry/Exit Config
        kalman_entry_enabled = kalman_cfg.get('entry_enabled', False)
        kalman_exit_enabled = kalman_cfg.get('exit_enabled', False) # Used in exit logic, but read here for consistency
        
        # [MODIFIED] Only force for Position mode entry if Entry filter is enabled? 
        # User said: "Apply Kalman regardless for Position/Cross Entry/Exit UNLESS toggled."
        # Actually user said: "Kalman was unconditional... make it toggleable for Entry ON/OFF and Exit ON/OFF."
        # So I will strictly follow the toggle. If user wants Position mode + Kalman OFF, so be it.
        # However, Position mode fundamentally relies on some trend definition. But I will trust the toggle.
        
        # NOTE: Original code FORCED kalman for 'position' and 'cross' modes.
        # New logic: Use kalman_entry_enabled for Entry signals.
        
        # ============ R2 Trend Quality Filter (Financial Engineering) ============
        common_cfg = self.get_runtime_common_settings()
        
        # 1. R2 Filter (Trend Quality)
        r2_entry_enabled = common_cfg.get('r2_entry_enabled', True)
        r2_thresh = common_cfg.get('r2_threshold', 0.25)
        curr_r2 = 0.0
        is_r2_pass = True
        
        if len(df) >= 14:
            df['idx_seq'] = np.arange(len(df))
            roll_corr = df['close'].rolling(window=14).corr(df['idx_seq'])
            df['r2'] = roll_corr ** 2
            curr_r2 = df['r2'].iloc[-2]
            if np.isnan(curr_r2): curr_r2 = 0.0
            
            # Entry Check
            if r2_entry_enabled and curr_r2 < r2_thresh:
                is_r2_pass = False

        # 2. Choppiness Index (Trend vs Chop)
        chop_entry_enabled = common_cfg.get('chop_entry_enabled', True)
        chop_thresh = common_cfg.get('chop_threshold', 50.0)
        curr_chop = 50.0
        is_chop_pass = True
        
        if len(df) >= 14:
            try:
                # pandas_ta chop: 100 * Log10(Sum(ATR, n) / (MaxHi - MinLo)) / Log10(n)
                # Use standard 14 length
                chop_series = df.ta.chop(length=14)
                if chop_series is not None:
                    curr_chop = chop_series.iloc[-2]
                    if np.isnan(curr_chop): curr_chop = 50.0
                    
                    if chop_entry_enabled and curr_chop > chop_thresh:
                        is_chop_pass = False
            except Exception as e:
                logger.warning(f"CHOP calculation error: {e}")

        # Update Status Data (Real-time monitoring)
        self.last_entry_filter_status[symbol] = {
            'r2_val': curr_r2,
            'chop_val': curr_chop,
            'r2_pass': is_r2_pass,
            'chop_pass': is_chop_pass
        }
        
        # Visual Logging
        r2_icon = "OK" if is_r2_pass else "NO"
        chop_icon = "OK" if is_chop_pass else "NO"
        
        if active_strategy in CORE_STRATEGIES:
             logger.info(f"?썳截?Filters (Entry): R2({curr_r2:.2f}{r2_icon}) CHOP({curr_chop:.1f}{chop_icon})")
             # Log Kalman too if used
             
        # ... MicroVBO / FractalFisher skipped (no changes needed) ...
        
        # ===== 1. Strategy-specific calculation state =====
        strategy_name = STRATEGY_DISPLAY_NAMES.get(active_strategy, active_strategy.upper())
        p_f = p_s = c_f = c_s = 0.0
        ma_bullish = False
        ma_bearish = False

        if active_strategy == 'hma':
            p = strategy_params.get('HMA', {})
            fast_period = p.get('fast_period', 9)
            slow_period = p.get('slow_period', 21)
            try:
                df['f'] = ta.hma(df['close'], length=fast_period)
                df['s'] = ta.hma(df['close'], length=slow_period)
                strategy_name = "HMA"
            except Exception as e:
                logger.error(f"HMA calculation error: {e}")
                return None, False, False, strategy_name, entry_mode, False

        elif active_strategy == 'sma':
            p = strategy_params.get('Triple_SMA', {})
            fast_period = p.get('fast_sma', 3)
            slow_period = p.get('slow_sma', 10) # Modified default from 33 to 10
            try:
                df['f'] = ta.sma(df['close'], length=fast_period)
                df['s'] = ta.sma(df['close'], length=slow_period)
                strategy_name = "SMA"
            except Exception as e:
                logger.error(f"SMA calculation error: {e}")
                return None, False, False, strategy_name, entry_mode, False
        
        if active_strategy in MA_STRATEGIES:
            # [-1] is open/current candle (unconfirmed), [-2] is last closed candle
            if len(df) >= 3:
                c_f = df['f'].iloc[-2]
                c_s = df['s'].iloc[-2]
                p_f = df['f'].iloc[-3]
                p_s = df['s'].iloc[-3]
                
                # Check alignment for Position Mode
                ma_bullish = c_f > c_s
                ma_bearish = c_f < c_s
            else:
                logger.warning(f"Not enough data for {strategy_name}: {len(df)}")
                return None, False, False, strategy_name, entry_mode, False

        # ===== 2. Kalman Filter 怨꾩궛 (?꾩슂?? =====
        kalman_bullish = None
        kalman_bearish = None
        
        # We assume we always calculate Kalman if Entry OR Exit is enabled, or just always for display
        # But for logic, we check the flag.
        
        # Always calculate for status/display
        kalman_vel = self._calculate_kalman_values(df, kalman_cfg)
        self.kalman_states[symbol]['velocity'] = kalman_vel
        
        # Update entry status with Kalman info
        if symbol in self.last_entry_filter_status:
            self.last_entry_filter_status[symbol]['kalman_vel'] = kalman_vel
        
        if kalman_vel > 0:
            self.kalman_states[symbol]['direction'] = 'long'
            kalman_bullish = True
            kalman_bearish = False
        elif kalman_vel < 0:
            self.kalman_states[symbol]['direction'] = 'short'
            kalman_bullish = False
            kalman_bearish = True
        else:
            self.kalman_states[symbol]['direction'] = None
            kalman_bullish = False
            kalman_bearish = False
            
        logger.info(f"?뱤 [Kalman] Velocity: {kalman_vel:.4f}, Direction: {self.kalman_states[symbol]['direction']}")
        
        # ===== 3. 吏꾩엯 紐⑤뱶蹂??좏샇 ?먮떒 (ENTRY SIGNAL) =====
        if active_strategy == 'utbot':
            entry_mode = 'utbot'
            sig, entry_reason, utbot_detail = self._calculate_utbot_signal(df, strategy_params)
            if sig is None and allow_utbot_stateful:
                bias_sig = utbot_detail.get('bias_side')
                if bias_sig in {'long', 'short'}:
                    sig = bias_sig
            is_bullish = sig == 'long'
            is_bearish = sig == 'short'

            if sig == 'long' and kalman_entry_enabled and not kalman_bullish:
                entry_reason = "UTBOT LONG 상태, Kalman 진입필터 미통과"
                sig = None
                is_bullish = False
            elif sig == 'short' and kalman_entry_enabled and not kalman_bearish:
                entry_reason = "UTBOT SHORT 상태, Kalman 진입필터 미통과"
                sig = None
                is_bearish = False

        elif active_strategy == 'utbb':
            entry_mode = 'utbb'
            sig, entry_reason, hybrid_detail = self._calculate_utbb_signal(symbol, df, strategy_params)
            ut_state = hybrid_detail.get('ut_state')
            is_bullish = ut_state == 'long'
            is_bearish = ut_state == 'short'

            if sig == 'long' and kalman_entry_enabled and not kalman_bullish:
                entry_reason = "UTBB LONG, Kalman 진입필터 미통과"
                sig = None
            elif sig == 'short' and kalman_entry_enabled and not kalman_bearish:
                entry_reason = "UTBB SHORT, Kalman 진입필터 미통과"
                sig = None

        elif active_strategy in UT_HYBRID_STRATEGIES:
            entry_mode = active_strategy
            hybrid_calc = {
                'utrsibb': self._calculate_utrsibb_signal,
                'utrsi': self._calculate_utrsi_signal
            }.get(active_strategy)
            sig, entry_reason, hybrid_detail = hybrid_calc(symbol, df, strategy_params)
            ut_state = hybrid_detail.get('ut_state')
            is_bullish = ut_state == 'long'
            is_bearish = ut_state == 'short'

            if sig == 'long' and kalman_entry_enabled and not kalman_bullish:
                entry_reason = f"{strategy_name} LONG, Kalman 진입필터 미통과"
                sig = None
            elif sig == 'short' and kalman_entry_enabled and not kalman_bearish:
                entry_reason = f"{strategy_name} SHORT, Kalman 진입필터 미통과"
                sig = None

        elif active_strategy == 'rsibb':
            entry_mode = 'rsibb'
            sig, entry_reason, _ = self._calculate_rsibb_signal(df, strategy_params)
            is_bullish = sig == 'long'
            is_bearish = sig == 'short'

            if sig == 'long' and kalman_entry_enabled and not kalman_bullish:
                entry_reason = "RSIBB LONG, Kalman 진입필터 미통과"
                sig = None
                is_bullish = False
            elif sig == 'short' and kalman_entry_enabled and not kalman_bearish:
                entry_reason = "RSIBB SHORT, Kalman 진입필터 미통과"
                sig = None
                is_bearish = False

        elif active_strategy == 'cameron':
            entry_mode = 'pattern'
            sig, entry_reason, cameron_state = self._calculate_cameron_signal(df, strategy_params)
            self.cameron_states[symbol] = cameron_state
            is_bullish = sig == 'long'
            is_bearish = sig == 'short'

            if sig == 'long' and kalman_entry_enabled and not kalman_bullish:
                entry_reason = "CAMERON LONG, Kalman 진입필터 미통과"
                sig = None
                is_bullish = False
            elif sig == 'short' and kalman_entry_enabled and not kalman_bearish:
                entry_reason = "CAMERON SHORT, Kalman 진입필터 미통과"
                sig = None
                is_bearish = False

        elif entry_mode == 'cross':
            # SMA Cross Only Logic First
            cross_up = p_f < p_s and c_f > c_s
            cross_down = p_f > p_s and c_f < c_s
            
            if cross_up:
                if kalman_entry_enabled:
                    if kalman_bullish:
                        sig = 'long'
                        entry_reason = f"{strategy_name} Cross Up + Kalman 통과"
                        logger.info(f"LONG signal: {strategy_name} Cross + Kalman Entry OK")
                    else:
                        entry_reason = f"{strategy_name} Cross Up, Kalman 진입필터 미통과"
                        logger.info(f"?썳截?Filtered: {strategy_name} Cross Up but Kalman Entry Filter Blocked")
                else:
                    sig = 'long'
                    entry_reason = f"{strategy_name} Cross Up"
                    logger.info(f"LONG signal: {strategy_name} Cross (Kalman Filter OFF)")
            
            elif cross_down:
                if kalman_entry_enabled:
                    if kalman_bearish:
                        sig = 'short'
                        entry_reason = f"{strategy_name} Cross Down + Kalman 통과"
                        logger.info(f"SHORT signal: {strategy_name} Cross + Kalman Entry OK")
                    else:
                        entry_reason = f"{strategy_name} Cross Down, Kalman 진입필터 미통과"
                        logger.info(f"?썳截?Filtered: {strategy_name} Cross Down but Kalman Entry Filter Blocked")
                else:
                    sig = 'short'
                    entry_reason = f"{strategy_name} Cross Down"
                    logger.info(f"SHORT signal: {strategy_name} Cross (Kalman Filter OFF)")
            
            else:
                entry_reason = f"{strategy_name} 크로스 없음"
                logger.debug(f"?뱣 No Cross: {strategy_name}")
        
        elif entry_mode == 'position':
            # Position Mode: MA Alignment
            if ma_bullish:
                if kalman_entry_enabled:
                    if kalman_bullish:
                        sig = 'long'
                        entry_reason = f"{strategy_name} 정배열 + Kalman 통과"
                        logger.info(f"LONG signal: {strategy_name} Position + Kalman Entry OK")
                    else:
                        entry_reason = f"{strategy_name} 정배열, Kalman 진입필터 미통과"
                else:
                    sig = 'long'
                    entry_reason = f"{strategy_name} 정배열"
                    logger.info(f"LONG signal: {strategy_name} Position (Kalman Filter OFF)")
            
            elif ma_bearish:
                if kalman_entry_enabled:
                    if kalman_bearish:
                        sig = 'short'
                        entry_reason = f"{strategy_name} 역배열 + Kalman 통과"
                        logger.info(f"SHORT signal: {strategy_name} Position + Kalman Entry OK")
                    else:
                        entry_reason = f"{strategy_name} 역배열, Kalman 진입필터 미통과"
                else:
                    sig = 'short'
                    entry_reason = f"{strategy_name} 역배열"
                    logger.info(f"SHORT signal: {strategy_name} Position (Kalman Filter OFF)")
            
            else:
                entry_reason = f"{strategy_name} 중립 구간"
                # Position Mode but neither MA Bullish nor Bearish (Neutral/Whipsaw)
                pass
                # Position mode logs are handled inside the if blocks (filtered vs signal).
                # If we are here, it means !ma_bullish and !ma_bearish (which shouldn't happen usually for bools unless equal)
                # Or Kalman filter blocked it but we handled logging inside?
                # Actually, in Position mode:
                # if ma_bullish: ...
                # elif ma_bearish: ...
                # So if neither, we do nothing.
                
                if not ma_bullish and not ma_bearish:
                     logger.debug(f"?뱣 Neutral Position (p_f={p_f:.2f}, p_s={p_s:.2f})")
                elif ma_bullish and kalman_entry_enabled and not kalman_bullish:
                     logger.debug(f"?썳截?Position Long Blocked by Kalman (Vel={kalman_vel:.4f})")
                elif ma_bearish and kalman_entry_enabled and not kalman_bearish:
                     logger.debug(f"?썳截?Position Short Blocked by Kalman (Vel={kalman_vel:.4f})")

        if active_strategy in MA_STRATEGIES:
            is_bullish = ma_bullish
            is_bearish = ma_bearish
        
        # ============ Apply Advanced Trends Filters to Entry Signal ============
        if sig and active_strategy in CORE_STRATEGIES:
            blocked_reasons = []
            if not is_r2_pass:
                blocked_reasons.append(f"R2({curr_r2:.2f})<{r2_thresh}")
            if not is_chop_pass:
                blocked_reasons.append(f"CHOP({curr_chop:.1f})>{chop_thresh}")
            
            if blocked_reasons:
                logger.info(f"?썳截?Entry Blocked by Filters: {', '.join(blocked_reasons)}")
                sig = None
                is_bullish = False
                is_bearish = False
                entry_reason = f"필터 차단: {', '.join(blocked_reasons)}"
        
        self.last_entry_reason[symbol] = entry_reason
        return sig, is_bullish, is_bearish, strategy_name, entry_mode, kalman_entry_enabled

    async def _update_exit_filter_values(self, symbol, df, current_side):
        """[Helper] Calculate exit filter values and update status without executing exit logic"""
        try:
            strategy_params = self.get_runtime_strategy_params()
            common_cfg = self.get_runtime_common_settings()
            
            # A. Kalman Filter
            kalman_cfg = strategy_params.get('kalman_filter', {})
            kalman_exit_enabled = kalman_cfg.get('exit_enabled', False)
            kalman_vel = self._calculate_kalman_values(df, kalman_cfg)
            
            # B. R2 Filter
            r2_exit_enabled = common_cfg.get('r2_exit_enabled', True)
            r2_thresh = common_cfg.get('r2_threshold', 0.25)
            curr_r2 = 0.0
            if len(df) >= 14:
                df['idx_seq'] = np.arange(len(df))
                curr_r2 = (df['close'].rolling(14).corr(df['idx_seq']).iloc[-2]) ** 2
                if np.isnan(curr_r2): curr_r2 = 0.0
            
            # C. Chop Filter
            chop_exit_enabled = common_cfg.get('chop_exit_enabled', True)
            chop_thresh = common_cfg.get('chop_threshold', 50.0)
            curr_chop = 50.0
            if len(df) >= 14 and chop_exit_enabled:
                try:
                    chop = df.ta.chop(length=14)
                    if chop is not None: 
                        curr_chop = chop.iloc[-2]
                        if np.isnan(curr_chop): curr_chop = 50.0
                    else:
                        logger.warning("?좑툘 Chop calculation returned None")
                except Exception as e:
                    logger.error(f"??Chop calculation error (Exit): {e}")
            
            # D. CC Filter (Correlation Coefficient)
            cc_exit_enabled = common_cfg.get('cc_exit_enabled', False)
            cc_thresh = common_cfg.get('cc_threshold', 0.70)
            cc_len = common_cfg.get('cc_length', 14)
            curr_cc = 0.0
            if len(df) >= cc_len:
                if 'idx_seq' not in df.columns:
                    df['idx_seq'] = np.arange(len(df))
                curr_cc = df['close'].rolling(cc_len).corr(df['idx_seq']).iloc[-2]
                if np.isnan(curr_cc): curr_cc = 0.0
            
            # Debug: CC Pass calculation
            cc_pass_result = (not cc_exit_enabled) or \
                           (current_side.lower() == 'long' and curr_cc < -cc_thresh) or \
                           (current_side.lower() == 'short' and curr_cc > cc_thresh)
            logger.debug(f"[CC Debug] enabled={cc_exit_enabled}, side={current_side}, cc={curr_cc:.3f}, thresh={cc_thresh}, pass={cc_pass_result}")

            # Update Status Data for Dashboard
            self.last_exit_filter_status[symbol] = {
                'r2_val': curr_r2,
                'chop_val': curr_chop,
                'kalman_vel': kalman_vel,
                'r2_pass': (not r2_exit_enabled) or (curr_r2 >= r2_thresh),
                'chop_pass': (not chop_exit_enabled) or (curr_chop <= chop_thresh),
                'cc_val': curr_cc,
                'cc_pass': (not cc_exit_enabled) or \
                           (current_side.lower() == 'long' and curr_cc < -cc_thresh) or \
                           (current_side.lower() == 'short' and curr_cc > cc_thresh),
                'kalman_pass': (not kalman_exit_enabled) or \
                              (current_side.lower() == 'long' and kalman_vel < 0) or \
                              (current_side.lower() == 'short' and kalman_vel > 0)
            }
        except Exception as e:
            logger.error(f"Error updating exit filter values for {symbol}: {e}")

    async def entry(self, symbol, side, price):
        try:
            if self.is_upbit_mode():
                await self._entry_upbit_spot(symbol, side, price)
                return

            # === [Single Position Enforcement] ===
            # ?대? ?ㅻⅨ ?ъ??섏씠 ?덈뒗吏 ?뺤씤 (?꾩껜 ?щ낵 ?ㅼ틪)
            # Volume Scanner ???대뼡 湲곕뒫???곕뜑?쇰룄 ?대? ?ъ??섏씠 ?덉쑝硫?異붽? 吏꾩엯 李⑤떒
            try:
                all_positions = await asyncio.to_thread(self.exchange.fetch_positions)
                for p in all_positions:
                    if float(p.get('contracts', 0)) > 0:
                        active_sym = p.get('symbol', '').replace(':USDT', '').replace('/', '')
                        target_sym = symbol.replace(':USDT', '').replace('/', '')
                        
                        if active_sym != target_sym:
                            logger.warning(f"?슟 [Single Limit] Entry blocked: Already holding {p['symbol']}")
                            await self.ctrl.notify(f"⚠️ **진입 차단**: 단일 포지션 제한 (보유 중: {p['symbol']})")
                            return
            except Exception as e:
                logger.error(f"Single position check failed: {e}")
                return # ?덉쟾???꾪빐 ?뺤씤 ?ㅽ뙣 ??吏꾩엯 以묐떒

            logger.info(f"?뱿 [Signal] Attempting {side.upper()} entry @ {price}")
            
            cfg = self.get_runtime_common_settings()
            lev = int(max(1.0, float(cfg.get('leverage', 10) or 10)))
            req_risk_pct = float(cfg.get('risk_per_trade_pct', 10.0) or 10.0)
            max_risk_pct = float(cfg.get('max_risk_per_trade_pct', 100.0) or 100.0)
            max_risk_pct = min(100.0, max(1.0, max_risk_pct))
            bounded_risk_pct = min(max(req_risk_pct, 1.0), max_risk_pct)
            risk_pct = bounded_risk_pct / 100.0
            
            _, free, _ = await self.get_balance_info()
            
            if free <= 0:
                logger.warning(f"Insufficient balance: {free}")
                await self.ctrl.notify(f"⚠️ 잔고 부족: ${free:.2f}")
                return

            def _safe_float(v):
                try:
                    if v is None or v == '':
                        return None
                    f = float(v)
                    return f if f > 0 else None
                except (TypeError, ValueError):
                    return None

            def _pick_min_positive(values):
                parsed = [x for x in (_safe_float(v) for v in values) if x is not None]
                return min(parsed) if parsed else 0.0
            
            # Position sizing (user-friendly):
            # 1) Use configured % of current free USDT as margin.
            # 2) Apply leverage to that margin to get position notional.
            # 3) Keep a small safety buffer to avoid -2019 by fees/slippage.
            margin_to_use = free * risk_pct
            safety_buffer = 0.98
            target_notional = margin_to_use * lev * safety_buffer

            # Exchange min notional check (prevents Binance -4164).
            min_notional = 0.0
            try:
                markets = await asyncio.to_thread(self.exchange.load_markets)
                market = markets.get(symbol) or markets.get(f"{symbol}:USDT") or {}
                limits = market.get('limits', {}) if isinstance(market, dict) else {}
                info = market.get('info', {}) if isinstance(market, dict) else {}
                filters = info.get('filters', []) if isinstance(info, dict) else []
                filter_values = []
                if isinstance(filters, list):
                    for f in filters:
                        if not isinstance(f, dict):
                            continue
                        if f.get('filterType') in ('MIN_NOTIONAL', 'NOTIONAL'):
                            filter_values.extend([f.get('notional'), f.get('minNotional')])
                min_notional = _pick_min_positive([
                    limits.get('cost', {}).get('min') if isinstance(limits.get('cost', {}), dict) else None,
                    info.get('notional'),
                    info.get('minNotional'),
                    *filter_values,
                ])
            except Exception as e:
                logger.debug(f"Entry min notional lookup failed for {symbol}: {e}")

            # Binance futures fallback (some responses omit min notional filter fields).
            if min_notional <= 0 and getattr(self.exchange, 'id', '') == 'binance':
                default_type = str(getattr(self.exchange, 'options', {}).get('defaultType', '')).lower()
                if default_type in ('future', 'futures'):
                    min_notional = 100.0

            max_notional = free * lev * safety_buffer
            if min_notional > 0 and target_notional < min_notional:
                # If balance/leverage can support exchange minimum, auto-bump notional.
                if max_notional >= min_notional:
                    target_notional = min_notional * 1.001  # small buffer for precision truncation
                    margin_to_use = target_notional / max(lev * safety_buffer, 1e-9)
                    implied_risk_pct = (margin_to_use / max(free, 1e-9)) * 100.0
                    await self.ctrl.notify(
                        f"ℹ️ 최소 주문금액 반영: {target_notional:.2f} USDT "
                        f"(요구 {min_notional:.2f}, 계산 리스크 {implied_risk_pct:.1f}%)"
                    )
                else:
                    required_risk_pct = (min_notional / max(max_notional, 1e-9)) * 100.0
                    logger.warning(
                        f"Entry blocked by min notional: target={target_notional:.2f}, "
                        f"min={min_notional:.2f}, max={max_notional:.2f}, free={free:.2f}, lev={lev}"
                    )
                    await self.ctrl.notify(
                        f"⚠️ 최소 주문금액 미달: 필요 {min_notional:.2f} USDT, 가능 {max_notional:.2f} USDT. "
                        f"잔고를 늘리거나 레버리지를 높이세요(필요 리스크 약 {required_risk_pct:.1f}%)."
                    )
                    return

            qty = self.safe_amount(symbol, target_notional / price)
            try:
                qty_notional = float(qty) * float(price)
            except (TypeError, ValueError):
                qty_notional = 0.0
            if min_notional > 0 and qty_notional < min_notional:
                logger.warning(
                    f"Entry quantity below min notional after precision: qty={qty}, "
                    f"notional={qty_notional:.4f}, min={min_notional:.4f}"
                )
                await self.ctrl.notify(
                    f"⚠️ 주문 수량 정밀도 때문에 최소 금액 미달({qty_notional:.2f} < {min_notional:.2f}). "
                    "레버리지/진입비율을 높여주세요."
                )
                return
            
            if float(qty) <= 0:
                logger.warning(f"Invalid quantity: {qty} (free={free}, risk={risk_pct}, lev={lev}, price={price}, target_notional={target_notional})")
                await self.ctrl.notify(f"⚠️ 주문 수량 계산 오류: {qty} (잔고: {free:.2f})")
                return
            
            if bounded_risk_pct != req_risk_pct:
                await self.ctrl.notify(f"⚠️ 리스크 상한 적용: {req_risk_pct:.1f}% -> {bounded_risk_pct:.1f}%")
            logger.info(
                f"Entry params: qty={qty}, lev={lev}x, risk={bounded_risk_pct:.1f}% "
                f"(free={free:.2f}, margin_to_use={margin_to_use:.2f}, target_notional={target_notional:.2f}, safety={safety_buffer:.2f})"
            )
            
            # [Enforce] Market Settings (Isolated + Leverage)
            await self.ensure_market_settings(symbol, leverage=lev)
            
            # await asyncio.to_thread(self.exchange.set_leverage, lev, symbol) # Redundant, handled above
            order = await asyncio.to_thread(
                self.exchange.create_order, symbol, 'market', 
                'buy' if side == 'long' else 'sell', qty
            )
            
            # 罹먯떆 ?꾩쟾 臾댄슚??
            self.position_cache = None
            self.position_cache_time = 0
            
            self.db.log_trade_entry(symbol, side, price, float(qty))
            await self.ctrl.notify(f"✅ [Signal] {side.upper()} {qty} @ {price:.2f}")
            logger.info(f"??Entry order success: {order.get('id', 'N/A')}")
            
            # ?꾨왂 ?뚮씪誘명꽣 濡쒕뱶
            strategy_params = self.get_runtime_strategy_params()
            active_strategy = strategy_params.get('active_strategy', '').lower()
            
            # 吏꾩엯 ???ъ????뺤씤?섏뿬 ?뺥솗??吏꾩엯媛 ?뚯븙
            await asyncio.sleep(1)
            self.position_cache = None
            verify_pos = await self.get_server_position(symbol, use_cache=False)
            actual_entry_price = float(verify_pos['entryPrice']) if verify_pos else price
            
            # ===== ?꾨왂蹂?TP/SL ?ㅼ젙 =====
            if active_strategy == 'microvbo':
                # MicroVBO: ATR 湲곕컲 TP/SL 二쇰Ц
                if self.vbo_breakout_level:
                    self.vbo_entry_price = actual_entry_price
                    self.vbo_entry_atr = self.vbo_breakout_level.get('atr', 0)
                    vbo_cfg = strategy_params.get('MicroVBO', {})
                    tp_mult = vbo_cfg.get('tp_atr_multiplier', 1.0)
                    sl_mult = vbo_cfg.get('sl_atr_multiplier', 0.5)
                    
                    await self._place_tp_sl_orders(symbol, side, actual_entry_price, qty,
                                                   self.vbo_entry_atr * tp_mult, 
                                                   self.vbo_entry_atr * sl_mult)
                    logger.info(f"?뮶 [MicroVBO] Entry state saved: price={actual_entry_price:.2f}, ATR={self.vbo_entry_atr:.2f}")
            
            elif active_strategy == 'fractalfisher':
                # FractalFisher: Trailing Stop (?뚰봽?몄썾??湲곕컲 ?좎? - ?숈쟻?대?濡?
                if self.fisher_entry_atr:
                    self.fisher_entry_price = actual_entry_price
                    ff_cfg = strategy_params.get('FractalFisher', {})
                    trailing_mult = ff_cfg.get('atr_trailing_multiplier', 2.0)
                    if side == 'long':
                        self.fisher_trailing_stop = actual_entry_price - (self.fisher_entry_atr * trailing_mult)
                    else:
                        self.fisher_trailing_stop = actual_entry_price + (self.fisher_entry_atr * trailing_mult)
                    logger.info(f"?뮶 [FractalFisher] Entry state: price={actual_entry_price:.2f}, TrailingStop={self.fisher_trailing_stop:.2f}")
                    await self.ctrl.notify(f"🧭 Trailing Stop 설정: {self.fisher_trailing_stop:.2f}")

            elif active_strategy == 'cameron':
                cam_state = self.cameron_states.get(symbol, {})
                cam_cfg = strategy_params.get('Cameron', {})
                rr_ratio = float(cam_cfg.get('risk_reward_ratio', 2.0) or 2.0)
                stop_price = float(cam_state.get('stop_price', 0.0) or 0.0)
                rr_applied = False

                if side == 'long' and stop_price > 0:
                    risk_distance = actual_entry_price - stop_price
                elif side == 'short' and stop_price > 0:
                    risk_distance = stop_price - actual_entry_price
                else:
                    risk_distance = 0.0

                if risk_distance > 0:
                    tp_distance = risk_distance * rr_ratio
                    await self._place_tp_sl_orders(
                        symbol,
                        side,
                        actual_entry_price,
                        qty,
                        tp_distance=tp_distance,
                        sl_distance=risk_distance
                    )
                    rr_applied = True
                    await self.ctrl.notify(
                        f"🎯 CAMERON RR {rr_ratio:.1f}:1 적용 "
                        f"(SL 기준 {risk_distance / max(actual_entry_price, 1e-9) * 100:.2f}%)"
                    )
                    logger.info(
                        f"[CAMERON] RR protection set: entry={actual_entry_price:.4f}, "
                        f"stop={stop_price:.4f}, risk={risk_distance:.4f}, rr={rr_ratio:.2f}"
                    )

                if not rr_applied:
                    logger.warning(f"[CAMERON] Invalid stop anchor for {symbol}. Falling back to default TP/SL.")
                    tp_master_enabled = bool(cfg.get('tp_sl_enabled', True))
                    tp_enabled = tp_master_enabled and bool(cfg.get('take_profit_enabled', True))
                    sl_enabled = tp_master_enabled and bool(cfg.get('stop_loss_enabled', True))
                    if tp_enabled or sl_enabled:
                        tp_pct = cfg.get('target_roe_pct', 20.0) / 100.0 / lev
                        sl_pct = cfg.get('stop_loss_pct', 10.0) / 100.0 / lev

                        tp_distance = (actual_entry_price * tp_pct) if (tp_enabled and tp_pct > 0) else None
                        sl_distance = (actual_entry_price * sl_pct) if (sl_enabled and sl_pct > 0) else None

                        if tp_distance is not None or sl_distance is not None:
                            await self._place_tp_sl_orders(
                                symbol,
                                side,
                                actual_entry_price,
                                qty,
                                tp_distance=tp_distance,
                                sl_distance=sl_distance
                            )
            
            else:
                # SMA/HMA: ?쇱꽱??湲곕컲 TP/SL 二쇰Ц
                tp_master_enabled = bool(cfg.get('tp_sl_enabled', True))
                tp_enabled = tp_master_enabled and bool(cfg.get('take_profit_enabled', True))
                sl_enabled = tp_master_enabled and bool(cfg.get('stop_loss_enabled', True))
                if tp_enabled or sl_enabled:
                    tp_pct = cfg.get('target_roe_pct', 20.0) / 100.0 / lev  # ROE瑜?媛寃?蹂?숇쪧濡?蹂??
                    sl_pct = cfg.get('stop_loss_pct', 10.0) / 100.0 / lev

                    tp_distance = (actual_entry_price * tp_pct) if (tp_enabled and tp_pct > 0) else None
                    sl_distance = (actual_entry_price * sl_pct) if (sl_enabled and sl_pct > 0) else None

                    if tp_distance is not None or sl_distance is not None:
                        await self._place_tp_sl_orders(
                            symbol,
                            side,
                            actual_entry_price,
                            qty,
                            tp_distance=tp_distance,
                            sl_distance=sl_distance
                        )
            
            if verify_pos:
                logger.info(f"??Position verified: {verify_pos['side']} {verify_pos['contracts']}")
            else:
                logger.warning("?좑툘 Position not found after entry (may take time to update)")
            
        except Exception as e:
            logger.error(f"Signal entry error: {e}")
            import traceback
            traceback.print_exc()
            await self.ctrl.notify(f"❌ 진입 실패: {e}")

    async def _entry_upbit_spot(self, symbol, side, price):
        if side != 'long':
            logger.info(f"Upbit spot entry ignored: side={side}")
            return

        try:
            cfg = self.get_runtime_common_settings()
            min_order_krw = max(5000.0, float(cfg.get('min_order_krw', 5000.0) or 5000.0))
            active_symbols = await self.get_active_position_symbols(use_cache=False)
            for active_symbol in sorted(active_symbols):
                if active_symbol != symbol:
                    active_pos = await self.get_server_position(active_symbol, use_cache=False)
                    active_qty = abs(float((active_pos or {}).get('contracts', 0) or 0))
                    active_price = float((active_pos or {}).get('markPrice', 0) or (active_pos or {}).get('entryPrice', 0) or 0)
                    active_notional = active_qty * active_price
                    if active_notional < min_order_krw:
                        logger.info(
                            f"[Upbit] Dust holding ignored for entry block: {active_symbol} value={active_notional:,.0f} KRW"
                        )
                        continue
                    display_active = self.ctrl.format_symbol_for_display(active_symbol)
                    await self.ctrl.notify(f"⚠️ **진입 차단**: 업비트 단일 보유 제한 (보유 중: {display_active})")
                    return

            req_risk_pct = float(cfg.get('risk_per_trade_pct', 10.0) or 10.0)
            max_risk_pct = float(cfg.get('max_risk_per_trade_pct', 100.0) or 100.0)
            max_risk_pct = min(100.0, max(1.0, max_risk_pct))
            bounded_risk_pct = min(max(req_risk_pct, 1.0), max_risk_pct)
            risk_pct = bounded_risk_pct / 100.0

            _, free_krw, _ = await self.get_balance_info()
            if free_krw <= 0:
                await self.ctrl.notify(f"⚠️ 업비트 KRW 잔고 부족: {free_krw:,.0f} KRW")
                return

            safety_buffer = 0.997
            target_notional = free_krw * risk_pct * safety_buffer
            if target_notional < min_order_krw:
                if free_krw >= min_order_krw:
                    target_notional = min_order_krw
                else:
                    await self.ctrl.notify(
                        f"⚠️ 업비트 최소 주문금액 미달: 필요 {min_order_krw:,.0f} KRW, 가용 {free_krw:,.0f} KRW"
                    )
                    return

            qty = self.safe_amount(symbol, target_notional / max(float(price), 1e-9))
            if float(qty) <= 0:
                await self.ctrl.notify(f"⚠️ 업비트 주문 수량 계산 오류: {qty}")
                return

            if bounded_risk_pct != req_risk_pct:
                await self.ctrl.notify(f"⚠️ 업비트 리스크 상한 적용: {req_risk_pct:.1f}% -> {bounded_risk_pct:.1f}%")

            await self.ensure_market_settings(symbol, leverage=1)
            order = await asyncio.to_thread(
                self.exchange.create_order,
                symbol,
                'market',
                'buy',
                float(qty),
                float(price)
            )

            self.position_cache = None
            self.position_cache_time = 0
            self.db.log_trade_entry(symbol, 'long', price, float(qty))

            display_symbol = self.ctrl.format_symbol_for_display(symbol)
            await self.ctrl.notify(
                f"✅ [Upbit] BUY {display_symbol} {qty}\n예상 체결가: {price:,.0f} KRW | 주문금액 약 {target_notional:,.0f} KRW"
            )

            await asyncio.sleep(1)
            self.position_cache = None
            verify_pos = await self.get_server_position(symbol, use_cache=False)
            if verify_pos:
                logger.info(
                    f"[Upbit] Position verified: {display_symbol} qty={verify_pos['contracts']} avg={verify_pos['entryPrice']}"
                )
            else:
                logger.warning(f"[Upbit] Position not found after buy: {display_symbol}, order={order}")
        except Exception as e:
            logger.error(f"Upbit spot entry error: {e}")
            await self.ctrl.notify(f"❌ 업비트 매수 실패: {e}")

    async def _place_tp_sl_orders(self, symbol, side, entry_price, qty, tp_distance=None, sl_distance=None):
        """嫄곕옒?뚯뿉 TP/SL 二쇰Ц 諛곗튂 (?ㅽ뵂?ㅻ뜑??蹂댁엫)"""
        try:
            if self.is_upbit_mode():
                logger.info("TP/SL order placement skipped in Upbit spot mode.")
                return

            tp_order = None
            sl_order = None
            tp_price = None
            sl_price = None

            if side == 'long':
                tp_side = 'sell'
                sl_side = 'sell'
                if tp_distance is not None and tp_distance > 0:
                    tp_price = self.safe_price(symbol, entry_price + tp_distance)
                if sl_distance is not None and sl_distance > 0:
                    sl_price = self.safe_price(symbol, entry_price - sl_distance)
            else:
                tp_side = 'buy'
                sl_side = 'buy'
                if tp_distance is not None and tp_distance > 0:
                    tp_price = self.safe_price(symbol, entry_price - tp_distance)
                if sl_distance is not None and sl_distance > 0:
                    sl_price = self.safe_price(symbol, entry_price + sl_distance)

            # Take Profit 二쇰Ц (吏?뺢? + reduceOnly)
            if tp_price is not None:
                try:
                    tp_order = await asyncio.to_thread(
                        self.exchange.create_order, symbol, 'limit', tp_side, qty, tp_price,
                        {'reduceOnly': True}
                    )
                    logger.info(f"??TP order placed: {tp_side.upper()} @ {tp_price}")
                except Exception as tp_e:
                    logger.error(f"TP order failed: {tp_e}")

            # Stop Loss 二쇰Ц (?ㅽ깙 留덉폆 + reduceOnly)
            if sl_price is not None:
                try:
                    sl_order = await asyncio.to_thread(
                        self.exchange.create_order, symbol, 'stop_market', sl_side, qty, None,
                        {'stopPrice': sl_price, 'reduceOnly': True}
                    )
                    logger.info(f"??SL order placed: {sl_side.upper()} @ {sl_price} (stop)")
                except Exception as sl_e:
                    logger.error(f"SL order failed: {sl_e}")
            
            notice_parts = []
            if tp_order and tp_price is not None:
                notice_parts.append(f"🎯 TP: `{float(tp_price):.2f}`")
            if sl_order and sl_price is not None:
                notice_parts.append(f"🛑 SL: `{float(sl_price):.2f}`")
            if notice_parts:
                await self.ctrl.notify(" | ".join(notice_parts))
            
        except Exception as e:
            logger.error(f"TP/SL order placement error: {e}")

    async def exit_position(self, symbol, reason):
        logger.info(f"?뱾 [Signal] Attempting exit: {reason}")
        if self.is_upbit_mode():
            await self._exit_upbit_spot(symbol, reason)
            return
        
        # 癒쇱? TP/SL 二쇰Ц 痍⑥냼 (?덈뒗 寃쎌슦)
        try:
            await asyncio.to_thread(self.exchange.cancel_all_orders, symbol)
            logger.info(f"??All orders cancelled for {symbol}")
        except Exception as cancel_e:
            logger.warning(f"Order cancellation failed (may have none): {cancel_e}")
        
        # 罹먯떆 臾댄슚?????ъ????뺤씤
        self.position_cache = None
        pos = await self.get_server_position(symbol, use_cache=False)
        if not pos:
            logger.info("No position to exit")
            return
        
        contracts = abs(float(pos['contracts']))
        if contracts <= 0:
            logger.info("No contracts to exit")
            return
        
        qty = self.safe_amount(symbol, contracts)
        side = 'sell' if pos['side'] == 'long' else 'buy'
        
        logger.info(f"Exit params: {side} {qty} (position: {pos['side']} {contracts})")
        
        # ?ъ떆??濡쒖쭅 (理쒕? 3??
        max_retries = 3
        order = None
        last_error = None
        
        for attempt in range(max_retries):
            try:
                order = await asyncio.to_thread(
                    self.exchange.create_order, symbol, 'market', side, qty, None,
                    {'reduceOnly': True}
                )
                break  # ?깃났 ??猷⑦봽 ?덉텧
            except Exception as e:
                last_error = e
                logger.error(f"Exit attempt {attempt + 1}/{max_retries} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)  # 1珥??湲????ъ떆??
                    await self.ctrl.notify(f"⚠️ 청산 재시도 중... ({attempt + 2}/{max_retries})")
        
        if not order:
            logger.error(f"??Exit failed after {max_retries} attempts, trying force close...")
            await self.ctrl.notify("🚨 청산 실패! 강제 청산을 시도합니다...")
            
            # 媛뺤젣 泥?궛: 紐⑤뱺 二쇰Ц 痍⑥냼 ??reduceOnly濡??ъ떆??
            try:
                await asyncio.to_thread(self.exchange.cancel_all_orders, symbol)
                await asyncio.sleep(0.5)
                
                # reduceOnly ?듭뀡?쇰줈 媛뺤젣 泥?궛
                order = await asyncio.to_thread(
                    self.exchange.create_order, symbol, 'market', side, qty, None,
                    {'reduceOnly': True}
                )
                await self.ctrl.notify("✅ 강제 청산 성공")
            except Exception as force_e:
                logger.error(f"Force close also failed: {force_e}")
                await self.ctrl.notify(f"🚨 강제 청산도 실패했습니다. 즉시 수동 청산이 필요합니다: {force_e}")
                return
        
        pnl = float(pos.get('unrealizedPnl', 0))
        pnl_pct = float(pos.get('percentage', 0))
        exit_price = float(order.get('average', 0)) if order.get('average') else float(pos.get('markPrice', 0))
        
        # 罹먯떆 ?꾩쟾 臾댄슚??
        self.position_cache = None
        self.position_cache_time = 0
        
        self.db.log_trade_close(symbol, pnl, pnl_pct, exit_price, reason)
        await self.ctrl.notify(f"📊 [{reason}] PnL: {pnl:+.2f} ({pnl_pct:+.2f}%)")
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


class ShannonEngine(BaseEngine):
    def __init__(self, controller):
        super().__init__(controller)
        self.last_logic_time = 0
        self.last_indicator_update = 0
        self.ratio = controller.cfg.get('shannon_engine', {}).get('asset_allocation', {}).get('target_ratio', 0.5)
        self.grid_orders = []
        
        # 吏??罹먯떆
        self.ema_200 = None
        self.atr_value = None
        self.trend_direction = None  # 'long', 'short', or None
        self.INDICATOR_UPDATE_INTERVAL = 10  # 10珥덈쭏??吏??媛깆떊

    def start(self):
        super().start()
        # ?ъ떆????吏??罹먯떆 珥덇린??
        self.last_logic_time = 0
        self.last_indicator_update = 0
        self.ema_200 = None
        self.atr_value = None
        self.trend_direction = None
        logger.info(f"?? [Shannon] Engine started and cache cleared")

    async def poll_tick(self):
        """
        [?쒖닔 ?대쭅] Shannon ?붿쭊 硫붿씤 ?대쭅 ?⑥닔
        - ?꾩옱 媛寃?議고쉶
        - 由щ갭?곗떛 濡쒖쭅 ?ㅽ뻾
        """
        if not self.running:
            return
        
        try:
            symbol = self._get_target_symbol()
            
            # ?꾩옱 媛寃?議고쉶 (ticker ?ъ슜)
            ticker = await asyncio.to_thread(self.exchange.fetch_ticker, symbol)
            price = float(ticker['last'])
            
            # 由щ갭?곗떛 濡쒖쭅 ?ㅽ뻾 (1珥?媛꾧꺽 ?쒗븳)
            if time.time() - self.last_logic_time > 1.0:
                await self.logic(symbol, price)
                self.last_logic_time = time.time()
                
        except Exception as e:
            logger.error(f"Shannon poll_tick error: {e}")
    
    def _get_target_symbol(self):
        """config?먯꽌 target_symbol 媛?몄삤湲?"""
        return self.cfg.get('shannon_engine', {}).get('target_symbol', 'BTC/USDT')

    async def update_indicators(self, symbol):
        """200 EMA? ATR ?낅뜲?댄듃"""
        now = time.time()
        
        # 泥??ㅽ뻾?닿굅??罹먯떆 留뚮즺 ???낅뜲?댄듃
        # trend_direction??None?대㈃ 媛뺤젣 ?낅뜲?댄듃 (泥?吏꾩엯 ?꾪빐)
        if self.trend_direction is not None and now - self.last_indicator_update < self.INDICATOR_UPDATE_INTERVAL:
            return  # 罹먯떆 ?ъ슜
        
        try:
            cfg = self.cfg.get('shannon_engine', {})
            # Shannon??timeframe ?ㅼ젙???놁쑝硫?signal_engine ?ㅼ젙 ?ъ슜
            tf = cfg.get('timeframe') or self.cfg.get('signal_engine', {}).get('common_settings', {}).get('timeframe', '15m')
            
            # OHLCV ?곗씠??媛?몄삤湲?(200 EMA 怨꾩궛???꾪빐 理쒖냼 250媛?
            ohlcv = await asyncio.to_thread(
                self.exchange.fetch_ohlcv, symbol, tf, limit=250
            )
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            # 200 EMA 怨꾩궛
            trend_cfg = cfg.get('trend_filter', {})
            if trend_cfg.get('enabled', True):
                ema_period = trend_cfg.get('ema_period', 200)
                df['ema'] = ta.ema(df['close'], length=ema_period)
                self.ema_200 = df['ema'].iloc[-1]
                # trend_direction? logic()?먯꽌 ?ㅼ떆媛?媛寃⑹쑝濡?寃곗젙
                logger.info(f"?뱤 {tf} 200 EMA ?낅뜲?댄듃: {self.ema_200:.2f}")
            
            # ATR 怨꾩궛
            atr_cfg = cfg.get('atr_settings', {})
            if atr_cfg.get('enabled', True):
                atr_period = atr_cfg.get('period', 14)
                df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=atr_period)
                self.atr_value = df['atr'].iloc[-1]
                logger.info(f"?뱤 ATR({atr_period}): {self.atr_value:.2f}")
            
            self.last_indicator_update = now
            
        except Exception as e:
            logger.error(f"Indicator update error: {e}")

    def get_drawdown_multiplier(self, total_equity, daily_pnl):
        """?쒕줈?곕떎??湲곕컲 由ъ뒪???뱀닔 怨꾩궛"""
        cfg = self.cfg.get('shannon_engine', {}).get('drawdown_protection', {})
        
        if not cfg.get('enabled', True):
            return 1.0
        
        threshold_pct = cfg.get('threshold_pct', 3.0)
        reduction_factor = cfg.get('reduction_factor', 0.5)
        
        # ?쇱씪 ?먯떎瑜?怨꾩궛
        if total_equity > 0:
            daily_loss_pct = abs(min(0, daily_pnl)) / total_equity * 100
        else:
            daily_loss_pct = 0
        
        if daily_loss_pct >= threshold_pct:
            logger.warning(f"?좑툘 Drawdown protection: {daily_loss_pct:.2f}% loss ??{reduction_factor*100:.0f}% size")
            return reduction_factor
        
        return 1.0

    async def logic(self, symbol, price):
        try:
            # 吏???낅뜲?댄듃
            await self.update_indicators(symbol)
            
            total, free, mmr = await self.get_balance_info()
            count, daily_pnl = self.db.get_daily_stats()
            pos = await self.get_server_position(symbol)
            
            # ?쒕줈?곕떎???뱀닔 怨꾩궛
            dd_multiplier = self.get_drawdown_multiplier(total, daily_pnl)
            
            coin_amt = float(pos['contracts']) if pos else 0.0
            coin_val = abs(coin_amt * price)
            diff = coin_val - (total * self.ratio)
            diff_pct = (diff / total * 100) if total > 0 else 0

            # ?곹깭 ?곗씠???낅뜲?댄듃 (Symbol Keyed)
            self.ctrl.status_data[symbol] = {
                'engine': 'Shannon', 'symbol': symbol, 'price': price,
                'total_equity': total, 'free_usdt': free, 'mmr': mmr,
                'daily_count': count, 'daily_pnl': daily_pnl,
                'ratio': self.ratio, 'diff_pct': diff_pct, 'coin_val': coin_val,
                'pos_side': pos['side'].upper() if pos else 'NONE',
                'entry_price': float(pos['entryPrice']) if pos else 0.0,
                'coin_amt': coin_amt,
                'pnl_pct': float(pos['percentage']) if pos else 0.0,
                'pnl_usdt': float(pos['unrealizedPnl']) if pos else 0.0,
                'grid_orders': len(self.grid_orders),
                'ema_200': self.ema_200,
                'atr': self.atr_value,
                'trend': self.trend_direction,
                'dd_multiplier': dd_multiplier
            }

            # MMR 寃쎄퀬 泥댄겕
            await self.check_mmr_alert(mmr)

            if self.ctrl.is_paused:
                return
            
            # ?쇱씪 ?먯떎 ?쒕룄 泥댄겕
            if await self.check_daily_loss_limit():
                return
            
            cfg = self.cfg.get('shannon_engine', {})
            trend_cfg = cfg.get('trend_filter', {})
            
            # ============ ?ㅼ떆媛?媛寃?湲곗? 異붿꽭 諛⑺뼢 寃곗젙 ============
            if trend_cfg.get('enabled', True) and self.ema_200 is not None:
                if price > self.ema_200:
                    self.trend_direction = 'long'
                else:
                    self.trend_direction = 'short'
                logger.info(f"?뱤 ?꾩옱媛 {price:.2f} vs 200 EMA {self.ema_200:.2f} ??{self.trend_direction.upper()}")
            
            # ============ 珥덇린 吏꾩엯 濡쒖쭅 (200 EMA 湲곕컲) ============
            # ?ъ??섏씠 ?놁쓣 ??200 EMA 諛⑺뼢?쇰줈 珥덇린 吏꾩엯
            if not pos and trend_cfg.get('enabled', True) and self.trend_direction:
                # 留ㅻℓ 諛⑺뼢 ?꾪꽣 ?곸슜
                d_mode = self.cfg.get('system_settings', {}).get('trade_direction', 'both')
                if (d_mode == 'long' and self.trend_direction == 'short') or \
                   (d_mode == 'short' and self.trend_direction == 'long'):
                    logger.info(f"??[Shannon] Entry blocked by direction filter: {d_mode}")
                    return  # ?꾪꽣???섑빐 吏꾩엯 李⑤떒
                
                target_value = total * self.ratio * dd_multiplier  # 紐⑺몴 50%
                entry_qty = self.safe_amount(symbol, target_value / price)
                
                if float(entry_qty) > 0:
                    # ?덈쾭由ъ? ?ㅼ젙 ?곸슜
                    lev = cfg.get('leverage', 5)
                    await asyncio.to_thread(self.exchange.set_leverage, lev, symbol)
                    
                    if self.trend_direction == 'long':
                        # 200 EMA ????濡?吏꾩엯
                        order = await asyncio.to_thread(
                            self.exchange.create_order, symbol, 'market', 'buy', entry_qty
                        )
                        self.position_cache = None
                        self.position_cache_time = 0
                        await self.ctrl.notify(f"✅ [Shannon] LONG 진입 {entry_qty} @ {price:.2f} (200 EMA 상향)")
                        self.db.log_shannon(total, "ENTRY_LONG", price, float(entry_qty), total)
                        logger.info(f"Shannon initial LONG entry: {order}")
                        return
                    
                    elif self.trend_direction == 'short':
                        # 200 EMA ?꾨옒 ????吏꾩엯
                        order = await asyncio.to_thread(
                            self.exchange.create_order, symbol, 'market', 'sell', entry_qty
                        )
                        self.position_cache = None
                        self.position_cache_time = 0
                        await self.ctrl.notify(f"✅ [Shannon] SHORT 진입 {entry_qty} @ {price:.2f} (200 EMA 하향)")
                        self.db.log_shannon(total, "ENTRY_SHORT", price, float(entry_qty), total)
                        logger.info(f"Shannon initial SHORT entry: {order}")
                        return
            
            # ============ 異붿꽭 諛섏쟾 媛먯?: ?ъ???泥?궛 ???ъ쭊??============
            reversed_position = False
            if pos and trend_cfg.get('enabled', True) and self.trend_direction:
                current_side = pos['side']
                # 濡??ъ??섏씤??異붿꽭媛 ?섎씫?쇰줈 ?꾪솚
                if current_side == 'long' and self.trend_direction == 'short':
                    await self._close_and_reverse(symbol, pos, price, 'short', total, dd_multiplier)
                    reversed_position = True
                    # ?ъ???蹂寃????ㅼ떆 議고쉶
                    pos = await self.get_server_position(symbol, use_cache=False)
                # ???ъ??섏씤??異붿꽭媛 ?곸듅?쇰줈 ?꾪솚
                elif current_side == 'short' and self.trend_direction == 'long':
                    await self._close_and_reverse(symbol, pos, price, 'long', total, dd_multiplier)
                    reversed_position = True
                    pos = await self.get_server_position(symbol, use_cache=False)
            
            # ============ Grid Trading (ATR 湲곕컲 媛꾧꺽) ============
            grid_cfg = cfg.get('grid_trading', {})
            if grid_cfg.get('enabled', False):
                await self.manage_grid_orders(symbol, price, grid_cfg, dd_multiplier)
            
            # ============ Rebalance (鍮꾩쑉 ?좎?) ============
            threshold = cfg.get('asset_allocation', {}).get('allowed_deviation_pct', 2.0)
            if abs(diff_pct) > threshold and pos:
                current_side = pos['side']
                contracts = abs(float(pos['contracts']))
                target_contracts = (total * self.ratio) / price  # 紐⑺몴 怨꾩빟 ??
                
                if current_side == 'long':
                    # 濡??ъ???由щ갭?곗떛
                    if contracts > target_contracts:
                        # ?ъ???怨쇰떎 ???쇰? 泥?궛 (留ㅻ룄)
                        reduce_qty = self.safe_amount(symbol, (contracts - target_contracts) * dd_multiplier)
                        if float(reduce_qty) > 0:
                            order = await asyncio.to_thread(
                                self.exchange.create_order, symbol, 'market', 'sell', reduce_qty
                            )
                            self.position_cache = None
                            self.position_cache_time = 0
                            await self.ctrl.notify(f"?뽳툘 [Long] 異뺤냼: SELL {reduce_qty}")
                            logger.info(f"Rebalance SELL: {order}")
                    else:
                        # ?ъ???遺議???異붽? 留ㅼ닔 (異붿꽭 ?꾪꽣: 濡?異붿꽭???뚮쭔)
                        if self.trend_direction == 'long':
                            add_qty = self.safe_amount(symbol, (target_contracts - contracts) * dd_multiplier)
                            if float(add_qty) > 0:
                                order = await asyncio.to_thread(
                                    self.exchange.create_order, symbol, 'market', 'buy', add_qty
                                )
                                self.position_cache = None
                                self.position_cache_time = 0
                                await self.ctrl.notify(f"?뽳툘 [Long] ?뺣?: BUY {add_qty}")
                                logger.info(f"Rebalance BUY: {order}")
                        else:
                            logger.info(f"??[Long] ?뺣? 李⑤떒: 異붿꽭媛 {self.trend_direction}")
                else:
                    # ???ъ???由щ갭?곗떛
                    if contracts > target_contracts:
                        # ??怨쇰떎 ???쇰? 泥?궛 (留ㅼ닔濡?而ㅻ쾭)
                        reduce_qty = self.safe_amount(symbol, (contracts - target_contracts) * dd_multiplier)
                        if float(reduce_qty) > 0:
                            order = await asyncio.to_thread(
                                self.exchange.create_order, symbol, 'market', 'buy', reduce_qty
                            )
                            self.position_cache = None
                            self.position_cache_time = 0
                            await self.ctrl.notify(f"?뽳툘 [Short] 異뺤냼: BUY {reduce_qty}")
                            logger.info(f"Rebalance BUY (cover): {order}")
                    else:
                        # ??遺議???異붽? 留ㅻ룄 (異붿꽭 ?꾪꽣: ??異붿꽭???뚮쭔)
                        if self.trend_direction == 'short':
                            add_qty = self.safe_amount(symbol, (target_contracts - contracts) * dd_multiplier)
                            if float(add_qty) > 0:
                                order = await asyncio.to_thread(
                                    self.exchange.create_order, symbol, 'market', 'sell', add_qty
                                )
                                self.position_cache = None
                                self.position_cache_time = 0
                                await self.ctrl.notify(f"?뽳툘 [Short] ?뺣?: SELL {add_qty}")
                                logger.info(f"Rebalance SELL (add short): {order}")
                        else:
                            logger.info(f"??[Short] ?뺣? 李⑤떒: 異붿꽭媛 {self.trend_direction}")
                
                self.db.log_shannon(total, "REBAL", price, coin_amt, total)
                    
        except Exception as e:
            logger.error(f"Shannon logic error: {e}")

    async def _close_and_reverse(self, symbol, pos, price, new_direction, total, dd_multiplier):
        """異붿꽭 諛섏쟾 ???ъ???泥?궛 ??諛섎? 諛⑺뼢 吏꾩엯"""
        try:
            # 留ㅻℓ 諛⑺뼢 ?꾪꽣 泥댄겕
            d_mode = self.cfg.get('system_settings', {}).get('trade_direction', 'both')
            if (d_mode == 'long' and new_direction == 'short') or \
               (d_mode == 'short' and new_direction == 'long'):
                # 諛⑺뼢 ?꾪꽣???섑빐 ?ъ쭊??遺덇? ??泥?궛留?
                close_qty = self.safe_amount(symbol, abs(float(pos['contracts'])))
                close_side = 'sell' if pos['side'] == 'long' else 'buy'
                await asyncio.to_thread(
                    self.exchange.create_order, symbol, 'market', close_side, close_qty
                )
                pnl = float(pos.get('unrealizedPnl', 0))
                await self.ctrl.notify(f"🔄 [Shannon] {pos['side'].upper()} 청산 (방향 필터) PnL: {pnl:+.2f}")
                self.position_cache = None
                self.position_cache_time = 0
                return
            
            # 湲곗〈 ?ъ???泥?궛
            close_qty = self.safe_amount(symbol, abs(float(pos['contracts'])))
            close_side = 'sell' if pos['side'] == 'long' else 'buy'
            
            await asyncio.to_thread(
                self.exchange.create_order, symbol, 'market', close_side, close_qty
            )
            
            pnl = float(pos.get('unrealizedPnl', 0))
            await self.ctrl.notify(f"🔄 [Shannon] {pos['side'].upper()} 청산 (추세 반전) PnL: {pnl:+.2f}")
            
            # 泥?궛 ?꾨즺 ?湲?
            await asyncio.sleep(2.0)  # 2珥??湲?
            
            # 諛섎? 諛⑺뼢 ???ъ???吏꾩엯
            target_value = total * self.ratio * dd_multiplier
            entry_qty = self.safe_amount(symbol, target_value / price)
            entry_side = 'buy' if new_direction == 'long' else 'sell'
            
            if float(entry_qty) > 0:
                await asyncio.to_thread(
                    self.exchange.create_order, symbol, 'market', entry_side, entry_qty
                )
                self.position_cache = None
                self.position_cache_time = 0
                await self.ctrl.notify(f"✅ [Shannon] {new_direction.upper()} 진입 {entry_qty} @ {price:.2f}")
                self.db.log_shannon(total, f"REVERSE_{new_direction.upper()}", price, float(entry_qty), total)
                
        except Exception as e:
            logger.error(f"Shannon close and reverse error: {e}")

    async def manage_grid_orders(self, symbol, price, grid_cfg, dd_multiplier=1.0):
        """Grid Trading 濡쒖쭅 - ATR 湲곕컲 ?숈쟻 媛꾧꺽"""
        try:
            levels = grid_cfg.get('grid_levels', 5)
            base_order_size = grid_cfg.get('order_size_usdt', 20)
            
            # ?쒕줈?곕떎??蹂댄샇 ?곸슜
            order_size = base_order_size * dd_multiplier
            
            # ?덈쾭由ъ? ?ㅼ젙 ?뺤씤 (Grid 二쇰Ц?먮룄 ?숈씪?섍쾶 ?곸슜)
            lev = self.cfg.get('shannon_engine', {}).get('leverage', 5)
            try:
                await asyncio.to_thread(self.exchange.set_leverage, lev, symbol)
            except Exception:
                pass  # ?대? ?ㅼ젙??寃쎌슦 臾댁떆
            
            # ATR 湲곕컲 洹몃━??媛꾧꺽 怨꾩궛
            atr_cfg = self.cfg.get('shannon_engine', {}).get('atr_settings', {})
            if atr_cfg.get('enabled', True) and self.atr_value:
                atr_multiplier = atr_cfg.get('grid_multiplier', 0.5)
                # ATR 湲곕컲 媛꾧꺽 (媛寃??鍮?鍮꾩쑉)
                step_pct = (self.atr_value * atr_multiplier) / price
                logger.debug(f"ATR Grid Step: {step_pct*100:.3f}%")
            else:
                # ATR ?놁쑝硫?湲곕낯媛??ъ슜
                step_pct = 0.005  # 0.5%
            
            # 200 EMA 諛⑺뼢 ?꾪꽣 ?곸슜
            trend_cfg = self.cfg.get('shannon_engine', {}).get('trend_filter', {})
            allow_buy = True
            allow_sell = True
            
            if trend_cfg.get('enabled', True) and self.trend_direction:
                if self.trend_direction == 'short':
                    allow_buy = False  # ?섎씫 異붿꽭: 留ㅼ닔 二쇰Ц 湲덉?
                elif self.trend_direction == 'long':
                    allow_sell = False  # ?곸듅 異붿꽭: 留ㅻ룄 二쇰Ц 湲덉?
            
            # 湲곗〈 洹몃━??二쇰Ц ?곹깭 ?뺤씤
            try:
                open_orders = await asyncio.to_thread(self.exchange.fetch_open_orders, symbol)
            except Exception as e:
                logger.error(f"Fetch open orders error: {e}")
                return
            
            # #5 Fix: ?꾩옱 媛寃⑹뿉???덈Т 硫?댁쭊 二쇰Ц 痍⑥냼 (媛寃⑹쓽 5% ?댁긽)
            max_distance_pct = 0.05
            for order in open_orders:
                try:
                    order_price = float(order['price'])
                    distance = abs(order_price - price) / price
                    if distance > max_distance_pct:
                        await asyncio.to_thread(self.exchange.cancel_order, order['id'], symbol)
                        logger.info(f"Grid order cancelled (too far): {order['side']} @ {order_price:.2f} ({distance*100:.1f}% away)")
                except Exception as e:
                    logger.error(f"Cancel distant order error: {e}")
            
            # ?꾩옱 ?ъ????뺤씤 (#10 Fix)
            pos = await self.get_server_position(symbol, use_cache=True)
            has_long_position = pos and pos['side'] == 'long' and float(pos['contracts']) > 0
            has_short_position = pos and pos['side'] == 'short' and abs(float(pos['contracts'])) > 0
            
            # 洹몃━??二쇰Ц 諛⑺뼢 寃곗젙 (?ъ???湲곕컲)
            # - 濡??ъ??? 留ㅻ룄 二쇰Ц ?덉슜 (?듭젅/泥?궛??, 留ㅼ닔 二쇰Ц???덉슜 (異붽? 吏꾩엯)
            # - ???ъ??? 留ㅼ닔 二쇰Ц ?덉슜 (?듭젅/泥?궛??, 留ㅻ룄 二쇰Ц???덉슜 (異붽? 吏꾩엯)
            # - ?ъ????놁쓬: 異붿꽭 諛⑺뼢?쇰줈留?二쇰Ц
            if has_long_position:
                allow_sell = True  # 濡??ъ???泥?궛??留ㅻ룄 ?덉슜
                # 濡??ъ??섏뿉??異붽? 留ㅼ닔??異붿꽭 ?꾪꽣 ?곕쫫
            elif has_short_position:
                allow_buy = True  # ???ъ???泥?궛??留ㅼ닔 ?덉슜
                # ???ъ??섏뿉??異붽? 留ㅼ닔??異붿꽭 ?꾪꽣 ?곕쫫
            else:
                # ?ъ????놁쓣 ?? 異붿꽭 諛⑺뼢??諛섑븯??二쇰Ц 湲덉?
                if self.trend_direction == 'short':
                    allow_buy = False  # ??異붿꽭: 留ㅼ닔 湲덉?
                elif self.trend_direction == 'long':
                    allow_sell = False  # 濡?異붿꽭: 留ㅻ룄 湲덉?
            
            # 洹몃━??二쇰Ц??遺議깊븯硫??덈줈 ?앹꽦
            if len(open_orders) < levels * 2:
                for i in range(1, levels + 1):
                    # 留ㅼ닔 二쇰Ц (?꾩옱媛 ?꾨옒)
                    buy_price = price * (1 - step_pct * i)
                    buy_qty = self.safe_amount(symbol, order_size / buy_price)
                    
                    # 留ㅻ룄 二쇰Ц (?꾩옱媛 ??
                    sell_price = price * (1 + step_pct * i)
                    sell_qty = self.safe_amount(symbol, order_size / sell_price)
                    
                    # 以묐났 泥댄겕
                    buy_exists = any(
                        abs(float(o['price']) - buy_price) / buy_price < 0.002 
                        for o in open_orders if o['side'] == 'buy'
                    )
                    sell_exists = any(
                        abs(float(o['price']) - sell_price) / sell_price < 0.002 
                        for o in open_orders if o['side'] == 'sell'
                    )
                    
                    # 留ㅼ닔 二쇰Ц (異붿꽭 ?꾪꽣 ?곸슜)
                    if allow_buy and not buy_exists and float(buy_qty) > 0:
                        try:
                            await asyncio.to_thread(
                                self.exchange.create_order, symbol, 'limit', 'buy',
                                buy_qty, self.safe_price(symbol, buy_price)
                            )
                            logger.info(f"Grid BUY: {buy_qty} @ {buy_price:.2f} (ATR step: {step_pct*100:.2f}%)")
                        except Exception as e:
                            logger.error(f"Grid buy order error: {e}")
                    
                    # 留ㅻ룄 二쇰Ц (異붿꽭 ?꾪꽣 + ?ъ???泥댄겕 ?곸슜)
                    if allow_sell and not sell_exists and float(sell_qty) > 0:
                        try:
                            await asyncio.to_thread(
                                self.exchange.create_order, symbol, 'limit', 'sell',
                                sell_qty, self.safe_price(symbol, sell_price)
                            )
                            logger.info(f"Grid SELL: {sell_qty} @ {sell_price:.2f} (ATR step: {step_pct*100:.2f}%)")
                        except Exception as e:
                            logger.error(f"Grid sell order error: {e}")
            
            self.grid_orders = open_orders
            
        except Exception as e:
            logger.error(f"Grid trading error: {e}")


class DualThrustEngine(BaseEngine):
    """????몃윭?ㅽ듃 蹂?숈꽦 ?뚰뙆 ?꾨왂"""
    def __init__(self, controller):
        super().__init__(controller)
        self.last_heartbeat = 0
        self.last_trigger_update = 0
        self.consecutive_errors = 0
        
        # ?몃━嫄?罹먯떆
        self.today_open = None
        self.range_value = None
        self.long_trigger = None
        self.short_trigger = None
        self.trigger_date = None  # ?몃━嫄?怨꾩궛 ?좎쭨 (?ㅻ쾭?섏엲 由ъ뀑??
        
        self.TRIGGER_UPDATE_INTERVAL = 60  # 60珥덈쭏??泥댄겕 (??蹂寃??뺤씤)

    def start(self):
        super().start()
        # ?ъ떆?????몃━嫄??뺣낫 珥덇린??
        self.last_heartbeat = 0
        self.last_trigger_update = 0
        self.trigger_date = None
        self.long_trigger = None
        self.short_trigger = None
        logger.info(f"?? [DualThrust] Engine started and triggers reset")

    def _get_target_symbol(self):
        return self.cfg.get('dual_thrust_engine', {}).get('target_symbol', 'BTC/USDT')

    async def poll_tick(self):
        """[?쒖닔 ?대쭅] Dual Thrust 硫붿씤 ?대쭅"""
        if not self.running:
            return
        
        try:
            symbol = self._get_target_symbol()
            
            # ?꾩옱 媛寃?議고쉶
            ticker = await asyncio.to_thread(self.exchange.fetch_ticker, symbol)
            price = float(ticker['last'])
            
            # ?몃━嫄??낅뜲?댄듃 (?섎（ 蹂寃???媛깆떊)
            await self._update_triggers(symbol)
            
            # ?섑듃鍮꾪듃 (30珥덈쭏??
            now = time.time()
            if now - self.last_heartbeat > 30:
                self.last_heartbeat = now
                pos_side = self.ctrl.status_data.get('pos_side', 'UNKNOWN') if self.ctrl.status_data else 'UNKNOWN'
                logger.info(f"?뮄 [DualThrust] Heartbeat: running={self.running}, paused={self.ctrl.is_paused}, pos={pos_side}, price={price:.2f}")
                if self.long_trigger and self.short_trigger:
                    logger.info(f"?뱤 [DualThrust] Triggers: Long={self.long_trigger:.2f}, Short={self.short_trigger:.2f}, Range={self.range_value:.2f}")
            
            # ?곹깭 ?낅뜲?댄듃 + 留ㅻℓ 濡쒖쭅
            await self._logic(symbol, price)
            self.consecutive_errors = 0
            
        except Exception as e:
            self.consecutive_errors += 1
            logger.error(f"DualThrust poll_tick error ({self.consecutive_errors}x): {e}")
            if self.consecutive_errors > 10:
                self.position_cache = None
                self.position_cache_time = 0
                self.consecutive_errors = 0

    async def _update_triggers(self, symbol):
        """N???쇰큺?쇰줈 Range 怨꾩궛 諛??몃━嫄??낅뜲?댄듃"""
        now = datetime.now(timezone.utc)
        today_str = now.strftime('%Y-%m-%d')
        
        # ?대? ?ㅻ뒛 怨꾩궛?덉쑝硫??ㅽ궢
        if self.trigger_date == today_str and self.long_trigger and self.short_trigger:
            return
        
        try:
            cfg = self.cfg.get('dual_thrust_engine', {})
            n_days = cfg.get('n_days', 4)
            k1 = cfg.get('k1', 0.5)
            k2 = cfg.get('k2', 0.5)
            
            # ?쇰큺 ?곗씠??媛?몄삤湲?(N+1媛? 怨쇨굅 N??+ ?ㅻ뒛)
            ohlcv = await asyncio.to_thread(
                self.exchange.fetch_ohlcv, symbol, '1d', limit=n_days + 1
            )
            
            if len(ohlcv) < n_days + 1:
                logger.warning(f"[DualThrust] Insufficient daily data: {len(ohlcv)} < {n_days + 1}")
                return
            
            # 怨쇨굅 N???곗씠??(?ㅻ뒛 ?쒖쇅)
            past_n = ohlcv[:-1][-n_days:]
            
            # HH, HC, LC, LL 怨꾩궛
            highs = [candle[2] for candle in past_n]
            lows = [candle[3] for candle in past_n]
            closes = [candle[4] for candle in past_n]
            
            hh = max(highs)  # Highest High
            hc = max(closes)  # Highest Close
            lc = min(closes)  # Lowest Close
            ll = min(lows)  # Lowest Low
            
            # Range = max(HH - LC, HC - LL)
            self.range_value = max(hh - lc, hc - ll)
            
            # ?뱀씪 ?쒓? (?ㅻ뒛 罹붾뱾)
            self.today_open = ohlcv[-1][1]
            
            # ?몃━嫄?怨꾩궛
            self.long_trigger = self.today_open + (self.range_value * k1)
            self.short_trigger = self.today_open - (self.range_value * k2)
            self.trigger_date = today_str
            
            logger.info(f"?뱢 [DualThrust] Triggers Updated: Open={self.today_open:.2f}, Range={self.range_value:.2f}")
            logger.info(f"   Long Trigger: {self.long_trigger:.2f}, Short Trigger: {self.short_trigger:.2f}")
            
        except Exception as e:
            logger.error(f"DualThrust trigger update error: {e}")

    async def _logic(self, symbol, price):
        """留ㅻℓ 濡쒖쭅"""
        try:
            total, free, mmr = await self.get_balance_info()
            count, daily_pnl = self.db.get_daily_stats()
            pos = await self.get_server_position(symbol)
            
            # ?곹깭 ?곗씠???낅뜲?댄듃 (Symbol Keyed)
            self.ctrl.status_data[symbol] = {
                'engine': 'DualThrust', 'symbol': symbol, 'price': price,
                'total_equity': total, 'free_usdt': free, 'mmr': mmr,
                'daily_count': count, 'daily_pnl': daily_pnl,
                'pos_side': pos['side'].upper() if pos else 'NONE',
                'entry_price': float(pos['entryPrice']) if pos else 0.0,
                'coin_amt': float(pos['contracts']) if pos else 0.0,
                'pnl_pct': float(pos['percentage']) if pos else 0.0,
                'pnl_usdt': float(pos['unrealizedPnl']) if pos else 0.0,
                # Dual Thrust ?꾩슜
                'long_trigger': self.long_trigger,
                'short_trigger': self.short_trigger,
                'range': self.range_value,
                'today_open': self.today_open
            }
            
            # MMR 寃쎄퀬
            await self.check_mmr_alert(mmr)
            
            if self.ctrl.is_paused:
                return
            
            # ?쇱씪 ?먯떎 ?쒕룄
            if await self.check_daily_loss_limit():
                return
            
            # ?몃━嫄곌? ?놁쑝硫??湲?
            if not self.long_trigger or not self.short_trigger:
                logger.debug("[DualThrust] Waiting for trigger calculation...")
                return
            
            # 留ㅻℓ 諛⑺뼢 ?꾪꽣
            d_mode = self.cfg.get('system_settings', {}).get('trade_direction', 'both')
            
            current_side = pos['side'] if pos else None
            
            # 濡??몃━嫄??뚰뙆
            if price > self.long_trigger:
                if d_mode != 'short':  # ???꾩슜 ?꾨땲硫?
                    if current_side == 'short':
                        # ??泥?궛 ??濡??ㅼ쐞移?
                        await self._close_and_switch(symbol, pos, price, 'long', total)
                    elif not pos:
                        # ?ъ????놁쑝硫?濡?吏꾩엯
                        await self._entry(symbol, 'long', price, total)
            
            # ???몃━嫄??댄깉
            elif price < self.short_trigger:
                if d_mode != 'long':  # 濡??꾩슜 ?꾨땲硫?
                    if current_side == 'long':
                        # 濡?泥?궛 ?????ㅼ쐞移?
                        await self._close_and_switch(symbol, pos, price, 'short', total)
                    elif not pos:
                        # ?ъ????놁쑝硫???吏꾩엯
                        await self._entry(symbol, 'short', price, total)
                        
        except Exception as e:
            logger.error(f"DualThrust logic error: {e}")

    async def _entry(self, symbol, side, price, total_equity):
        """?ъ???吏꾩엯"""
        try:
            cfg = self.cfg.get('dual_thrust_engine', {})
            lev = cfg.get('leverage', 5)
            risk_pct = cfg.get('risk_per_trade_pct', 50.0) / 100.0
            
            # ?덈쾭由ъ? ?ㅼ젙 & 寃⑸━ 紐⑤뱶 媛뺤젣
            await self.ensure_market_settings(symbol, leverage=lev)
            # await asyncio.to_thread(self.exchange.set_leverage, lev, symbol)
            
            # ?섎웾 怨꾩궛
            _, free, _ = await self.get_balance_info()
            qty = self.safe_amount(symbol, (free * risk_pct * lev) / price)
            
            if float(qty) <= 0:
                logger.warning(f"[DualThrust] Invalid qty: {qty}")
                return
            
            order = await asyncio.to_thread(
                self.exchange.create_order, symbol, 'market',
                'buy' if side == 'long' else 'sell', qty
            )
            
            self.position_cache = None
            self.position_cache_time = 0
            
            self.db.log_trade_entry(symbol, side, price, float(qty))
            await self.ctrl.notify(f"✅ [DualThrust] {side.upper()} {qty} @ {price:.2f}")
            logger.info(f"??[DualThrust] Entry: {side} {qty} @ {price}")
            
        except Exception as e:
            logger.error(f"DualThrust entry error: {e}")
            await self.ctrl.notify(f"❌ [DualThrust] 진입 실패: {e}")

    async def _close_and_switch(self, symbol, pos, price, new_side, total_equity):
        """?ъ???泥?궛 ??諛섎? 諛⑺뼢 吏꾩엯"""
        try:
            # 留ㅻℓ 諛⑺뼢 ?꾪꽣 泥댄겕
            d_mode = self.cfg.get('system_settings', {}).get('trade_direction', 'both')
            if (d_mode == 'long' and new_side == 'short') or (d_mode == 'short' and new_side == 'long'):
                # 泥?궛留??섍퀬 ?ъ쭊???덊븿
                await self.exit_position(symbol, "DirectionFilter")
                return
            
            # 湲곗〈 ?ъ???泥?궛
            close_qty = self.safe_amount(symbol, abs(float(pos['contracts'])))
            close_side = 'sell' if pos['side'] == 'long' else 'buy'
            
            pnl = float(pos.get('unrealizedPnl', 0))
            
            await asyncio.to_thread(
                self.exchange.create_order, symbol, 'market', close_side, close_qty
            )
            
            self.db.log_trade_close(symbol, pnl, float(pos.get('percentage', 0)), price, "Switch")
            await self.ctrl.notify(f"🔄 [DualThrust] {pos['side'].upper()} 청산 -> {new_side.upper()} 전환 | PnL: {pnl:+.2f}")
            
            self.position_cache = None
            self.position_cache_time = 0
            
            # ?좎떆 ?湲???諛섎? 諛⑺뼢 吏꾩엯
            await asyncio.sleep(1.0)
            await self._entry(symbol, new_side, price, total_equity)
            
        except Exception as e:
            logger.error(f"DualThrust switch error: {e}")

    async def exit_position(self, symbol, reason):
        """?ъ???泥?궛"""
        try:
            pos = await self.get_server_position(symbol, use_cache=False)
            if not pos:
                return
            
            contracts = abs(float(pos['contracts']))
            if contracts <= 0:
                return
            
            qty = self.safe_amount(symbol, contracts)
            side = 'sell' if pos['side'] == 'long' else 'buy'
            
            order = await asyncio.to_thread(
                self.exchange.create_order, symbol, 'market', side, qty
            )
            
            pnl = float(pos.get('unrealizedPnl', 0))
            pnl_pct = float(pos.get('percentage', 0))
            exit_price = float(order.get('average', 0)) or float(pos.get('markPrice', 0))
            
            self.position_cache = None
            self.position_cache_time = 0
            
            self.db.log_trade_close(symbol, pnl, pnl_pct, exit_price, reason)
            await self.ctrl.notify(f"📊 [DualThrust] [{reason}] PnL: {pnl:+.2f} ({pnl_pct:+.2f}%)")
            
        except Exception as e:
            logger.error(f"DualThrust exit error: {e}")


class DualModeFractalEngine(BaseEngine):
    """
    DualModeFractalStrategy瑜??ъ슜?섎뒗 ?낅┰ ?붿쭊
    - Mode: Scalping (5m~15m) / Standard (1h~4h)
    """
    def __init__(self, controller):
        super().__init__(controller)
        self.strategy = None
        self.current_mode = None
        self.last_candle_ts = 0
    
    def _get_target_symbol(self):
        # 怨듯넻 ?ㅼ젙??Watchlist 泥?踰덉㎏ 肄붿씤???寃잛쑝濡??ъ슜
        watchlist = self.cfg.get('signal_engine', {}).get('watchlist', [])
        return watchlist[0] if watchlist else 'BTC/USDT'

    def start(self):
        super().start()
        self.last_candle_ts = 0  # ??꾩뒪?ы봽 珥덇린??異붽?
        if not self._init_strategy():
            self.running = False

    def _init_strategy(self):
        if not DUAL_MODE_AVAILABLE:
            logger.error("DualMode strategy module is not available.")
            return False
        cfg = self.cfg.get('dual_mode_engine', {})
        mode = cfg.get('mode', 'standard')
        self.strategy = DualModeFractalStrategy(mode=mode)
        self.current_mode = mode
        logger.info(f"?쏉툘 [DualMode] Strategy initialized: {mode.upper()}")
        return True

    async def poll_tick(self):
        if not self.running:
            return
        if not self.strategy:
            return
        
        try:
            symbol = self._get_target_symbol()
            cfg = self.cfg.get('dual_mode_engine', {})
            
            # 紐⑤뱶 蹂寃?媛먯? 諛??ъ큹湲고솕
            if cfg.get('mode') != self.current_mode:
                if not self._init_strategy():
                    return
            
            # ??꾪봽?덉엫 寃곗젙
            if self.current_mode == 'scalping':
                tf = cfg.get('scalping_tf', '5m')
            else:
                tf = cfg.get('standard_tf', '4h')
                
            # OHLCV 媛?몄삤湲?(Limit reduced to 300 for optimization)
            ohlcv = await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, tf, limit=300)
            if not ohlcv: return
            
            # 留덉?留??뺤젙 罹붾뱾 湲곗? (?꾩옱 吏꾪뻾 以묒씤 罹붾뱾? ?쒖쇅?섍굅???ы븿 ?щ? 寃곗젙)
            # ?꾨왂 濡쒖쭅??'close' ?곗씠?곕? ?곕?濡??뺤젙??罹붾뱾???덉쟾??
            last_closed = ohlcv[-2] 
            ts = last_closed[0]
            price = float(ohlcv[-1][4]) # ?꾩옱媛???ㅼ떆媛?
            
            # ??罹붾뱾 媛깆떊 ?쒖뿉留??쒓렇??怨꾩궛 (?대쭅 遺??媛먯냼)
            if ts > self.last_candle_ts:
                self.last_candle_ts = ts
                
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                
                # ?꾨왂 ?ㅽ뻾
                res = self.strategy.generate_signals(df)
                last_row = res.iloc[-2] # ?뺤젙 罹붾뱾 湲곗? ?쒓렇??
                
                sig = int(last_row['signal'])
                chop = float(last_row['chop_idx'])
                kalman = float(last_row['kalman_val'])
                exit_price = float(last_row['exit_price'])
                
                logger.info(f"?쏉툘 [DualMode] {tf} Candle Close: Chop={chop:.1f}, Kalman={kalman:.1f}, Signal={sig}")
                
                # 留ㅻℓ 濡쒖쭅
                await self._execute_signal(symbol, sig, price)
                
            # ?곹깭 ?낅뜲?댄듃 (??쒕낫?쒖슜)
            await self._update_status(symbol, price, tf)
                
        except Exception as e:
            logger.error(f"DualMode poll error: {e}")

    async def _execute_signal(self, symbol, sig, price):
        pos = await self.get_server_position(symbol, use_cache=False)
        total, free, _ = await self.get_balance_info()
        
        # 1. Long Entry (Signal=1)
        if sig == 1:
            if not pos:
                await self._entry(symbol, 'long', price, free)
            elif pos['side'] == 'short':
                # Close Short & Flip Long
                await self.exit_position(symbol, "Signal_Flip_Long")
                await asyncio.sleep(1)
                await self._entry(symbol, 'long', price, free)
        
        # 2. Short Entry (Signal=-1)
        elif sig == -1:
            if not pos:
                await self._entry(symbol, 'short', price, free)
            elif pos['side'] == 'long':
                # Close Long & Flip Short
                await self.exit_position(symbol, "Signal_Flip_Short")
                await asyncio.sleep(1)
                await self._entry(symbol, 'short', price, free)

        # 3. Exit Long (Signal=2)
        elif sig == 2:
            if pos and pos['side'] == 'long':
                await self.exit_position(symbol, "Signal_Exit_Long")

        # 4. Exit Short (Signal=-2)
        elif sig == -2:
            if pos and pos['side'] == 'short':
                await self.exit_position(symbol, "Signal_Exit_Short")

    async def _entry(self, symbol, side, price, free_usdt):
        # 怨듯넻 ?ㅼ젙?먯꽌 由ъ뒪??諛?TP/SL 媛?몄삤湲?
        common_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
        risk = common_cfg.get('risk_per_trade_pct', 50.0) / 100.0
        
        # ?덈쾭由ъ?????쇰え???ㅼ젙???곕Ⅴ嫄곕굹 怨듯넻 ?ㅼ젙???곕쫫 (?ъ슜???붿껌: ?먯궛/肄붿씤/TP/SL)
        # 臾몃㎘???덈쾭由ъ???怨듯넻???곕Ⅴ??寃껋씠 ?덉쟾?섎굹, 紐낆떆???붿껌? ?놁뿀?? 
        # ?섏?留??ㅻⅨ ?ㅼ젙?ㅼ씠 怨듯넻???곕Ⅴ誘濡??덈쾭由ъ???怨듯넻???쎈뒗 寃껋씠 ?쇨??곸엫.
        lev = common_cfg.get('leverage', 5)
        
        # ?덈쾭由ъ? ?ㅼ젙 & 寃⑸━ 紐⑤뱶 媛뺤젣
        await self.ensure_market_settings(symbol, leverage=lev)
        # await asyncio.to_thread(self.exchange.set_leverage, lev, symbol)
        
        cost = free_usdt * risk
        qty = self.safe_amount(symbol, (cost * lev) / price)
        
        if float(qty) > 0:
            actual_side = 'buy' if side == 'long' else 'sell'
            
            # 1. 吏꾩엯 二쇰Ц
            order = await asyncio.to_thread(
                self.exchange.create_order, symbol, 'market', actual_side, qty
            )
            entry_price = float(order.get('average', price))
            self.position_cache = None
            self.db.log_trade_entry(symbol, side, entry_price, float(qty))
            
            # 2. TP/SL ?ㅼ젙 (怨듯넻 ?ㅼ젙 媛??ъ슜)
            tp_master_enabled = bool(common_cfg.get('tp_sl_enabled', True))
            tp_enabled = tp_master_enabled and bool(common_cfg.get('take_profit_enabled', True))
            sl_enabled = tp_master_enabled and bool(common_cfg.get('stop_loss_enabled', True))
            tp_pct = common_cfg.get('target_roe_pct', 0.0) if tp_enabled else 0.0
            sl_pct = common_cfg.get('stop_loss_pct', 0.0) if sl_enabled else 0.0
            
            msg = f"✅ [DualMode] {side.upper()} 진입: {qty} @ {entry_price}"
            
            # TP/SL 二쇰Ц 諛곗튂 (鍮꾨룞湲??ㅻ쪟 諛⑹?瑜??꾪빐 try-except)
            if tp_pct > 0 or sl_pct > 0:
                try:
                    # 湲곗〈 ?ㅽ뵂 ?ㅻ뜑 痍⑥냼
                    await asyncio.to_thread(self.exchange.cancel_all_orders, symbol)
                    
                    if side == 'long':
                        if tp_pct > 0:
                            tp_price = entry_price * (1 + tp_pct / 100 / lev)
                            await asyncio.to_thread(
                                self.exchange.create_order,
                                symbol,
                                'limit',
                                'sell',
                                qty,
                                tp_price,
                                {'reduceOnly': True}
                            )
                            msg += f" | TP: {tp_price:.2f}"
                        if sl_pct > 0:
                            sl_price = entry_price * (1 - sl_pct / 100 / lev)
                            await asyncio.to_thread(
                                self.exchange.create_order,
                                symbol,
                                'stop_market',
                                'sell',
                                qty,
                                None,
                                {'stopPrice': sl_price, 'reduceOnly': True}
                            )
                            msg += f" | SL: {sl_price:.2f}"
                    else: # short
                        if tp_pct > 0:
                            tp_price = entry_price * (1 - tp_pct / 100 / lev)
                            await asyncio.to_thread(
                                self.exchange.create_order,
                                symbol,
                                'limit',
                                'buy',
                                qty,
                                tp_price,
                                {'reduceOnly': True}
                            )
                            msg += f" | TP: {tp_price:.2f}"
                        if sl_pct > 0:
                            sl_price = entry_price * (1 + sl_pct / 100 / lev)
                            await asyncio.to_thread(
                                self.exchange.create_order,
                                symbol,
                                'stop_market',
                                'buy',
                                qty,
                                None,
                                {'stopPrice': sl_price, 'reduceOnly': True}
                            )
                            msg += f" | SL: {sl_price:.2f}"
                except Exception as e:
                    logger.error(f"TP/SL Order Failed: {e}")
                    msg += " | ⚠️ TP/SL 오류"

            await self.ctrl.notify(msg)

    async def exit_position(self, symbol, reason):
        pos = await self.get_server_position(symbol)
        if pos:
            qty = self.safe_amount(symbol, abs(float(pos['contracts'])))
            side = 'sell' if pos['side'] == 'long' else 'buy'
            await asyncio.to_thread(
                self.exchange.create_order, symbol, 'market', side, qty
            )
            pnl = float(pos.get('unrealizedPnl', 0))
            self.db.log_trade_close(symbol, pnl, 0, 0, reason)
            await self.ctrl.notify(f"📊 [DualMode] 청산 [{reason}]: PnL {pnl:.2f}")
            self.position_cache = None

    async def _update_status(self, symbol, price, tf):
        total, free, mmr = await self.get_balance_info()
        count, daily_pnl = self.db.get_daily_stats()
        pos = await self.get_server_position(symbol)
        
        self.ctrl.status_data[symbol] = {
            'engine': 'DualMode', 'symbol': symbol, 'price': price,
            'total_equity': total, 'free_usdt': free, 'mmr': mmr,
            'daily_count': count, 'daily_pnl': daily_pnl,
            'pos_side': pos['side'].upper() if pos else 'NONE',
            'entry_price': float(pos['entryPrice']) if pos else 0.0,
            'pnl_usdt': float(pos['unrealizedPnl']) if pos else 0.0,
            'pnl_pct': float(pos['percentage']) if pos else 0.0,
            # DualMode ?꾩슜
            'dm_mode': self.current_mode.upper(),
            'dm_tf': tf
        }


# ---------------------------------------------------------
# 3. 硫붿씤 而⑦듃濡ㅻ윭
# ---------------------------------------------------------
class MainController:
    def __init__(self):
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.runtime_dir = os.path.join(self.base_dir, 'runtime')
        os.makedirs(self.runtime_dir, exist_ok=True)
        config_path = os.path.join(self.base_dir, 'config.json')
        self.cfg = TradingConfig(config_path)
        logger.info(f"Config path: {config_path}")
        self.db = DBManager(self.cfg.get('logging', {}).get('db_path', 'bot_database.db'))
        self.heartbeat_file = os.environ.get(
            'BOT_HEARTBEAT_FILE',
            os.path.join(self.runtime_dir, 'bot_heartbeat.json')
        )
        self.exit_file = os.path.join(self.runtime_dir, 'bot_last_exit.json')
        self.launch_reason = os.environ.get('BOT_LAUNCH_REASON', 'manual_start')
        self.launch_started_at = os.environ.get(
            'BOT_START_TS',
            datetime.now().astimezone().isoformat(timespec='seconds')
        )
        last_heartbeat_age = os.environ.get('BOT_LAST_HEARTBEAT_AGE', '').strip()
        try:
            self.last_heartbeat_age = int(float(last_heartbeat_age)) if last_heartbeat_age else None
        except ValueError:
            self.last_heartbeat_age = None
        self.last_pid_before_start = os.environ.get('BOT_LAST_PID', '').strip()
        self.last_log_line = os.environ.get('BOT_LAST_LOG_LINE', '').replace('`', "'").strip()
        prev_paused_raw = os.environ.get('BOT_PREV_PAUSED', '').strip().lower()
        if prev_paused_raw in ('1', 'true', 'yes', 'on'):
            self.prev_paused_state = True
        elif prev_paused_raw in ('0', 'false', 'no', 'off'):
            self.prev_paused_state = False
        else:
            self.prev_paused_state = None
        self.process_start_ts = time.time()
        self.last_exit_info = self._read_last_exit_info()
        self._exit_recorded = False

        self.exchange_mode = self.get_exchange_mode()
        creds = self._get_exchange_credentials(self.exchange_mode)
        network_name = self.get_exchange_mode_label(self.exchange_mode)
        has_key = bool(str(creds.get('api_key', '')).strip())
        has_secret = bool(str(creds.get('secret_key', '')).strip())
        logger.info(
            f"API credential check ({network_name}): "
            f"key={'SET' if has_key else 'EMPTY'}, "
            f"secret={'SET' if has_secret else 'EMPTY'}"
        )
        if not (has_key and has_secret):
            logger.error(
                f"Selected {network_name} API credentials are empty. "
                "Private endpoints (balance/positions/orders) will fail."
            )
        
        self.exchange = self._build_exchange(creds, self.exchange_mode)
        self._configure_exchange_network(self.exchange, self.exchange_mode)
        self.market_data_exchange = self._build_public_market_data_exchange(self.exchange_mode)
        self._configure_exchange_network(self.market_data_exchange, self.exchange_mode)
        self.market_data_source_label = self._get_market_data_source_label(self.exchange_mode)
        logger.info(f"Market data source configured: {self.market_data_source_label}")
        
        self.engines = {
            CORE_ENGINE: SignalEngine(self)
        }
        logger.info("Core mode enabled: Signal(SMA/HMA/CAMERON) + risk controls only. Legacy engines archived.")
        self.active_engine = None
        self.tg_app = None
        self.status_data = {}
        self.is_paused = True  # 기본: 수동 시작 시 안전하게 일시정지
        resume_reasons = {'ensure_restart', 'manual_restart', 'reboot_start', 'deploy_start'}
        if self.launch_reason in resume_reasons and self.prev_paused_state is not None:
            self.is_paused = self.prev_paused_state
            logger.info(
                f"Pause state restored from previous heartbeat: paused={self.is_paused} "
                f"(launch_reason={self.launch_reason})"
            )
        self.dashboard_msg_id = None
        self.dashboard_plain_text_mode = True
        self.last_status_snapshot_key = None
        self.blink_state = False
        self.last_hourly_report = 0

    def get_exchange_mode(self):
        api_cfg = self.cfg.get('api', {})
        mode = str(api_cfg.get('exchange_mode', '')).lower()
        if mode not in SUPPORTED_EXCHANGE_MODES:
            mode = BINANCE_TESTNET if api_cfg.get('use_testnet', True) else BINANCE_MAINNET
        return mode

    def is_upbit_mode(self):
        return self.get_exchange_mode() == UPBIT_MODE

    def get_effective_trade_direction(self):
        if self.is_upbit_mode():
            return 'long'
        return self.cfg.get('system_settings', {}).get('trade_direction', 'both')

    def get_exchange_mode_label(self, exchange_mode=None):
        mode = exchange_mode or self.get_exchange_mode()
        labels = {
            BINANCE_TESTNET: 'binance testnet',
            BINANCE_MAINNET: 'binance mainnet',
            UPBIT_MODE: 'upbit krw spot'
        }
        return labels.get(mode, mode)

    def get_network_status_label(self, exchange_mode=None):
        mode = exchange_mode or self.get_exchange_mode()
        labels = {
            BINANCE_TESTNET: '테스트넷(데모) 🧪',
            BINANCE_MAINNET: '메인넷 💰',
            UPBIT_MODE: '업비트 KRW 현물'
        }
        return labels.get(mode, mode)

    def get_exchange_display_name(self, exchange_mode=None):
        mode = exchange_mode or self.get_exchange_mode()
        names = {
            BINANCE_TESTNET: 'BINANCE FUTURES',
            BINANCE_MAINNET: 'BINANCE FUTURES',
            UPBIT_MODE: 'UPBIT SPOT'
        }
        return names.get(mode, mode.upper())

    def _get_market_data_source_label(self, exchange_mode=None):
        mode = exchange_mode or self.get_exchange_mode()
        labels = {
            BINANCE_TESTNET: 'BINANCE FUTURES TESTNET PUBLIC',
            BINANCE_MAINNET: 'BINANCE FUTURES MAINNET PUBLIC',
            UPBIT_MODE: 'UPBIT KRW SPOT PUBLIC'
        }
        return labels.get(mode, mode.upper())

    def _get_exchange_credentials(self, exchange_mode=None):
        mode = exchange_mode or self.get_exchange_mode()
        api = self.cfg.get('api', {})
        if mode == UPBIT_MODE:
            return api.get('upbit', {})
        if mode == BINANCE_TESTNET:
            return api.get('testnet', {})
        return api.get('mainnet', {})

    def get_active_trade_section(self):
        return 'upbit' if self.is_upbit_mode() else 'signal_engine'

    def get_active_trade_config(self):
        return self.cfg.get(self.get_active_trade_section(), {})

    def get_active_common_settings(self):
        return self.get_active_trade_config().get('common_settings', {})

    def get_active_strategy_params(self):
        strategy = dict(self.get_active_trade_config().get('strategy_params', {}))
        if self.is_upbit_mode():
            strategy['active_strategy'] = 'utbot'
            strategy['entry_mode'] = 'position'
        return strategy

    def get_active_watchlist(self):
        watchlist = self.get_active_trade_config().get('watchlist', [])
        if not isinstance(watchlist, list) or not watchlist:
            return ['BTC/KRW'] if self.is_upbit_mode() else ['BTC/USDT']
        return list(watchlist)

    def normalize_symbol_for_exchange(self, raw_symbol, exchange_mode=None):
        mode = exchange_mode or self.get_exchange_mode()
        text = str(raw_symbol or '').strip().upper()
        if not text:
            raise ValueError("빈 심볼은 사용할 수 없습니다.")

        if mode == UPBIT_MODE:
            text = text.replace(' ', '')
            if text.startswith('KRW-'):
                base = text.split('-', 1)[1]
            elif '/' in text:
                left, right = text.split('/', 1)
                if left == 'KRW':
                    base = right
                elif right == 'KRW':
                    base = left
                else:
                    raise ValueError("업비트는 KRW 마켓만 지원합니다.")
            else:
                base = text
            if not re.fullmatch(r'[A-Z0-9]{2,15}', base or ''):
                raise ValueError("업비트 코인은 영문/숫자 심볼로 입력하세요. 예: BTC, XRP, KRW-BTC")
            return f"{base}/KRW"

        if '/' in text:
            return text
        return f"{text}/USDT"

    def format_symbol_for_display(self, symbol, exchange_mode=None):
        mode = exchange_mode or self.get_exchange_mode()
        raw = str(symbol or '')
        if mode == UPBIT_MODE and '/' in raw:
            base, quote = raw.split('/', 1)
            if quote.upper() == 'KRW':
                return f"KRW-{base.upper()}"
        return raw

    def _build_exchange(self, creds, exchange_mode=None):
        mode = exchange_mode or self.get_exchange_mode()
        if mode == UPBIT_MODE:
            return ccxt.upbit({
                'apiKey': creds.get('api_key', ''),
                'secret': creds.get('secret_key', ''),
                'enableRateLimit': True
            })
        return ccxt.binance({
            'apiKey': creds.get('api_key', ''),
            'secret': creds.get('secret_key', ''),
            'options': {'defaultType': 'future'},
            'enableRateLimit': True
        })

    def _build_public_market_data_exchange(self, exchange_mode=None):
        mode = exchange_mode or self.get_exchange_mode()
        if mode == UPBIT_MODE:
            return ccxt.upbit({
                'enableRateLimit': True
            })
        return ccxt.binance({
            'options': {'defaultType': 'future'},
            'enableRateLimit': True
        })

    def _configure_exchange_network(self, exchange, exchange_mode=None):
        mode = exchange_mode or self.get_exchange_mode()
        if mode == UPBIT_MODE:
            return "업비트 KRW 현물"

        if mode != BINANCE_TESTNET:
            return "메인넷 💰"

        try:
            if hasattr(exchange, 'enable_demo_trading'):
                exchange.enable_demo_trading(True)
                logger.info("Exchange network configured: Binance Demo Trading")
                return "테스트넷(데모) 🧪"
            if hasattr(exchange, 'enableDemoTrading'):
                exchange.enableDemoTrading(True)
                logger.info("Exchange network configured: Binance Demo Trading")
                return "테스트넷(데모) 🧪"
        except Exception as e:
            logger.warning(f"Failed to enable demo trading, fallback to sandbox mode: {e}")

        try:
            exchange.set_sandbox_mode(True)
            logger.warning("Using legacy sandbox mode. Consider updating ccxt >= 4.5.6.")
            return "테스트넷(샌드박스) 🧪"
        except Exception as e:
            logger.error(f"Failed to configure testnet mode: {e}")
            raise

    def _format_duration(self, seconds):
        if seconds is None:
            return "-"
        seconds = max(0, int(seconds))
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        if hours >= 24:
            days, hours = divmod(hours, 24)
            return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def _read_last_exit_info(self):
        try:
            if not os.path.exists(self.exit_file):
                return {}
            with open(self.exit_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"Exit marker read error: {e}")
            return {}

    def _get_rss_mb(self):
        try:
            with open('/proc/self/status', 'r', encoding='utf-8') as f:
                for line in f:
                    if line.startswith('VmRSS:'):
                        parts = line.split()
                        if len(parts) >= 2:
                            return round(int(parts[1]) / 1024.0, 1)
        except Exception:
            return None
        return None

    def _get_effective_last_exit_info(self):
        info = self.last_exit_info if isinstance(self.last_exit_info, dict) else {}
        if not info:
            return {}
        prev_pid = str(self.last_pid_before_start or "").strip()
        exit_pid = str(info.get('pid', '')).strip()
        if prev_pid and exit_pid and prev_pid != exit_pid:
            return {}
        return info

    def _write_heartbeat(self):
        payload = {
            'ts': datetime.now().astimezone().isoformat(timespec='seconds'),
            'epoch': int(time.time()),
            'pid': os.getpid(),
            'launch_reason': self.launch_reason,
            'paused': self.is_paused,
            'engine': self.cfg.get('system_settings', {}).get('active_engine', CORE_ENGINE),
            'status_count': len(self.status_data) if isinstance(self.status_data, dict) else 0,
            'rss_mb': self._get_rss_mb()
        }
        try:
            os.makedirs(os.path.dirname(self.heartbeat_file), exist_ok=True)
            with open(self.heartbeat_file, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Heartbeat write error: {e}")

    def record_exit_marker(self, reason, detail=""):
        if self._exit_recorded:
            return
        self._exit_recorded = True
        payload = {
            'ts': datetime.now().astimezone().isoformat(timespec='seconds'),
            'pid': os.getpid(),
            'reason': str(reason),
            'detail': str(detail or "")[:500],
            'uptime_sec': int(max(0, time.time() - self.process_start_ts)),
            'rss_mb': self._get_rss_mb()
        }
        try:
            os.makedirs(os.path.dirname(self.exit_file), exist_ok=True)
            with open(self.exit_file, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False)
            logger.info(f"Exit marker recorded: {payload}")
        except Exception as e:
            logger.error(f"Exit marker write error: {e}")

    def get_runtime_diag(self):
        last_exit = self._get_effective_last_exit_info()
        return {
            'launch_reason': self.launch_reason,
            'launch_started_at': self.launch_started_at,
            'uptime_human': self._format_duration(time.time() - self.process_start_ts),
            'last_heartbeat_age': self.last_heartbeat_age,
            'last_heartbeat_age_human': self._format_duration(self.last_heartbeat_age),
            'last_pid_before_start': self.last_pid_before_start,
            'last_log_line': self.last_log_line,
            'rss_mb': self._get_rss_mb(),
            'last_exit_reason': last_exit.get('reason'),
            'last_exit_ts': last_exit.get('ts'),
            'last_exit_detail': last_exit.get('detail'),
            'last_exit_uptime_human': self._format_duration(last_exit.get('uptime_sec')),
            'last_exit_rss_mb': last_exit.get('rss_mb')
        }

    def _build_startup_notice(self):
        header = "⏸ **봇 시작됨 (일시정지 상태)**" if self.is_paused else "▶ **봇 시작됨 (자동 재개 상태)**"
        lines = [header, ""]
        lines.append(f"거래모드: `{self.get_exchange_display_name()}` / `{self.get_network_status_label()}`")
        lines.append(f"시작사유: `{self.launch_reason}`")
        lines.append(f"시작시각: `{self.launch_started_at}`")
        if self.last_heartbeat_age is not None:
            lines.append(f"이전 heartbeat age: `{self._format_duration(self.last_heartbeat_age)}`")
        if self.last_pid_before_start:
            lines.append(f"이전 PID: `{self.last_pid_before_start}`")
        if self.prev_paused_state is not None:
            lines.append(f"이전 일시정지 상태: `{'ON' if self.prev_paused_state else 'OFF'}`")
        if self.last_log_line:
            lines.append(f"직전 로그: `{self.last_log_line}`")
        last_exit = self._get_effective_last_exit_info()
        if last_exit:
            lines.append(
                f"직전 종료: `{last_exit.get('reason', '-')}` "
                f"@ `{last_exit.get('ts', '-')}`"
            )
        if self.is_paused:
            lines.extend(["", "설정 확인 후 `▶ RESUME`을 눌러주세요."])
        else:
            lines.extend(["", "이전 실행 상태를 유지해 자동으로 재개되었습니다."])
        return "\n".join(lines)

    async def run(self):
        logger.info("Bot starting... (Pure Polling Mode)")
        
        if self.cfg.get('logging', {}).get('debug_mode', False):
            logger.setLevel(logging.DEBUG)
            logger.info("Debug mode enabled")
        
        token = self.cfg.get('telegram', {}).get('token', '')
        if not token:
            logger.error("??Telegram token is missing!")
            return
        
        self.tg_app = ApplicationBuilder().token(token).build()
        await self._setup_telegram()
        
        await self._switch_engine(self.cfg.get('system_settings', {}).get('active_engine', CORE_ENGINE))
        
        await self.tg_app.initialize()
        await self.tg_app.start()
        await self.tg_app.updater.start_polling()
        self._write_heartbeat()
        
        # Startup notice + keyboard
        await self.notify(self._build_startup_notice())
        cid = self.cfg.get_chat_id()
        if cid:
            try:
                await self.tg_app.bot.send_message(
                    chat_id=cid,
                    text="📱 메인 메뉴를 띄웠습니다.",
                    reply_markup=self._build_main_keyboard()
                )
            except Exception as e:
                logger.warning(f"Failed to send main menu keyboard: {e}")
        
        await asyncio.gather(
            self._main_polling_loop(),  # [?대쭅 ?꾩슜] 硫붿씤 ?대쭅 猷⑦봽
            self._dashboard_loop(),
            self._hourly_report_loop(),
            self._heartbeat_loop()
        )

    async def _switch_engine(self, name):
        requested = (name or CORE_ENGINE).lower()
        if requested != CORE_ENGINE:
            logger.warning(f"Legacy engine request ignored in core mode: {requested} -> {CORE_ENGINE}")
            requested = CORE_ENGINE
            await self.cfg.update_value(['system_settings', 'active_engine'], CORE_ENGINE)

        if CORE_ENGINE not in self.engines:
            logger.error(f"Required engine not available: {CORE_ENGINE}")
            return
        
        if self.active_engine:
            self.active_engine.stop()
        
        # ?붿쭊 ?꾪솚 ???곹깭 ?곗씠??珥덇린??
        self.status_data = {}
        
        self.active_engine = self.engines[CORE_ENGINE]
        self.active_engine.start()
        
        sym = self._get_current_symbol()
        
        await self.active_engine.ensure_market_settings(sym)
        logger.info(f"Active engine: {CORE_ENGINE.upper()}")

    def _get_current_symbol(self):
        """?꾩옱 ?쒖꽦 ?붿쭊???щ낵 諛섑솚"""
        watchlist = self.get_active_watchlist()
        return watchlist[0] if watchlist else ('BTC/KRW' if self.is_upbit_mode() else 'BTC/USDT')

    async def reinit_exchange(self, target_mode):
        """거래소 연결 재초기화."""
        try:
            if isinstance(target_mode, bool):
                exchange_mode = BINANCE_TESTNET if target_mode else BINANCE_MAINNET
            else:
                exchange_mode = str(target_mode or '').lower()
            if exchange_mode not in SUPPORTED_EXCHANGE_MODES:
                return False, f"지원하지 않는 거래모드: {target_mode}"

            # 1. ?꾩옱 ?붿쭊 ?뺤?
            if self.active_engine:
                self.active_engine.stop()
                logger.info("??Engine stopped for exchange reinit")
            
            # 2. ?ㅼ젙 ?낅뜲?댄듃
            await self.cfg.update_value(['api', 'exchange_mode'], exchange_mode)
            await self.cfg.update_value(['api', 'use_testnet'], exchange_mode == BINANCE_TESTNET)
            
            # 3. ??API ?먭꺽利앸챸 濡쒕뱶
            self.exchange_mode = exchange_mode
            creds = self._get_exchange_credentials(exchange_mode)
            
            # 4. 嫄곕옒???ъ큹湲고솕
            self.exchange = self._build_exchange(creds, exchange_mode)
            network_name = self._configure_exchange_network(self.exchange, exchange_mode)
            self.market_data_exchange = self._build_public_market_data_exchange(exchange_mode)
            self._configure_exchange_network(self.market_data_exchange, exchange_mode)
            self.market_data_source_label = self._get_market_data_source_label(exchange_mode)
            
            # 5. 留덉폆 ?뺣낫 濡쒕뱶
            await asyncio.to_thread(self.exchange.load_markets)
            
            # 6. ?붿쭊?ㅼ뿉 ??exchange ?꾨떖
            for engine in self.engines.values():
                engine.exchange = self.exchange
                engine.market_data_exchange = self.market_data_exchange
                engine.position_cache = None
                engine.position_cache_time = 0
                engine.all_positions_cache = None
                engine.all_positions_cache_time = 0
            
            # 7. ?쒖꽦 ?붿쭊 ?ъ떆??
            eng_name = self.cfg.get('system_settings', {}).get('active_engine', CORE_ENGINE)
            await self._switch_engine(eng_name)
            
            logger.info(f"Exchange reinitialized: {network_name}")
            return True, network_name
            
        except Exception as e:
            logger.error(f"Exchange reinit error: {e}")
            return False, str(e)

    # ---------------- UI: emergency controls ----------------
    async def global_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text if update.message else ""
        
        if "STOP" in text:
            await self.emergency_stop()
            await update.message.reply_text("🚨 긴급 정지 완료 - 모든 포지션 청산")
            return ConversationHandler.END
        elif "PAUSE" in text:
            self.is_paused = True
            await update.message.reply_text("⏸ 일시정지 (매매 중단, 모니터링 유지)")
            return ConversationHandler.END
        elif "RESUME" in text:
            self.is_paused = False
            # Restart engine if it is not running
            if self.active_engine and not self.active_engine.running:
                self.active_engine.start()
                await update.message.reply_text("▶ 매매 재개 (엔진 재시작)")
            else:
                await update.message.reply_text("▶ 매매 재개")
            return ConversationHandler.END
        elif text == "/status":
            all_data = self.status_data if isinstance(self.status_data, dict) else {}
            status_text, history_text, snapshot_key = self._build_dashboard_messages(
                all_data,
                "●",
                " [PAUSED]" if self.is_paused else ""
            )
            self._record_status_snapshot(snapshot_key, history_text)
            try:
                await update.message.reply_text(status_text, parse_mode=ParseMode.MARKDOWN)
            except BadRequest as md_err:
                if "can't parse entities" in str(md_err).lower():
                    await update.message.reply_text(status_text)
                else:
                    raise
            return ConversationHandler.END
        
        return None

    async def _sync_signal_protection_orders(self):
        """현재 보유 포지션의 보호주문(TP/SL)을 최신 설정으로 동기화한다."""
        try:
            if self.is_upbit_mode():
                logger.info("Protection sync skipped: Upbit spot mode does not use futures TP/SL orders.")
                return

            common_cfg = self.get_active_common_settings()
            tp_master_enabled = bool(common_cfg.get('tp_sl_enabled', True))
            tp_enabled = tp_master_enabled and bool(common_cfg.get('take_profit_enabled', True))
            sl_enabled = tp_master_enabled and bool(common_cfg.get('stop_loss_enabled', True))

            lev = max(1.0, float(common_cfg.get('leverage', 10) or 10))
            tp_pct = float(common_cfg.get('target_roe_pct', 20.0) or 0.0) / 100.0 / lev
            sl_pct = float(common_cfg.get('stop_loss_pct', 10.0) or 0.0) / 100.0 / lev

            positions = await asyncio.to_thread(self.exchange.fetch_positions)
            refreshed = 0
            cancelled_only = 0

            for p in positions:
                contracts = abs(float(p.get('contracts', 0) or 0))
                if contracts <= 0:
                    continue

                raw_symbol = str(p.get('symbol', ''))
                symbol = raw_symbol.replace(':USDT', '')
                side = str(p.get('side', '')).lower()
                if side not in ('long', 'short'):
                    continue

                try:
                    await asyncio.to_thread(self.exchange.cancel_all_orders, symbol)
                except Exception as cancel_e:
                    logger.warning(f"Protection sync: cancel_all_orders failed for {symbol}: {cancel_e}")

                if not (tp_enabled or sl_enabled):
                    cancelled_only += 1
                    continue

                entry_price = float(p.get('entryPrice') or p.get('markPrice') or 0.0)
                if entry_price <= 0:
                    logger.warning(f"Protection sync skipped for {symbol}: invalid entry price")
                    continue

                close_side = 'sell' if side == 'long' else 'buy'
                try:
                    qty = self.exchange.amount_to_precision(symbol, contracts)
                except Exception:
                    qty = str(round(contracts, 6))

                if tp_enabled and tp_pct > 0:
                    tp_price_raw = entry_price * (1 + tp_pct) if side == 'long' else entry_price * (1 - tp_pct)
                    try:
                        tp_price = self.exchange.price_to_precision(symbol, tp_price_raw)
                    except Exception:
                        tp_price = str(round(tp_price_raw, 6))
                    try:
                        await asyncio.to_thread(
                            self.exchange.create_order,
                            symbol,
                            'limit',
                            close_side,
                            qty,
                            tp_price,
                            {'reduceOnly': True}
                        )
                    except Exception as tp_e:
                        logger.warning(f"Protection sync TP failed for {symbol}: {tp_e}")

                if sl_enabled and sl_pct > 0:
                    sl_price_raw = entry_price * (1 - sl_pct) if side == 'long' else entry_price * (1 + sl_pct)
                    try:
                        sl_price = self.exchange.price_to_precision(symbol, sl_price_raw)
                    except Exception:
                        sl_price = str(round(sl_price_raw, 6))
                    try:
                        await asyncio.to_thread(
                            self.exchange.create_order,
                            symbol,
                            'stop_market',
                            close_side,
                            qty,
                            None,
                            {'stopPrice': sl_price, 'reduceOnly': True}
                        )
                    except Exception as sl_e:
                        logger.warning(f"Protection sync SL failed for {symbol}: {sl_e}")

                refreshed += 1

            logger.info(
                f"Protection sync done: refreshed={refreshed}, "
                f"cancelled_only={cancelled_only}, tp={tp_enabled}, sl={sl_enabled}"
            )
        except Exception as e:
            logger.error(f"Protection order sync error: {e}")

    async def show_setup_menu(self, update: Update):
        sys_cfg = self.cfg.get('system_settings', {})
        sig = self.cfg.get('signal_engine', {})
        up_cfg = self.cfg.get('upbit', {})
        sha = self.cfg.get('shannon_engine', {})
        dt = self.cfg.get('dual_thrust_engine', {})
        dm = self.cfg.get('dual_mode_engine', {})
        eng = sys_cfg.get('active_engine', CORE_ENGINE)
        direction = self.get_effective_trade_direction()
        watchlist = sig.get('watchlist', ['BTC/USDT'])
        if not isinstance(watchlist, list) or not watchlist:
            watchlist = ['BTC/USDT']
        
        if eng == 'shannon':
            lev = sha.get('leverage', 5)
            symbol = sha.get('target_symbol', 'BTC/USDT')
        elif eng == 'dualthrust':
            lev = dt.get('leverage', 5)
            symbol = dt.get('target_symbol', 'BTC/USDT')
        elif eng == 'dualmode':
            lev = dm.get('leverage', 5)
            symbol = dm.get('target_symbol', 'BTC/USDT')
        else:
            lev = sig.get('common_settings', {}).get('leverage', 20)
            symbol = watchlist[0] if watchlist else 'BTC/USDT'
            
        status = "🔴 OFF" if self.is_paused else "🟢 ON"
        direction_str = {'both': '양방향', 'long': '롱만', 'short': '숏만'}.get(direction, 'both')

        if self.is_upbit_mode():
            up_watchlist = up_cfg.get('watchlist', ['BTC/KRW'])
            if not isinstance(up_watchlist, list) or not up_watchlist:
                up_watchlist = ['BTC/KRW']
            up_common = up_cfg.get('common_settings', {})
            up_strategy = up_cfg.get('strategy_params', {})
            up_utbot = up_strategy.get('UTBot', {})
            up_symbol = self.format_symbol_for_display(up_watchlist[0])
            network_status = self.get_network_status_label()
            hourly_report_status = "ON" if self.cfg.get('telegram', {}).get('reporting', {}).get('hourly_report_enabled', True) else "OFF"

            msg = f"""
🔧 **설정 메뉴** (번호 입력)

**현재 상태**: `{eng.upper()}` | `{up_symbol}` | `UPBIT`

**거래소**
22. 거래소/네트워크 전환 (`{network_status}`)
7. 매매 방향 (`롱만 고정`)

**Upbit**
43. 업비트 코인 (`{up_symbol}`)
44. 업비트 UT Bot (`K={float(up_utbot.get('key_value', 1.0) or 1.0):.2f}` / `ATR={int(up_utbot.get('atr_period', 10) or 10)}` / `HA={'ON' if up_utbot.get('use_heikin_ashi', False) else 'OFF'}`)
45. 업비트 진입 비율 (`{up_common.get('risk_per_trade_pct', 10)}%`)
46. 업비트 진입 TF (`{up_common.get('entry_timeframe', up_common.get('timeframe', '1h'))}`)
47. 업비트 청산 TF (`{up_common.get('exit_timeframe', '1h')}`)
48. 업비트 일일 손실 제한 (`₩{float(up_common.get('daily_loss_limit', 50000) or 50000):,.0f}`)

**운영**
42. 시간별 리포트 (`{hourly_report_status}`)
9. 매매 시작/중지 (`{status}`)
0. 나가기
"""
            await update.message.reply_text(msg.strip(), parse_mode=ParseMode.MARKDOWN)
            return
        
        # ?덉쟾???ㅼ젙 ?묎렐
        sig_common = sig.get('common_settings', {})
        
        # SMA ?ㅼ젙 媛?몄삤湲?
        sma_params = sig.get('strategy_params', {}).get('Triple_SMA', {})
        fast_sma = sma_params.get('fast_sma', 2)
        slow_sma = sma_params.get('slow_sma', 10)
        
        # Shannon ?ㅼ젙
        shannon_ratio = int(sha.get('asset_allocation', {}).get('target_ratio', 0.5) * 100)
        grid_enabled = "ON" if sha.get('grid_trading', {}).get('enabled', False) else "OFF"
        grid_size = sha.get('grid_trading', {}).get('order_size_usdt', 200)
        
        # Dual Thrust ?ㅼ젙
        dt_n = dt.get('n_days', 4)
        dt_k1 = dt.get('k1', 0.5)
        dt_k2 = dt.get('k2', 0.5)
        
        # Dual Mode ?ㅼ젙
        dm_mode = dm.get('mode', 'standard').upper()
        dm_tf = dm.get('scalping_tf', '5m') if dm_mode == 'SCALPING' else dm.get('standard_tf', '4h')
        
        # TP/SL ?곹깭
        tp_enabled = bool(sig_common.get('take_profit_enabled', sig_common.get('tp_sl_enabled', True)))
        sl_enabled = bool(sig_common.get('stop_loss_enabled', sig_common.get('tp_sl_enabled', True)))
        tp_sl_status = "ON" if (tp_enabled or sl_enabled) else "OFF"
        tp_status = "ON" if tp_enabled else "OFF"
        sl_status = "ON" if sl_enabled else "OFF"
        
        # Signal ?꾨왂 ?ㅼ젙 (異붽?)
        strategy_params = sig.get('strategy_params', {})
        active_strategy = strategy_params.get('active_strategy', 'sma').upper()
        entry_mode = strategy_params.get('entry_mode', 'cross').upper()
        if active_strategy in {'UTBOT', 'RSIBB', 'UTRSIBB', 'UTRSI', 'UTBB'}:
            entry_mode = active_strategy
        hma_params = strategy_params.get('HMA', {})
        hma_fast = hma_params.get('fast_period', 9)
        hma_slow = hma_params.get('slow_period', 21)
        utbot_params = strategy_params.get('UTBot', {})
        utbot_key = float(utbot_params.get('key_value', 1.0) or 1.0)
        utbot_atr = int(utbot_params.get('atr_period', 10) or 10)
        utbot_ha = "ON" if utbot_params.get('use_heikin_ashi', False) else "OFF"
        rsibb_params = strategy_params.get('RSIBB', {})
        rsibb_rsi = int(rsibb_params.get('rsi_length', 6) or 6)
        rsibb_bb = int(rsibb_params.get('bb_length', 200) or 200)
        rsibb_mult = float(rsibb_params.get('bb_mult', 2.0) or 2.0)
        watchlist_preview = ", ".join(watchlist[:4]) if isinstance(watchlist, list) and watchlist else symbol
        if isinstance(watchlist, list) and len(watchlist) > 4:
            watchlist_preview += " ..."
        
        # Scanner ?곹깭
        scanner_enabled = sig_common.get('scanner_enabled', True)
        scanner_status = "ON 📡" if scanner_enabled else "OFF"
        scanner_tf = sig_common.get('scanner_timeframe', '15m')
        scanner_exit_tf = sig_common.get('scanner_exit_timeframe', '1h')

        # Hourly Report Status
        hourly_report_status = "ON" if self.cfg.get('telegram', {}).get('reporting', {}).get('hourly_report_enabled', True) else "OFF"

        # Filter Status (Split)
        r2_entry = "ON" if sig_common.get('r2_entry_enabled', True) else "OFF"
        r2_exit = "ON" if sig_common.get('r2_exit_enabled', True) else "OFF"
        r2_threshold = sig_common.get('r2_threshold', 0.25)

        chop_entry = "ON" if sig_common.get('chop_entry_enabled', True) else "OFF"
        chop_exit = "ON" if sig_common.get('chop_exit_enabled', True) else "OFF"
        chop_threshold = sig_common.get('chop_threshold', 50.0)
        
        kalman_cfg = strategy_params.get('kalman_filter', {})
        kalman_entry = "ON" if kalman_cfg.get('entry_enabled', False) else "OFF"
        kalman_exit = "ON" if kalman_cfg.get('exit_enabled', False) else "OFF"
        
        cc_exit = "ON" if sig_common.get('cc_exit_enabled', False) else "OFF"
        cc_threshold = sig_common.get('cc_threshold', 0.70)
        
        # Network status
        network_status = self.get_network_status_label()
        
        msg = f"""
🔧 **설정 메뉴** (번호 입력)

**현재 상태**: `{eng.upper()}` | `{symbol}`

**공통**
1. 레버리지 (`{lev}x`)
2. 목표 ROE (`{sig_common.get('target_roe_pct', 20)}%` / `{tp_status}`)
3. 손절 (`{sig_common.get('stop_loss_pct', 10)}%` / `{sl_status}`)
4. 진입 타임프레임 (`{sig_common.get('timeframe', '15m')}`)
41. 청산 타임프레임 (`{sig_common.get('exit_timeframe', '4h')}`)
5. 일일 손실 제한 (`${sig_common.get('daily_loss_limit', 5000)}`)
6. 진입 비율 (`{sig_common.get('risk_per_trade_pct', 50)}%`)
7. 매매 방향 (`{direction_str}`)
8. 심볼 변경 (`{symbol}`)
38. 감시 심볼 추가 (`{watchlist_preview}`)

**Signal**
16. 전략 (`{active_strategy}`)
18. 진입모드 (`{entry_mode}`)
10. SMA (`{fast_sma}/{slow_sma}`)
17. HMA (`{hma_fast}/{hma_slow}`)
19. UT Bot (`K={utbot_key:.2f}` / `ATR={utbot_atr}` / `HA={utbot_ha}`)
20. RSI+BB 보조설정 (`RSI={rsibb_rsi}` / `BB={rsibb_bb}` / `x{rsibb_mult:.2f}`)
21. UT Hybrid 안내 (`UTRSI / UTBB / UTRSIBB`)
13. TP/SL 자동청산 (`{tp_sl_status}`)
36. ROE 자동청산 (`{tp_status}`)
37. 손절 자동청산 (`{sl_status}`)
23. 거래량 급등 채굴 (`{scanner_status}` / `{scanner_tf}`)
24. 채굴 진입 TF
25. 채굴 청산 TF (`{scanner_exit_tf}`)

**필터 (Entry / Exit)**
26. R2 (`{r2_entry}` / `{r2_exit}`) 기준 `{r2_threshold}`
30. CHOP (`{chop_entry}` / `{chop_exit}`) 기준 `{chop_threshold}`
32. Kalman (`{kalman_entry}` / `{kalman_exit}`)
33. CC Exit (`{cc_exit}`) 기준 `{cc_threshold}`
27. R2 민감도
31. CHOP 민감도
34. CC 민감도

**기타**
22. 거래소/네트워크 전환 (`{network_status}`)
42. 시간별 리포트 (`{hourly_report_status}`)
00. 엔진 교체 (현재: `{eng.upper()}`)
9. 매매 시작/중지 (`{status}`)
0. 나가기
"""
        await update.message.reply_text(msg.strip(), parse_mode=ParseMode.MARKDOWN)

    async def setup_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.show_setup_menu(update)
        return SELECT

    async def setup_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        
        if text == '0':
            await update.message.reply_text("✅ 설정 종료")
            return ConversationHandler.END
        
        context.user_data['setup_choice'] = text
        
        prompts = {
            '1': "📝 **레버리지** 값을 입력하세요 (1~20, 예: 5)",
            '2': "📝 **목표 ROE 설정**: 값(%) 또는 ON/OFF 입력 (예: 20, on, off)",
            '3': "📝 **손절 설정**: 값(%) 또는 ON/OFF 입력 (예: 5, on, off)",
            '4': "📝 **진입 타임프레임** 입력 (예: 15m)\n1m,2m,3m,5m,15m,30m | 1h,2h,4h | 1d",
            '41': "📝 **청산 타임프레임** 입력 (예: 1h)\n1m,2m,3m,5m,15m,30m | 1h,2h,4h | 1d",
            '5': "📝 **일일 손실 제한($)** 입력 (예: 1000)",
            '6': "📝 **진입 비율(%)** 입력 (1~100, 예: 50)",
            '7': "↕️ **매매 방향** 선택 (양방향/롱만/숏만)",
            '8': "💱 **심볼** 입력 또는 선택\n1: BTC  2: ETH  3: SOL\n또는 직접 입력 (예: DOGE, XRP, PEPE)",
            '38': "➕ **감시 심볼 추가**\n1: BTC  2: ETH  3: SOL\n또는 직접 입력 (예: DOGE, XRP, PEPE)",
            '9': "▶️ 매매 시작/중지를 바꾸려면 1 또는 0 입력",
            '10': "📝 **SMA 기간** 입력 (형식: fast,slow 예: 2,10)",
            '11': "📝 **자산 비율(%)** 입력 (예: 50)",
            '12': "📝 **Grid 설정** 입력 (예: on,200 또는 off)",
            '14': "📝 **N Days** 입력 (예: 4)",
            '15': "📝 **K1/K2** 입력 (예: 0.5,0.5)",
            '16': "📝 **전략 선택** (1=SMA, 2=HMA, 3=CAMERON, 4=UTBOT, 5=RSIBB, 6=UTRSIBB, 7=UTRSI, 8=UTBB)",
            '17': "📝 **HMA 기간** 입력 (예: 9,21)",
            '18': "📝 **진입 모드** 선택 (1=Cross, 2=Position)",
            '19': "📝 **UT Bot 설정** 입력 (형식: key,atr,on/off 예: 1,10,off)",
            '20': "RSI/BB 보조설정 입력\n형식: RSI길이,BB길이,BB배수\n예: 6,200,2",
            '21': "ℹ️ **UT Hybrid 안내**: UTRSI/UTRSIBB는 조합형, UTBB는 비대칭 롱/숏 규칙을 사용합니다.",
            '22': "📝 **거래소/네트워크 선택** (1=바이낸스 테스트넷, 2=바이낸스 메인넷, 3=업비트 KRW 현물)",
            '23': "📝 **거래량 급등 채굴 기능** (1=ON, 0=OFF)",
            '24': "📝 **채굴 진입 타임프레임** 입력 (예: 5m)\n1m, 5m, 15m, 30m, 1h",
            '25': "📝 **채굴 청산 타임프레임** 입력 (예: 1h)\n1m, 5m, 15m, 30m, 1h, 4h",
            '26': "📝 **R2 필터** 메뉴로 이동합니다.",
            '27': "📝 **R2 기준값** 입력 (0.1 ~ 0.5 권장)",
            '30': "📝 **CHOP 필터** 메뉴로 이동합니다.",
            '31': "📝 **CHOP 기준값** 입력 (예: 50.0)",
            '33': "📝 **CC Exit 필터**를 ON/OFF 합니다.",
            '34': "📝 **CC 설정** 입력 (예: 0.70 또는 0.70,14)",
            '35': "📝 **듀얼모드 변경** (1=스탠다드, 2=스캘핑)",
            '36': "📝 **ROE 자동청산** ON/OFF 토글",
            '37': "📝 **손절 자동청산** ON/OFF 토글",
            '43': "📝 **업비트 코인** 입력\n예: BTC, XRP, KRW-BTC, BTC/KRW",
            '44': "📝 **업비트 UT Bot 설정** 입력 (형식: key,atr,on/off 예: 1,10,off)",
            '45': "📝 **업비트 진입 비율(%)** 입력 (1~100, 예: 25)",
            '46': "📝 **업비트 진입 타임프레임** 입력 (예: 1h)\n1m,3m,5m,15m,30m | 1h,4h | 1d",
            '47': "📝 **업비트 청산 타임프레임** 입력 (예: 1h)\n1m,3m,5m,15m,30m | 1h,4h | 1d",
            '48': "📝 **업비트 일일 손실 제한(KRW)** 입력 (예: 50000)",
        }
        if self.is_upbit_mode():
            blocked_choices = {
                '1', '2', '3', '4', '5', '6', '8', '10', '11', '12', '13', '14', '15',
                '16', '17', '18', '19', '20', '21', '23', '24', '25', '26', '27', '30',
                '31', '32', '33', '34', '35', '36', '37', '38', '41', '00'
            }
            if text in blocked_choices:
                await update.message.reply_text("ℹ️ 업비트 모드에서는 업비트 전용 메뉴(22, 43~48)만 사용합니다.")
                await self.show_setup_menu(update)
                return SELECT
        elif text in {'43', '44', '45', '46', '47', '48'}:
            await update.message.reply_text("ℹ️ 업비트 전용 메뉴입니다. 먼저 22번에서 업비트 KRW 현물로 전환하세요.")
            await self.show_setup_menu(update)
            return SELECT
        if text == '7':
            if self.is_upbit_mode():
                await update.message.reply_text("ℹ️ 업비트 현물은 숏/레버리지가 없어 `롱만`으로 고정됩니다.")
                await self.show_setup_menu(update)
                return SELECT
            keyboard = [
                [KeyboardButton("양방향 (Long+Short)")],
                [KeyboardButton("롱만 (Long Only)")],
                [KeyboardButton("숏만 (Short Only)")]
            ]
            await update.message.reply_text(
                "📝 **매매 방향** 선택:",
                reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
                parse_mode=ParseMode.MARKDOWN
            )
            return DIRECTION_SELECT
        elif text == '00':
            msg = """
🔀 **엔진 교체**

현재 코어 모드에서는 아래만 사용합니다.

1. **Signal Engine**
"""
            await update.message.reply_text(msg.strip(), parse_mode=ParseMode.MARKDOWN)
            return ENGINE_SELECT

        elif text == '9':
            self.is_paused = not self.is_paused
            await self.show_setup_menu(update)
            return SELECT
        elif text == '13':
            # TP/SL ?좉?
            common_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
            current = bool(common_cfg.get('tp_sl_enabled', True))
            new_val = not current
            await self.cfg.update_value(['signal_engine', 'common_settings', 'tp_sl_enabled'], new_val)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'take_profit_enabled'], new_val)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'stop_loss_enabled'], new_val)
            await self._sync_signal_protection_orders()
            status = "ON" if new_val else "OFF"
            await update.message.reply_text(f"✅ TP/SL 자동청산(전체): {status}")
            await self.show_setup_menu(update)
            return SELECT
        elif text == '36':
            common_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
            current_tp = bool(common_cfg.get('take_profit_enabled', common_cfg.get('tp_sl_enabled', True)))
            new_tp = not current_tp
            curr_sl = bool(common_cfg.get('stop_loss_enabled', common_cfg.get('tp_sl_enabled', True)))
            await self.cfg.update_value(['signal_engine', 'common_settings', 'take_profit_enabled'], new_tp)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'tp_sl_enabled'], bool(new_tp or curr_sl))
            await self._sync_signal_protection_orders()
            await update.message.reply_text(f"✅ ROE 자동청산: {'ON' if new_tp else 'OFF'}")
            await self.show_setup_menu(update)
            return SELECT
        elif text == '37':
            common_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
            current_sl = bool(common_cfg.get('stop_loss_enabled', common_cfg.get('tp_sl_enabled', True)))
            new_sl = not current_sl
            curr_tp = bool(common_cfg.get('take_profit_enabled', common_cfg.get('tp_sl_enabled', True)))
            await self.cfg.update_value(['signal_engine', 'common_settings', 'stop_loss_enabled'], new_sl)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'tp_sl_enabled'], bool(curr_tp or new_sl))
            await self._sync_signal_protection_orders()
            await update.message.reply_text(f"✅ 손절 자동청산: {'ON' if new_sl else 'OFF'}")
            await self.show_setup_menu(update)
            return SELECT
        elif text == '42':
            # Hourly Report Toggle
            curr = self.cfg.get('telegram', {}).get('reporting', {}).get('hourly_report_enabled', True)
            new_val = not curr
            await self.cfg.update_value(['telegram', 'reporting', 'hourly_report_enabled'], new_val)
            status = "ON" if new_val else "OFF"
            await update.message.reply_text(f"⚙️ 시간별 리포트: {status}")
            await self.show_setup_menu(update)
            return SELECT
        elif text == '26':
            # R2 Filter Menu
            keyboard = [[KeyboardButton("1. Entry Toggle"), KeyboardButton("2. Exit Toggle")]]
            await update.message.reply_text(
                "📉 **R2 필터 설정**\n1. Entry 토글\n2. Exit 토글",
                reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
            )
            return "R2_SELECT"

        elif text in ('28', '29'):
            await update.message.reply_text("ℹ️ Hurst 필터는 코드에서 제거되었습니다.")
            return SELECT

        elif text == '30':
            # Chop Filter Menu
            keyboard = [[KeyboardButton("1. Entry Toggle"), KeyboardButton("2. Exit Toggle")]]
            await update.message.reply_text(
                "🌀 **CHOP 필터 설정**\n1. Entry 토글\n2. Exit 토글",
                reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
            )
            return "CHOP_SELECT"

        elif text == '32':
            # Kalman Filter Menu
            keyboard = [[KeyboardButton("1. Entry Toggle"), KeyboardButton("2. Exit Toggle")]]
            await update.message.reply_text(
                "🚀 **Kalman 필터 설정**\n1. Entry 토글\n2. Exit 토글",
                reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
            )
            return "KALMAN_SELECT"

            return "KALMAN_SELECT"
            
        elif text == '33':
            # CC Filter Menu (Exit only)
            curr = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('cc_exit_enabled', False)
            new_val = not curr
            await self.cfg.update_value(['signal_engine', 'common_settings', 'cc_exit_enabled'], new_val)
            status = "ON" if new_val else "OFF"
            await update.message.reply_text(f"✅ CC 필터 (Exit): {status}")
            await self.show_setup_menu(update)
            return SELECT
            
        elif text == '21':
            await update.message.reply_text(
                "ℹ️ UT Hybrid 전략 안내\n"
                "- UTRSI: UT 방향 + RSI 타이밍 조합\n"
                "- UTRSIBB: UT 방향 + RSI/BB 동시 타이밍 조합\n"
                "- UTBB LONG: UT 롱신호 진입, UT 숏신호 또는 BB 상단 돌파 마감 시 청산\n"
                "- UTBB SHORT: BB 상단 재진입 숏 셋업 이후 UT 숏신호가 나오면 진입\n"
                "- UTBB SHORT 청산: UT 롱신호가 나오면 청산\n"
                "- 모든 판단은 확정봉 기준"
            )
            await self.show_setup_menu(update)
            return SELECT

        elif text == '24':
            await update.message.reply_text(prompts['24'])
            return INPUT

        elif text == '27':
            # R2 Threshold Input
            await update.message.reply_text(prompts['27'])
            return INPUT
        elif text == '31':
            return INPUT
        elif text in prompts:
            if text == '20':
                await update.message.reply_text(prompts[text])
            else:
                await update.message.reply_text(prompts[text], parse_mode=ParseMode.MARKDOWN)
            if text in {'8', '38', '43'}:
                return SYMBOL_INPUT
            return INPUT
        else:
            await update.message.reply_text("❌ 잘못된 번호입니다.")
            return SELECT

    async def handle_manual_symbol_input(self, update: Update, symbol: str):
        """Telegram manual symbol input handler."""
        try:
            symbol = self.normalize_symbol_for_exchange(symbol)
            
            # ?щ낵 ?좏슚??寃??(Exchange check)
            # SignalEngine???쒖꽦?붾릺???덉뼱????
            eng_type = self.cfg.get('system_settings', {}).get('active_engine', CORE_ENGINE)
            if eng_type != 'signal':
                await update.message.reply_text("⚠️ 현재 Signal 엔진이 활성화되어 있지 않습니다. (`/strat 1`)")
                return

            signal_engine = self.engines.get('signal')
            if not signal_engine:
                await update.message.reply_text("❌ Signal 엔진을 찾을 수 없습니다.")
                return

            # ?щ낵 寃利?(exchange load_markets ?꾩슂?????덉쓬, ?ш린??try fetch ticker濡??泥?
            try:
                await asyncio.to_thread(self.exchange.fetch_ticker, symbol)
            except Exception:
                await update.message.reply_text(f"❌ 유효하지 않은 심볼입니다: `{symbol}`", parse_mode=ParseMode.MARKDOWN)
                return

            display_symbol = self.format_symbol_for_display(symbol)
            if self.is_upbit_mode():
                await self.cfg.update_value(['upbit', 'watchlist'], [symbol])
                signal_engine.active_symbols.clear()
                signal_engine.active_symbols.add(symbol)
                signal_engine.last_candle_time = {}
                signal_engine.last_processed_candle_ts = {}
                signal_engine.last_state_sync_candle_ts = {}
                signal_engine.last_stateful_retry_ts = {}
                signal_engine.last_processed_exit_candle_ts = {}
                await signal_engine.prime_symbol_to_next_closed_candle(symbol)
                await update.message.reply_text(f"✅ 업비트 코인 변경: `{display_symbol}`", parse_mode=ParseMode.MARKDOWN)
                logger.info(f"Upbit manual symbol set: {symbol}")
            else:
                # Active Symbols??異붽?
                if symbol not in signal_engine.active_symbols:
                    signal_engine.active_symbols.add(symbol)
                    await signal_engine.prime_symbol_to_next_closed_candle(symbol)
                    await update.message.reply_text(f"✅ **{display_symbol}** 감시 시작 (수동 추가)", parse_mode=ParseMode.MARKDOWN)
                    logger.info(f"Manual symbol added: {symbol}")
                else:
                    await update.message.reply_text(f"ℹ️ 이미 감시 중인 심볼입니다: `{display_symbol}`", parse_mode=ParseMode.MARKDOWN)

        except Exception as e:
            logger.error(f"Manual input error: {e}")
            await update.message.reply_text(f"❌ 처리 실패: {e}")

    async def setup_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        choice = context.user_data.get('setup_choice')
        val = update.message.text
        
        try:
            if choice == '1':
                v = int(val)
                # ?덈쾭由ъ? 理쒕? 20諛??쒗븳 (?ъ슜???붿껌: 5 -> 20)
                if v < 1 or v > 20:
                    await update.message.reply_text("❌ 레버리지는 1~20 사이 값만 가능합니다.")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'leverage'], v)
                await self.cfg.update_value(['shannon_engine', 'leverage'], v)
                await self.cfg.update_value(['dual_thrust_engine', 'leverage'], v)
                await self.cfg.update_value(['dual_mode_engine', 'leverage'], v)
                # TEMA??common_settings瑜?李몄“?섎?濡?蹂꾨룄 ?낅뜲?댄듃 遺덊븘?뷀븯吏留? 
                # ?쒖꽦 ?붿쭊??TEMA??寃쎌슦 market settings 利됱떆 ?곸슜 ?꾩슂
                if self.active_engine:
                    sym = self._get_current_symbol()
                    await self.active_engine.ensure_market_settings(sym)
                await update.message.reply_text(f"✅ 레버리지 변경: {v}x")
            elif choice == '2':
                v_low = str(val).strip().lower()
                common_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
                if v_low in ('on', '1', 'true'):
                    curr_sl = bool(common_cfg.get('stop_loss_enabled', common_cfg.get('tp_sl_enabled', True)))
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'take_profit_enabled'], True)
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'tp_sl_enabled'], bool(True or curr_sl))
                    await self._sync_signal_protection_orders()
                    await update.message.reply_text("✅ 목표 ROE 자동청산: ON")
                elif v_low in ('off', '0', 'false'):
                    curr_sl = bool(common_cfg.get('stop_loss_enabled', common_cfg.get('tp_sl_enabled', True)))
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'take_profit_enabled'], False)
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'tp_sl_enabled'], bool(False or curr_sl))
                    await self._sync_signal_protection_orders()
                    await update.message.reply_text("✅ 목표 ROE 자동청산: OFF")
                else:
                    v = float(val)
                    if v < 0:
                        await update.message.reply_text("❌ 목표 ROE는 0 이상으로 입력하세요.")
                        return SELECT
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'target_roe_pct'], v)
                    await self.cfg.update_value(['dual_mode_engine', 'target_roe_pct'], v)
                    await self._sync_signal_protection_orders()
                    await update.message.reply_text(f"✅ 목표 ROE 변경: {v}%")
            elif choice == '3':
                v_low = str(val).strip().lower()
                common_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
                if v_low in ('on', '1', 'true'):
                    curr_tp = bool(common_cfg.get('take_profit_enabled', common_cfg.get('tp_sl_enabled', True)))
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'stop_loss_enabled'], True)
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'tp_sl_enabled'], bool(curr_tp or True))
                    await self._sync_signal_protection_orders()
                    await update.message.reply_text("✅ 손절 자동청산: ON")
                elif v_low in ('off', '0', 'false'):
                    curr_tp = bool(common_cfg.get('take_profit_enabled', common_cfg.get('tp_sl_enabled', True)))
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'stop_loss_enabled'], False)
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'tp_sl_enabled'], bool(curr_tp or False))
                    await self._sync_signal_protection_orders()
                    await update.message.reply_text("✅ 손절 자동청산: OFF")
                else:
                    v = float(val)
                    if v < 0:
                        await update.message.reply_text("❌ 손절 비율은 0 이상으로 입력하세요.")
                        return SELECT
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'stop_loss_pct'], v)
                    await self.cfg.update_value(['dual_thrust_engine', 'stop_loss_pct'], v)
                    await self.cfg.update_value(['dual_mode_engine', 'stop_loss_pct'], v)
                    await self._sync_signal_protection_orders()
                    await update.message.reply_text(f"✅ 손절 비율 변경: {v}%")
            elif choice == '4':
                # 타임프레임 유효성 검사
                valid_tf = ['1m', '2m', '3m', '4m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', '1d', '3d', '1w', '1M']
                if val not in valid_tf:
                    await update.message.reply_text(f"❌ 유효하지 않은 타임프레임입니다.\n사용 가능: {', '.join(valid_tf)}")
                    return SELECT
                # 타임프레임 업데이트 (Common, Signal, Shannon 동기화)
                await self.cfg.update_value(['signal_engine', 'common_settings', 'timeframe'], val)
                await self.cfg.update_value(['signal_engine', 'common_settings', 'entry_timeframe'], val) # Sync Entry TF
                await self.cfg.update_value(['shannon_engine', 'timeframe'], val)
                
                # DualMode 타임프레임도 모드에 따라 동기화
                dm_mode = self.cfg.get('dual_mode_engine', {}).get('mode', 'standard')
                if dm_mode == 'scalping':
                    await self.cfg.update_value(['dual_mode_engine', 'scalping_tf'], val)
                else:
                    await self.cfg.update_value(['dual_mode_engine', 'standard_tf'], val)

                # Shannon 엔진 200 EMA 캐시 초기화 (변경 TF 즉시 반영)
                if self.active_engine and hasattr(self.active_engine, 'ema_200'):
                    self.active_engine.ema_200 = None
                    self.active_engine.trend_direction = None
                    self.active_engine.last_indicator_update = 0
                # Signal 엔진 캐시 초기화
                signal_engine = self.engines.get('signal')
                if signal_engine:
                    signal_engine.last_processed_candle_ts = {}
                    signal_engine.last_candle_time = {}
                await update.message.reply_text(f"✅ 진입 타임프레임 변경: {val}")

                # DualMode 엔진 캐시 초기화
                dm_engine = self.engines.get('dualmode')
                if dm_engine:
                    dm_engine.last_candle_ts = 0

            elif choice == '5':
                await self.cfg.update_value(['shannon_engine', 'daily_loss_limit'], float(val))
                await self.cfg.update_value(['signal_engine', 'common_settings', 'daily_loss_limit'], float(val))
                await self.cfg.update_value(['dual_thrust_engine', 'daily_loss_limit'], float(val))
                await self.cfg.update_value(['dual_mode_engine', 'daily_loss_limit'], float(val))
            elif choice == '6':
                v = float(val)
                max_risk = float(self.cfg.get('signal_engine', {}).get('common_settings', {}).get('max_risk_per_trade_pct', 100.0) or 100.0)
                max_risk = min(100.0, max(1.0, max_risk))
                if v < 1 or v > max_risk:
                    await update.message.reply_text(f"❌ 1~{max_risk:.0f} 사이 값을 입력하세요.")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'risk_per_trade_pct'], v)
                await self.cfg.update_value(['dual_thrust_engine', 'risk_per_trade_pct'], v)
                await self.cfg.update_value(['dual_mode_engine', 'risk_per_trade_pct'], v)
            elif choice == '45':
                v = float(val)
                max_risk = float(self.cfg.get('upbit', {}).get('common_settings', {}).get('max_risk_per_trade_pct', 100.0) or 100.0)
                max_risk = min(100.0, max(1.0, max_risk))
                if v < 1 or v > max_risk:
                    await update.message.reply_text(f"❌ 업비트 진입 비율은 1~{max_risk:.0f} 사이여야 합니다.")
                    return SELECT
                await self.cfg.update_value(['upbit', 'common_settings', 'risk_per_trade_pct'], v)
                await update.message.reply_text(f"✅ 업비트 진입 비율 변경: {v}%")
            
            elif choice == '24':
                # Scanner Entry Timeframe
                valid_tf = ['1m', '2m', '3m', '5m', '15m', '30m', '1h', '4h']
                if val not in valid_tf:
                    await update.message.reply_text("❌ 유효하지 않은 타임프레임입니다.\n추천: 1m, 5m, 15m")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_timeframe'], val)
                await update.message.reply_text(f"✅ 채굴 진입 타임프레임 변경: {val}")

            elif choice == '25':
                # Scanner Exit Timeframe
                valid_tf = ['1m', '2m', '3m', '5m', '15m', '30m', '1h', '4h']
                if val not in valid_tf:
                    await update.message.reply_text("❌ 유효하지 않은 타임프레임입니다.\n추천: 1m, 5m, 15m, 1h")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_exit_timeframe'], val)
                await update.message.reply_text(f"✅ 채굴 청산 타임프레임 변경: {val}")

            # ======== Signal (SMA) ?꾩슜 ========
            elif choice == '10':
                # SMA 湲곌컙 蹂寃?(?뺤떇: "2,10" ?먮뒗 "5,25")
                parts = val.replace(' ', '').split(',')
                if len(parts) != 2:
                    await update.message.reply_text("❌ 형식: fast,slow (예: 2,10)")
                    return SELECT
                fast, slow = int(parts[0]), int(parts[1])
                if fast >= slow:
                    await update.message.reply_text("❌ fast SMA는 slow SMA보다 작아야 합니다.")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'Triple_SMA', 'fast_sma'], fast)
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'Triple_SMA', 'slow_sma'], slow)
                await update.message.reply_text(f"✅ SMA 기간 변경: {fast}/{slow}")
            
            # ======== Shannon ?꾩슜 ========
            elif choice == '11':
                # ?먯궛 鍮꾩쑉 蹂寃?(?뺤떇: "50" = 50%)
                v = float(val)
                if v < 10 or v > 90:
                    await update.message.reply_text("❌ 10~90 사이 값을 입력하세요.")
                    return SELECT
                ratio = v / 100.0
                await self.cfg.update_value(['shannon_engine', 'asset_allocation', 'target_ratio'], ratio)
                # Shannon ?붿쭊??利됱떆 ?곸슜
                shannon_engine = self.engines.get('shannon')
                if shannon_engine:
                    shannon_engine.ratio = ratio
                await update.message.reply_text(f"✅ Shannon 자산 비율 변경: {int(v)}%")
            
            elif choice == '12':
                # Grid ?ㅼ젙 (?뺤떇: "on,200" ?먮뒗 "off")
                val_lower = val.lower().strip()
                if val_lower == 'off':
                    await self.cfg.update_value(['shannon_engine', 'grid_trading', 'enabled'], False)
                    await update.message.reply_text("✅ Grid Trading: OFF")
                else:
                    parts = val_lower.replace(' ', '').split(',')
                    if parts[0] == 'on' and len(parts) >= 2:
                        size = float(parts[1])
                        await self.cfg.update_value(['shannon_engine', 'grid_trading', 'enabled'], True)
                        await self.cfg.update_value(['shannon_engine', 'grid_trading', 'order_size_usdt'], size)
                        await update.message.reply_text(f"✅ Grid Trading: ON, ${size}")
                    else:
                        await update.message.reply_text("❌ 형식: on,금액 또는 off (예: on,200)")
                        return SELECT
            
            # ======== Dual Thrust ?꾩슜 ========
            elif choice == '14':
                # N Days 蹂寃?
                v = int(val)
                if v < 1 or v > 30:
                    await update.message.reply_text("❌ 1~30 사이 값을 입력하세요.")
                    return SELECT
                await self.cfg.update_value(['dual_thrust_engine', 'n_days'], v)
                # ?붿쭊 罹먯떆 珥덇린??(??N?쇰줈 ?몃━嫄??ш퀎??
                dt_engine = self.engines.get('dualthrust')
                if dt_engine:
                    dt_engine.trigger_date = None
                await update.message.reply_text(f"✅ Dual Thrust N Days 변경: {v}")
            
            elif choice == '15':
                # K1/K2 蹂寃?(?뺤떇: "0.5,0.5" ?먮뒗 "0.4,0.6")
                parts = val.replace(' ', '').split(',')
                if len(parts) != 2:
                    await update.message.reply_text("❌ 형식: k1,k2 (예: 0.5,0.5)")
                    return SELECT
                k1, k2 = float(parts[0]), float(parts[1])
                if k1 <= 0 or k1 > 1 or k2 <= 0 or k2 > 1:
                    await update.message.reply_text("❌ K1, K2는 0~1 사이 값이어야 합니다.")
                    return SELECT
                await self.cfg.update_value(['dual_thrust_engine', 'k1'], k1)
                await self.cfg.update_value(['dual_thrust_engine', 'k2'], k2)
                # ?붿쭊 罹먯떆 珥덇린??(??K濡??몃━嫄??ш퀎??
                dt_engine = self.engines.get('dualthrust')
                if dt_engine:
                    dt_engine.trigger_date = None
                await update.message.reply_text(f"✅ Dual Thrust K1/K2 변경: {k1}/{k2}")
            # ======== Signal ?좉퇋 ?듭뀡 ========
            elif choice == '16':
                # ?꾨왂 蹂寃?(踰덊샇 ?먮뒗 ?대쫫?쇰줈 ?좏깮)
                strategy_map = {
                    '1': 'sma',
                    '2': 'hma',
                    '3': 'cameron',
                    '4': 'utbot',
                    '5': 'rsibb',
                    '6': 'utrsibb',
                    '7': 'utrsi',
                    '8': 'utbb'
                }
                val_lower = val.lower().strip()
                
                # 踰덊샇 ?낅젰 ??蹂??
                if val_lower in strategy_map:
                    val_lower = strategy_map[val_lower]
                
                if val_lower in CORE_STRATEGIES:
                    await self.cfg.update_value(['signal_engine', 'strategy_params', 'active_strategy'], val_lower)
                    signal_engine = self.engines.get('signal')
                    # ?꾨왂蹂??곹깭 珥덇린??
                    if val_lower == 'microvbo' and signal_engine:
                        signal_engine.vbo_entry_price = None
                        signal_engine.vbo_entry_atr = None
                        signal_engine.vbo_breakout_level = None
                    elif val_lower == 'fractalfisher' and signal_engine:
                        signal_engine.fisher_entry_price = None
                        signal_engine.fisher_entry_atr = None
                        signal_engine.fisher_trailing_stop = None
                        signal_engine.fisher_hurst = None
                        signal_engine.fisher_value = None
                    elif val_lower == 'cameron' and signal_engine:
                        signal_engine.cameron_states = {}
                    elif val_lower in (STATEFUL_UT_STRATEGIES | {'rsibb'}) and signal_engine:
                        signal_engine.last_processed_candle_ts = {}
                        signal_engine.last_state_sync_candle_ts = {}
                        signal_engine.last_stateful_retry_ts = {}
                        signal_engine.last_stateful_diag = {}
                        signal_engine.last_stateful_diag_notice = {}
                        signal_engine.ut_hybrid_timing_latches = {}
                        signal_engine.ut_hybrid_timing_consumed_ts = {}
                    await update.message.reply_text(f"✅ 전략 변경: {val_lower.upper()}")
                else:
                    await update.message.reply_text(
                        "❌ 1~8 또는 sma/hma/cameron/utbot/rsibb/utrsibb/utrsi/utbb를 입력하세요.\n"
                        "1=SMA, 2=HMA, 3=CAMERON, 4=UTBOT, 5=RSIBB, 6=UTRSIBB, 7=UTRSI, 8=UTBB"
                    )
                    return SELECT
            
            elif choice == '20':
                parts = val.replace(' ', '').split(',')
                if len(parts) != 3:
                    await update.message.reply_text("❌ 형식: rsi_length,bb_length,bb_mult (예: 6,200,2)")
                    return SELECT

                rsi_length = int(parts[0])
                bb_length = int(parts[1])
                bb_mult = float(parts[2])
                if rsi_length < 1 or bb_length < 2 or bb_mult <= 0:
                    await update.message.reply_text("❌ RSI 기간은 1 이상, BB 기간은 2 이상, 배수는 0보다 커야 합니다.")
                    return SELECT

                await self.cfg.update_value(['signal_engine', 'strategy_params', 'RSIBB', 'rsi_length'], rsi_length)
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'RSIBB', 'bb_length'], bb_length)
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'RSIBB', 'bb_mult'], bb_mult)
                signal_engine = self.engines.get('signal')
                if signal_engine:
                    signal_engine.last_processed_candle_ts = {}
                    signal_engine.last_state_sync_candle_ts = {}
                    signal_engine.last_stateful_retry_ts = {}
                    signal_engine.last_stateful_diag = {}
                    signal_engine.last_stateful_diag_notice = {}
                    signal_engine.ut_hybrid_timing_latches = {}
                    signal_engine.ut_hybrid_timing_consumed_ts = {}
                await update.message.reply_text(
                    f"✅ RSI+BB 설정 변경: RSI={rsi_length}, BB={bb_length}, x{bb_mult:.2f}"
                )
            
            elif choice == '17':
                # HMA 湲곌컙 蹂寃?(?뺤떇: "9,21")
                parts = val.replace(' ', '').split(',')
                if len(parts) != 2:
                    await update.message.reply_text("❌ 형식: fast,slow (예: 9,21)")
                    return SELECT
                fast, slow = int(parts[0]), int(parts[1])
                if fast >= slow:
                    await update.message.reply_text("❌ fast HMA는 slow HMA보다 작아야 합니다.")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'HMA', 'fast_period'], fast)
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'HMA', 'slow_period'], slow)
                await update.message.reply_text(f"✅ HMA 기간 변경: {fast}/{slow}")

            elif choice == '19':
                parts = val.replace(' ', '').split(',')
                if len(parts) not in (2, 3):
                    await update.message.reply_text("❌ 형식: key,atr,on/off (예: 1,10,off)")
                    return SELECT

                key_value = float(parts[0])
                atr_period = int(parts[1])
                if key_value <= 0 or atr_period < 1:
                    await update.message.reply_text("❌ key는 0보다 커야 하고 ATR 기간은 1 이상이어야 합니다.")
                    return SELECT

                use_ha = self.cfg.get('signal_engine', {}).get('strategy_params', {}).get('UTBot', {}).get('use_heikin_ashi', False)
                if len(parts) == 3:
                    ha_raw = parts[2].lower()
                    if ha_raw in ('on', 'true', '1', 'yes'):
                        use_ha = True
                    elif ha_raw in ('off', 'false', '0', 'no'):
                        use_ha = False
                    else:
                        await update.message.reply_text("❌ HA 옵션은 on/off 로 입력하세요.")
                        return SELECT

                await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBot', 'key_value'], key_value)
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBot', 'atr_period'], atr_period)
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBot', 'use_heikin_ashi'], use_ha)
                signal_engine = self.engines.get('signal')
                if signal_engine:
                    signal_engine.last_processed_candle_ts = {}
                await update.message.reply_text(
                    f"✅ UT Bot 설정 변경: key={key_value:.2f}, ATR={atr_period}, HA={'ON' if use_ha else 'OFF'}"
                )
            
            elif choice == '18':
                # 吏꾩엯紐⑤뱶 蹂寃?(1=cross, 2=position)
                if val == '1':
                    val_lower = 'cross'
                elif val == '2':
                    val_lower = 'position'
                else:
                    await update.message.reply_text("❌ 1(Cross) 또는 2(Position)를 입력하세요.")
                    return SELECT
                
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'entry_mode'], val_lower)
                await update.message.reply_text(f"✅ 진입모드 변경: {val_lower.upper()}")
            
            elif choice == '22':
                mode_map = {
                    '1': BINANCE_TESTNET,
                    '2': BINANCE_MAINNET,
                    '3': UPBIT_MODE
                }
                if val not in mode_map:
                    await update.message.reply_text("❌ 1, 2, 3 중 하나를 입력하세요.\n1=바이낸스 테스트넷, 2=바이낸스 메인넷, 3=업비트 KRW 현물")
                    return SELECT

                target_mode = mode_map[val]
                current_mode = self.get_exchange_mode()

                if target_mode == current_mode:
                    await update.message.reply_text(f"ℹ️ 이미 {self.get_network_status_label(target_mode)} 사용 중입니다.")
                else:
                    target_name = self.get_network_status_label(target_mode)
                    await update.message.reply_text(f"🔄 {target_name}으로 전환 중...")

                    if target_mode == UPBIT_MODE:
                        await self.cfg.update_value(['system_settings', 'trade_direction'], 'long')

                    success, result = await self.reinit_exchange(target_mode)

                    if success:
                        await update.message.reply_text(f"✅ 거래소 전환 완료: {result}")
                    else:
                        await update.message.reply_text(f"❌ 거래소 전환 실패: {result}")
            
            elif choice == '23':
                # Scanner Toggle
                if val in ['1', 'on', 'ON']:
                    new_val = True
                elif val in ['0', 'off', 'OFF']:
                    new_val = False
                else:
                    await update.message.reply_text("❌ 1(ON) 또는 0(OFF)를 입력하세요.")
                    return SELECT
                
                await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], new_val)
                status = "ON 📡" if new_val else "OFF"
                await update.message.reply_text(f"✅ 거래량 급등 채굴: {status}")

            elif choice == '27':
                # R2 Threshold
                v = float(val)
                if v < 0.01 or v > 0.9:
                    await update.message.reply_text("❌ 0.01 ~ 0.9 사이 값을 입력하세요.")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'r2_threshold'], v)
                await update.message.reply_text(f"✅ R2 기준값 변경: {v}")

            elif choice == '31':
                # CHOP Threshold
                v = float(val)
                if v < 0 or v > 100:
                    await update.message.reply_text("❌ 0 ~ 100 사이 값을 입력하세요.")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'chop_threshold'], v)
                await update.message.reply_text(f"✅ CHOP 기준값 변경: {v}")
            
            elif choice == '34':
                # CC Threshold & Length
                parts = val.replace(' ', '').split(',')
                if len(parts) == 1:
                    v = float(parts[0])
                    if v < 0.1 or v > 1.0:
                        await update.message.reply_text("❌ 임계값은 0.1 ~ 1.0 사이여야 합니다.")
                        return SELECT
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'cc_threshold'], v)
                    await update.message.reply_text(f"✅ CC 임계값 변경: {v}")
                elif len(parts) == 2:
                    v = float(parts[0])
                    l = int(parts[1])
                    if v < 0.1 or v > 1.0 or l < 5 or l > 100:
                        await update.message.reply_text("❌ 임계값(0.1~1.0), 기간(5~100)을 확인하세요.")
                        return SELECT
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'cc_threshold'], v)
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'cc_length'], l)
                    await update.message.reply_text(f"✅ CC 설정: 임계값={v}, 기간={l}")
                else:
                    await update.message.reply_text("❌ 형식: 임계값 또는 임계값,기간")
                    return SELECT

            elif choice == '35':
                # 듀얼모드 변경
                if val == '1':
                    new_mode = 'standard'
                    mode_label = '스탠다드'
                elif val == '2':
                    new_mode = 'scalping'
                    mode_label = '스캘핑'
                else:
                    await update.message.reply_text("❌ 1(스탠다드) 또는 2(스캘핑)를 입력하세요.")
                    return SELECT
                
                await self.cfg.update_value(['dual_mode_engine', 'mode'], new_mode)
                await update.message.reply_text(f"✅ 듀얼모드 변경: {mode_label}")
                
                # 利됱떆 ?ъ큹湲고솕 ?몃━嫄?(poll_tick?먯꽌 媛먯??섏?留?紐낆떆??由ъ뀑)
                dm_engine = self.engines.get('dualmode')
                if dm_engine:
                    dm_engine._init_strategy()
            
            elif choice == '41':
                # 청산 타임프레임 변경
                valid_tf = ['1m', '2m', '3m', '4m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', '1d', '3d', '1w', '1M']
                if val not in valid_tf:
                    await update.message.reply_text(f"❌ 유효하지 않은 타임프레임입니다.\n사용 가능: {', '.join(valid_tf)}")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'exit_timeframe'], val)
                # Signal 엔진 캐시 초기화
                signal_engine = self.engines.get('signal')
                if signal_engine:
                    signal_engine.last_processed_exit_candle_ts = {}
                await update.message.reply_text(f"✅ 청산 타임프레임 변경: {val}")

            elif choice == '44':
                parts = val.replace(' ', '').split(',')
                if len(parts) not in (2, 3):
                    await update.message.reply_text("❌ 형식: key,atr,on/off (예: 1,10,off)")
                    return SELECT

                key_value = float(parts[0])
                atr_period = int(parts[1])
                if key_value <= 0 or atr_period < 1:
                    await update.message.reply_text("❌ key는 0보다 커야 하고 ATR 기간은 1 이상이어야 합니다.")
                    return SELECT

                use_ha = self.cfg.get('upbit', {}).get('strategy_params', {}).get('UTBot', {}).get('use_heikin_ashi', False)
                if len(parts) == 3:
                    ha_raw = parts[2].lower()
                    if ha_raw in ('on', 'true', '1', 'yes'):
                        use_ha = True
                    elif ha_raw in ('off', 'false', '0', 'no'):
                        use_ha = False
                    else:
                        await update.message.reply_text("❌ HA 옵션은 on/off 로 입력하세요.")
                        return SELECT

                await self.cfg.update_value(['upbit', 'strategy_params', 'UTBot', 'key_value'], key_value)
                await self.cfg.update_value(['upbit', 'strategy_params', 'UTBot', 'atr_period'], atr_period)
                await self.cfg.update_value(['upbit', 'strategy_params', 'UTBot', 'use_heikin_ashi'], use_ha)
                signal_engine = self.engines.get('signal')
                if signal_engine:
                    signal_engine.last_processed_candle_ts = {}
                await update.message.reply_text(
                    f"✅ 업비트 UT Bot 설정 변경: key={key_value:.2f}, ATR={atr_period}, HA={'ON' if use_ha else 'OFF'}"
                )

            elif choice == '46':
                valid_tf = ['1m', '3m', '5m', '15m', '30m', '1h', '4h', '1d']
                if val not in valid_tf:
                    await update.message.reply_text(f"❌ 유효하지 않은 타임프레임입니다.\n사용 가능: {', '.join(valid_tf)}")
                    return SELECT
                await self.cfg.update_value(['upbit', 'common_settings', 'timeframe'], val)
                await self.cfg.update_value(['upbit', 'common_settings', 'entry_timeframe'], val)
                signal_engine = self.engines.get('signal')
                if signal_engine:
                    signal_engine.last_processed_candle_ts = {}
                    signal_engine.last_candle_time = {}
                await update.message.reply_text(f"✅ 업비트 진입 타임프레임 변경: {val}")

            elif choice == '47':
                valid_tf = ['1m', '3m', '5m', '15m', '30m', '1h', '4h', '1d']
                if val not in valid_tf:
                    await update.message.reply_text(f"❌ 유효하지 않은 타임프레임입니다.\n사용 가능: {', '.join(valid_tf)}")
                    return SELECT
                await self.cfg.update_value(['upbit', 'common_settings', 'exit_timeframe'], val)
                signal_engine = self.engines.get('signal')
                if signal_engine:
                    signal_engine.last_processed_exit_candle_ts = {}
                await update.message.reply_text(f"✅ 업비트 청산 타임프레임 변경: {val}")

            elif choice == '48':
                limit_krw = float(val)
                if limit_krw <= 0:
                    await update.message.reply_text("❌ 업비트 일일 손실 제한은 0보다 커야 합니다.")
                    return SELECT
                await self.cfg.update_value(['upbit', 'common_settings', 'daily_loss_limit'], limit_krw)
                await update.message.reply_text(f"✅ 업비트 일일 손실 제한 변경: ₩{limit_krw:,.0f}")
            
            # 10~41 success message handled
            if choice not in ['2', '3', '10', '11', '12', '14', '15', '16', '17', '18', '19', '20', '21', '22', '23', '26', '27', '28', '30', '31', '33', '34', '35', '41', '44', '45', '46', '47', '48']:
                await update.message.reply_text(f"✅ 설정 완료: {val}")
            await self._restore_main_keyboard(update)
            await self.show_setup_menu(update)
            return SELECT
            
        except ValueError:
            await update.message.reply_text("❌ 올바른 숫자를 입력하세요.")
            return SELECT
        except Exception as e:
            logger.error(f"Setup input error: {e}")
            await update.message.reply_text(f"❌ 오류: {e}")
            return SELECT

    async def setup_symbol_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """?щ낵 蹂寃?泥섎━ - 1/2/3 ?⑥텞???먮뒗 吏곸젒 ?낅젰"""
        choice = update.message.text.strip().upper()
        setup_choice = context.user_data.get('setup_choice')
        new_watchlist = self.cfg.get('signal_engine', {}).get('watchlist', ['BTC/USDT'])
        if not isinstance(new_watchlist, list):
            new_watchlist = ['BTC/USDT']
        upbit_watchlist = self.cfg.get('upbit', {}).get('watchlist', ['BTC/KRW'])
        if not isinstance(upbit_watchlist, list):
            upbit_watchlist = ['BTC/KRW']
        
        # 1/2/3 踰덊샇濡??щ낵 留ㅽ븨 (?⑥텞??
        symbol_map = (
            {'1': 'BTC/KRW', '2': 'ETH/KRW', '3': 'SOL/KRW'}
            if setup_choice == '43' or self.is_upbit_mode()
            else {'1': 'BTC/USDT', '2': 'ETH/USDT', '3': 'SOL/USDT'}
        )
        
        # ?⑥텞???먮뒗 吏곸젒 ?낅젰 ?ъ슜
        if choice in symbol_map:
            symbol = symbol_map[choice]
        else:
            try:
                normalize_mode = UPBIT_MODE if setup_choice == '43' else self.get_exchange_mode()
                symbol = self.normalize_symbol_for_exchange(choice, exchange_mode=normalize_mode)
            except ValueError as ve:
                await update.message.reply_text(f"❌ {ve}")
                return SELECT

        # ?좏슚??寃??(媛꾨떒??Ticker 議고쉶)
        try:
            await asyncio.to_thread(self.exchange.fetch_ticker, symbol)
        except Exception:
            await update.message.reply_text(f"❌ 유효하지 않은 심볼 또는 거래쌍입니다: {symbol}")
            return SELECT
        
        try:
            eng = self.cfg.get('system_settings', {}).get('active_engine', CORE_ENGINE)
            display_symbol = self.format_symbol_for_display(symbol)
            if eng == 'shannon':
                await self.cfg.update_value(['shannon_engine', 'target_symbol'], symbol)
            elif eng == 'dualthrust':
                await self.cfg.update_value(['dual_thrust_engine', 'target_symbol'], symbol)
            elif eng == 'dualmode':
                await self.cfg.update_value(['dual_mode_engine', 'target_symbol'], symbol)
            elif eng == 'tema':
                await self.cfg.update_value(['tema_engine', 'target_symbol'], symbol)
            elif setup_choice == '43':
                await self.cfg.update_value(['upbit', 'watchlist'], [symbol])
                upbit_watchlist = [symbol]
                await update.message.reply_text(f"✅ 업비트 코인 변경: {display_symbol}")
            else:
                if setup_choice == '38':
                    if symbol not in new_watchlist:
                        new_watchlist = new_watchlist + [symbol]
                        await self.cfg.update_value(['signal_engine', 'watchlist'], new_watchlist)
                        await update.message.reply_text(f"✅ 감시 심볼 추가: {display_symbol}")
                    else:
                        await update.message.reply_text(f"ℹ️ 이미 감시 목록에 있습니다: {display_symbol}")
                else:
                    # Signal ?붿쭊: 硫붾돱?먯꽌 蹂寃???Watchlist瑜??대떦 ?щ낵濡?**?泥?* (湲곗〈 ?숈옉 ?좎?)
                    await self.cfg.update_value(['signal_engine', 'watchlist'], [symbol])
                    new_watchlist = [symbol]
                    await update.message.reply_text("ℹ️ Signal 엔진 감시 목록이 해당 심볼로 초기화되었습니다.")
            
            # 留덉폆 ?ㅼ젙 ?곸슜
            # 留덉폆 ?ㅼ젙 ?곸슜
            if self.active_engine:
                await self.active_engine.ensure_market_settings(symbol)
            
            # Shannon ?붿쭊 罹먯떆 珥덇린??(?щ낵 蹂寃????꾩닔!)
            shannon_engine = self.engines.get('shannon')
            if shannon_engine:
                shannon_engine.ema_200 = None
                shannon_engine.atr_value = None
                shannon_engine.trend_direction = None
                shannon_engine.last_indicator_update = 0
                shannon_engine.position_cache = None
                shannon_engine.grid_orders = []
                logger.info(f"?봽 Shannon engine cache cleared for new symbol: {symbol}")
            
            # Signal ?붿쭊 罹먯떆??珥덇린??
            signal_engine = self.engines.get('signal')
            if signal_engine:
                signal_engine.position_cache = None
                if setup_choice == '38':
                    signal_engine.active_symbols.add(symbol)
                    await signal_engine.prime_symbol_to_next_closed_candle(symbol)
                elif setup_choice == '43':
                    signal_engine.last_candle_time = {}
                    signal_engine.last_processed_candle_ts = {}
                    signal_engine.last_state_sync_candle_ts = {}
                    signal_engine.last_stateful_retry_ts = {}
                    signal_engine.last_processed_exit_candle_ts = {}
                    signal_engine.active_symbols.clear()
                    signal_engine.active_symbols.add(symbol)
                    await signal_engine.prime_symbol_to_next_closed_candle(symbol)
                else:
                    signal_engine.last_candle_time = {} # Dict reset
                    signal_engine.last_processed_candle_ts = {}
                    signal_engine.last_state_sync_candle_ts = {}
                    signal_engine.last_stateful_retry_ts = {}
                    signal_engine.last_processed_exit_candle_ts = {}
                    signal_engine.active_symbols.clear() # 湲곗〈 ?섎룞 紐⑸줉??珥덇린??(紐낇솗?깆쓣 ?꾪빐)
                    signal_engine.active_symbols.add(symbol)
                    await signal_engine.prime_symbol_to_next_closed_candle(symbol)
            
            # Dual Thrust ?붿쭊 罹먯떆??珥덇린??
            dt_engine = self.engines.get('dualthrust')
            if dt_engine:
                dt_engine.position_cache = None
                dt_engine.trigger_date = None  # ?몃━嫄??ш퀎??
                logger.info(f"?봽 DualThrust engine cache cleared for new symbol: {symbol}")

            # TEMA ?붿쭊 罹먯떆 珥덇린??
            tema_engine = self.engines.get('tema')
            if tema_engine:
                tema_engine.last_candle_time = 0
                tema_engine.ema1 = None
                tema_engine.ema2 = None
                tema_engine.ema3 = None
                logger.info(f"?봽 TEMA engine cache cleared for new symbol: {symbol}")
            
            if setup_choice == '38':
                watchlist_text = ", ".join(new_watchlist)
                await update.message.reply_text(f"✅ 감시 목록: {watchlist_text}")
            elif setup_choice == '43':
                watchlist_text = ", ".join(self.format_symbol_for_display(s) for s in upbit_watchlist)
                await update.message.reply_text(f"✅ 업비트 감시 코인: {watchlist_text}")
            else:
                await update.message.reply_text(f"✅ 심볼 변경 완료: {display_symbol}")
            await self._restore_main_keyboard(update)
            await self.show_setup_menu(update)
            return SELECT
            
        except Exception as e:
            logger.error(f"Symbol change error: {e}")
            await update.message.reply_text(f"❌ 심볼 변경 실패: {e}")
            return SELECT

    async def setup_direction_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """留ㅻℓ 諛⑺뼢 ?좏깮 泥섎━"""
        text = update.message.text

        if self.is_upbit_mode():
            await self.cfg.update_value(['system_settings', 'trade_direction'], 'long')
            await update.message.reply_text("ℹ️ 업비트 KRW 현물은 숏이 없어 매매 방향이 `롱만`으로 고정됩니다.", parse_mode=ParseMode.MARKDOWN)
            await self._restore_main_keyboard(update)
            await self.show_setup_menu(update)
            return SELECT
        
        direction_map = {
            '양방향': 'both',
            'Long+Short': 'both',
            '롱만': 'long',
            'Long Only': 'long',
            '숏만': 'short',
            'Short Only': 'short'
        }
        
        direction = None
        for key, val in direction_map.items():
            if key in text:
                direction = val
                break
        
        if direction:
            await self.cfg.update_value(['system_settings', 'trade_direction'], direction)
            await update.message.reply_text(f"✅ 매매 방향 변경: {direction}")
        else:
            await update.message.reply_text("❌ 유효하지 않은 선택입니다.")
        
        await self._restore_main_keyboard(update)
        await self.show_setup_menu(update)
        return SELECT

    async def setup_engine_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """?붿쭊 援먯껜 泥섎━"""
        text = update.message.text.strip()
        
        mode_map = {'1': CORE_ENGINE}
        
        if text in mode_map:
            mode = mode_map[text]
            if mode == 'dualmode' and not DUAL_MODE_AVAILABLE:
                await update.message.reply_text("❌ DualMode 관련 모듈이 없어 사용할 수 없습니다.")
            else:
                await self.cfg.update_value(['system_settings', 'active_engine'], mode)
                await self._switch_engine(mode)
                await update.message.reply_text(f"✅ 엔진 변경 완료: {mode.upper()}")
        else:
            await update.message.reply_text("ℹ️ 코어 모드에서는 `1 (Signal)`만 사용 가능합니다.", parse_mode=ParseMode.MARKDOWN)
        
        await self._restore_main_keyboard(update)
        await self.show_setup_menu(update)
        return SELECT

    def _build_main_keyboard(self):
        kb = [
            [KeyboardButton("🚨 STOP"), KeyboardButton("⏸ PAUSE"), KeyboardButton("▶ RESUME")],
            [KeyboardButton("/setup"), KeyboardButton("/status"), KeyboardButton("/history")],
            [KeyboardButton("/log"), KeyboardButton("/help")]
        ]
        return ReplyKeyboardMarkup(kb, resize_keyboard=True)

    async def _restore_main_keyboard(self, update: Update):
        """Restore the main keyboard."""
        await update.message.reply_text("📱 메인 메뉴", reply_markup=self._build_main_keyboard())

    async def _setup_telegram(self):
        markup = self._build_main_keyboard()
        text_filter = filters.TEXT & ~filters.COMMAND

        async def start_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            # Re-bind chat_id to the current chat to avoid stale configuration lockout.
            incoming_chat_id = u.effective_chat.id if u.effective_chat else 0
            configured_chat_id = self.cfg.get_chat_id()
            if incoming_chat_id and configured_chat_id != incoming_chat_id:
                await self.cfg.update_value(['telegram', 'chat_id'], incoming_chat_id)
                logger.info(f"Telegram chat_id updated: {incoming_chat_id}")
            await u.message.reply_text("🤖 봇 준비 완료", reply_markup=markup)

        async def strat_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            if not c.args:
                await u.message.reply_text("사용법: /strat 번호\n1: Signal")
                return
            mode_map = {'1': CORE_ENGINE}
            arg = c.args[0]
            if arg not in mode_map:
                await u.message.reply_text("잘못된 입력입니다. 현재는 `1 (Signal)`만 사용 가능합니다.", parse_mode=ParseMode.MARKDOWN)
                return
            mode = mode_map[arg]
            await self.cfg.update_value(['system_settings', 'active_engine'], mode)
            await self._switch_engine(mode)
            await u.message.reply_text(f"✅ 전략 변경: {mode.upper()}")

        async def log_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            logs = list(log_buffer)[-15:]
            if logs:
                await u.message.reply_text("\n".join(logs))
            else:
                await u.message.reply_text("📝 최근 로그가 없습니다.")

        async def history_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            limit = 3
            if c.args:
                try:
                    limit = int(c.args[0])
                except ValueError:
                    await u.message.reply_text("사용법: /history 또는 /history 3")
                    return
            limit = max(1, min(limit, 5))
            rows = self.db.get_recent_status_history(limit=limit + 1)
            prev_rows = rows[1:] if len(rows) > 1 else []
            if not prev_rows:
                await u.message.reply_text("📝 지난 상태 이력이 없습니다.")
                return

            await u.message.reply_text(
                f"🕘 최근 상태 이력 {len(prev_rows)}건을 표시합니다. (`/history {limit}`)",
                parse_mode=ParseMode.MARKDOWN
            )
            for created_at, snapshot_text in prev_rows:
                await u.message.reply_text(
                    f"`{created_at}`\n{snapshot_text}",
                    parse_mode=ParseMode.MARKDOWN
                )

        async def close_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            await self.emergency_stop()
            await u.message.reply_text("🚨 긴급 정지 완료")

        async def stats_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            daily_count, daily_pnl = self.db.get_daily_stats()
            weekly_count, weekly_pnl = self.db.get_weekly_stats()

            quote_currency = 'KRW' if self.is_upbit_mode() else 'USDT'

            def fmt_stats_pnl(value):
                amount = float(value or 0)
                prefix = '+' if amount >= 0 else ''
                if quote_currency == 'KRW':
                    return f"{prefix}₩{amount:,.0f}"
                return f"{prefix}${amount:.2f}"

            msg = f"""
📊 **매매 통계**

**오늘**
- 거래: {daily_count}건
- 손익: {fmt_stats_pnl(daily_pnl)}

**7일**
- 거래: {weekly_count}건
- 손익: {fmt_stats_pnl(weekly_pnl)}
"""
            await u.message.reply_text(msg.strip(), parse_mode=ParseMode.MARKDOWN)

        async def help_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            msg = """
📚 **명령어**

/start - 메인 메뉴 표시
/setup - 설정 메뉴
/status - 대시보드 갱신
/history - 지난 상태 조회
/stats - 통계
/log - 최근 로그
/close - 긴급 청산

🚨 STOP - 긴급 정지
⏸ PAUSE - 일시정지
▶ RESUME - 재개
"""
            await u.message.reply_text(msg.strip(), parse_mode=ParseMode.MARKDOWN)

        emergency_handler = MessageHandler(filters.Regex("STOP|PAUSE|RESUME|/status"), self.global_handler)
        self.tg_app.add_handler(emergency_handler, group=-1)

        self.tg_app.add_handler(CommandHandler("start", start_cmd))
        self.tg_app.add_handler(CommandHandler("strat", strat_cmd))
        self.tg_app.add_handler(CommandHandler("history", history_cmd))
        self.tg_app.add_handler(CommandHandler("log", log_cmd))
        self.tg_app.add_handler(CommandHandler("close", close_cmd))
        self.tg_app.add_handler(CommandHandler("stats", stats_cmd))
        self.tg_app.add_handler(CommandHandler("help", help_cmd))

        conv = ConversationHandler(
            entry_points=[CommandHandler('setup', self.setup_entry)],
            states={
                SELECT: [MessageHandler(text_filter, self.setup_select)],
                INPUT: [MessageHandler(text_filter, self.setup_input)],
                SYMBOL_INPUT: [MessageHandler(text_filter, self.setup_symbol_input)],
                DIRECTION_SELECT: [MessageHandler(text_filter, self.setup_direction_select)],
                ENGINE_SELECT: [MessageHandler(text_filter, self.setup_engine_select)],
                "R2_SELECT": [MessageHandler(text_filter, self.setup_r2_select)],
                "CHOP_SELECT": [MessageHandler(text_filter, self.setup_chop_select)],
                "KALMAN_SELECT": [MessageHandler(text_filter, self.setup_kalman_select)]
            },
            fallbacks=[
                CommandHandler('setup', self.setup_entry),
                emergency_handler
            ]
        )

        self.tg_app.add_handler(conv)

        async def manual_symbol_handler(u: Update, c: ContextTypes.DEFAULT_TYPE):
            text = u.message.text.strip().upper()
            if re.match(r'^[A-Z0-9]{2,15}([/-][A-Z0-9]{2,15})?(:[A-Z0-9]+)?$', text):
                if text.startswith('/'):
                    return
                await self.handle_manual_symbol_input(u, text)

        self.tg_app.add_handler(MessageHandler(text_filter, manual_symbol_handler))

    async def setup_r2_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        if "1" in text:
            curr = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('r2_entry_enabled', True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'r2_entry_enabled'], not curr)
            await update.message.reply_text(f"✅ R2 Entry: {'ON' if not curr else 'OFF'}")
        elif "2" in text:
            curr = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('r2_exit_enabled', True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'r2_exit_enabled'], not curr)
            await update.message.reply_text(f"✅ R2 Exit: {'ON' if not curr else 'OFF'}")
        
        await self._restore_main_keyboard(update)
        await self.show_setup_menu(update)
        return SELECT

    async def setup_chop_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        if "1" in text:
            curr = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('chop_entry_enabled', True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'chop_entry_enabled'], not curr)
            await update.message.reply_text(f"✅ CHOP Entry: {'ON' if not curr else 'OFF'}")
        elif "2" in text:
            curr = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('chop_exit_enabled', True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'chop_exit_enabled'], not curr)
            await update.message.reply_text(f"✅ CHOP Exit: {'ON' if not curr else 'OFF'}")
        
        await self._restore_main_keyboard(update)
        await self.show_setup_menu(update)
        return SELECT

    async def setup_kalman_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        if "1" in text:
            curr = self.cfg.get('signal_engine', {}).get('strategy_params', {}).get('kalman_filter', {}).get('entry_enabled', False)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'kalman_filter', 'entry_enabled'], not curr)
            await update.message.reply_text(f"✅ Kalman Entry: {'ON' if not curr else 'OFF'}")
        elif "2" in text:
            curr = self.cfg.get('signal_engine', {}).get('strategy_params', {}).get('kalman_filter', {}).get('exit_enabled', False)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'kalman_filter', 'exit_enabled'], not curr)
            await update.message.reply_text(f"✅ Kalman Exit: {'ON' if not curr else 'OFF'}")
            
        await self._restore_main_keyboard(update)
        await self.show_setup_menu(update)
        return SELECT

    # ---------------- Hourly Report ----------------
    async def _hourly_report_loop(self):
        """시간별 리포트."""
        await asyncio.sleep(60)  # ?쒖옉 ?湲?
        
        while True:
            try:
                reporting = self.cfg.get('telegram', {}).get('reporting', {})
                if reporting.get('hourly_report_enabled', False):
                    now = datetime.now()
                    if now.minute == 0 and time.time() - self.last_hourly_report > 3500:
                        self.last_hourly_report = time.time()
                        
                        daily_count, daily_pnl = self.db.get_daily_stats()
                        d = {}
                        if isinstance(self.status_data, dict) and self.status_data:
                            first_key = next(iter(self.status_data))
                            first_val = self.status_data.get(first_key)
                            if isinstance(first_val, dict):
                                d = first_val

                        quote_currency = d.get('quote_currency', 'KRW' if self.is_upbit_mode() else 'USDT')

                        def fmt_report_money(value, signed=False):
                            amount = float(value or 0)
                            if str(quote_currency).upper() == 'KRW':
                                prefix = '+' if signed and amount >= 0 else ''
                                return f"{prefix}₩{amount:,.0f}"
                            prefix = '+' if signed and amount >= 0 else ''
                            return f"{prefix}${amount:.2f}"
                        
                        msg = f"""
⏱ **시간별 리포트** [{now.strftime('%H:%M')}]

💰 자산: {fmt_report_money(d.get('total_equity', 0))}
📈 일일 손익: {fmt_report_money(daily_pnl, signed=True)} ({daily_count}건)
🛡 MMR: {d.get('mmr', 0):.2f}%
"""
                        await self.notify(msg.strip())
                
                await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"Hourly report error: {e}")
                await asyncio.sleep(60)

    async def _heartbeat_loop(self):
        """런타임 heartbeat 파일 갱신."""
        await asyncio.sleep(5)

        while True:
            try:
                self._write_heartbeat()
                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"Heartbeat loop error: {e}")
                await asyncio.sleep(30)

    # ---------------- [?쒖닔 ?대쭅] 硫붿씤 ?대쭅 猷⑦봽 ----------------
    async def _main_polling_loop(self):
        """
        ?쒖닔 ?대쭅 硫붿씤 猷⑦봽 - WebSocket ?놁씠 紐⑤뱺 寃껋쓣 泥섎━
        - Signal ?붿쭊: 媛寃?紐⑤땲?곕쭅 + 罹붾뱾 ?좏샇 泥댄겕
        - Shannon ?붿쭊: 媛寃?紐⑤땲?곕쭅 + 由щ갭?곗떛 泥댄겕
        """
        logger.info("?봽 [Polling] Main polling loop started (Pure Polling Mode)")
        await asyncio.sleep(3)  # ?쒖옉 ?湲?
        
        while True:
            try:
                eng = self.cfg.get('system_settings', {}).get('active_engine', CORE_ENGINE)
                
                if eng == 'signal' and self.active_engine and self.active_engine.running:
                    # Signal ?붿쭊 ?대쭅
                    signal_engine = self.engines.get('signal')
                    if signal_engine and hasattr(signal_engine, 'poll_tick'):
                        await signal_engine.poll_tick()
                        
                elif eng == 'shannon' and self.active_engine and self.active_engine.running:
                    # Shannon ?붿쭊 ?대쭅
                    shannon_engine = self.engines.get('shannon')
                    if shannon_engine and hasattr(shannon_engine, 'poll_tick'):
                        await shannon_engine.poll_tick()
                
                elif eng == 'dualthrust' and self.active_engine and self.active_engine.running:
                    # Dual Thrust ?붿쭊 ?대쭅
                    dt_engine = self.engines.get('dualthrust')
                    if dt_engine and hasattr(dt_engine, 'poll_tick'):
                        await dt_engine.poll_tick()

                elif eng == 'dualmode' and self.active_engine and self.active_engine.running:
                    # Dual Mode ?붿쭊 ?대쭅
                    dm_engine = self.engines.get('dualmode')
                    if dm_engine and hasattr(dm_engine, 'poll_tick'):
                        await dm_engine.poll_tick()

                elif eng == 'tema' and self.active_engine and self.active_engine.running:
                    # TEMA ?붿쭊 ?대쭅
                    tema_engine = self.engines.get('tema')
                    if tema_engine and hasattr(tema_engine, 'poll_tick'):
                        await tema_engine.poll_tick()
                
                # [MODIFIED] Prioritize entry_timeframe for polling interval
                sys_cfg = self.get_active_common_settings()
                tf = sys_cfg.get('entry_timeframe', sys_cfg.get('timeframe', '15m'))
                poll_interval = self._get_poll_interval(tf)
                await asyncio.sleep(poll_interval)
                
            except Exception as e:
                logger.error(f"Main polling loop error: {e}")
                await asyncio.sleep(30)

    def _get_poll_interval(self, tf):
        """??꾪봽?덉엫???곕Ⅸ ?대쭅 媛꾧꺽 怨꾩궛"""
        tf_seconds = {
            '1m': 60, '3m': 180, '5m': 300, '15m': 900,
            '30m': 1800, '1h': 3600, '2h': 7200, '4h': 14400,
            '6h': 21600, '8h': 28800, '12h': 43200, '1d': 86400
        }
        candle_seconds = tf_seconds.get(tf, 900)  # 湲곕낯 15遺?
        # 罹붾뱾 ?쒓컙??1/6 媛꾧꺽?쇰줈 ?대쭅 (理쒖냼 10珥? 理쒕? 60珥?
        # 2H = 7200珥???1200珥?20遺?... 理쒕? 60珥덈줈 ?쒗븳
        return max(10, min(60, candle_seconds // 6))

    # ---------------- ??쒕낫??----------------
    async def _dashboard_loop(self):
        cid = self.cfg.get_chat_id()
        if not cid:
            logger.error("??Invalid chat_id - Dashboard disabled")
            return
        
        await asyncio.sleep(3)
        
        while True:
            try:
                if self.cfg.get('system_settings', {}).get('show_dashboard', True):
                    await self._refresh_dashboard()
                
                interval = self.cfg.get('system_settings', {}).get('monitoring_interval_seconds', 10)
                await asyncio.sleep(interval)
                
            except Exception as e:
                logger.error(f"Dashboard loop error: {e}")
                await asyncio.sleep(10)


    async def _refresh_dashboard(self, force=False):
        if not force and not self.cfg.get('system_settings', {}).get('show_dashboard', True):
            return False

        self.blink_state = not self.blink_state
        blink = "●" if self.blink_state else "○"
        pause_indicator = " [PAUSED]" if self.is_paused else ""
        all_data = self.status_data if isinstance(self.status_data, dict) else {}
        live_text, history_text, snapshot_key = self._build_dashboard_messages(all_data, blink, pause_indicator)
        self._record_status_snapshot(snapshot_key, history_text)
        return await self._upsert_dashboard_message(live_text)

    def _record_status_snapshot(self, snapshot_key, snapshot_text):
        if not snapshot_key or snapshot_key == self.last_status_snapshot_key:
            return
        self.db.log_status_snapshot(snapshot_key, snapshot_text)
        self.last_status_snapshot_key = snapshot_key

    async def _upsert_dashboard_message(self, text):
        cid = self.cfg.get_chat_id()
        if not cid or not self.tg_app:
            logger.error("Invalid chat_id - dashboard refresh skipped")
            return False

        plain_text = text.replace('**', '').replace('`', '')

        if self.dashboard_msg_id:
            try:
                if self.dashboard_plain_text_mode:
                    await self.tg_app.bot.edit_message_text(
                        chat_id=cid,
                        message_id=self.dashboard_msg_id,
                        text=plain_text
                    )
                else:
                    await self.tg_app.bot.edit_message_text(
                        chat_id=cid,
                        message_id=self.dashboard_msg_id,
                        text=text,
                        parse_mode=ParseMode.MARKDOWN
                    )
                return True
            except RetryAfter as e:
                logger.warning(f"Flood Wait: Sleeping {e.retry_after}s")
                await asyncio.sleep(e.retry_after)
                try:
                    if self.dashboard_plain_text_mode:
                        await self.tg_app.bot.edit_message_text(
                            chat_id=cid,
                            message_id=self.dashboard_msg_id,
                            text=plain_text
                        )
                    else:
                        await self.tg_app.bot.edit_message_text(
                            chat_id=cid,
                            message_id=self.dashboard_msg_id,
                            text=text,
                            parse_mode=ParseMode.MARKDOWN
                        )
                    return True
                except Exception as retry_error:
                    logger.warning(f"Dashboard retry edit error: {retry_error}")
                    self.dashboard_msg_id = None
            except TimedOut as e:
                logger.warning(f"Dashboard edit timeout: {e}")
            except BadRequest as e:
                if "Message is not modified" in str(e):
                    return True
                if "can't parse entities" in str(e).lower():
                    logger.warning("Dashboard markdown parse failed on edit; switching to plain text mode")
                    self.dashboard_plain_text_mode = True
                    try:
                        await self.tg_app.bot.edit_message_text(
                            chat_id=cid,
                            message_id=self.dashboard_msg_id,
                            text=plain_text
                        )
                        return True
                    except Exception as plain_error:
                        logger.warning(f"Dashboard plain text edit error: {plain_error}")
                        self.dashboard_msg_id = None
                        return False
                logger.warning(f"Dashboard edit error: {e}")
                if "message to edit not found" in str(e).lower():
                    self.dashboard_msg_id = None
            except Exception as e:
                logger.error(f"Dashboard error: {e}")

        try:
            if self.dashboard_plain_text_mode:
                m = await self.tg_app.bot.send_message(
                    chat_id=cid,
                    text=plain_text
                )
            else:
                m = await self.tg_app.bot.send_message(
                    chat_id=cid,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN
                )
            self.dashboard_msg_id = m.message_id
            return True
        except RetryAfter as e:
            logger.warning(f"Send Flood Wait: {e.retry_after}s")
            await asyncio.sleep(min(e.retry_after, 60))
        except TimedOut as e:
            logger.warning(f"Dashboard send timeout: {e}")
        except BadRequest as e:
            if "can't parse entities" in str(e).lower():
                logger.warning("Dashboard markdown parse failed on send; switching to plain text mode")
                self.dashboard_plain_text_mode = True
                try:
                    m = await self.tg_app.bot.send_message(chat_id=cid, text=plain_text)
                    self.dashboard_msg_id = m.message_id
                    return True
                except Exception as plain_error:
                    logger.error(f"Dashboard plain text send error: {plain_error}")
                    return False
            logger.error(f"Dashboard send error: {e}")
        except Exception as e:
            logger.error(f"Dashboard send error: {e}")
        return False

    def _build_dashboard_messages(self, all_data, blink, pause_indicator):
        eng = self.cfg.get('system_settings', {}).get('active_engine', 'unknown').upper()
        now = datetime.now()
        body = self._format_dashboard_body(all_data)
        live_text = f"{blink} **[{eng}] 대시보드**{pause_indicator} [{now.strftime('%H:%M:%S')}]\n\n{body}"
        history_text = f"**[{eng}] 상태 이력**{pause_indicator} [{now.strftime('%Y-%m-%d %H:%M:%S')}]\n\n{body}"
        snapshot_key = f"{eng}|{pause_indicator}|{body}"
        return live_text, history_text, snapshot_key

    def _format_dashboard_body(self, all_data):
        """텔레그램 대시보드 본문 생성."""
        try:
            if not all_data:
                return "데이터 수집 대기 중..."

            def fmt_money(value, quote_currency):
                amount = float(value or 0)
                if str(quote_currency).upper() == 'KRW':
                    return f"₩{amount:,.0f}"
                return f"${amount:.2f}"

            def fmt_signed_money(value, quote_currency):
                amount = float(value or 0)
                prefix = '+' if amount >= 0 else ''
                if str(quote_currency).upper() == 'KRW':
                    return f"{prefix}₩{amount:,.0f}"
                return f"{prefix}${amount:.2f}"

            def fmt_price(value, quote_currency):
                amount = float(value or 0)
                if str(quote_currency).upper() == 'KRW':
                    return f"{amount:,.0f}"
                return f"{amount:.2f}"

            msg = ""
            summary_candidates = [v for v in all_data.values() if isinstance(v, dict)]
            d_first = max(
                summary_candidates,
                key=lambda row: float(row.get('total_equity', 0) or 0),
                default=next(iter(all_data.values()))
            )
            first_quote = d_first.get('quote_currency', 'KRW' if self.is_upbit_mode() else 'USDT')

            msg += "💰 **자산 요약**\n"
            msg += (
                f"총자산: `{fmt_money(d_first.get('total_equity', 0), first_quote)}` | "
                f"가용자산: `{fmt_money(d_first.get('free_usdt', 0), first_quote)}`\n"
            )
            msg += f"MMR: `{d_first.get('mmr', 0):.2f}%` | 일일 PnL: `{fmt_signed_money(d_first.get('daily_pnl', 0), first_quote)}`\n"
            runtime_diag = d_first.get('runtime_diag', {})
            if runtime_diag:
                msg += (
                    f"♻️ 런타임: `{runtime_diag.get('uptime_human', '-')}` | "
                    f"시작 `{runtime_diag.get('launch_reason', '-')}` | "
                    f"메모리 `{runtime_diag.get('rss_mb', '-')}`MB\n"
                )
                restart_bits = []
                if runtime_diag.get('last_heartbeat_age') is not None:
                    restart_bits.append(f"이전 heartbeat `{runtime_diag.get('last_heartbeat_age_human', '-')}`")
                if runtime_diag.get('last_pid_before_start'):
                    restart_bits.append(f"이전 PID `{runtime_diag.get('last_pid_before_start')}`")
                if restart_bits:
                    msg += f"🧷 최근 재시작: {' | '.join(restart_bits)}\n"
                if runtime_diag.get('last_log_line'):
                    msg += f"🪵 직전 로그: `{runtime_diag.get('last_log_line')}`\n"
                if runtime_diag.get('last_exit_reason'):
                    exit_bits = [
                        f"사유 `{runtime_diag.get('last_exit_reason')}`",
                        f"시각 `{runtime_diag.get('last_exit_ts', '-')}`"
                    ]
                    if runtime_diag.get('last_exit_uptime_human'):
                        exit_bits.append(f"uptime `{runtime_diag.get('last_exit_uptime_human')}`")
                    if runtime_diag.get('last_exit_rss_mb') is not None:
                        exit_bits.append(f"mem `{runtime_diag.get('last_exit_rss_mb')}`MB")
                    msg += f"🧯 직전 종료: {' | '.join(exit_bits)}\n"
                if runtime_diag.get('last_exit_detail'):
                    msg += f"🧾 종료상세: `{runtime_diag.get('last_exit_detail')}`\n"
            msg += "━━━━━━━━━━\n"

            for symbol, d in all_data.items():
                cur_price = d.get('price', 0)
                pos_side = d.get('pos_side', 'NONE')
                lev = d.get('leverage', '?')
                mm = d.get('margin_mode', 'ISO')
                quote_currency = d.get('quote_currency', first_quote)
                symbol_label = self.format_symbol_for_display(symbol)
                mode_str = "(SPOT)" if d.get('is_spot') else (f"({mm} {lev}x)" if 'leverage' in d else "")

                pos_icon = "🟢" if pos_side == 'LONG' else "🔴" if pos_side == 'SHORT' else "⚪"
                msg += f"{pos_icon} **{symbol_label}** {mode_str} | `{pos_side}`\n"

                if pos_side != 'NONE':
                    pnl_pct = d.get('pnl_pct', 0)
                    pnl_icon = "📈" if pnl_pct >= 0 else "📉"
                    msg += f"{pnl_icon} PnL: `{fmt_signed_money(d.get('pnl_usdt', 0), quote_currency)}` (`{pnl_pct:+.2f}%`)\n"
                    msg += (
                        f"진입가: `{fmt_price(d.get('entry_price', 0), quote_currency)}` | "
                        f"현재가: `{fmt_price(cur_price, quote_currency)}`\n"
                    )
                else:
                    msg += f"현재가: `{fmt_price(cur_price, quote_currency)}`\n"

                d_eng = d.get('engine', '').upper()
                if d_eng == 'SIGNAL':
                    e_tf = d.get('entry_tf', '?')
                    x_tf = d.get('exit_tf', '?')
                    entry_reason = d.get('entry_reason', '대기')
                    protection_cfg = d.get('protection_config', {})
                    tp_text = "ON" if protection_cfg.get('tp_enabled', False) else "OFF"
                    sl_text = "ON" if protection_cfg.get('sl_enabled', False) else "OFF"
                    network = d.get('network', '?')
                    exchange_id = d.get('exchange_id', '?')
                    market_data_exchange_id = d.get('market_data_exchange_id', '?')
                    market_data_source = d.get('market_data_source', 'BINANCE FUTURES MAINNET PUBLIC')
                    msg += f"⏱ TF: 진입 `{e_tf}` / 청산 `{x_tf}`\n"
                    msg += f"🌐 거래소: `{exchange_id}` | 네트워크 `{network}`\n"
                    msg += f"📡 신호데이터: `{market_data_exchange_id}` | `{market_data_source}`\n"
                    msg += f"🛡 보호주문: TP `{tp_text}` | SL `{sl_text}`\n"
                    msg += f"📝 진입판정: `{entry_reason}`\n"
                    stateful_diag = d.get('stateful_diag', {})
                    if stateful_diag:
                        msg += (
                            f"🧪 상태진단: stage `{stateful_diag.get('stage', '-')}` | "
                            f"raw `{stateful_diag.get('raw_state', '-')}` | "
                            f"entry `{stateful_diag.get('entry_sig', '-')}` | "
                            f"pos `{stateful_diag.get('pos_side', '-')}`\n"
                        )
                        if stateful_diag.get('timing_label'):
                            timing_line = (
                                f"타이밍: `{stateful_diag.get('timing_label')}` | "
                                f"raw `{stateful_diag.get('timing_signal', '-')}`"
                            )
                            if stateful_diag.get('timing_signal_ts_human'):
                                timing_line += f" | signal `{stateful_diag.get('timing_signal_ts_human')}`"
                            if stateful_diag.get('effective_timing_signal') not in (None, 'none'):
                                timing_line += f" | effective `{stateful_diag.get('effective_timing_signal')}`"
                            if stateful_diag.get('timing_match_source'):
                                timing_line += f" | source `{stateful_diag.get('timing_match_source')}`"
                            msg += timing_line + "\n"
                        if stateful_diag.get('latched_timing_signal'):
                            latched_line = (
                                f"저장신호: `{stateful_diag.get('latched_timing_signal')}` | "
                                f"usable `{stateful_diag.get('latched_timing_available')}`"
                            )
                            if stateful_diag.get('latched_timing_signal_ts_human'):
                                latched_line += f" | ts `{stateful_diag.get('latched_timing_signal_ts_human')}`"
                            msg += latched_line + "\n"
                        if stateful_diag.get('timing_reason'):
                            msg += f"타이밍판정: `{stateful_diag.get('timing_reason')}`\n"
                        if stateful_diag.get('ut_key') is not None:
                            msg += (
                                f"UT 설정: K `{stateful_diag.get('ut_key')}` | "
                                f"ATR `{stateful_diag.get('ut_atr')}` | "
                                f"HA `{stateful_diag.get('ut_ha', '-')}`\n"
                            )
                        if stateful_diag.get('src') is not None and stateful_diag.get('stop') is not None:
                            msg += (
                                f"UT 값: src `{float(stateful_diag.get('src')):.2f}` | "
                                f"stop `{float(stateful_diag.get('stop')):.2f}`\n"
                            )
                        if stateful_diag.get('signal_ts_human') or stateful_diag.get('feed_last_ts_human'):
                            msg += (
                                f"UT 캔들: tf `{stateful_diag.get('tf_used', '?')}` | "
                                f"signal `{stateful_diag.get('signal_ts_human', '?')}` | "
                                f"feed_last `{stateful_diag.get('feed_last_ts_human', '?')}`\n"
                            )
                        if stateful_diag.get('closed_ohlc_text'):
                            msg += f"UT 확정봉: `{stateful_diag.get('closed_ohlc_text')}`\n"
                        if stateful_diag.get('live_ohlc_text'):
                            msg += f"UT 진행봉: `{stateful_diag.get('live_ohlc_text')}`\n"
                        diag_note = stateful_diag.get('note')
                        if diag_note:
                            msg += f"진단메모: `{diag_note}`\n"

                    f_cfg = d.get('filter_config', {})
                    entry_st = d.get('entry_filters', {})
                    exit_st = d.get('exit_filters', {})

                    def get_st_text(st_dict, cfg_key, val_key, pass_key, is_entry=True):
                        en_key = 'en_entry' if is_entry else 'en_exit'
                        if not f_cfg.get(cfg_key, {}).get(en_key, False):
                            return "-"
                        val = st_dict.get(val_key, 0.0)
                        if val == 0.0 and not is_entry:
                            return "~"
                        return "PASS" if st_dict.get(pass_key, False) else "FAIL"

                    e_r2 = get_st_text(entry_st, 'r2', 'r2_val', 'r2_pass', True)
                    e_c = get_st_text(entry_st, 'chop', 'chop_val', 'chop_pass', True)

                    x_r2 = get_st_text(exit_st, 'r2', 'r2_val', 'r2_pass', False)
                    x_c = get_st_text(exit_st, 'chop', 'chop_val', 'chop_pass', False)
                    x_cc = get_st_text(exit_st, 'cc', 'cc_val', 'cc_pass', False)
                    cc_val = exit_st.get('cc_val', 0.0)

                    msg += f"🧪 필터(진입): R2 {e_r2} | CHOP {e_c}\n"
                    msg += f"🧪 필터(청산): R2 {x_r2} | CHOP {x_c} | CC {x_cc}({cc_val:.2f})\n"

                    active_strat = d.get('active_strategy', '')
                    if active_strat == 'MICROVBO':
                        vbo = d.get('vbo_breakout_level', {})
                        if vbo:
                            msg += f"VBO: `L:{vbo.get('long', 0):.1f} / S:{vbo.get('short', 0):.1f}`\n"
                    elif active_strat == 'FRACTALFISHER':
                        msg += f"FF: `H:{d.get('fisher_hurst', 0):.2f} / F:{d.get('fisher_value', 0):.2f}`\n"
                        if d.get('fisher_trailing_stop') and pos_side != 'NONE':
                            msg += f"TS: `{d.get('fisher_trailing_stop', 0):.2f}`\n"

                elif d_eng == 'SHANNON':
                    msg += f"추세: `{d.get('trend', 'N/A')}` | EMA200: `{d.get('ema_200', 0):.1f}`\n"
                    msg += f"그리드: `{d.get('grid_orders', 0)}` | Diff: `{d.get('diff_pct', 0):.1f}%`\n"

                elif d_eng == 'DUALTHRUST':
                    msg += f"트리거: `L:{d.get('long_trigger', 0):.1f} / S:{d.get('short_trigger', 0):.1f}`\n"

                elif d_eng == 'DUALMODE':
                    mode_raw = str(d.get('dm_mode', 'N/A')).upper()
                    mode_name = {'STANDARD': '스탠다드', 'SCALPING': '스캘핑'}.get(mode_raw, mode_raw)
                    msg += f"듀얼모드: `{mode_name}` | TF: `{d.get('dm_tf')}`\n"

                msg += "\n"

            return msg.rstrip()
        except Exception as e:
            logger.error(f"Dashboard format error: {e}")
            return "❌ 대시보드 메시지 생성 오류"

    def _format_dashboard_message(self, all_data, blink, pause_indicator):
        """텔레그램 대시보드 메시지 생성."""
        live_text, _, _ = self._build_dashboard_messages(all_data, blink, pause_indicator)
        return live_text

    async def emergency_stop(self):
        """湲닿툒 ?뺤? - 紐⑤뱺 ?ㅽ뵂 ?ъ???泥?궛"""
        logger.warning("Emergency stop triggered")

        engine = self.active_engine or self.engines.get(CORE_ENGINE)
        if self.active_engine:
            self.active_engine.stop()

        self.is_paused = True

        try:
            if self.is_upbit_mode():
                upbit_engine = engine or self.engines.get(CORE_ENGINE)
                open_symbols = set()
                if upbit_engine:
                    open_symbols = await upbit_engine.get_active_position_symbols(use_cache=False)

                if not open_symbols:
                    cancelled_orders = 0
                    for sym in self.get_active_watchlist():
                        try:
                            open_orders = await asyncio.to_thread(self.exchange.fetch_open_orders, sym)
                        except Exception as order_fetch_error:
                            logger.warning(f"Open order fetch error for {sym}: {order_fetch_error}")
                            continue

                        for order in open_orders or []:
                            order_id = order.get('id')
                            if not order_id:
                                continue
                            try:
                                await asyncio.to_thread(self.exchange.cancel_order, order_id, sym)
                                cancelled_orders += 1
                            except Exception as cancel_error:
                                logger.warning(f"Cancel order error for {sym} / {order_id}: {cancel_error}")

                    if cancelled_orders:
                        await self.notify(f"ℹ️ 업비트 보유 코인은 없어서 미체결 주문 `{cancelled_orders}`건만 취소했습니다.")
                    else:
                        await self.notify("ℹ️ 청산할 업비트 보유 코인이 없습니다.")
                    return

                symbols_text = ", ".join(self.format_symbol_for_display(sym) for sym in sorted(open_symbols))
                await self.notify(
                    f"🚨 **긴급 정지 실행**\n업비트 보유 코인 `{len(open_symbols)}`개를 즉시 매도합니다.\n대상: `{symbols_text}`"
                )

                for sym in sorted(open_symbols):
                    try:
                        await upbit_engine.exit_position(sym, "EmergencyStop")
                    except Exception as e:
                        logger.error(f"Failed to close Upbit position {sym}: {e}")
                        await self.notify(f"❌ {self.format_symbol_for_display(sym)} 청산 실패: {e}")

                await self.notify("🧯 긴급 정지 처리 완료")
                return

            # 1. ?ㅽ뵂??紐⑤뱺 ?ъ???議고쉶
            positions = await asyncio.to_thread(self.exchange.fetch_positions)
            open_positions = []
            for p in positions:
                if float(p.get('contracts', 0)) != 0:
                    open_positions.append(p)
            
            if not open_positions:
                # ?ъ??섏씠 ?녿떎硫? ?뱀떆 紐⑤Ⅴ???꾩옱 ?ㅼ젙???щ낵??誘몄껜寃?二쇰Ц留?痍⑥냼 ?쒕룄
                sym = self._get_current_symbol()
                try:
                    await asyncio.to_thread(self.exchange.cancel_all_orders, sym)
                    logger.info(f"??All orders cancelled for {sym}")
                except: pass
                await self.notify("ℹ️ 청산할 오픈 포지션이 없습니다. (미체결 주문만 취소)")
                return

            await self.notify(f"🚨 **긴급 정지 실행**\n발견된 포지션 {len(open_positions)}개를 즉시 청산합니다.")

            # 2. 紐⑤뱺 ?ㅽ뵂 ?ъ????쒖감 泥?궛
            for pos in open_positions:
                sym = pos['symbol'].replace(':USDT', '') # ?щ낵 ?щ㎎??
                
                # 二쇰Ц 痍⑥냼
                try:
                    await asyncio.to_thread(self.exchange.cancel_all_orders, sym)
                except Exception as e:
                    logger.error(f"Cancel orders error for {sym}: {e}")
                
                # 泥?궛 二쇰Ц
                try:
                    side = 'sell' if pos['side'] == 'long' else 'buy'
                    qty = abs(float(pos['contracts']))
                    pnl = float(pos.get('unrealizedPnl', 0))
                    
                    # ?섎웾 ?뺣????곸슜 ?깆쓣 ?꾪빐 engine.exit_position???곕㈃ 醫뗪쿋吏留?
                    # 湲닿툒 ?뺤??대?濡??⑥닚?섍쾶 market order濡??좊┝ (?먮뒗 reduceOnly)
                    # ?섏?留??섎웾 ?뺣???臾몄젣 ?앷만 ???덉쑝誘濡?exchange.create_order ?ъ슜 ??二쇱쓽
                    # ?ш린?쒕뒗 媛꾨떒???ㅽ뻾?섎릺, ?ㅻ쪟 ??濡쒓렇 ?④?
                    
                    order = await asyncio.to_thread(
                        self.exchange.create_order, sym, 'market', side, qty, None, {'reduceOnly': True}
                    )
                    logger.info(f"??Emergency Close: {sym} {side} {qty}")
                    await self.notify(f"✅ **{sym}** 청산 완료\nPnL: ${pnl:+.2f}")
                    
                except Exception as e:
                    logger.error(f"Failed to close {sym}: {e}")
                    await self.notify(f"❌ {sym} 청산 실패: {e}")
            
            await self.notify("🧯 긴급 정지 처리 완료")
                    
        except Exception as e:
            logger.error(f"Emergency stop error: {e}")
            await self.notify(f"❌ 긴급 정지 중 오류: {e}")

    async def notify(self, text):
        """?뚮┝ ?꾩넚"""
        try:
            cid = self.cfg.get_chat_id()
            if not cid or not self.tg_app:
                return

            try:
                await self.tg_app.bot.send_message(
                    chat_id=cid,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN
                )
            except BadRequest as md_err:
                # Dynamic symbols/text can break Markdown entities; retry as plain text.
                if "can't parse entities" in str(md_err).lower():
                    await self.tg_app.bot.send_message(chat_id=cid, text=text)
                else:
                    raise
        except Exception as e:
            logger.error(f"Notify error: {e}")


if __name__ == "__main__":
    controller = None

    def _handle_exit_signal(signum, frame):
        signame = signal.Signals(signum).name
        if controller:
            controller.record_exit_marker(f"signal_{signame.lower()}", f"received {signame}")
        raise SystemExit(128 + signum)

    try:
        controller = MainController()
        atexit.register(lambda: controller and controller.record_exit_marker("atexit", "process exiting"))
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, _handle_exit_signal)
        asyncio.run(controller.run())
    except KeyboardInterrupt:
        if controller:
            controller.record_exit_marker("keyboard_interrupt", "KeyboardInterrupt")
        print("\n?몝 Bye")
    except Exception as e:
        if controller:
            controller.record_exit_marker("fatal_exception", repr(e))
        logger.error(f"Fatal error: {e}")
        traceback.print_exc()
    finally:
        if controller:
            controller.record_exit_marker("process_finally", "finally block reached")
        # DB ?곌껐 醫낅즺
        if controller and hasattr(controller, 'db'):
            try:
                controller.db.conn.close()
                logger.info("??Database connection closed")
            except Exception:
                pass
        if sys.stdin and sys.stdin.isatty():
            try:
                input("Press Enter to Exit...")
            except EOFError:
                pass
