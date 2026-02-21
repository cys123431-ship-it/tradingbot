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
import ccxt
import pandas as pd
import pandas_ta as ta
import numpy as np
from pykalman import KalmanFilter as PyKalmanFilter
try:
    from hurst import compute_Hc
    HURST_AVAILABLE = True
except ImportError:
    HURST_AVAILABLE = False
    logging.warning("?좑툘 hurst ?⑦궎吏 ?놁쓬. FractalFisher ?꾨왂 ?ъ슜 遺덇?. ?ㅼ튂: pip install hurst")
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
CORE_ENGINE = 'signal'
CORE_STRATEGIES = {'sma', 'hma'}

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
                    'max_risk_per_trade_pct': 20.0,
                    'target_roe_pct': 20.0,
                    'stop_loss_pct': 10.0,
                    'daily_loss_limit': 5000.0,
                    'daily_loss_limit_pct': 5.0,
                    'tp_sl_enabled': True,
                    'scanner_enabled': False,
                    'scanner_timeframe': '15m', # [New] Dedicated Scanner TF
                    'scanner_exit_timeframe': '1h', # [New] Dedicated Scanner Exit TF
                    'scanner_min_rise_pct': 0.5,
                    'scanner_max_rise_pct': 8.0,
                    'r2_entry_enabled': True,
                    'r2_exit_enabled': True,
                    'r2_threshold': 0.25,
                    'hurst_entry_enabled': True,
                    'hurst_exit_enabled': True,
                    'hurst_threshold': 0.55,
                    'chop_entry_enabled': True,
                    'chop_exit_enabled': True,
                    'chop_threshold': 50.0,
                    'cc_exit_enabled': False,
                    'cc_threshold': 0.70,
                    'cc_length': 14
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
        system_cfg = self.config.setdefault('system_settings', {})
        if system_cfg.get('active_engine') != CORE_ENGINE:
            system_cfg['active_engine'] = CORE_ENGINE
            changed = True

        signal_cfg = self.config.setdefault('signal_engine', {})
        strategy_params = signal_cfg.setdefault('strategy_params', {})
        active_strategy = str(strategy_params.get('active_strategy', 'sma')).lower()
        if active_strategy not in CORE_STRATEGIES:
            strategy_params['active_strategy'] = 'sma'
            changed = True

        entry_mode = str(strategy_params.get('entry_mode', 'cross')).lower()
        if entry_mode not in {'cross', 'position'}:
            strategy_params['entry_mode'] = 'cross'
            changed = True

        common_cfg = signal_cfg.setdefault('common_settings', {})
        max_risk_pct = float(common_cfg.get('max_risk_per_trade_pct', 20.0) or 20.0)
        if max_risk_pct < 1.0:
            common_cfg['max_risk_per_trade_pct'] = 1.0
            max_risk_pct = 1.0
            changed = True

        risk_pct = float(common_cfg.get('risk_per_trade_pct', 10.0) or 10.0)
        if risk_pct > max_risk_pct:
            common_cfg['risk_per_trade_pct'] = max_risk_pct
            changed = True
        elif risk_pct < 1.0:
            common_cfg['risk_per_trade_pct'] = 1.0
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

        if changed:
            self.save_config_sync()

    def create_default_config(self):
        self.config = {
            "api": {
                "use_testnet": True,
                "mainnet": {"api_key": "", "secret_key": ""},
                "testnet": {"api_key": "", "secret_key": ""}
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
                    "max_risk_per_trade_pct": 20.0,
                    "target_roe_pct": 20.0, "stop_loss_pct": 10.0,
                    "daily_loss_limit": 5000.0,
                    "daily_loss_limit_pct": 5.0,
                    "tp_sl_enabled": True,
                    "scanner_enabled": False,
                    "scanner_timeframe": "15m",
                    "scanner_exit_timeframe": "1h",
                    "scanner_min_rise_pct": 0.5,
                    "scanner_max_rise_pct": 8.0,
                    "r2_entry_enabled": True,
                    "r2_exit_enabled": True,
                    "hurst_entry_enabled": True,
                    "hurst_exit_enabled": True,
                    "chop_entry_enabled": True,
                    "chop_exit_enabled": True
                },
                "strategy_params": {
                    "active_strategy": "sma",
                    "entry_mode": "cross",
                    "Triple_SMA": {"fast_sma": 2, "slow_sma": 10},
                    "kalman_filter": {"entry_enabled": False, "exit_enabled": False, "observation_covariance": 0.1, "transition_covariance": 0.05}
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

# ---------------------------------------------------------
# 2. ?붿쭊 (Signal / Shannon)
# ---------------------------------------------------------
class BaseEngine:
    def __init__(self, controller):
        self.ctrl = controller
        self.cfg = controller.cfg
        self.db = controller.db
        self.exchange = controller.exchange
        self.running = False
        self.position_cache = None
        self.position_cache_time = 0
        self.POSITION_CACHE_TTL = 2.0  # 2珥?罹먯떆

    def start(self):
        self.running = True
        logger.info(f"?? {self.__class__.__name__} started")

    def stop(self):
        self.running = False
        logger.info(f"??{self.__class__.__name__} stopped")

    async def on_tick(self, data_type, data):
        pass

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
                    leverage = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('leverage', 20)
            
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

    async def get_balance_info(self):
        try:
            bal = await asyncio.to_thread(self.exchange.fetch_balance)
            info = bal.get('info', {})
            total = float(info.get('totalMarginBalance', 0))
            if total == 0:
                total = float(bal.get('USDT', {}).get('total', 0))
            mmr = (float(info.get('totalMaintMargin', 0)) / total * 100) if total > 0 else 0.0
            return total, float(bal.get('USDT', {}).get('free', 0)), mmr
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

        for row in status_rows:
            unrealized_pnl += float(row.get('pnl_usdt', 0) or 0)
            pos_side = str(row.get('pos_side', 'NONE')).upper()
            symbol = row.get('symbol')
            if symbol and pos_side != 'NONE':
                open_symbols.append(symbol)

        total_daily_pnl = daily_pnl + unrealized_pnl
        total_equity = 0.0
        if status_rows:
            total_equity = float(status_rows[0].get('total_equity', 0) or 0)
        if total_equity <= 0:
            total_equity, _, _ = await self.get_balance_info()
        
        if eng == 'shannon':
            sh_cfg = self.cfg.get('shannon_engine', {})
            limit_abs = float(sh_cfg.get('daily_loss_limit', 5000) or 5000)
            limit_pct = float(sh_cfg.get('daily_loss_limit_pct', 0) or 0)
        else:
            sig_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
            limit_abs = float(sig_cfg.get('daily_loss_limit', 5000) or 5000)
            limit_pct = float(sig_cfg.get('daily_loss_limit_pct', 0) or 0)

        limits = []
        if limit_abs > 0:
            limits.append(limit_abs)
        if limit_pct > 0 and total_equity > 0:
            limits.append(total_equity * (limit_pct / 100.0))
        effective_limit = min(limits) if limits else 5000.0

        if total_daily_pnl < -effective_limit:
            logger.warning(
                f"?좑툘 Daily loss limit reached: {total_daily_pnl:.2f} "
                f"(realized: {daily_pnl:.2f}, unrealized: {unrealized_pnl:.2f}) / "
                f"Limit: -{effective_limit:.2f} (abs={limit_abs:.2f}, pct={limit_pct:.2f}%)"
            )
            # ?ъ??섏씠 ?덉쑝硫?泥?궛
            if open_symbols and hasattr(self, 'exit_position'):
                await self.ctrl.notify("?썞 ?쇱씪 ?먯떎 ?쒕룄 ?꾨떖! ?ъ???泥?궛 ?쒖옉...")
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
                await self.ctrl.notify(f"?좑툘 **MMR 寃쎄퀬!** ?꾩옱 {mmr:.2f}% (?쒕룄: {max_mmr}%)")
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
                            await self.ctrl.notify(f"?슟 **吏꾩엯 李⑤떒**: ?⑥씪 ?ъ????쒗븳 (蹂댁쑀以? {p['symbol']})")
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
            
            await self.ctrl.notify(f"?? **TEMA 吏꾩엯**: {symbol} {side.upper()}\n媛寃? {price}\n?섎웾: {amount_str}")
            
            # 4. TP/SL ?ㅼ젙 (怨듯넻 ?ㅼ젙 ?ъ슜)
            tp_sl_enabled = common_cfg.get('tp_sl_enabled', False)
            if tp_sl_enabled:
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
                    # 1. Take Profit
                    params_tp = {
                        'stopPrice': self.safe_price(symbol, tp_price),
                        'reduceOnly': True
                    }
                    if side == 'long':
                        await asyncio.to_thread(self.exchange.create_order, symbol, 'TAKE_PROFIT_MARKET', 'sell', amount_str, None, params_tp)
                    else:
                        await asyncio.to_thread(self.exchange.create_order, symbol, 'TAKE_PROFIT_MARKET', 'buy', amount_str, None, params_tp)
                    
                    # 2. Stop Loss
                    params_sl = {
                        'stopPrice': self.safe_price(symbol, sl_price),
                        'reduceOnly': True
                    }
                    if side == 'long':
                        await asyncio.to_thread(self.exchange.create_order, symbol, 'STOP_MARKET', 'sell', amount_str, None, params_sl)
                    else:
                        await asyncio.to_thread(self.exchange.create_order, symbol, 'STOP_MARKET', 'buy', amount_str, None, params_sl)
                    
                    logger.info(f"??TP/SL Order Placed: TP={tp_price:.4f}, SL={sl_price:.4f}")
                except Exception as e:
                    logger.error(f"Failed to place TP/SL order: {e}")
                    await self.ctrl.notify(f"?좑툘 TP/SL 二쇰Ц ?ㅽ뙣: {e}")

        except Exception as e:
            logger.error(f"TEMA entry failed: {e}")
            await self.ctrl.notify(f"??吏꾩엯 ?ㅽ뙣: {e}")

    async def exit_position(self, symbol, reason):
        try:
            pos = await self.get_server_position(symbol, use_cache=False)
            if not pos: return

            amount = abs(float(pos.get('contracts', 0) or 0))
            pos_side = str(pos.get('side', '')).lower()
            side = 'sell' if pos_side == 'long' else 'buy'
            
            if amount > 0:
                await asyncio.to_thread(self.exchange.create_market_order, symbol, side, amount)
                await self.ctrl.notify(f"?몝 **TEMA 泥?궛**: {symbol} ({reason})")
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
        self.last_processed_exit_candle_ts = {}
        self.pending_reentry = {} # {symbol: {'side': 'long'|'short', 'target_time': ts}}
        
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
        
        # [New] Filter Status Persistence (Dashboard)
        self.last_entry_filter_status = {} # symbol -> {r2_val, ...}
        self.last_exit_filter_status = {}  # symbol -> {r2_val, ...}

    def start(self):
        super().start()
        self.last_activity = time.time()
        # [Fix] ?ш컻(RESUME) ???곹깭 珥덇린?뷀븯??利됱떆 ?ъ쭊??媛?ν븯?꾨줉 ?섏젙
        self.last_candle_time = {}
        self.last_candle_success = {}
        self.last_processed_candle_ts = {}
        self.last_processed_exit_candle_ts = {}
        
        # 珥덇린??
        config_watchlist = self.cfg.get('signal_engine', {}).get('watchlist', [])
        for s in config_watchlist:
            self.active_symbols.add(s)
        logger.info(f"?? [Signal] Engine started (Multi-Symbol Mode). Watching: {self.active_symbols}")

    def _get_exit_timeframe(self, symbol=None):
        """泥?궛????꾪봽?덉엫 (User Defined)
           醫낅ぉ???ㅼ틦?덉뿉 ?섑빐 ?≫엺 寃쎌슦 ?꾩슜 ??꾪봽?덉엫 諛섑솚
        """
        cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
        if symbol and symbol == self.scanner_active_symbol:
            return cfg.get('scanner_exit_timeframe', '1h')
        return cfg.get('exit_timeframe', '4h')

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
            cfg = self.cfg.get('signal_engine', {})
            common_cfg = cfg.get('common_settings', {})
            entry_tf = common_cfg.get('entry_timeframe', common_cfg.get('timeframe', '8h'))
            
            # Common: Fetch Current Positions (Always monitor existing positions)
            positions = await asyncio.to_thread(self.exchange.fetch_positions)
            active_position_symbols = set()
            for p in positions:
                if float(p.get('contracts', 0)) > 0:
                    sym = p['symbol'].replace(':USDT', '')
                    active_position_symbols.add(sym)

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
                config_symbols = set(self.cfg.get('signal_engine', {}).get('watchlist', []))
                
                # Merge: Config + Chat Manual + Positions
                target_symbols = self.active_symbols | config_symbols | active_position_symbols
 
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
                        'margin_mode': 'ISOLATED',
                        'entry_tf': entry_tf,
                        'exit_tf': common_cfg.get('exit_timeframe', '4h')
                    }
                return

            # If targets exist, remove SCANNER placeholder to avoid duplicate display
            if 'SCANNER' in self.ctrl.status_data:
                del self.ctrl.status_data['SCANNER']

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
            
        try:
            # 1. OHLCV (Primary) - Basic monitoring
            ohlcv_p = await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, primary_tf, limit=5)
            if not ohlcv_p or len(ohlcv_p) < 3:
                return
            
            current_price = float(ohlcv_p[-1][4])
        
            # [Fix] Update Status and get local pos_side to avoid race condition
            pos_side = await self.check_status(symbol, current_price)
            
            # 2. Check Primary TF (Entry Logic)
            last_closed_p = ohlcv_p[-2]
            ts_p = last_closed_p[0]
            
            # ?щ낵蹂??곹깭 珥덇린??
            if symbol not in self.last_processed_candle_ts:
                self.last_processed_candle_ts[symbol] = 0
            
            if ts_p > self.last_processed_candle_ts[symbol]:
                logger.info(f"?빉截?[Primary {primary_tf}] {symbol} New Candle: {ts_p} close={last_closed_p[4]}")
                self.last_processed_candle_ts[symbol] = ts_p
                
                k_p = {
                    't': ts_p, 'o': str(last_closed_p[1]), 'h': str(last_closed_p[2]),
                    'l': str(last_closed_p[3]), 'c': str(last_closed_p[4]), 'v': str(last_closed_p[5])
                }
                await self.process_primary_candle(symbol, k_p)
                
            # 3. Check Exit TF (Exit Logic)
            strategy_params = cfg.get('strategy_params', {})
            entry_mode = strategy_params.get('entry_mode', 'cross').lower()
            active_strategy = strategy_params.get('active_strategy', 'sma').lower()
            
            # Cross/Position 紐⑤뱶?먯꽌留?Secondary TF 泥?궛 濡쒖쭅 ?ъ슜
            if (pos_side != 'NONE') and (active_strategy in ['sma', 'hma']) and (entry_mode in ['cross', 'position']):
                exit_tf = self._get_exit_timeframe(symbol)
                
                ohlcv_e = await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, exit_tf, limit=5)
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
                            
                        self.last_processed_exit_candle_ts[symbol] = ts_e
                        await self.process_exit_candle(symbol, exit_tf, pos_side)

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
            logger.info("?뱻 Scanning high volume markets (>200M USDT)...")
            tickers = await asyncio.to_thread(self.exchange.fetch_tickers)
            
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
                    cfg = self.cfg.get('signal_engine', {})
                    common_cfg = cfg.get('common_settings', {})
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
                    if active_strategy not in ['sma', 'hma']:
                        active_strategy = 'sma'
                    
                    # Use scanner_timeframe if set, but ensure we are thinking about consistency
                    ohlcv = await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, scan_tf, limit=300)
                    if not ohlcv: continue
                    
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    
                    sig, _, _, _, _, _ = await self._calculate_strategy_signal(symbol, df, scan_params, active_strategy)
                    
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
            strategy_params = self.cfg.get('signal_engine', {}).get('strategy_params', {})
            kalman_enabled = strategy_params.get('kalman_filter', {}).get('enabled', False)
            active_strategy = strategy_params.get('active_strategy', 'sma').upper()
            entry_mode = strategy_params.get('entry_mode', 'cross').upper()
            
            # Cross/Position 紐⑤뱶?먯꽌 Kalman ?꾪꽣媛 濡쒖쭅??媛뺤젣 ?ъ슜?섎?濡??곹깭 ?쒖떆???쒖꽦??(SMA/HMA)
            if active_strategy in ['SMA', 'HMA'] and entry_mode in ['CROSS', 'POSITION']:
                kalman_enabled = True
            
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
                'pos_side': pos_side,
                'entry_price': float(pos['entryPrice']) if pos else 0.0,
                'coin_amt': float(pos['contracts']) if pos else 0.0,
                'pnl_pct': float(pos['percentage']) if pos else 0.0,
                'pnl_usdt': float(pos['unrealizedPnl']) if pos else 0.0,
                # Signal ?꾩슜 ?꾨뱶
                'kalman_enabled': kalman_enabled,
                'kalman_velocity': self.kalman_states.get(symbol, {}).get('velocity', 0.0),
                'kalman_direction': self.kalman_states.get(symbol, {}).get('direction'),
                'active_strategy': active_strategy,
                'entry_mode': entry_mode,
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
            comm_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
            symbol_status['filter_config'] = {
                'r2': {
                    'en_entry': comm_cfg.get('r2_entry_enabled', True), 
                    'en_exit': comm_cfg.get('r2_exit_enabled', True),
                    'th': comm_cfg.get('r2_threshold', 0.25)
                },
                'hurst': {
                    'en_entry': comm_cfg.get('hurst_entry_enabled', True), 
                    'en_exit': comm_cfg.get('hurst_exit_enabled', True),
                    'th': comm_cfg.get('hurst_threshold', 0.55)
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
            
            # [New] Status Display Enhancement
            symbol_status['leverage'] = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('leverage', 20)
            symbol_status['margin_mode'] = 'ISOLATED' # Enforced
            symbol_status['entry_tf'] = comm_cfg.get('entry_timeframe', comm_cfg.get('timeframe', '8h'))
            symbol_status['exit_tf'] = comm_cfg.get('exit_timeframe', '4h')

            self.ctrl.status_data[symbol] = symbol_status
            
            # MMR 寃쎄퀬 泥댄겕
            await self.check_mmr_alert(mmr)
            
            return pos_side
            
            if self.ctrl.is_paused or not pos:
                return
            
            cfg = comm_cfg # Alias for below use
            
            # ===== MicroVBO: 嫄곕옒??二쇰Ц TP/SL ?ъ슜 =====
            # 嫄곕옒?뚯뿉??吏곸젒 TP/SL 泥닿껐??(?ㅽ뵂?ㅻ뜑???쒖떆??
            if active_strategy == 'MICROVBO':
                if vbo_state.get('entry_price') and vbo_state.get('entry_atr') and pos:
                    pnl = float(pos['percentage'])
                    logger.debug(f"[MicroVBO] Position PnL: {pnl:+.2f}% - TP/SL via exchange orders")
                return  # MicroVBO??嫄곕옒??二쇰Ц TP/SL ?ъ슜
            
            # ===== FractalFisher ?꾩슜 ATR Trailing Stop =====
            if active_strategy == 'FRACTALFISHER':
                entry_price = fisher_state.get('entry_price')
                entry_atr = fisher_state.get('entry_atr')
                trailing_stop = fisher_state.get('trailing_stop')
                
                if entry_price and entry_atr:
                    ff_cfg = strategy_params.get('FractalFisher', {})
                    trailing_mult = ff_cfg.get('atr_trailing_multiplier', 2.0)
                    
                    if pos['side'] == 'long':
                        # Trailing Stop 怨꾩궛 (??긽 ?щ━湲곕쭔)
                        new_stop = price - (entry_atr * trailing_mult)
                        if trailing_stop is None:
                            trailing_stop = new_stop
                        else:
                            trailing_stop = max(trailing_stop, new_stop)
                        
                        # ?곹깭 ?낅뜲?댄듃
                        if symbol not in self.fisher_states: self.fisher_states[symbol] = {}
                        self.fisher_states[symbol]['trailing_stop'] = trailing_stop
                        
                        if price <= trailing_stop:
                            logger.info(f"?썞 [FractalFisher] Trailing Stop Hit: {price:.2f} <= {trailing_stop:.2f}")
                            await self.exit_position(symbol, "Fisher_TrailingStop")
                            self.fisher_states[symbol] = {} # state reset
                    
                    elif pos['side'] == 'short':
                        # Trailing Stop 怨꾩궛 (??긽 ?대━湲곕쭔)
                        new_stop = price + (entry_atr * trailing_mult)
                        if trailing_stop is None:
                            trailing_stop = new_stop
                        else:
                            trailing_stop = min(trailing_stop, new_stop)
                        
                        # ?곹깭 ?낅뜲?댄듃
                        if symbol not in self.fisher_states: self.fisher_states[symbol] = {}
                        self.fisher_states[symbol]['trailing_stop'] = trailing_stop
                        
                        if price >= trailing_stop:
                            logger.info(f"?썞 [FractalFisher] Trailing Stop Hit: {price:.2f} >= {trailing_stop:.2f}")
                            await self.exit_position(symbol, "Fisher_TrailingStop")
                            self.fisher_states[symbol] = {} # state reset
                    
                return  # FractalFisher??Trailing Stop留??ъ슜
            
            # ===== SMA/HMA/MicroVBO: 嫄곕옒??二쇰Ц TP/SL ?ъ슜 =====
            # 嫄곕옒?뚯뿉??吏곸젒 TP/SL 泥닿껐??(?ㅽ뵂?ㅻ뜑???쒖떆??
            # ?뚰봽?몄썾??紐⑤땲?곕쭅 遺덊븘??
            # (諛깆뾽??濡쒓렇留??④?)
            if pos:
                pnl = float(pos['percentage'])
                logger.debug(f"Position PnL monitoring: {pnl:+.2f}% - TP/SL via exchange orders")
                
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

    async def process_primary_candle(self, symbol, k):
        candle_time = k['t']
        
        # ?щ낵蹂??곹깭 珥덇린??
        if symbol not in self.last_candle_time:
            self.last_candle_time[symbol] = 0
            self.last_candle_success[symbol] = True

        # 以묐났 罹붾뱾 諛⑹? (媛숈? 罹붾뱾 ?ъ쿂由?李⑤떒) - ?깃났??寃쎌슦?먮쭔 ?ㅽ궢
        if candle_time <= self.last_candle_time[symbol] and self.last_candle_success[symbol]:
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
            common_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
            tf = common_cfg.get('entry_timeframe', common_cfg.get('timeframe', '15m'))
            ohlcv = await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, tf, limit=300)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            # [CRITICAL] Ensure numeric types (Robust Loop)
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            
            # ===== ?꾨왂 ?ㅼ젙 濡쒕뱶 =====
            strategy_params = self.cfg.get('signal_engine', {}).get('strategy_params', {})
            active_strategy = strategy_params.get('active_strategy', 'sma').lower()
            
            # ?꾨왂蹂??좏샇 怨꾩궛
            sig, is_bullish, is_bearish, strategy_name, entry_mode, kalman_enabled = await self._calculate_strategy_signal(symbol, df, strategy_params, active_strategy)
            
            # 留ㅻℓ 諛⑺뼢 ?꾪꽣
            d_mode = self.cfg.get('system_settings', {}).get('trade_direction', 'both')
            if sig and ((d_mode == 'long' and sig == 'short') or (d_mode == 'short' and sig == 'long')):
                logger.info(f"??Signal {sig} blocked by direction filter: {d_mode}")
                sig = None

            # ?ъ????뺤씤
            self.position_cache = None
            self.position_cache_time = 0
            pos = await self.get_server_position(symbol, use_cache=False)
            
            current_side = pos['side'] if pos else 'NONE'
            logger.info(f"?뱧 Current position: {current_side}, Signal: {sig or 'NONE'}, Mode: {entry_mode}")
            
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
            if entry_mode in ['cross', 'position']:
                # Cross/Position 紐⑤뱶: Primary TF?먯꽌??"吏꾩엯(Entry)"留?泥섎━
                # 泥?궛(Exit)? Secondary TF candle (process_exit_candle)?먯꽌 泥섎━??
                
                if pos:
                    # ?대? ?ъ??섏씠 ?덉쑝硫?Entry 泥댄겕 ????(Wait for Exit TF signal)
                    # ?? Pending Re-entry???꾩뿉??泥섎━??
                    logger.debug(f"ProcessPrimary: Position exists ({pos['side']}), waiting for Exit TF signal.")
                    
                elif not pos and sig:
                    # ?ъ????놁쓬 + 吏꾩엯 ?좏샇 -> 吏꾩엯
                    strategy_label = "Cross" if entry_mode == 'cross' else "Position"
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
           Applies EXIT filters (Kalman, R2, Hurst, Chop) independently.
        """
        try:
            # Fetch history for Exit TF
            ohlcv = await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, tf, limit=300)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            # [CRITICAL] Ensure numeric types (Robust Loop)
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            
            # ===== 1. Calculate Raw Signal (SMA/HMA Cross) on Exit TF =====
            strategy_params = self.cfg.get('signal_engine', {}).get('strategy_params', {})
            active_strategy = strategy_params.get('active_strategy', 'sma').lower()
            
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
            
            # Raw Exit Signals (Opposite to position)
            raw_exit_long = False
            raw_exit_short = False
            
            # Check Cross (Standard Exit Trigger)
            # If Long -> Cross Down is exit signal
            if p_f > p_s and c_f < c_s:
                raw_exit_long = True
                logger.info(f"?뱣 [Exit Debug] {symbol} Dead Cross: {p_f:.2f}/{p_s:.2f} -> {c_f:.2f}/{c_s:.2f}")
            
            # If Short -> Cross Up is exit signal
            if p_f < p_s and c_f > c_s:
                raw_exit_short = True
                logger.info(f"?뱢 [Exit Debug] {symbol} Golden Cross: {p_f:.2f}/{p_s:.2f} -> {c_f:.2f}/{c_s:.2f}")
                
            # Or Position Check? 
            # Usually 'Cross' strategy uses Cross for exit. 
            # 'Position' strategy uses Position (Alignment) for entry, but for exit? 
            # If MA Alignment flips against position, treat as raw exit signal.
            # Let's support both: Cross OR Alignment Flip
            
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
                return

            # ?좏샇媛 ?덉쓣 ?뚮룄 ?낅뜲?댄듃 諛?泥?궛 濡쒖쭅 吏꾪뻾
            await self._update_exit_filter_values(symbol, df, current_side)
            
            # ===== 3. Exit Filter logic check =====
            can_exit = True
            block_reasons = []
            
            # Re-fetch the calculated values for checking
            st = self.last_exit_filter_status.get(symbol, {})
            kalman_vel = st.get('kalman_vel', 0.0)
            curr_r2 = st.get('r2_val', 0.0)
            curr_hurst = st.get('hurst_val', 0.0)
            curr_chop = st.get('chop_val', 50.0)
            
            kalman_cfg = strategy_params.get('kalman_filter', {})
            kalman_exit_enabled = kalman_cfg.get('exit_enabled', False)
            
            common_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
            r2_exit_enabled = common_cfg.get('r2_exit_enabled', True)
            r2_thresh = common_cfg.get('r2_threshold', 0.25)
            hurst_exit_enabled = common_cfg.get('hurst_exit_enabled', True)
            hurst_thresh = common_cfg.get('hurst_threshold', 0.55)
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
            
            # 3. Hurst Check
            if hurst_exit_enabled:
                if curr_hurst < hurst_thresh:
                    can_exit = False
                    block_reasons.append(f"Hurst({curr_hurst:.2f})<{hurst_thresh}")

            # 4. CHOP Check
            can_exit_by_chop = not chop_exit_enabled or (curr_chop <= chop_thresh)
            if chop_exit_enabled and not can_exit_by_chop:
                block_reasons.append(f"Chop({curr_chop:.1f})>{chop_thresh}")
            
            # 5. CC Check (Correlation)
            cc_exit_enabled = common_cfg.get('cc_exit_enabled', False)
            cc_thresh = common_cfg.get('cc_threshold', 0.70)
            curr_cc = st.get('cc_val', 0.0)
            can_exit_by_cc = False
            if cc_exit_enabled:
                if current_side.lower() == 'long' and curr_cc < -cc_thresh:
                    can_exit_by_cc = True
                elif current_side.lower() == 'short' and curr_cc > cc_thresh:
                    can_exit_by_cc = True
            
            # [New] Update Pass/Fail Status based on logic above
            st['kalman_pass'] = (not kalman_exit_enabled) or \
                              (current_side.lower() == 'long' and kalman_vel < 0) or \
                              (current_side.lower() == 'short' and kalman_vel > 0)
            st['r2_pass'] = (not r2_exit_enabled) or (curr_r2 >= r2_thresh)
            st['hurst_pass'] = (not hurst_exit_enabled) or (curr_hurst >= hurst_thresh)
            st['chop_pass'] = (not chop_exit_enabled) or (curr_chop <= chop_thresh)
            
            
            # ===== 5. Execute Exit =====
            # Use specific logic for Position Strategy as requested by user
            # Rule: Long Exit -> Bearish Alignment AND Chop Green
            #       Short Exit -> Bullish Alignment AND Chop Green

            # CC is an ALTERNATIVE to Chop (OR logic)
            # Re-confirm CC pass status for specific side
            cc_pass = can_exit_by_cc

            if current_side.lower() == 'long':
                if c_f < c_s: # Bearish Alignment (Signal)
                    if can_exit and (can_exit_by_chop or can_exit_by_cc):
                        reason = "Chop Green" if can_exit_by_chop else "CC Trend"
                        logger.info(f"?뵒 [Exit {tf}] LONG Exit Triggered: Bearish Alignment AND {reason} (Chop:{curr_chop:.1f}, CC:{curr_cc:.2f})")
                        await self.exit_position(symbol, f"{strategy_name}_Exit_L")
                    else:
                        why = ", ".join(block_reasons) if block_reasons else "Chop/CC blocked"
                        logger.info(f"?썳截?[Exit {tf}] LONG Exit Blocked: {why} (Chop:{curr_chop:.1f}, CC:{curr_cc:.2f})")
                else:
                    logger.debug(f"??[Exit {tf}] LONG Exit Ignored: Still Bullish Alignment (Fast {c_f:.2f} > Slow {c_s:.2f})")
            
            elif current_side.lower() == 'short':
                if c_f > c_s: # Bullish Alignment (Signal)
                    if can_exit and (can_exit_by_chop or can_exit_by_cc):
                        reason = "Chop Green" if can_exit_by_chop else "CC Trend"
                        logger.info(f"?뵒 [Exit {tf}] SHORT Exit Triggered: Bullish Alignment AND {reason} (Chop:{curr_chop:.1f}, CC:{curr_cc:.2f})")
                        await self.exit_position(symbol, f"{strategy_name}_Exit_S")
                    else:
                        why = ", ".join(block_reasons) if block_reasons else "Chop/CC blocked"
                        logger.info(f"?썳截?[Exit {tf}] SHORT Exit Blocked: {why} (Chop:{curr_chop:.1f}, CC:{curr_cc:.2f})")
                else:
                    logger.debug(f"??[Exit {tf}] SHORT Exit Ignored: Still Bearish Alignment (Fast {c_f:.2f} < Slow {c_s:.2f})")
                
        except Exception as e:
            logger.error(f"Process exit candle error: {e}")
            import traceback
            traceback.print_exc()

    async def _calculate_strategy_signal(self, symbol, df, strategy_params, active_strategy):
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
        
        # Init state dicts if needed
        if symbol not in self.vbo_states: self.vbo_states[symbol] = {}
        if symbol not in self.fisher_states: self.fisher_states[symbol] = {}
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
        common_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
        
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
        
        # 2. Hurst Exponent (Trend vs Mean Reversion)
        hurst_entry_enabled = common_cfg.get('hurst_entry_enabled', True)
        hurst_thresh = common_cfg.get('hurst_threshold', 0.55)
        curr_hurst = 0.5
        is_hurst_pass = True
        
        if len(df) >= 100:
            try:
                from hurst import compute_Hc
                # Calculate Hurst on last 100 candles (close price)
                # simplified=True is faster
                H, _, _ = compute_Hc(df['close'].values[-100:], kind='price', simplified=True)
                curr_hurst = H
                
                if hurst_entry_enabled and curr_hurst < hurst_thresh:
                    # Hurst < 0.5 (Mean Reverting), < Thresh -> Trend Weak
                    is_hurst_pass = False
            except ImportError:
                 logger.warning("Package 'hurst' not installed. Hurst filter skipped.")
            except Exception as e:
                 logger.warning(f"Hurst calculation error: {e}")
                 
        # 3. Choppiness Index (Trend vs Chop)
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
            'hurst_val': curr_hurst,
            'chop_val': curr_chop,
            'r2_pass': is_r2_pass,
            'hurst_pass': is_hurst_pass,
            'chop_pass': is_chop_pass
        }
        
        # Visual Logging
        r2_icon = "OK" if is_r2_pass else "NO"
        hurst_icon = "OK" if is_hurst_pass else "NO"
        chop_icon = "OK" if is_chop_pass else "NO"
        
        if active_strategy in ['sma', 'hma']:
             logger.info(f"?썳截?Filters (Entry): R2({curr_r2:.2f}{r2_icon}) Hurst({curr_hurst:.2f}{hurst_icon}) CHOP({curr_chop:.1f}{chop_icon})")
             # Log Kalman too if used
             
        # ... MicroVBO / FractalFisher skipped (no changes needed) ...
        
        # ===== 1. SMA/HMA 怨꾩궛 =====
        strategy_name = active_strategy.upper()
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
        
        if active_strategy in ['sma', 'hma']:
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
        if entry_mode == 'cross':
            # SMA Cross Only Logic First
            cross_up = p_f < p_s and c_f > c_s
            cross_down = p_f > p_s and c_f < c_s
            
            if cross_up:
                if kalman_entry_enabled:
                    if kalman_bullish:
                        sig = 'long'
                        logger.info(f"LONG signal: {strategy_name} Cross + Kalman Entry OK")
                    else:
                        logger.info(f"?썳截?Filtered: {strategy_name} Cross Up but Kalman Entry Filter Blocked")
                else:
                    sig = 'long'
                    logger.info(f"LONG signal: {strategy_name} Cross (Kalman Filter OFF)")
            
            elif cross_down:
                if kalman_entry_enabled:
                    if kalman_bearish:
                        sig = 'short'
                        logger.info(f"SHORT signal: {strategy_name} Cross + Kalman Entry OK")
                    else:
                        logger.info(f"?썳截?Filtered: {strategy_name} Cross Down but Kalman Entry Filter Blocked")
                else:
                    sig = 'short'
                    logger.info(f"SHORT signal: {strategy_name} Cross (Kalman Filter OFF)")
            
            else:
                logger.debug(f"?뱣 No Cross: {strategy_name}")
        
        elif entry_mode == 'position':
            # Position Mode: MA Alignment
            if ma_bullish:
                if kalman_entry_enabled:
                    if kalman_bullish:
                        sig = 'long'
                        logger.info(f"LONG signal: {strategy_name} Position + Kalman Entry OK")
                else:
                    sig = 'long'
                    logger.info(f"LONG signal: {strategy_name} Position (Kalman Filter OFF)")
            
            elif ma_bearish:
                if kalman_entry_enabled:
                    if kalman_bearish:
                        sig = 'short'
                        logger.info(f"SHORT signal: {strategy_name} Position + Kalman Entry OK")
                else:
                    sig = 'short'
                    logger.info(f"SHORT signal: {strategy_name} Position (Kalman Filter OFF)")
            
            else:
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

        is_bullish = ma_bullish
        is_bearish = ma_bearish
        
        # ============ Apply Advanced Trends Filters to Entry Signal ============
        if sig and active_strategy in ['sma', 'hma']:
            blocked_reasons = []
            if not is_r2_pass:
                blocked_reasons.append(f"R2({curr_r2:.2f})<{r2_thresh}")
            if not is_hurst_pass:
                blocked_reasons.append(f"Hurst({curr_hurst:.2f})<{hurst_thresh}")
            if not is_chop_pass:
                blocked_reasons.append(f"CHOP({curr_chop:.1f})>{chop_thresh}")
            
            if blocked_reasons:
                logger.info(f"?썳截?Entry Blocked by Filters: {', '.join(blocked_reasons)}")
                sig = None
                is_bullish = False
                is_bearish = False
            
        return sig, is_bullish, is_bearish, strategy_name, entry_mode, kalman_entry_enabled

    async def _update_exit_filter_values(self, symbol, df, current_side):
        """[Helper] Calculate exit filter values and update status without executing exit logic"""
        try:
            strategy_params = self.cfg.get('signal_engine', {}).get('strategy_params', {})
            common_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
            
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
            
            # C. Hurst Filter
            hurst_exit_enabled = common_cfg.get('hurst_exit_enabled', True)
            hurst_thresh = common_cfg.get('hurst_threshold', 0.55)
            curr_hurst = 0.5
            if len(df) >= 100 and hurst_exit_enabled:
                try:
                    from hurst import compute_Hc
                    curr_hurst, _, _ = compute_Hc(df['close'].values[-100:], kind='price', simplified=True)
                except: pass
            
            # D. Chop Filter
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
            
            # E. CC Filter (Correlation Coefficient)
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
                'hurst_val': curr_hurst,
                'chop_val': curr_chop,
                'kalman_vel': kalman_vel,
                'r2_pass': (not r2_exit_enabled) or (curr_r2 >= r2_thresh),
                'hurst_pass': (not hurst_exit_enabled) or (curr_hurst >= hurst_thresh),
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
                            await self.ctrl.notify(f"?슟 **吏꾩엯 李⑤떒**: ?⑥씪 ?ъ????쒗븳 (蹂댁쑀以? {p['symbol']})")
                            return
            except Exception as e:
                logger.error(f"Single position check failed: {e}")
                return # ?덉쟾???꾪빐 ?뺤씤 ?ㅽ뙣 ??吏꾩엯 以묐떒

            logger.info(f"?뱿 [Signal] Attempting {side.upper()} entry @ {price}")
            
            cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
            lev = int(max(1.0, float(cfg.get('leverage', 10) or 10)))
            req_risk_pct = float(cfg.get('risk_per_trade_pct', 10.0) or 10.0)
            max_risk_pct = float(cfg.get('max_risk_per_trade_pct', 20.0) or 20.0)
            bounded_risk_pct = min(max(req_risk_pct, 1.0), max(max_risk_pct, 1.0))
            risk_pct = bounded_risk_pct / 100.0
            
            bal = await asyncio.to_thread(self.exchange.fetch_balance)
            free = float(bal.get('USDT', {}).get('free', 0))
            
            if free <= 0:
                logger.warning(f"Insufficient balance: {free}")
                await self.ctrl.notify(f"?좑툘 ?붽퀬 遺議? ${free:.2f}")
                return
            
            # Risk-based sizing: cap by (a) risk budget at configured stop distance and (b) max margin notional.
            stop_loss_pct = max(float(cfg.get('stop_loss_pct', 10.0) or 10.0), 0.1)
            stop_move_pct = (stop_loss_pct / 100.0) / lev
            risk_budget = free * risk_pct
            max_notional = free * lev
            if stop_move_pct > 0:
                notional_by_risk = risk_budget / stop_move_pct
            else:
                notional_by_risk = max_notional
            target_notional = min(notional_by_risk, max_notional)

            qty = self.safe_amount(symbol, target_notional / price)
            
            if float(qty) <= 0:
                logger.warning(f"Invalid quantity: {qty} (free={free}, risk={risk_pct}, lev={lev}, price={price})")
                await self.ctrl.notify(f"?좑툘 二쇰Ц ?섎웾 怨꾩궛 ?ㅻ쪟: {qty} (?붽퀬: {free:.2f})")
                return
            
            if bounded_risk_pct != req_risk_pct:
                await self.ctrl.notify(f"?좑툘 Risk capped: {req_risk_pct:.1f}% -> {bounded_risk_pct:.1f}%")
            logger.info(
                f"Entry params: qty={qty}, lev={lev}x, risk={bounded_risk_pct:.1f}% "
                f"(risk_budget={risk_budget:.2f}, max_notional={max_notional:.2f}, target_notional={target_notional:.2f})"
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
            await self.ctrl.notify(f"?? [Signal] {side.upper()} {qty} @ {price:.2f}")
            logger.info(f"??Entry order success: {order.get('id', 'N/A')}")
            
            # ?꾨왂 ?뚮씪誘명꽣 濡쒕뱶
            strategy_params = self.cfg.get('signal_engine', {}).get('strategy_params', {})
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
                    await self.ctrl.notify(f"?뱧 Trailing Stop ?ㅼ젙: {self.fisher_trailing_stop:.2f}")
            
            else:
                # SMA/HMA: ?쇱꽱??湲곕컲 TP/SL 二쇰Ц
                tp_sl_enabled = bool(cfg.get('tp_sl_enabled', True))
                if tp_sl_enabled:
                    tp_pct = cfg.get('target_roe_pct', 20.0) / 100.0 / lev  # ROE瑜?媛寃?蹂?숇쪧濡?蹂??
                    sl_pct = cfg.get('stop_loss_pct', 10.0) / 100.0 / lev
                    
                    tp_distance = actual_entry_price * tp_pct
                    sl_distance = actual_entry_price * sl_pct
                    
                    await self._place_tp_sl_orders(symbol, side, actual_entry_price, qty,
                                                   tp_distance, sl_distance)
            
            if verify_pos:
                logger.info(f"??Position verified: {verify_pos['side']} {verify_pos['contracts']}")
            else:
                logger.warning("?좑툘 Position not found after entry (may take time to update)")
            
        except Exception as e:
            logger.error(f"Signal entry error: {e}")
            import traceback
            traceback.print_exc()
            await self.ctrl.notify(f"??吏꾩엯 ?ㅽ뙣: {e}")

    async def _place_tp_sl_orders(self, symbol, side, entry_price, qty, tp_distance, sl_distance):
        """嫄곕옒?뚯뿉 TP/SL 二쇰Ц 諛곗튂 (?ㅽ뵂?ㅻ뜑??蹂댁엫)"""
        try:
            if side == 'long':
                tp_price = self.safe_price(symbol, entry_price + tp_distance)
                sl_price = self.safe_price(symbol, entry_price - sl_distance)
                tp_side = 'sell'
                sl_side = 'sell'
            else:
                tp_price = self.safe_price(symbol, entry_price - tp_distance)
                sl_price = self.safe_price(symbol, entry_price + sl_distance)
                tp_side = 'buy'
                sl_side = 'buy'
            
            # Take Profit 二쇰Ц (吏?뺢? + reduceOnly)
            try:
                tp_order = await asyncio.to_thread(
                    self.exchange.create_order, symbol, 'limit', tp_side, qty, tp_price,
                    {'reduceOnly': True}
                )
                logger.info(f"??TP order placed: {tp_side.upper()} @ {tp_price}")
            except Exception as tp_e:
                logger.error(f"TP order failed: {tp_e}")
                tp_order = None
            
            # Stop Loss 二쇰Ц (?ㅽ깙 留덉폆 + reduceOnly)
            try:
                sl_order = await asyncio.to_thread(
                    self.exchange.create_order, symbol, 'stop_market', sl_side, qty, None,
                    {'stopPrice': sl_price, 'reduceOnly': True}
                )
                logger.info(f"??SL order placed: {sl_side.upper()} @ {sl_price} (stop)")
            except Exception as sl_e:
                logger.error(f"SL order failed: {sl_e}")
                sl_order = None
            
            if tp_order or sl_order:
                await self.ctrl.notify(f"?렞 TP: `{tp_price:.2f}` | ?썞 SL: `{sl_price:.2f}`")
            
        except Exception as e:
            logger.error(f"TP/SL order placement error: {e}")

    async def exit_position(self, symbol, reason):
        logger.info(f"?뱾 [Signal] Attempting exit: {reason}")
        
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
                    self.exchange.create_order, symbol, 'market', side, qty
                )
                break  # ?깃났 ??猷⑦봽 ?덉텧
            except Exception as e:
                last_error = e
                logger.error(f"Exit attempt {attempt + 1}/{max_retries} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)  # 1珥??湲????ъ떆??
                    await self.ctrl.notify(f"?좑툘 泥?궛 ?ъ떆??以?.. ({attempt + 2}/{max_retries})")
        
        if not order:
            logger.error(f"??Exit failed after {max_retries} attempts, trying force close...")
            await self.ctrl.notify(f"?슚 泥?궛 ?ㅽ뙣! 媛뺤젣 泥?궛 ?쒕룄 以?..")
            
            # 媛뺤젣 泥?궛: 紐⑤뱺 二쇰Ц 痍⑥냼 ??reduceOnly濡??ъ떆??
            try:
                await asyncio.to_thread(self.exchange.cancel_all_orders, symbol)
                await asyncio.sleep(0.5)
                
                # reduceOnly ?듭뀡?쇰줈 媛뺤젣 泥?궛
                order = await asyncio.to_thread(
                    self.exchange.create_order, symbol, 'market', side, qty, None,
                    {'reduceOnly': True}
                )
                await self.ctrl.notify(f"??媛뺤젣 泥?궛 ?깃났!")
            except Exception as force_e:
                logger.error(f"Force close also failed: {force_e}")
                await self.ctrl.notify(f"?슚?슚 媛뺤젣 泥?궛???ㅽ뙣! 利됱떆 ?섎룞 泥?궛 ?꾩슂: {force_e}")
                return
        
        pnl = float(pos.get('unrealizedPnl', 0))
        pnl_pct = float(pos.get('percentage', 0))
        exit_price = float(order.get('average', 0)) if order.get('average') else float(pos.get('markPrice', 0))
        
        # 罹먯떆 ?꾩쟾 臾댄슚??
        self.position_cache = None
        self.position_cache_time = 0
        
        self.db.log_trade_close(symbol, pnl, pnl_pct, exit_price, reason)
        await self.ctrl.notify(f"?㏏ [{reason}] PnL: {pnl:+.2f} ({pnl_pct:+.2f}%)")
        logger.info(f"??Exit order success: {order.get('id', 'N/A')}")


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
                        await self.ctrl.notify(f"?? [Shannon] LONG 吏꾩엯 {entry_qty} @ {price:.2f} (200 EMA ?곹뼢)")
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
                        await self.ctrl.notify(f"?? [Shannon] SHORT 吏꾩엯 {entry_qty} @ {price:.2f} (200 EMA ?섑뼢)")
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
                await self.ctrl.notify(f"?뵏 [Shannon] {pos['side'].upper()} 泥?궛 (諛⑺뼢 ?꾪꽣) PnL: {pnl:+.2f}")
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
            await self.ctrl.notify(f"?봽 [Shannon] {pos['side'].upper()} 泥?궛 (異붿꽭 諛섏쟾) PnL: {pnl:+.2f}")
            
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
                await self.ctrl.notify(f"?? [Shannon] {new_direction.upper()} 吏꾩엯 {entry_qty} @ {price:.2f}")
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
            bal = await asyncio.to_thread(self.exchange.fetch_balance)
            free = float(bal.get('USDT', {}).get('free', 0))
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
            await self.ctrl.notify(f"?? [DualThrust] {side.upper()} {qty} @ {price:.2f}")
            logger.info(f"??[DualThrust] Entry: {side} {qty} @ {price}")
            
        except Exception as e:
            logger.error(f"DualThrust entry error: {e}")
            await self.ctrl.notify(f"??[DualThrust] 吏꾩엯 ?ㅽ뙣: {e}")

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
            await self.ctrl.notify(f"?봽 [DualThrust] {pos['side'].upper()} 泥?궛 ??{new_side.upper()} ?ㅼ쐞移?| PnL: {pnl:+.2f}")
            
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
            await self.ctrl.notify(f"?㏏ [DualThrust] [{reason}] PnL: {pnl:+.2f} ({pnl_pct:+.2f}%)")
            
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
            tp_pct = common_cfg.get('target_roe_pct', 0.0)
            sl_pct = common_cfg.get('stop_loss_pct', 0.0)
            
            msg = f"?? [DualMode] {side.upper()} 吏꾩엯: {qty} @ {entry_price}"
            
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
                    msg += f" | ?좑툘 TP/SL Error"

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
            await self.ctrl.notify(f"?㏏ [DualMode] 泥?궛 [{reason}]: PnL {pnl:.2f}")
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
        self.cfg = TradingConfig()
        self.db = DBManager(self.cfg.get('logging', {}).get('db_path', 'bot_database.db'))
        
        api = self.cfg.get('api', {})
        creds = api.get('testnet', {}) if api.get('use_testnet', True) else api.get('mainnet', {})
        
        self.exchange = ccxt.binance({
            'apiKey': creds.get('api_key', ''),
            'secret': creds.get('secret_key', ''),
            'options': {'defaultType': 'future'},
            'enableRateLimit': True
        })
        
        if api.get('use_testnet', True):
            self.exchange.set_sandbox_mode(True)
        
        self.engines = {
            CORE_ENGINE: SignalEngine(self)
        }
        logger.info("Core mode enabled: Signal(SMA/HMA) + risk controls only. Legacy engines archived.")
        self.active_engine = None
        self.tg_app = None
        self.status_data = {}
        self.is_paused = True  # 遊??쒖옉 ???쇱떆?뺤? ?곹깭 (?ㅼ젙 議곗젅 ??RESUME)
        self.dashboard_msg_id = None
        self.blink_state = False
        self.last_hourly_report = 0

    async def run(self):
        logger.info("?윟 Bot Starting... (Pure Polling Mode)")
        
        if self.cfg.get('logging', {}).get('debug_mode', False):
            logger.setLevel(logging.DEBUG)
            logger.info("?맄 Debug Mode Enabled")
        
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
        
        # ?쒖옉 ???쇱떆?뺤? ?곹깭 ?뚮┝
        await self.notify("??**遊??쒖옉??(?쇱떆?뺤? ?곹깭)**\n\n?ㅼ젙 議곗젅 ????RESUME???뚮윭二쇱꽭??")
        
        await asyncio.gather(
            self._main_polling_loop(),  # [?대쭅 ?꾩슜] 硫붿씤 ?대쭅 猷⑦봽
            self._dashboard_loop(),
            self._hourly_report_loop()
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
        watchlist = self.cfg.get('signal_engine', {}).get('watchlist', ['BTC/USDT'])
        return watchlist[0] if watchlist else 'BTC/USDT'

    async def reinit_exchange(self, use_testnet: bool):
        """嫄곕옒???곌껐 ?ъ큹湲고솕 (?뚯뒪?몃꽬/硫붿씤???꾪솚)"""
        try:
            # 1. ?꾩옱 ?붿쭊 ?뺤?
            if self.active_engine:
                self.active_engine.stop()
                logger.info("??Engine stopped for exchange reinit")
            
            # 2. ?ㅼ젙 ?낅뜲?댄듃
            await self.cfg.update_value(['api', 'use_testnet'], use_testnet)
            
            # 3. ??API ?먭꺽利앸챸 濡쒕뱶
            api = self.cfg.get('api', {})
            creds = api.get('testnet', {}) if use_testnet else api.get('mainnet', {})
            
            # 4. 嫄곕옒???ъ큹湲고솕
            self.exchange = ccxt.binance({
                'apiKey': creds.get('api_key', ''),
                'secret': creds.get('secret_key', ''),
                'options': {'defaultType': 'future'},
                'enableRateLimit': True
            })
            
            if use_testnet:
                self.exchange.set_sandbox_mode(True)
            
            # 5. 留덉폆 ?뺣낫 濡쒕뱶
            await asyncio.to_thread(self.exchange.load_markets)
            
            # 6. ?붿쭊?ㅼ뿉 ??exchange ?꾨떖
            for engine in self.engines.values():
                engine.exchange = self.exchange
                engine.position_cache = None
                engine.position_cache_time = 0
            
            # 7. ?쒖꽦 ?붿쭊 ?ъ떆??
            eng_name = self.cfg.get('system_settings', {}).get('active_engine', CORE_ENGINE)
            await self._switch_engine(eng_name)
            
            network_name = "?뚯뒪?몃꽬 ?㎦" if use_testnet else "硫붿씤???뮥"
            logger.info(f"??Exchange reinitialized: {network_name}")
            return True, network_name
            
        except Exception as e:
            logger.error(f"Exchange reinit error: {e}")
            return False, str(e)

    # ---------------- UI: 鍮꾩긽 踰꾪듉 理쒖슦??泥섎━ ----------------
    async def global_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text if update.message else ""
        
        if "STOP" in text:
            await self.emergency_stop()
            await update.message.reply_text("?썞 湲닿툒 ?뺤? ?꾨즺 - 紐⑤뱺 ?ъ???泥?궛")
            return ConversationHandler.END
        elif "PAUSE" in text:
            self.is_paused = True
            await update.message.reply_text("???쇱떆?뺤? (留ㅻℓ 以묐떒, 紐⑤땲?곕쭅 ?좎?)")
            return ConversationHandler.END
        elif "RESUME" in text:
            self.is_paused = False
            # ?붿쭊??以묒???寃쎌슦 ?ъ떆??
            if self.active_engine and not self.active_engine.running:
                self.active_engine.start()
                await update.message.reply_text("??留ㅻℓ ?ш컻 (?붿쭊 ?ъ떆?묐맖)")
            else:
                await update.message.reply_text("??留ㅻℓ ?ш컻")
            return ConversationHandler.END
        elif text == "/status":
            self.dashboard_msg_id = None
            await update.message.reply_text("?봽 ??쒕낫??媛깆떊")
            return ConversationHandler.END
        
        return None

    async def show_setup_menu(self, update: Update):
        sys_cfg = self.cfg.get('system_settings', {})
        sig = self.cfg.get('signal_engine', {})
        sha = self.cfg.get('shannon_engine', {})
        dt = self.cfg.get('dual_thrust_engine', {})
        dm = self.cfg.get('dual_mode_engine', {})
        eng = sys_cfg.get('active_engine', CORE_ENGINE)
        direction = sys_cfg.get('trade_direction', 'both')
        
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
            watchlist = sig.get('watchlist', ['BTC/USDT'])
            symbol = watchlist[0] if watchlist else 'BTC/USDT'
            
        status = "?뵶 OFF" if self.is_paused else "?윟 ON"
        direction_str = {'both': '양방향', 'long': '롱만', 'short': '숏만'}.get(direction, 'both')
        
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
        tp_sl_status = "ON" if sig_common.get('tp_sl_enabled', True) else "OFF"
        
        # Signal ?꾨왂 ?ㅼ젙 (異붽?)
        strategy_params = sig.get('strategy_params', {})
        active_strategy = strategy_params.get('active_strategy', 'sma').upper()
        entry_mode = strategy_params.get('entry_mode', 'cross').upper()
        hma_params = strategy_params.get('HMA', {})
        hma_fast = hma_params.get('fast_period', 9)
        hma_slow = hma_params.get('slow_period', 21)
        
        # Scanner ?곹깭
        scanner_enabled = sig_common.get('scanner_enabled', True)
        scanner_status = "ON ?뱻" if scanner_enabled else "OFF"
        scanner_tf = sig_common.get('scanner_timeframe', '15m')
        scanner_exit_tf = sig_common.get('scanner_exit_timeframe', '1h')

        # Hourly Report Status
        hourly_report_status = "ON" if self.cfg.get('telegram', {}).get('reporting', {}).get('hourly_report_enabled', True) else "OFF"

        # Filter Status (Split)
        r2_entry = "ON" if sig_common.get('r2_entry_enabled', True) else "OFF"
        r2_exit = "ON" if sig_common.get('r2_exit_enabled', True) else "OFF"
        r2_threshold = sig_common.get('r2_threshold', 0.25)
        
        hurst_entry = "ON" if sig_common.get('hurst_entry_enabled', True) else "OFF"
        hurst_exit = "ON" if sig_common.get('hurst_exit_enabled', True) else "OFF"
        hurst_threshold = sig_common.get('hurst_threshold', 0.55)
        
        chop_entry = "ON" if sig_common.get('chop_entry_enabled', True) else "OFF"
        chop_exit = "ON" if sig_common.get('chop_exit_enabled', True) else "OFF"
        chop_threshold = sig_common.get('chop_threshold', 50.0)
        
        kalman_cfg = strategy_params.get('kalman_filter', {})
        kalman_entry = "ON" if kalman_cfg.get('entry_enabled', False) else "OFF"
        kalman_exit = "ON" if kalman_cfg.get('exit_enabled', False) else "OFF"
        
        cc_exit = "ON" if sig_common.get('cc_exit_enabled', False) else "OFF"
        cc_threshold = sig_common.get('cc_threshold', 0.70)
        
        # ?ㅽ듃?뚰겕 ?곹깭
        use_testnet = self.cfg.get('api', {}).get('use_testnet', True)
        network_status = "?뚯뒪?몃꽬 ?㎦" if use_testnet else "硫붿씤???뮥"
        
        msg = f"""
?뵩 **?ㅼ젙 硫붾돱** (踰덊샇 ?낅젰)

**?꾩옱 ?곹깭**: {eng.upper()} | {symbol}

?곣봺??怨듯넻 ?ㅼ젙 ?곣봺??
1. ?덈쾭由ъ? (`{lev}諛?)
2. 紐⑺몴 ROE (`{sig_common.get('target_roe_pct', 20)}%`)
3. ?먯젅 (`{sig_common.get('stop_loss_pct', 10)}%`)
4. 吏꾩엯 ??꾪봽?덉엫 (`{sig_common.get('timeframe', '15m')}`)
41. 泥?궛 ??꾪봽?덉엫 (`{sig_common.get('exit_timeframe', '4h')}`)
5. ?먯떎 ?쒗븳 (`${sig_common.get('daily_loss_limit', 5000)}`)
6. 吏꾩엯 鍮꾩쑉 (`{sig_common.get('risk_per_trade_pct', 50)}%`)
7. 留ㅻℓ 諛⑺뼢 (`{direction_str}`)
8. ?щ낵 蹂寃?(`{symbol}`)

?곣봺??Signal ?꾩슜 ?곣봺??
16. ?꾨왂 (`{active_strategy}`)
18. 吏꾩엯紐⑤뱶 (`{entry_mode}`) - SMA/HMA??
10. SMA 湲곌컙 (`{fast_sma}/{slow_sma}`)
17. HMA 湲곌컙 (`{hma_fast}/{hma_slow}`)
20. VBO ?ㅼ젙 (LEGACY 遺꾨━)
21. FractalFisher ?ㅼ젙 (LEGACY 遺꾨━)
13. TP/SL ?먮룞泥?궛 (`{tp_sl_status}`)
23. 嫄곕옒?됯툒?깆콈援?(`{scanner_status}`) (`TF: {scanner_tf}`)
24. 湲됰벑梨꾧뎬 吏꾩엯 ?꾨젅???ㅼ젙
25. 湲됰벑梨꾧뎬 泥?궛 ?꾨젅???ㅼ젙 (`{scanner_exit_tf}`)

**?꾪꽣 (Entry / Exit)**
26. R2 ?꾪꽣 (`{r2_entry}` / `{r2_exit}`) (湲곗?: `{r2_threshold}`)
28. Hurst ?꾪꽣 (`{hurst_entry}` / `{hurst_exit}`) (湲곗?: `{hurst_threshold}`)
30. CHOP ?꾪꽣 (`{chop_entry}` / `{chop_exit}`) (湲곗?: `{chop_threshold}`)
32. Kalman ?꾪꽣 (`{kalman_entry}` / `{kalman_exit}`)
33. CC ?꾪꽣 (Exit Only) (`{cc_exit}`) (湲곗?: `{cc_threshold}`)

27. R2 誘쇨컧???ㅼ젙
29. Hurst 誘쇨컧???ㅼ젙
31. CHOP 誘쇨컧???ㅼ젙
34. CC 誘쇨컧???ㅼ젙

?곣봺??Shannon ?꾩슜 (LEGACY) ?곣봺??11. ?먯궛 鍮꾩쑉 (`{shannon_ratio}%`)
12. Grid ?ㅼ젙 (`{grid_enabled}`, ${grid_size})

?곣봺??Dual Thrust ?꾩슜 (LEGACY) ?곣봺??14. N Days (`{dt_n}??)
15. K1/K2 (`{dt_k1}/{dt_k2}`)

?곣봺??Dual Mode ?꾩슜 (LEGACY) ?곣봺??35. 紐⑤뱶 蹂寃?({dm_mode}, {dm_tf})

?곣봺???쒖뒪???곣봺??
22. ?ㅽ듃?뚰겕 ?꾪솚 (`{network_status}`)
42. ?쒓컙蹂?由ы룷??(`{hourly_report_status}`)


?곣봺???쒖뼱 ?곣봺??
00. ?? ?붿쭊 援먯껜 (?꾩옱: {eng.upper()})
9. ?띰툘 留ㅻℓ?쒖옉/以묒? ({status})
0. ?섍?湲?
"""
        await update.message.reply_text(msg.strip(), parse_mode=ParseMode.MARKDOWN)

    async def setup_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.show_setup_menu(update)
        return SELECT

    async def setup_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        
        if text == '0':
            await update.message.reply_text("???ㅼ젙 醫낅즺")
            return ConversationHandler.END
        
        context.user_data['setup_choice'] = text
        
        prompts = {
            '1': "?뱷 **?덈쾭由ъ?** 媛믪쓣 ?낅젰?섏꽭??(1~5諛? ?? 5)",
            '2': "?뱷 **紐⑺몴 ROE** (%)瑜??낅젰?섏꽭??(?? 20)",
            '3': "?뱷 **?먯젅 鍮꾩쑉** (%)瑜??낅젰?섏꽭??(?? 5)",
            '4': "?뱷 **吏꾩엯 ??꾪봽?덉엫**???낅젰?섏꽭??(?? 15m)\n1m,2m,3m,5m,15m,30m | 1h,2h,4h | 1d",
            '41': "?뱷 **泥?궛 ??꾪봽?덉엫**???낅젰?섏꽭??(?? 1h)\n1m,2m,3m,5m,15m,30m | 1h,2h,4h | 1d",
            '5': "?뱷 **?쇱씪 ?먯떎 ?쒗븳** ($)???낅젰?섏꽭??(?? 1000)",
            '6': "?뮥 **吏꾩엯 鍮꾩쑉(%)**???낅젰?섏꽭??(safety cap ?좏슜)",
            '7': "?뺧툘 **留ㅻℓ 諛⑺뼢**???좏깮?섏꽭??(1=?묐갑?? 2=濡깅쭔, 3=?뤿쭔)",
            '8': "?뮦 **蹂寃쏀븷 肄붿씤 ?щ낵**???낅젰?섍굅??踰덊샇瑜??좏깮?섏꽭??\n\n1: BTC  2: ETH  3: SOL\n?먮뒗 吏곸젒 ?낅젰 (?? **DOGE**, **XRP**, **PEPE**)",
            '9': "?띰툘 留ㅻℓ瑜??쒖옉?섏떆寃좎뒿?덇퉴? (1=?쒖옉, 0=以묒?)",
            '10': "?뱢 **SMA 湲곌컙**???낅젰?섏꽭??(?뺤떇: fast,slow ?? 2,10)",
            '11': "?뱷 **?먯궛 鍮꾩쑉** (%)瑜??낅젰?섏꽭??(?? 50 = 50%)",
            '12': "?뱷 **Grid ?ㅼ젙**???낅젰?섏꽭??(?? on,200 ?먮뒗 off)",
            '14': "?뱷 **N Days** (Range 怨꾩궛 ?쇱닔)瑜??낅젰?섏꽭??(?? 4)",
            '15': "?뱷 **K1/K2** (諛곗닔)瑜??낅젰?섏꽭??(?? 0.5,0.5 ?먮뒗 0.4,0.6)",
            '16': "?뱷 **?꾨왂 ?좏깮** (1=SMA, 2=HMA)",
            '17': "?뱷 **HMA 湲곌컙**???낅젰?섏꽭??(?? 9,21)",
            '18': "?뱷 **吏꾩엯紐⑤뱶**瑜??좏깮?섏꽭??(1=Cross, 2=Position)",
            '20': "?뱷 **VBO ?ㅼ젙**: LEGACY濡?遺꾨━??",
            '21': "?뱷 **FractalFisher ?ㅼ젙**: LEGACY濡?遺꾨━??",
            '22': "?뱷 **?ㅽ듃?뚰겕 ?좏깮** (1=?뚯뒪?몃꽬, 2=硫붿씤??",
            '23': "?뱷 **嫄곕옒??湲됰벑 梨꾧뎬 湲곕뒫**??耳쒖떆寃좎뒿?덇퉴? (1=ON, 0=OFF)",
            '24': "?뱷 **梨꾧뎬 吏꾩엯 ??꾪봽?덉엫**???낅젰?섏꽭??(?? 5m)\n1m, 5m, 15m, 30m, 1h",
            '25': "?뱷 **梨꾧뎬 泥?궛 ??꾪봽?덉엫**???낅젰?섏꽭??(?? 1h)\n1m, 5m, 15m, 30m, 1h, 4h",
            '26': "?뱷 **異붿꽭 ?꾪꽣($R^2$) 湲곕뒫**??耳쒖떆寃좎뒿?덇퉴? (1=ON, 0=OFF)", # Toggle?대?濡??ㅼ젣濡쒕뒗 ?ъ슜?섏? ?딆쓣 ???덉쑝??prompt dict 援ъ깋 留욎땄
            '27': "?뱷 **$R^2$ 湲곗?媛?*???낅젰?섏꽭??(0.1 ~ 0.5 沅뚯옣)\n- ??쓣?섎줉(0.1): 吏꾩엯 ?먯＜ ??(?몄씠利??덉슜)\n- ?믪쓣?섎줉(0.4): ?뺤떎??異붿꽭留?吏꾩엯 (吏꾩엯 媛먯냼)",
            '28': "?뱷 **Hurst ?꾪꽣**瑜?耳쒖떆寃좎뒿?덇퉴? (1=ON, 0=OFF)", 
            '29': "?뱷 **Hurst 湲곗?媛?*???낅젰?섏꽭??(?? 0.55)\n- 0.5 ?댄븯???됯퇏?뚭?(?〓낫), 0.5 ?댁긽? 異붿꽭.\n- ?믪쓣?섎줉 媛뺥븳 異붿꽭留?吏꾩엯.",
            '30': "?뱷 **CHOP ?꾪꽣**瑜?耳쒖떆寃좎뒿?덇퉴? (1=ON, 0=OFF)",
            '31': "?뱷 **CHOP 湲곗?媛?*???낅젰?섏꽭??(?? 50.0)\n- 100??媛源뚯슱?섎줉 ?〓낫(Choppy).\n- ?ㅼ젙媛?**蹂대떎 ?щ㈃** 吏꾩엯 湲덉?.",
            '33': "?뱷 **CC ?꾪꽣**瑜?耳쒖떆寃좎뒿?덇퉴? (1=ON, 0=OFF)",
            '34': "?뱷 **CC ?ㅼ젙**???낅젰?섏꽭??(?? 0.70 ?먮뒗 0.70,14)\n- ?뺤떇: ?꾧퀎媛??먮뒗 ?꾧퀎媛?湲곌컙\n- ??쓣?섎줉 誘쇨컧, ?믪쓣?섎줉 媛뺥븳 異붿꽭留?泥?궛.",
            '35': "?뱷 **Dual Mode 蹂寃?* (1=Standard, 2=Scalping)",
        }
        if text == '7':
            keyboard = [
                [KeyboardButton("?묐갑??(Long+Short)")],
                [KeyboardButton("濡깅쭔 (Long Only)")],
                [KeyboardButton("?뤿쭔 (Short Only)")]
            ]
            await update.message.reply_text(
                "?뱷 **留ㅻℓ 諛⑺뼢** ?좏깮:",
                reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
                parse_mode=ParseMode.MARKDOWN
            )
            return DIRECTION_SELECT
        elif text == '00':
            msg = """
?? **?붿쭊 援먯껜**
?ъ슜???붿쭊 踰덊샇瑜??좏깮?섏꽭??

1. ?뱻 **Signal Engine**
   - ?꾨왂 湲곕컲 (SMA, HMA, VBO ??
   - ?ㅼ뼇???뚰듃肄붿씤 吏??

2. ?뽳툘 **Shannon Engine**
   - ?먯궛 諛곕텇 & 由щ갭?곗떛
   - 蹂?숈꽦 ?쒖슜 (Grid Trading)

3. ?뮙 **Dual Thrust Engine**
   - 蹂?숈꽦 ?뚰뙆 ?꾨왂
   - 異붿꽭 異붿쥌

4. ?쏉툘 **Dual Mode Engine**
   - Fractal Choppiness + Kalman
   - Scalping / Standard 紐⑤뱶

5. ?뙥截?**TEMA Engine**
   - RSI + TEMA + Bollinger Strategy
   - 鍮좊Ⅸ 諛섏쓳 ?띾룄 (怨듯넻 ?ㅼ젙 怨듭쑀)
"""
            await update.message.reply_text(msg.strip(), parse_mode=ParseMode.MARKDOWN)
            return ENGINE_SELECT

        elif text == '9':
            self.is_paused = not self.is_paused
            await self.show_setup_menu(update)
            return SELECT
        elif text == '13':
            # TP/SL ?좉?
            current = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('tp_sl_enabled', True)
            new_val = not current
            await self.cfg.update_value(['signal_engine', 'common_settings', 'tp_sl_enabled'], new_val)
            status = "ON" if new_val else "OFF"
            await update.message.reply_text(f"??TP/SL ?먮룞泥?궛: {status}")
            await self.show_setup_menu(update)
            return SELECT
        elif text == '42':
            # Hourly Report Toggle
            curr = self.cfg.get('telegram', {}).get('reporting', {}).get('hourly_report_enabled', True)
            new_val = not curr
            await self.cfg.update_value(['telegram', 'reporting', 'hourly_report_enabled'], new_val)
            status = "ON" if new_val else "OFF"
            await update.message.reply_text(f"?숋툘 ?쒓컙蹂?由ы룷?? {status}")
            await self.show_setup_menu(update)
            return SELECT
        elif text == '26':
            # R2 Filter Menu
            keyboard = [[KeyboardButton("1. Entry Toggle"), KeyboardButton("2. Exit Toggle")]]
            await update.message.reply_text(
                "?뱣 **R2 ?꾪꽣 ?ㅼ젙**:\n1. Entry ?꾪꽣 ?좉?\n2. Exit ?꾪꽣 ?좉?",
                reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
            )
            return "R2_SELECT"

        elif text == '28':
            # Hurst Filter Menu
            keyboard = [[KeyboardButton("1. Entry Toggle"), KeyboardButton("2. Exit Toggle")]]
            await update.message.reply_text(
                "?뙄 **Hurst ?꾪꽣 ?ㅼ젙**:\n1. Entry ?꾪꽣 ?좉?\n2. Exit ?꾪꽣 ?좉?",
                reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
            )
            return "HURST_SELECT"

        elif text == '30':
            # Chop Filter Menu
            keyboard = [[KeyboardButton("1. Entry Toggle"), KeyboardButton("2. Exit Toggle")]]
            await update.message.reply_text(
                "?? **Chop ?꾪꽣 ?ㅼ젙**:\n1. Entry ?꾪꽣 ?좉?\n2. Exit ?꾪꽣 ?좉?",
                reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
            )
            return "CHOP_SELECT"

        elif text == '32':
            # Kalman Filter Menu
            keyboard = [[KeyboardButton("1. Entry Toggle"), KeyboardButton("2. Exit Toggle")]]
            await update.message.reply_text(
                "?? **Kalman ?꾪꽣 ?ㅼ젙**:\n1. Entry ?꾪꽣 ?좉?\n2. Exit ?꾪꽣 ?좉?",
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
            await update.message.reply_text(f"??CC ?꾪꽣 (Exit): {status}")
            await self.show_setup_menu(update)
            return SELECT
            
        elif text == '24':
            await update.message.reply_text(prompts['24'])
            return INPUT

        elif text == '27':
            # R2 Threshold Input
            await update.message.reply_text(prompts['27'])
            return INPUT
        elif text == '29':
            return INPUT
        elif text == '31':
            return INPUT
        elif text in prompts:
            await update.message.reply_text(prompts[text], parse_mode=ParseMode.MARKDOWN)
            if text == '8':
                return SYMBOL_INPUT
            return INPUT
        else:
            await update.message.reply_text("???섎せ??踰덊샇?낅땲??")
            return SELECT

    async def handle_manual_symbol_input(self, update: Update, symbol: str):
        """[New] ?붾젅洹몃옩 ?섎룞 ?щ낵 ?낅젰 泥섎━"""
        try:
            # ?щ낵 ?щ㎎??(BTC -> BTC/USDT)
            if '/' not in symbol:
                symbol = f"{symbol}/USDT"
            
            # ?щ낵 ?좏슚??寃??(Exchange check)
            # SignalEngine???쒖꽦?붾릺???덉뼱????
            eng_type = self.cfg.get('system_settings', {}).get('active_engine', CORE_ENGINE)
            if eng_type != 'signal':
                await update.message.reply_text("?좑툘 ?꾩옱 Signal ?붿쭊???쒖꽦?붾릺???덉? ?딆뒿?덈떎. (/strat 1)")
                return

            signal_engine = self.engines.get('signal')
            if not signal_engine:
                await update.message.reply_text("??Signal ?붿쭊??李얠쓣 ???놁뒿?덈떎.")
                return

            # ?щ낵 寃利?(exchange load_markets ?꾩슂?????덉쓬, ?ш린??try fetch ticker濡??泥?
            try:
                await asyncio.to_thread(self.exchange.fetch_ticker, symbol)
            except Exception:
                await update.message.reply_text(f"???좏슚?섏? ?딆? ?щ낵?낅땲?? {symbol}")
                return

            # Active Symbols??異붽?
            if symbol not in signal_engine.active_symbols:
                signal_engine.active_symbols.add(symbol)
                await update.message.reply_text(f"??**{symbol}** 媛먯떆 ?쒖옉! (?섎룞 ?낅젰)")
                logger.info(f"Manual symbol added: {symbol}")
                
                # 利됱떆 ?대쭅 ?몃━嫄?(?좏깮 ?ы빆)
                # await signal_engine.poll_symbol(symbol) 
            else:
                await update.message.reply_text(f"?뱄툘 ?대? 媛먯떆 以묒씤 ?щ낵?낅땲?? {symbol}")

        except Exception as e:
            logger.error(f"Manual input error: {e}")
            await update.message.reply_text(f"??泥섎━ ?ㅽ뙣: {e}")

    async def setup_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        choice = context.user_data.get('setup_choice')
        val = update.message.text
        
        try:
            if choice == '1':
                v = int(val)
                # ?덈쾭由ъ? 理쒕? 20諛??쒗븳 (?ъ슜???붿껌: 5 -> 20)
                if v < 1 or v > 20:
                    await update.message.reply_text("???덈쾭由ъ???1~20諛??ъ씠留?媛?ν빀?덈떎.")
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
                await update.message.reply_text(f"???덈쾭由ъ? 蹂寃? {v}x")
            elif choice == '2':
                await self.cfg.update_value(['signal_engine', 'common_settings', 'target_roe_pct'], float(val))
                await self.cfg.update_value(['dual_mode_engine', 'target_roe_pct'], float(val))
            elif choice == '3':
                await self.cfg.update_value(['signal_engine', 'common_settings', 'stop_loss_pct'], float(val))
                await self.cfg.update_value(['dual_thrust_engine', 'stop_loss_pct'], float(val))
                await self.cfg.update_value(['dual_mode_engine', 'stop_loss_pct'], float(val))
            elif choice == '4':
                # ??꾪봽?덉엫 ?좏슚??寃??
                valid_tf = ['1m', '2m', '3m', '4m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', '1d', '3d', '1w', '1M']
                if val not in valid_tf:
                    await update.message.reply_text(f"???좏슚?섏? ?딆? ??꾪봽?덉엫.\n?ъ슜 媛?? {', '.join(valid_tf)}")
                    return SELECT
                # ??꾪봽?덉엫 ?낅뜲?댄듃 (Common, Signal, Shannon 紐⑤몢 ?곸슜)
                await self.cfg.update_value(['signal_engine', 'common_settings', 'timeframe'], val)
                await self.cfg.update_value(['signal_engine', 'common_settings', 'entry_timeframe'], val) # Sync Entry TF
                await self.cfg.update_value(['shannon_engine', 'timeframe'], val)
                
                # DualMode ??꾪봽?덉엫 蹂寃?(?꾩옱 紐⑤뱶??留욎떠??
                dm_mode = self.cfg.get('dual_mode_engine', {}).get('mode', 'standard')
                if dm_mode == 'scalping':
                    await self.cfg.update_value(['dual_mode_engine', 'scalping_tf'], val)
                else:
                    await self.cfg.update_value(['dual_mode_engine', 'standard_tf'], val)

                # Shannon ?붿쭊??200 EMA 罹먯떆 珥덇린??(????꾪봽?덉엫 利됱떆 ?곸슜)
                if self.active_engine and hasattr(self.active_engine, 'ema_200'):
                    self.active_engine.ema_200 = None
                    self.active_engine.trend_direction = None
                    self.active_engine.last_indicator_update = 0
                # Signal ?붿쭊 罹먯떆??珥덇린??
                signal_engine = self.engines.get('signal')
                if signal_engine:
                    signal_engine.last_processed_candle_ts = {}
                    signal_engine.last_candle_time = {}
                await update.message.reply_text(f"??吏꾩엯 ??꾪봽?덉엫 蹂寃? {val}")
                # DualMode ?붿쭊 罹먯떆 珥덇린??
                dm_engine = self.engines.get('dualmode')
                if dm_engine:
                    dm_engine.last_candle_ts = 0
                
                await update.message.reply_text(f"????꾪봽?덉엫 蹂寃? {val} (DualMode: {dm_mode} TF ?곸슜)")
            elif choice == '5':
                await self.cfg.update_value(['shannon_engine', 'daily_loss_limit'], float(val))
                await self.cfg.update_value(['signal_engine', 'common_settings', 'daily_loss_limit'], float(val))
                await self.cfg.update_value(['dual_thrust_engine', 'daily_loss_limit'], float(val))
                await self.cfg.update_value(['dual_mode_engine', 'daily_loss_limit'], float(val))
            elif choice == '6':
                v = float(val)
                max_risk = float(self.cfg.get('signal_engine', {}).get('common_settings', {}).get('max_risk_per_trade_pct', 20.0) or 20.0)
                if v < 1 or v > max_risk:
                    await update.message.reply_text(f"??1~{max_risk:.0f} ?ъ씠??媛믪쓣 ?낅젰?섏꽭??")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'risk_per_trade_pct'], v)
                await self.cfg.update_value(['dual_thrust_engine', 'risk_per_trade_pct'], v)
                await self.cfg.update_value(['dual_mode_engine', 'risk_per_trade_pct'], v)
            
            elif choice == '24':
                # Scanner Entry Timeframe
                valid_tf = ['1m', '2m', '3m', '5m', '15m', '30m', '1h', '4h']
                if val not in valid_tf:
                    await update.message.reply_text(f"???좏슚?섏? ?딆? ??꾪봽?덉엫.\n異붿쿇: 1m, 5m, 15m")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_timeframe'], val)
                await update.message.reply_text(f"??梨꾧뎬 吏꾩엯 ??꾪봽?덉엫 蹂寃? {val}")

            elif choice == '25':
                # Scanner Exit Timeframe
                valid_tf = ['1m', '2m', '3m', '5m', '15m', '30m', '1h', '4h']
                if val not in valid_tf:
                    await update.message.reply_text(f"???좏슚?섏? ?딆? ??꾪봽?덉엫.\n異붿쿇: 1m, 5m, 15m, 1h")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_exit_timeframe'], val)
                await update.message.reply_text(f"??梨꾧뎬 泥?궛 ??꾪봽?덉엫 蹂寃? {val}")

            # ======== Signal (SMA) ?꾩슜 ========
            elif choice == '10':
                # SMA 湲곌컙 蹂寃?(?뺤떇: "2,10" ?먮뒗 "5,25")
                parts = val.replace(' ', '').split(',')
                if len(parts) != 2:
                    await update.message.reply_text("???뺤떇: fast,slow (?? 2,10)")
                    return SELECT
                fast, slow = int(parts[0]), int(parts[1])
                if fast >= slow:
                    await update.message.reply_text("??fast SMA媛 slow蹂대떎 ?묒븘???⑸땲??")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'Triple_SMA', 'fast_sma'], fast)
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'Triple_SMA', 'slow_sma'], slow)
                await update.message.reply_text(f"??SMA 湲곌컙 蹂寃? {fast}/{slow}")
            
            # ======== Shannon ?꾩슜 ========
            elif choice == '11':
                # ?먯궛 鍮꾩쑉 蹂寃?(?뺤떇: "50" = 50%)
                v = float(val)
                if v < 10 or v > 90:
                    await update.message.reply_text("??10~90 ?ъ씠??媛믪쓣 ?낅젰?섏꽭??")
                    return SELECT
                ratio = v / 100.0
                await self.cfg.update_value(['shannon_engine', 'asset_allocation', 'target_ratio'], ratio)
                # Shannon ?붿쭊??利됱떆 ?곸슜
                shannon_engine = self.engines.get('shannon')
                if shannon_engine:
                    shannon_engine.ratio = ratio
                await update.message.reply_text(f"??Shannon ?먯궛 鍮꾩쑉 蹂寃? {int(v)}%")
            
            elif choice == '12':
                # Grid ?ㅼ젙 (?뺤떇: "on,200" ?먮뒗 "off")
                val_lower = val.lower().strip()
                if val_lower == 'off':
                    await self.cfg.update_value(['shannon_engine', 'grid_trading', 'enabled'], False)
                    await update.message.reply_text("??Grid Trading: OFF")
                else:
                    parts = val_lower.replace(' ', '').split(',')
                    if parts[0] == 'on' and len(parts) >= 2:
                        size = float(parts[1])
                        await self.cfg.update_value(['shannon_engine', 'grid_trading', 'enabled'], True)
                        await self.cfg.update_value(['shannon_engine', 'grid_trading', 'order_size_usdt'], size)
                        await update.message.reply_text(f"??Grid Trading: ON, ${size}")
                    else:
                        await update.message.reply_text("???뺤떇: on,湲덉븸 ?먮뒗 off (?? on,200)")
                        return SELECT
            
            # ======== Dual Thrust ?꾩슜 ========
            elif choice == '14':
                # N Days 蹂寃?
                v = int(val)
                if v < 1 or v > 30:
                    await update.message.reply_text("??1~30 ?ъ씠??媛믪쓣 ?낅젰?섏꽭??")
                    return SELECT
                await self.cfg.update_value(['dual_thrust_engine', 'n_days'], v)
                # ?붿쭊 罹먯떆 珥덇린??(??N?쇰줈 ?몃━嫄??ш퀎??
                dt_engine = self.engines.get('dualthrust')
                if dt_engine:
                    dt_engine.trigger_date = None
                await update.message.reply_text(f"??Dual Thrust N Days 蹂寃? {v}")
            
            elif choice == '15':
                # K1/K2 蹂寃?(?뺤떇: "0.5,0.5" ?먮뒗 "0.4,0.6")
                parts = val.replace(' ', '').split(',')
                if len(parts) != 2:
                    await update.message.reply_text("???뺤떇: k1,k2 (?? 0.5,0.5)")
                    return SELECT
                k1, k2 = float(parts[0]), float(parts[1])
                if k1 <= 0 or k1 > 1 or k2 <= 0 or k2 > 1:
                    await update.message.reply_text("??K1, K2??0~1 ?ъ씠??媛믪씠?댁빞 ?⑸땲??")
                    return SELECT
                await self.cfg.update_value(['dual_thrust_engine', 'k1'], k1)
                await self.cfg.update_value(['dual_thrust_engine', 'k2'], k2)
                # ?붿쭊 罹먯떆 珥덇린??(??K濡??몃━嫄??ш퀎??
                dt_engine = self.engines.get('dualthrust')
                if dt_engine:
                    dt_engine.trigger_date = None
                await update.message.reply_text(f"??Dual Thrust K1/K2 蹂寃? {k1}/{k2}")
            # ======== Signal ?좉퇋 ?듭뀡 ========
            elif choice == '16':
                # ?꾨왂 蹂寃?(踰덊샇 ?먮뒗 ?대쫫?쇰줈 ?좏깮)
                strategy_map = {'1': 'sma', '2': 'hma'}
                val_lower = val.lower().strip()
                
                # 踰덊샇 ?낅젰 ??蹂??
                if val_lower in strategy_map:
                    val_lower = strategy_map[val_lower]
                
                if val_lower in ['sma', 'hma']:
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
                    await update.message.reply_text(f"???꾨왂 蹂寃? {val_lower.upper()}")
                else:
                    await update.message.reply_text("??1~2 ?먮뒗 sma/hma ?낅젰\n1=SMA, 2=HMA")
                    return SELECT
            
            elif choice == '20':
                await update.message.reply_text("MicroVBO is archived in legacy mode. Core mode uses SMA/HMA only.")
                return SELECT
                # VBO ?ㅼ젙 蹂寃?(?뺤떇: "atr湲곌컙,?뚰뙆諛곗닔,TP諛곗닔,SL諛곗닔")
                parts = val.replace(' ', '').split(',')
                if len(parts) != 4:
                    await update.message.reply_text("???뺤떇: atr,?뚰뙆,TP,SL (?? 14,0.5,1.0,0.5)")
                    return SELECT
                try:
                    atr_period = int(parts[0])
                    breakout_mult = float(parts[1])
                    tp_mult = float(parts[2])
                    sl_mult = float(parts[3])
                    
                    await self.cfg.update_value(['signal_engine', 'strategy_params', 'MicroVBO', 'atr_period'], atr_period)
                    await self.cfg.update_value(['signal_engine', 'strategy_params', 'MicroVBO', 'breakout_multiplier'], breakout_mult)
                    await self.cfg.update_value(['signal_engine', 'strategy_params', 'MicroVBO', 'tp_atr_multiplier'], tp_mult)
                    await self.cfg.update_value(['signal_engine', 'strategy_params', 'MicroVBO', 'sl_atr_multiplier'], sl_mult)
                    
                    await update.message.reply_text(f"??VBO ?ㅼ젙: ATR={atr_period}, ?뚰뙆={breakout_mult}, TP={tp_mult}x, SL={sl_mult}x")
                except ValueError:
                    await update.message.reply_text("???щ컮瑜??レ옄瑜??낅젰?섏꽭??")
                    return SELECT
            
            elif choice == '21':
                await update.message.reply_text("FractalFisher is archived in legacy mode. Core mode uses SMA/HMA only.")
                return SELECT
                # FractalFisher ?ㅼ젙 蹂寃?(?뺤떇: "hurst湲곌컙,hurst?꾧퀎,fisher湲곌컙,trailing諛곗닔")
                parts = val.replace(' ', '').split(',')
                if len(parts) != 4:
                    await update.message.reply_text("???뺤떇: hurst湲곌컙,hurst?꾧퀎,fisher湲곌컙,trailing諛곗닔 (?? 100,0.55,10,2.0)")
                    return SELECT
                try:
                    hurst_period = int(parts[0])
                    hurst_threshold = float(parts[1])
                    fisher_period = int(parts[2])
                    trailing_mult = float(parts[3])
                    
                    await self.cfg.update_value(['signal_engine', 'strategy_params', 'FractalFisher', 'hurst_period'], hurst_period)
                    await self.cfg.update_value(['signal_engine', 'strategy_params', 'FractalFisher', 'hurst_threshold'], hurst_threshold)
                    await self.cfg.update_value(['signal_engine', 'strategy_params', 'FractalFisher', 'fisher_period'], fisher_period)
                    await self.cfg.update_value(['signal_engine', 'strategy_params', 'FractalFisher', 'atr_trailing_multiplier'], trailing_mult)
                    
                    await update.message.reply_text(f"??FractalFisher: Hurst={hurst_period}遊?{hurst_threshold}, Fisher={fisher_period}, Trailing={trailing_mult}x ATR")
                except ValueError:
                    await update.message.reply_text("???щ컮瑜??レ옄瑜??낅젰?섏꽭??")
                    return SELECT
            
            elif choice == '17':
                # HMA 湲곌컙 蹂寃?(?뺤떇: "9,21")
                parts = val.replace(' ', '').split(',')
                if len(parts) != 2:
                    await update.message.reply_text("???뺤떇: fast,slow (?? 9,21)")
                    return SELECT
                fast, slow = int(parts[0]), int(parts[1])
                if fast >= slow:
                    await update.message.reply_text("??fast HMA媛 slow蹂대떎 ?묒븘???⑸땲??")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'HMA', 'fast_period'], fast)
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'HMA', 'slow_period'], slow)
                await update.message.reply_text(f"??HMA 湲곌컙 蹂寃? {fast}/{slow}")
            
            elif choice == '18':
                # 吏꾩엯紐⑤뱶 蹂寃?(1=cross, 2=position)
                if val == '1':
                    val_lower = 'cross'
                elif val == '2':
                    val_lower = 'position'
                else:
                    await update.message.reply_text("??1(Cross) ?먮뒗 2(Position)瑜??낅젰?섏꽭??")
                    return SELECT
                
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'entry_mode'], val_lower)
                await update.message.reply_text(f"??吏꾩엯紐⑤뱶 蹂寃? {val_lower.upper()}")
            
            elif choice == '22':
                # ?ㅽ듃?뚰겕 ?꾪솚 (1=?뚯뒪?몃꽬, 2=硫붿씤??
                if val not in ['1', '2']:
                    await update.message.reply_text("??1 ?먮뒗 2瑜??낅젰?섏꽭??\n1=?뚯뒪?몃꽬, 2=硫붿씤??")
                    return SELECT
                
                use_testnet = val == '1'
                current = self.cfg.get('api', {}).get('use_testnet', True)
                
                if use_testnet == current:
                    network_name = "?뚯뒪?몃꽬 ?㎦" if use_testnet else "硫붿씤???뮥"
                    await update.message.reply_text(f"?뱄툘 ?대? {network_name} ?ъ슜 以묒엯?덈떎.")
                else:
                    # ?꾪솚 ?뺤씤 硫붿떆吏
                    target_name = "?뚯뒪?몃꽬 ?㎦" if use_testnet else "硫붿씤???뮥"
                    await update.message.reply_text(f"?봽 {target_name}(??濡??꾪솚 以?..")
                    
                    success, result = await self.reinit_exchange(use_testnet)
                    
                    if success:
                        await update.message.reply_text(f"???ㅽ듃?뚰겕 ?꾪솚 ?꾨즺: {result}")
                    else:
                        await update.message.reply_text(f"???ㅽ듃?뚰겕 ?꾪솚 ?ㅽ뙣: {result}")
            
            elif choice == '23':
                # Scanner Toggle
                if val in ['1', 'on', 'ON']:
                    new_val = True
                elif val in ['0', 'off', 'OFF']:
                    new_val = False
                else:
                    await update.message.reply_text("??1(ON) ?먮뒗 0(OFF)瑜??낅젰?섏꽭??")
                    return SELECT
                
                await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], new_val)
                status = "ON ?뱻" if new_val else "OFF"
                await update.message.reply_text(f"??嫄곕옒??湲됰벑 梨꾧뎬: {status}")

            elif choice == '27':
                # R2 Threshold
                v = float(val)
                if v < 0.01 or v > 0.9:
                    await update.message.reply_text("??0.01 ~ 0.9 ?ъ씠??媛믪쓣 ?낅젰?섏꽭??")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'r2_threshold'], v)
                await update.message.reply_text(f"??異붿꽭 ?꾪꽣 誘쇨컧??蹂寃? {v}")

            elif choice == '29':
                # Hurst Threshold
                v = float(val)
                if v < 0.0 or v > 1.0:
                    await update.message.reply_text("??0.0 ~ 1.0 ?ъ씠??媛믪쓣 ?낅젰?섏꽭??")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'hurst_threshold'], v)
                await update.message.reply_text(f"??Hurst 誘쇨컧??蹂寃? {v}")

            elif choice == '31':
                # CHOP Threshold
                v = float(val)
                if v < 0 or v > 100:
                    await update.message.reply_text("??0 ~ 100 ?ъ씠??媛믪쓣 ?낅젰?섏꽭??")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'chop_threshold'], v)
                await update.message.reply_text(f"??CHOP 誘쇨컧??蹂寃? {v}")
            
            elif choice == '34':
                # CC Threshold & Length
                parts = val.replace(' ', '').split(',')
                if len(parts) == 1:
                    v = float(parts[0])
                    if v < 0.1 or v > 1.0:
                        await update.message.reply_text("???꾧퀎媛믪? 0.1 ~ 1.0 ?ъ씠?ъ빞 ?⑸땲??")
                        return SELECT
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'cc_threshold'], v)
                    await update.message.reply_text(f"??CC 誘쇨컧??蹂寃? {v}")
                elif len(parts) == 2:
                    v = float(parts[0])
                    l = int(parts[1])
                    if v < 0.1 or v > 1.0 or l < 5 or l > 100:
                        await update.message.reply_text("???꾧퀎媛?0.1~1.0), 湲곌컙(5~100)???뺤씤?섏꽭??")
                        return SELECT
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'cc_threshold'], v)
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'cc_length'], l)
                    await update.message.reply_text(f"??CC ?ㅼ젙: 誘쇨컧??{v}, 湲곌컙={l}")
                else:
                    await update.message.reply_text("???뺤떇: ?꾧퀎媛??먮뒗 ?꾧퀎媛?湲곌컙")
                    return SELECT

            elif choice == '35':
                # Dual Mode 蹂寃?
                if val == '1':
                    new_mode = 'standard'
                elif val == '2':
                    new_mode = 'scalping'
                else:
                    await update.message.reply_text("??1(Standard) ?먮뒗 2(Scalping)???낅젰?섏꽭??")
                    return SELECT
                
                await self.cfg.update_value(['dual_mode_engine', 'mode'], new_mode)
                await update.message.reply_text(f"??Dual Mode 蹂寃? {new_mode.upper()}")
                
                # 利됱떆 ?ъ큹湲고솕 ?몃━嫄?(poll_tick?먯꽌 媛먯??섏?留?紐낆떆??由ъ뀑)
                dm_engine = self.engines.get('dualmode')
                if dm_engine:
                    dm_engine._init_strategy()
            
            elif choice == '41':
                # 泥?궛 ??꾪봽?덉엫 蹂寃?
                valid_tf = ['1m', '2m', '3m', '4m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', '1d', '3d', '1w', '1M']
                if val not in valid_tf:
                    await update.message.reply_text(f"???좏슚?섏? ?딆? ??꾪봽?덉엫.\n?ъ슜 媛?? {', '.join(valid_tf)}")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'exit_timeframe'], val)
                # Signal ?붿쭊 罹먯떆 珥덇린??
                signal_engine = self.engines.get('signal')
                if signal_engine:
                    signal_engine.last_processed_exit_candle_ts = {}
                await update.message.reply_text(f"??泥?궛 ??꾪봽?덉엫 蹂寃? {val}")
            
            # 10~41 success message handled
            if choice not in ['10', '11', '12', '14', '15', '16', '17', '18', '20', '21', '22', '23', '26', '27', '28', '29', '30', '31', '33', '34', '35', '41']:
                await update.message.reply_text(f"???ㅼ젙 ?꾨즺: {val}")
            await self._restore_main_keyboard(update)
            await self.show_setup_menu(update)
            return SELECT
            
        except ValueError:
            await update.message.reply_text("???щ컮瑜??レ옄瑜??낅젰?섏꽭??")
            return SELECT
        except Exception as e:
            logger.error(f"Setup input error: {e}")
            await update.message.reply_text(f"???ㅻ쪟: {e}")
            return SELECT

    async def setup_symbol_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """?щ낵 蹂寃?泥섎━ - 1/2/3 ?⑥텞???먮뒗 吏곸젒 ?낅젰"""
        choice = update.message.text.strip().upper()
        
        # 1/2/3 踰덊샇濡??щ낵 留ㅽ븨 (?⑥텞??
        symbol_map = {
            '1': 'BTC/USDT',
            '2': 'ETH/USDT',
            '3': 'SOL/USDT'
        }
        
        # ?⑥텞???먮뒗 吏곸젒 ?낅젰 ?ъ슜
        if choice in symbol_map:
            symbol = symbol_map[choice]
        else:
            # 吏곸젒 ?낅젰??寃쎌슦 ?щ㎎ ?뺤씤
            symbol = choice
            if '/' not in symbol:
                symbol = f"{symbol}/USDT"
            
            # ?좏슚??寃??(媛꾨떒??Ticker 議고쉶)
            try:
                await asyncio.to_thread(self.exchange.fetch_ticker, symbol)
            except Exception:
                await update.message.reply_text(f"???좏슚?섏? ?딆? ?щ낵 ?먮뒗 嫄곕옒??誘몄??? {symbol}\n(?? BTC/USDT ?먮뒗 洹몃깷 BTC)")
                return SELECT
        
        try:
            eng = self.cfg.get('system_settings', {}).get('active_engine', CORE_ENGINE)
            if eng == 'shannon':
                await self.cfg.update_value(['shannon_engine', 'target_symbol'], symbol)
            elif eng == 'dualthrust':
                await self.cfg.update_value(['dual_thrust_engine', 'target_symbol'], symbol)
            elif eng == 'dualmode':
                await self.cfg.update_value(['dual_mode_engine', 'target_symbol'], symbol)
            elif eng == 'tema':
                await self.cfg.update_value(['tema_engine', 'target_symbol'], symbol)
            else:
                # Signal ?붿쭊: 硫붾돱?먯꽌 蹂寃???Watchlist瑜??대떦 ?щ낵濡?**?泥?* (湲곗〈 ?숈옉 ?좎?)
                # ?ㅼ쨷 媛먯떆瑜??먰븯硫?硫붾돱媛 ?꾨땲??梨꾪똿李쎌뿉??異붽??댁빞 ?⑥쓣 ?덈궡
                await self.cfg.update_value(['signal_engine', 'watchlist'], [symbol])
                await update.message.reply_text("?뱄툘 Signal ?붿쭊??媛먯떆 紐⑸줉?????щ낵濡?珥덇린?붾릺?덉뒿?덈떎.\n(異붽?瑜??먰븯?쒕㈃ 硫붾돱 諛뽰뿉???щ낵???낅젰?섏꽭??")
            
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
                signal_engine.last_candle_time = {} # Dict reset
                signal_engine.last_processed_candle_ts = {} 
                signal_engine.active_symbols.clear() # 湲곗〈 ?섎룞 紐⑸줉??珥덇린??(紐낇솗?깆쓣 ?꾪빐)
                signal_engine.active_symbols.add(symbol)
            
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
            
            await update.message.reply_text(f"???щ낵 蹂寃??꾨즺: {symbol}")
            await self._restore_main_keyboard(update)
            await self.show_setup_menu(update)
            return SELECT
            
        except Exception as e:
            logger.error(f"Symbol change error: {e}")
            await update.message.reply_text(f"???щ낵 蹂寃??ㅽ뙣: {e}")
            return SELECT

    async def setup_direction_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """留ㅻℓ 諛⑺뼢 ?좏깮 泥섎━"""
        text = update.message.text
        
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
            await update.message.reply_text(f"??留ㅻℓ 諛⑺뼢 蹂寃? {direction}")
        else:
            await update.message.reply_text("???좏슚?섏? ?딆? ?좏깮")
        
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
                await update.message.reply_text("??DualMode 愿??紐⑤뱢???놁뼱 ?ъ슜?????놁뒿?덈떎.")
            else:
                await self.cfg.update_value(['system_settings', 'active_engine'], mode)
                await self._switch_engine(mode)
                self.dashboard_msg_id = None
                await update.message.reply_text(f"???붿쭊 蹂寃??꾨즺: {mode.upper()}")
        else:
            await update.message.reply_text("?뱄툘 Core 紐⑤뱶?먯꽌??1(Signal)留??ъ슜?⑸땲??")
        
        await self._restore_main_keyboard(update)
        await self.show_setup_menu(update)
        return SELECT

    async def _restore_main_keyboard(self, update: Update):
        """硫붿씤 ?ㅻ낫??蹂듭썝"""
        kb = [
            [KeyboardButton("?슚 STOP"), KeyboardButton("??PAUSE"), KeyboardButton("??RESUME")],
            [KeyboardButton("/setup"), KeyboardButton("/status"), KeyboardButton("/log")]
        ]
        markup = ReplyKeyboardMarkup(kb, resize_keyboard=True)
        await update.message.reply_text("?벑 硫붿씤 硫붾돱", reply_markup=markup)

    async def _setup_telegram(self):
        kb = [
            [KeyboardButton("?슚 STOP"), KeyboardButton("??PAUSE"), KeyboardButton("??RESUME")],
            [KeyboardButton("/setup"), KeyboardButton("/status"), KeyboardButton("/log")]
        ]
        markup = ReplyKeyboardMarkup(kb, resize_keyboard=True)

        cid = self.cfg.get_chat_id()
        if cid:
            authorized_chat_filter = filters.Chat(chat_id=cid)
        else:
            logger.error("Invalid chat_id. Telegram handlers will ignore incoming messages.")
            authorized_chat_filter = filters.Chat(chat_id=-1)

        authorized_text_filter = filters.TEXT & ~filters.COMMAND & authorized_chat_filter

        async def start_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            await u.message.reply_text("?쨼 遊?以鍮??꾨즺", reply_markup=markup)

        async def strat_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            if not c.args:
                await u.message.reply_text("?ъ슜踰? /strat 踰덊샇\n1: Signal")
                return
            mode_map = {'1': CORE_ENGINE}
            arg = c.args[0]
            if arg not in mode_map:
                await u.message.reply_text("???섎せ???낅젰. 1(Signal)留??ъ슜 媛?ν빀?덈떎.")
                return
            mode = mode_map[arg]
            if mode == 'dualmode' and not DUAL_MODE_AVAILABLE:
                await u.message.reply_text("??DualMode 愿??紐⑤뱢???놁뼱 ?ъ슜?????놁뒿?덈떎.")
                return
            await self.cfg.update_value(['system_settings', 'active_engine'], mode)
            await self._switch_engine(mode)
            self.dashboard_msg_id = None
            await u.message.reply_text(f"???꾨왂 蹂寃? {mode.upper()}")

        async def log_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            logs = list(log_buffer)[-15:]
            if logs:
                await u.message.reply_text("\n".join(logs))
            else:
                await u.message.reply_text("?뱷 濡쒓렇媛 鍮꾩뼱?덉뒿?덈떎.")

        async def close_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            await self.emergency_stop()
            await u.message.reply_text("?썞 湲닿툒 ?뺤? ?꾨즺")

        async def stats_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            """?듦퀎 紐낅졊??"""
            daily_count, daily_pnl = self.db.get_daily_stats()
            weekly_count, weekly_pnl = self.db.get_weekly_stats()
            
            msg = f"""
?뱤 **留ㅻℓ ?듦퀎**

**?ㅻ뒛**
- 嫄곕옒: {daily_count}嫄?
- ?먯씡: ${daily_pnl:+.2f}

**7?쇨컙**
- 嫄곕옒: {weekly_count}嫄?
- ?먯씡: ${weekly_pnl:+.2f}
"""
            await u.message.reply_text(msg.strip(), parse_mode=ParseMode.MARKDOWN)

        async def help_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            msg = """
?뱴 **紐낅졊??紐⑸줉**

?뵩 **?ㅼ젙**
/setup - ?ㅼ젙 硫붾돱
/strat 1 - Signal ?꾨왂

?뱤 **?뺣낫**
/status - ??쒕낫??媛깆떊
/stats - 留ㅻℓ ?듦퀎
/log - 理쒓렐 濡쒓렇

?슚 **?쒖뼱**
/close - 湲닿툒 泥?궛
?슚 STOP - 湲닿툒 ?뺤?
??PAUSE - ?쇱떆?뺤?
??RESUME - ?ш컻
"""
            await u.message.reply_text(msg.strip(), parse_mode=ParseMode.MARKDOWN)

        # 鍮꾩긽 踰꾪듉 ?몃뱾??(理쒖슦??
        emergency_handler = MessageHandler(
            filters.Regex("STOP|PAUSE|RESUME|/status") & authorized_chat_filter,
            self.global_handler
        )
        self.tg_app.add_handler(emergency_handler, group=-1)
        
        # /start 紐낅졊???몃뱾??異붽?
        self.tg_app.add_handler(CommandHandler("start", start_cmd, filters=authorized_chat_filter))
        self.tg_app.add_handler(CommandHandler("strat", strat_cmd, filters=authorized_chat_filter))
        self.tg_app.add_handler(CommandHandler("log", log_cmd, filters=authorized_chat_filter))
        self.tg_app.add_handler(CommandHandler("close", close_cmd, filters=authorized_chat_filter))
        self.tg_app.add_handler(CommandHandler("stats", stats_cmd, filters=authorized_chat_filter))
        self.tg_app.add_handler(CommandHandler("help", help_cmd, filters=authorized_chat_filter))

        # ?ㅼ젙 ????몃뱾??
        conv = ConversationHandler(
            entry_points=[CommandHandler('setup', self.setup_entry, filters=authorized_chat_filter)],
            states={
                SELECT: [MessageHandler(authorized_text_filter, self.setup_select)],
                INPUT: [MessageHandler(authorized_text_filter, self.setup_input)],
                SYMBOL_INPUT: [MessageHandler(authorized_text_filter, self.setup_symbol_input)],
                DIRECTION_SELECT: [MessageHandler(authorized_text_filter, self.setup_direction_select)],
                ENGINE_SELECT: [MessageHandler(authorized_text_filter, self.setup_engine_select)],
                "R2_SELECT": [MessageHandler(authorized_text_filter, self.setup_r2_select)],
                "HURST_SELECT": [MessageHandler(authorized_text_filter, self.setup_hurst_select)],
                "CHOP_SELECT": [MessageHandler(authorized_text_filter, self.setup_chop_select)],
                "KALMAN_SELECT": [MessageHandler(authorized_text_filter, self.setup_kalman_select)]
            },
            fallbacks=[
                CommandHandler('setup', self.setup_entry, filters=authorized_chat_filter),
                emergency_handler
            ]
        )
        
        self.tg_app.add_handler(conv)
        
        # [New] ?섎룞 ?щ낵 ?낅젰 ?몃뱾??(?ㅼ젙 紐⑤뱶媛 ?꾨땺 ???숈옉)
        async def manual_symbol_handler(u: Update, c: ContextTypes.DEFAULT_TYPE):
            text = u.message.text.strip().upper()
            # 媛꾨떒???뺢퇋?앹쑝濡??щ낵 ?뺥깭?몄? ?뺤씤 (?뚰뙆踰?2~5湲???먮뒗 XXX/YYY ?뺤떇)
            import re
            if re.match(r'^[A-Z0-9]{2,10}(/[A-Z0-9]{2,10})?(:[A-Z0-9]+)?$', text):
                # /setup ??而ㅻ㎤?쒕뒗 ?쒖쇅
                if text.startswith('/'): return
                
                await self.handle_manual_symbol_input(u, text)

        self.tg_app.add_handler(MessageHandler(authorized_text_filter, manual_symbol_handler))

    async def setup_r2_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        if "1" in text:
            curr = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('r2_entry_enabled', True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'r2_entry_enabled'], not curr)
            await update.message.reply_text(f"??R2 Entry: {'ON' if not curr else 'OFF'}")
        elif "2" in text:
            curr = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('r2_exit_enabled', True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'r2_exit_enabled'], not curr)
            await update.message.reply_text(f"??R2 Exit: {'ON' if not curr else 'OFF'}")
        
        await self._restore_main_keyboard(update)
        await self.show_setup_menu(update)
        return SELECT

    async def setup_hurst_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        if "1" in text:
            curr = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('hurst_entry_enabled', True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'hurst_entry_enabled'], not curr)
            await update.message.reply_text(f"??Hurst Entry: {'ON' if not curr else 'OFF'}")
        elif "2" in text:
            curr = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('hurst_exit_enabled', True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'hurst_exit_enabled'], not curr)
            await update.message.reply_text(f"??Hurst Exit: {'ON' if not curr else 'OFF'}")
        
        await self._restore_main_keyboard(update)
        await self.show_setup_menu(update)
        return SELECT

    async def setup_chop_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        if "1" in text:
            curr = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('chop_entry_enabled', True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'chop_entry_enabled'], not curr)
            await update.message.reply_text(f"??Chop Entry: {'ON' if not curr else 'OFF'}")
        elif "2" in text:
            curr = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('chop_exit_enabled', True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'chop_exit_enabled'], not curr)
            await update.message.reply_text(f"??Chop Exit: {'ON' if not curr else 'OFF'}")
        
        await self._restore_main_keyboard(update)
        await self.show_setup_menu(update)
        return SELECT

    async def setup_kalman_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        if "1" in text:
            curr = self.cfg.get('signal_engine', {}).get('strategy_params', {}).get('kalman_filter', {}).get('entry_enabled', False)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'kalman_filter', 'entry_enabled'], not curr)
            await update.message.reply_text(f"??Kalman Entry: {'ON' if not curr else 'OFF'}")
        elif "2" in text:
            curr = self.cfg.get('signal_engine', {}).get('strategy_params', {}).get('kalman_filter', {}).get('exit_enabled', False)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'kalman_filter', 'exit_enabled'], not curr)
            await update.message.reply_text(f"??Kalman Exit: {'ON' if not curr else 'OFF'}")
            
        await self._restore_main_keyboard(update)
        await self.show_setup_menu(update)
        return SELECT

    # ---------------- Hourly Report ----------------
    async def _hourly_report_loop(self):
        """?쒓컙蹂?由ы룷??"""
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
                        
                        msg = f"""
??**?쒓컙蹂?由ы룷??* [{now.strftime('%H:%M')}]

?뮥 ?먯궛: ${d.get('total_equity', 0):.2f}
?뱢 ?ㅻ뒛 ?먯씡: ${daily_pnl:+.2f} ({daily_count}嫄?
?뱤 MMR: {d.get('mmr', 0):.2f}%
"""
                        await self.notify(msg.strip())
                
                await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"Hourly report error: {e}")
                await asyncio.sleep(60)

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
                sys_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
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
                    self.blink_state = not self.blink_state
                    blink = "●" if self.blink_state else "○"
                    pause_indicator = " [PAUSED]" if self.is_paused else ""
                    
                    all_data = self.status_data # Dict[symbol, status_dict]
                    
                    if not all_data:
                        # Fallback: ?뺣낫媛 ?섎굹???놁쓣 ??
                        eng = self.cfg.get('system_settings', {}).get('active_engine', 'LOADING').upper()
                        msg = f"{blink} **[{eng}] Dashboard**{pause_indicator} [{datetime.now().strftime('%H:%M:%S')}]\n???곗씠???섏떊 ?湲?以?.."
                    else:
                        msg = self._format_dashboard_message(all_data, blink, pause_indicator)


                    # 硫붿떆吏 ?꾩넚/?섏젙
                    if self.dashboard_msg_id:
                        try:
                            await self.tg_app.bot.edit_message_text(
                                chat_id=cid,
                                message_id=self.dashboard_msg_id,
                                text=msg,
                                parse_mode=ParseMode.MARKDOWN
                            )
                        except RetryAfter as e:
                            logger.warning(f"Flood Wait: Sleeping {e.retry_after}s")
                            await asyncio.sleep(e.retry_after)
                            # ?ъ떆??
                            try:
                                await self.tg_app.bot.edit_message_text(
                                    chat_id=cid,
                                    message_id=self.dashboard_msg_id,
                                    text=msg,
                                    parse_mode=ParseMode.MARKDOWN
                                )
                            except Exception:
                                self.dashboard_msg_id = None
                        except BadRequest as e:
                            if "Message is not modified" not in str(e):
                                logger.warning(f"Dashboard edit error: {e}")
                                # 硫붿떆吏 ??젣??寃쎌슦留?由ъ뀑 (?ㅻⅨ ?먮윭???좎?)
                                if "message to edit not found" in str(e).lower():
                                    self.dashboard_msg_id = None
                        except Exception as e:
                            logger.error(f"Dashboard error: {e}")
                            # ?먮윭 ?쒖뿉??msg_id ?좎? (??硫붿떆吏 ??＜ 諛⑹?)
                    else:
                        try:
                            m = await self.tg_app.bot.send_message(
                                chat_id=cid,
                                text=msg,
                                parse_mode=ParseMode.MARKDOWN
                            )
                            self.dashboard_msg_id = m.message_id
                        except RetryAfter as e:
                            logger.warning(f"Send Flood Wait: {e.retry_after}s")
                            await asyncio.sleep(min(e.retry_after, 60))  # 理쒕? 60珥??湲?
                        except Exception as e:
                            logger.error(f"Dashboard send error: {e}")
                            await asyncio.sleep(30)  # ?먮윭 ??30珥??湲????ъ떆??
                
                interval = self.cfg.get('system_settings', {}).get('monitoring_interval_seconds', 10)
                await asyncio.sleep(interval)
                
            except Exception as e:
                logger.error(f"Dashboard loop error: {e}")
                await asyncio.sleep(10)


    def _format_dashboard_message(self, all_data, blink, pause_indicator):
        """??쒕낫??硫붿떆吏 ?앹꽦 (Enhanced with Margin/Lev/TF info)"""
        try:
            eng = self.cfg.get('system_settings', {}).get('active_engine', 'unknown').upper()
            msg = f"{blink} **[{eng}] Dashboard**{pause_indicator} [{datetime.now().strftime('%H:%M:%S')}]\n\n"
            
            # 1. 怨듯넻 ?뺣낫 (泥?踰덉㎏ ?곗씠?곗뿉??異붿텧)
            first_symbol = list(all_data.keys())[0]
            d_first = all_data[first_symbol]
            
            msg += f"?뮥 **Asset Summary**\n"
            msg += f"Eq: `${d_first.get('total_equity', 0):.2f}` | Free: `${d_first.get('free_usdt', 0):.2f}`\n"
            msg += f"MMR: `{d_first.get('mmr', 0):.2f}%` | PnL: `${d_first.get('daily_pnl', 0):+.2f}`\n"
            msg += "?곣봺?곣봺?곣봺?곣봺?곣봺?곣봺?곣봺?곣봺?곣봺\n"

            # 2. 媛??щ낵蹂??뺣낫
            for symbol, d in all_data.items():
                cur_price = d.get('price', 0)
                pos_side = d.get('pos_side', 'NONE')
                
                # ?щ낵 ?ㅻ뜑
                # [New] Add Margin Mode & Leverage Info to Header
                lev = d.get('leverage', '?')
                mm = d.get('margin_mode', 'ISO') # Forced ISO
                mode_str = f"({mm} {lev}x)" if 'leverage' in d else ""
                
                p_emoji = "L" if pos_side == 'LONG' else "S" if pos_side == 'SHORT' else "-"
                msg += f"{p_emoji} **{symbol}** {mode_str} | {pos_side}\n"
                
                if pos_side != 'NONE':
                    pnl = d.get('pnl_pct', 0)
                    pnl_emoji = "?뱢" if pnl >= 0 else "?뱣"
                    msg += f"??PnL: `{d.get('pnl_usdt', 0):+.2f}` (`{pnl:+.2f}%`)\n"
                    # [New] Entry Price
                    msg += f"??Entry: `{d.get('entry_price', 0):.2f}` | Cur: `{cur_price:.2f}`\n"
                else:
                    msg += f"??Cur: `{cur_price:.2f}`\n"

                # ?붿쭊/?꾨왂蹂??곸꽭 ?꾪꽣
                d_eng = d.get('engine', '').upper()
                if d_eng == 'SIGNAL':
                    # [New] Timeframes Information
                    e_tf = d.get('entry_tf', '?')
                    x_tf = d.get('exit_tf', '?')
                    msg += f"??TF: In[{e_tf}] / Out[{x_tf}]\n"
                    
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

                    # ?꾪꽣 ?곹깭 (Entry / Exit 遺꾨━?쒖떆)
                    # Entry
                    e_r2 = get_st_text(entry_st, 'r2', 'r2_val', 'r2_pass', True)
                    e_h = get_st_text(entry_st, 'hurst', 'hurst_val', 'hurst_pass', True)
                    e_c = get_st_text(entry_st, 'chop', 'chop_val', 'chop_pass', True)
                    
                    # Exit
                    x_r2 = get_st_text(exit_st, 'r2', 'r2_val', 'r2_pass', False)
                    x_h = get_st_text(exit_st, 'hurst', 'hurst_val', 'hurst_pass', False)
                    x_c = get_st_text(exit_st, 'chop', 'chop_val', 'chop_pass', False)
                    x_cc = get_st_text(exit_st, 'cc', 'cc_val', 'cc_pass', False)
                    cc_val = exit_st.get('cc_val', 0.0)
                    
                    msg += f"??Filter(In): R2{e_r2} Hurst{e_h} Chop{e_c}\n"
                    msg += f"??Filter(Out): R2{x_r2} Hurst{x_h} Chop{x_c} CC{x_cc}({cc_val:.2f})\n"
                    
                    # ?꾨왂 ?꾩슜 ?뺣낫
                    active_strat = d.get('active_strategy', '')
                    if active_strat == 'MICROVBO':
                        vbo = d.get('vbo_breakout_level', {})
                        if vbo:
                            msg += f"??VBO: `L:{vbo.get('long',0):.1f}/S:{vbo.get('short',0):.1f}`\n"
                    elif active_strat == 'FRACTALFISHER':
                        msg += f"??FF: `H:{d.get('fisher_hurst',0):.2f}/F:{d.get('fisher_value',0):.2f}`\n"
                        if d.get('fisher_trailing_stop') and pos_side != 'NONE':
                            msg += f"??TS: `{d.get('fisher_trailing_stop', 0):.2f}`\n"

                elif d_eng == 'SHANNON':
                    msg += f"??Trend: `{d.get('trend', 'N/A')}` | EMA: `{d.get('ema_200', 0):.1f}`\n"
                    msg += f"??Grid: `{d.get('grid_orders', 0)}` | Diff: `{d.get('diff_pct', 0):.1f}%`\n"

                elif d_eng == 'DUALTHRUST':
                    msg += f"??Triggers: `L:{d.get('long_trigger',0):.1f}/S:{d.get('short_trigger',0):.1f}`\n"

                elif d_eng == 'DUALMODE':
                    msg += f"??Mode: `{d.get('dm_mode', 'N/A')}` | TF: `{d.get('dm_tf')}`\n"

                msg += "\n" # 肄붿씤 媛?媛꾧꺽
            
            return msg
        except Exception as e:
            logger.error(f"Dashboard format error: {e}")
            return "????쒕낫???щ㎎ ?ㅻ쪟"

    async def emergency_stop(self):
        """湲닿툒 ?뺤? - 紐⑤뱺 ?ㅽ뵂 ?ъ???泥?궛"""
        logger.warning("?슚 Emergency stop triggered")
        
        if self.active_engine:
            self.active_engine.stop()
        
        self.is_paused = True
        
        try:
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
                await self.notify("?뱄툘 泥?궛???ㅽ뵂 ?ъ??섏씠 ?놁뒿?덈떎. (遊??뺤???")
                return

            await self.notify(f"?슚 **湲닿툒 ?뺤? ?ㅽ뻾**\n諛쒓껄???ъ??? {len(open_positions)}媛?-> ?쇨큵 泥?궛 ?쒖옉")

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
                    await self.notify(f"?뵏 **{sym}** 泥?궛 ?꾨즺\nPnL: ${pnl:+.2f}")
                    
                except Exception as e:
                    logger.error(f"Failed to close {sym}: {e}")
                    await self.notify(f"??{sym} 泥?궛 ?ㅽ뙣: {e}")
            
            await self.notify("?뢾 湲닿툒 ?뺤? ?덉감 ?꾨즺")
                    
        except Exception as e:
            logger.error(f"Emergency stop error: {e}")
            await self.notify(f"??湲닿툒 ?뺤? 以??ㅻ쪟: {e}")

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
    try:
        controller = MainController()
        asyncio.run(controller.run())
    except KeyboardInterrupt:
        print("\n?몝 Bye")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        traceback.print_exc()
    finally:
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
