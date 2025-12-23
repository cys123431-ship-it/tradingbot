# emas_improved.py
# [순수 폴링 버전]
# 1. WebSocket 제거 → 안정적인 폴링 방식으로 전환
# 2. Signal/Shannon 엔진 모두 폴링 지원
# 3. 모든 Critical 이슈 수정: chat_id 타입, 예외 로깅, async 핸들러
# 4. 미구현 기능 추가: Grid Trading, Daily Loss Limit, Hourly Report, MMR Alert

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
    logging.warning("⚠️ hurst 패키지 없음. FractalFisher 전략 사용 불가. 설치: pip install hurst")
from datetime import datetime, timezone, timedelta
from collections import deque
from dual_mode_fractal_strategy import DualModeFractalStrategy

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.error import BadRequest, TimedOut, RetryAfter
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, 
    MessageHandler, filters, ConversationHandler, CallbackQueryHandler
)

# ---------------------------------------------------------
# 0. 로깅 및 유틸리티
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
        logging.FileHandler('trading_bot.log', encoding='utf-8')  # 로그 파일 저장
    ]
)
logger = logging.getLogger(__name__)

# 대화형 상태
SELECT, INPUT, SYMBOL_INPUT, DIRECTION_SELECT, ENGINE_SELECT = range(5)

# ---------------------------------------------------------
# 1. 설정 및 DB 관리
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
                # 누락된 필드 자동 추가
                self._ensure_defaults()
                return True
            except Exception as e:
                logger.error(f"Config load error: {e}")
        self.create_default_config()
        return True

    def _ensure_defaults(self):
        """누락된 필드 자동 추가"""
        defaults = {
            'system_settings': {
                'active_engine': 'shannon', 
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
                    'leverage': 20, 
                    'timeframe': '15m',
                    'entry_timeframe': '8h',
                    'exit_timeframe': '4h',
                    'risk_per_trade_pct': 50.0,
                    'target_roe_pct': 20.0,
                    'stop_loss_pct': 10.0,
                    'daily_loss_limit': 5000.0,
                    'r2_entry_enabled': True,
                    'r2_exit_enabled': True,
                    'r2_threshold': 0.25,
                    'hurst_entry_enabled': True,
                    'hurst_exit_enabled': True,
                    'hurst_threshold': 0.55,
                    'chop_entry_enabled': True,
                    'chop_exit_enabled': True,
                    'chop_threshold': 50.0
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
                "active_engine": "shannon",
                "trade_direction": "both",
                "show_dashboard": True,
                "monitoring_interval_seconds": 3
            },
            "signal_engine": {
                "watchlist": ["BTC/USDT"],
                "common_settings": {
                    "leverage": 20, "timeframe": "15m", 
                    "risk_per_trade_pct": 50.0,
                    "target_roe_pct": 20.0, "stop_loss_pct": 10.0,
                    "daily_loss_limit": 5000.0
                },
                "strategy_params": {
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
        """chat_id를 정수로 안전하게 반환"""
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
# 2. 엔진 (Signal / Shannon)
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
        self.POSITION_CACHE_TTL = 2.0  # 2초 캐시

    def start(self):
        self.running = True
        logger.info(f"🚀 {self.__class__.__name__} started")

    def stop(self):
        self.running = False
        logger.info(f"⏹ {self.__class__.__name__} stopped")

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

    async def ensure_market_settings(self, symbol):
        try:
            await asyncio.to_thread(self.exchange.set_position_mode, hedged=False, symbol=symbol)
        except Exception as e:
            logger.warning(f"Position mode setting: {e}")
        
        try:
            await asyncio.to_thread(self.exchange.set_margin_mode, 'ISOLATED', symbol)
        except Exception as e:
            logger.warning(f"Margin mode setting: {e}")
        
        try:
            eng = self.cfg.get('system_settings', {}).get('active_engine', 'shannon')
            if eng == 'shannon':
                lev = self.cfg.get('shannon_engine', {}).get('leverage', 5)
            elif eng == 'dualthrust':
                lev = self.cfg.get('dual_thrust_engine', {}).get('leverage', 5)
            else:
                lev = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('leverage', 20)
            
            await asyncio.to_thread(self.exchange.set_leverage, lev, symbol)
            logger.info(f"✅ {symbol} Settings: ISOLATED / {lev}x")
        except Exception as e:
            logger.error(f"Leverage setting error: {e}")

    async def get_server_position(self, symbol, use_cache=True):
        """포지션 조회 (캐시 적용)"""
        now = time.time()
        if use_cache and self.position_cache and (now - self.position_cache_time) < self.POSITION_CACHE_TTL:
            return self.position_cache
        
        try:
            positions = await asyncio.to_thread(self.exchange.fetch_positions, [symbol])
            
            # 심볼 정규화 (BTC/USDT -> BTCUSDT)
            base_symbol = symbol.replace('/', '')
            
            for p in positions:
                pos_symbol = p.get('symbol', '')
                # 다양한 형식 매칭: BTC/USDT, BTC/USDT:USDT, BTCUSDT
                pos_base = pos_symbol.replace('/', '').replace(':USDT', '')
                
                if pos_base == base_symbol or pos_symbol == symbol or pos_symbol == f"{symbol}:USDT":
                    if abs(float(p.get('contracts', 0))) > 0:
                        self.position_cache = p
                        self.position_cache_time = now
                        logger.debug(f"Position found: {p['symbol']} contracts={p['contracts']}")
                        return p
            
            self.position_cache = None
            self.position_cache_time = now
            return None
        except Exception as e:
            logger.error(f"Position fetch error: {e}")
            return self.position_cache  # 에러 시 캐시 반환

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
        """일일 손실 한도 체크 (미실현 손익 포함)"""
        _, daily_pnl = self.db.get_daily_stats()
        eng = self.cfg.get('system_settings', {}).get('active_engine', 'shannon')
        
        # 미실현 손익도 포함
        unrealized_pnl = self.ctrl.status_data.get('pnl_usdt', 0) if self.ctrl.status_data else 0
        total_daily_pnl = daily_pnl + unrealized_pnl
        
        if eng == 'shannon':
            limit = self.cfg.get('shannon_engine', {}).get('daily_loss_limit', 5000)
        else:
            limit = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('daily_loss_limit', 5000)
        
        if total_daily_pnl < -limit:
            logger.warning(f"⚠️ Daily loss limit reached: {total_daily_pnl:.2f} (realized: {daily_pnl:.2f}, unrealized: {unrealized_pnl:.2f}) / Limit: -{limit}")
            # 포지션이 있으면 청산
            if self.ctrl.status_data.get('pos_side') != 'NONE':
                symbol = self.ctrl.status_data.get('symbol')
                if symbol:
                    await self.ctrl.notify(f"🛑 일일 손실 한도 도달! 포지션 청산 시작...")
                    # 엔진에서 청산 처리
                    if hasattr(self, 'exit_position'):
                        await self.exit_position(symbol, "DailyLossLimit")
            return True
        return False

    async def check_mmr_alert(self, mmr):
        """MMR 경고 체크 (쿨다운 적용)"""
        max_mmr = self.cfg.get('shannon_engine', {}).get('risk_monitor', {}).get('max_mmr_alert_pct', 25.0)
        
        # 쿨다운: 5분 동안 중복 알림 방지
        now = time.time()
        if not hasattr(self, '_last_mmr_alert_time'):
            self._last_mmr_alert_time = 0
        
        if mmr >= max_mmr:
            if now - self._last_mmr_alert_time > 300:  # 5분
                self._last_mmr_alert_time = now
                await self.ctrl.notify(f"⚠️ **MMR 경고!** 현재 {mmr:.2f}% (한도: {max_mmr}%)")
            return True
        return False


class TemaEngine(BaseEngine):
    def __init__(self, controller):
        super().__init__(controller)
        self.last_candle_time = 0
        self.consecutive_errors = 0
        
        # 기본 기술적 지표 캐시
        self.ema1 = None
        self.ema2 = None
        self.ema3 = None
    
    def start(self):
        super().start()
        logger.info(f"🚀 [TEMA] Engine started")
        
    async def poll_tick(self):
        if not self.running: return
        
        try:
            # 1. 설정 로드 (공통 설정 사용)
            cfg = self.cfg.get('tema_engine', {})
            common_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
            
            symbol = cfg.get('target_symbol', 'BTC/USDT')
            tf = cfg.get('timeframe', '5m')
            
            # 2. 캔들 데이터 조회
            ohlcv = await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, tf, limit=100)
            if not ohlcv or len(ohlcv) < 50:
                return
                
            last_closed = ohlcv[-2]
            current_ts = int(last_closed[0])
            current_close = float(last_closed[4])
            
            # 3. 새로운 캔들 마감 시 분석
            if current_ts > self.last_candle_time:
                logger.info(f"🕯️ [TEMA {tf}] {symbol} New Candle: close={current_close}")
                self.last_candle_time = current_ts
                
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                
                # 지표 계산
                df = self._calculate_indicators(df, cfg)
                
                # 신호 확인
                signal, reason = self._check_entry_conditions(df, cfg)
                
                # 포지션 조회
                pos = await self.get_server_position(symbol, use_cache=False)
                pos_side = 'long' if pos and float(pos['contracts']) > 0 and float(pos['side'] if 'side' in pos else 1) > 0 else \
                           'short' if pos and float(pos['contracts']) > 0 and float(pos['side'] if 'side' in pos else 1) < 0 else 'none'
                
                # 진입 감지
                if signal and pos_side == 'none':
                    logger.info(f"🚀 TEMA Signal Detected: {signal.upper()} ({reason})")
                    current_price = float(ohlcv[-1][4])
                    await self.entry(symbol, signal, current_price, common_cfg)
                    
                # 청산 감지 (포지션이 있을 때만)
                elif pos_side != 'none':
                     exit_signal, exit_reason = self._check_exit_conditions(df, pos_side, cfg)
                     if exit_signal:
                         logger.info(f"👋 TEMA Exit Signal: {exit_reason}")
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
        # 전략: SampleStrategy.py 로직 구현
        # Long: RSI > 30 & TEMA < BB_Mid & TEMA Rising
        # Short: RSI > 70 & TEMA > BB_Mid & TEMA Falling
        
        try:
            last = df.iloc[-2] # 직전 확정 봉
            prev = df.iloc[-3] # 그 전 봉 (추세 확인용)
            
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
        # 전략: SampleStrategy.py 로직 구현
        # Exit Long: RSI > 70 & TEMA > BB_Mid & TEMA Falling (과매수 + 꺾임)
        # Exit Short: RSI < 30 & TEMA < BB_Mid & TEMA Rising (과매도 + 반등)
        
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
            # 1. 자산 확인
            total, free, _ = await self.get_balance_info()
            if total <= 0: return

            # 2. 투자 비중 (Risk %) - 공통 설정 사용
            risk_pct = common_cfg.get('risk_per_trade_pct', 50.0)
            leverage = common_cfg.get('leverage', 5)
            
            # USDT 투입 금액 계산
            invest_amount = (total * (risk_pct / 100.0)) * leverage
            
            # 수량 계산
            quantity = invest_amount / price
            amount_str = self.safe_amount(symbol, quantity)
            price_str = self.safe_price(symbol, price) # Limit 주문용 (현재가)
            
            logger.info(f"💰 TEMA Entry: {side.upper()} {symbol} Qty={amount_str} Price={price_str} (Lev {leverage}x)")
            
            # 3. 주문 전송
            params = {'leverage': leverage}
            
            if side == 'long':
                order = await asyncio.to_thread(self.exchange.create_market_buy_order, symbol, float(amount_str), params)
            else:
                order = await asyncio.to_thread(self.exchange.create_market_sell_order, symbol, float(amount_str), params)
            
            await self.ctrl.notify(f"🚀 **TEMA 진입**: {symbol} {side.upper()}\n가격: {price}\n수량: {amount_str}")
            
            # 4. TP/SL 설정 (공통 설정 사용)
            tp_sl_enabled = common_cfg.get('tp_sl_enabled', False)
            if tp_sl_enabled:
                roe_target = common_cfg.get('target_roe_pct', 20.0) / 100.0
                stop_loss = common_cfg.get('stop_loss_pct', 10.0) / 100.0
                
                # 주문 체결가 기준 TP/SL 계산
                entry_price = float(order['average']) if order.get('average') else price
                
                if side == 'long':
                    tp_price = entry_price * (1 + roe_target/leverage)
                    sl_price = entry_price * (1 - stop_loss/leverage)
                else:
                    tp_price = entry_price * (1 - roe_target/leverage)
                    sl_price = entry_price * (1 + stop_loss/leverage)
                    
                # 바이낸스 기준 TP/SL 주문 (STOP_MARKET / TAKE_PROFIT_MARKET)
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
                    
                    logger.info(f"✅ TP/SL Order Placed: TP={tp_price:.4f}, SL={sl_price:.4f}")
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

            amount = float(pos['contracts'])
            side = 'sell' if float(pos['contracts']) > 0 else 'buy' # 포지션 반대 매매
            
            if amount > 0:
                await asyncio.to_thread(self.exchange.create_market_order, symbol, side, amount)
                await self.ctrl.notify(f"👋 **TEMA 청산**: {symbol} ({reason})")
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
        self.scanner_active_symbol = None # 현재 스캐너가 잡고 있는 코인 (Serial Hunter Mode)
        
        # Kalman 상태 캐시 (대시보드 표시용)
        self.kalman_states = {} # {symbol: {'velocity': float, 'direction': str}}
        
        # Strategy states (Dict[symbol, value] or just cache keying)
        # MicroVBO 상태 캐시
        self.vbo_states = {} # {symbol: {entry_price, entry_atr, breakout_level}}
        
        # FractalFisher 상태 캐시
        self.fisher_states = {} # {symbol: {hurst, value, prev_value, entry_price, entry_atr, trailing_stop}}
        
        # [New] Filter Status Persistence (Dashboard)
        self.last_entry_filter_status = {} # symbol -> {r2_val, ...}
        self.last_exit_filter_status = {}  # symbol -> {r2_val, ...}

    def start(self):
        super().start()
        self.last_activity = time.time()
        # 초기화
        config_watchlist = self.cfg.get('signal_engine', {}).get('watchlist', [])
        for s in config_watchlist:
            self.active_symbols.add(s)
        logger.info(f"🚀 [Signal] Engine started (Multi-Symbol Mode). Watching: {self.active_symbols}")

    def _get_exit_timeframe(self):
        """청산용 타임프레임 (User Defined)"""
        cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
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
        [순수 폴링] 메인 폴링 함수 (Multi-Symbol)
        Mode 1: Scanner ON -> Serial Hunter (One at a time, ignoring Watchlist)
        Mode 2: Scanner OFF -> Watchlist + Manual Added
        """
        if not self.running:
            return
        
        try:
            self.last_activity = time.time()
            
            # Check Scanner Setting
            scanner_enabled = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('scanner_enabled', True)
            
            target_symbols = set()
            
            if scanner_enabled:
                # === [Mode 1: Serial Scanner] ===
                # 1. 만약 이미 잡고 있는 스캐너 코인이 있다면? -> 그것만 관리
                if self.scanner_active_symbol:
                    # 포지션이 아직 살아있는지 확인
                    pos = await self.get_server_position(self.scanner_active_symbol, use_cache=False)
                    if pos:
                        # 포지션 유지 중 -> 이놈만 집중 케어 (스캔 중지)
                        target_symbols.add(self.scanner_active_symbol)
                    else:
                        # 포지션 없음 (청산됨) -> 스캐너 변수 초기화 & 다시 스캔 모드 진입
                        logger.info(f"♻️ Scanner trade completed for {self.scanner_active_symbol}. Resuming scan.")
                        self.scanner_active_symbol = None
                        # 바로 스캔 로직으로 넘어감
                
                # 2. 잡고 있는게 없다면? -> 스캔 실행
                if not self.scanner_active_symbol:
                    # 스캔 주기는 30분이나, "잡고 있는게 없을 때"는 즉시/빈번하게 스캔할지 결정
                    # 여기서는 쿨타임(5분) 정도 주거나, 아니면 바로 진입 시도. 
                    # user: "청산 후 다시 스캔시" -> 즉시 스캔이 맞음. 
                    # 다만 너무 잦은 API 호출 방지를 위해 last_volume_scan 체크는 유지하되 쿨타임 짧게
                    
                    # 쿨타임 로직: 포지션 청산 직후라면 바로 스캔해야 함.
                    # last_volume_scan을 0으로 만들어주는 로직이 필요하거나, 여기서 조건 완화
                    if time.time() - self.last_volume_scan > 60: # 1분 쿨타임 (빈번한 스캔 방지)
                        await self.scan_and_trade_high_volume()
                        self.last_volume_scan = time.time()
                    
                    # 스캔 결과 진입했으면 target_symbols에 추가됨 (아래 scan 함수에서 설정)
                    if self.scanner_active_symbol:
                        target_symbols.add(self.scanner_active_symbol)
            else:
                # === [Mode 2: Manual / Watchlist] ===
                # Config Watchlist
                config_symbols = set(self.cfg.get('signal_engine', {}).get('watchlist', []))
                # Current Positions (Legacy/Manual)
                positions = await asyncio.to_thread(self.exchange.fetch_positions)
                active_position_symbols = set()
                for p in positions:
                    if float(p.get('contracts', 0)) > 0:
                        sym = p['symbol'].replace(':USDT', '')
                        active_position_symbols.add(sym)
                
                # Merge: Config + Chat Manual + Positions
                target_symbols = self.active_symbols | config_symbols | active_position_symbols

            if not target_symbols:
                return

            # Configs
            cfg = self.cfg.get('signal_engine', {})
            common_cfg = cfg.get('common_settings', {})
            # Entry timeframe (진입용)
            entry_tf = common_cfg.get('entry_timeframe', common_cfg.get('timeframe', '8h'))
            
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
        """개별 심볼 폴링 로직"""
        # Defensive Check: 상태 변수가 오염되었을 경우 복구
        if not isinstance(self.last_processed_candle_ts, dict):
            logger.error(f"⚠️ State corrupted: last_processed_candle_ts is {type(self.last_processed_candle_ts)}, resetting.")
            self.last_processed_candle_ts = {}
        if not isinstance(self.last_processed_exit_candle_ts, dict):
            logger.error(f"⚠️ State corrupted: last_processed_exit_candle_ts is {type(self.last_processed_exit_candle_ts)}, resetting.")
            self.last_processed_exit_candle_ts = {}
        if not isinstance(self.last_candle_time, dict):
            logger.error(f"⚠️ State corrupted: last_candle_time is {type(self.last_candle_time)}, resetting.")
            self.last_candle_time = {}
        if not isinstance(self.fisher_states, dict):
            logger.error(f"⚠️ State corrupted: fisher_states is {type(self.fisher_states)}, resetting.")
            self.fisher_states = {}
        if not isinstance(self.vbo_states, dict):
            logger.error(f"⚠️ State corrupted: vbo_states is {type(self.vbo_states)}, resetting.")
            self.vbo_states = {}
            
        try:
            # 1. OHLCV (Primary) - Basic monitoring
            ohlcv_p = await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, primary_tf, limit=5)
            if not ohlcv_p or len(ohlcv_p) < 3:
                return
            
            current_price = float(ohlcv_p[-1][4])
            
            # Update Status
            await self.check_status(symbol, current_price)
            
            # 2. Check Primary TF (Entry Logic)
            last_closed_p = ohlcv_p[-2]
            ts_p = last_closed_p[0]
            
            # 심볼별 상태 초기화
            if symbol not in self.last_processed_candle_ts:
                self.last_processed_candle_ts[symbol] = 0
            
            if ts_p > self.last_processed_candle_ts[symbol]:
                logger.info(f"🕯️ [Primary {primary_tf}] {symbol} New Candle: {ts_p} close={last_closed_p[4]}")
                self.last_processed_candle_ts[symbol] = ts_p
                
                k_p = {
                    't': ts_p, 'o': str(last_closed_p[1]), 'h': str(last_closed_p[2]),
                    'l': str(last_closed_p[3]), 'c': str(last_closed_p[4]), 'v': str(last_closed_p[5])
                }
                await self.process_primary_candle(symbol, k_p)
                
            # 3. Check Exit TF (Exit Logic)
            pos_side = self.ctrl.status_data.get('pos_side', 'NONE')
            
            strategy_params = cfg.get('strategy_params', {})
            entry_mode = strategy_params.get('entry_mode', 'cross').lower()
            active_strategy = strategy_params.get('active_strategy', 'sma').lower()
            
            # Cross/Position 모드에서만 Secondary TF 청산 로직 사용
            if (pos_side != 'NONE') and (active_strategy in ['sma', 'hma']) and (entry_mode in ['cross', 'position']):
                exit_tf = self._get_exit_timeframe()
                
                ohlcv_e = await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, exit_tf, limit=5)
                if ohlcv_e and len(ohlcv_e) >= 3:
                    last_closed_e = ohlcv_e[-2]
                    ts_e = last_closed_e[0]
                    
                    if symbol not in self.last_processed_exit_candle_ts:
                        self.last_processed_exit_candle_ts[symbol] = 0

                    if ts_e > self.last_processed_exit_candle_ts[symbol]:
                        logger.info(f"🕯️ [Exit {exit_tf}] {symbol} New Candle: {ts_e} close={last_closed_e[4]}")
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
            logger.info("📡 Scanning high volume markets (>200M USDT)...")
            tickers = await asyncio.to_thread(self.exchange.fetch_tickers)
            
            # 1. 1차 필터: 거래금액 200M 이상
            candidates = []
            for symbol, data in tickers.items():
                if '/USDT' in symbol:
                    quote_vol = float(data.get('quoteVolume', 0) or 0)
                    percentage = float(data.get('percentage', 0) or 0)
                    if quote_vol >= 200_000_000:
                        candidates.append({'symbol': symbol, 'vol': quote_vol, 'pct': percentage})
            
            if not candidates:
                logger.info("scanner: No coins > 200M vol found.")
                return

            # 2. 2차 필터: 상승률 상위 5개
            candidates.sort(key=lambda x: x['pct'], reverse=True)
            top_5_risers = candidates[:5]
            
            # Debug Log: Top 5 Risers
            log_msg = "📊 [Scanner Debug] Top 5 Risers:\n"
            for idx, c in enumerate(top_5_risers):
                log_msg += f"  {idx+1}. {c['symbol']}: Rise={c['pct']:.2f}%, Vol={c['vol']/1_000_000:.1f}M\n"
            logger.info(log_msg.strip())

            if not top_5_risers:
                 logger.info("scanner: No candidates after filtering.")
                 return

            # 3. 3차 필터: 그 중 거래대금 1등
            top_5_risers.sort(key=lambda x: x['vol'], reverse=True)
            target_coin = top_5_risers[0]
            
            symbol = target_coin['symbol']
            logger.info(f"🎯 Scanner Target Selected: {symbol} (Vol: {target_coin['vol']/1_000_000:.1f}M, Rise: {target_coin['pct']:.2f}%)")

            # 4. 전략 실행
            if self.ctrl.is_paused: return
            
            try:
                cfg = self.cfg.get('signal_engine', {})
                primary_tf = cfg.get('common_settings', {}).get('timeframe', '15m')
                strategy_params = cfg.get('strategy_params', {})
                
                ohlcv = await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, primary_tf, limit=300)
                if not ohlcv: return
                
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                
                # 강제로 entry_mode='cross'
                scan_params = strategy_params.copy()
                scan_params['entry_mode'] = 'cross' 
                active_strategy = scan_params.get('active_strategy', 'sma').lower()
                if active_strategy not in ['sma', 'hma']:
                    active_strategy = 'sma' # Safety fallback
                
                sig, _, _, _, _, _ = await self._calculate_strategy_signal(symbol, df, scan_params, active_strategy)
                
                # 포지션 확인 (서버)
                pos = await self.get_server_position(symbol, use_cache=False)
                
                if not pos and sig:
                    logger.info(f"🚀 Scanner Locking In: {symbol} [{sig.upper()}] detected!")
                    current_price = float(ohlcv[-1][4])
                    await self.entry(symbol, sig, current_price)
                    
                    # [Serial Mode] Lock this symbol
                    self.scanner_active_symbol = symbol
                    
                    # [Important] Mark candle as processed to prevent double-entry by poll_tick
                    # poll_tick이 곧바로 이 심볼을 폴링할 텐데, 이미 진입했음을 알리기 위해 타임스탬프 갱신
                    current_ts = int(ohlcv[-1][0])
                    self.last_processed_candle_ts[symbol] = current_ts
                    self.last_candle_time[symbol] = current_ts
                    self.last_candle_success[symbol] = True
                    # active_symbols에 추가할 필요 없음 (poll_tick에서 scanner_active_symbol 관리)
                else:
                    logger.info(f"👀 Scanner Checked {symbol}: Waiting for signal (Sig={sig}, Pos={pos['side'] if pos else 'None'})")

            except Exception as e:
                logger.error(f"Scanner strategy check failed for {symbol}: {e}")
                
        except Exception as e:
            logger.error(f"Volume scanner error: {e}")

    def _timeframe_to_ms(self, tf):
        """타임프레임을 밀리초로 변환"""
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
            
            # 전략 상태 가져오기
            strategy_params = self.cfg.get('signal_engine', {}).get('strategy_params', {})
            kalman_enabled = strategy_params.get('kalman_filter', {}).get('enabled', False)
            active_strategy = strategy_params.get('active_strategy', 'sma').upper()
            entry_mode = strategy_params.get('entry_mode', 'cross').upper()
            
            # Cross/Position 모드에서는 Kalman 필터가 로직상 강제 사용되므로 상태 표시도 활성화 (SMA/HMA)
            if active_strategy in ['SMA', 'HMA'] and entry_mode in ['CROSS', 'POSITION']:
                kalman_enabled = True
            
            # MicroVBO State
            vbo_state = self.vbo_states.get(symbol, {})
            # FractalFisher State
            fisher_state = self.fisher_states.get(symbol, {})
            
            self.ctrl.status_data = {
                'engine': 'Signal', 'symbol': symbol, 'price': price,
                'total_equity': total, 'free_usdt': free, 'mmr': mmr,
                'daily_count': count, 'daily_pnl': daily_pnl,
                'pos_side': pos['side'].upper() if pos else 'NONE',
                'entry_price': float(pos['entryPrice']) if pos else 0.0,
                'coin_amt': float(pos['contracts']) if pos else 0.0,
                'pnl_pct': float(pos['percentage']) if pos else 0.0,
                'pnl_usdt': float(pos['unrealizedPnl']) if pos else 0.0,
                # Signal 전용 필드
                'kalman_enabled': kalman_enabled,
                'kalman_velocity': self.kalman_states.get(symbol, {}).get('velocity', 0.0),
                'kalman_direction': self.kalman_states.get(symbol, {}).get('direction'),
                'active_strategy': active_strategy,
                'entry_mode': entry_mode,
                # MicroVBO 전용 필드
                'vbo_breakout_level': vbo_state.get('breakout_level'),
                'vbo_entry_atr': vbo_state.get('entry_atr'),
                # FractalFisher 전용 필드
                'fisher_hurst': fisher_state.get('hurst'),
                'fisher_value': fisher_state.get('value'),
                'fisher_trailing_stop': fisher_state.get('trailing_stop'),
                'fisher_entry_atr': fisher_state.get('entry_atr')
            }
            
            # [New] Merge Last Filter Status (Persistence)
            # Retrieve specific symbol status or empty dict
            entry_status = self.last_entry_filter_status.get(symbol, {})
            exit_status = self.last_exit_filter_status.get(symbol, {})
            
            self.ctrl.status_data['entry_filters'] = entry_status
            self.ctrl.status_data['exit_filters'] = exit_status
            
            # [New] Real-time Filter Config (Read from current config)
            comm_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
            self.ctrl.status_data['filter_config'] = {
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
                # Add Kalman Config for display convenience
                'kalman': {
                    'en_entry': strategy_params.get('kalman_filter', {}).get('entry_enabled', False),
                    'en_exit': strategy_params.get('kalman_filter', {}).get('exit_enabled', False)
                }
            }

            # MMR 경고 체크
            await self.check_mmr_alert(mmr)
            
            if self.ctrl.is_paused or not pos:
                return
            
            cfg = comm_cfg # Alias for below use
            
            # ===== MicroVBO: 거래소 주문 TP/SL 사용 =====
            # 거래소에서 직접 TP/SL 체결됨 (오픈오더에 표시됨)
            if active_strategy == 'MICROVBO':
                if vbo_state.get('entry_price') and vbo_state.get('entry_atr') and pos:
                    pnl = float(pos['percentage'])
                    logger.debug(f"[MicroVBO] Position PnL: {pnl:+.2f}% - TP/SL via exchange orders")
                return  # MicroVBO는 거래소 주문 TP/SL 사용
            
            # ===== FractalFisher 전용 ATR Trailing Stop =====
            if active_strategy == 'FRACTALFISHER':
                entry_price = fisher_state.get('entry_price')
                entry_atr = fisher_state.get('entry_atr')
                trailing_stop = fisher_state.get('trailing_stop')
                
                if entry_price and entry_atr:
                    ff_cfg = strategy_params.get('FractalFisher', {})
                    trailing_mult = ff_cfg.get('atr_trailing_multiplier', 2.0)
                    
                    if pos['side'] == 'long':
                        # Trailing Stop 계산 (항상 올리기만)
                        new_stop = price - (entry_atr * trailing_mult)
                        if trailing_stop is None:
                            trailing_stop = new_stop
                        else:
                            trailing_stop = max(trailing_stop, new_stop)
                        
                        # 상태 업데이트
                        if symbol not in self.fisher_states: self.fisher_states[symbol] = {}
                        self.fisher_states[symbol]['trailing_stop'] = trailing_stop
                        
                        if price <= trailing_stop:
                            logger.info(f"🛑 [FractalFisher] Trailing Stop Hit: {price:.2f} <= {trailing_stop:.2f}")
                            await self.exit_position(symbol, "Fisher_TrailingStop")
                            self.fisher_states[symbol] = {} # state reset
                    
                    elif pos['side'] == 'short':
                        # Trailing Stop 계산 (항상 내리기만)
                        new_stop = price + (entry_atr * trailing_mult)
                        if trailing_stop is None:
                            trailing_stop = new_stop
                        else:
                            trailing_stop = min(trailing_stop, new_stop)
                        
                        # 상태 업데이트
                        if symbol not in self.fisher_states: self.fisher_states[symbol] = {}
                        self.fisher_states[symbol]['trailing_stop'] = trailing_stop
                        
                        if price >= trailing_stop:
                            logger.info(f"🛑 [FractalFisher] Trailing Stop Hit: {price:.2f} >= {trailing_stop:.2f}")
                            await self.exit_position(symbol, "Fisher_TrailingStop")
                            self.fisher_states[symbol] = {} # state reset
                    
                return  # FractalFisher는 Trailing Stop만 사용
            
            # ===== SMA/HMA/MicroVBO: 거래소 주문 TP/SL 사용 =====
            # 거래소에서 직접 TP/SL 체결됨 (오픈오더에 표시됨)
            # 소프트웨어 모니터링 불필요
            # (백업용 로그만 남김)
            if pos:
                pnl = float(pos['percentage'])
                logger.debug(f"Position PnL monitoring: {pnl:+.2f}% - TP/SL via exchange orders")
                
        except Exception as e:
            logger.error(f"Signal check_status error: {e}")

    def _reset_vbo_state(self):
        """MicroVBO 상태 초기화"""
        self.vbo_entry_price = None
        self.vbo_entry_atr = None

    def _reset_fisher_state(self):
        """FractalFisher 상태 초기화"""
        self.fisher_entry_price = None
        self.fisher_entry_atr = None
        self.fisher_trailing_stop = None

    async def process_primary_candle(self, symbol, k):
        candle_time = k['t']
        
        # 심볼별 상태 초기화
        if symbol not in self.last_candle_time:
            self.last_candle_time[symbol] = 0
            self.last_candle_success[symbol] = True

        # 중복 캔들 방지 (같은 캔들 재처리 차단) - 성공한 경우에만 스킵
        if candle_time <= self.last_candle_time[symbol] and self.last_candle_success[symbol]:
            logger.debug(f"⏭ Skipping duplicate candle: {candle_time} <= {self.last_candle_time[symbol]}")
            return
        
        processing_candle_time = candle_time
        self.last_signal_check = time.time()
        self.last_candle_success[symbol] = False
        
        logger.info(f"🕯️ [Signal] Processing candle: {symbol} close={k['c']}")
        
        if await self.check_daily_loss_limit():
            logger.info("⏸ Daily loss limit reached, skipping trade")
            self.last_candle_time[symbol] = processing_candle_time
            self.last_candle_success[symbol] = True
            return
        
        try:
            await self.check_status(symbol, float(k['c']))
            
            tf = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('timeframe', '15m')
            ohlcv = await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, tf, limit=300)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            # [CRITICAL] Ensure numeric types (Robust Loop)
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            
            # ===== 전략 설정 로드 =====
            strategy_params = self.cfg.get('signal_engine', {}).get('strategy_params', {})
            active_strategy = strategy_params.get('active_strategy', 'sma').lower()
            
            # 전략별 신호 계산
            sig, is_bullish, is_bearish, strategy_name, entry_mode, kalman_enabled = await self._calculate_strategy_signal(symbol, df, strategy_params, active_strategy)
            
            # 매매 방향 필터
            d_mode = self.cfg.get('system_settings', {}).get('trade_direction', 'both')
            if sig and ((d_mode == 'long' and sig == 'short') or (d_mode == 'short' and sig == 'long')):
                logger.info(f"⏸ Signal {sig} blocked by direction filter: {d_mode}")
                sig = None

            # 포지션 확인
            self.position_cache = None
            self.position_cache_time = 0
            pos = await self.get_server_position(symbol, use_cache=False)
            
            current_side = pos['side'] if pos else 'NONE'
            logger.info(f"📍 Current position: {current_side}, Signal: {sig or 'NONE'}, Mode: {entry_mode}")
            
            # 6.5 Pending Re-entry Check (지연 진입)
            # 이전 캔들에서 청산 후 예약된 진입이 있는지 확인
            if self.pending_reentry.get(symbol):
                reentry_data = self.pending_reentry[symbol]
                target_ts = reentry_data.get('target_time', 0)
                side = reentry_data.get('side')
                
                # 타겟 캔들 시간이거나 그 이후면 진입
                if candle_time >= target_ts:
                    logger.info(f"⏰ Executing PENDING re-entry for {side.upper()} at {candle_time} (scheduled for {target_ts})")
                    self.pending_reentry.pop(symbol, None) # Reset first to avoid loop
                    
                    if not pos: # 포지션이 비어있어야 진입
                        await self.entry(symbol, side, float(k['c']))
                    else:
                        logger.warning(f"⚠️ Pending entry skipped: Position not empty ({pos['side']})")
                else:
                    logger.info(f"⏳ Waiting for pending re-entry: target={target_ts}, current={candle_time}")
                    # 아직 시간 안 됐으면 이번 틱 종료 (신규 신호 무시)
                    return 

            # ===== Kalman 방향 가져오기 (참고용) =====
            kalman_direction = self.kalman_states.get(symbol, {}).get('direction')  # 'long' or 'short' or None
            
            # 다음 캔들 시간 계산용
            next_candle_ts = candle_time + self._timeframe_to_ms(tf)

            # ===== entry_mode에 따른 진입 처리 (Exit는 제외) =====
            if entry_mode in ['cross', 'position']:
                # Cross/Position 모드: Primary TF에서는 "진입(Entry)"만 처리
                # 청산(Exit)은 Secondary TF candle (process_exit_candle)에서 처리함
                
                if pos:
                    # 이미 포지션이 있으면 Entry 체크 안 함 (Wait for Exit TF signal)
                    # 단, Pending Re-entry는 위에서 처리됨
                    logger.debug(f"ProcessPrimary: Position exists ({pos['side']}), waiting for Exit TF signal.")
                    
                elif not pos and sig:
                    # 포지션 없음 + 진입 신호 -> 진입
                    strategy_label = "Cross" if entry_mode == 'cross' else "Position"
                    logger.info(f"🚀 {strategy_label} (Primary): New entry {sig.upper()}")
                    await self.entry(symbol, sig, float(k['c']))
            
            elif entry_mode in ['hurst_fisher', 'microvbo']:
                # FractalFisher / MicroVBO 모드: 기존 로직 유지 (단일 TF)
                if not pos and sig:
                    logger.info(f"🚀 New entry ({entry_mode.upper()}): {sig.upper()}")
                    await self.entry(symbol, sig, float(k['c']))
                elif pos and sig:
                    # 포지션 있고 반대 신호 시 플립
                    if (pos['side'] == 'long' and sig == 'short') or (pos['side'] == 'short' and sig == 'long'):
                        logger.info(f"🔄 {entry_mode.upper()}: Flip position {pos['side']} → {sig}")
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
            logger.debug(f"✅ Candle processing completed successfully")
                
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
                slow_period = p.get('slow_sma', 33)
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
            
            # If Short -> Cross Up is exit signal
            if p_f < p_s and c_f > c_s:
                raw_exit_short = True
                
            # Or Position Check? 
            # Usually 'Cross' strategy uses Cross for exit. 
            # 'Position' strategy uses Position (Alignment) for entry, but for exit? 
            # If MA Alignment flips against position, treat as raw exit signal.
            # Let's support both: Cross OR Alignment Flip
            
            if current_side.lower() == 'long':
                if c_f < c_s: # Alignment becomes bearish
                    raw_exit_long = True
            elif current_side.lower() == 'short':
                if c_f > c_s: # Alignment becomes bullish
                    raw_exit_short = True
            
            if not raw_exit_long and not raw_exit_short:
                # No raw exit signal -> No need to check filters
                return

            # ===== 2. Calculate Exit Filters on Exit TF =====
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
                        logger.info(f"✅ Chop Calculated: {curr_chop:.2f} (Thresh: {chop_thresh})")
                    else:
                        logger.warning("⚠️ Chop calculation returned None")
                except Exception as e:
                    logger.error(f"❌ Chop calculation error (Exit): {e}")

            # [New] Update Status Data for Dashboard (Exit Filters)
            # Store pass/fail status later after checking conditions
            self.last_exit_filter_status[symbol] = {
                'r2_val': curr_r2,
                'hurst_val': curr_hurst,
                'chop_val': curr_chop,
                'kalman_vel': kalman_vel,
                # Pass/Fail status defaults to False, updated below
                'r2_pass': False,
                'hurst_pass': False,
                'chop_pass': False,
                'kalman_pass': False,
                'tf': tf
            }
            
            # ===== 3. Check Filter Conditions =====
            # Rule: If Filter ON -> Must confirm Trend (Strong Signal). If OFF -> Ignore.
            # AND Logic: All enabled filters must pass.
            
            can_exit = True
            block_reasons = []
            
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

            # 4. Chop Check
            if chop_exit_enabled:
                if curr_chop > chop_thresh:
                    can_exit = False
                    block_reasons.append(f"Chop({curr_chop:.1f})>{chop_thresh}")
            
            # [New] Update Pass/Fail Status based on logic above
            # Note: The logic above sets can_exit=False if ANY fail.
            # We want individual status. Re-evaluate strictly for display or use flags.
            # Let's just re-evaluate simple boolean logic for display.
            
            st = self.last_exit_filter_status[symbol]
            
            # Kalman Pass?
            k_pass = True
            if kalman_exit_enabled:
                if current_side.lower() == 'long' and kalman_vel >= 0: k_pass = False
                elif current_side.lower() == 'short' and kalman_vel <= 0: k_pass = False
            st['kalman_pass'] = k_pass
            
            # R2 Pass?
            st['r2_pass'] = (not r2_exit_enabled) or (curr_r2 >= r2_thresh)
            
            # Hurst Pass?
            st['hurst_pass'] = (not hurst_exit_enabled) or (curr_hurst >= hurst_thresh)
            
            # Chop Pass?
            st['chop_pass'] = (not chop_exit_enabled) or (curr_chop <= chop_thresh)
            
            
            # ===== 4. Execute Exit =====
            signal_type = "Signal"
            if raw_exit_long and current_side.lower() == 'long':
                signal_type = "Bearish_Cross"
                if can_exit:
                    logger.info(f"🔔 [Exit {tf}] {signal_type} Detected + Filters OK. EXIT LONG.")
                    await self.exit_position(symbol, f"{strategy_name}_Exit_L")
                else:
                    logger.info(f"🛡️ [Exit {tf}] {signal_type} Detected but Blocked by Filters: {', '.join(block_reasons)}")
            
            elif raw_exit_short and current_side.lower() == 'short':
                signal_type = "Bullish_Cross"
                if can_exit:
                    logger.info(f"🔔 [Exit {tf}] {signal_type} Detected + Filters OK. EXIT SHORT.")
                    await self.exit_position(symbol, f"{strategy_name}_Exit_S")
                else:
                    logger.info(f"🛡️ [Exit {tf}] {signal_type} Detected but Blocked by Filters: {', '.join(block_reasons)}")
                
        except Exception as e:
            logger.error(f"Process exit candle error: {e}")
            import traceback
            traceback.print_exc()

    async def _calculate_strategy_signal(self, symbol, df, strategy_params, active_strategy):
        """
        전략별 신호 계산
        
        설정:
        - active_strategy: 'sma', 'hma', 또는 'microvbo'
        - entry_mode: 'cross' (교차) 또는 'position' (위치) - SMA/HMA용
        - kalman_filter.enabled: True/False - SMA/HMA용
        
        Returns: (signal, is_bullish, is_bearish, strategy_name, entry_mode, kalman_enabled)
        """
        sig = None
        is_bullish = False
        is_bearish = False
        
        # Init state dicts if needed
        if symbol not in self.vbo_states: self.vbo_states[symbol] = {}
        if symbol not in self.fisher_states: self.fisher_states[symbol] = {}
        if symbol not in self.kalman_states: self.kalman_states[symbol] = {'velocity': 0.0, 'direction': None}

        entry_mode = strategy_params.get('entry_mode', 'cross')
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
        r2_icon = "✅" if is_r2_pass else "⛔"
        hurst_icon = "✅" if is_hurst_pass else "⛔"
        chop_icon = "✅" if is_chop_pass else "⛔"
        
        if active_strategy in ['sma', 'hma']:
             logger.info(f"🛡️ Filters (Entry): R2({curr_r2:.2f}{r2_icon}) Hurst({curr_hurst:.2f}{hurst_icon}) CHOP({curr_chop:.1f}{chop_icon})")
             # Log Kalman too if used
             
        # ... MicroVBO / FractalFisher skipped (no changes needed) ...
        
        # ===== 1. SMA/HMA 계산 =====
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
            slow_period = p.get('slow_sma', 33) # Default defaults
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

        # ===== 2. Kalman Filter 계산 (필요시) =====
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
            
        logger.info(f"📊 [Kalman] Velocity: {kalman_vel:.4f}, Direction: {self.kalman_states[symbol]['direction']}")
        
        # ===== 3. 진입 모드별 신호 판단 (ENTRY SIGNAL) =====
        if entry_mode == 'cross':
            # SMA Cross Only Logic First
            cross_up = p_f < p_s and c_f > c_s
            cross_down = p_f > p_s and c_f < c_s
            
            if cross_up:
                if kalman_entry_enabled:
                    if kalman_bullish:
                        sig = 'long'
                        logger.info(f"🔔 LONG signal: {strategy_name} Cross + Kalman Entry OK ✅")
                    else:
                        logger.info(f"🛡️ Filtered: {strategy_name} Cross Up but Kalman Entry Filter Blocked")
                else:
                    sig = 'long'
                    logger.info(f"🔔 LONG signal: {strategy_name} Cross (Kalman Filter OFF) ✅")
            
            elif cross_down:
                if kalman_entry_enabled:
                    if kalman_bearish:
                        sig = 'short'
                        logger.info(f"🔔 SHORT signal: {strategy_name} Cross + Kalman Entry OK ✅")
                    else:
                        logger.info(f"🛡️ Filtered: {strategy_name} Cross Down but Kalman Entry Filter Blocked")
                else:
                    sig = 'short'
                    logger.info(f"🔔 SHORT signal: {strategy_name} Cross (Kalman Filter OFF) ✅")
            
            else:
                logger.debug(f"📉 No Cross: {strategy_name}")
        
        elif entry_mode == 'position':
            # Position Mode: MA Alignment
            if ma_bullish:
                if kalman_entry_enabled:
                    if kalman_bullish:
                        sig = 'long'
                        logger.info(f"🔔 LONG signal: {strategy_name} Position + Kalman Entry OK ✅")
                else:
                    sig = 'long'
                    logger.info(f"🔔 LONG signal: {strategy_name} Position (Kalman Filter OFF) ✅")
            
            elif ma_bearish:
                if kalman_entry_enabled:
                    if kalman_bearish:
                        sig = 'short'
                        logger.info(f"🔔 SHORT signal: {strategy_name} Position + Kalman Entry OK ✅")
                else:
                    sig = 'short'
                    logger.info(f"🔔 SHORT signal: {strategy_name} Position (Kalman Filter OFF) ✅")
            
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
                     logger.debug(f"📉 Neutral Position (p_f={p_f:.2f}, p_s={p_s:.2f})")
                elif ma_bullish and kalman_entry_enabled and not kalman_bullish:
                     logger.debug(f"🛡️ Position Long Blocked by Kalman (Vel={kalman_vel:.4f})")
                elif ma_bearish and kalman_entry_enabled and not kalman_bearish:
                     logger.debug(f"🛡️ Position Short Blocked by Kalman (Vel={kalman_vel:.4f})")

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
                logger.info(f"🛡️ Entry Blocked by Filters: {', '.join(blocked_reasons)}")
                sig = None
                is_bullish = False
                is_bearish = False
            
        return sig, is_bullish, is_bearish, strategy_name, entry_mode, kalman_entry_enabled

    async def entry(self, symbol, side, price):
        try:
            logger.info(f"📥 [Signal] Attempting {side.upper()} entry @ {price}")
            
            cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
            lev = cfg.get('leverage', 20)
            risk_pct = cfg.get('risk_per_trade_pct', 50.0) / 100.0  # 실제 적용
            
            bal = await asyncio.to_thread(self.exchange.fetch_balance)
            free = float(bal.get('USDT', {}).get('free', 0))
            
            if free <= 0:
                logger.warning(f"Insufficient balance: {free}")
                await self.ctrl.notify(f"⚠️ 잔고 부족: ${free:.2f}")
                return
            
            qty = self.safe_amount(symbol, (free * risk_pct * lev) / price)
            
            if float(qty) <= 0:
                logger.warning(f"Invalid quantity: {qty} (free={free}, risk={risk_pct}, lev={lev}, price={price})")
                await self.ctrl.notify(f"⚠️ 주문 수량 계산 오류: {qty} (잔고: {free:.2f})")
                return
            
            logger.info(f"Entry params: qty={qty}, lev={lev}x, risk={risk_pct*100}%, raw_calc={(free * risk_pct * lev) / price}")
            
            await asyncio.to_thread(self.exchange.set_leverage, lev, symbol)
            order = await asyncio.to_thread(
                self.exchange.create_order, symbol, 'market', 
                'buy' if side == 'long' else 'sell', qty
            )
            
            # 캐시 완전 무효화
            self.position_cache = None
            self.position_cache_time = 0
            
            self.db.log_trade_entry(symbol, side, price, float(qty))
            await self.ctrl.notify(f"🚀 [Signal] {side.upper()} {qty} @ {price:.2f}")
            logger.info(f"✅ Entry order success: {order.get('id', 'N/A')}")
            
            # 전략 파라미터 로드
            strategy_params = self.cfg.get('signal_engine', {}).get('strategy_params', {})
            active_strategy = strategy_params.get('active_strategy', '').lower()
            
            # 진입 후 포지션 확인하여 정확한 진입가 파악
            await asyncio.sleep(1)
            self.position_cache = None
            verify_pos = await self.get_server_position(symbol, use_cache=False)
            actual_entry_price = float(verify_pos['entryPrice']) if verify_pos else price
            
            # ===== 전략별 TP/SL 설정 =====
            if active_strategy == 'microvbo':
                # MicroVBO: ATR 기반 TP/SL 주문
                if self.vbo_breakout_level:
                    self.vbo_entry_price = actual_entry_price
                    self.vbo_entry_atr = self.vbo_breakout_level.get('atr', 0)
                    vbo_cfg = strategy_params.get('MicroVBO', {})
                    tp_mult = vbo_cfg.get('tp_atr_multiplier', 1.0)
                    sl_mult = vbo_cfg.get('sl_atr_multiplier', 0.5)
                    
                    await self._place_tp_sl_orders(symbol, side, actual_entry_price, qty,
                                                   self.vbo_entry_atr * tp_mult, 
                                                   self.vbo_entry_atr * sl_mult)
                    logger.info(f"💾 [MicroVBO] Entry state saved: price={actual_entry_price:.2f}, ATR={self.vbo_entry_atr:.2f}")
            
            elif active_strategy == 'fractalfisher':
                # FractalFisher: Trailing Stop (소프트웨어 기반 유지 - 동적이므로)
                if self.fisher_entry_atr:
                    self.fisher_entry_price = actual_entry_price
                    ff_cfg = strategy_params.get('FractalFisher', {})
                    trailing_mult = ff_cfg.get('atr_trailing_multiplier', 2.0)
                    if side == 'long':
                        self.fisher_trailing_stop = actual_entry_price - (self.fisher_entry_atr * trailing_mult)
                    else:
                        self.fisher_trailing_stop = actual_entry_price + (self.fisher_entry_atr * trailing_mult)
                    logger.info(f"💾 [FractalFisher] Entry state: price={actual_entry_price:.2f}, TrailingStop={self.fisher_trailing_stop:.2f}")
                    await self.ctrl.notify(f"📍 Trailing Stop 설정: {self.fisher_trailing_stop:.2f}")
            
            else:
                # SMA/HMA: 퍼센트 기반 TP/SL 주문
                if cfg.get('tp_sl_enabled', True):
                    tp_pct = cfg.get('target_roe_pct', 20.0) / 100.0 / lev  # ROE를 가격 변동률로 변환
                    sl_pct = cfg.get('stop_loss_pct', 10.0) / 100.0 / lev
                    
                    tp_distance = actual_entry_price * tp_pct
                    sl_distance = actual_entry_price * sl_pct
                    
                    await self._place_tp_sl_orders(symbol, side, actual_entry_price, qty,
                                                   tp_distance, sl_distance)
            
            if verify_pos:
                logger.info(f"✅ Position verified: {verify_pos['side']} {verify_pos['contracts']}")
            else:
                logger.warning("⚠️ Position not found after entry (may take time to update)")
            
        except Exception as e:
            logger.error(f"Signal entry error: {e}")
            import traceback
            traceback.print_exc()
            await self.ctrl.notify(f"❌ 진입 실패: {e}")

    async def _place_tp_sl_orders(self, symbol, side, entry_price, qty, tp_distance, sl_distance):
        """거래소에 TP/SL 주문 배치 (오픈오더에 보임)"""
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
            
            # Take Profit 주문 (지정가 + reduceOnly)
            try:
                tp_order = await asyncio.to_thread(
                    self.exchange.create_order, symbol, 'limit', tp_side, qty, tp_price,
                    {'reduceOnly': True}
                )
                logger.info(f"✅ TP order placed: {tp_side.upper()} @ {tp_price}")
            except Exception as tp_e:
                logger.error(f"TP order failed: {tp_e}")
                tp_order = None
            
            # Stop Loss 주문 (스탑 마켓 + reduceOnly)
            try:
                sl_order = await asyncio.to_thread(
                    self.exchange.create_order, symbol, 'stop_market', sl_side, qty, None,
                    {'stopPrice': sl_price, 'reduceOnly': True}
                )
                logger.info(f"✅ SL order placed: {sl_side.upper()} @ {sl_price} (stop)")
            except Exception as sl_e:
                logger.error(f"SL order failed: {sl_e}")
                sl_order = None
            
            if tp_order or sl_order:
                await self.ctrl.notify(f"🎯 TP: `{tp_price:.2f}` | 🛑 SL: `{sl_price:.2f}`")
            
        except Exception as e:
            logger.error(f"TP/SL order placement error: {e}")

    async def exit_position(self, symbol, reason):
        logger.info(f"📤 [Signal] Attempting exit: {reason}")
        
        # 먼저 TP/SL 주문 취소 (있는 경우)
        try:
            await asyncio.to_thread(self.exchange.cancel_all_orders, symbol)
            logger.info(f"✅ All orders cancelled for {symbol}")
        except Exception as cancel_e:
            logger.warning(f"Order cancellation failed (may have none): {cancel_e}")
        
        # 캐시 무효화 후 포지션 확인
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
        
        # 재시도 로직 (최대 3회)
        max_retries = 3
        order = None
        last_error = None
        
        for attempt in range(max_retries):
            try:
                order = await asyncio.to_thread(
                    self.exchange.create_order, symbol, 'market', side, qty
                )
                break  # 성공 시 루프 탈출
            except Exception as e:
                last_error = e
                logger.error(f"Exit attempt {attempt + 1}/{max_retries} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)  # 1초 대기 후 재시도
                    await self.ctrl.notify(f"⚠️ 청산 재시도 중... ({attempt + 2}/{max_retries})")
        
        if not order:
            logger.error(f"❌ Exit failed after {max_retries} attempts, trying force close...")
            await self.ctrl.notify(f"🚨 청산 실패! 강제 청산 시도 중...")
            
            # 강제 청산: 모든 주문 취소 후 reduceOnly로 재시도
            try:
                await asyncio.to_thread(self.exchange.cancel_all_orders, symbol)
                await asyncio.sleep(0.5)
                
                # reduceOnly 옵션으로 강제 청산
                order = await asyncio.to_thread(
                    self.exchange.create_order, symbol, 'market', side, qty,
                    {'reduceOnly': True}
                )
                await self.ctrl.notify(f"✅ 강제 청산 성공!")
            except Exception as force_e:
                logger.error(f"Force close also failed: {force_e}")
                await self.ctrl.notify(f"🚨🚨 강제 청산도 실패! 즉시 수동 청산 필요: {force_e}")
                return
        
        pnl = float(pos.get('unrealizedPnl', 0))
        pnl_pct = float(pos.get('percentage', 0))
        exit_price = float(order.get('average', 0)) if order.get('average') else float(pos.get('markPrice', 0))
        
        # 캐시 완전 무효화
        self.position_cache = None
        self.position_cache_time = 0
        
        self.db.log_trade_close(symbol, pnl, pnl_pct, exit_price, reason)
        await self.ctrl.notify(f"🧹 [{reason}] PnL: {pnl:+.2f} ({pnl_pct:+.2f}%)")
        logger.info(f"✅ Exit order success: {order.get('id', 'N/A')}")


class ShannonEngine(BaseEngine):
    def __init__(self, controller):
        super().__init__(controller)
        self.last_logic_time = 0
        self.last_indicator_update = 0
        self.ratio = controller.cfg.get('shannon_engine', {}).get('asset_allocation', {}).get('target_ratio', 0.5)
        self.grid_orders = []
        
        # 지표 캐시
        self.ema_200 = None
        self.atr_value = None
        self.trend_direction = None  # 'long', 'short', or None
        self.INDICATOR_UPDATE_INTERVAL = 10  # 10초마다 지표 갱신

    async def poll_tick(self):
        """
        [순수 폴링] Shannon 엔진 메인 폴링 함수
        - 현재 가격 조회
        - 리밸런싱 로직 실행
        """
        if not self.running:
            return
        
        try:
            symbol = self._get_target_symbol()
            
            # 현재 가격 조회 (ticker 사용)
            ticker = await asyncio.to_thread(self.exchange.fetch_ticker, symbol)
            price = float(ticker['last'])
            
            # 리밸런싱 로직 실행 (1초 간격 제한)
            if time.time() - self.last_logic_time > 1.0:
                await self.logic(symbol, price)
                self.last_logic_time = time.time()
                
        except Exception as e:
            logger.error(f"Shannon poll_tick error: {e}")
    
    def _get_target_symbol(self):
        """config에서 target_symbol 가져오기"""
        return self.cfg.get('shannon_engine', {}).get('target_symbol', 'BTC/USDT')

    async def update_indicators(self, symbol):
        """200 EMA와 ATR 업데이트"""
        now = time.time()
        
        # 첫 실행이거나 캐시 만료 시 업데이트
        # trend_direction이 None이면 강제 업데이트 (첫 진입 위해)
        if self.trend_direction is not None and now - self.last_indicator_update < self.INDICATOR_UPDATE_INTERVAL:
            return  # 캐시 사용
        
        try:
            cfg = self.cfg.get('shannon_engine', {})
            # Shannon에 timeframe 설정이 없으면 signal_engine 설정 사용
            tf = cfg.get('timeframe') or self.cfg.get('signal_engine', {}).get('common_settings', {}).get('timeframe', '15m')
            
            # OHLCV 데이터 가져오기 (200 EMA 계산을 위해 최소 250개)
            ohlcv = await asyncio.to_thread(
                self.exchange.fetch_ohlcv, symbol, tf, limit=250
            )
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            # 200 EMA 계산
            trend_cfg = cfg.get('trend_filter', {})
            if trend_cfg.get('enabled', True):
                ema_period = trend_cfg.get('ema_period', 200)
                df['ema'] = ta.ema(df['close'], length=ema_period)
                self.ema_200 = df['ema'].iloc[-1]
                # trend_direction은 logic()에서 실시간 가격으로 결정
                logger.info(f"📊 {tf} 200 EMA 업데이트: {self.ema_200:.2f}")
            
            # ATR 계산
            atr_cfg = cfg.get('atr_settings', {})
            if atr_cfg.get('enabled', True):
                atr_period = atr_cfg.get('period', 14)
                df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=atr_period)
                self.atr_value = df['atr'].iloc[-1]
                logger.info(f"📊 ATR({atr_period}): {self.atr_value:.2f}")
            
            self.last_indicator_update = now
            
        except Exception as e:
            logger.error(f"Indicator update error: {e}")

    def get_drawdown_multiplier(self, total_equity, daily_pnl):
        """드로우다운 기반 리스크 승수 계산"""
        cfg = self.cfg.get('shannon_engine', {}).get('drawdown_protection', {})
        
        if not cfg.get('enabled', True):
            return 1.0
        
        threshold_pct = cfg.get('threshold_pct', 3.0)
        reduction_factor = cfg.get('reduction_factor', 0.5)
        
        # 일일 손실률 계산
        if total_equity > 0:
            daily_loss_pct = abs(min(0, daily_pnl)) / total_equity * 100
        else:
            daily_loss_pct = 0
        
        if daily_loss_pct >= threshold_pct:
            logger.warning(f"⚠️ Drawdown protection: {daily_loss_pct:.2f}% loss → {reduction_factor*100:.0f}% size")
            return reduction_factor
        
        return 1.0

    async def logic(self, symbol, price):
        try:
            # 지표 업데이트
            await self.update_indicators(symbol)
            
            total, free, mmr = await self.get_balance_info()
            count, daily_pnl = self.db.get_daily_stats()
            pos = await self.get_server_position(symbol)
            
            # 드로우다운 승수 계산
            dd_multiplier = self.get_drawdown_multiplier(total, daily_pnl)
            
            coin_amt = float(pos['contracts']) if pos else 0.0
            coin_val = abs(coin_amt * price)
            diff = coin_val - (total * self.ratio)
            diff_pct = (diff / total * 100) if total > 0 else 0

            # 상태 데이터 업데이트
            self.ctrl.status_data = {
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

            # MMR 경고 체크
            await self.check_mmr_alert(mmr)

            if self.ctrl.is_paused:
                return
            
            # 일일 손실 한도 체크
            if await self.check_daily_loss_limit():
                return
            
            cfg = self.cfg.get('shannon_engine', {})
            trend_cfg = cfg.get('trend_filter', {})
            
            # ============ 실시간 가격 기준 추세 방향 결정 ============
            if trend_cfg.get('enabled', True) and self.ema_200 is not None:
                if price > self.ema_200:
                    self.trend_direction = 'long'
                else:
                    self.trend_direction = 'short'
                logger.info(f"📊 현재가 {price:.2f} vs 200 EMA {self.ema_200:.2f} → {self.trend_direction.upper()}")
            
            # ============ 초기 진입 로직 (200 EMA 기반) ============
            # 포지션이 없을 때 200 EMA 방향으로 초기 진입
            if not pos and trend_cfg.get('enabled', True) and self.trend_direction:
                # 매매 방향 필터 적용
                d_mode = self.cfg.get('system_settings', {}).get('trade_direction', 'both')
                if (d_mode == 'long' and self.trend_direction == 'short') or \
                   (d_mode == 'short' and self.trend_direction == 'long'):
                    logger.info(f"⏸ [Shannon] Entry blocked by direction filter: {d_mode}")
                    return  # 필터에 의해 진입 차단
                
                target_value = total * self.ratio * dd_multiplier  # 목표 50%
                entry_qty = self.safe_amount(symbol, target_value / price)
                
                if float(entry_qty) > 0:
                    # 레버리지 설정 적용
                    lev = cfg.get('leverage', 5)
                    await asyncio.to_thread(self.exchange.set_leverage, lev, symbol)
                    
                    if self.trend_direction == 'long':
                        # 200 EMA 위 → 롱 진입
                        order = await asyncio.to_thread(
                            self.exchange.create_order, symbol, 'market', 'buy', entry_qty
                        )
                        self.position_cache = None
                        self.position_cache_time = 0
                        await self.ctrl.notify(f"🚀 [Shannon] LONG 진입 {entry_qty} @ {price:.2f} (200 EMA 상향)")
                        self.db.log_shannon(total, "ENTRY_LONG", price, float(entry_qty), total)
                        logger.info(f"Shannon initial LONG entry: {order}")
                        return
                    
                    elif self.trend_direction == 'short':
                        # 200 EMA 아래 → 숏 진입
                        order = await asyncio.to_thread(
                            self.exchange.create_order, symbol, 'market', 'sell', entry_qty
                        )
                        self.position_cache = None
                        self.position_cache_time = 0
                        await self.ctrl.notify(f"🚀 [Shannon] SHORT 진입 {entry_qty} @ {price:.2f} (200 EMA 하향)")
                        self.db.log_shannon(total, "ENTRY_SHORT", price, float(entry_qty), total)
                        logger.info(f"Shannon initial SHORT entry: {order}")
                        return
            
            # ============ 추세 반전 감지: 포지션 청산 후 재진입 ============
            reversed_position = False
            if pos and trend_cfg.get('enabled', True) and self.trend_direction:
                current_side = pos['side']
                # 롱 포지션인데 추세가 하락으로 전환
                if current_side == 'long' and self.trend_direction == 'short':
                    await self._close_and_reverse(symbol, pos, price, 'short', total, dd_multiplier)
                    reversed_position = True
                    # 포지션 변경 후 다시 조회
                    pos = await self.get_server_position(symbol, use_cache=False)
                # 숏 포지션인데 추세가 상승으로 전환
                elif current_side == 'short' and self.trend_direction == 'long':
                    await self._close_and_reverse(symbol, pos, price, 'long', total, dd_multiplier)
                    reversed_position = True
                    pos = await self.get_server_position(symbol, use_cache=False)
            
            # ============ Grid Trading (ATR 기반 간격) ============
            grid_cfg = cfg.get('grid_trading', {})
            if grid_cfg.get('enabled', False):
                await self.manage_grid_orders(symbol, price, grid_cfg, dd_multiplier)
            
            # ============ Rebalance (비율 유지) ============
            threshold = cfg.get('asset_allocation', {}).get('allowed_deviation_pct', 2.0)
            if abs(diff_pct) > threshold and pos:
                current_side = pos['side']
                contracts = abs(float(pos['contracts']))
                target_contracts = (total * self.ratio) / price  # 목표 계약 수
                
                if current_side == 'long':
                    # 롱 포지션 리밸런싱
                    if contracts > target_contracts:
                        # 포지션 과다 → 일부 청산 (매도)
                        reduce_qty = self.safe_amount(symbol, (contracts - target_contracts) * dd_multiplier)
                        if float(reduce_qty) > 0:
                            order = await asyncio.to_thread(
                                self.exchange.create_order, symbol, 'market', 'sell', reduce_qty
                            )
                            self.position_cache = None
                            self.position_cache_time = 0
                            await self.ctrl.notify(f"⚖️ [Long] 축소: SELL {reduce_qty}")
                            logger.info(f"Rebalance SELL: {order}")
                    else:
                        # 포지션 부족 → 추가 매수 (추세 필터: 롱 추세일 때만)
                        if self.trend_direction == 'long':
                            add_qty = self.safe_amount(symbol, (target_contracts - contracts) * dd_multiplier)
                            if float(add_qty) > 0:
                                order = await asyncio.to_thread(
                                    self.exchange.create_order, symbol, 'market', 'buy', add_qty
                                )
                                self.position_cache = None
                                self.position_cache_time = 0
                                await self.ctrl.notify(f"⚖️ [Long] 확대: BUY {add_qty}")
                                logger.info(f"Rebalance BUY: {order}")
                        else:
                            logger.info(f"⏸ [Long] 확대 차단: 추세가 {self.trend_direction}")
                else:
                    # 숏 포지션 리밸런싱
                    if contracts > target_contracts:
                        # 숏 과다 → 일부 청산 (매수로 커버)
                        reduce_qty = self.safe_amount(symbol, (contracts - target_contracts) * dd_multiplier)
                        if float(reduce_qty) > 0:
                            order = await asyncio.to_thread(
                                self.exchange.create_order, symbol, 'market', 'buy', reduce_qty
                            )
                            self.position_cache = None
                            self.position_cache_time = 0
                            await self.ctrl.notify(f"⚖️ [Short] 축소: BUY {reduce_qty}")
                            logger.info(f"Rebalance BUY (cover): {order}")
                    else:
                        # 숏 부족 → 추가 매도 (추세 필터: 숏 추세일 때만)
                        if self.trend_direction == 'short':
                            add_qty = self.safe_amount(symbol, (target_contracts - contracts) * dd_multiplier)
                            if float(add_qty) > 0:
                                order = await asyncio.to_thread(
                                    self.exchange.create_order, symbol, 'market', 'sell', add_qty
                                )
                                self.position_cache = None
                                self.position_cache_time = 0
                                await self.ctrl.notify(f"⚖️ [Short] 확대: SELL {add_qty}")
                                logger.info(f"Rebalance SELL (add short): {order}")
                        else:
                            logger.info(f"⏸ [Short] 확대 차단: 추세가 {self.trend_direction}")
                
                self.db.log_shannon(total, "REBAL", price, coin_amt, total)
                    
        except Exception as e:
            logger.error(f"Shannon logic error: {e}")

    async def _close_and_reverse(self, symbol, pos, price, new_direction, total, dd_multiplier):
        """추세 반전 시 포지션 청산 후 반대 방향 진입"""
        try:
            # 매매 방향 필터 체크
            d_mode = self.cfg.get('system_settings', {}).get('trade_direction', 'both')
            if (d_mode == 'long' and new_direction == 'short') or \
               (d_mode == 'short' and new_direction == 'long'):
                # 방향 필터에 의해 재진입 불가 → 청산만
                close_qty = self.safe_amount(symbol, abs(float(pos['contracts'])))
                close_side = 'sell' if pos['side'] == 'long' else 'buy'
                await asyncio.to_thread(
                    self.exchange.create_order, symbol, 'market', close_side, close_qty
                )
                pnl = float(pos.get('unrealizedPnl', 0))
                await self.ctrl.notify(f"🔒 [Shannon] {pos['side'].upper()} 청산 (방향 필터) PnL: {pnl:+.2f}")
                self.position_cache = None
                self.position_cache_time = 0
                return
            
            # 기존 포지션 청산
            close_qty = self.safe_amount(symbol, abs(float(pos['contracts'])))
            close_side = 'sell' if pos['side'] == 'long' else 'buy'
            
            await asyncio.to_thread(
                self.exchange.create_order, symbol, 'market', close_side, close_qty
            )
            
            pnl = float(pos.get('unrealizedPnl', 0))
            await self.ctrl.notify(f"🔄 [Shannon] {pos['side'].upper()} 청산 (추세 반전) PnL: {pnl:+.2f}")
            
            # 청산 완료 대기
            await asyncio.sleep(2.0)  # 2초 대기
            
            # 반대 방향 새 포지션 진입
            target_value = total * self.ratio * dd_multiplier
            entry_qty = self.safe_amount(symbol, target_value / price)
            entry_side = 'buy' if new_direction == 'long' else 'sell'
            
            if float(entry_qty) > 0:
                await asyncio.to_thread(
                    self.exchange.create_order, symbol, 'market', entry_side, entry_qty
                )
                self.position_cache = None
                self.position_cache_time = 0
                await self.ctrl.notify(f"🚀 [Shannon] {new_direction.upper()} 진입 {entry_qty} @ {price:.2f}")
                self.db.log_shannon(total, f"REVERSE_{new_direction.upper()}", price, float(entry_qty), total)
                
        except Exception as e:
            logger.error(f"Shannon close and reverse error: {e}")

    async def manage_grid_orders(self, symbol, price, grid_cfg, dd_multiplier=1.0):
        """Grid Trading 로직 - ATR 기반 동적 간격"""
        try:
            levels = grid_cfg.get('grid_levels', 5)
            base_order_size = grid_cfg.get('order_size_usdt', 20)
            
            # 드로우다운 보호 적용
            order_size = base_order_size * dd_multiplier
            
            # 레버리지 설정 확인 (Grid 주문에도 동일하게 적용)
            lev = self.cfg.get('shannon_engine', {}).get('leverage', 5)
            try:
                await asyncio.to_thread(self.exchange.set_leverage, lev, symbol)
            except Exception:
                pass  # 이미 설정된 경우 무시
            
            # ATR 기반 그리드 간격 계산
            atr_cfg = self.cfg.get('shannon_engine', {}).get('atr_settings', {})
            if atr_cfg.get('enabled', True) and self.atr_value:
                atr_multiplier = atr_cfg.get('grid_multiplier', 0.5)
                # ATR 기반 간격 (가격 대비 비율)
                step_pct = (self.atr_value * atr_multiplier) / price
                logger.debug(f"ATR Grid Step: {step_pct*100:.3f}%")
            else:
                # ATR 없으면 기본값 사용
                step_pct = 0.005  # 0.5%
            
            # 200 EMA 방향 필터 적용
            trend_cfg = self.cfg.get('shannon_engine', {}).get('trend_filter', {})
            allow_buy = True
            allow_sell = True
            
            if trend_cfg.get('enabled', True) and self.trend_direction:
                if self.trend_direction == 'short':
                    allow_buy = False  # 하락 추세: 매수 주문 금지
                elif self.trend_direction == 'long':
                    allow_sell = False  # 상승 추세: 매도 주문 금지
            
            # 기존 그리드 주문 상태 확인
            try:
                open_orders = await asyncio.to_thread(self.exchange.fetch_open_orders, symbol)
            except Exception as e:
                logger.error(f"Fetch open orders error: {e}")
                return
            
            # #5 Fix: 현재 가격에서 너무 멀어진 주문 취소 (가격의 5% 이상)
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
            
            # 현재 포지션 확인 (#10 Fix)
            pos = await self.get_server_position(symbol, use_cache=True)
            has_long_position = pos and pos['side'] == 'long' and float(pos['contracts']) > 0
            has_short_position = pos and pos['side'] == 'short' and abs(float(pos['contracts'])) > 0
            
            # 그리드 주문 방향 결정 (포지션 기반)
            # - 롱 포지션: 매도 주문 허용 (익절/청산용), 매수 주문도 허용 (추가 진입)
            # - 숏 포지션: 매수 주문 허용 (익절/청산용), 매도 주문도 허용 (추가 진입)
            # - 포지션 없음: 추세 방향으로만 주문
            if has_long_position:
                allow_sell = True  # 롱 포지션 청산용 매도 허용
                # 롱 포지션에서 추가 매수는 추세 필터 따름
            elif has_short_position:
                allow_buy = True  # 숏 포지션 청산용 매수 허용
                # 숏 포지션에서 추가 매수는 추세 필터 따름
            else:
                # 포지션 없을 때: 추세 방향에 반하는 주문 금지
                if self.trend_direction == 'short':
                    allow_buy = False  # 숏 추세: 매수 금지
                elif self.trend_direction == 'long':
                    allow_sell = False  # 롱 추세: 매도 금지
            
            # 그리드 주문이 부족하면 새로 생성
            if len(open_orders) < levels * 2:
                for i in range(1, levels + 1):
                    # 매수 주문 (현재가 아래)
                    buy_price = price * (1 - step_pct * i)
                    buy_qty = self.safe_amount(symbol, order_size / buy_price)
                    
                    # 매도 주문 (현재가 위)
                    sell_price = price * (1 + step_pct * i)
                    sell_qty = self.safe_amount(symbol, order_size / sell_price)
                    
                    # 중복 체크
                    buy_exists = any(
                        abs(float(o['price']) - buy_price) / buy_price < 0.002 
                        for o in open_orders if o['side'] == 'buy'
                    )
                    sell_exists = any(
                        abs(float(o['price']) - sell_price) / sell_price < 0.002 
                        for o in open_orders if o['side'] == 'sell'
                    )
                    
                    # 매수 주문 (추세 필터 적용)
                    if allow_buy and not buy_exists and float(buy_qty) > 0:
                        try:
                            await asyncio.to_thread(
                                self.exchange.create_order, symbol, 'limit', 'buy',
                                buy_qty, self.safe_price(symbol, buy_price)
                            )
                            logger.info(f"Grid BUY: {buy_qty} @ {buy_price:.2f} (ATR step: {step_pct*100:.2f}%)")
                        except Exception as e:
                            logger.error(f"Grid buy order error: {e}")
                    
                    # 매도 주문 (추세 필터 + 포지션 체크 적용)
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
    """듀얼 트러스트 변동성 돌파 전략"""
    def __init__(self, controller):
        super().__init__(controller)
        self.last_heartbeat = 0
        self.last_trigger_update = 0
        self.consecutive_errors = 0
        
        # 트리거 캐시
        self.today_open = None
        self.range_value = None
        self.long_trigger = None
        self.short_trigger = None
        self.trigger_date = None  # 트리거 계산 날짜 (오버나잇 리셋용)
        
        self.TRIGGER_UPDATE_INTERVAL = 60  # 60초마다 체크 (일 변경 확인)

    def _get_target_symbol(self):
        return self.cfg.get('dual_thrust_engine', {}).get('target_symbol', 'BTC/USDT')

    async def poll_tick(self):
        """[순수 폴링] Dual Thrust 메인 폴링"""
        if not self.running:
            return
        
        try:
            symbol = self._get_target_symbol()
            
            # 현재 가격 조회
            ticker = await asyncio.to_thread(self.exchange.fetch_ticker, symbol)
            price = float(ticker['last'])
            
            # 트리거 업데이트 (하루 변경 시 갱신)
            await self._update_triggers(symbol)
            
            # 하트비트 (30초마다)
            now = time.time()
            if now - self.last_heartbeat > 30:
                self.last_heartbeat = now
                pos_side = self.ctrl.status_data.get('pos_side', 'UNKNOWN') if self.ctrl.status_data else 'UNKNOWN'
                logger.info(f"💓 [DualThrust] Heartbeat: running={self.running}, paused={self.ctrl.is_paused}, pos={pos_side}, price={price:.2f}")
                if self.long_trigger and self.short_trigger:
                    logger.info(f"📊 [DualThrust] Triggers: Long={self.long_trigger:.2f}, Short={self.short_trigger:.2f}, Range={self.range_value:.2f}")
            
            # 상태 업데이트 + 매매 로직
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
        """N일 일봉으로 Range 계산 및 트리거 업데이트"""
        now = datetime.now(timezone.utc)
        today_str = now.strftime('%Y-%m-%d')
        
        # 이미 오늘 계산했으면 스킵
        if self.trigger_date == today_str and self.long_trigger and self.short_trigger:
            return
        
        try:
            cfg = self.cfg.get('dual_thrust_engine', {})
            n_days = cfg.get('n_days', 4)
            k1 = cfg.get('k1', 0.5)
            k2 = cfg.get('k2', 0.5)
            
            # 일봉 데이터 가져오기 (N+1개: 과거 N일 + 오늘)
            ohlcv = await asyncio.to_thread(
                self.exchange.fetch_ohlcv, symbol, '1d', limit=n_days + 1
            )
            
            if len(ohlcv) < n_days + 1:
                logger.warning(f"[DualThrust] Insufficient daily data: {len(ohlcv)} < {n_days + 1}")
                return
            
            # 과거 N일 데이터 (오늘 제외)
            past_n = ohlcv[:-1][-n_days:]
            
            # HH, HC, LC, LL 계산
            highs = [candle[2] for candle in past_n]
            lows = [candle[3] for candle in past_n]
            closes = [candle[4] for candle in past_n]
            
            hh = max(highs)  # Highest High
            hc = max(closes)  # Highest Close
            lc = min(closes)  # Lowest Close
            ll = min(lows)  # Lowest Low
            
            # Range = max(HH - LC, HC - LL)
            self.range_value = max(hh - lc, hc - ll)
            
            # 당일 시가 (오늘 캔들)
            self.today_open = ohlcv[-1][1]
            
            # 트리거 계산
            self.long_trigger = self.today_open + (self.range_value * k1)
            self.short_trigger = self.today_open - (self.range_value * k2)
            self.trigger_date = today_str
            
            logger.info(f"📈 [DualThrust] Triggers Updated: Open={self.today_open:.2f}, Range={self.range_value:.2f}")
            logger.info(f"   Long Trigger: {self.long_trigger:.2f}, Short Trigger: {self.short_trigger:.2f}")
            
        except Exception as e:
            logger.error(f"DualThrust trigger update error: {e}")

    async def _logic(self, symbol, price):
        """매매 로직"""
        try:
            total, free, mmr = await self.get_balance_info()
            count, daily_pnl = self.db.get_daily_stats()
            pos = await self.get_server_position(symbol)
            
            # 상태 데이터 업데이트
            self.ctrl.status_data = {
                'engine': 'DualThrust', 'symbol': symbol, 'price': price,
                'total_equity': total, 'free_usdt': free, 'mmr': mmr,
                'daily_count': count, 'daily_pnl': daily_pnl,
                'pos_side': pos['side'].upper() if pos else 'NONE',
                'entry_price': float(pos['entryPrice']) if pos else 0.0,
                'coin_amt': float(pos['contracts']) if pos else 0.0,
                'pnl_pct': float(pos['percentage']) if pos else 0.0,
                'pnl_usdt': float(pos['unrealizedPnl']) if pos else 0.0,
                # Dual Thrust 전용
                'long_trigger': self.long_trigger,
                'short_trigger': self.short_trigger,
                'range': self.range_value,
                'today_open': self.today_open
            }
            
            # MMR 경고
            await self.check_mmr_alert(mmr)
            
            if self.ctrl.is_paused:
                return
            
            # 일일 손실 한도
            if await self.check_daily_loss_limit():
                return
            
            # 트리거가 없으면 대기
            if not self.long_trigger or not self.short_trigger:
                logger.debug("[DualThrust] Waiting for trigger calculation...")
                return
            
            # 매매 방향 필터
            d_mode = self.cfg.get('system_settings', {}).get('trade_direction', 'both')
            
            current_side = pos['side'] if pos else None
            
            # 롱 트리거 돌파
            if price > self.long_trigger:
                if d_mode != 'short':  # 숏 전용 아니면
                    if current_side == 'short':
                        # 숏 청산 후 롱 스위칭
                        await self._close_and_switch(symbol, pos, price, 'long', total)
                    elif not pos:
                        # 포지션 없으면 롱 진입
                        await self._entry(symbol, 'long', price, total)
            
            # 숏 트리거 이탈
            elif price < self.short_trigger:
                if d_mode != 'long':  # 롱 전용 아니면
                    if current_side == 'long':
                        # 롱 청산 후 숏 스위칭
                        await self._close_and_switch(symbol, pos, price, 'short', total)
                    elif not pos:
                        # 포지션 없으면 숏 진입
                        await self._entry(symbol, 'short', price, total)
                        
        except Exception as e:
            logger.error(f"DualThrust logic error: {e}")

    async def _entry(self, symbol, side, price, total_equity):
        """포지션 진입"""
        try:
            cfg = self.cfg.get('dual_thrust_engine', {})
            lev = cfg.get('leverage', 5)
            risk_pct = cfg.get('risk_per_trade_pct', 50.0) / 100.0
            
            # 레버리지 설정
            await asyncio.to_thread(self.exchange.set_leverage, lev, symbol)
            
            # 수량 계산
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
            await self.ctrl.notify(f"🚀 [DualThrust] {side.upper()} {qty} @ {price:.2f}")
            logger.info(f"✅ [DualThrust] Entry: {side} {qty} @ {price}")
            
        except Exception as e:
            logger.error(f"DualThrust entry error: {e}")
            await self.ctrl.notify(f"❌ [DualThrust] 진입 실패: {e}")

    async def _close_and_switch(self, symbol, pos, price, new_side, total_equity):
        """포지션 청산 후 반대 방향 진입"""
        try:
            # 매매 방향 필터 체크
            d_mode = self.cfg.get('system_settings', {}).get('trade_direction', 'both')
            if (d_mode == 'long' and new_side == 'short') or (d_mode == 'short' and new_side == 'long'):
                # 청산만 하고 재진입 안함
                await self.exit_position(symbol, "DirectionFilter")
                return
            
            # 기존 포지션 청산
            close_qty = self.safe_amount(symbol, abs(float(pos['contracts'])))
            close_side = 'sell' if pos['side'] == 'long' else 'buy'
            
            pnl = float(pos.get('unrealizedPnl', 0))
            
            await asyncio.to_thread(
                self.exchange.create_order, symbol, 'market', close_side, close_qty
            )
            
            self.db.log_trade_close(symbol, pnl, float(pos.get('percentage', 0)), price, "Switch")
            await self.ctrl.notify(f"🔄 [DualThrust] {pos['side'].upper()} 청산 → {new_side.upper()} 스위칭 | PnL: {pnl:+.2f}")
            
            self.position_cache = None
            self.position_cache_time = 0
            
            # 잠시 대기 후 반대 방향 진입
            await asyncio.sleep(1.0)
            await self._entry(symbol, new_side, price, total_equity)
            
        except Exception as e:
            logger.error(f"DualThrust switch error: {e}")

    async def exit_position(self, symbol, reason):
        """포지션 청산"""
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
            await self.ctrl.notify(f"🧹 [DualThrust] [{reason}] PnL: {pnl:+.2f} ({pnl_pct:+.2f}%)")
            
        except Exception as e:
            logger.error(f"DualThrust exit error: {e}")


class DualModeFractalEngine(BaseEngine):
    """
    DualModeFractalStrategy를 사용하는 독립 엔진
    - Mode: Scalping (5m~15m) / Standard (1h~4h)
    """
    def __init__(self, controller):
        super().__init__(controller)
        self.strategy = None
        self.current_mode = None
        self.last_candle_ts = 0
    
    def _get_target_symbol(self):
        # 공통 설정의 Watchlist 첫 번째 코인을 타겟으로 사용
        watchlist = self.cfg.get('signal_engine', {}).get('watchlist', [])
        return watchlist[0] if watchlist else 'BTC/USDT'

    def start(self):
        super().start()
        self._init_strategy()

    def _init_strategy(self):
        cfg = self.cfg.get('dual_mode_engine', {})
        mode = cfg.get('mode', 'standard')
        self.strategy = DualModeFractalStrategy(mode=mode)
        self.current_mode = mode
        logger.info(f"⚛️ [DualMode] Strategy initialized: {mode.upper()}")

    async def poll_tick(self):
        if not self.running:
            return
        
        try:
            symbol = self._get_target_symbol()
            cfg = self.cfg.get('dual_mode_engine', {})
            
            # 모드 변경 감지 및 재초기화
            if cfg.get('mode') != self.current_mode:
                self._init_strategy()
            
            # 타임프레임 결정
            if self.current_mode == 'scalping':
                tf = cfg.get('scalping_tf', '5m')
            else:
                tf = cfg.get('standard_tf', '4h')
                
            # OHLCV 가져오기 (Limit reduced to 300 for optimization)
            ohlcv = await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, tf, limit=300)
            if not ohlcv: return
            
            # 마지막 확정 캔들 기준 (현재 진행 중인 캔들은 제외하거나 포함 여부 결정)
            # 전략 로직상 'close' 데이터를 쓰므로 확정된 캔들이 안전함
            last_closed = ohlcv[-2] 
            ts = last_closed[0]
            price = float(ohlcv[-1][4]) # 현재가는 실시간
            
            # 새 캔들 갱신 시에만 시그널 계산 (폴링 부하 감소)
            if ts > self.last_candle_ts:
                self.last_candle_ts = ts
                
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                
                # 전략 실행
                res = self.strategy.generate_signals(df)
                last_row = res.iloc[-2] # 확정 캔들 기준 시그널
                
                sig = int(last_row['signal'])
                chop = float(last_row['chop_idx'])
                kalman = float(last_row['kalman_val'])
                exit_price = float(last_row['exit_price'])
                
                logger.info(f"⚛️ [DualMode] {tf} Candle Close: Chop={chop:.1f}, Kalman={kalman:.1f}, Signal={sig}")
                
                # 매매 로직
                await self._execute_signal(symbol, sig, price)
                
            # 상태 업데이트 (대시보드용)
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
        # 공통 설정에서 리스크 및 TP/SL 가져오기
        common_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
        risk = common_cfg.get('risk_per_trade_pct', 50.0) / 100.0
        
        # 레버리지는 듀얼모드 설정을 따르거나 공통 설정을 따름 (사용자 요청: 자산/코인/TP/SL)
        # 문맥상 레버리지도 공통을 따르는 것이 안전하나, 명시적 요청은 없었음. 
        # 하지만 다른 설정들이 공통을 따르므로 레버리지도 공통을 읽는 것이 일관적임.
        lev = common_cfg.get('leverage', 5)
        
        # 레버리지 설정
        await asyncio.to_thread(self.exchange.set_leverage, lev, symbol)
        
        cost = free_usdt * risk
        qty = self.safe_amount(symbol, (cost * lev) / price)
        
        if float(qty) > 0:
            actual_side = 'buy' if side == 'long' else 'sell'
            
            # 1. 진입 주문
            order = await asyncio.to_thread(
                self.exchange.create_order, symbol, 'market', actual_side, qty
            )
            entry_price = float(order.get('average', price))
            self.position_cache = None
            self.db.log_trade_entry(symbol, side, entry_price, float(qty))
            
            # 2. TP/SL 설정 (공통 설정 값 사용)
            tp_pct = common_cfg.get('target_roe_pct', 0.0)
            sl_pct = common_cfg.get('stop_loss_pct', 0.0)
            
            msg = f"🚀 [DualMode] {side.upper()} 진입: {qty} @ {entry_price}"
            
            # TP/SL 주문 배치 (비동기 오류 방지를 위해 try-except)
            if tp_pct > 0 or sl_pct > 0:
                try:
                    # 기존 오픈 오더 취소
                    await asyncio.to_thread(self.exchange.cancel_all_orders, symbol)
                    
                    if side == 'long':
                        if tp_pct > 0:
                            tp_price = entry_price * (1 + tp_pct / 100 / lev)
                            await asyncio.to_thread(self.exchange.create_order, symbol, 'limit', 'sell', qty, tp_price)
                            msg += f" | TP: {tp_price:.2f}"
                        if sl_pct > 0:
                            sl_price = entry_price * (1 - sl_pct / 100 / lev)
                            # Stop Market
                            params = {'stopPrice': sl_price}
                            await asyncio.to_thread(self.exchange.create_order, symbol, 'market', 'sell', qty, params=params)
                            msg += f" | SL: {sl_price:.2f}"
                    else: # short
                        if tp_pct > 0:
                            tp_price = entry_price * (1 - tp_pct / 100 / lev)
                            await asyncio.to_thread(self.exchange.create_order, symbol, 'limit', 'buy', qty, tp_price)
                            msg += f" | TP: {tp_price:.2f}"
                        if sl_pct > 0:
                            sl_price = entry_price * (1 + sl_pct / 100 / lev)
                            # Stop Market
                            params = {'stopPrice': sl_price}
                            await asyncio.to_thread(self.exchange.create_order, symbol, 'market', 'buy', qty, params=params)
                            msg += f" | SL: {sl_price:.2f}"
                except Exception as e:
                    logger.error(f"TP/SL Order Failed: {e}")
                    msg += f" | ⚠️ TP/SL Error"

            await self.ctrl.notify(msg)

    async def exit_position(self, symbol, reason):
        pos = await self.get_server_position(symbol)
        if pos:
            qty = self.safe_amount(symbol, abs(float(pos['contracts'])))
            await asyncio.to_thread(
                self.exchange.create_order, symbol, 'market', 'sell', qty
            )
            pnl = float(pos.get('unrealizedPnl', 0))
            self.db.log_trade_close(symbol, pnl, 0, 0, reason)
            await self.ctrl.notify(f"🧹 [DualMode] 청산 [{reason}]: PnL {pnl:.2f}")
            self.position_cache = None

    async def _update_status(self, symbol, price, tf):
        total, free, mmr = await self.get_balance_info()
        count, daily_pnl = self.db.get_daily_stats()
        pos = await self.get_server_position(symbol)
        
        self.ctrl.status_data = {
            'engine': 'DualMode', 'symbol': symbol, 'price': price,
            'total_equity': total, 'free_usdt': free, 'mmr': mmr,
            'daily_count': count, 'daily_pnl': daily_pnl,
            'pos_side': pos['side'].upper() if pos else 'NONE',
            'entry_price': float(pos['entryPrice']) if pos else 0.0,
            'pnl_usdt': float(pos['unrealizedPnl']) if pos else 0.0,
            'pnl_pct': float(pos['percentage']) if pos else 0.0,
            # DualMode 전용
            'dm_mode': self.current_mode.upper(),
            'dm_tf': tf
        }


# ---------------------------------------------------------
# 3. 메인 컨트롤러
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
            'signal': SignalEngine(self), 
            'shannon': ShannonEngine(self), 
            'dualthrust': DualThrustEngine(self),
            'dualmode': DualModeFractalEngine(self),
            'tema': TemaEngine(self)
        }
        self.active_engine = None
        self.tg_app = None
        self.status_data = {}
        self.is_paused = True  # 봇 시작 시 일시정지 상태 (설정 조절 후 RESUME)
        self.dashboard_msg_id = None
        self.blink_state = False
        self.last_hourly_report = 0

    async def run(self):
        logger.info("🟢 Bot Starting... (Pure Polling Mode)")
        
        if self.cfg.get('logging', {}).get('debug_mode', False):
            logger.setLevel(logging.DEBUG)
            logger.info("🐞 Debug Mode Enabled")
        
        token = self.cfg.get('telegram', {}).get('token', '')
        if not token:
            logger.error("❌ Telegram token is missing!")
            return
        
        self.tg_app = ApplicationBuilder().token(token).build()
        await self._setup_telegram()
        
        await self._switch_engine(self.cfg.get('system_settings', {}).get('active_engine', 'shannon'))
        
        await self.tg_app.initialize()
        await self.tg_app.start()
        await self.tg_app.updater.start_polling()
        
        # 시작 시 일시정지 상태 알림
        await self.notify("⏸ **봇 시작됨 (일시정지 상태)**\n\n설정 조절 후 ▶ RESUME을 눌러주세요!")
        
        await asyncio.gather(
            self._main_polling_loop(),  # [폴링 전용] 메인 폴링 루프
            self._dashboard_loop(),
            self._hourly_report_loop()
        )

    async def _switch_engine(self, name):
        if name not in self.engines:
            logger.error(f"Unknown engine: {name}")
            return
        
        if self.active_engine:
            self.active_engine.stop()
        
        # 엔진 전환 시 상태 데이터 초기화
        self.status_data = {}
        
        self.active_engine = self.engines[name]
        self.active_engine.start()
        
        if name == 'shannon':
            sym = self.cfg.get('shannon_engine', {}).get('target_symbol', 'BTC/USDT')
        elif name == 'dualthrust':
            sym = self.cfg.get('dual_thrust_engine', {}).get('target_symbol', 'BTC/USDT')
        elif name == 'dualmode':
            sym = self.cfg.get('dual_mode_engine', {}).get('target_symbol', 'BTC/USDT')
        elif name == 'tema':
            sym = self.cfg.get('tema_engine', {}).get('target_symbol', 'BTC/USDT')
        else:
            watchlist = self.cfg.get('signal_engine', {}).get('watchlist', ['BTC/USDT'])
            sym = watchlist[0] if watchlist else 'BTC/USDT'
        
        await self.active_engine.ensure_market_settings(sym)
        logger.info(f"✅ Active: {name.upper()}")

    def _get_current_symbol(self):
        """현재 활성 엔진의 심볼 반환"""
        eng = self.cfg.get('system_settings', {}).get('active_engine', 'shannon')
        if eng == 'shannon':
            return self.cfg.get('shannon_engine', {}).get('target_symbol', 'BTC/USDT')
        elif eng == 'dualthrust':
            return self.cfg.get('dual_thrust_engine', {}).get('target_symbol', 'BTC/USDT')
        elif eng == 'dualmode':
            return self.cfg.get('dual_mode_engine', {}).get('target_symbol', 'BTC/USDT')
        elif eng == 'tema':
            return self.cfg.get('tema_engine', {}).get('target_symbol', 'BTC/USDT')
        else:
            watchlist = self.cfg.get('signal_engine', {}).get('watchlist', ['BTC/USDT'])
            return watchlist[0] if watchlist else 'BTC/USDT'

    async def reinit_exchange(self, use_testnet: bool):
        """거래소 연결 재초기화 (테스트넷/메인넷 전환)"""
        try:
            # 1. 현재 엔진 정지
            if self.active_engine:
                self.active_engine.stop()
                logger.info("⏹ Engine stopped for exchange reinit")
            
            # 2. 설정 업데이트
            await self.cfg.update_value(['api', 'use_testnet'], use_testnet)
            
            # 3. 새 API 자격증명 로드
            api = self.cfg.get('api', {})
            creds = api.get('testnet', {}) if use_testnet else api.get('mainnet', {})
            
            # 4. 거래소 재초기화
            self.exchange = ccxt.binance({
                'apiKey': creds.get('api_key', ''),
                'secret': creds.get('secret_key', ''),
                'options': {'defaultType': 'future'},
                'enableRateLimit': True
            })
            
            if use_testnet:
                self.exchange.set_sandbox_mode(True)
            
            # 5. 마켓 정보 로드
            await asyncio.to_thread(self.exchange.load_markets)
            
            # 6. 엔진들에 새 exchange 전달
            for engine in self.engines.values():
                engine.exchange = self.exchange
                engine.position_cache = None
                engine.position_cache_time = 0
            
            # 7. 활성 엔진 재시작
            eng_name = self.cfg.get('system_settings', {}).get('active_engine', 'shannon')
            await self._switch_engine(eng_name)
            
            network_name = "테스트넷 🧪" if use_testnet else "메인넷 💰"
            logger.info(f"✅ Exchange reinitialized: {network_name}")
            return True, network_name
            
        except Exception as e:
            logger.error(f"Exchange reinit error: {e}")
            return False, str(e)

    # ---------------- UI: 비상 버튼 최우선 처리 ----------------
    async def global_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text if update.message else ""
        
        if "STOP" in text:
            await self.emergency_stop()
            await update.message.reply_text("🛑 긴급 정지 완료 - 모든 포지션 청산")
            return ConversationHandler.END
        elif "PAUSE" in text:
            self.is_paused = True
            await update.message.reply_text("⏸ 일시정지 (매매 중단, 모니터링 유지)")
            return ConversationHandler.END
        elif "RESUME" in text:
            self.is_paused = False
            # 엔진이 중지된 경우 재시작
            if self.active_engine and not self.active_engine.running:
                self.active_engine.start()
                await update.message.reply_text("▶ 매매 재개 (엔진 재시작됨)")
            else:
                await update.message.reply_text("▶ 매매 재개")
            return ConversationHandler.END
        elif text == "/status":
            self.dashboard_msg_id = None
            await update.message.reply_text("🔄 대시보드 갱신")
            return ConversationHandler.END
        
        return None

    async def show_setup_menu(self, update: Update):
        sys_cfg = self.cfg.get('system_settings', {})
        sig = self.cfg.get('signal_engine', {})
        sha = self.cfg.get('shannon_engine', {})
        dt = self.cfg.get('dual_thrust_engine', {})
        dm = self.cfg.get('dual_mode_engine', {})
        eng = sys_cfg.get('active_engine', 'shannon')
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
            
        status = "🔴 OFF" if self.is_paused else "🟢 ON"
        direction_str = {'both': '양방향', 'long': '롱만', 'short': '숏만'}.get(direction, 'both')
        
        # 안전한 설정 접근
        sig_common = sig.get('common_settings', {})
        
        # SMA 설정 가져오기
        sma_params = sig.get('strategy_params', {}).get('Triple_SMA', {})
        fast_sma = sma_params.get('fast_sma', 2)
        slow_sma = sma_params.get('slow_sma', 10)
        
        # Shannon 설정
        shannon_ratio = int(sha.get('asset_allocation', {}).get('target_ratio', 0.5) * 100)
        grid_enabled = "ON" if sha.get('grid_trading', {}).get('enabled', False) else "OFF"
        grid_size = sha.get('grid_trading', {}).get('order_size_usdt', 200)
        
        # Dual Thrust 설정
        dt_n = dt.get('n_days', 4)
        dt_k1 = dt.get('k1', 0.5)
        dt_k2 = dt.get('k2', 0.5)
        
        # Dual Mode 설정
        dm_mode = dm.get('mode', 'standard').upper()
        dm_tf = dm.get('scalping_tf', '5m') if dm_mode == 'SCALPING' else dm.get('standard_tf', '4h')
        
        # TP/SL 상태
        tp_sl_status = "ON" if sig_common.get('tp_sl_enabled', True) else "OFF"
        
        # Signal 전략 설정 (추가)
        strategy_params = sig.get('strategy_params', {})
        active_strategy = strategy_params.get('active_strategy', 'sma').upper()
        entry_mode = strategy_params.get('entry_mode', 'cross').upper()
        hma_params = strategy_params.get('HMA', {})
        hma_fast = hma_params.get('fast_period', 9)
        hma_slow = hma_params.get('slow_period', 21)
        
        # Scanner 상태
        scanner_enabled = sig_common.get('scanner_enabled', True)
        scanner_status = "ON 📡" if scanner_enabled else "OFF"

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
        
        # 네트워크 상태
        use_testnet = self.cfg.get('api', {}).get('use_testnet', True)
        network_status = "테스트넷 🧪" if use_testnet else "메인넷 💰"
        
        msg = f"""
🔧 **설정 메뉴** (번호 입력)

**현재 상태**: {eng.upper()} | {symbol}

━━━ 공통 설정 ━━━
1. 레버리지 (`{lev}배`)
2. 목표 ROE (`{sig_common.get('target_roe_pct', 20)}%`)
3. 손절 (`{sig_common.get('stop_loss_pct', 10)}%`)
4. 진입 타임프레임 (`{sig_common.get('timeframe', '15m')}`)
41. 청산 타임프레임 (`{sig_common.get('exit_timeframe', '4h')}`)
5. 손실 제한 (`${sha.get('daily_loss_limit', 5000)}`)
6. 진입 비율 (`{sig_common.get('risk_per_trade_pct', 50)}%`)
7. 매매 방향 (`{direction_str}`)
8. 심볼 변경 (`{symbol}`)

━━━ Signal 전용 ━━━
16. 전략 (`{active_strategy}`)
18. 진입모드 (`{entry_mode}`) - SMA/HMA용
10. SMA 기간 (`{fast_sma}/{slow_sma}`)
17. HMA 기간 (`{hma_fast}/{hma_slow}`)
20. VBO 설정 (ATR/돌파/TP/SL)
21. FractalFisher 설정 (Hurst/Fisher/Trailing)
13. TP/SL 자동청산 (`{tp_sl_status}`)
23. 거래량급등채굴 (`{scanner_status}`)

**필터 (Entry / Exit)**
26. R2 필터 (`{r2_entry}` / `{r2_exit}`) (기준: `{r2_threshold}`)
28. Hurst 필터 (`{hurst_entry}` / `{hurst_exit}`) (기준: `{hurst_threshold}`)
30. CHOP 필터 (`{chop_entry}` / `{chop_exit}`) (기준: `{chop_threshold}`)
32. Kalman 필터 (`{kalman_entry}` / `{kalman_exit}`)

27. R2 민감도 설정
29. Hurst 민감도 설정
31. CHOP 민감도 설정

━━━ Shannon 전용 ━━━
11. 자산 비율 (`{shannon_ratio}%`)
12. Grid 설정 (`{grid_enabled}`, ${grid_size})

━━━ Dual Thrust 전용 ━━━
14. N Days (`{dt_n}일`)
15. K1/K2 (`{dt_k1}/{dt_k2}`)

━━━ Dual Mode 전용 ━━━
35. 모드 변경 ({dm_mode}, {dm_tf})

━━━ 시스템 ━━━
22. 네트워크 전환 (`{network_status}`)
25. 시간별 리포트 (`{hourly_report_status}`)


━━━ 제어 ━━━
00. 🔀 엔진 교체 (현재: {eng.upper()})
9. ▶️ 매매시작/중지 ({status})
0. 나가기
"""
        await update.message.reply_text(msg.strip(), parse_mode=ParseMode.MARKDOWN)

    async def setup_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.show_setup_menu(update)
        return SELECT

    async def setup_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        
        if text == '0':
            await update.message.reply_text("✅ 설정 종료")
            return ConversationHandler.END
        
        context.user_data['setup_choice'] = text
        
        prompts = {
            '1': "📝 **레버리지** 값을 입력하세요 (1~5배, 예: 5)",
            '2': "📝 **목표 ROE** (%)를 입력하세요 (예: 20)",
            '3': "📝 **손절 비율** (%)를 입력하세요 (예: 5)",
            '4': "📝 **진입 타임프레임**을 입력하세요 (예: 15m)\n1m,2m,3m,5m,15m,30m | 1h,2h,4h | 1d",
            '41': "📝 **청산 타임프레임**을 입력하세요 (예: 1h)\n1m,2m,3m,5m,15m,30m | 1h,2h,4h | 1d",
            '5': "📝 **일일 손실 제한** ($)을 입력하세요 (예: 1000)",
            '6': "💰 **진입 비율(%)**을 입력하세요 (예: 50 -> 자산의 50% 진입)",
            '7': "↕️ **매매 방향**을 선택하세요 (1=양방향, 2=롱만, 3=숏만)",
            '8': "💱 **변경할 코인 심볼**을 입력하거나 번호를 선택하세요.\n\n1: BTC  2: ETH  3: SOL\n또는 직접 입력 (예: **DOGE**, **XRP**, **PEPE**)",
            '9': "▶️ 매매를 시작하시겠습니까? (1=시작, 0=중지)",
            '10': "📈 **SMA 기간**을 입력하세요 (형식: fast,slow 예: 2,10)",
            '11': "📝 **자산 비율** (%)를 입력하세요 (예: 50 = 50%)",
            '12': "📝 **Grid 설정**을 입력하세요 (예: on,200 또는 off)",
            '14': "📝 **N Days** (Range 계산 일수)를 입력하세요 (예: 4)",
            '15': "📝 **K1/K2** (배수)를 입력하세요 (예: 0.5,0.5 또는 0.4,0.6)",
            '16': "📝 **전략 선택** (1=SMA, 2=HMA, 3=MicroVBO, 4=FractalFisher)",
            '17': "📝 **HMA 기간**을 입력하세요 (예: 9,21)",
            '18': "📝 **진입모드**를 선택하세요 (1=Cross, 2=Position)",
            '20': "📝 **VBO 설정**을 입력하세요 (형식: atr기간,돌파배수,TP배수,SL배수 예: 14,0.5,1.0,0.5)",
            '21': "📝 **FractalFisher 설정**을 입력하세요 (형식: hurst기간,hurst임계,fisher기간,trailing배수 예: 100,0.55,10,2.0)",
            '22': "📝 **네트워크 선택** (1=테스트넷, 2=메인넷)",
            '23': "📝 **거래량 급등 채굴 기능**을 켜시겠습니까? (1=ON, 0=OFF)",
            '26': "📝 **추세 필터($R^2$) 기능**을 켜시겠습니까? (1=ON, 0=OFF)", # Toggle이므로 실제로는 사용되지 않을 수 있으나 prompt dict 구색 맞춤
            '27': "📝 **$R^2$ 기준값**을 입력하세요 (0.1 ~ 0.5 권장)\n- 낮을수록(0.1): 진입 자주 함 (노이즈 허용)\n- 높을수록(0.4): 확실한 추세만 진입 (진입 감소)",
            '28': "📝 **Hurst 필터**를 켜시겠습니까? (1=ON, 0=OFF)", 
            '29': "📝 **Hurst 기준값**을 입력하세요 (예: 0.55)\n- 0.5 이하는 평균회귀(횡보), 0.5 이상은 추세.\n- 높을수록 강한 추세만 진입.",
            '30': "📝 **CHOP 필터**를 켜시겠습니까? (1=ON, 0=OFF)",
            '31': "📝 **CHOP 기준값**을 입력하세요 (예: 50.0)\n- 100에 가까울수록 횡보(Choppy).\n- 설정값 **보다 크면** 진입 금지.",
            '35': "📝 **Dual Mode 변경** (1=Standard, 2=Scalping)",
        }
        if text == '7':
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
사용할 엔진 번호를 선택하세요:

1. 📡 **Signal Engine**
   - 전략 기반 (SMA, HMA, VBO 등)
   - 다양한 알트코인 지원

2. ⚖️ **Shannon Engine**
   - 자산 배분 & 리밸런싱
   - 변동성 활용 (Grid Trading)

3. 💥 **Dual Thrust Engine**
   - 변동성 돌파 전략
   - 추세 추종

4. ⚛️ **Dual Mode Engine**
   - Fractal Choppiness + Kalman
   - Scalping / Standard 모드

5. 🌩️ **TEMA Engine**
   - RSI + TEMA + Bollinger Strategy
   - 빠른 반응 속도 (공통 설정 공유)
"""
            await update.message.reply_text(msg.strip(), parse_mode=ParseMode.MARKDOWN)
            return ENGINE_SELECT

        elif text == '9':
            self.is_paused = not self.is_paused
            await self.show_setup_menu(update)
            return SELECT
        elif text == '13':
            # TP/SL 토글
            current = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('tp_sl_enabled', True)
            new_val = not current
            await self.cfg.update_value(['signal_engine', 'common_settings', 'tp_sl_enabled'], new_val)
            status = "ON" if new_val else "OFF"
            await update.message.reply_text(f"✅ TP/SL 자동청산: {status}")
            await self.show_setup_menu(update)
            return SELECT
        elif text == '25':
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
                "📉 **R2 필터 설정**:\n1. Entry 필터 토글\n2. Exit 필터 토글",
                reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
            )
            return "R2_SELECT"

        elif text == '28':
            # Hurst Filter Menu
            keyboard = [[KeyboardButton("1. Entry Toggle"), KeyboardButton("2. Exit Toggle")]]
            await update.message.reply_text(
                "🌊 **Hurst 필터 설정**:\n1. Entry 필터 토글\n2. Exit 필터 토글",
                reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
            )
            return "HURST_SELECT"

        elif text == '30':
            # Chop Filter Menu
            keyboard = [[KeyboardButton("1. Entry Toggle"), KeyboardButton("2. Exit Toggle")]]
            await update.message.reply_text(
                "🌀 **Chop 필터 설정**:\n1. Entry 필터 토글\n2. Exit 필터 토글",
                reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
            )
            return "CHOP_SELECT"

        elif text == '32':
            # Kalman Filter Menu
            keyboard = [[KeyboardButton("1. Entry Toggle"), KeyboardButton("2. Exit Toggle")]]
            await update.message.reply_text(
                "🚀 **Kalman 필터 설정**:\n1. Entry 필터 토글\n2. Exit 필터 토글",
                reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
            )
            return "KALMAN_SELECT"

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
            await update.message.reply_text("❌ 잘못된 번호입니다.")
            return SELECT

    async def handle_manual_symbol_input(self, update: Update, symbol: str):
        """[New] 텔레그램 수동 심볼 입력 처리"""
        try:
            # 심볼 포맷팅 (BTC -> BTC/USDT)
            if '/' not in symbol:
                symbol = f"{symbol}/USDT"
            
            # 심볼 유효성 검사 (Exchange check)
            # SignalEngine이 활성화되어 있어야 함
            eng_type = self.cfg.get('system_settings', {}).get('active_engine', 'shannon')
            if eng_type != 'signal':
                await update.message.reply_text("⚠️ 현재 Signal 엔진이 활성화되어 있지 않습니다. (/strat 1)")
                return

            signal_engine = self.engines.get('signal')
            if not signal_engine:
                await update.message.reply_text("❌ Signal 엔진을 찾을 수 없습니다.")
                return

            # 심볼 검증 (exchange load_markets 필요할 수 있음, 여기선 try fetch ticker로 대체)
            try:
                await asyncio.to_thread(self.exchange.fetch_ticker, symbol)
            except Exception:
                await update.message.reply_text(f"❌ 유효하지 않은 심볼입니다: {symbol}")
                return

            # Active Symbols에 추가
            if symbol not in signal_engine.active_symbols:
                signal_engine.active_symbols.add(symbol)
                await update.message.reply_text(f"✅ **{symbol}** 감시 시작! (수동 입력)")
                logger.info(f"Manual symbol added: {symbol}")
                
                # 즉시 폴링 트리거 (선택 사항)
                # await signal_engine.poll_symbol(symbol) 
            else:
                await update.message.reply_text(f"ℹ️ 이미 감시 중인 심볼입니다: {symbol}")

        except Exception as e:
            logger.error(f"Manual input error: {e}")
            await update.message.reply_text(f"❌ 처리 실패: {e}")

    async def setup_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        choice = context.user_data.get('setup_choice')
        val = update.message.text
        
        try:
            if choice == '1':
                v = int(val)
                # 레버리지 최대 20배 제한 (사용자 요청: 5 -> 20)
                if v < 1 or v > 20:
                    await update.message.reply_text("❌ 레버리지는 1~20배 사이만 가능합니다.")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'leverage'], v)
                await self.cfg.update_value(['shannon_engine', 'leverage'], v)
                await self.cfg.update_value(['dual_thrust_engine', 'leverage'], v)
                await self.cfg.update_value(['dual_mode_engine', 'leverage'], v)
                # TEMA는 common_settings를 참조하므로 별도 업데이트 불필요하지만, 
                # 활성 엔진이 TEMA일 경우 market settings 즉시 적용 필요
                if self.active_engine:
                    sym = self._get_current_symbol()
                    await self.active_engine.ensure_market_settings(sym)
                await update.message.reply_text(f"✅ 레버리지 변경: {v}x")
            elif choice == '2':
                await self.cfg.update_value(['signal_engine', 'common_settings', 'target_roe_pct'], float(val))
                await self.cfg.update_value(['dual_mode_engine', 'target_roe_pct'], float(val))
            elif choice == '3':
                await self.cfg.update_value(['signal_engine', 'common_settings', 'stop_loss_pct'], float(val))
                await self.cfg.update_value(['dual_thrust_engine', 'stop_loss_pct'], float(val))
                await self.cfg.update_value(['dual_mode_engine', 'stop_loss_pct'], float(val))
            elif choice == '4':
                # 타임프레임 유효성 검사
                valid_tf = ['1m', '2m', '3m', '4m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', '1d', '3d', '1w', '1M']
                if val not in valid_tf:
                    await update.message.reply_text(f"❌ 유효하지 않은 타임프레임.\n사용 가능: {', '.join(valid_tf)}")
                    return SELECT
                # 타임프레임 업데이트 (Common, Signal, Shannon 모두 적용)
                await self.cfg.update_value(['signal_engine', 'common_settings', 'timeframe'], val)
                await self.cfg.update_value(['signal_engine', 'common_settings', 'entry_timeframe'], val) # Sync Entry TF
                await self.cfg.update_value(['shannon_engine', 'timeframe'], val)
                
                # DualMode 타임프레임 변경 (현재 모드에 맞춰서)
                dm_mode = self.cfg.get('dual_mode_engine', {}).get('mode', 'standard')
                if dm_mode == 'scalping':
                    await self.cfg.update_value(['dual_mode_engine', 'scalping_tf'], val)
                else:
                    await self.cfg.update_value(['dual_mode_engine', 'standard_tf'], val)

                # Shannon 엔진의 200 EMA 캐시 초기화 (새 타임프레임 즉시 적용)
                if self.active_engine and hasattr(self.active_engine, 'ema_200'):
                    self.active_engine.ema_200 = None
                    self.active_engine.trend_direction = None
                    self.active_engine.last_indicator_update = 0
                # Signal 엔진 캐시도 초기화
                signal_engine = self.engines.get('signal')
                if signal_engine:
                    signal_engine.last_processed_candle_ts = {}
                    signal_engine.last_candle_time = {}
                await update.message.reply_text(f"✅ 진입 타임프레임 변경: {val}")
                # DualMode 엔진 캐시 초기화
                dm_engine = self.engines.get('dualmode')
                if dm_engine:
                    dm_engine.last_candle_ts = 0
                
                await update.message.reply_text(f"✅ 타임프레임 변경: {val} (DualMode: {dm_mode} TF 적용)")
            elif choice == '5':
                await self.cfg.update_value(['shannon_engine', 'daily_loss_limit'], float(val))
                await self.cfg.update_value(['signal_engine', 'common_settings', 'daily_loss_limit'], float(val))
                await self.cfg.update_value(['dual_thrust_engine', 'daily_loss_limit'], float(val))
                await self.cfg.update_value(['dual_mode_engine', 'daily_loss_limit'], float(val))
            elif choice == '6':
                v = float(val)
                if v < 1 or v > 100:
                    await update.message.reply_text("❌ 1~100 사이의 값을 입력하세요.")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'risk_per_trade_pct'], v)
                await self.cfg.update_value(['dual_thrust_engine', 'risk_per_trade_pct'], v)
                await self.cfg.update_value(['dual_mode_engine', 'risk_per_trade_pct'], v)
            
            # ======== Signal (SMA) 전용 ========
            elif choice == '10':
                # SMA 기간 변경 (형식: "2,10" 또는 "5,25")
                parts = val.replace(' ', '').split(',')
                if len(parts) != 2:
                    await update.message.reply_text("❌ 형식: fast,slow (예: 2,10)")
                    return SELECT
                fast, slow = int(parts[0]), int(parts[1])
                if fast >= slow:
                    await update.message.reply_text("❌ fast SMA가 slow보다 작아야 합니다.")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'Triple_SMA', 'fast_sma'], fast)
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'Triple_SMA', 'slow_sma'], slow)
                await update.message.reply_text(f"✅ SMA 기간 변경: {fast}/{slow}")
            
            # ======== Shannon 전용 ========
            elif choice == '11':
                # 자산 비율 변경 (형식: "50" = 50%)
                v = float(val)
                if v < 10 or v > 90:
                    await update.message.reply_text("❌ 10~90 사이의 값을 입력하세요.")
                    return SELECT
                ratio = v / 100.0
                await self.cfg.update_value(['shannon_engine', 'asset_allocation', 'target_ratio'], ratio)
                # Shannon 엔진에 즉시 적용
                shannon_engine = self.engines.get('shannon')
                if shannon_engine:
                    shannon_engine.ratio = ratio
                await update.message.reply_text(f"✅ Shannon 자산 비율 변경: {int(v)}%")
            
            elif choice == '12':
                # Grid 설정 (형식: "on,200" 또는 "off")
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
            
            # ======== Dual Thrust 전용 ========
            elif choice == '14':
                # N Days 변경
                v = int(val)
                if v < 1 or v > 30:
                    await update.message.reply_text("❌ 1~30 사이의 값을 입력하세요.")
                    return SELECT
                await self.cfg.update_value(['dual_thrust_engine', 'n_days'], v)
                # 엔진 캐시 초기화 (새 N으로 트리거 재계산)
                dt_engine = self.engines.get('dualthrust')
                if dt_engine:
                    dt_engine.trigger_date = None
                await update.message.reply_text(f"✅ Dual Thrust N Days 변경: {v}일")
            
            elif choice == '15':
                # K1/K2 변경 (형식: "0.5,0.5" 또는 "0.4,0.6")
                parts = val.replace(' ', '').split(',')
                if len(parts) != 2:
                    await update.message.reply_text("❌ 형식: k1,k2 (예: 0.5,0.5)")
                    return SELECT
                k1, k2 = float(parts[0]), float(parts[1])
                if k1 <= 0 or k1 > 1 or k2 <= 0 or k2 > 1:
                    await update.message.reply_text("❌ K1, K2는 0~1 사이의 값이어야 합니다.")
                    return SELECT
                await self.cfg.update_value(['dual_thrust_engine', 'k1'], k1)
                await self.cfg.update_value(['dual_thrust_engine', 'k2'], k2)
                # 엔진 캐시 초기화 (새 K로 트리거 재계산)
                dt_engine = self.engines.get('dualthrust')
                if dt_engine:
                    dt_engine.trigger_date = None
                await update.message.reply_text(f"✅ Dual Thrust K1/K2 변경: {k1}/{k2}")
            # ======== Signal 신규 옵션 ========
            elif choice == '16':
                # 전략 변경 (번호 또는 이름으로 선택)
                strategy_map = {'1': 'sma', '2': 'hma', '3': 'microvbo', '4': 'fractalfisher'}
                val_lower = val.lower().strip()
                
                # 번호 입력 시 변환
                if val_lower in strategy_map:
                    val_lower = strategy_map[val_lower]
                
                if val_lower in ['sma', 'hma', 'microvbo', 'fractalfisher']:
                    await self.cfg.update_value(['signal_engine', 'strategy_params', 'active_strategy'], val_lower)
                    signal_engine = self.engines.get('signal')
                    # 전략별 상태 초기화
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
                    await update.message.reply_text(f"✅ 전략 변경: {val_lower.upper()}")
                else:
                    await update.message.reply_text("❌ 1~4 또는 sma/hma/microvbo/fractalfisher 입력\n1=SMA, 2=HMA, 3=MicroVBO, 4=FractalFisher")
                    return SELECT
            
            elif choice == '20':
                # VBO 설정 변경 (형식: "atr기간,돌파배수,TP배수,SL배수")
                parts = val.replace(' ', '').split(',')
                if len(parts) != 4:
                    await update.message.reply_text("❌ 형식: atr,돌파,TP,SL (예: 14,0.5,1.0,0.5)")
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
                    
                    await update.message.reply_text(f"✅ VBO 설정: ATR={atr_period}, 돌파={breakout_mult}, TP={tp_mult}x, SL={sl_mult}x")
                except ValueError:
                    await update.message.reply_text("❌ 올바른 숫자를 입력하세요.")
                    return SELECT
            
            elif choice == '21':
                # FractalFisher 설정 변경 (형식: "hurst기간,hurst임계,fisher기간,trailing배수")
                parts = val.replace(' ', '').split(',')
                if len(parts) != 4:
                    await update.message.reply_text("❌ 형식: hurst기간,hurst임계,fisher기간,trailing배수 (예: 100,0.55,10,2.0)")
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
                    
                    await update.message.reply_text(f"✅ FractalFisher: Hurst={hurst_period}봉/{hurst_threshold}, Fisher={fisher_period}, Trailing={trailing_mult}x ATR")
                except ValueError:
                    await update.message.reply_text("❌ 올바른 숫자를 입력하세요.")
                    return SELECT
            
            elif choice == '17':
                # HMA 기간 변경 (형식: "9,21")
                parts = val.replace(' ', '').split(',')
                if len(parts) != 2:
                    await update.message.reply_text("❌ 형식: fast,slow (예: 9,21)")
                    return SELECT
                fast, slow = int(parts[0]), int(parts[1])
                if fast >= slow:
                    await update.message.reply_text("❌ fast HMA가 slow보다 작아야 합니다.")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'HMA', 'fast_period'], fast)
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'HMA', 'slow_period'], slow)
                await update.message.reply_text(f"✅ HMA 기간 변경: {fast}/{slow}")
            
            elif choice == '18':
                # 진입모드 변경 (1=cross, 2=position)
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
                # 네트워크 전환 (1=테스트넷, 2=메인넷)
                if val not in ['1', '2']:
                    await update.message.reply_text("❌ 1 또는 2를 입력하세요.\n1=테스트넷, 2=메인넷")
                    return SELECT
                
                use_testnet = val == '1'
                current = self.cfg.get('api', {}).get('use_testnet', True)
                
                if use_testnet == current:
                    network_name = "테스트넷 🧪" if use_testnet else "메인넷 💰"
                    await update.message.reply_text(f"ℹ️ 이미 {network_name} 사용 중입니다.")
                else:
                    # 전환 확인 메시지
                    target_name = "테스트넷 🧪" if use_testnet else "메인넷 💰"
                    await update.message.reply_text(f"🔄 {target_name}(으)로 전환 중...")
                    
                    success, result = await self.reinit_exchange(use_testnet)
                    
                    if success:
                        await update.message.reply_text(f"✅ 네트워크 전환 완료: {result}")
                    else:
                        await update.message.reply_text(f"❌ 네트워크 전환 실패: {result}")
            
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
                    await update.message.reply_text("❌ 0.01 ~ 0.9 사이의 값을 입력하세요.")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'r2_threshold'], v)
                await update.message.reply_text(f"✅ 추세 필터 민감도 변경: {v}")

            elif choice == '29':
                # Hurst Threshold
                v = float(val)
                if v < 0.0 or v > 1.0:
                    await update.message.reply_text("❌ 0.0 ~ 1.0 사이의 값을 입력하세요.")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'hurst_threshold'], v)
                await update.message.reply_text(f"✅ Hurst 민감도 변경: {v}")

            elif choice == '31':
                # CHOP Threshold
                v = float(val)
                if v < 0 or v > 100:
                    await update.message.reply_text("❌ 0 ~ 100 사이의 값을 입력하세요.")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'chop_threshold'], v)
                await update.message.reply_text(f"✅ CHOP 민감도 변경: {v}")

            elif choice == '35':
                # Dual Mode 변경
                if val == '1':
                    new_mode = 'standard'
                elif val == '2':
                    new_mode = 'scalping'
                else:
                    await update.message.reply_text("❌ 1(Standard) 또는 2(Scalping)을 입력하세요.")
                    return SELECT
                
                await self.cfg.update_value(['dual_mode_engine', 'mode'], new_mode)
                await update.message.reply_text(f"✅ Dual Mode 변경: {new_mode.upper()}")
                
                # 즉시 재초기화 트리거 (poll_tick에서 감지하지만 명시적 리셋)
                dm_engine = self.engines.get('dualmode')
                if dm_engine:
                    dm_engine._init_strategy()
            
            elif choice == '41':
                # 청산 타임프레임 변경
                valid_tf = ['1m', '2m', '3m', '4m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', '1d', '3d', '1w', '1M']
                if val not in valid_tf:
                    await update.message.reply_text(f"❌ 유효하지 않은 타임프레임.\n사용 가능: {', '.join(valid_tf)}")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'exit_timeframe'], val)
                # Signal 엔진 캐시 초기화
                signal_engine = self.engines.get('signal')
                if signal_engine:
                    signal_engine.last_processed_exit_candle_ts = {}
                await update.message.reply_text(f"✅ 청산 타임프레임 변경: {val}")
            
            # 10~41 success message handled
            if choice not in ['10', '11', '12', '14', '15', '16', '17', '18', '20', '21', '22', '23', '26', '27', '28', '29', '30', '31', '35', '41']:
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
        """심볼 변경 처리 - 1/2/3 단축키 또는 직접 입력"""
        choice = update.message.text.strip().upper()
        
        # 1/2/3 번호로 심볼 매핑 (단축키)
        symbol_map = {
            '1': 'BTC/USDT',
            '2': 'ETH/USDT',
            '3': 'SOL/USDT'
        }
        
        # 단축키 또는 직접 입력 사용
        if choice in symbol_map:
            symbol = symbol_map[choice]
        else:
            # 직접 입력한 경우 포맷 확인
            symbol = choice
            if '/' not in symbol:
                symbol = f"{symbol}/USDT"
            
            # 유효성 검사 (간단히 Ticker 조회)
            try:
                await asyncio.to_thread(self.exchange.fetch_ticker, symbol)
            except Exception:
                await update.message.reply_text(f"❌ 유효하지 않은 심볼 또는 거래소 미지원: {symbol}\n(예: BTC/USDT 또는 그냥 BTC)")
                return SELECT
        
        try:
            eng = self.cfg.get('system_settings', {}).get('active_engine', 'shannon')
            if eng == 'shannon':
                await self.cfg.update_value(['shannon_engine', 'target_symbol'], symbol)
            elif eng == 'dualthrust':
                await self.cfg.update_value(['dual_thrust_engine', 'target_symbol'], symbol)
            elif eng == 'dualmode':
                await self.cfg.update_value(['dual_mode_engine', 'target_symbol'], symbol)
            elif eng == 'tema':
                await self.cfg.update_value(['tema_engine', 'target_symbol'], symbol)
            else:
                # Signal 엔진: 메뉴에서 변경 시 Watchlist를 해당 심볼로 **대체** (기존 동작 유지)
                # 다중 감시를 원하면 메뉴가 아니라 채팅창에서 추가해야 함을 안내
                await self.cfg.update_value(['signal_engine', 'watchlist'], [symbol])
                await update.message.reply_text("ℹ️ Signal 엔진의 감시 목록이 이 심볼로 초기화되었습니다.\n(추가를 원하시면 메뉴 밖에서 심볼을 입력하세요)")
            
            # 마켓 설정 적용
            # 마켓 설정 적용
            if self.active_engine:
                await self.active_engine.ensure_market_settings(symbol)
            
            # Shannon 엔진 캐시 초기화 (심볼 변경 시 필수!)
            shannon_engine = self.engines.get('shannon')
            if shannon_engine:
                shannon_engine.ema_200 = None
                shannon_engine.atr_value = None
                shannon_engine.trend_direction = None
                shannon_engine.last_indicator_update = 0
                shannon_engine.position_cache = None
                shannon_engine.grid_orders = []
                logger.info(f"🔄 Shannon engine cache cleared for new symbol: {symbol}")
            
            # Signal 엔진 캐시도 초기화
            signal_engine = self.engines.get('signal')
            if signal_engine:
                signal_engine.position_cache = None
                signal_engine.last_candle_time = {} # Dict reset
                signal_engine.last_processed_candle_ts = {} 
                signal_engine.active_symbols.clear() # 기존 수동 목록도 초기화 (명확성을 위해)
                signal_engine.active_symbols.add(symbol)
            
            # Dual Thrust 엔진 캐시도 초기화
            dt_engine = self.engines.get('dualthrust')
            if dt_engine:
                dt_engine.position_cache = None
                dt_engine.trigger_date = None  # 트리거 재계산
                logger.info(f"🔄 DualThrust engine cache cleared for new symbol: {symbol}")

            # TEMA 엔진 캐시 초기화
            tema_engine = self.engines.get('tema')
            if tema_engine:
                tema_engine.last_candle_time = 0
                tema_engine.ema1 = None
                tema_engine.ema2 = None
                tema_engine.ema3 = None
                logger.info(f"🔄 TEMA engine cache cleared for new symbol: {symbol}")
            
            await update.message.reply_text(f"✅ 심볼 변경 완료: {symbol}")
            await self._restore_main_keyboard(update)
            await self.show_setup_menu(update)
            return SELECT
            
        except Exception as e:
            logger.error(f"Symbol change error: {e}")
            await update.message.reply_text(f"❌ 심볼 변경 실패: {e}")
            return SELECT

    async def setup_direction_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """매매 방향 선택 처리"""
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
            await update.message.reply_text(f"✅ 매매 방향 변경: {direction}")
        else:
            await update.message.reply_text("❌ 유효하지 않은 선택")
        
        await self._restore_main_keyboard(update)
        await self.show_setup_menu(update)
        await self.show_setup_menu(update)
        return SELECT

    async def setup_engine_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """엔진 교체 처리"""
        text = update.message.text.strip()
        
        mode_map = {'1': 'signal', '2': 'shannon', '3': 'dualthrust', '4': 'dualmode', '5': 'tema'}
        
        if text in mode_map:
            mode = mode_map[text]
            await self.cfg.update_value(['system_settings', 'active_engine'], mode)
            await self._switch_engine(mode)
            self.dashboard_msg_id = None
            await update.message.reply_text(f"✅ 엔진 변경 완료: {mode.upper()}")
        else:
            await update.message.reply_text("❌ 잘못된 선택입니다. (1~4 입력)")
        
        await self._restore_main_keyboard(update)
        await self.show_setup_menu(update)
        return SELECT

    async def _restore_main_keyboard(self, update: Update):
        """메인 키보드 복원"""
        kb = [
            [KeyboardButton("🚨 STOP"), KeyboardButton("⏸ PAUSE"), KeyboardButton("▶ RESUME")],
            [KeyboardButton("/setup"), KeyboardButton("/status"), KeyboardButton("/log")]
        ]
        markup = ReplyKeyboardMarkup(kb, resize_keyboard=True)
        await update.message.reply_text("📱 메인 메뉴", reply_markup=markup)

    async def _setup_telegram(self):
        kb = [
            [KeyboardButton("🚨 STOP"), KeyboardButton("⏸ PAUSE"), KeyboardButton("▶ RESUME")],
            [KeyboardButton("/setup"), KeyboardButton("/status"), KeyboardButton("/log")]
        ]
        markup = ReplyKeyboardMarkup(kb, resize_keyboard=True)

        async def start_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            await u.message.reply_text("🤖 봇 준비 완료", reply_markup=markup)

        async def strat_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            if not c.args:
                await u.message.reply_text("사용법: /strat 번호\n1: Signal\n2: Shannon\n3: DualThrust\n4: DualMode\n5: TEMA")
                return
            arg = c.args[0]
            if arg == '1':
                mode = 'signal'
            elif arg == '2':
                mode = 'shannon'
            elif arg == '3':
                mode = 'dualthrust'
            elif arg == '4':
                mode = 'dualmode'
            else:
                await u.message.reply_text("❌ 잘못된 입력. 1~4 사이의 번호를 입력하세요.")
                return
            await self.cfg.update_value(['system_settings', 'active_engine'], mode)
            await self._switch_engine(mode)
            self.dashboard_msg_id = None
            await u.message.reply_text(f"✅ 전략 변경: {mode.upper()}")

        async def log_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            logs = list(log_buffer)[-15:]
            if logs:
                await u.message.reply_text("\n".join(logs))
            else:
                await u.message.reply_text("📝 로그가 비어있습니다.")

        async def close_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            await self.emergency_stop()
            await u.message.reply_text("🛑 긴급 정지 완료")

        async def stats_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            """통계 명령어"""
            daily_count, daily_pnl = self.db.get_daily_stats()
            weekly_count, weekly_pnl = self.db.get_weekly_stats()
            
            msg = f"""
📊 **매매 통계**

**오늘**
- 거래: {daily_count}건
- 손익: ${daily_pnl:+.2f}

**7일간**
- 거래: {weekly_count}건
- 손익: ${weekly_pnl:+.2f}
"""
            await u.message.reply_text(msg.strip(), parse_mode=ParseMode.MARKDOWN)

        # 비상 버튼 핸들러 (최우선)
        emergency_handler = MessageHandler(
            filters.Regex("STOP|PAUSE|RESUME|/status"), 
            self.global_handler
        )
        self.tg_app.add_handler(emergency_handler, group=-1)
        
        # /start 명령어 핸들러 추가
        self.tg_app.add_handler(CommandHandler("start", start_cmd))

        # 설정 대화 핸들러
        conv = ConversationHandler(
            entry_points=[CommandHandler('setup', self.setup_entry)],
            states={
                SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.setup_select)],
                INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.setup_input)],
                SYMBOL_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.setup_symbol_input)],
                DIRECTION_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.setup_direction_select)],
                ENGINE_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.setup_engine_select)],
                "R2_SELECT": [MessageHandler(filters.TEXT & ~filters.COMMAND, self.setup_r2_select)],
                "HURST_SELECT": [MessageHandler(filters.TEXT & ~filters.COMMAND, self.setup_hurst_select)],
                "CHOP_SELECT": [MessageHandler(filters.TEXT & ~filters.COMMAND, self.setup_chop_select)],
                "KALMAN_SELECT": [MessageHandler(filters.TEXT & ~filters.COMMAND, self.setup_kalman_select)]
            },
            fallbacks=[
                CommandHandler('setup', self.setup_entry),
                emergency_handler
            ]
        )
        
        self.tg_app.add_handler(conv)
        
        # [New] 수동 심볼 입력 핸들러 (설정 모드가 아닐 때 동작)
        async def manual_symbol_handler(u: Update, c: ContextTypes.DEFAULT_TYPE):
            text = u.message.text.strip().upper()
            # 간단한 정규식으로 심볼 형태인지 확인 (알파벳 2~5글자 또는 XXX/YYY 형식)
            import re
            if re.match(r'^[A-Z0-9]{2,10}(/[A-Z0-9]{2,10})?(:[A-Z0-9]+)?$', text):
                # /setup 등 커맨드는 제외
                if text.startswith('/'): return
                
                await self.handle_manual_symbol_input(u, text)

        self.tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manual_symbol_handler))

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

    async def setup_hurst_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        if "1" in text:
            curr = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('hurst_entry_enabled', True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'hurst_entry_enabled'], not curr)
            await update.message.reply_text(f"✅ Hurst Entry: {'ON' if not curr else 'OFF'}")
        elif "2" in text:
            curr = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('hurst_exit_enabled', True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'hurst_exit_enabled'], not curr)
            await update.message.reply_text(f"✅ Hurst Exit: {'ON' if not curr else 'OFF'}")
        
        await self._restore_main_keyboard(update)
        await self.show_setup_menu(update)
        return SELECT

    async def setup_chop_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        if "1" in text:
            curr = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('chop_entry_enabled', True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'chop_entry_enabled'], not curr)
            await update.message.reply_text(f"✅ Chop Entry: {'ON' if not curr else 'OFF'}")
        elif "2" in text:
            curr = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('chop_exit_enabled', True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'chop_exit_enabled'], not curr)
            await update.message.reply_text(f"✅ Chop Exit: {'ON' if not curr else 'OFF'}")
        
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

        # CommandHandler("start") 이미 라인 1751에서 등록됨 - 중복 제거
        self.tg_app.add_handler(CommandHandler("strat", strat_cmd))
        self.tg_app.add_handler(CommandHandler("log", log_cmd))
        self.tg_app.add_handler(CommandHandler("close", close_cmd))
        self.tg_app.add_handler(CommandHandler("stats", stats_cmd))
        
        # /help 명령어
        async def help_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            msg = """
📚 **명령어 목록**

🔧 **설정**
/setup - 설정 메뉴
/strat 1 - Signal (SMA) 전략
/strat 2 - Shannon (자산배분) 전략
/strat 3 - DualThrust (변동성돌파) 전략

📊 **정보**
/status - 대시보드 갱신
/stats - 매매 통계
/log - 최근 로그

🚨 **제어**
/close - 긴급 청산
🚨 STOP - 긴급 정지
⏸ PAUSE - 일시정지
▶ RESUME - 재개
"""
            await u.message.reply_text(msg.strip(), parse_mode=ParseMode.MARKDOWN)
        
        self.tg_app.add_handler(CommandHandler("help", help_cmd))

    # ---------------- Hourly Report ----------------
    async def _hourly_report_loop(self):
        """시간별 리포트"""
        await asyncio.sleep(60)  # 시작 대기
        
        while True:
            try:
                reporting = self.cfg.get('telegram', {}).get('reporting', {})
                if reporting.get('hourly_report_enabled', False):
                    now = datetime.now()
                    if now.minute == 0 and time.time() - self.last_hourly_report > 3500:
                        self.last_hourly_report = time.time()
                        
                        daily_count, daily_pnl = self.db.get_daily_stats()
                        d = self.status_data
                        
                        msg = f"""
⏰ **시간별 리포트** [{now.strftime('%H:%M')}]

💰 자산: ${d.get('total_equity', 0):.2f}
📈 오늘 손익: ${daily_pnl:+.2f} ({daily_count}건)
📊 MMR: {d.get('mmr', 0):.2f}%
"""
                        await self.notify(msg.strip())
                
                await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"Hourly report error: {e}")
                await asyncio.sleep(60)

    # ---------------- [순수 폴링] 메인 폴링 루프 ----------------
    async def _main_polling_loop(self):
        """
        순수 폴링 메인 루프 - WebSocket 없이 모든 것을 처리
        - Signal 엔진: 가격 모니터링 + 캔들 신호 체크
        - Shannon 엔진: 가격 모니터링 + 리밸런싱 체크
        """
        logger.info("🔄 [Polling] Main polling loop started (Pure Polling Mode)")
        await asyncio.sleep(3)  # 시작 대기
        
        while True:
            try:
                eng = self.cfg.get('system_settings', {}).get('active_engine', 'shannon')
                
                if eng == 'signal' and self.active_engine and self.active_engine.running:
                    # Signal 엔진 폴링
                    signal_engine = self.engines.get('signal')
                    if signal_engine and hasattr(signal_engine, 'poll_tick'):
                        await signal_engine.poll_tick()
                        
                elif eng == 'shannon' and self.active_engine and self.active_engine.running:
                    # Shannon 엔진 폴링
                    shannon_engine = self.engines.get('shannon')
                    if shannon_engine and hasattr(shannon_engine, 'poll_tick'):
                        await shannon_engine.poll_tick()
                
                elif eng == 'dualthrust' and self.active_engine and self.active_engine.running:
                    # Dual Thrust 엔진 폴링
                    dt_engine = self.engines.get('dualthrust')
                    if dt_engine and hasattr(dt_engine, 'poll_tick'):
                        await dt_engine.poll_tick()

                elif eng == 'dualmode' and self.active_engine and self.active_engine.running:
                    # Dual Mode 엔진 폴링
                    dm_engine = self.engines.get('dualmode')
                    if dm_engine and hasattr(dm_engine, 'poll_tick'):
                        await dm_engine.poll_tick()
                
                # 타임프레임에 따른 폴링 간격
                tf = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('timeframe', '15m')
                poll_interval = self._get_poll_interval(tf)
                await asyncio.sleep(poll_interval)
                
            except Exception as e:
                logger.error(f"Main polling loop error: {e}")
                await asyncio.sleep(30)

    def _get_poll_interval(self, tf):
        """타임프레임에 따른 폴링 간격 계산"""
        tf_seconds = {
            '1m': 60, '3m': 180, '5m': 300, '15m': 900,
            '30m': 1800, '1h': 3600, '2h': 7200, '4h': 14400,
            '6h': 21600, '8h': 28800, '12h': 43200, '1d': 86400
        }
        candle_seconds = tf_seconds.get(tf, 900)  # 기본 15분
        # 캔들 시간의 1/6 간격으로 폴링 (최소 10초, 최대 60초)
        # 2H = 7200초 → 1200초(20분)... 최대 60초로 제한
        return max(10, min(60, candle_seconds // 6))

    # ---------------- 대시보드 ----------------
    async def _dashboard_loop(self):
        cid = self.cfg.get_chat_id()
        if not cid:
            logger.error("❌ Invalid chat_id - Dashboard disabled")
            return
        
        await asyncio.sleep(3)
        
        while True:
            try:
                if self.cfg.get('system_settings', {}).get('show_dashboard', True):
                    self.blink_state = not self.blink_state
                    blink = "🟢" if self.blink_state else "⚪"
                    pause_indicator = " ⏸" if self.is_paused else ""
                    d = self.status_data
                    
                    if not d:
                        eng = self.cfg.get('system_settings', {}).get('active_engine', 'LOADING').upper()
                        # 수동으로 상태 갱신 시도
                        if self.active_engine:
                            try:
                                sym = self._get_current_symbol()
                                total, free, mmr = await self.active_engine.get_balance_info()
                                self.status_data = {
                                    'engine': eng, 'symbol': sym, 'price': 0,
                                    'total_equity': total, 'free_usdt': free, 'mmr': mmr,
                                    'pos_side': 'NONE', 'daily_count': 0, 'daily_pnl': 0
                                }
                                d = self.status_data
                            except Exception as e:
                                logger.debug(f"Manual status fetch failed: {e}")
                    
                    # msg 변수 초기화
                    if not d:
                        eng = self.cfg.get('system_settings', {}).get('active_engine', 'LOADING').upper()
                        msg = f"{blink} **[{eng}] Dashboard**{pause_indicator} [{datetime.now().strftime('%H:%M:%S')}]\n⏳ 데이터 수신 대기 중..."
                    else:
                        # 항상 config의 active_engine 사용 (캐시된 status_data가 아닌 실제 설정)
                        eng = self.cfg.get('system_settings', {}).get('active_engine', 'unknown').upper()
                        msg = f"{blink} **[{eng}] Dashboard**{pause_indicator} [{datetime.now().strftime('%H:%M:%S')}]\n\n"
                        msg += f"💰 **Asset**\n"
                        msg += f"Eq: `${d.get('total_equity', 0):.2f}` | Free: `${d.get('free_usdt', 0):.2f}`\n"
                        msg += f"MMR: `{d.get('mmr', 0):.2f}%`\n\n"
                        
                        # 포지션 정보
                        msg += f"🚀 **Pos** ({d.get('symbol', 'N/A')})\n"
                        if d.get('pos_side') != 'NONE':
                            pnl = d.get('pnl_pct', 0)
                            pnl_emoji = "📈" if pnl >= 0 else "📉"
                            msg += f"{d.get('pos_side')} @ {d.get('entry_price', 0):.2f}\n"
                            msg += f"Amt: `{d.get('coin_amt', 0):.4f}`\n"
                            msg += f"{pnl_emoji} PnL: `{d.get('pnl_usdt', 0):+.2f}` (`{pnl:+.2f}%`)\n"
                        else:
                            msg += "NONE @ 0.00\nAmt: `0.0000`\nPnL: `0.00` (`0.00%`)\n"
                        
                        # 일일 통계
                        msg += f"\n📊 **Today**: {d.get('daily_count', 0)}건 / `${d.get('daily_pnl', 0):+.2f}`"
                        
                        # Shannon 전용 정보
                        if eng.upper() == 'SHANNON':
                            # 200 EMA 및 추세 정보
                            if d.get('ema_200'):
                                trend = d.get('trend', 'N/A')
                                trend_emoji = "🟢" if trend == 'long' else "🔴" if trend == 'short' else "⚪"
                                msg += f"\n\n📈 **Indicators**"
                                msg += f"\n200 EMA: `{d.get('ema_200', 0):.2f}`"
                                msg += f"\nTrend: {trend_emoji} `{trend.upper() if trend else 'N/A'}`"
                                if d.get('atr'):
                                    msg += f"\nATR: `{d.get('atr', 0):.2f}`"
                            
                            # 그리드 주문 수
                            if 'grid_orders' in d:
                                msg += f"\n🔲 Grid: {d.get('grid_orders', 0)}개"
                        
                        # Signal 전용 정보
                        elif eng.upper() == 'SIGNAL':
                            msg += f"\n\n📈 **Strategy**"
                            msg += f"\n{d.get('active_strategy', 'SMA')} | {d.get('entry_mode', 'CROSS')}"
                            
                            # [New] Detailed Filter Display
                            filter_cfg = d.get('filter_config', {})
                            entry_st = d.get('entry_filters', {})
                            exit_st = d.get('exit_filters', {})
                            
                            # Helper for Kalman Text
                            def get_kalman_text(vel):
                                if vel > 0: return "🟢LONG"
                                elif vel < 0: return "🔴SHORT"
                                return "⚪️NEUTRAL"
                            
                            # Helper for Pass/Fail Light
                            def get_light(is_pass):
                                return "🟢ON" if is_pass else "🔴OFF"

                            # 1. Entry Filter Section
                            if filter_cfg:
                                msg += f"\n\n🛡️ **Entry Filter**"
                                
                                # Kalman (Entry)
                                k_en = filter_cfg['kalman']['en_entry']
                                if k_en:
                                    k_vel = entry_st.get('kalman_vel', 0.0)
                                    # Kalman Pass Logic defaults: Long requires >0, Short requires <0. 
                                    # But here we just show state. Pass/Fail depends on signal direction which we don't know here easily 
                                    # without context. But we can show current state.
                                    # User asked for: "Kalman (ShortRed/LongGreen) (Light Red(off)/Green(on)) Value"
                                    # Since Entry Direction depends on the Cross signal, we can't definitively say "Green Light" 
                                    # unless we know the intended direction.
                                    # However, we can simply show the current status.
                                    # Let's show: Kalman: 🟢LONG (vel)
                                    msg += f"\nKalman: {get_kalman_text(k_vel)} (v:{k_vel:.4f})"
                                else:
                                    msg += f"\nKalman: ⚪️OFF"

                                # R2 (Entry)
                                r2_c = filter_cfg['r2']
                                if r2_c['en_entry']:
                                    val = entry_st.get('r2_val', 0.0)
                                    passed = entry_st.get('r2_pass', False)
                                    msg += f"\nR2: {get_light(passed)} `{val:.2f}` (th:{r2_c['th']})"
                                else:
                                     msg += f"\nR2: ⚪️OFF"

                                # Hurst (Entry)
                                h_c = filter_cfg['hurst']
                                if h_c['en_entry']:
                                    val = entry_st.get('hurst_val', 0.0)
                                    passed = entry_st.get('hurst_pass', False)
                                    msg += f"\nHurst: {get_light(passed)} `{val:.2f}` (th:{h_c['th']})"
                                else:
                                     msg += f"\nHurst: ⚪️OFF"
                                     
                                # Chop (Entry)
                                c_c = filter_cfg['chop']
                                if c_c['en_entry']:
                                    val = entry_st.get('chop_val', 0.0)
                                    passed = entry_st.get('chop_pass', False)
                                    msg += f"\nChop: {get_light(passed)} `{val:.1f}` (th:{c_c['th']})"
                                else:
                                     msg += f"\nChop: ⚪️OFF"

                            # 2. Exit Filter Section
                            if filter_cfg:
                                msg += f"\n\n🛡️ **Exit Filter**"
                                
                                # Kalman (Exit)
                                k_en = filter_cfg['kalman']['en_exit']
                                if k_en:
                                    k_vel = exit_st.get('kalman_vel', 0.0)
                                    k_pass = exit_st.get('kalman_pass', False)
                                    # Exit Kalman Logic is implied: Long Position -> Needs Bearish Kalman to Exit? 
                                    # Or just showing current state.
                                    msg += f"\nKalman: {get_kalman_text(k_vel)} (v:{k_vel:.4f})"
                                else:
                                    msg += f"\nKalman: ⚪️OFF"

                                # R2 (Exit)
                                r2_c = filter_cfg['r2']
                                if r2_c['en_exit']:
                                    val = exit_st.get('r2_val', 0.0)
                                    passed = exit_st.get('r2_pass', False)
                                    msg += f"\nR2: {get_light(passed)} `{val:.2f}`"
                                else:
                                     msg += f"\nR2: ⚪️OFF"

                                # Hurst (Exit)
                                h_c = filter_cfg['hurst']
                                if h_c['en_exit']:
                                    val = exit_st.get('hurst_val', 0.0)
                                    passed = exit_st.get('hurst_pass', False)
                                    msg += f"\nHurst: {get_light(passed)} `{val:.2f}`"
                                else:
                                     msg += f"\nHurst: ⚪️OFF"
                                     
                                # Chop (Exit)
                                c_c = filter_cfg['chop']
                                if c_c['en_exit']:
                                    val = exit_st.get('chop_val', 0.0)
                                    passed = exit_st.get('chop_pass', False)
                                    
                                    # [Display Fix] If val is 0.0 and enabled, it likely hasn't calculated yet
                                    if val == 0.0:
                                        msg += f"\nChop: ⏳Pending"
                                    else:
                                        msg += f"\nChop: {get_light(passed)} `{val:.1f}`"
                                else:
                                     msg += f"\nChop: ⚪️OFF"
                            
                            # MicroVBO 전용 대시보드
                            if d.get('active_strategy') == 'MICROVBO':
                                vbo = d.get('vbo_breakout_level')
                                if vbo:
                                    current_price = d.get('price', 0)
                                    long_lvl = vbo.get('long', 0)
                                    short_lvl = vbo.get('short', 0)
                                    atr_val = vbo.get('atr', 0)
                                    
                                    # 현재가 위치 표시
                                    if current_price > long_lvl:
                                        pos_ind = "🟢 LONG ZONE"
                                    elif current_price < short_lvl:
                                        pos_ind = "🔴 SHORT ZONE"
                                    else:
                                        pos_ind = "⚪ NEUTRAL"
                                    
                                    msg += f"\n\n📊 **VBO Breakout**"
                                    msg += f"\n{pos_ind}"
                                    msg += f"\n🔼 Long: `{long_lvl:.2f}`"
                                    msg += f"\n🔽 Short: `{short_lvl:.2f}`"
                                    msg += f"\n📏 ATR: `{atr_val:.2f}`"
                                    
                                    # 포지션 있으면 TP/SL 표시
                                    entry_atr = d.get('vbo_entry_atr')
                                    if entry_atr and d.get('pos_side') != 'NONE':
                                        entry_p = d.get('entry_price', 0)
                                        if d.get('pos_side') == 'LONG':
                                            tp = entry_p + entry_atr
                                            sl = entry_p - (entry_atr * 0.5)
                                        else:
                                            tp = entry_p - entry_atr
                                            sl = entry_p + (entry_atr * 0.5)
                                        msg += f"\n🎯 TP: `{tp:.2f}` | SL: `{sl:.2f}`"
                            
                            # FractalFisher 전용 대시보드
                            elif d.get('active_strategy') == 'FRACTALFISHER':
                                hurst = d.get('fisher_hurst')
                                fisher = d.get('fisher_value')
                                trailing = d.get('fisher_trailing_stop')
                                
                                msg += f"\n\n📊 **Fractal Fisher**"
                                
                                # Hurst 상태
                                if hurst is not None:
                                    h_emoji = "🟢" if hurst >= 0.55 else "🔴"
                                    h_status = "TREND" if hurst >= 0.55 else "RANGE"
                                    msg += f"\n{h_emoji} Hurst: `{hurst:.4f}` ({h_status})"
                                
                                # Fisher 값
                                if fisher is not None:
                                    f_emoji = "🟢" if fisher < -1.5 else "🔴" if fisher > 1.5 else "⚪"
                                    msg += f"\n{f_emoji} Fisher: `{fisher:.4f}`"
                                
                                # Trailing Stop (포지션 있을 때만)
                                if trailing and d.get('pos_side') != 'NONE':
                                    msg += f"\n📍 Trailing Stop: `{trailing:.2f}`"
                        
                        # Dual Thrust 전용 정보
                        elif eng.upper() == 'DUALTHRUST':
                            msg += f"\n\n📊 **Triggers**"
                            if d.get('long_trigger') and d.get('short_trigger'):
                                current_price = d.get('price', 0)
                                long_t = d.get('long_trigger', 0)
                                short_t = d.get('short_trigger', 0)
                                
                                # 현재가 대비 트리거 위치 표시
                                if current_price > long_t:
                                    pos_indicator = "🟢 ABOVE LONG"
                                elif current_price < short_t:
                                    pos_indicator = "🔴 BELOW SHORT"
                                else:
                                    pos_indicator = "⚪ IN RANGE"
                                
                                msg += f"\n{pos_indicator}"
                                msg += f"\n🔼 Long: `{long_t:.2f}`"
                                msg += f"\n🔽 Short: `{short_t:.2f}`"
                                msg += f"\n📏 Range: `{d.get('range', 0):.2f}`"
                                if d.get('today_open'):
                                    msg += f"\n📅 Open: `{d.get('today_open', 0):.2f}`"
                            else:
                                msg += "\n⏳ Calculating triggers..."

                    # 메시지 전송/수정
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
                            # 재시도
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
                                # 메시지 삭제된 경우만 리셋 (다른 에러는 유지)
                                if "message to edit not found" in str(e).lower():
                                    self.dashboard_msg_id = None
                        except Exception as e:
                            logger.error(f"Dashboard error: {e}")
                            # 에러 시에도 msg_id 유지 (새 메시지 폭주 방지)
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
                            await asyncio.sleep(min(e.retry_after, 60))  # 최대 60초 대기
                        except Exception as e:
                            logger.error(f"Dashboard send error: {e}")
                            await asyncio.sleep(30)  # 에러 시 30초 대기 후 재시도
                
                interval = self.cfg.get('system_settings', {}).get('monitoring_interval_seconds', 10)
                await asyncio.sleep(interval)
                
            except Exception as e:
                logger.error(f"Dashboard loop error: {e}")
                await asyncio.sleep(10)


    async def emergency_stop(self):
        """긴급 정지 - 모든 오픈 포지션 청산"""
        logger.warning("🚨 Emergency stop triggered")
        
        if self.active_engine:
            self.active_engine.stop()
        
        self.is_paused = True
        
        try:
            # 1. 오픈된 모든 포지션 조회
            positions = await asyncio.to_thread(self.exchange.fetch_positions)
            open_positions = []
            for p in positions:
                if float(p.get('contracts', 0)) != 0:
                    open_positions.append(p)
            
            if not open_positions:
                # 포지션이 없다면, 혹시 모르니 현재 설정된 심볼의 미체결 주문만 취소 시도
                sym = self._get_current_symbol()
                try:
                    await asyncio.to_thread(self.exchange.cancel_all_orders, sym)
                    logger.info(f"✅ All orders cancelled for {sym}")
                except: pass
                await self.notify("ℹ️ 청산할 오픈 포지션이 없습니다. (봇 정지됨)")
                return

            await self.notify(f"🚨 **긴급 정지 실행**\n발견된 포지션: {len(open_positions)}개 -> 일괄 청산 시작")

            # 2. 모든 오픈 포지션 순차 청산
            for pos in open_positions:
                sym = pos['symbol'].replace(':USDT', '') # 심볼 포맷팅
                
                # 주문 취소
                try:
                    await asyncio.to_thread(self.exchange.cancel_all_orders, sym)
                except Exception as e:
                    logger.error(f"Cancel orders error for {sym}: {e}")
                
                # 청산 주문
                try:
                    side = 'sell' if pos['side'] == 'long' else 'buy'
                    qty = abs(float(pos['contracts']))
                    pnl = float(pos.get('unrealizedPnl', 0))
                    
                    # 수량 정밀도 적용 등을 위해 engine.exit_position을 쓰면 좋겠지만,
                    # 긴급 정지이므로 단순하게 market order로 날림 (또는 reduceOnly)
                    # 하지만 수량 정밀도 문제 생길 수 있으므로 exchange.create_order 사용 시 주의
                    # 여기서는 간단히 실행하되, 오류 시 로그 남김
                    
                    order = await asyncio.to_thread(
                        self.exchange.create_order, sym, 'market', side, qty, {'reduceOnly': True}
                    )
                    logger.info(f"✅ Emergency Close: {sym} {side} {qty}")
                    await self.notify(f"🔒 **{sym}** 청산 완료\nPnL: ${pnl:+.2f}")
                    
                except Exception as e:
                    logger.error(f"Failed to close {sym}: {e}")
                    await self.notify(f"❌ {sym} 청산 실패: {e}")
            
            await self.notify("🏁 긴급 정지 절차 완료")
                    
        except Exception as e:
            logger.error(f"Emergency stop error: {e}")
            await self.notify(f"❌ 긴급 정지 중 오류: {e}")

    async def notify(self, text):
        """알림 전송"""
        try:
            cid = self.cfg.get_chat_id()
            if cid:
                await self.tg_app.bot.send_message(chat_id=cid, text=text, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"Notify error: {e}")


if __name__ == "__main__":
    controller = None
    try:
        controller = MainController()
        asyncio.run(controller.run())
    except KeyboardInterrupt:
        print("\n👋 Bye")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        traceback.print_exc()
    finally:
        # DB 연결 종료
        if controller and hasattr(controller, 'db'):
            try:
                controller.db.conn.close()
                logger.info("✅ Database connection closed")
            except Exception:
                pass
        input("Press Enter to Exit...")