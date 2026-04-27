# emas_improved.py
# [?쒖닔 ?대쭅 踰꾩쟾]
# Version: 2025-12-25-Recovery (Emergency Access)
# 1. WebSocket ?쒓굅 ???덉젙?곸씤 ?대쭅 諛⑹떇?쇰줈 ?꾪솚
# 2. Signal/Shannon ?붿쭊 紐⑤몢 ?대쭅 吏??
# 3. 紐⑤뱺 Critical ?댁뒋 ?섏젙: chat_id ??? ?덉쇅 濡쒓퉭, async ?몃뱾??
# 4. 誘멸뎄??湲곕뒫 異붽?: Grid Trading, Daily Loss Limit, Hourly Report, MMR Alert

import logging
from logging.handlers import RotatingFileHandler
import io
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
import urllib.parse
import urllib.request
import ccxt
import pandas as pd
import pandas_ta as ta
import numpy as np
from pykalman import KalmanFilter as PyKalmanFilter
from datetime import datetime, timezone, timedelta
from collections import Counter, deque
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
from utbreakout.indicators import previous_donchian
from utbreakout.research import format_research_summary
from utbreakout.risk import calculate_risk_plan

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
UTBREAKOUT_DIAGNOSTIC_LOG_FILE = os.path.abspath('utbreakout_diagnostics.log')
UTBREAKOUT_DIAGNOSTIC_MAX_BYTES = 5 * 1024 * 1024
UTBREAKOUT_DIAGNOSTIC_BACKUP_COUNT = 2


def _build_utbreakout_diagnostic_logger():
    diag_logger = logging.getLogger('utbreakout_diagnostics')
    diag_logger.setLevel(logging.INFO)
    diag_logger.propagate = False
    if not any(isinstance(h, RotatingFileHandler) for h in diag_logger.handlers):
        handler = RotatingFileHandler(
            UTBREAKOUT_DIAGNOSTIC_LOG_FILE,
            maxBytes=UTBREAKOUT_DIAGNOSTIC_MAX_BYTES,
            backupCount=UTBREAKOUT_DIAGNOSTIC_BACKUP_COUNT,
            encoding='utf-8'
        )
        handler.setFormatter(logging.Formatter('%(message)s'))
        diag_logger.addHandler(handler)
    return diag_logger


utbreakout_diag_logger = _build_utbreakout_diagnostic_logger()


def _safe_float_or_none(value):
    try:
        if value is None or value == '':
            return None
        parsed = float(value)
        return parsed if np.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def get_utbreakout_diagnostic_log_paths():
    paths = []
    for idx in range(UTBREAKOUT_DIAGNOSTIC_BACKUP_COUNT, 0, -1):
        rotated = f"{UTBREAKOUT_DIAGNOSTIC_LOG_FILE}.{idx}"
        if os.path.exists(rotated):
            paths.append(rotated)
    if os.path.exists(UTBREAKOUT_DIAGNOSTIC_LOG_FILE):
        paths.append(UTBREAKOUT_DIAGNOSTIC_LOG_FILE)
    return paths


def read_utbreakout_diagnostic_events(days=7):
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(days or 7)))
    events = []
    for path in get_utbreakout_diagnostic_log_paths():
        try:
            with open(path, 'r', encoding='utf-8') as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    raw_ts = event.get('ts')
                    try:
                        event_ts = datetime.fromisoformat(str(raw_ts).replace('Z', '+00:00'))
                    except (TypeError, ValueError):
                        continue
                    if event_ts.tzinfo is None:
                        event_ts = event_ts.replace(tzinfo=timezone.utc)
                    event_ts = event_ts.astimezone(timezone.utc)
                    if event_ts >= cutoff:
                        event['_dt'] = event_ts
                        events.append(event)
        except OSError:
            continue
    events.sort(key=lambda item: item.get('_dt') or datetime.min.replace(tzinfo=timezone.utc))
    return events


def format_utbreakout_diagnostic_summary():
    events = read_utbreakout_diagnostic_events(days=7)
    if not events:
        return "최근 7일 진단 로그 없음"

    now = datetime.now(timezone.utc)
    lines = []
    for label, hours in (('24h', 24), ('7d', 24 * 7)):
        cutoff = now - timedelta(hours=hours)
        window_events = [e for e in events if e.get('_dt') and e['_dt'] >= cutoff]
        candidate_keys = {
            (
                e.get('symbol'),
                e.get('side'),
                e.get('decision_candle_ts') or e.get('ut_signal_ts') or e.get('ts')
            )
            for e in window_events
        }
        accepted = sum(1 for e in window_events if e.get('code') == 'ACCEPTED_ENTRY')
        blocked = sum(1 for e in window_events if e.get('event') == 'entry_blocked')
        rejected = [e for e in window_events if str(e.get('code') or '').startswith('REJECTED_')]
        code_counts = Counter(e.get('code') or 'UNKNOWN' for e in rejected)
        top_codes = ', '.join(f"{code}:{count}" for code, count in code_counts.most_common(3)) or 'none'
        lines.append(
            f"{label}: candidates {len(candidate_keys)}, accepted {accepted}, "
            f"blocked {blocked}, top rejects {top_codes}"
        )

    last = events[-1]
    last_dt = last.get('_dt')
    if last_dt:
        kst = last_dt.astimezone(timezone(timedelta(hours=9))).strftime('%m-%d %H:%M')
    else:
        kst = 'unknown'
    lines.append(
        f"last: {kst} {last.get('symbol', '?')} {str(last.get('side') or '?').upper()} "
        f"{last.get('code') or last.get('event') or 'UNKNOWN'}"
    )
    return "\n".join(lines)


def format_utbreakout_research_summary(protection_status=None, days=7):
    events = read_utbreakout_diagnostic_events(days=days)
    if not events:
        return (
            "UT Breakout Research Summary\n"
            f"Window: last {int(days or 7)} days\n"
            "No diagnostic events yet. 다음 15분 판단봉 이후 다시 확인하세요."
        )
    return format_research_summary(
        events,
        protection_status=protection_status or {},
        days=days
    )
CORE_ENGINE = 'signal'
UTBOT_FILTERED_BREAKOUT_STRATEGY = 'utbot_filtered_breakout_v1'
UT_ONLY_STRATEGIES = {'utbot', 'utrsibb', 'utrsi', 'utbb', 'utsmc'}
MA_STRATEGIES = set()
PATTERN_STRATEGIES = set(UT_ONLY_STRATEGIES)
CORE_STRATEGIES = set(UT_ONLY_STRATEGIES) | {UTBOT_FILTERED_BREAKOUT_STRATEGY}
UT_HYBRID_STRATEGIES = {'utrsibb', 'utrsi', 'utbb'}
STATEFUL_UT_STRATEGIES = {'utbot', 'utsmc'} | UT_HYBRID_STRATEGIES
STRATEGY_DISPLAY_NAMES = {
    'sma': 'SMA',
    'hma': 'HMA',
    'cameron': 'CAMERON',
    'utbot': 'UTBOT',
    'utsmc': 'UTSMC',
    'rsibb': 'RSIBB',
    'utrsibb': 'UTRSIBB',
    'utrsi': 'UTRSI',
    'utbb': 'UTBB',
    UTBOT_FILTERED_BREAKOUT_STRATEGY: 'UTBOT_FILTERED_BREAKOUT_V1'
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
UTBOT_FILTER_PACK_LABELS = {
    1: 'CHOP',
    2: 'ADX+DMI',
    3: 'VWAP',
    4: 'HTF Supertrend',
    5: 'BOS/CHoCH'
}
UTBOT_FILTER_PACK_ID_SET = set(UTBOT_FILTER_PACK_LABELS.keys())
ALT_TREND_ALLOWED_TIMEFRAMES = ['5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '1d']
ALT_TREND_TIMEFRAME_ORDER = {
    timeframe: idx for idx, timeframe in enumerate(ALT_TREND_ALLOWED_TIMEFRAMES)
}
BINANCE_FAPI_PUBLIC_BASE_URL = 'https://fapi.binance.com'
UTBREAKOUT_ACTIVE_SET_MAX = 50
UTBREAKOUT_DEFAULT_SET_ID = 2
UTBREAKOUT_AUTO_TIMEFRAMES = ['15m', '30m', '1h', '2h', '4h']


def _build_utbreakout_set(
    set_id,
    family,
    name,
    description,
    regime,
    pros,
    cons,
    frequency_impact,
    indicators,
    entry_filters=None,
    params=None,
):
    status = 'active' if int(set_id) <= UTBREAKOUT_ACTIVE_SET_MAX else 'planned'
    return {
        'id': int(set_id),
        'key': f"set{int(set_id)}",
        'family': family,
        'name': name,
        'status': status,
        'description': description,
        'regime': regime,
        'pros': pros,
        'cons': cons,
        'frequency_impact': frequency_impact,
        'indicators': list(indicators or []),
        'entry_filters': list(entry_filters or []),
        'params': dict(params or {}),
    }


def build_utbreakout_set_registry():
    base_params = {
        'utbot_key_value': 2.5,
        'utbot_atr_period': 14,
        'stop_atr_multiplier': 1.5,
        'take_profit_r_multiple': 2.0,
        'min_risk_reward': 2.0,
    }
    rows = [
        (1, 'UT Core', 'UT only', 'UTBot 방향 유지 상태만 진입 후보로 쓰는 가장 단순한 세트입니다.', '애매한 장세에서 진입 자체가 사라지는 것을 피하고 싶을 때', '진입 빈도가 가장 높고 구조가 단순함', '횡보장 false signal 방어는 약함', '매우 많음', ['UTBot'], [], {'utbot_key_value': 2.0, 'utbot_atr_period': 10}),
        (2, 'UT Core', 'UT + ATR guard', 'UTBot에 최소/최대 변동성 가드만 붙입니다.', '변동성이 너무 죽었거나 과열된 구간만 피하고 싶을 때', '진입을 크게 줄이지 않으면서 위험 구간만 제거', '추세 방향 품질 검증은 약함', '많음', ['UTBot', 'ATR%'], ['atr_guard'], {'utbot_key_value': 2.0, 'utbot_atr_period': 10}),
        (3, 'UT Core', 'UT + HTF trend', '1시간 EMA50/EMA200 방향이 UT 방향과 맞을 때만 허용합니다.', '상위 추세가 비교적 뚜렷한 장', '역방향 진입을 줄임', '상위 추세 전환 초반에는 늦을 수 있음', '중간', ['UTBot', '1H EMA50/200'], ['htf_trend'], {'utbot_key_value': 2.5, 'utbot_atr_period': 14}),
        (4, 'UT Core', 'UT + EMA slope', '15분 EMA50 기울기와 가격 위치만 확인합니다.', '짧은 추세가 막 살아나는 구간', 'HTF보다 빠르게 반응', '짧은 노이즈에 흔들릴 수 있음', '중간~많음', ['UTBot', 'EMA slope'], ['ema_slope'], {'utbot_key_value': 2.5, 'utbot_atr_period': 14}),
        (5, 'UT Core', 'UT + HTF Supertrend', '상위봉 Supertrend 방향과 UTBot 방향을 맞춥니다.', '추세 추종 성격이 강한 장', '추세장 필터로 직관적', '횡보 전환 때 늦게 반응할 수 있음', '중간', ['UTBot', 'HTF Supertrend'], ['htf_supertrend'], {'utbot_key_value': 2.5, 'utbot_atr_period': 14}),
        (6, 'Trend Strength', 'UT + ADX loose', 'ADX 20 이상이면 추세 가능성이 있다고 보고 허용합니다.', '추세 강도만 약하게 보고 싶을 때', 'Set7보다 진입이 많음', '방향성은 UTBot에 많이 의존', '중간~많음', ['UTBot', 'ADX'], ['adx_loose'], {'utbot_key_value': 2.0, 'utbot_atr_period': 10, 'adx_threshold': 20.0}),
        (7, 'Trend Strength', 'UT + ADX + DMI', 'ADX와 +DI/-DI 방향까지 함께 확인합니다.', '추세 강도와 방향이 함께 잡히는 구간', '횡보장 손실을 줄이는 데 유리', 'Set6보다 진입이 줄어듦', '중간', ['UTBot', 'ADX', 'DMI'], ['adx_dmi'], {'utbot_key_value': 2.5, 'utbot_atr_period': 14, 'adx_threshold': 22.0}),
        (8, 'Trend Strength', 'UT + CHOP trend', 'CHOP 값이 낮아 추세성 장세일 때만 허용합니다.', '횡보가 줄고 방향성이 생기는 구간', '횡보 회피에 직접적', '강한 압축 후 초기 돌파는 놓칠 수 있음', '적음~중간', ['UTBot', 'CHOP'], ['chop_trend'], {'utbot_key_value': 2.5, 'utbot_atr_period': 14}),
        (9, 'Trend Strength', 'UT + Vortex', 'Vortex 방향성이 UTBot 방향과 맞을 때 허용합니다.', '방향 전환과 추세 지속을 같이 보고 싶을 때', 'DMI와 다른 방식의 방향 확인', '급변동 구간에서 흔들릴 수 있음', '중간', ['UTBot', 'Vortex'], ['vortex'], {'utbot_key_value': 2.5, 'utbot_atr_period': 14}),
        (10, 'Trend Strength', 'UT + Aroon', 'Aroon Up/Down으로 최근 고저점 갱신 방향을 확인합니다.', '신고가/신저가 갱신 흐름이 있는 장', '돌파성 추세 포착에 유리', '박스권에서는 잦은 전환 가능', '중간', ['UTBot', 'Aroon'], ['aroon'], {'utbot_key_value': 2.5, 'utbot_atr_period': 14}),
        (11, 'Momentum', 'UT + RSI50', 'RSI 50선을 방향성 확인용으로 쓰는 세트입니다.', '모멘텀이 선명한 장', '간단하고 해석 쉬움', 'RSI 50 근처에서 진입 감소', '중간', ['UTBot', 'RSI'], ['rsi_momentum'], {}),
        (12, 'Momentum', 'UT + MACD histogram', 'MACD 히스토그램 방향으로 모멘텀을 확인합니다.', '추세 전환 후 가속 구간', '가속도 확인에 유리', '횡보 중 잦은 반전 가능', '중간', ['UTBot', 'MACD'], ['macd_histogram'], {}),
        (13, 'Momentum', 'UT + ROC', 'Rate of Change로 단기 가격 추진력을 확인합니다.', '방향성이 빠르게 붙는 구간', '빠른 반응', '급등락 후 늦은 진입 가능', '중간~많음', ['UTBot', 'ROC'], ['roc_momentum'], {}),
        (14, 'Momentum', 'UT + CCI', 'CCI가 0선 기준으로 방향성을 보일 때 허용합니다.', '평균 대비 가격 위치가 방향성을 띨 때', '추세/과열을 같이 관찰 가능', '노이즈 구간에서는 흔들릴 수 있음', '중간', ['UTBot', 'CCI'], ['cci_direction'], {}),
        (15, 'Momentum', 'UT + Stochastic direction', '스토캐스틱 K/D 방향으로 단기 힘을 확인합니다.', '짧은 파동 추종', '민감한 진입 가능', '잦은 신호로 과매매 위험', '많음', ['UTBot', 'Stochastic'], ['stoch_direction'], {}),
        (16, 'Volatility Regime', 'UT + ATR normal', 'ATR% 정상 구간만 허용합니다.', '일반 변동성 장', '위험한 저/고변동성 회피', '기회 일부 감소', '중간~많음', ['UTBot', 'ATR%'], ['atr_guard'], {}),
        (17, 'Volatility Regime', 'UT + ATR low-vol caution', '저변동성에서는 더 보수적으로 진입합니다.', '거래량과 변동성이 낮은 장', '무의미한 진입 감소', '초기 변동성 확장 전 놓칠 수 있음', '적음', ['UTBot', 'ATR%'], ['atr_low_vol_caution'], {}),
        (18, 'Volatility Regime', 'UT + ATR high-vol reduce', '고변동성에서는 진입 축소 또는 회피를 목표로 합니다.', '뉴스성 급변동 장', '큰 손실 꼬리 방어', '강한 추세 수익 일부 포기', '적음', ['UTBot', 'ATR%'], ['atr_high_vol_reduce'], {}),
        (19, 'Volatility Regime', 'UT + BB Width expansion', '볼린저 밴드 폭 확장으로 변동성 시작을 봅니다.', '압축 후 확장 구간', '돌파 초반 감지', '가짜 확장 가능', '중간', ['UTBot', 'Bollinger BandWidth'], ['bb_width_expansion'], {}),
        (20, 'Volatility Regime', 'UT + Keltner expansion', 'ATR 기반 Keltner 채널 확장으로 추세성을 봅니다.', 'ATR 변동성 확장 구간', '변동성 기반이라 실전적', '급등락 후 늦을 수 있음', '중간', ['UTBot', 'Keltner Channel'], ['keltner_expansion'], {}),
        (21, 'Breakout', 'UT + Donchian 10', '직전 10봉 Donchian 돌파를 확인합니다.', '짧은 돌파장', 'Set22보다 빠름', '가짜 돌파 증가', '중간', ['UTBot', 'Donchian'], ['donchian_breakout'], {'donchian_length': 10}),
        (22, 'Breakout', 'UT + Donchian 20', '직전 20봉 Donchian 돌파를 확인합니다.', '표준 돌파장', '고전적 돌파 규칙과 잘 맞음', '진입이 늦거나 적을 수 있음', '적음~중간', ['UTBot', 'Donchian'], ['donchian_breakout'], {'donchian_length': 20}),
        (23, 'Breakout', 'UT + BB band breakout', '볼린저 상/하단 종가 돌파를 봅니다.', '변동성 돌파장', '돌파 해석이 쉬움', '상단 추격 리스크', '중간', ['UTBot', 'Bollinger Bands'], ['bb_band_breakout'], {}),
        (24, 'Breakout', 'UT + Keltner breakout', 'Keltner 채널 밖 종가 돌파를 봅니다.', 'ATR 기반 추세 돌파', '노이즈가 BB보다 적을 수 있음', '강한 변동성 후 늦음', '중간', ['UTBot', 'Keltner Channel'], ['keltner_breakout'], {}),
        (25, 'Breakout', 'UT + range expansion candle', '현재 봉 범위가 평균보다 커진 확장봉을 확인합니다.', '강한 캔들 돌파', '급가속 구간 포착', '꼬리 큰 봉에 취약', '중간', ['UTBot', 'Range Expansion'], ['range_expansion'], {}),
        (26, 'Pullback/Continuation', 'UT + VWAP pullback', 'VWAP 근처 눌림 후 UT 방향 지속을 봅니다.', '추세 중 눌림목', '추격보다 가격이 유리할 수 있음', '강한 추세에서는 못 탈 수 있음', '중간', ['UTBot', 'VWAP'], ['vwap_pullback'], {}),
        (27, 'Pullback/Continuation', 'UT + EMA pullback', 'EMA 근처 눌림 후 재개를 봅니다.', 'EMA를 따라가는 추세', '실전 해석이 쉬움', '횡보 EMA에서는 무의미', '중간', ['UTBot', 'EMA'], ['ema_pullback'], {}),
        (28, 'Pullback/Continuation', 'UT + BB midline reclaim', '볼린저 중심선 회복/이탈로 눌림 회복을 봅니다.', '중심선 리클레임 장', '추세 복귀 확인', '중심선 근처 노이즈', '중간', ['UTBot', 'Bollinger Midline'], ['bb_midline_reclaim'], {}),
        (29, 'Pullback/Continuation', 'UT + RSI pullback', 'RSI 눌림 후 방향 재개를 봅니다.', '모멘텀 유지 중 눌림', '과열 추격을 줄임', '추세 초반 진입 감소', '중간', ['UTBot', 'RSI'], ['rsi_pullback'], {}),
        (30, 'Pullback/Continuation', 'UT + ATR trailing continuation', 'ATR trailing 방향 유지로 추세 지속을 봅니다.', '추세가 길게 이어지는 장', '추세 유지 확인', '전환 초반 늦음', '적음~중간', ['UTBot', 'ATR Trail'], ['atr_trail_continuation'], {}),
        (31, 'Volume/Flow', 'UT + volume spike', '거래량 급증을 동반한 UT 방향만 봅니다.', '관심이 몰리는 돌파/추세', '약한 신호 제거', '저거래량 추세 놓침', '중간', ['UTBot', 'Volume'], ['volume_spike'], {}),
        (32, 'Volume/Flow', 'UT + relative volume', '상대 거래량이 평균보다 높은지 봅니다.', '평균보다 활발한 장', '실전적 유동성 필터', '거래량 없는 추세 제외', '중간', ['UTBot', 'Relative Volume'], ['relative_volume'], {}),
        (33, 'Volume/Flow', 'UT + OBV slope', 'OBV 기울기로 누적 매수/매도 흐름을 봅니다.', '수급 방향이 쌓이는 장', '가격보다 선행 가능성', '선물 거래량 해석 한계', '중간', ['UTBot', 'OBV'], ['obv_slope'], {}),
        (34, 'Volume/Flow', 'UT + MFI flow', 'Money Flow Index 방향을 확인합니다.', '가격과 거래량 흐름이 함께 움직일 때', '거래량 가중 모멘텀', '급변동 시 과열 신호', '중간', ['UTBot', 'MFI'], ['mfi_flow'], {}),
        (35, 'Volume/Flow', 'UT + VWAP slope', 'VWAP 기울기가 UT 방향과 맞는지 봅니다.', '당일 평균가격 흐름이 기울어진 장', '데이 트레이딩에 직관적', '세션 기준 변화에 민감', '중간', ['UTBot', 'VWAP'], ['vwap_slope'], {}),
        (36, 'Chop Avoidance', 'UT + CHOP avoid', 'CHOP 높은 구간을 회피합니다.', '횡보 회피 목적', 'false signal 감소', '진입 감소', '적음~중간', ['UTBot', 'CHOP'], ['chop_avoid'], {}),
        (37, 'Chop Avoidance', 'UT + Donchian width avoid', 'Donchian 폭이 너무 좁은 압축장을 피합니다.', '박스권 회피', '좁은 횡보 손실 감소', '압축 후 첫 돌파 놓침', '적음~중간', ['UTBot', 'Donchian Width'], ['donchian_width'], {}),
        (38, 'Chop Avoidance', 'UT + EMA gap avoid', 'EMA50/200 간격이 너무 좁으면 피합니다.', '추세 불명확 회피', '방향 없는 장 방어', '전환 초반 늦음', '적음', ['UTBot', 'EMA Gap'], ['htf_ema_gap'], {}),
        (39, 'Chop Avoidance', 'UT + BB squeeze avoid', '볼린저 squeeze 구간을 피합니다.', '압축 횡보 회피', '무의미한 신호 감소', 'squeeze breakout 초반 놓침', '적음', ['UTBot', 'BB Squeeze'], ['bb_squeeze_avoid'], {}),
        (40, 'Chop Avoidance', 'UT + range compression avoid', '최근 range 압축이 심하면 피합니다.', '좁은 박스권', '수수료 소모 감소', '초기 확장 누락 가능', '적음', ['UTBot', 'Range Compression'], ['range_compression_avoid'], {}),
        (41, 'MTF Alignment', 'UT + 15m/30m align', '15분과 30분 방향 일치를 봅니다.', '단기 MTF 정렬', '빠른 MTF 확인', '신호 감소', '중간', ['UTBot', '15m', '30m'], ['mtf_15_30'], {}),
        (42, 'MTF Alignment', 'UT + 30m/1h align', '30분과 1시간 방향 일치를 봅니다.', '중기 MTF 정렬', '노이즈 감소', '진입 늦음', '적음~중간', ['UTBot', '30m', '1h'], ['mtf_30_1h'], {}),
        (43, 'MTF Alignment', 'UT + 1h/4h align', '1시간과 4시간 방향 일치를 봅니다.', '큰 추세 정렬', '역추세 감소', '진입 매우 감소', '적음', ['UTBot', '1h', '4h'], ['mtf_1h_4h'], {}),
        (44, 'MTF Alignment', 'UT + MTF momentum score', '여러 봉 모멘텀 점수로 set을 고릅니다.', '모멘텀 점수 우위 장', '분산된 정보 활용', '구현 검증 필요', '중간', ['UTBot', 'MTF Momentum'], ['mtf_momentum_score'], {}),
        (45, 'MTF Alignment', 'UT + MTF volatility score', '여러 봉 변동성 점수로 set을 고릅니다.', '변동성 regime 전환', '장세 구분에 유리', '구현 검증 필요', '중간', ['UTBot', 'MTF Volatility'], ['mtf_volatility_score'], {}),
        (46, 'Special Regime', 'UT + Parabolic SAR', 'PSAR 방향과 UT 방향을 맞춥니다.', '추세 추종 특화', '명확한 trailing 구조', '횡보 whipsaw', '중간', ['UTBot', 'Parabolic SAR'], ['psar_direction'], {}),
        (47, 'Special Regime', 'UT + Ichimoku cloud', '일목 구름 위치로 큰 방향을 봅니다.', '중장기 방향성 장', '구조적 추세 판단', '계산/해석 복잡', '적음', ['UTBot', 'Ichimoku'], ['ichimoku_cloud'], {}),
        (48, 'Special Regime', 'UT + session/time volatility', '시간대별 변동성 특성을 반영합니다.', '세션별 움직임 차이가 큰 장', '실거래 시간대 최적화 가능', '시장 구조 변화에 민감', '중간', ['UTBot', 'Session'], ['session_volatility'], {}),
        (49, 'Special Regime', 'UT + market regime fallback', '분석 점수가 애매하면 안전한 단순 set으로 후퇴합니다.', '분류가 애매한 장', '과도한 필터링 방지', '방어력은 낮음', '많음', ['UTBot', 'Regime Score'], ['regime_fallback'], {}),
        (50, 'Special Regime', 'UT emergency simple mode', '장애/데이터 부족 시 UT와 리스크만 남기는 비상 단순 모드입니다.', '데이터 분석이 불안정할 때', '진입 로직이 멈추지 않음', '품질 필터 거의 없음', '매우 많음', ['UTBot', 'Risk Control'], [], {}),
    ]
    registry = {}
    for row in rows:
        set_id, family, name, description, regime, pros, cons, frequency, indicators, filters, params = row
        merged_params = dict(base_params)
        merged_params.update(params or {})
        registry[int(set_id)] = _build_utbreakout_set(
            set_id,
            family,
            name,
            description,
            regime,
            pros,
            cons,
            frequency,
            indicators,
            filters,
            merged_params,
        )
    return registry


UTBREAKOUT_SET_REGISTRY = build_utbreakout_set_registry()


def normalize_utbreakout_set_id(value, default=UTBREAKOUT_DEFAULT_SET_ID):
    try:
        text = str(value or '').strip().lower()
        if text.startswith('set'):
            text = text[3:]
        set_id = int(float(text))
    except (TypeError, ValueError):
        set_id = int(default)
    if set_id not in UTBREAKOUT_SET_REGISTRY:
        set_id = int(default)
    return set_id


def get_utbreakout_set_definition(set_id):
    return UTBREAKOUT_SET_REGISTRY.get(
        normalize_utbreakout_set_id(set_id),
        UTBREAKOUT_SET_REGISTRY[UTBREAKOUT_DEFAULT_SET_ID],
    )


def format_utbreakout_set_brief(set_id):
    info = get_utbreakout_set_definition(set_id)
    status = '실거래' if info.get('status') == 'active' else '예정'
    return (
        f"Set{info['id']} {info['name']} [{status}] - {info['description']} "
        f"진입빈도: {info['frequency_impact']}"
    )


def build_default_utbot_filter_pack():
    return {
        'entry': {
            'selected': [],
            'logic': 'and'
        },
        'exit': {
            'selected': [],
            'logic': 'and',
            'mode_by_filter': {}
        }
    }


def build_default_utbot_filtered_breakout_config():
    return {
        'profile': 'set2',
        'active_set_id': UTBREAKOUT_DEFAULT_SET_ID,
        'auto_select_enabled': False,
        'selection_mode': 'manual',
        'auto_timeframes': list(UTBREAKOUT_AUTO_TIMEFRAMES),
        'entry_timeframe': '15m',
        'exit_timeframe': '15m',
        'htf_timeframe': '1h',
        'utbot_key_value': 2.5,
        'utbot_atr_period': 14,
        'legacy_utbot_key_value': 2.0,
        'legacy_utbot_atr_period': 10,
        'use_heikin_ashi': False,
        'ema_fast': 50,
        'ema_slow': 200,
        'rsi_length': 14,
        'rsi_threshold': 50.0,
        'rsi_long_extreme': 80.0,
        'rsi_short_extreme': 20.0,
        'exclude_rsi_extreme': False,
        'adx_length': 14,
        'adx_threshold': 22.0,
        'atr_length': 14,
        'atr_min_percent': 0.12,
        'atr_max_percent': 1.20,
        'donchian_length': 20,
        'ema_near_percent': 0.20,
        'htf_ema_gap_min_percent': 0.15,
        'donchian_width_min_percent': 0.50,
        'stop_atr_multiplier': 1.5,
        'take_profit_r_multiple': 2.0,
        'min_risk_reward': 2.0,
        'risk_per_trade_percent': 1.0,
        'max_risk_per_trade_usdt': 1.0,
        'daily_max_loss_usdt': 3.0,
        'max_daily_trades': 5,
        'max_consecutive_losses': 3,
        'daily_profit_target_enabled': False,
        'daily_profit_target_usdt': 5.0,
        'opposite_signal_exit_enabled': False,
        'opposite_set_exit_enabled': False,
        'opposite_set_exit_min_hold_candles': 3,
        'opposite_set_exit_min_pnl_enabled': False,
        'opposite_set_exit_min_pnl_usdt': 0.0,
        'ema_rsi_exit_enabled': False,
        'adx_donchian_exit_enabled': False
    }


def build_utbot_filtered_breakout_profile(profile):
    profile_key = str(profile or 'set2').strip().lower()
    set_id = normalize_utbreakout_set_id(profile_key, UTBREAKOUT_DEFAULT_SET_ID)
    cfg = build_default_utbot_filtered_breakout_config()
    if profile_key in {'aggressive'}:
        set_id = 1
    elif profile_key in {'conservative'}:
        set_id = 7
    set_info = get_utbreakout_set_definition(set_id)
    cfg.update(set_info.get('params', {}))
    cfg['profile'] = f"set{set_id}"
    cfg['active_set_id'] = set_id
    cfg['selection_mode'] = 'manual'
    cfg['auto_select_enabled'] = False
    return cfg


def get_utbot_filter_pack_label(filter_id):
    try:
        return UTBOT_FILTER_PACK_LABELS[int(filter_id)]
    except (TypeError, ValueError, KeyError):
        return f"Filter {filter_id}"


def normalize_utbot_filter_pack_selected(selected):
    if selected is None:
        return []

    if isinstance(selected, str):
        raw_items = [item.strip() for item in selected.split(',')]
    elif isinstance(selected, (list, tuple, set)):
        raw_items = list(selected)
    else:
        raw_items = [selected]

    normalized = []
    seen = set()
    for raw_item in raw_items:
        item_text = str(raw_item or '').strip().lower()
        if not item_text or item_text in {'0', 'off', 'none', '[]'}:
            continue
        try:
            filter_id = int(item_text)
        except (TypeError, ValueError):
            continue
        if filter_id in UTBOT_FILTER_PACK_ID_SET and filter_id not in seen:
            normalized.append(filter_id)
            seen.add(filter_id)
    return sorted(normalized)


def normalize_utbot_filter_pack_logic(logic):
    return 'or' if str(logic or 'and').strip().lower() == 'or' else 'and'


def normalize_utbot_filter_pack_exit_mode(mode):
    aliases = {
        'c': 'confirm',
        'confirm': 'confirm',
        's': 'signal',
        'signal': 'signal'
    }
    return aliases.get(str(mode or 'confirm').strip().lower(), 'confirm')


def normalize_utbot_filter_pack_config(filter_pack):
    raw_pack = filter_pack if isinstance(filter_pack, dict) else {}
    entry_cfg = raw_pack.get('entry') if isinstance(raw_pack.get('entry'), dict) else {}
    exit_cfg = raw_pack.get('exit') if isinstance(raw_pack.get('exit'), dict) else {}

    entry_selected = normalize_utbot_filter_pack_selected(entry_cfg.get('selected', []))
    exit_selected = normalize_utbot_filter_pack_selected(exit_cfg.get('selected', []))

    raw_mode_by_filter = exit_cfg.get('mode_by_filter')
    normalized_mode_by_filter = {}
    if isinstance(raw_mode_by_filter, dict):
        for filter_id in exit_selected:
            raw_mode = raw_mode_by_filter.get(str(filter_id), raw_mode_by_filter.get(filter_id, 'confirm'))
            normalized_mode_by_filter[str(filter_id)] = normalize_utbot_filter_pack_exit_mode(raw_mode)
    else:
        for filter_id in exit_selected:
            normalized_mode_by_filter[str(filter_id)] = 'confirm'

    for filter_id in exit_selected:
        normalized_mode_by_filter.setdefault(str(filter_id), 'confirm')

    return {
        'entry': {
            'selected': entry_selected,
            'logic': normalize_utbot_filter_pack_logic(entry_cfg.get('logic', 'and'))
        },
        'exit': {
            'selected': exit_selected,
            'logic': normalize_utbot_filter_pack_logic(exit_cfg.get('logic', 'and')),
            'mode_by_filter': normalized_mode_by_filter
        }
    }


def format_utbot_filter_pack_selected(selected):
    normalized = normalize_utbot_filter_pack_selected(selected)
    if not normalized:
        return 'OFF'
    return "[" + ",".join(str(filter_id) for filter_id in normalized) + "]"


def format_utbot_filter_pack_logic(logic):
    return normalize_utbot_filter_pack_logic(logic).upper()


def format_utbot_filter_pack_mode_map(mode_by_filter, selected=None):
    normalized_selected = normalize_utbot_filter_pack_selected(selected or [])
    if not normalized_selected:
        return 'OFF'
    if isinstance(mode_by_filter, dict):
        raw_map = mode_by_filter
    else:
        raw_map = {}
    items = []
    for filter_id in normalized_selected:
        mode = normalize_utbot_filter_pack_exit_mode(raw_map.get(str(filter_id), raw_map.get(filter_id, 'confirm')))
        items.append(f"{filter_id}={'C' if mode == 'confirm' else 'S'}")
    return ", ".join(items) if items else 'OFF'


def normalize_alt_trend_timeframes(timeframes):
    if isinstance(timeframes, str):
        raw_tokens = [token.strip().lower() for token in timeframes.split(',') if token.strip()]
    elif isinstance(timeframes, (list, tuple, set)):
        raw_tokens = [str(token or '').strip().lower() for token in timeframes if str(token or '').strip()]
    else:
        try:
            raw_tokens = [str(token or '').strip().lower() for token in list(timeframes) if str(token or '').strip()]
        except Exception:
            raw_tokens = []

    selected = []
    seen = set()
    for token in raw_tokens:
        if token not in ALT_TREND_TIMEFRAME_ORDER:
            continue
        if token in seen:
            continue
        seen.add(token)
        selected.append(token)

    selected.sort(key=lambda timeframe: ALT_TREND_TIMEFRAME_ORDER.get(timeframe, 999))
    return selected


def format_alt_trend_timeframes(timeframes):
    normalized = normalize_alt_trend_timeframes(timeframes)
    return ", ".join(normalized) if normalized else 'OFF'

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
            'telegram': {
                'reporting': {
                    'periodic_reports_enabled': False,
                    'hourly_report_enabled': False,
                    'stateful_diag_enabled': False,
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
                        'bb_mult': 2.0
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
                'bb_mult': 2.0
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
        active_strategy = str(strategy_params.get('active_strategy', 'utbot')).lower()
        if active_strategy not in CORE_STRATEGIES:
            strategy_params['active_strategy'] = 'utbot'
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
        if 'periodic_reports_enabled' not in reporting_cfg:
            reporting_cfg['periodic_reports_enabled'] = False
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
                    "periodic_reports_enabled": False,
                    "hourly_report_enabled": False,
                    "stateful_diag_enabled": False,
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
                        "bb_mult": 2.0
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

    def get_daily_entry_count(self):
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT COUNT(*) FROM trades WHERE entry_time LIKE ?", (f"{today}%",))
            res = cur.fetchone()
            return res[0] if res and res[0] else 0

    def get_recent_closed_trade_pnls(self, limit=10, today_only=False):
        limit = max(1, int(limit or 1))
        with self.lock:
            cur = self.conn.cursor()
            if today_only:
                today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
                cur.execute(
                    "SELECT pnl_usdt FROM trades WHERE exit_time LIKE ? ORDER BY exit_time DESC, id DESC LIMIT ?",
                    (f"{today}%", limit)
                )
            else:
                cur.execute(
                    "SELECT pnl_usdt FROM trades WHERE exit_time IS NOT NULL ORDER BY exit_time DESC, id DESC LIMIT ?",
                    (limit,)
                )
            return [float(row[0] or 0.0) for row in cur.fetchall()]

    def get_latest_open_trade(self, symbol):
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(
                """SELECT symbol, side, entry_price, quantity, entry_time
                FROM trades WHERE symbol=? AND exit_time IS NULL
                ORDER BY id DESC LIMIT 1""",
                (symbol,)
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                'symbol': row[0],
                'side': row[1],
                'entry_price': float(row[2] or 0.0),
                'quantity': float(row[3] or 0.0),
                'entry_time': row[4],
            }

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

    def get_effective_trade_direction(self):
        return self.ctrl.get_effective_trade_direction()

    def is_trade_direction_allowed(self, side):
        side = str(side or '').strip().lower()
        if side not in {'long', 'short'}:
            return True
        direction = self.get_effective_trade_direction()
        return not (
            (direction == 'long' and side == 'short') or
            (direction == 'short' and side == 'long')
        )

    def format_trade_direction_block_reason(self, side):
        return f"방향 필터 차단 ({str(side or '').upper()} vs {self.get_effective_trade_direction().upper()})"

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
        self.utbb_special_long_state = {}
        self.utbb_special_short_state = {}
        self.ut_pending_entries = {}
        self.ut_strategy_signal_state = {}
        self.utsmc_pending_entries = {}
        self.utsmc_last_entry_signal_ts = {}
        self.utsmc_entry_invalidation = {}
        self.utsmc_fixed_exit_obs = {}
        
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
        self.last_utbot_filter_pack_status = {}  # symbol -> UTBot filter pack diagnostics
        self.last_utbot_rsi_momentum_filter_status = {}  # symbol -> UTBot RSI Momentum filter diagnostics
        self.last_utsmc_candidate_filter_status = {}  # symbol -> candidate filter diagnostics
        self.last_utbot_filtered_breakout_status = {}  # symbol -> filtered breakout diagnostics
        self.utbot_filtered_breakout_entry_plans = {}  # symbol -> accepted risk plan
        self.utbot_filtered_breakout_failures = {}  # symbol -> side -> recent failed candidate timestamps
        self.utbreakout_futures_context_cache = {}  # symbol -> cached funding/OI context for research logs
        self.last_protection_order_status = {}  # symbol -> exchange-side TP/SL audit status
        self.last_protection_alert_ts = {}  # symbol:kind -> last Telegram alert timestamp
        self.last_entry_reason = {}        # symbol -> latest entry decision reason

    def start(self):
        super().start()
        self.last_activity = time.time()
        # [Fix] ?ш컻(RESUME) ???곹깭 珥덇린?뷀븯??利됱떆 ?ъ쭊??媛?ν븯?꾨줉 ?섏젙
        self.reset_signal_runtime_state(
            reset_entry_cache=True,
            reset_exit_cache=True,
            reset_stateful_strategy=True
        )
        self.cameron_states = {}

        # 珥덇린??
        self.active_symbols = set()
        config_watchlist = self.get_runtime_watchlist()
        for s in config_watchlist:
            self.active_symbols.add(s)
        logger.info(f"?? [Signal] Engine started (Multi-Symbol Mode). Watching: {self.active_symbols}")

    def reset_entry_runtime_state(self):
        self.last_candle_time = {}
        self.last_candle_success = {}
        self.last_processed_candle_ts = {}

    def reset_exit_runtime_state(self):
        self.last_processed_exit_candle_ts = {}

    def reset_stateful_strategy_runtime_state(self):
        self.last_state_sync_candle_ts = {}
        self.last_stateful_retry_ts = {}
        self.last_stateful_diag = {}
        self.last_stateful_diag_notice = {}
        self.last_entry_filter_status = {}
        self.last_exit_filter_status = {}
        self.last_utbot_filter_pack_status = {}
        self.last_utbot_rsi_momentum_filter_status = {}
        self.last_utsmc_candidate_filter_status = {}
        self.last_utbot_filtered_breakout_status = {}
        self.utbot_filtered_breakout_entry_plans = {}
        self.utbot_filtered_breakout_failures = {}
        self.utbreakout_futures_context_cache = {}
        self.last_protection_order_status = {}
        self.last_protection_alert_ts = {}
        self.last_entry_reason = {}
        self.pending_reentry = {}
        self.ut_hybrid_timing_latches = {}
        self.ut_hybrid_timing_consumed_ts = {}
        self.utbb_special_long_state = {}
        self.utbb_special_short_state = {}
        self.ut_pending_entries = {}
        self.ut_strategy_signal_state = {}
        self.utsmc_pending_entries = {}
        self.utsmc_last_entry_signal_ts = {}
        self.utsmc_entry_invalidation = {}
        self.utsmc_fixed_exit_obs = {}

    def reset_signal_runtime_state(self, *, reset_entry_cache=False, reset_exit_cache=False, reset_stateful_strategy=False):
        if reset_entry_cache:
            self.reset_entry_runtime_state()
        if reset_exit_cache:
            self.reset_exit_runtime_state()
        if reset_stateful_strategy:
            self.reset_stateful_strategy_runtime_state()

    def _ensure_runtime_state_container(self, attr_name, default_factory=dict):
        value = getattr(self, attr_name, None)
        if not isinstance(value, dict):
            logger.error(f"State corrupted: {attr_name} is {type(value)}, resetting.")
            value = default_factory()
            setattr(self, attr_name, value)
        return value

    def _ensure_runtime_state_containers(self, attr_names):
        for attr_name in attr_names:
            self._ensure_runtime_state_container(attr_name)

    def _get_ut_entry_timing_mode(self):
        strategy_params = self.get_runtime_strategy_params()
        mode = str(strategy_params.get('ut_entry_timing_mode', 'next_candle') or 'next_candle').strip().lower()
        if mode not in {'next_candle', 'persistent'}:
            return 'next_candle'
        return mode

    def _get_ut_entry_timing_label(self, mode=None):
        parsed_mode = str(mode or self._get_ut_entry_timing_mode()).strip().lower()
        return '다음봉 진입' if parsed_mode == 'next_candle' else '신호유지 진입'

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
        if active_strategy == UTBOT_FILTERED_BREAKOUT_STRATEGY:
            fb_cfg = self._get_utbot_filtered_breakout_config(strategy_params)
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

    async def _maybe_execute_ut_live_entry(self, symbol, live_candle_ts, live_price, pos_side):
        pending = self._get_ut_pending_entry(symbol)
        pending_source = self._get_ut_pending_source(pending) if pending else None
        is_flip_pending = self._is_ut_flip_pending(pending) if pending else False
        pending_side = str((pending or {}).get('side') or '').strip().lower()
        if pos_side != 'NONE':
            if pending:
                if is_flip_pending:
                    current_pos_side = str(pos_side).strip().lower()
                    if pending_side in {'long', 'short'} and current_pos_side == pending_side:
                        execute_ts = int(pending.get('execute_ts') or 0)
                        expires_ts = int(pending.get('expires_ts') or 0)
                        signal_ts = int(pending.get('signal_ts') or 0)
                        retry_count = int(pending.get('retry_count') or 0)
                        strategy_name = str(pending.get('strategy_name') or 'UT')
                        self._clear_ut_pending_entry(symbol)
                        self.last_entry_reason[symbol] = (
                            f"{strategy_name} flip pending stale cleared ({pending_side.upper()} already open)"
                        )
                        self._update_stateful_diag(
                            symbol,
                            stage='flip_stale_cleared',
                            strategy=strategy_name,
                            entry_sig=(pending_side or 'none'),
                            pos_side=current_pos_side.upper(),
                            ut_pending_source='flip',
                            ut_pending_side=(pending_side.upper() if pending_side in {'long', 'short'} else None),
                            ut_pending_state='stale_cleared',
                            ut_pending_execute_ts=execute_ts or None,
                            ut_pending_execute_ts_human=(
                                datetime.fromtimestamp(execute_ts / 1000).strftime('%m-%d %H:%M')
                                if execute_ts else None
                            ),
                            ut_pending_expires_ts=expires_ts or None,
                            ut_pending_expires_ts_human=(
                                datetime.fromtimestamp(expires_ts / 1000).strftime('%m-%d %H:%M')
                                if expires_ts else None
                            ),
                            ut_flip_target_side=(pending_side.upper() if pending_side in {'long', 'short'} else None),
                            ut_flip_signal_ts=signal_ts or None,
                            ut_flip_signal_ts_human=(
                                datetime.fromtimestamp(signal_ts / 1000).strftime('%m-%d %H:%M')
                                if signal_ts else None
                            ),
                            ut_flip_execute_ts=execute_ts or None,
                            ut_flip_execute_ts_human=(
                                datetime.fromtimestamp(execute_ts / 1000).strftime('%m-%d %H:%M')
                                if execute_ts else None
                            ),
                            ut_flip_expires_ts=expires_ts or None,
                            ut_flip_expires_ts_human=(
                                datetime.fromtimestamp(expires_ts / 1000).strftime('%m-%d %H:%M')
                                if expires_ts else None
                            ),
                            ut_flip_retry_count=retry_count,
                            ut_flip_block_reason='same_side_position',
                            note='flip pending stale cleared | matching live position already exists'
                        )
                    else:
                        self._touch_ut_pending_entry(symbol, pending_state='waiting_flat')
                else:
                    self._clear_ut_pending_entry(symbol)
            return pos_side
        if not pending:
            return pos_side

        execute_ts = int(pending.get('execute_ts') or 0)
        if live_candle_ts < execute_ts:
            return pos_side

        timing_mode = self._get_ut_entry_timing_mode()
        expires_ts = int(pending.get('expires_ts') or 0)
        if timing_mode == 'next_candle':
            if is_flip_pending:
                if expires_ts > 0 and live_candle_ts > expires_ts:
                    entry_sig = str(pending.get('side') or '').lower()
                    strategy_name = str(pending.get('strategy_name') or 'UT')
                    retry_count = int(pending.get('retry_count') or 0)
                    self._clear_ut_pending_entry(symbol)
                    self.last_entry_reason[symbol] = (
                        f"{strategy_name} flip {entry_sig.upper()} 대기 만료"
                        if entry_sig in {'long', 'short'}
                        else f"{strategy_name} flip 대기 만료"
                    )
                    self._update_stateful_diag(
                        symbol,
                        stage='flip_expired',
                        strategy=strategy_name,
                        entry_sig=(entry_sig or 'none'),
                        pos_side='NONE',
                        ut_pending_source=pending_source,
                        ut_pending_side=(entry_sig.upper() if entry_sig in {'long', 'short'} else None),
                        ut_pending_state='expired',
                        ut_pending_execute_ts=execute_ts,
                        ut_pending_execute_ts_human=datetime.fromtimestamp(execute_ts / 1000).strftime('%m-%d %H:%M') if execute_ts else None,
                        ut_pending_expires_ts=expires_ts,
                        ut_pending_expires_ts_human=datetime.fromtimestamp(expires_ts / 1000).strftime('%m-%d %H:%M') if expires_ts else None,
                        ut_flip_target_side=(entry_sig.upper() if entry_sig in {'long', 'short'} else None),
                        ut_flip_signal_ts=int(pending.get('signal_ts') or 0) or None,
                        ut_flip_signal_ts_human=(
                            datetime.fromtimestamp(int(pending.get('signal_ts') or 0) / 1000).strftime('%m-%d %H:%M')
                            if int(pending.get('signal_ts') or 0) else None
                        ),
                        ut_flip_execute_ts=execute_ts,
                        ut_flip_execute_ts_human=datetime.fromtimestamp(execute_ts / 1000).strftime('%m-%d %H:%M') if execute_ts else None,
                        ut_flip_expires_ts=expires_ts,
                        ut_flip_expires_ts_human=datetime.fromtimestamp(expires_ts / 1000).strftime('%m-%d %H:%M') if expires_ts else None,
                        ut_flip_retry_count=retry_count,
                        ut_flip_block_reason=pending.get('block_reason'),
                        note=(
                            f"flip pending expired | live_ts={live_candle_ts} | "
                            f"execute_ts={execute_ts} | expires_ts={expires_ts}"
                        )
                    )
                    await self._notify_stateful_diag(symbol)
                    return pos_side
            elif live_candle_ts > execute_ts:
                self._clear_ut_pending_entry(symbol)
                return pos_side

        entry_sig = str(pending.get('side') or '').lower()
        if entry_sig not in {'long', 'short'}:
            self._clear_ut_pending_entry(symbol)
            return pos_side

        strategy_name = str(pending.get('strategy_name') or 'UT')
        timing_label = self._get_ut_entry_timing_label(timing_mode)
        if not self.is_trade_direction_allowed(entry_sig):
            block_reason = self.format_trade_direction_block_reason(entry_sig)
            self._clear_ut_pending_entry(symbol)
            self.last_entry_reason[symbol] = f"{strategy_name} {block_reason}"
            self._update_stateful_diag(
                symbol,
                stage='entry_blocked',
                strategy=strategy_name,
                entry_sig=entry_sig,
                pos_side='NONE',
                ut_pending_source=pending_source,
                ut_pending_side=entry_sig.upper(),
                ut_pending_state='blocked',
                ut_pending_execute_ts=execute_ts or None,
                ut_pending_execute_ts_human=(
                    datetime.fromtimestamp(execute_ts / 1000).strftime('%m-%d %H:%M')
                    if execute_ts else None
                ),
                ut_pending_expires_ts=expires_ts or None,
                ut_pending_expires_ts_human=(
                    datetime.fromtimestamp(expires_ts / 1000).strftime('%m-%d %H:%M')
                    if expires_ts else None
                ),
                ut_flip_target_side=(entry_sig.upper() if is_flip_pending else None),
                note=f"live {timing_mode} entry blocked by direction filter"
            )
            return pos_side

        self._clear_ut_pending_entry(symbol)
        self.last_entry_reason[symbol] = (
            f"{strategy_name} flip 유지 확인 -> {entry_sig.upper()} 진입"
            if is_flip_pending else
            f"{strategy_name} {timing_label} 기준 {entry_sig.upper()} 진입"
        )
        self._update_stateful_diag(
            symbol,
            stage='flip_reentered' if is_flip_pending else 'entry_submitted',
            strategy=strategy_name,
            entry_sig=entry_sig,
            pos_side=entry_sig.upper(),
            ut_pending_source=pending_source,
            ut_pending_side=entry_sig.upper(),
            ut_pending_state='executed',
            ut_pending_execute_ts=execute_ts,
            ut_pending_execute_ts_human=datetime.fromtimestamp(execute_ts / 1000).strftime('%m-%d %H:%M') if execute_ts else None,
            ut_pending_expires_ts=expires_ts,
            ut_pending_expires_ts_human=datetime.fromtimestamp(expires_ts / 1000).strftime('%m-%d %H:%M') if expires_ts else None,
            ut_flip_target_side=(entry_sig.upper() if is_flip_pending else None),
            ut_flip_signal_ts=int(pending.get('signal_ts') or 0) or None,
            ut_flip_signal_ts_human=(
                datetime.fromtimestamp(int(pending.get('signal_ts') or 0) / 1000).strftime('%m-%d %H:%M')
                if int(pending.get('signal_ts') or 0) else None
            ),
            ut_flip_execute_ts=execute_ts if is_flip_pending else None,
            ut_flip_execute_ts_human=(
                datetime.fromtimestamp(execute_ts / 1000).strftime('%m-%d %H:%M')
                if is_flip_pending and execute_ts else None
            ),
            ut_flip_expires_ts=expires_ts if is_flip_pending else None,
            ut_flip_expires_ts_human=(
                datetime.fromtimestamp(expires_ts / 1000).strftime('%m-%d %H:%M')
                if is_flip_pending and expires_ts else None
            ),
            ut_flip_retry_count=int(pending.get('retry_count') or 0) if is_flip_pending else None,
            ut_flip_block_reason=pending.get('block_reason') if is_flip_pending else None,
            note=(
                f"live flip entry | live_ts={live_candle_ts} | armed_ts={execute_ts} | expires_ts={expires_ts}"
                if is_flip_pending else
                f"live {timing_mode} entry | live_ts={live_candle_ts} | armed_ts={execute_ts}"
            )
        )
        logger.info(f"[{strategy_name}] live {timing_mode} entry {entry_sig.upper()} @ {live_candle_ts}")
        await self.entry(symbol, entry_sig, float(live_price))

        hybrid_strategy = str(pending.get('hybrid_strategy') or '').lower()
        if hybrid_strategy:
            self._consume_ut_hybrid_timing_latch(symbol, hybrid_strategy, entry_sig)
        return entry_sig.upper()

    def _expire_utbb_long_setup(self, symbol, current_ts, candle_ms, max_bars=3):
        current_ts = int(current_ts or 0)
        candle_ms = int(candle_ms or 0)
        max_bars = max(1, int(max_bars or 1))
        if current_ts <= 0 or candle_ms <= 0:
            return
        expiry_window = candle_ms * max_bars
        for source in ('ut', 'bb'):
            setup = self._get_utbb_long_setup(symbol, source)
            if not setup:
                continue
            setup_ts = int(setup.get('signal_ts') or 0)
            if setup_ts <= 0 or (current_ts - setup_ts) > expiry_window:
                self._clear_utbb_long_setup(symbol, source)

    def _is_utbb_long_setup_ready(self, symbol, candle_ms, max_bars=3):
        ut_setup = self._get_utbb_long_setup(symbol, 'ut')
        bb_setup = self._get_utbb_long_setup(symbol, 'bb')
        if not ut_setup or not bb_setup:
            return False
        ut_ts = int(ut_setup.get('signal_ts') or 0)
        bb_ts = int(bb_setup.get('signal_ts') or 0)
        candle_ms = int(candle_ms or 0)
        max_bars = max(1, int(max_bars or 1))
        if ut_ts <= 0 or bb_ts <= 0 or candle_ms <= 0:
            return False
        return abs(ut_ts - bb_ts) <= (candle_ms * max_bars)

    def _set_utbb_special_long_state(self, symbol, signal_ts=None):
        self.utbb_special_long_state[symbol] = {
            'active': True,
            'signal_ts': int(signal_ts or 0)
        }

    def _clear_utbb_special_long_state(self, symbol):
        self.utbb_special_long_state.pop(symbol, None)

    def _get_utbb_special_long_state(self, symbol):
        return self.utbb_special_long_state.get(symbol)

    def _is_utbb_special_long_active(self, symbol):
        state = self._get_utbb_special_long_state(symbol)
        return bool(state and state.get('active'))

    def _set_utbb_special_short_state(self, symbol, signal_ts=None, mode='lower_reentry'):
        self.utbb_special_short_state[symbol] = {
            'active': True,
            'signal_ts': int(signal_ts or 0),
            'mode': str(mode or 'lower_reentry')
        }

    def _clear_utbb_special_short_state(self, symbol):
        self.utbb_special_short_state.pop(symbol, None)

    def _get_utbb_special_short_state(self, symbol):
        return self.utbb_special_short_state.get(symbol)

    def _is_utbb_special_short_active(self, symbol):
        state = self._get_utbb_special_short_state(symbol)
        return bool(state and state.get('active'))

    def _get_utbb_special_short_mode(self, symbol):
        state = self._get_utbb_special_short_state(symbol)
        if not state or not state.get('active'):
            return None
        return str(state.get('mode') or 'lower_reentry')

    def _apply_utbb_long_entry_state(self, symbol, raw_hybrid_detail, bb_upper_breakout):
        if bb_upper_breakout:
            self._set_utbb_special_long_state(symbol, raw_hybrid_detail.get('bb_upper_signal_ts'))
            return
        self._clear_utbb_special_long_state(symbol)

    def _apply_utbb_short_entry_state(self, symbol, raw_hybrid_detail, *, special_short_candidate=False, short_rebound_fail_ready=False):
        if special_short_candidate:
            self._set_utbb_special_short_state(symbol, raw_hybrid_detail.get('bb_lower_signal_ts'), mode='lower_reentry')
            return
        if short_rebound_fail_ready:
            self._set_utbb_special_short_state(
                symbol,
                raw_hybrid_detail.get('bb_mid_rebound_fail_signal_ts'),
                mode='mid_rebound_fail'
            )
            return
        self._clear_utbb_special_short_state(symbol)

    def _build_utbb_long_entry_reason(self, strategy_name, *, ut_signal=None, long_pair_source=None, ut_long_fresh=False, bb_long_fresh=False, bb_upper_breakout=False, reentry=False):
        if bb_upper_breakout:
            if reentry:
                return (
                    f"{strategy_name} 숏 청산 후 특수 LONG 재진입"
                    if ut_signal == 'long'
                    else f"{strategy_name} 숏 청산 후 UT 롱상태로 특수 LONG 재진입"
                )
            return (
                f"{strategy_name} UT 롱신호 + BB 상단돌파 -> 특수 LONG 진입"
                if ut_signal == 'long'
                else f"{strategy_name} UT 롱상태 유지 + BB 상단돌파 -> 특수 LONG 진입"
            )

        if long_pair_source == 'same_candle':
            return (
                f"{strategy_name} 숏 청산 후 UT/BB 롱 동시 확인 -> LONG 재진입"
                if reentry else f"{strategy_name} UT/BB 롱 동시 확인 -> LONG 진입"
            )
        if long_pair_source == 'bb_first':
            return (
                f"{strategy_name} 숏 청산 후 저장된 BB 롱 + UT 롱신호 -> LONG 재진입"
                if reentry else f"{strategy_name} 저장된 BB 롱 + UT 롱신호 -> LONG 진입"
            )
        if long_pair_source == 'ut_first':
            return (
                f"{strategy_name} 숏 청산 후 저장된 UT 롱 + BB 롱신호 -> LONG 재진입"
                if reentry else f"{strategy_name} 저장된 UT 롱 + BB 롱신호 -> LONG 진입"
            )
        if ut_long_fresh:
            return (
                f"{strategy_name} 숏 청산 후 UT 롱신호 -> LONG 재진입"
                if reentry else f"{strategy_name} UT 롱신호 -> LONG 진입"
            )
        if bb_long_fresh:
            return f"{strategy_name} BB 롱신호 -> LONG 진입"
        return (
            f"{strategy_name} 숏 청산 후 LONG 재진입"
            if reentry else f"{strategy_name} LONG 진입"
        )

    def _build_utbb_short_entry_note(self, *, ut_signal=None, short_mid_ready=False, short_rebound_fail_ready=False, short_rebound_fail_bull_count=0, special_short_candidate=False):
        if special_short_candidate:
            return (
                "BB 중간선 하락 돌파 + BB 하단 하향 돌파 + UT 숏신호"
                if ut_signal == 'short'
                else "BB 중간선 하락 돌파 + BB 하단 하향 돌파 + UT 숏상태"
            )
        if short_rebound_fail_ready:
            return (
                f"BB 중간선 아래 양봉 {short_rebound_fail_bull_count}개 후 반등 실패 + UT 숏신호"
                if ut_signal == 'short'
                else f"BB 중간선 아래 양봉 {short_rebound_fail_bull_count}개 후 반등 실패 + UT 숏상태"
            )
        if short_mid_ready:
            return (
                "BB 중간선 하락 돌파 + UT 숏신호"
                if ut_signal == 'short'
                else "BB 중간선 하락 돌파 + UT 숏상태"
            )
        return "BB 숏 셋업 + UT 숏신호"

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
            'take_profit_r_multiple': 2.0,
            'min_risk_reward': 2.0,
            'risk_per_trade_percent': 1.0,
            'max_risk_per_trade_usdt': 1.0,
            'daily_max_loss_usdt': 3.0,
            'daily_profit_target_usdt': 5.0,
            'opposite_set_exit_min_pnl_usdt': 0.0
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
            'max_consecutive_losses': 3,
            'opposite_set_exit_min_hold_candles': 3
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
        cfg['entry_timeframe'] = str(cfg.get('entry_timeframe') or '15m').strip().lower() or '15m'
        cfg['exit_timeframe'] = str(cfg.get('exit_timeframe') or cfg['entry_timeframe']).strip().lower() or cfg['entry_timeframe']
        cfg['htf_timeframe'] = str(cfg.get('htf_timeframe') or '1h').strip().lower() or '1h'
        for key in (
            'use_heikin_ashi',
            'exclude_rsi_extreme',
            'daily_profit_target_enabled',
            'opposite_signal_exit_enabled',
            'opposite_set_exit_enabled',
            'opposite_set_exit_min_pnl_enabled',
            'ema_rsi_exit_enabled',
            'adx_donchian_exit_enabled'
        ):
            cfg[key] = bool(cfg.get(key, False))
        # A-option (opposite UT signal only) is intentionally rejected for UT Breakout.
        # Only the stricter opposite-set exit can be enabled from Telegram.
        cfg['opposite_signal_exit_enabled'] = False
        if cfg['atr_max_percent'] < cfg['atr_min_percent']:
            cfg['atr_max_percent'] = cfg['atr_min_percent']
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
            'keltner_upper': keltner_upper_value,
            'keltner_lower': keltner_lower_value,
            'keltner_mid': keltner_mid_value,
            'keltner_width_pct': keltner_width_pct,
            'keltner_width_prev_pct': keltner_width_prev_pct,
            'candle_range_pct': candle_range_pct,
            'avg_range20_pct': avg_range20_pct,
            'avg_range50_pct': avg_range50_pct,
            'range_expansion_ratio': range_expansion_ratio,
            'range_compression_ratio': range_compression_ratio,
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
        bb_expansion_score = _avg(bb_expansion_scores, 40.0)
        if squeeze_release_scores:
            squeeze_release = _avg(squeeze_release_scores, 40.0)
            bb_expansion_score = (bb_expansion_score + squeeze_release) / 2.0

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
        return {
            'timeframes': timeframe_metrics,
            'scores': scores,
            'errors': errors,
        }

    def _select_utbreakout_auto_set(self, analysis, cfg):
        raw_scores = (analysis or {}).get('scores') if isinstance(analysis, dict) else None
        scores = dict(raw_scores or {})
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
        ready = int(scores.get('ready_timeframes', 0) or 0)

        if ready <= 0:
            return 50, "AUTO 분석 데이터 부족: Set50 emergency simple mode"

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
        }
        selected_id, selected_score = max(candidate_scores.items(), key=lambda item: item[1])
        top3 = sorted(candidate_scores.items(), key=lambda item: item[1], reverse=True)[:3]
        top3_text = ", ".join(f"Set{set_id}:{score:.1f}" for set_id, score in top3)
        second_score = top3[1][1] if len(top3) > 1 else 0.0
        score_margin = selected_score - second_score
        if isinstance(raw_scores, dict):
            raw_scores['auto_candidate_scores'] = {
                f"Set{set_id}": round(float(score), 2)
                for set_id, score in sorted(candidate_scores.items())
            }
            raw_scores['auto_top3'] = [
                {'set_id': int(set_id), 'score': round(float(score), 2)}
                for set_id, score in top3
            ]
            raw_scores['auto_selected_score'] = round(float(selected_score), 2)
            raw_scores['auto_score_margin'] = round(float(score_margin), 2)
            raw_scores['auto_confidence'] = 'weak' if selected_score < 50.0 or score_margin < 3.0 else 'normal'
        if selected_score < 50.0:
            selected_id = 2 if volatility < 45.0 else 49 if fallback >= 55.0 else 1
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
                f"-> Set{selected_id} score {selected_score:.1f} "
                f"(top {top3_text})"
            )
        if isinstance(raw_scores, dict):
            raw_scores['auto_final_set_id'] = int(selected_id)
        return selected_id, reason

    async def _resolve_utbreakout_selected_set(self, symbol, df, cfg):
        analysis = None
        auto_reason = None
        selected_id = normalize_utbreakout_set_id(cfg.get('active_set_id'), UTBREAKOUT_DEFAULT_SET_ID)
        if bool(cfg.get('auto_select_enabled', False)) or str(cfg.get('selection_mode', '')).lower() == 'auto':
            try:
                analysis = await self._build_utbreakout_auto_analysis(symbol, df, cfg)
                selected_id, auto_reason = self._select_utbreakout_auto_set(analysis, cfg)
            except Exception as exc:
                auto_reason = f"AUTO 분석 실패: {exc}. 수동 Set{selected_id} 유지"
        selected_info = self._get_utbreakout_set_info(cfg, selected_id)
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
                if not self._is_valid_number(volume_ratio):
                    _add('상대 거래량', None, '상대 거래량 계산 대기', 'REJECTED_RSI_MOMENTUM')
                else:
                    ok = float(volume_ratio) >= 1.20
                    _add('상대 거래량', ok, f"relVol {_fmt(volume_ratio, 2)} / 조건 >= 1.20", 'REJECTED_RSI_MOMENTUM')
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
            else:
                _add(filter_name, None, 'planned 필터: 아직 실거래 연결 안 됨', 'REJECTED_RISK_REWARD_LOW')
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

    def _clear_utbot_filtered_breakout_entry_plan(self, symbol):
        self.utbot_filtered_breakout_entry_plans.pop(symbol, None)

    def _get_utbot_filtered_breakout_entry_plan(self, symbol, side=None):
        plan = self.utbot_filtered_breakout_entry_plans.get(symbol)
        if not isinstance(plan, dict):
            return None
        if side and str(plan.get('side', '')).lower() != str(side).lower():
            return None
        return plan

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
            note=status.get('reject_code') or status.get('accepted_code') or status.get('reason')
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
                'auto_scores': status.get('auto_scores'),
                'auto_top3': auto_scores.get('auto_top3'),
                'auto_score_margin': _safe_float_or_none(auto_scores.get('auto_score_margin')),
                'auto_confidence': auto_scores.get('auto_confidence'),
                'set_filters': status.get('set_filters'),
                'entry_timeframe': status.get('entry_timeframe'),
                'htf_timeframe': status.get('htf_timeframe'),
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
                'htf_summary': status.get('htf_summary'),
                'metric_summary': status.get('metric_summary'),
                'risk_summary': status.get('risk_summary'),
                'funding_rate': _safe_float_or_none(status.get('funding_rate')),
                'next_funding_time': status.get('next_funding_time'),
                'open_interest': _safe_float_or_none(status.get('open_interest')),
                'mark_price': _safe_float_or_none(status.get('mark_price')),
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
                    'rr_multiple': _safe_float_or_none(plan.get('rr_multiple'))
                })
            if isinstance(extra, dict):
                for key, value in extra.items():
                    if key not in payload:
                        payload[key] = value
            clean_payload = {k: v for k, v in payload.items() if v is not None}
            utbreakout_diag_logger.info(json.dumps(clean_payload, ensure_ascii=False, separators=(',', ':')))
        except Exception as e:
            logger.debug(f"UT breakout diagnostic log write failed: {e}")

    async def _calculate_utbot_filtered_breakout_signal(self, symbol, df, strategy_params):
        cfg = self._get_utbot_filtered_breakout_config(strategy_params)
        self._clear_utbot_filtered_breakout_entry_plan(symbol)
        status = {
            'strategy': 'UTBOT_FILTERED_BREAKOUT_V1',
            'stage': 'evaluate',
            'entry_timeframe': cfg.get('entry_timeframe', '15m'),
            'htf_timeframe': cfg.get('htf_timeframe', '1h'),
            'utbot_key_value': cfg.get('utbot_key_value'),
            'utbot_atr_period': cfg.get('utbot_atr_period'),
            'use_heikin_ashi': cfg.get('use_heikin_ashi', False)
        }

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
            if record_failure and side:
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
        cfg = effective_cfg
        selected_filters = set(selected_set.get('entry_filters') or [])
        status.update({
            'selection_mode': 'auto' if cfg.get('auto_select_enabled') else 'manual',
            'auto_select_enabled': bool(cfg.get('auto_select_enabled', False)),
            'auto_selected_set_id': selected_set.get('id'),
            'auto_selected_set_name': selected_set.get('name'),
            'auto_selected_set_family': selected_set.get('family'),
            'auto_selected_set_status': selected_set.get('status'),
            'auto_selection_reason': auto_reason,
            'auto_scores': (auto_analysis or {}).get('scores') if isinstance(auto_analysis, dict) else None,
            'set_filters': list(selected_set.get('entry_filters') or []),
            'utbot_key_value': cfg.get('utbot_key_value'),
            'utbot_atr_period': cfg.get('utbot_atr_period'),
        })

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
        recent_pnls = self.db.get_recent_closed_trade_pnls(max_losses, today_only=True)
        status['recent_closed_pnls'] = recent_pnls
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
        try:
            futures_context = await self._fetch_utbreakout_futures_context(symbol)
            if futures_context:
                status.update(futures_context)
        except Exception as e:
            status['futures_context_error'] = str(e)

        filter_values = {
            'entry_price': entry_price,
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
            'keltner_upper': entry_metrics.get('keltner_upper'),
            'keltner_lower': entry_metrics.get('keltner_lower'),
            'keltner_mid': entry_metrics.get('keltner_mid'),
            'keltner_width_pct': entry_metrics.get('keltner_width_pct'),
            'keltner_width_prev_pct': entry_metrics.get('keltner_width_prev_pct'),
            'range_expansion_ratio': entry_metrics.get('range_expansion_ratio'),
            'range_compression_ratio': entry_metrics.get('range_compression_ratio'),
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
        }
        filter_items = self._evaluate_utbreakout_set_filter_items(side, selected_set, cfg, filter_values)
        status['set_filter_items'] = filter_items
        for item in filter_items:
            if item.get('state') is not True:
                code = item.get('code') or 'REJECTED_RISK_REWARD_LOW'
                return _finish(
                    None,
                    f"{code}: Set{selected_set.get('id')} {item.get('name')} - {item.get('detail')}",
                    code,
                    record_failure=True,
                    side=side
                )

        if not self._is_valid_number(atr_value) or float(atr_value) <= 0:
            return _finish(None, "REJECTED_ATR_TOO_LOW: ATR risk distance calculation pending", 'REJECTED_ATR_TOO_LOW', record_failure=True, side=side)

        total_balance, free_balance, _ = await self.get_balance_info()
        balance_for_risk = total_balance if total_balance > 0 else free_balance
        common_cfg = self.get_runtime_common_settings()
        leverage = int(max(1.0, float(common_cfg.get('leverage', 5) or 5)))
        try:
            plan = calculate_risk_plan(
                side=side,
                entry_price=entry_price,
                atr_value=atr_value,
                stop_atr_multiplier=cfg.get('stop_atr_multiplier', 1.5),
                ut_stop=ut_detail.get('curr_stop'),
                take_profit_r_multiple=cfg.get('take_profit_r_multiple', 2.0),
                min_risk_reward=cfg.get('min_risk_reward', 2.0),
                balance_usdt=balance_for_risk,
                risk_per_trade_percent=cfg.get('risk_per_trade_percent', 1.0),
                max_risk_per_trade_usdt=cfg.get('max_risk_per_trade_usdt', 1.0),
                leverage=leverage,
            )
        except ValueError as e:
            return _finish(None, f"REJECTED_RISK_REWARD_LOW: {e}", 'REJECTED_RISK_REWARD_LOW', record_failure=True, side=side)
        plan.update({
            'strategy': UTBOT_FILTERED_BREAKOUT_STRATEGY,
            'atr': atr_value,
            'atr_pct': atr_pct,
            'decision_candle_ts': decision_ts
        })
        self.utbot_filtered_breakout_entry_plans[symbol] = plan
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

    async def build_utbreakout_condition_status_text(self, symbol):
        cfg = self._get_utbot_filtered_breakout_config(self.get_runtime_strategy_params())
        common_cfg = self.get_runtime_common_settings()
        lev = int(max(1.0, float(common_cfg.get('leverage', 5) or 5)))
        entry_tf = cfg.get('entry_timeframe', '15m')
        htf_tf = cfg.get('htf_timeframe', '1h')

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
            return "🚦 UT Breakout 조건 스테이터스\n\n업비트 현물 모드에서는 UTBOT_FILTERED_BREAKOUT_V1을 사용하지 않습니다."

        try:
            ohlcv = await asyncio.to_thread(
                self.market_data_exchange.fetch_ohlcv,
                symbol,
                entry_tf,
                limit=300
            )
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        except Exception as e:
            return f"🚦 UT Breakout 조건 스테이터스\n\n{symbol} {entry_tf} 데이터 조회 실패: {e}"

        if df is None or len(df) < 5:
            return f"🚦 UT Breakout 조건 스테이터스\n\n{symbol} {entry_tf} 데이터 부족"

        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        closed = df.iloc[:-1].copy().dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
        if len(closed) < 5:
            return f"🚦 UT Breakout 조건 스테이터스\n\n{symbol} {entry_tf} 유효 데이터 부족"

        selected_set, auto_analysis, auto_reason = await self._resolve_utbreakout_selected_set(symbol, df, cfg)
        effective_cfg = dict(cfg)
        effective_cfg.update(selected_set.get('params', {}) if isinstance(selected_set, dict) else {})
        effective_cfg['active_set_id'] = selected_set.get('id', cfg.get('active_set_id', UTBREAKOUT_DEFAULT_SET_ID))
        effective_cfg['profile'] = f"set{effective_cfg['active_set_id']}"
        cfg = effective_cfg
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

        daily_count, daily_pnl = self.db.get_daily_stats()
        daily_entries = self.db.get_daily_entry_count()
        max_losses = int(cfg.get('max_consecutive_losses', 3) or 3)
        recent_pnls = self.db.get_recent_closed_trade_pnls(max_losses, today_only=True)
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
        atr_value = metrics.get('atr')
        atr_pct = metrics.get('atr_pct')
        if self._is_valid_number(atr_value):
            try:
                total_balance, free_balance, _ = await self.get_balance_info()
                balance_for_risk = total_balance if total_balance > 0 else free_balance
                side_for_plan = candidate_side if candidate_side in {'long', 'short'} else 'long'
                plan = calculate_risk_plan(
                    side=side_for_plan,
                    entry_price=entry_price,
                    atr_value=atr_value,
                    stop_atr_multiplier=cfg.get('stop_atr_multiplier', 1.5),
                    ut_stop=ut_detail.get('curr_stop'),
                    take_profit_r_multiple=cfg.get('take_profit_r_multiple', 2.0),
                    min_risk_reward=cfg.get('min_risk_reward', 2.0),
                    balance_usdt=balance_for_risk,
                    risk_per_trade_percent=cfg.get('risk_per_trade_percent', 1.0),
                    max_risk_per_trade_usdt=cfg.get('max_risk_per_trade_usdt', 1.0),
                    leverage=lev,
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
                risk_ok = True
                balance_detail = (
                    f"손실한도 {_fmt(risk_usdt, 2)} USDT / 손절거리 {_fmt(risk_distance, 4)} "
                    f"({_fmt(risk_distance_pct, 3)}%) / qty {_fmt(planned_qty, 6)}"
                )
                entry_plan_detail = (
                    f"진입 계획: 증거금 {_fmt(planned_margin, 2)} USDT / "
                    f"포지션 {_fmt(planned_notional, 2)} USDT / 레버리지 {lev}x / "
                    f"손절시 손실 {_fmt(risk_usdt, 2)} USDT"
                )
                take_profit_detail = (
                    f"익절 계획: {_fmt(rr_multiple, 1)}R / 익절거리 {_fmt(take_profit_distance, 4)} "
                    f"({_fmt(take_profit_pct, 3)}%) / 예상수익 {_fmt(expected_profit_usdt, 2)} USDT"
                )
            except Exception as e:
                balance_detail = f"잔고 조회 실패 {e}"
                entry_plan_detail = f"진입 계획: 잔고 조회 실패 {e}"
                take_profit_detail = f"익절 계획: 잔고 조회 실패 {e}"
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

        def _side_conditions(side):
            side_upper = side.upper()
            if candidate_side == side:
                ut_state = True
                ut_detail_text = f"{side_upper} {candidate_type}"
            else:
                ut_state = False if candidate_side in {'long', 'short'} else None
                ut_detail_text = f"현재 {str(candidate_side or 'none').upper()} / bias {str(ut_bias_side or 'none').upper()}"

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
                'keltner_upper': metrics.get('keltner_upper'),
                'keltner_lower': metrics.get('keltner_lower'),
                'keltner_mid': metrics.get('keltner_mid'),
                'keltner_width_pct': metrics.get('keltner_width_pct'),
                'keltner_width_prev_pct': metrics.get('keltner_width_prev_pct'),
                'range_expansion_ratio': metrics.get('range_expansion_ratio'),
                'range_compression_ratio': metrics.get('range_compression_ratio'),
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
            }
            selected_items = self._evaluate_utbreakout_set_filter_items(side, selected_set, cfg, filter_values)
            items = [
                ("UTBot 방향", ut_state, ut_detail_text),
            ]
            items.extend((item.get('name'), item.get('state'), item.get('detail')) for item in selected_items)
            items.extend([
                ("일일 리스크", daily_ok, daily_detail),
                ("ATR 손절/RR/수량", risk_ok, balance_detail),
            ])
            ok = all(item[1] is True for item in items)
            lines = [f"{side_upper}: {'진입 가능' if ok else '대기'}"]
            lines.extend(_line(idx, label, state, detail) for idx, (label, state, detail) in enumerate(items, 1))
            return ok, lines

        long_ok, long_lines = _side_conditions('long')
        short_ok, short_lines = _side_conditions('short')
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
        text_lines = [
            "🚦 UT Breakout 조건 스테이터스",
            f"심볼: {symbol}",
            f"TF: 진입 {entry_tf} / HTF {htf_tf}",
            f"마지막 마감봉: {_fmt_ts(decision_ts)} / close {_fmt(entry_price, 4)}",
            f"현재 UTBot 방향: {ut_label}",
            f"선택모드: {mode_label}",
            f"선택 Set: Set{selected_set.get('id')} {selected_set.get('name')} ({set_status})",
            f"선택 이유: {auto_reason or '수동 선택'}",
            entry_plan_detail,
            take_profit_detail,
            opposite_set_exit_detail,
            f"AUTO 점수: {score_line}",
            "주의: AUTO/MTF 지표는 set 선택용이고, 실제 진입은 아래 선택 Set 조건만 봅니다.",
            f"최종: LONG {'가능' if long_ok else '대기'} / SHORT {'가능' if short_ok else '대기'}",
            "",
            *long_lines,
            "",
            *short_lines,
            "",
            f"UT 사유: {ut_reason}"
        ]
        return "\n".join(text_lines)

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
            'keltner_upper': metrics.get('keltner_upper'),
            'keltner_lower': metrics.get('keltner_lower'),
            'keltner_mid': metrics.get('keltner_mid'),
            'keltner_width_pct': metrics.get('keltner_width_pct'),
            'keltner_width_prev_pct': metrics.get('keltner_width_prev_pct'),
            'range_expansion_ratio': metrics.get('range_expansion_ratio'),
            'range_compression_ratio': metrics.get('range_compression_ratio'),
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
        }
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
                        await self._cancel_protection_orders(
                            self.scanner_active_symbol,
                            reason='scanner position completed'
                        )
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
        self._ensure_runtime_state_containers((
            'last_processed_candle_ts',
            'last_state_sync_candle_ts',
            'last_stateful_retry_ts',
            'last_processed_exit_candle_ts',
            'last_candle_time',
            'fisher_states',
            'vbo_states',
            'cameron_states'
        ))
            
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
            active_strategy = strategy_params.get('active_strategy', 'utbot').lower()
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

            if active_strategy == 'utsmc':
                live_candle_ts = int(ohlcv_p[-1][0])
                pos_side = await self._maybe_execute_utsmc_live_entry(
                    symbol,
                    live_candle_ts,
                    current_price,
                    pos_side,
                    strategy_name='UTSMC'
                )
            elif (not self.is_upbit_mode()) and (active_strategy == 'utbot' or active_strategy in UT_HYBRID_STRATEGIES):
                live_candle_ts = int(ohlcv_p[-1][0])
                pos_side = await self._maybe_execute_ut_live_entry(
                    symbol,
                    live_candle_ts,
                    current_price,
                    pos_side
                )
                
            # 3. Check Exit TF (Exit Logic)
            # Cross/Position 紐⑤뱶?먯꽌留?Secondary TF 泥?궛 濡쒖쭅 ?ъ슜
            uses_secondary_exit = (
                pos_side != 'NONE'
                and (
                    (active_strategy in MA_STRATEGIES and entry_mode in ['cross', 'position'])
                    or active_strategy == 'cameron'
                    or active_strategy == 'utsmc'
                    or active_strategy == 'utbot'
                    or active_strategy == UTBOT_FILTERED_BREAKOUT_STRATEGY
                    or active_strategy in UT_HYBRID_STRATEGIES
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
                    active_strategy = scan_params.get('active_strategy', 'utbot').lower()
                    if active_strategy not in CORE_STRATEGIES:
                        active_strategy = 'utbot'
                    if active_strategy == UTBOT_FILTERED_BREAKOUT_STRATEGY:
                        scan_tf = self._get_utbot_filtered_breakout_config(scan_params).get('entry_timeframe', '15m')
                    
                    # Use scanner_timeframe if set, but ensure we are thinking about consistency
                    ohlcv = await asyncio.to_thread(self.market_data_exchange.fetch_ohlcv, symbol, scan_tf, limit=300)
                    if not ohlcv: continue
                    
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    
                    strategy_context = self._collect_primary_strategy_context(symbol, df, scan_params, active_strategy)
                    sig, _, _, _, _, _ = await self._calculate_strategy_signal(
                        symbol, df, scan_params, active_strategy, allow_utbot_stateful=False,
                        precomputed=strategy_context.get('precomputed')
                    )
                    
                    if sig:
                        if active_strategy == 'utbot':
                            utbot_entry_filter_eval = await self._evaluate_utbot_filter_pack(
                                symbol,
                                df,
                                scan_params,
                                scan_tf,
                                'entry'
                            )
                            allowed, filter_reason = self._utbot_filter_pack_allows_entry(utbot_entry_filter_eval, sig)
                            if not allowed:
                                logger.info(
                                    f"🚫 Scanner Skip {symbol}: UTBot filter pack blocked "
                                    f"({filter_reason})"
                                )
                                continue
                            utbot_rsi_momentum_entry_eval = self._evaluate_utbot_rsi_momentum_filter(
                                df,
                                scan_params,
                                'entry'
                            )
                            allowed, filter_reason = self._utbot_rsi_momentum_filter_allows(
                                utbot_rsi_momentum_entry_eval,
                                sig
                            )
                            if not allowed:
                                logger.info(
                                    f"🚫 Scanner Skip {symbol}: RSI Momentum Trend filter blocked "
                                    f"({filter_reason})"
                                )
                                continue
                        elif active_strategy == 'utsmc':
                            utsmc_detail = strategy_context.get('raw_hybrid_detail', {}) or {}
                            allowed, candidate_reason = self._utsmc_candidate_filter_allows(utsmc_detail, sig, is_persistent=False)
                            if not allowed:
                                logger.info(
                                    f"?? Scanner Skip {symbol}: UTSMC candidate filter blocked "
                                    f"({utsmc_detail.get('candidate_filter_mode_label', 'OFF')} | {candidate_reason})"
                                )
                                continue
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
            'd': 24 * 60 * 60 * 1000,
            'w': 7 * 24 * 60 * 60 * 1000
        }
        try:
            tf = str(tf or '').strip().lower()
            if not tf:
                return 0
            if tf.isdigit():
                return int(tf) * multipliers['m']
            unit = tf[-1]
            value = int(tf[:-1])
            return value * multipliers.get(unit, 0)
        except:
            return 0

    def _timeframes_equivalent(self, tf_a, tf_b):
        tf_a = str(tf_a or '').strip().lower()
        tf_b = str(tf_b or '').strip().lower()
        if not tf_a or not tf_b:
            return False
        ms_a = self._timeframe_to_ms(tf_a)
        ms_b = self._timeframe_to_ms(tf_b)
        if ms_a > 0 and ms_b > 0:
            return ms_a == ms_b
        return tf_a == tf_b

    async def check_status(self, symbol, price):
        try:
            total, free, mmr = await self.get_balance_info()
            count, daily_pnl = self.db.get_daily_stats()
            pos = await self.get_server_position(symbol, use_cache=False)
            
            # ?꾨왂 ?곹깭 媛?몄삤湲?
            strategy_params = self.get_runtime_strategy_params()
            comm_cfg = self.get_runtime_common_settings()
            active_strategy = strategy_params.get('active_strategy', 'utbot').upper()
            entry_mode = strategy_params.get('entry_mode', 'cross').upper()
            if active_strategy in {'UTBOT', 'UTSMC', 'RSIBB', 'UTRSIBB', 'UTRSI', 'UTBB', 'UTBOT_FILTERED_BREAKOUT_V1'}:
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
                'kalman_enabled': False,
                'kalman_velocity': 0.0,
                'kalman_direction': None,
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
            
            symbol_status['entry_filters'] = {}
            symbol_status['exit_filters'] = {}
            symbol_status['filter_config'] = {}
            utbot_filter_pack_status = dict(self.last_utbot_filter_pack_status.get(symbol, {}) or {})
            utbot_filter_pack_status.setdefault('config', self._get_utbot_filter_pack(strategy_params))
            symbol_status['utbot_filter_pack'] = utbot_filter_pack_status
            utbot_rsi_momentum_status = dict(self.last_utbot_rsi_momentum_filter_status.get(symbol, {}) or {})
            utbot_rsi_momentum_status.setdefault('config', self._get_utbot_rsi_momentum_filter_config(strategy_params))
            symbol_status['utbot_rsi_momentum_filter'] = utbot_rsi_momentum_status
            candidate_status = self.last_utsmc_candidate_filter_status.get(symbol, {})
            symbol_status['utsmc_candidate_filter_mode'] = candidate_status.get('mode_label', 'OFF')
            symbol_status['utsmc_candidate_filter'] = candidate_status
            symbol_status['utbot_filtered_breakout'] = self.last_utbot_filtered_breakout_status.get(symbol, {})
            tp_master_enabled = False if self.is_upbit_mode() else bool(comm_cfg.get('tp_sl_enabled', True))
            tp_enabled = tp_master_enabled and bool(comm_cfg.get('take_profit_enabled', True))
            sl_enabled = tp_master_enabled and bool(comm_cfg.get('stop_loss_enabled', True))
            expected_tp, expected_sl = self._protection_expected_from_config(symbol, pos)
            protection_audit = await self._audit_protection_orders(
                symbol,
                pos=pos,
                expected_tp=expected_tp,
                expected_sl=expected_sl,
                alert=True
            )
            symbol_status['protection_config'] = {
                'tp_enabled': tp_enabled,
                'sl_enabled': sl_enabled,
                'tp_expected': expected_tp,
                'sl_expected': expected_sl,
                'tp_present': protection_audit.get('tp_present', False),
                'sl_present': protection_audit.get('sl_present', False),
                'tp_count': protection_audit.get('tp_count', 0),
                'sl_count': protection_audit.get('sl_count', 0),
                'missing_tp': protection_audit.get('missing_tp', False),
                'missing_sl': protection_audit.get('missing_sl', False),
                'audit_status': protection_audit.get('status', 'UNKNOWN')
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
        if 'smc_reason' in kwargs:
            next_reason = kwargs.get('smc_reason')
            prev_reason = current.get('smc_reason')
            if next_reason:
                if next_reason != prev_reason:
                    reason_now = datetime.now()
                    current['smc_reason_ts'] = int(reason_now.timestamp())
                    current['smc_reason_ts_human'] = reason_now.strftime('%m-%d %H:%M:%S')
            else:
                current.pop('smc_reason_ts', None)
                current.pop('smc_reason_ts_human', None)
        current.update(kwargs)
        self.last_stateful_diag[symbol] = current

    async def _notify_stateful_diag(self, symbol, force=False):
        reporting_cfg = self.cfg.get('telegram', {}).get('reporting', {})
        if not reporting_cfg.get('periodic_reports_enabled', False):
            return
        if not reporting_cfg.get('stateful_diag_enabled', False):
            return
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
            str(info.get('utsmc_candidate_filter_mode', '')),
            str(info.get('utsmc_candidate_final_long_pass', '')),
            str(info.get('utsmc_candidate_final_short_pass', '')),
            str(info.get('ut_pending_source', '')),
            str(info.get('ut_pending_side', '')),
            str(info.get('ut_pending_state', '')),
            str(info.get('ut_pending_execute_ts_human', '')),
            str(info.get('ut_flip_target_side', '')),
            str(info.get('ut_flip_retry_count', '')),
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
        pending_visible = any([
            info.get('ut_pending_source'),
            info.get('ut_pending_side'),
            info.get('ut_pending_state'),
            info.get('ut_pending_execute_ts_human')
        ])
        if pending_visible:
            pending_label = (
                f"{info.get('ut_pending_source')}:{info.get('ut_pending_side', '-')}"
                if info.get('ut_pending_source') else
                f"{info.get('ut_pending_side', '-')}"
            )
            pending_line = (
                f"pending={pending_label}"
                f" state={info.get('ut_pending_state', '-')}"
                f" execute={info.get('ut_pending_execute_ts_human', '-')}"
            )
            if info.get('ut_pending_expires_ts_human'):
                pending_line += f" expires={info.get('ut_pending_expires_ts_human')}"
            lines.append(pending_line)
        if info.get('ut_flip_target_side'):
            flip_line = (
                f"flip_target={info.get('ut_flip_target_side')} "
                f"signal={info.get('ut_flip_signal_ts_human', '-')}"
            )
            if info.get('ut_flip_retry_count') is not None:
                flip_line += f" retry={info.get('ut_flip_retry_count')}"
            if info.get('ut_flip_block_reason'):
                flip_line += f" block={info.get('ut_flip_block_reason')}"
            lines.append(flip_line)
        if info.get('utsmc_candidate_filter_mode'):
            lines.append(
                f"candidate={info.get('utsmc_candidate_filter_mode')} "
                f"long={'PASS' if info.get('utsmc_candidate_final_long_pass') else 'FAIL'} "
                f"short={'PASS' if info.get('utsmc_candidate_final_short_pass') else 'FAIL'}"
            )
        if info.get('closed_ohlc_text'):
            lines.append(f"closed={info.get('closed_ohlc_text')}")
        if info.get('live_ohlc_text'):
            lines.append(f"live={info.get('live_ohlc_text')}")
        note = info.get('note')
        if note:
            lines.append(f"note={note}")
        await self.ctrl.notify("\n".join(lines))

    def _fmt_signal_trade_value(self, value, digits=2, signed=False):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return '-'
        prefix = '+' if signed and number >= 0 else ''
        quote_currency = self.get_quote_currency()
        if str(quote_currency).upper() == 'KRW':
            return f"{prefix}{number:,.0f}"
        return f"{prefix}{number:.{digits}f}"

    def _append_signal_diag_lines(self, lines, diag, *, include_exit=False):
        if not isinstance(diag, dict) or not diag:
            return

        if diag.get('strategy'):
            lines.append(
                f"진단: stage `{diag.get('stage', '-')}` | raw `{diag.get('raw_state', '-')}` | "
                f"entry `{diag.get('entry_sig', '-')}` | pos `{diag.get('pos_side', '-')}`"
            )
        if diag.get('ut_key') is not None:
            lines.append(
                f"UT 설정: K `{diag.get('ut_key')}` | ATR `{diag.get('ut_atr')}` | HA `{diag.get('ut_ha', '-')}`"
            )
        if diag.get('src') is not None and diag.get('stop') is not None:
            lines.append(
                f"UT 값: src `{self._fmt_signal_trade_value(diag.get('src'))}` | "
                f"stop `{self._fmt_signal_trade_value(diag.get('stop'))}`"
            )
        if diag.get('signal_ts_human') or diag.get('feed_last_ts_human'):
            lines.append(
                f"UT 기준봉: tf `{diag.get('tf_used', '?')}` | signal `{diag.get('signal_ts_human', '?')}` | "
                f"feed_last `{diag.get('feed_last_ts_human', '?')}`"
            )
        if (
            diag.get('signal_open') is not None or diag.get('signal_high') is not None
            or diag.get('signal_low') is not None or diag.get('signal_close') is not None
        ):
            lines.append(
                f"UT 기준값: O `{self._fmt_signal_trade_value(diag.get('signal_open'))}` | "
                f"H `{self._fmt_signal_trade_value(diag.get('signal_high'))}` | "
                f"L `{self._fmt_signal_trade_value(diag.get('signal_low'))}` | "
                f"C `{self._fmt_signal_trade_value(diag.get('signal_close'))}`"
            )
        elif diag.get('closed_ohlc_text'):
            lines.append(f"UT 확정봉: `{diag.get('closed_ohlc_text')}`")
        pending_visible = any([
            diag.get('ut_pending_source'),
            diag.get('ut_pending_side'),
            diag.get('ut_pending_state'),
            diag.get('ut_pending_execute_ts_human')
        ])
        if pending_visible:
            pending_line = "UT pending: "
            if diag.get('ut_pending_source'):
                pending_line += f"source `{diag.get('ut_pending_source')}` | "
            pending_line += (
                f"side `{diag.get('ut_pending_side', '-')}` | "
                f"state `{diag.get('ut_pending_state', '-')}` | "
                f"execute `{diag.get('ut_pending_execute_ts_human', '-')}`"
            )
            if diag.get('ut_pending_expires_ts_human'):
                pending_line += f" | expires `{diag.get('ut_pending_expires_ts_human')}`"
            lines.append(pending_line)
        if diag.get('ut_flip_target_side'):
            flip_line = (
                f"UTBOT flip: target `{diag.get('ut_flip_target_side')}` | "
                f"signal `{diag.get('ut_flip_signal_ts_human', '-')}`"
            )
            if diag.get('ut_flip_execute_ts_human'):
                flip_line += f" | execute `{diag.get('ut_flip_execute_ts_human')}`"
            if diag.get('ut_flip_expires_ts_human'):
                flip_line += f" | expires `{diag.get('ut_flip_expires_ts_human')}`"
            if diag.get('ut_flip_retry_count') is not None:
                flip_line += f" | retry `{diag.get('ut_flip_retry_count')}`"
            lines.append(flip_line)
        if diag.get('ut_flip_block_reason'):
            lines.append(f"UTBOT flip 차단사유: `{diag.get('ut_flip_block_reason')}`")

        if diag.get('utsmc_entry_smc_reason'):
            lines.append(f"UTSMC 진입판정: `{diag.get('utsmc_entry_smc_reason')}`")
        if diag.get('utsmc_signal_high') is not None or diag.get('utsmc_signal_low') is not None:
            lines.append(
                f"UTSMC invalidation: high `{self._fmt_signal_trade_value(diag.get('utsmc_signal_high'))}` | "
                f"low `{self._fmt_signal_trade_value(diag.get('utsmc_signal_low'))}`"
            )
        if (
            diag.get('utsmc_entry_bullish_ob_entry') is not None
            or diag.get('utsmc_entry_bearish_ob_entry') is not None
            or diag.get('utsmc_entry_ob_tags')
        ):
            smc_entry_bull = 'Y' if diag.get('utsmc_entry_bullish_ob_entry') else 'N'
            smc_entry_bear = 'Y' if diag.get('utsmc_entry_bearish_ob_entry') else 'N'
            smc_entry_tf = diag.get('utsmc_entry_tf')
            line = f"UTSMC entry OB{f'[{smc_entry_tf}]' if smc_entry_tf else ''}: bull `{smc_entry_bull}` | bear `{smc_entry_bear}`"
            if diag.get('utsmc_entry_ob_tags'):
                line += f" | tags `{diag.get('utsmc_entry_ob_tags')}`"
            lines.append(line)
        if (
            diag.get('utsmc_entry_bullish_ob_bottom') is not None
            or diag.get('utsmc_entry_bullish_ob_top') is not None
            or diag.get('utsmc_entry_bearish_ob_bottom') is not None
            or diag.get('utsmc_entry_bearish_ob_top') is not None
        ):
            lines.append(
                f"UTSMC entry OB range: "
                f"bull `{self._fmt_signal_trade_value(diag.get('utsmc_entry_bullish_ob_bottom'))}` ~ "
                f"`{self._fmt_signal_trade_value(diag.get('utsmc_entry_bullish_ob_top'))}` | "
                f"bear `{self._fmt_signal_trade_value(diag.get('utsmc_entry_bearish_ob_bottom'))}` ~ "
                f"`{self._fmt_signal_trade_value(diag.get('utsmc_entry_bearish_ob_top'))}`"
            )
        if diag.get('utsmc_candidate_filter_mode'):
            c1_long = 'PASS' if diag.get('utsmc_candidate1_long_pass') else 'FAIL'
            c2_long = 'PASS' if diag.get('utsmc_candidate2_long_pass') else 'FAIL'
            final_long = 'PASS' if diag.get('utsmc_candidate_final_long_pass') else 'FAIL'
            c1_short = 'PASS' if diag.get('utsmc_candidate1_short_pass') else 'FAIL'
            c2_short = 'PASS' if diag.get('utsmc_candidate2_short_pass') else 'FAIL'
            final_short = 'PASS' if diag.get('utsmc_candidate_final_short_pass') else 'FAIL'
            lines.append(
                f"UTSMC 후보필터: mode `{diag.get('utsmc_candidate_filter_mode')}` | "
                f"LONG `C1 {c1_long} / C2 {c2_long} / FINAL {final_long}` | "
                f"SHORT `C1 {c1_short} / C2 {c2_short} / FINAL {final_short}`"
            )
            if diag.get('utsmc_candidate_reason_long'):
                lines.append(f"UTSMC 후보필터 LONG: `{diag.get('utsmc_candidate_reason_long')}`")
            if diag.get('utsmc_candidate_reason_short'):
                lines.append(f"UTSMC 후보필터 SHORT: `{diag.get('utsmc_candidate_reason_short')}`")

        if diag.get('utbreakout_reason') or diag.get('utbreakout_reject_code'):
            lines.append(
                f"Filtered Breakout: `{diag.get('utbreakout_reject_code') or 'ACCEPTED_ENTRY'}` | "
                f"`{diag.get('utbreakout_reason', '-')}`"
            )
        if diag.get('utbreakout_htf'):
            lines.append(f"HTF 필터: `{diag.get('utbreakout_htf')}`")
        if diag.get('utbreakout_metrics'):
            lines.append(f"지표 필터: `{diag.get('utbreakout_metrics')}`")
        if diag.get('utbreakout_risk'):
            lines.append(f"리스크 계획: `{diag.get('utbreakout_risk')}`")

        if include_exit:
            if (
                diag.get('utsmc_fixed_ob_side')
                or diag.get('utsmc_fixed_ob_bottom') is not None
                or diag.get('utsmc_fixed_ob_top') is not None
            ):
                lines.append(
                    f"UTSMC 고정 청산 OB: side `{diag.get('utsmc_fixed_ob_side', '-')}` | "
                    f"range `{self._fmt_signal_trade_value(diag.get('utsmc_fixed_ob_bottom'))}` ~ "
                    f"`{self._fmt_signal_trade_value(diag.get('utsmc_fixed_ob_top'))}`"
                )
            if diag.get('utsmc_fixed_ob_created_ts_human') or diag.get('utsmc_fixed_ob_origin_ts_human'):
                lines.append(
                    f"UTSMC 고정 OB 시각: created `{diag.get('utsmc_fixed_ob_created_ts_human', '-')}` | "
                    f"origin `{diag.get('utsmc_fixed_ob_origin_ts_human', '-')}`"
                )
            if diag.get('exit_reason_text'):
                lines.append(f"청산판정: `{diag.get('exit_reason_text')}`")
            if diag.get('exit_trigger_kind'):
                lines.append(f"청산트리거: `{diag.get('exit_trigger_kind')}`")
            if diag.get('exit_closed_ts_human') or diag.get('exit_tf'):
                lines.append(
                    f"청산봉: tf `{diag.get('exit_tf', '?')}` | close_ts `{diag.get('exit_closed_ts_human', '?')}`"
                )
            if diag.get('exit_closed_ohlc_text'):
                lines.append(f"청산 확정봉: `{diag.get('exit_closed_ohlc_text')}`")
            elif (
                diag.get('exit_closed_open') is not None or diag.get('exit_closed_high') is not None
                or diag.get('exit_closed_low') is not None or diag.get('exit_closed_close') is not None
            ):
                lines.append(
                    f"청산봉 값: O `{self._fmt_signal_trade_value(diag.get('exit_closed_open'))}` | "
                    f"H `{self._fmt_signal_trade_value(diag.get('exit_closed_high'))}` | "
                    f"L `{self._fmt_signal_trade_value(diag.get('exit_closed_low'))}` | "
                    f"C `{self._fmt_signal_trade_value(diag.get('exit_closed_close'))}`"
                )
            if (
                diag.get('smc_bullish_ob_entry') is not None
                or diag.get('smc_bearish_ob_entry') is not None
                or diag.get('smc_ob_tags')
            ):
                smc_bull = 'Y' if diag.get('smc_bullish_ob_entry') else 'N'
                smc_bear = 'Y' if diag.get('smc_bearish_ob_entry') else 'N'
                smc_tf = diag.get('smc_tf')
                line = f"UTSMC exit OB{f'[{smc_tf}]' if smc_tf else ''}: bull `{smc_bull}` | bear `{smc_bear}`"
                if diag.get('smc_ob_tags'):
                    line += f" | tags `{diag.get('smc_ob_tags')}`"
                lines.append(line)
            if diag.get('smc_reason'):
                lines.append(f"UTSMC exit 판정: `{diag.get('smc_reason')}`")
            if diag.get('smc_reason_ts_human'):
                lines.append(f"UTSMC exit 판정시각: `{diag.get('smc_reason_ts_human')}`")
            if (
                diag.get('smc_bullish_ob_bottom') is not None
                or diag.get('smc_bullish_ob_top') is not None
                or diag.get('smc_bearish_ob_bottom') is not None
                or diag.get('smc_bearish_ob_top') is not None
            ):
                lines.append(
                    f"UTSMC exit OB range: "
                    f"bull `{self._fmt_signal_trade_value(diag.get('smc_bullish_ob_bottom'))}` ~ "
                    f"`{self._fmt_signal_trade_value(diag.get('smc_bullish_ob_top'))}` | "
                    f"bear `{self._fmt_signal_trade_value(diag.get('smc_bearish_ob_bottom'))}` ~ "
                    f"`{self._fmt_signal_trade_value(diag.get('smc_bearish_ob_top'))}`"
                )

        note = diag.get('note')
        if note:
            lines.append(f"진단메모: `{note}`")

    def _build_signal_entry_notice(
        self,
        symbol,
        side,
        qty,
        requested_price,
        actual_entry_price,
        entry_plan=None,
        leverage=None,
        target_notional=None,
        margin_to_use=None
    ):
        display_symbol = self.ctrl.format_symbol_for_display(symbol)
        diag = dict(self.last_stateful_diag.get(symbol, {}) or {})
        strategy_name = str(self.get_runtime_strategy_params().get('active_strategy', 'utbot') or 'utbot').upper()
        cfg = self.get_runtime_common_settings()

        lines = [
            f"✅ [Signal Entry] {display_symbol} `{str(side).upper()}`",
            f"전략: `{strategy_name}` | 수량 `{qty}`",
            f"주문가: `{self._fmt_signal_trade_value(requested_price)}` | 체결가: `{self._fmt_signal_trade_value(actual_entry_price)}`",
            f"TF: 진입 `{cfg.get('entry_timeframe', cfg.get('timeframe', '15m'))}` / 청산 `{self._get_exit_timeframe(symbol)}`",
            f"네트워크: `{self.ctrl.get_network_status_label()}` | 거래소 `{self.ctrl.get_exchange_display_name()}`",
        ]
        entry_reason = self.last_entry_reason.get(symbol)
        if entry_reason:
            lines.append(f"진입근거: `{entry_reason}`")
        if strategy_name == UTBOT_FILTERED_BREAKOUT_STRATEGY.upper() and isinstance(entry_plan, dict):
            try:
                lev = float(leverage or cfg.get('leverage', 1) or 1)
                notional = float(target_notional) if target_notional is not None else float(qty) * float(actual_entry_price)
                margin = float(margin_to_use) if margin_to_use is not None else notional / max(lev, 1e-9)
                risk_usdt = float(entry_plan.get('risk_usdt', 0.0) or 0.0)
                risk_distance = float(entry_plan.get('risk_distance', 0.0) or 0.0)
                rr_multiple = float(entry_plan.get('rr_multiple', 2.0) or 2.0)
                take_profit_distance = risk_distance * rr_multiple
                expected_profit = risk_usdt * rr_multiple
                risk_pct = risk_distance / max(abs(float(actual_entry_price)), 1e-9) * 100.0
                take_profit_pct = take_profit_distance / max(abs(float(actual_entry_price)), 1e-9) * 100.0
                lines.append(
                    f"진입금액: `증거금 {margin:.2f} USDT / 포지션 {notional:.2f} USDT / {lev:.0f}x`"
                )
                lines.append(
                    f"손절계획: `거리 {risk_distance:.4f} ({risk_pct:.3f}%) / 손실 {risk_usdt:.2f} USDT`"
                )
                lines.append(
                    f"익절계획: `{rr_multiple:.1f}R / 거리 {take_profit_distance:.4f} "
                    f"({take_profit_pct:.3f}%) / 예상수익 {expected_profit:.2f} USDT`"
                )
            except Exception as e:
                logger.debug(f"UT breakout entry notice sizing detail failed: {e}")
        self._append_signal_diag_lines(lines, diag, include_exit=False)
        return "\n".join(lines)

    def _build_signal_exit_notice(self, symbol, pos, reason, pnl, pnl_pct, exit_price):
        display_symbol = self.ctrl.format_symbol_for_display(symbol)
        diag = dict(self.last_stateful_diag.get(symbol, {}) or {})
        strategy_name = str(self.get_runtime_strategy_params().get('active_strategy', 'utbot') or 'utbot').upper()
        side = str(pos.get('side', 'unknown')).upper()
        entry_price = pos.get('entryPrice')

        lines = [
            f"📊 [Signal Exit] {display_symbol} `{side}`",
            f"전략: `{strategy_name}` | 호출사유 `{reason}`",
            f"PnL: `{self._fmt_signal_trade_value(pnl, signed=True)}` (`{float(pnl_pct):+.2f}%`)",
            f"진입가: `{self._fmt_signal_trade_value(entry_price)}` | 청산가: `{self._fmt_signal_trade_value(exit_price)}`",
        ]
        self._append_signal_diag_lines(lines, diag, include_exit=True)
        return "\n".join(lines)

    async def _handle_utbot_primary_strategy(
        self,
        symbol,
        k,
        pos,
        strategy_name,
        raw_strategy_sig,
        raw_state_sig,
        raw_ut_detail,
        sig,
        filter_pack_entry=None,
        rsi_momentum_entry_eval=None,
        rsi_momentum_exit_eval=None
    ):
        target_sig = raw_state_sig
        entry_sig = sig
        current_ts = int(k.get('t') or 0)
        tf = self.get_runtime_common_settings().get('entry_timeframe', self.get_runtime_common_settings().get('timeframe', '15m'))
        candle_ms = self._timeframe_to_ms(tf)
        timing_mode = self._get_ut_entry_timing_mode()
        timing_label = self._get_ut_entry_timing_label(timing_mode)
        pending = self._get_ut_pending_entry(symbol)
        signal_basis_ts = int(raw_ut_detail.get('signal_ts') or 0)
        signal_basis_side = str(raw_ut_detail.get('signal_side') or '').lower()

        def _fmt_pending_ts(ts):
            ts = int(ts or 0)
            return datetime.fromtimestamp(ts / 1000).strftime('%m-%d %H:%M') if ts else None

        pending_source = self._get_ut_pending_source(pending) if pending else None
        is_flip_pending = self._is_ut_flip_pending(pending) if pending else False

        def _entry_filter_gate(side, *, persistent=False):
            allowed, filter_reason = self._utbot_filter_pack_allows_entry(filter_pack_entry, side)
            if not allowed:
                suffix = " | persistent=Y" if persistent else ""
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} {side.upper()} 진입 필터 차단 ({filter_reason})"
                )
                self._update_stateful_diag(
                    symbol,
                    stage='entry_blocked',
                    strategy=strategy_name,
                    raw_state=(target_sig or 'none'),
                    raw_signal=(raw_strategy_sig or 'none'),
                    entry_sig=(side or 'none'),
                    pos_side='NONE',
                    note=f"utbot filter pack blocked | reason={filter_reason}{suffix}"
                )
                return False, filter_reason

            allowed, filter_reason = self._utbot_rsi_momentum_filter_allows(rsi_momentum_entry_eval, side)
            if allowed:
                return True, filter_reason
            suffix = " | persistent=Y" if persistent else ""
            self.last_entry_reason[symbol] = (
                f"{strategy_name} {side.upper()} 진입 필터 차단 ({filter_reason})"
            )
            self._update_stateful_diag(
                symbol,
                stage='entry_blocked',
                strategy=strategy_name,
                raw_state=(target_sig or 'none'),
                raw_signal=(raw_strategy_sig or 'none'),
                entry_sig=(side or 'none'),
                pos_side='NONE',
                note=f"rsi momentum filter blocked | reason={filter_reason}{suffix}"
            )
            return False, filter_reason

        if self.is_upbit_mode():
            if pos and target_sig == 'short':
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} SELL 상태 감지, 청산은 exit TF `{self._get_exit_timeframe(symbol)}` 확인 후 실행"
                )
            elif not pos and entry_sig == 'long':
                allowed, _ = _entry_filter_gate('long')
                if not allowed:
                    return
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
            return

        if pending and timing_mode == 'next_candle':
            pending_execute_ts = int(pending.get('execute_ts') or 0)
            pending_expires_ts = int(pending.get('expires_ts') or 0)
            if is_flip_pending:
                if (not pos) and pending_expires_ts > 0 and current_ts > pending_expires_ts:
                    self._clear_ut_pending_entry(symbol)
                    pending = None
                    pending_source = None
                    is_flip_pending = False
            elif current_ts > pending_execute_ts:
                self._clear_ut_pending_entry(symbol)
                pending = None
                pending_source = None
                is_flip_pending = False

        if pending and target_sig not in {'long', 'short'}:
            self._clear_ut_pending_entry(symbol)
            pending = None
            pending_source = None
            is_flip_pending = False
        elif pending and target_sig in {'long', 'short'} and target_sig != pending.get('side'):
            self._clear_ut_pending_entry(symbol)
            pending = None
            pending_source = None
            is_flip_pending = False

        raw_flip_detected = pos and target_sig and (
            (pos['side'] == 'long' and target_sig == 'short') or
            (pos['side'] == 'short' and target_sig == 'long')
        )

        exit_tf = self._get_exit_timeframe(symbol)
        flip_on_entry_tf = self._timeframes_equivalent(exit_tf, tf)
        flip_filter_confirmed = True
        flip_filter_reason = 'RSI Momentum Trend filter off'
        if raw_flip_detected and flip_on_entry_tf:
            flip_filter_confirmed, flip_filter_reason = self._utbot_rsi_momentum_filter_allows(
                rsi_momentum_exit_eval,
                str(pos['side']).lower()
            )

        if pos and raw_flip_detected and not flip_on_entry_tf:
            self._clear_ut_pending_entry(symbol)
            flip_label = "반전 신호" if raw_strategy_sig == target_sig else "상태 동기화"
            self.last_entry_reason[symbol] = (
                f"{strategy_name} {flip_label} 감지, 청산은 exit TF `{exit_tf}` 확인 후 실행"
            )
            self._update_stateful_diag(
                symbol,
                stage='exit_wait',
                strategy=strategy_name,
                raw_state=(target_sig or 'none'),
                raw_signal=(raw_strategy_sig or 'none'),
                entry_sig=(entry_sig or 'none'),
                pos_side=str(pos['side']).upper(),
                note=f"{flip_label} detected on entry TF | exit_tf={exit_tf}"
            )
            return
        if pos and raw_flip_detected and flip_on_entry_tf and not flip_filter_confirmed:
            self._clear_ut_pending_entry(symbol)
            flip_label = "반전 신호" if raw_strategy_sig == target_sig else "상태 동기화"
            self.last_entry_reason[symbol] = (
                f"{strategy_name} {flip_label} 감지, RSI Momentum Trend 반전 미확인으로 "
                f"{pos['side'].upper()} 유지 ({flip_filter_reason})"
            )
            self._update_stateful_diag(
                symbol,
                stage='exit_wait',
                strategy=strategy_name,
                raw_state=(target_sig or 'none'),
                raw_signal=(raw_strategy_sig or 'none'),
                entry_sig=(entry_sig or 'none'),
                pos_side=str(pos['side']).upper(),
                note=f"{flip_label} blocked by rsi momentum filter | reason={flip_filter_reason}"
            )
            return
        if pos and raw_flip_detected and flip_on_entry_tf and flip_filter_confirmed:
            flip_label = "반전 신호" if raw_strategy_sig == target_sig else "상태 동기화"
            reentry_allowed = False
            reentry_filter_reason = None
            can_reenter = entry_sig == target_sig
            execute_ts = current_ts + candle_ms if candle_ms > 0 else current_ts
            expires_ts = execute_ts + (candle_ms * 2 if candle_ms > 0 else 0)
            retry_count = int(pending.get('retry_count') or 0) if is_flip_pending and pending.get('side') == target_sig else 0

            if can_reenter:
                reentry_allowed, reentry_filter_reason = _entry_filter_gate(entry_sig)
                if reentry_allowed:
                    self._set_ut_pending_entry(
                        symbol,
                        entry_sig,
                        signal_basis_ts or current_ts,
                        execute_ts,
                        strategy_name=strategy_name,
                        source='flip',
                        expires_ts=expires_ts,
                        signal_side=signal_basis_side or entry_sig,
                        retry_count=retry_count,
                        pending_state='exit_pending'
                    )
                    pending = self._get_ut_pending_entry(symbol)
                    pending_source = self._get_ut_pending_source(pending)
                    is_flip_pending = self._is_ut_flip_pending(pending)
                elif pending and is_flip_pending and pending.get('side') == entry_sig:
                    self._clear_ut_pending_entry(symbol)
                    pending = None
                    pending_source = None
                    is_flip_pending = False
            elif pending and is_flip_pending and pending.get('side') == target_sig:
                self._clear_ut_pending_entry(symbol)
                pending = None
                pending_source = None
                is_flip_pending = False

            self._update_stateful_diag(
                symbol,
                stage='flip_detected',
                strategy=strategy_name,
                raw_state=(target_sig or 'none'),
                raw_signal=(raw_strategy_sig or 'none'),
                entry_sig=(entry_sig or 'none'),
                pos_side=str(pos['side']).upper(),
                ut_pending_source=pending_source,
                ut_pending_side=(target_sig.upper() if can_reenter and reentry_allowed else None),
                ut_pending_state=('exit_pending' if can_reenter and reentry_allowed else None),
                ut_pending_execute_ts=execute_ts if can_reenter and reentry_allowed else None,
                ut_pending_execute_ts_human=_fmt_pending_ts(execute_ts) if can_reenter and reentry_allowed else None,
                ut_pending_expires_ts=expires_ts if can_reenter and reentry_allowed else None,
                ut_pending_expires_ts_human=_fmt_pending_ts(expires_ts) if can_reenter and reentry_allowed else None,
                ut_flip_target_side=target_sig.upper(),
                ut_flip_signal_ts=(signal_basis_ts or current_ts),
                ut_flip_signal_ts_human=_fmt_pending_ts(signal_basis_ts or current_ts),
                ut_flip_execute_ts=execute_ts if can_reenter and reentry_allowed else None,
                ut_flip_execute_ts_human=_fmt_pending_ts(execute_ts) if can_reenter and reentry_allowed else None,
                ut_flip_expires_ts=expires_ts if can_reenter and reentry_allowed else None,
                ut_flip_expires_ts_human=_fmt_pending_ts(expires_ts) if can_reenter and reentry_allowed else None,
                ut_flip_retry_count=retry_count,
                ut_flip_block_reason=(
                    reentry_filter_reason if can_reenter and not reentry_allowed else
                    ('direction_or_timing' if not can_reenter else None)
                ),
                note=(
                    f"{flip_label} | flip pending armed"
                    if can_reenter and reentry_allowed else
                    (
                        f"{flip_label} | re-entry blocked ({reentry_filter_reason})"
                        if can_reenter else
                        f"{flip_label} | waiting state/timing alignment"
                    )
                )
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
                    ut_pending_source=pending_source,
                    ut_pending_side=(target_sig.upper() if can_reenter and reentry_allowed else None),
                    ut_pending_state=('armed' if can_reenter and reentry_allowed else None),
                    ut_pending_execute_ts=execute_ts if can_reenter and reentry_allowed else None,
                    ut_pending_execute_ts_human=_fmt_pending_ts(execute_ts) if can_reenter and reentry_allowed else None,
                    ut_pending_expires_ts=expires_ts if can_reenter and reentry_allowed else None,
                    ut_pending_expires_ts_human=_fmt_pending_ts(expires_ts) if can_reenter and reentry_allowed else None,
                    ut_flip_target_side=target_sig.upper(),
                    ut_flip_signal_ts=(signal_basis_ts or current_ts),
                    ut_flip_signal_ts_human=_fmt_pending_ts(signal_basis_ts or current_ts),
                    ut_flip_execute_ts=execute_ts if can_reenter and reentry_allowed else None,
                    ut_flip_execute_ts_human=_fmt_pending_ts(execute_ts) if can_reenter and reentry_allowed else None,
                    ut_flip_expires_ts=expires_ts if can_reenter and reentry_allowed else None,
                    ut_flip_expires_ts_human=_fmt_pending_ts(expires_ts) if can_reenter and reentry_allowed else None,
                    ut_flip_retry_count=retry_count,
                    ut_flip_block_reason=(
                        reentry_filter_reason if can_reenter and not reentry_allowed else
                        ('direction_or_timing' if not can_reenter else None)
                    ),
                    note=f"{flip_label} exit complete"
                )
                if can_reenter and reentry_allowed:
                    self._touch_ut_pending_entry(symbol, pending_state='armed', retry_count=retry_count, block_reason=None)
                    self.last_entry_reason[symbol] = (
                        f"{strategy_name} {flip_label} -> {entry_sig.upper()} {timing_label} 대기"
                    )
                    self._update_stateful_diag(
                        symbol,
                        stage='flip_reentry_armed',
                        entry_sig=entry_sig,
                        pos_side='NONE',
                        ut_pending_source='flip',
                        ut_pending_side=entry_sig.upper(),
                        ut_pending_state='armed',
                        ut_pending_execute_ts=execute_ts,
                        ut_pending_execute_ts_human=_fmt_pending_ts(execute_ts),
                        ut_pending_expires_ts=expires_ts,
                        ut_pending_expires_ts_human=_fmt_pending_ts(expires_ts),
                        ut_flip_target_side=entry_sig.upper(),
                        ut_flip_signal_ts=(signal_basis_ts or current_ts),
                        ut_flip_signal_ts_human=_fmt_pending_ts(signal_basis_ts or current_ts),
                        ut_flip_execute_ts=execute_ts,
                        ut_flip_execute_ts_human=_fmt_pending_ts(execute_ts),
                        ut_flip_expires_ts=expires_ts,
                        ut_flip_expires_ts_human=_fmt_pending_ts(expires_ts),
                        ut_flip_retry_count=retry_count,
                        ut_flip_block_reason=None,
                        note=f"re-entry armed | execute_ts={execute_ts} | expires_ts={expires_ts} | mode={timing_mode}"
                    )
                else:
                    self._clear_ut_pending_entry(symbol)
                    self.last_entry_reason[symbol] = (
                        f"{strategy_name} {flip_label} 청산 완료, 재진입은 "
                        f"{'필터 차단' if can_reenter else '방향/타이밍 미정렬'}"
                    )
                    self._update_stateful_diag(
                        symbol,
                        stage='entry_blocked' if can_reenter else 'flip_exit_only',
                        pos_side='NONE',
                        ut_pending_source=None,
                        ut_pending_side=None,
                        ut_pending_state=None,
                        ut_pending_execute_ts=None,
                        ut_pending_execute_ts_human=None,
                        ut_pending_expires_ts=None,
                        ut_pending_expires_ts_human=None,
                        ut_flip_target_side=target_sig.upper(),
                        ut_flip_signal_ts=(signal_basis_ts or current_ts),
                        ut_flip_signal_ts_human=_fmt_pending_ts(signal_basis_ts or current_ts),
                        ut_flip_execute_ts=None,
                        ut_flip_execute_ts_human=None,
                        ut_flip_expires_ts=None,
                        ut_flip_expires_ts_human=None,
                        ut_flip_retry_count=retry_count,
                        ut_flip_block_reason=(
                            reentry_filter_reason if can_reenter and not reentry_allowed else 'direction_or_timing'
                        ),
                        note=(
                            f"flip re-entry blocked | reason={reentry_filter_reason}"
                            if can_reenter else
                            're-entry blocked by direction or timing'
                        )
                    )
                    await self._notify_stateful_diag(symbol)
            else:
                if can_reenter and reentry_allowed:
                    retry_count += 1
                    self._touch_ut_pending_entry(
                        symbol,
                        retry_count=retry_count,
                        pending_state='waiting_flat',
                        block_reason=None
                    )
                self.last_entry_reason[symbol] = f"{strategy_name} {flip_label} 청산 미확인, 상태 재동기화 재시도 대기"
                self.last_stateful_retry_ts[symbol] = 0.0
                self._update_stateful_diag(
                    symbol,
                    stage='flip_still_open',
                    pos_side=str(check_pos['side']).upper(),
                    ut_pending_source=('flip' if can_reenter and reentry_allowed else None),
                    ut_pending_side=(target_sig.upper() if can_reenter and reentry_allowed else None),
                    ut_pending_state=('waiting_flat' if can_reenter and reentry_allowed else None),
                    ut_pending_execute_ts=execute_ts if can_reenter and reentry_allowed else None,
                    ut_pending_execute_ts_human=_fmt_pending_ts(execute_ts) if can_reenter and reentry_allowed else None,
                    ut_pending_expires_ts=expires_ts if can_reenter and reentry_allowed else None,
                    ut_pending_expires_ts_human=_fmt_pending_ts(expires_ts) if can_reenter and reentry_allowed else None,
                    ut_flip_target_side=target_sig.upper(),
                    ut_flip_signal_ts=(signal_basis_ts or current_ts),
                    ut_flip_signal_ts_human=_fmt_pending_ts(signal_basis_ts or current_ts),
                    ut_flip_execute_ts=execute_ts if can_reenter and reentry_allowed else None,
                    ut_flip_execute_ts_human=_fmt_pending_ts(execute_ts) if can_reenter and reentry_allowed else None,
                    ut_flip_expires_ts=expires_ts if can_reenter and reentry_allowed else None,
                    ut_flip_expires_ts_human=_fmt_pending_ts(expires_ts) if can_reenter and reentry_allowed else None,
                    ut_flip_retry_count=retry_count,
                    ut_flip_block_reason=(
                        reentry_filter_reason if can_reenter and not reentry_allowed else
                        ('direction_or_timing' if not can_reenter else None)
                    ),
                    note=(
                        f"position still open after exit attempt | retry_count={retry_count}"
                        if can_reenter and reentry_allowed else
                        'position still open after exit attempt'
                    )
                )
            return
        elif not pos and entry_sig:
            allowed, filter_reason = _entry_filter_gate(entry_sig)
            if not allowed:
                return
            execute_ts = current_ts + candle_ms if candle_ms > 0 else current_ts
            self._set_ut_pending_entry(
                symbol,
                entry_sig,
                signal_basis_ts or current_ts,
                execute_ts,
                strategy_name=strategy_name
            )
            self.last_entry_reason[symbol] = (
                f"{strategy_name} {entry_sig.upper()} 신호 확정, {timing_label} 대기"
            )
            self._update_stateful_diag(
                symbol,
                stage='entry_armed',
                strategy=strategy_name,
                raw_state=(target_sig or 'none'),
                raw_signal=(raw_strategy_sig or 'none'),
                entry_sig=entry_sig,
                pos_side='NONE',
                note=f"pending {timing_mode} entry | execute_ts={execute_ts}"
            )
        elif not pos and timing_mode == 'persistent' and target_sig in {'long', 'short'}:
            if not self.is_trade_direction_allowed(target_sig):
                self._clear_ut_pending_entry(symbol)
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} 현재 {target_sig.upper()} 상태지만 "
                    f"{self.format_trade_direction_block_reason(target_sig)}"
                )
                self._update_stateful_diag(
                    symbol,
                    stage='entry_blocked',
                    strategy=strategy_name,
                    raw_state=(target_sig or 'none'),
                    raw_signal=(raw_strategy_sig or 'none'),
                    entry_sig=target_sig,
                    pos_side='NONE',
                    note="persistent entry blocked by direction filter"
                )
                return
            if signal_basis_side == target_sig and signal_basis_ts > 0:
                allowed, filter_reason = _entry_filter_gate(target_sig, persistent=True)
                if not allowed:
                    self.last_entry_reason[symbol] = (
                        f"{strategy_name} 현재 {target_sig.upper()} 상태지만 진입 필터 차단 ({filter_reason})"
                    )
                    return
                execute_ts = current_ts + candle_ms if candle_ms > 0 else current_ts
                self._set_ut_pending_entry(
                    symbol,
                    target_sig,
                    signal_basis_ts,
                    execute_ts,
                    strategy_name=strategy_name
                )
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} 현재 유지 중인 {target_sig.upper()} 상태 확인, 마지막 UT 시그널 확정봉 기준 {timing_label} 즉시 대기"
                )
                self._update_stateful_diag(
                    symbol,
                    stage='entry_armed',
                    strategy=strategy_name,
                    raw_state=(target_sig or 'none'),
                    raw_signal=(raw_strategy_sig or 'none'),
                    entry_sig=target_sig,
                    pos_side='NONE',
                    note=(
                        f"persistent state entry | execute_ts={execute_ts} | "
                        f"signal_ts={signal_basis_ts} | source=last_signal_candle"
                    )
                )
            else:
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} 현재 상태는 {target_sig.upper()}지만 기준이 될 마지막 UT 시그널 확정봉 대기"
                )
        elif not pos and target_sig:
            if pending and timing_mode == 'persistent' and pending.get('side') == target_sig:
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} 예약된 {target_sig.upper()} {timing_label} 대기"
                )
            else:
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} 현재 상태는 {target_sig.upper()}지만 fresh signal 대기"
                )
        elif pos:
            if pending and self._is_ut_flip_pending(pending) and pending.get('side') in {'long', 'short'} and pending.get('side') != str(pos['side']).lower():
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} flip {str(pending.get('side')).upper()} 대기 "
                    f"(현재 {pos['side'].upper()} 청산 확인 중)"
                )
            else:
                self._clear_ut_pending_entry(symbol)
                self.last_entry_reason[symbol] = (
                    f"포지션 보유 중 ({pos['side'].upper()}), {strategy_name} 청산은 exit TF `{exit_tf}` 기준"
                )

    async def _handle_utsmc_primary_strategy(self, symbol, k, pos, strategy_name, raw_strategy_sig, raw_state_sig, raw_smc_detail, sig):
        target_sig = raw_state_sig
        ut_signal = raw_strategy_sig if raw_strategy_sig in {'long', 'short'} else None
        current_ts = int(k.get('t') or 0)
        tf = self.get_runtime_common_settings().get('entry_timeframe', self.get_runtime_common_settings().get('timeframe', '15m'))
        candle_ms = self._timeframe_to_ms(tf)
        timing_mode = self._get_ut_entry_timing_mode()
        timing_label = self._get_ut_entry_timing_label(timing_mode)
        pending = self._get_utsmc_pending_entry(symbol)
        ut_detail = raw_smc_detail.get('ut_detail') or {}
        ut_signal_high = ut_detail.get('signal_high')
        ut_signal_low = ut_detail.get('signal_low')
        ut_signal_ts = int(ut_detail.get('signal_ts') or 0)
        ut_signal_side = str(ut_detail.get('signal_side') or '').lower()
        smc_reason = raw_smc_detail.get('smc_reason') or raw_smc_detail.get('smc_internal_ob_reason')
        ob_tags = raw_smc_detail.get('ob_tags') or []
        ob_tag_text = ", ".join(ob_tags) if ob_tags else "-"
        bullish_ob_entry = bool(raw_smc_detail.get('bullish_internal_ob_entry'))
        bearish_ob_entry = bool(raw_smc_detail.get('bearish_internal_ob_entry'))

        if pending and timing_mode == 'next_candle' and current_ts > int(pending.get('execute_ts') or 0):
            self._clear_utsmc_pending_entry(symbol)
            pending = None

        if pos:
            self._clear_utsmc_pending_entry(symbol)
            self.utsmc_last_entry_signal_ts[symbol] = int(self.utsmc_last_entry_signal_ts.get(symbol, 0) or 0)
            if pos['side'] == 'long':
                hold_note = "SMC internal bearish OB 진입 마감 청산 대기"
                if bearish_ob_entry:
                    hold_note = f"SMC internal bearish OB 감지({ob_tag_text}), exit TF 청산 대기"
            else:
                hold_note = "SMC internal bullish OB 진입 마감 청산 대기"
                if bullish_ob_entry:
                    hold_note = f"SMC internal bullish OB 감지({ob_tag_text}), exit TF 청산 대기"
            self.last_entry_reason[symbol] = f"포지션 보유 중 ({pos['side'].upper()}), {hold_note}"
            return

        if pending and target_sig not in {'long', 'short'}:
            self._clear_utsmc_pending_entry(symbol)
            self.last_entry_reason[symbol] = f"{strategy_name} UT 상태 해제, fresh UT 신호 재대기"
            return

        if pending and target_sig in {'long', 'short'} and target_sig != pending.get('side'):
            self._clear_utsmc_pending_entry(symbol)
            pending = None

        if pending and ut_signal and ut_signal != pending.get('side'):
            if not self.is_trade_direction_allowed(ut_signal):
                self._clear_utsmc_pending_entry(symbol)
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} UT {ut_signal.upper()} "
                    f"{self.format_trade_direction_block_reason(ut_signal)}"
                )
                self._update_stateful_diag(
                    symbol,
                    stage='entry_blocked',
                    strategy=strategy_name,
                    raw_state=(target_sig or 'none'),
                    raw_signal=(ut_signal or 'none'),
                    entry_sig=(ut_signal or 'none'),
                    pos_side='NONE',
                    note="candidate replacement blocked by direction filter"
                )
                return
            allowed, candidate_reason = self._utsmc_candidate_filter_allows(raw_smc_detail, ut_signal, is_persistent=False)
            if not allowed:
                self._clear_utsmc_pending_entry(symbol)
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} UT {ut_signal.upper()} 후보 필터 차단 "
                    f"({raw_smc_detail.get('candidate_filter_mode_label', 'OFF')} | {candidate_reason})"
                )
                self._update_stateful_diag(
                    symbol,
                    stage='entry_blocked',
                    strategy=strategy_name,
                    raw_state=(target_sig or 'none'),
                    raw_signal=(ut_signal or 'none'),
                    entry_sig=(ut_signal or 'none'),
                    pos_side='NONE',
                    note=(
                        f"candidate filter blocked | mode={raw_smc_detail.get('candidate_filter_mode_label', 'OFF')} | "
                        f"reason={candidate_reason}"
                    )
                )
                return
            execute_ts = current_ts + candle_ms if candle_ms > 0 else current_ts
            self._set_utsmc_pending_entry(
                symbol,
                ut_signal,
                ut_signal_ts or raw_smc_detail.get('ut_signal_ts'),
                execute_ts,
                signal_high=ut_signal_high,
                signal_low=ut_signal_low
            )
            self.last_entry_reason[symbol] = (
                f"{strategy_name} 기존 {pending.get('side', '').upper()} 예약 취소, "
                f"새 UT {ut_signal.upper()} 신호로 {timing_label} 대기"
            )
            self._update_stateful_diag(
                symbol,
                stage='entry_armed',
                strategy=strategy_name,
                raw_state=(target_sig or 'none'),
                raw_signal=(ut_signal or 'none'),
                entry_sig=(ut_signal or 'none'),
                pos_side='NONE',
                note=f"pending replaced | execute_ts={execute_ts} | mode={timing_mode} | ob={smc_reason or '-'}"
            )
            return

        if ut_signal in {'long', 'short'}:
            if not self.is_trade_direction_allowed(ut_signal):
                self._clear_utsmc_pending_entry(symbol)
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} UT {ut_signal.upper()} "
                    f"{self.format_trade_direction_block_reason(ut_signal)}"
                )
                self._update_stateful_diag(
                    symbol,
                    stage='entry_blocked',
                    strategy=strategy_name,
                    raw_state=(target_sig or 'none'),
                    raw_signal=(ut_signal or 'none'),
                    entry_sig=(ut_signal or 'none'),
                    pos_side='NONE',
                    note="fresh entry blocked by direction filter"
                )
                return
            allowed, candidate_reason = self._utsmc_candidate_filter_allows(raw_smc_detail, ut_signal, is_persistent=False)
            if not allowed:
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} UT {ut_signal.upper()} 후보 필터 차단 "
                    f"({raw_smc_detail.get('candidate_filter_mode_label', 'OFF')} | {candidate_reason})"
                )
                self._update_stateful_diag(
                    symbol,
                    stage='entry_blocked',
                    strategy=strategy_name,
                    raw_state=(target_sig or 'none'),
                    raw_signal=(ut_signal or 'none'),
                    entry_sig=(ut_signal or 'none'),
                    pos_side='NONE',
                    note=(
                        f"candidate filter blocked | mode={raw_smc_detail.get('candidate_filter_mode_label', 'OFF')} | "
                        f"reason={candidate_reason}"
                    )
                )
                return
            execute_ts = current_ts + candle_ms if candle_ms > 0 else current_ts
            self._set_utsmc_pending_entry(
                symbol,
                ut_signal,
                ut_signal_ts or raw_smc_detail.get('ut_signal_ts'),
                execute_ts,
                signal_high=ut_signal_high,
                signal_low=ut_signal_low
            )
            self.last_entry_reason[symbol] = (
                f"{strategy_name} UT {ut_signal.upper()} 확정, {timing_label} 대기"
            )
            self._update_stateful_diag(
                symbol,
                stage='entry_armed',
                strategy=strategy_name,
                raw_state=(target_sig or 'none'),
                raw_signal=(ut_signal or 'none'),
                entry_sig=(ut_signal or 'none'),
                pos_side='NONE',
                note=f"pending {timing_mode} entry | execute_ts={execute_ts} | ob={smc_reason or '-'}"
            )
            return

        if timing_mode == 'persistent' and target_sig in {'long', 'short'}:
            if not self.is_trade_direction_allowed(target_sig):
                self._clear_utsmc_pending_entry(symbol)
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} 현재 UT {target_sig.upper()} 상태지만 "
                    f"{self.format_trade_direction_block_reason(target_sig)}"
                )
                self._update_stateful_diag(
                    symbol,
                    stage='entry_blocked',
                    strategy=strategy_name,
                    raw_state=(target_sig or 'none'),
                    raw_signal=(ut_signal or 'none'),
                    entry_sig=target_sig,
                    pos_side='NONE',
                    note="persistent entry blocked by direction filter"
                )
                return
            if ut_signal_side == target_sig and ut_signal_ts > 0:
                allowed, candidate_reason = self._utsmc_candidate_filter_allows(raw_smc_detail, target_sig, is_persistent=True)
                if not allowed:
                    self.last_entry_reason[symbol] = (
                        f"{strategy_name} 현재 UT {target_sig.upper()} 상태지만 후보 필터 차단 "
                        f"({raw_smc_detail.get('candidate_filter_mode_label', 'OFF')} | {candidate_reason})"
                    )
                    self._update_stateful_diag(
                        symbol,
                        stage='entry_blocked',
                        strategy=strategy_name,
                        raw_state=(target_sig or 'none'),
                        raw_signal=(ut_signal or 'none'),
                        entry_sig=target_sig,
                        pos_side='NONE',
                        note=(
                            f"candidate filter blocked | mode={raw_smc_detail.get('candidate_filter_mode_label', 'OFF')} | "
                            f"reason={candidate_reason} | persistent=Y"
                        )
                    )
                    return
                execute_ts = current_ts + candle_ms if candle_ms > 0 else current_ts
                self._set_utsmc_pending_entry(
                    symbol,
                    target_sig,
                    ut_signal_ts,
                    execute_ts,
                    signal_high=ut_signal_high,
                    signal_low=ut_signal_low
                )
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} 현재 유지 중인 UT {target_sig.upper()} 상태 확인, 마지막 UT 시그널 확정봉 기준 {timing_label} 즉시 대기"
                )
                self._update_stateful_diag(
                    symbol,
                    stage='entry_armed',
                    strategy=strategy_name,
                    raw_state=(target_sig or 'none'),
                    raw_signal=(ut_signal or 'none'),
                    entry_sig=target_sig,
                    pos_side='NONE',
                    note=(
                        f"persistent state entry | execute_ts={execute_ts} | signal_ts={ut_signal_ts} | "
                        f"signal_high={ut_signal_high} | signal_low={ut_signal_low} | "
                        f"source=last_signal_candle | ob={smc_reason or '-'}"
                    )
                )
            else:
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} 현재 UT 상태는 {target_sig.upper()}지만 기준이 될 마지막 UT 시그널 확정봉 대기"
                )
            return

        if pending:
            if timing_mode == 'persistent' and target_sig == pending.get('side'):
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} 예약된 {str(pending.get('side', '')).upper()} {timing_label} 대기 "
                    f"(armed ts={int(pending.get('execute_ts') or 0)}, UT 상태 유지 중)"
                )
            else:
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} 예약된 {str(pending.get('side', '')).upper()} {timing_label} 대기"
                )
            return

        if target_sig in {'long', 'short'}:
            self.last_entry_reason[symbol] = (
                f"{strategy_name} 현재 UT 상태는 {target_sig.upper()}지만 fresh UT 신호 재대기"
            )

    async def _handle_ut_hybrid_primary_strategy(self, symbol, k, pos, strategy_name, active_strategy, raw_strategy_sig, raw_state_sig, raw_hybrid_detail, sig):
        timing_label = raw_hybrid_detail.get('timing_label', UT_HYBRID_TIMING_LABELS.get(active_strategy, 'signal'))
        regime_sig = raw_state_sig
        entry_sig = sig
        fresh_entry_sig = self._remember_ut_strategy_signal_state(symbol, active_strategy, entry_sig)
        current_ts = int(k.get('t') or 0)
        tf = self.get_runtime_common_settings().get('entry_timeframe', self.get_runtime_common_settings().get('timeframe', '15m'))
        candle_ms = self._timeframe_to_ms(tf)
        timing_mode = self._get_ut_entry_timing_mode()
        entry_mode_label = self._get_ut_entry_timing_label(timing_mode)
        pending = self._get_ut_pending_entry(symbol)

        if pending and timing_mode == 'next_candle' and current_ts > int(pending.get('execute_ts') or 0):
            self._clear_ut_pending_entry(symbol)
            pending = None

        if pending and (entry_sig not in {'long', 'short'} or entry_sig != pending.get('side')):
            self._clear_ut_pending_entry(symbol)
            pending = None

        need_flip = pos and regime_sig and (
            (pos['side'] == 'long' and regime_sig == 'short') or
            (pos['side'] == 'short' and regime_sig == 'long')
        )

        if pos:
            self._clear_ut_pending_entry(symbol)
            exit_tf = self._get_exit_timeframe(symbol)
            if need_flip:
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} UT {regime_sig.upper()} 상태 감지, 청산은 exit TF `{exit_tf}` 확인 후 실행"
                )
                self._update_stateful_diag(
                    symbol,
                    stage='exit_wait',
                    strategy=strategy_name,
                    raw_state=(regime_sig or 'none'),
                    raw_signal=(raw_strategy_sig or 'none'),
                    entry_sig=(entry_sig or 'none'),
                    pos_side=str(pos['side']).upper(),
                    note=f"UT regime flip detected on entry TF | exit_tf={exit_tf}"
                )
            else:
                self.last_entry_reason[symbol] = (
                    f"포지션 보유 중 ({pos['side'].upper()}), {strategy_name} 청산은 exit TF `{exit_tf}` 기준"
                )
            return
        elif not pos and fresh_entry_sig:
            execute_ts = current_ts + candle_ms if candle_ms > 0 else current_ts
            self._set_ut_pending_entry(
                symbol,
                fresh_entry_sig,
                current_ts,
                execute_ts,
                strategy_name=strategy_name,
                hybrid_strategy=active_strategy
            )
            self.last_entry_reason[symbol] = (
                f"{strategy_name} 조건 충족 -> {fresh_entry_sig.upper()} {entry_mode_label} 대기"
            )
            self._update_stateful_diag(
                symbol,
                stage='entry_armed',
                strategy=strategy_name,
                raw_state=(regime_sig or 'none'),
                raw_signal=(raw_strategy_sig or 'none'),
                entry_sig=fresh_entry_sig,
                pos_side='NONE',
                note=f"pending {timing_mode} entry | execute_ts={execute_ts} | timing={timing_label}"
            )
        elif not pos and timing_mode == 'persistent' and entry_sig in {'long', 'short'}:
            execute_ts = current_ts + candle_ms if candle_ms > 0 else current_ts
            self._set_ut_pending_entry(
                symbol,
                entry_sig,
                current_ts,
                execute_ts,
                strategy_name=strategy_name,
                hybrid_strategy=active_strategy
            )
            self.last_entry_reason[symbol] = (
                f"{strategy_name} 현재 유지 중인 {entry_sig.upper()} 조건 확인 -> {entry_mode_label} 즉시 대기"
            )
            self._update_stateful_diag(
                symbol,
                stage='entry_armed',
                strategy=strategy_name,
                raw_state=(regime_sig or 'none'),
                raw_signal=(raw_strategy_sig or 'none'),
                entry_sig=entry_sig,
                pos_side='NONE',
                note=f"persistent state entry | execute_ts={execute_ts} | timing={timing_label}"
            )
        elif not pos and pending and timing_mode == 'persistent' and entry_sig == pending.get('side'):
            self.last_entry_reason[symbol] = (
                f"{strategy_name} 예약된 {entry_sig.upper()} {entry_mode_label} 대기"
            )
        elif not pos and regime_sig:
            self.last_entry_reason[symbol] = (
                f"{strategy_name} UT {regime_sig.upper()} 상태, {timing_label} 타이밍 대기"
            )

    async def _handle_rsibb_primary_strategy(self, symbol, k, pos, strategy_name, raw_strategy_sig, sig):
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

    def _build_utbb_long_exit_note(self, *, ut_signal=None, ut_state=None, bb_reentry_down=False):
        if ut_signal == 'short':
            return "UT short signal"
        if ut_state == 'short':
            return "UT short state"
        if bb_reentry_down:
            return "BB upper reentry down"
        return "BB middle above two bearish candles"

    def _build_utbb_short_exit_note(self, *, ut_signal=None, ut_state=None, special_short_mode=None):
        if ut_signal == 'long':
            return 'short exit by UT long signal'
        if ut_state == 'long':
            return 'short exit by UT long state'
        if special_short_mode == 'mid_rebound_fail':
            return 'short exit by BB middle reentry up'
        if special_short_mode == 'lower_reentry':
            return 'short exit by BB lower reentry up'
        return 'short exit by BB lower breakout'

    def _build_utbb_hold_note(self, strategy_name, pos_side, *, special_long_active=False, special_short_active=False, special_short_mode=None):
        if pos_side == 'long' and special_long_active:
            return "특수 LONG 하향돌파 청산 대기"
        if pos_side == 'long':
            return "LONG 청산 조건 대기 (UT 숏상태 또는 BB 상단 하향 돌파 또는 중간선 위 음봉 2개)"
        if pos_side == 'short' and special_short_mode == 'mid_rebound_fail':
            return "특수 SHORT 중간선 상향돌파 청산 대기"
        if pos_side == 'short' and special_short_active:
            return "특수 SHORT 하단 상향돌파 청산 대기"
        if pos_side == 'short':
            return f"{strategy_name} 청산 조건 대기 (UT 롱 또는 BB 하단 돌파)"
        return f"{strategy_name} 청산 조건 대기"

    def _evaluate_utbb_exit_context(self, symbol, raw_hybrid_detail, current_side):
        current_side = str(current_side or '').lower()
        ut_signal = raw_hybrid_detail.get('ut_signal')
        ut_state = raw_hybrid_detail.get('ut_state')
        bb_reentry_down = bool(raw_hybrid_detail.get('bb_reentry_down'))
        bb_mid_two_bearish_exit = bool(raw_hybrid_detail.get('bb_mid_two_bearish_exit'))
        bb_reentry_up = bool(raw_hybrid_detail.get('bb_reentry_up'))
        bb_mid_reentry_up = bool(raw_hybrid_detail.get('bb_mid_reentry_up'))
        bb_lower_breakout = bool(raw_hybrid_detail.get('bb_lower_breakout'))
        special_long_active = self._is_utbb_special_long_active(symbol)
        special_short_active = self._is_utbb_special_short_active(symbol)
        special_short_mode = self._get_utbb_special_short_mode(symbol)

        result = {
            'raw_exit_long': False,
            'raw_exit_short': False,
            'exit_note': None,
            'hold_note': self._build_utbb_hold_note(
                'UTBB',
                current_side,
                special_long_active=special_long_active,
                special_short_active=special_short_active,
                special_short_mode=special_short_mode
            ),
            'special_long_active': special_long_active,
            'special_short_active': special_short_active,
            'special_short_mode': special_short_mode
        }

        if current_side == 'long' and (
            ut_state == 'short'
            or bb_reentry_down
            or ((not special_long_active) and bb_mid_two_bearish_exit)
        ):
            result['raw_exit_long'] = True
            result['exit_note'] = self._build_utbb_long_exit_note(
                ut_signal=ut_signal,
                ut_state=ut_state,
                bb_reentry_down=bb_reentry_down
            )
            return result

        if current_side == 'short' and (
            ut_state == 'long'
            or ((not special_short_active) and bb_lower_breakout)
            or (special_short_mode == 'lower_reentry' and bb_reentry_up)
            or (special_short_mode == 'mid_rebound_fail' and bb_mid_reentry_up)
        ):
            result['raw_exit_short'] = True
            result['exit_note'] = self._build_utbb_short_exit_note(
                ut_signal=ut_signal,
                ut_state=ut_state,
                special_short_mode=special_short_mode
            )
            return result

        return result

    async def _handle_utbb_primary_strategy(self, symbol, k, pos, strategy_name, raw_strategy_sig, raw_state_sig, raw_hybrid_detail, sig):
        ut_signal = raw_hybrid_detail.get('ut_signal')
        ut_state = raw_hybrid_detail.get('ut_state')
        ut_long_fresh = bool(raw_hybrid_detail.get('ut_long_fresh'))
        bb_long_fresh = bool(raw_hybrid_detail.get('bb_long_fresh'))
        long_pair_source = raw_hybrid_detail.get('utbb_long_pair_source')
        bb_upper_breakout = bool(raw_hybrid_detail.get('bb_upper_breakout'))
        bb_reentry_down = bool(raw_hybrid_detail.get('bb_reentry_down'))
        bb_mid_two_bearish_exit = bool(raw_hybrid_detail.get('bb_mid_two_bearish_exit'))
        bb_reentry_up = bool(raw_hybrid_detail.get('bb_reentry_up'))
        bb_mid_reentry_up = bool(raw_hybrid_detail.get('bb_mid_reentry_up'))
        bb_lower_breakout = bool(raw_hybrid_detail.get('bb_lower_breakout'))
        short_setup_ready = bool(raw_hybrid_detail.get('bb_short_setup_ready'))
        short_mid_ready = bool(raw_hybrid_detail.get('bb_mid_short_ready'))
        short_rebound_fail_ready = bool(raw_hybrid_detail.get('bb_mid_rebound_fail_ready'))
        short_rebound_fail_bull_count = int(raw_hybrid_detail.get('bb_mid_rebound_fail_bull_count') or 0)
        special_short_candidate = bool(raw_hybrid_detail.get('bb_special_short_candidate'))
        entry_sig = sig
        special_long_active = self._is_utbb_special_long_active(symbol)
        special_short_active = self._is_utbb_special_short_active(symbol)
        special_short_mode = self._get_utbb_special_short_mode(symbol)

        if pos:
            exit_tf = self._get_exit_timeframe(symbol)
            exit_ctx = self._evaluate_utbb_exit_context(symbol, raw_hybrid_detail, pos['side'])
            if exit_ctx.get('raw_exit_long') or exit_ctx.get('raw_exit_short'):
                exit_note = exit_ctx.get('exit_note') or 'exit condition'
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} {pos['side'].upper()} 청산 조건 감지 ({exit_note}), "
                    f"청산은 exit TF `{exit_tf}` 확인 후 실행"
                )
                self._update_stateful_diag(
                    symbol,
                    stage='exit_wait',
                    strategy=strategy_name,
                    raw_state=(raw_state_sig or 'none'),
                    raw_signal=(raw_strategy_sig or 'none'),
                    entry_sig=(entry_sig or 'none'),
                    pos_side=str(pos['side']).upper(),
                    note=f"primary exit condition detected | exit_tf={exit_tf} | {exit_note}"
                )
            else:
                hold_note = self._build_utbb_hold_note(
                    strategy_name,
                    pos['side'],
                    special_long_active=special_long_active,
                    special_short_active=special_short_active,
                    special_short_mode=special_short_mode
                )
                self.last_entry_reason[symbol] = (
                    f"포지션 보유 중 ({pos['side'].upper()}), {hold_note} | exit TF `{exit_tf}`"
                )
            return

        if not pos and entry_sig == 'long':
            self.last_entry_reason[symbol] = self._build_utbb_long_entry_reason(
                strategy_name,
                ut_signal=ut_signal,
                long_pair_source=long_pair_source,
                ut_long_fresh=ut_long_fresh,
                bb_long_fresh=bb_long_fresh,
                bb_upper_breakout=bb_upper_breakout
            )
            self._apply_utbb_long_entry_state(symbol, raw_hybrid_detail, bb_upper_breakout)
            self._clear_utbb_long_setup(symbol)
            self._clear_utbb_short_setup(symbol)
            self._clear_utbb_special_short_state(symbol)
            logger.info(f"[{strategy_name}] New LONG entry")
            await self.entry(symbol, 'long', float(k['c']))
            return

        if not pos and entry_sig == 'short':
            short_note = self._build_utbb_short_entry_note(
                ut_signal=ut_signal,
                short_mid_ready=short_mid_ready,
                short_rebound_fail_ready=short_rebound_fail_ready,
                short_rebound_fail_bull_count=short_rebound_fail_bull_count,
                special_short_candidate=special_short_candidate
            )
            self.last_entry_reason[symbol] = f"{strategy_name} {short_note} -> SHORT 진입"
            self._apply_utbb_short_entry_state(
                symbol,
                raw_hybrid_detail,
                special_short_candidate=special_short_candidate,
                short_rebound_fail_ready=short_rebound_fail_ready
            )
            self._clear_utbb_long_setup(symbol)
            self._clear_utbb_special_long_state(symbol)
            logger.info(f"[{strategy_name}] New SHORT entry")
            await self.entry(symbol, 'short', float(k['c']))
            if short_setup_ready:
                self._consume_utbb_short_setup(symbol)
            return

        if not pos and short_setup_ready:
            self._clear_utbb_special_long_state(symbol)
            self._clear_utbb_special_short_state(symbol)
            self.last_entry_reason[symbol] = f"{strategy_name} BB 숏 셋업 저장 중, UT 숏신호 대기"
            return

        if not pos:
            self._clear_utbb_special_long_state(symbol)
            self._clear_utbb_special_short_state(symbol)
            return

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
        strategy_params = self.get_runtime_strategy_params()
        active_strategy = strategy_params.get('active_strategy', 'utbot').lower()
        
        if await self.check_daily_loss_limit():
            logger.info("??Daily loss limit reached, skipping trade")
            if active_strategy == UTBOT_FILTERED_BREAKOUT_STRATEGY:
                self.last_entry_reason[symbol] = "REJECTED_DAILY_LOSS_LIMIT"
            self.last_candle_time[symbol] = processing_candle_time
            self.last_candle_success[symbol] = True
            return
        
        try:
            await self.check_status(symbol, float(k['c']))
            
            # [MODIFIED] Prioritize entry_timeframe for fetching entry OHLCV
            common_cfg = self.get_runtime_common_settings()
            if active_strategy == UTBOT_FILTERED_BREAKOUT_STRATEGY:
                filtered_cfg = self._get_utbot_filtered_breakout_config(strategy_params)
                tf = filtered_cfg.get('entry_timeframe', '15m')
            else:
                tf = common_cfg.get('entry_timeframe', common_cfg.get('timeframe', '15m'))
            ohlcv = await asyncio.to_thread(self.market_data_exchange.fetch_ohlcv, symbol, tf, limit=300)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            # [CRITICAL] Ensure numeric types (Robust Loop)
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            
            # ===== ?꾨왂 ?ㅼ젙 濡쒕뱶 =====
            strategy_context = self._collect_primary_strategy_context(symbol, df, strategy_params, active_strategy)
            raw_strategy_sig = strategy_context['raw_strategy_sig']
            raw_state_sig = strategy_context['raw_state_sig']
            raw_ut_detail = strategy_context['raw_ut_detail']
            raw_hybrid_detail = strategy_context['raw_hybrid_detail']
            
            # ?꾨왂蹂??좏샇 怨꾩궛
            sig, is_bullish, is_bearish, strategy_name, entry_mode, kalman_enabled = await self._calculate_strategy_signal(
                symbol,
                df,
                strategy_params,
                active_strategy,
                precomputed=strategy_context['precomputed']
            )
            strategy_sig = sig
            utbot_entry_filter_eval = None
            utbot_rsi_momentum_entry_eval = None
            utbot_rsi_momentum_exit_eval = None
            if active_strategy == 'utbot':
                utbot_entry_filter_eval = await self._evaluate_utbot_filter_pack(
                    symbol,
                    df,
                    strategy_params,
                    tf,
                    'entry'
                )
                self._store_utbot_filter_pack_status(symbol, 'entry', utbot_entry_filter_eval, strategy_params)
                utbot_rsi_momentum_entry_eval = self._evaluate_utbot_rsi_momentum_filter(
                    df,
                    strategy_params,
                    'entry'
                )
                utbot_rsi_momentum_exit_eval = self._evaluate_utbot_rsi_momentum_filter(
                    df,
                    strategy_params,
                    'exit'
                )
                self._store_utbot_rsi_momentum_filter_status(
                    symbol,
                    'entry',
                    utbot_rsi_momentum_entry_eval,
                    strategy_params
                )
            
            # 留ㅻℓ 諛⑺뼢 ?꾪꽣
            d_mode = self.get_effective_trade_direction()
            if sig and not self.is_trade_direction_allowed(sig):
                logger.info(f"??Signal {sig} blocked by direction filter: {d_mode}")
                self.last_entry_reason[symbol] = self.format_trade_direction_block_reason(sig)
                if active_strategy == UTBOT_FILTERED_BREAKOUT_STRATEGY:
                    self._clear_utbot_filtered_breakout_entry_plan(symbol)
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
                if active_strategy == 'utsmc':
                    ut_pending = self._get_utsmc_pending_entry(symbol)
                    ut_pending_source = None
                    ut_pending_state = 'armed' if ut_pending else None
                else:
                    ut_pending = self._get_ut_pending_entry(symbol)
                    ut_pending_source = self._get_ut_pending_source(ut_pending) if ut_pending else None
                    ut_pending_state = ut_pending.get('pending_state') if ut_pending else None
                ut_pending_side = str(ut_pending.get('side') or '').upper() if ut_pending else None
                ut_pending_execute_ts = int(ut_pending.get('execute_ts') or 0) if ut_pending else 0
                ut_pending_expires_ts = (
                    int(ut_pending.get('expires_ts') or 0)
                    if ut_pending and active_strategy != 'utsmc' else 0
                )

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
                    signal_open=raw_ut_detail.get('signal_open'),
                    signal_high=raw_ut_detail.get('signal_high'),
                    signal_low=raw_ut_detail.get('signal_low'),
                    signal_close=raw_ut_detail.get('signal_close'),
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
                    utsmc_entry_bullish_ob_entry=(
                        raw_hybrid_detail.get('bullish_internal_ob_entry') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_entry_bearish_ob_entry=(
                        raw_hybrid_detail.get('bearish_internal_ob_entry') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_entry_ob_tags=(
                        ", ".join(raw_hybrid_detail.get('ob_tags') or [])
                        if active_strategy == 'utsmc' and raw_hybrid_detail.get('ob_tags')
                        else None
                    ),
                    utsmc_entry_bullish_ob_top=(
                        raw_hybrid_detail.get('bullish_ob_top') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_entry_bullish_ob_bottom=(
                        raw_hybrid_detail.get('bullish_ob_bottom') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_entry_bearish_ob_top=(
                        raw_hybrid_detail.get('bearish_ob_top') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_entry_bearish_ob_bottom=(
                        raw_hybrid_detail.get('bearish_ob_bottom') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_entry_smc_signal=(
                        raw_hybrid_detail.get('smc_signal') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_entry_smc_reason=(
                        raw_hybrid_detail.get('smc_reason') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_entry_tf=tf if active_strategy == 'utsmc' else None,
                    utsmc_candidate_filter_mode=(
                        raw_hybrid_detail.get('candidate_filter_mode_label') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_candidate1_long_pass=(
                        raw_hybrid_detail.get('candidate1_long_pass') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_candidate1_short_pass=(
                        raw_hybrid_detail.get('candidate1_short_pass') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_candidate2_long_pass=(
                        raw_hybrid_detail.get('candidate2_long_pass') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_candidate2_short_pass=(
                        raw_hybrid_detail.get('candidate2_short_pass') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_candidate_final_long_pass=(
                        raw_hybrid_detail.get('candidate_final_long_pass') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_candidate_final_short_pass=(
                        raw_hybrid_detail.get('candidate_final_short_pass') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_candidate_reason_long=(
                        raw_hybrid_detail.get('candidate_reason_long') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_candidate_reason_short=(
                        raw_hybrid_detail.get('candidate_reason_short') if active_strategy == 'utsmc' else None
                    ),
                    ut_pending_source=ut_pending_source,
                    ut_pending_side=ut_pending_side,
                    ut_pending_state=ut_pending_state,
                    ut_pending_execute_ts=(ut_pending_execute_ts or None),
                    ut_pending_execute_ts_human=(
                        datetime.fromtimestamp(ut_pending_execute_ts / 1000).strftime('%m-%d %H:%M')
                        if ut_pending_execute_ts else None
                    ),
                    ut_pending_expires_ts=(ut_pending_expires_ts or None),
                    ut_pending_expires_ts_human=(
                        datetime.fromtimestamp(ut_pending_expires_ts / 1000).strftime('%m-%d %H:%M')
                        if ut_pending_expires_ts else None
                    ),
                    ut_flip_target_side=(
                        str(ut_pending.get('side') or '').upper()
                        if ut_pending and ut_pending_source == 'flip' else None
                    ),
                    ut_flip_signal_ts=(
                        int(ut_pending.get('signal_ts') or 0) if ut_pending and ut_pending_source == 'flip' else None
                    ),
                    ut_flip_signal_ts_human=(
                        datetime.fromtimestamp(int(ut_pending.get('signal_ts') or 0) / 1000).strftime('%m-%d %H:%M')
                        if ut_pending and ut_pending_source == 'flip' and int(ut_pending.get('signal_ts') or 0) else None
                    ),
                    ut_flip_execute_ts=(
                        ut_pending_execute_ts if ut_pending and ut_pending_source == 'flip' and ut_pending_execute_ts else None
                    ),
                    ut_flip_execute_ts_human=(
                        datetime.fromtimestamp(ut_pending_execute_ts / 1000).strftime('%m-%d %H:%M')
                        if ut_pending and ut_pending_source == 'flip' and ut_pending_execute_ts else None
                    ),
                    ut_flip_expires_ts=(
                        ut_pending_expires_ts if ut_pending and ut_pending_source == 'flip' and ut_pending_expires_ts else None
                    ),
                    ut_flip_expires_ts_human=(
                        datetime.fromtimestamp(ut_pending_expires_ts / 1000).strftime('%m-%d %H:%M')
                        if ut_pending and ut_pending_source == 'flip' and ut_pending_expires_ts else None
                    ),
                    ut_flip_retry_count=(
                        int(ut_pending.get('retry_count') or 0) if ut_pending and ut_pending_source == 'flip' else None
                    ),
                    ut_flip_block_reason=(
                        ut_pending.get('block_reason') if ut_pending and ut_pending_source == 'flip' else None
                    ),
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
                await self._handle_utbot_primary_strategy(
                    symbol,
                    k,
                    pos,
                    strategy_name,
                    raw_strategy_sig,
                    raw_state_sig,
                    raw_ut_detail,
                    sig,
                    utbot_entry_filter_eval,
                    utbot_rsi_momentum_entry_eval,
                    utbot_rsi_momentum_exit_eval
                )

            elif active_strategy == 'utsmc':
                await self._handle_utsmc_primary_strategy(
                    symbol,
                    k,
                    pos,
                    strategy_name,
                    raw_strategy_sig,
                    raw_state_sig,
                    raw_hybrid_detail,
                    sig
                )

            elif active_strategy == 'utbb':
                await self._handle_utbb_primary_strategy(
                    symbol,
                    k,
                    pos,
                    strategy_name,
                    raw_strategy_sig,
                    raw_state_sig,
                    raw_hybrid_detail,
                    sig
                )

            elif active_strategy in UT_HYBRID_STRATEGIES:
                await self._handle_ut_hybrid_primary_strategy(
                    symbol,
                    k,
                    pos,
                    strategy_name,
                    active_strategy,
                    raw_strategy_sig,
                    raw_state_sig,
                    raw_hybrid_detail,
                    sig
                )

            elif active_strategy == 'rsibb':
                await self._handle_rsibb_primary_strategy(
                    symbol,
                    k,
                    pos,
                    strategy_name,
                    raw_strategy_sig,
                    sig
                )

            elif active_strategy == UTBOT_FILTERED_BREAKOUT_STRATEGY:
                if pos:
                    self.last_entry_reason[symbol] = (
                        f"포지션 보유 중 ({pos['side'].upper()}), UTBOT_FILTERED_BREAKOUT_V1 신규 진입 대기"
                    )
                    self._clear_utbot_filtered_breakout_entry_plan(symbol)
                elif sig:
                    self.last_entry_reason[symbol] = f"ACCEPTED_ENTRY: {sig.upper()} filtered breakout -> 진입"
                    logger.info(f"[UTBOT_FILTERED_BREAKOUT_V1] New {sig.upper()} entry")
                    await self.entry(symbol, sig, float(k['c']))
                else:
                    self._clear_utbot_filtered_breakout_entry_plan(symbol)

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

    async def _post_ut_secondary_exit(self, symbol, active_strategy, previous_side):
        strategy_key = str(active_strategy or '').lower()
        previous_side = str(previous_side or '').lower()
        if self.is_upbit_mode():
            return
        if strategy_key not in ({'utbot'} | UT_HYBRID_STRATEGIES):
            return

        await asyncio.sleep(1)
        self.position_cache = None
        check_pos = await self.get_server_position(symbol, use_cache=False)
        if check_pos:
            logger.warning(
                f"[{STRATEGY_DISPLAY_NAMES.get(strategy_key, strategy_key.upper())}] "
                f"Secondary exit not yet confirmed for {symbol}: {check_pos['side']}"
            )
            self.last_stateful_retry_ts[symbol] = 0.0
            return

        pending = self._get_ut_pending_entry(symbol)
        pending_source = self._get_ut_pending_source(pending) if pending else None
        is_flip_pending = self._is_ut_flip_pending(pending) if pending else False
        if is_flip_pending:
            pending_side = str(pending.get('side') or '').upper() if pending else None
            execute_ts = int(pending.get('execute_ts') or 0) if pending else 0
            expires_ts = int(pending.get('expires_ts') or 0) if pending else 0
            signal_ts = int(pending.get('signal_ts') or 0) if pending else 0
            retry_count = int(pending.get('retry_count') or 0) if pending else 0
            self._touch_ut_pending_entry(symbol, pending_state='armed', block_reason=None)
            self._update_stateful_diag(
                symbol,
                stage='secondary_exit_confirmed',
                strategy=STRATEGY_DISPLAY_NAMES.get(strategy_key, strategy_key.upper()),
                pos_side='NONE',
                ut_pending_source=pending_source,
                ut_pending_side=pending_side,
                ut_pending_state='armed',
                ut_pending_execute_ts=execute_ts or None,
                ut_pending_execute_ts_human=(
                    datetime.fromtimestamp(execute_ts / 1000).strftime('%m-%d %H:%M')
                    if execute_ts else None
                ),
                ut_pending_expires_ts=expires_ts or None,
                ut_pending_expires_ts_human=(
                    datetime.fromtimestamp(expires_ts / 1000).strftime('%m-%d %H:%M')
                    if expires_ts else None
                ),
                ut_flip_target_side=pending_side,
                ut_flip_signal_ts=signal_ts or None,
                ut_flip_signal_ts_human=(
                    datetime.fromtimestamp(signal_ts / 1000).strftime('%m-%d %H:%M')
                    if signal_ts else None
                ),
                ut_flip_execute_ts=execute_ts or None,
                ut_flip_execute_ts_human=(
                    datetime.fromtimestamp(execute_ts / 1000).strftime('%m-%d %H:%M')
                    if execute_ts else None
                ),
                ut_flip_expires_ts=expires_ts or None,
                ut_flip_expires_ts_human=(
                    datetime.fromtimestamp(expires_ts / 1000).strftime('%m-%d %H:%M')
                    if expires_ts else None
                ),
                ut_flip_retry_count=retry_count,
                ut_flip_block_reason=None,
                note='secondary exit confirmed, flip pending preserved'
            )
        else:
            self._clear_ut_pending_entry(symbol)
        if strategy_key in UT_HYBRID_STRATEGIES:
            self._clear_ut_strategy_signal_state(symbol, strategy_key)
        if strategy_key == 'utbb':
            if previous_side == 'long':
                self._clear_utbb_special_long_state(symbol)
            elif previous_side == 'short':
                self._clear_utbb_special_short_state(symbol)
                self._clear_utbb_special_long_state(symbol)

        self.last_state_sync_candle_ts[symbol] = 0
        self.last_stateful_retry_ts[symbol] = 0.0

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
            active_strategy = strategy_params.get('active_strategy', 'utbot').lower()
            raw_exit_long = False
            raw_exit_short = False
            bypass_exit_filters = False
            c_f = c_s = 0.0

            if active_strategy == 'utsmc':
                strategy_name = "UTSMC(Exit)"
                smc_sig, exit_reason, smc_detail = self._calculate_smc_internal_ob_signal(df, strategy_params)
                candidate_eval_df = df.iloc[:-1].copy() if len(df.index) > 2 else df.copy()
                candidate_detail = self._calculate_utsmc_candidate_filter_state(candidate_eval_df, strategy_params)
                utsmc_cfg = strategy_params.get('UTSMC', {}) or {}
                exit_candidate2_enabled = bool(utsmc_cfg.get('exit_candidate2_enabled', False))
                current_closed_row = df.iloc[-2]
                current_closed_ts = int(current_closed_row['timestamp'])
                current_closed_price = float(current_closed_row['close'])
                current_closed_open = float(current_closed_row['open'])
                current_closed_high = float(current_closed_row['high'])
                current_closed_low = float(current_closed_row['low'])
                exit_candle_ms = self._timeframe_to_ms(tf)
                current_closed_end_ts = (
                    current_closed_ts + exit_candle_ms
                    if exit_candle_ms > 0 else current_closed_ts
                )
                current_closed_end_human = datetime.fromtimestamp(
                    current_closed_end_ts / 1000
                ).strftime('%m-%d %H:%M') if current_closed_end_ts else None
                current_closed_ohlc_text = (
                    f"{datetime.fromtimestamp(current_closed_ts / 1000).strftime('%m-%d %H:%M')} "
                    f"O{current_closed_open:.2f} H{current_closed_high:.2f} "
                    f"L{current_closed_low:.2f} C{current_closed_price:.2f}"
                )
                invalidation = self._get_utsmc_entry_invalidation(symbol) or {}
                invalidation_side = str(invalidation.get('side') or '').lower()
                signal_high = invalidation.get('signal_high')
                signal_low = invalidation.get('signal_low')
                position_side = str(current_side or '').lower()
                entry_anchor_ts = int(self.utsmc_last_entry_signal_ts.get(symbol, 0) or 0)
                required_ob_side = 'bearish' if position_side == 'long' else 'bullish'
                fixed_exit_ob = self._get_utsmc_fixed_exit_ob(symbol)
                if fixed_exit_ob:
                    fixed_position_side = str(fixed_exit_ob.get('position_side') or '').lower()
                    fixed_ob_side = str(fixed_exit_ob.get('ob_side') or '').lower()
                    if fixed_position_side != position_side or fixed_ob_side != required_ob_side:
                        self._clear_utsmc_fixed_exit_ob(symbol)
                        fixed_exit_ob = None

                if not fixed_exit_ob and position_side in {'long', 'short'}:
                    candidate_obs = (
                        list(smc_detail.get('active_bearish_obs') or [])
                        if position_side == 'long'
                        else list(smc_detail.get('active_bullish_obs') or [])
                    )
                    eligible_obs = []
                    for order_block in candidate_obs:
                        created_ts = int(order_block.get('created_ts') or 0)
                        if entry_anchor_ts and created_ts < entry_anchor_ts:
                            continue
                        eligible_obs.append(order_block)
                    if eligible_obs:
                        fixed_candidate = min(
                            eligible_obs,
                            key=lambda ob: (
                                int(ob.get('created_ts') or 0),
                                int(ob.get('origin_ts') or 0)
                            )
                        )
                        self._set_utsmc_fixed_exit_ob(
                            symbol,
                            position_side,
                            required_ob_side,
                            fixed_candidate,
                            tf=tf
                        )
                        fixed_exit_ob = self._get_utsmc_fixed_exit_ob(symbol)

                fixed_ob_side = str((fixed_exit_ob or {}).get('ob_side') or '').lower()
                fixed_ob_top = (fixed_exit_ob or {}).get('top')
                fixed_ob_bottom = (fixed_exit_ob or {}).get('bottom')
                fixed_ob_origin_ts = int((fixed_exit_ob or {}).get('origin_ts') or 0)
                fixed_ob_created_ts = int((fixed_exit_ob or {}).get('created_ts') or 0)
                fixed_ob_origin_human = (
                    datetime.fromtimestamp(fixed_ob_origin_ts / 1000).strftime('%m-%d %H:%M')
                    if fixed_ob_origin_ts else None
                )
                fixed_ob_created_human = (
                    datetime.fromtimestamp(fixed_ob_created_ts / 1000).strftime('%m-%d %H:%M')
                    if fixed_ob_created_ts else None
                )
                fixed_bearish_ob_entry = (
                    position_side == 'long'
                    and fixed_ob_side == 'bearish'
                    and fixed_ob_bottom is not None
                    and fixed_ob_top is not None
                    and float(fixed_ob_bottom) <= current_closed_price <= float(fixed_ob_top)
                )
                fixed_bullish_ob_entry = (
                    position_side == 'short'
                    and fixed_ob_side == 'bullish'
                    and fixed_ob_bottom is not None
                    and fixed_ob_top is not None
                    and float(fixed_ob_bottom) <= current_closed_price <= float(fixed_ob_top)
                )
                ut_invalidation_long = (
                    current_side.lower() == 'long'
                    and invalidation_side in {'', 'long'}
                    and signal_low is not None
                    and current_closed_price <= float(signal_low)
                )
                ut_invalidation_short = (
                    current_side.lower() == 'short'
                    and invalidation_side in {'', 'short'}
                    and signal_high is not None
                    and current_closed_price >= float(signal_high)
                )
                exit_trigger_tags = []
                exit_reason_parts = []
                candidate2_long_exit = bool(
                    exit_candidate2_enabled and candidate_detail.get('candidate2_long_pass', False)
                )
                candidate2_short_exit = bool(
                    exit_candidate2_enabled and candidate_detail.get('candidate2_short_pass', False)
                )
                if current_side.lower() == 'long':
                    if fixed_bearish_ob_entry:
                        exit_trigger_tags.append('fixed_internal_bearish_ob_entry')
                        exit_reason_parts.append(
                            f"FIXED INTERNAL BEARISH OB ENTRY {float(fixed_ob_bottom or 0.0):.4f} ~ "
                            f"{float(fixed_ob_top or 0.0):.4f}"
                        )
                    if ut_invalidation_long:
                        exit_trigger_tags.append('ut_long_invalidation')
                        exit_reason_parts.append(
                            f"UT LONG INVALIDATION close {current_closed_price:.4f} <= signal low {float(signal_low):.4f}"
                        )
                    if candidate2_short_exit:
                        exit_trigger_tags.append('candidate2_short_exit')
                        exit_reason_parts.append(
                            f"OPPOSITE C2 SHORT {candidate_detail.get('candidate2_reason_short')}"
                        )
                    raw_exit_long = bool(exit_reason_parts)
                    raw_exit_short = False
                else:
                    if fixed_bullish_ob_entry:
                        exit_trigger_tags.append('fixed_internal_bullish_ob_entry')
                        exit_reason_parts.append(
                            f"FIXED INTERNAL BULLISH OB ENTRY {float(fixed_ob_bottom or 0.0):.4f} ~ "
                            f"{float(fixed_ob_top or 0.0):.4f}"
                        )
                    if ut_invalidation_short:
                        exit_trigger_tags.append('ut_short_invalidation')
                        exit_reason_parts.append(
                            f"UT SHORT INVALIDATION close {current_closed_price:.4f} >= signal high {float(signal_high):.4f}"
                        )
                    if candidate2_long_exit:
                        exit_trigger_tags.append('candidate2_long_exit')
                        exit_reason_parts.append(
                            f"OPPOSITE C2 LONG {candidate_detail.get('candidate2_reason_long')}"
                        )
                    raw_exit_short = bool(exit_reason_parts)
                    raw_exit_long = False
                if exit_reason_parts:
                    exit_reason = " | ".join(exit_reason_parts)
                elif current_side.lower() == 'long':
                    if fixed_ob_side == 'bearish' and fixed_ob_bottom is not None and fixed_ob_top is not None:
                        exit_reason = (
                            f"FIXED INTERNAL BEARISH OB WAIT {float(fixed_ob_bottom):.4f} ~ "
                            f"{float(fixed_ob_top):.4f}"
                        )
                    else:
                        exit_reason = (
                            f"FIXED INTERNAL BEARISH OB 대기: "
                            f"active_bear_ob={'Y' if smc_detail.get('active_bearish_ob_count', 0) else 'N'}"
                        )
                else:
                    if fixed_ob_side == 'bullish' and fixed_ob_bottom is not None and fixed_ob_top is not None:
                        exit_reason = (
                            f"FIXED INTERNAL BULLISH OB WAIT {float(fixed_ob_bottom):.4f} ~ "
                            f"{float(fixed_ob_top):.4f}"
                        )
                    else:
                        exit_reason = (
                            f"FIXED INTERNAL BULLISH OB 대기: "
                            f"active_bull_ob={'Y' if smc_detail.get('active_bullish_ob_count', 0) else 'N'}"
                        )
                bypass_exit_filters = True
                self._update_stateful_diag(
                    symbol,
                    smc_bullish_ob_entry=fixed_bullish_ob_entry,
                    smc_bearish_ob_entry=fixed_bearish_ob_entry,
                    smc_ob_tags=(
                        ", ".join(exit_trigger_tags)
                        if exit_trigger_tags else
                        (f"fixed_{fixed_ob_side}_ob_locked" if fixed_ob_side else None)
                    ),
                    smc_bullish_ob_top=(float(fixed_ob_top) if fixed_ob_side == 'bullish' and fixed_ob_top is not None else None),
                    smc_bullish_ob_bottom=(float(fixed_ob_bottom) if fixed_ob_side == 'bullish' and fixed_ob_bottom is not None else None),
                    smc_bearish_ob_top=(float(fixed_ob_top) if fixed_ob_side == 'bearish' and fixed_ob_top is not None else None),
                    smc_bearish_ob_bottom=(float(fixed_ob_bottom) if fixed_ob_side == 'bearish' and fixed_ob_bottom is not None else None),
                    smc_signal=smc_sig,
                    smc_reason=exit_reason,
                    smc_tf=tf,
                    utsmc_signal_high=signal_high,
                    utsmc_signal_low=signal_low,
                    utsmc_fixed_ob_side=(fixed_ob_side or None),
                    utsmc_fixed_ob_top=fixed_ob_top,
                    utsmc_fixed_ob_bottom=fixed_ob_bottom,
                    utsmc_fixed_ob_created_ts_human=fixed_ob_created_human,
                    utsmc_fixed_ob_origin_ts_human=fixed_ob_origin_human,
                    utsmc_exit_candidate2_enabled=exit_candidate2_enabled,
                    utsmc_exit_candidate2_long_pass=candidate_detail.get('candidate2_long_pass'),
                    utsmc_exit_candidate2_short_pass=candidate_detail.get('candidate2_short_pass'),
                    utsmc_exit_candidate2_reason_long=candidate_detail.get('candidate2_reason_long'),
                    utsmc_exit_candidate2_reason_short=candidate_detail.get('candidate2_reason_short'),
                    exit_reason_text=exit_reason,
                    exit_trigger_kind=", ".join(exit_trigger_tags) if exit_trigger_tags else None,
                    exit_tf=tf,
                    exit_closed_ts=current_closed_end_ts,
                    exit_closed_ts_human=current_closed_end_human,
                    exit_closed_open=current_closed_open,
                    exit_closed_high=current_closed_high,
                    exit_closed_low=current_closed_low,
                    exit_closed_close=current_closed_price,
                    exit_closed_ohlc_text=current_closed_ohlc_text
                )
                last_utsmc_entry_ts = int(self.utsmc_last_entry_signal_ts.get(symbol, 0) or 0)
                if last_utsmc_entry_ts and current_closed_end_ts <= last_utsmc_entry_ts:
                    raw_exit_long = False
                    raw_exit_short = False
                if raw_exit_long or raw_exit_short:
                    logger.info(f"[Exit Debug] {symbol} {strategy_name}: {exit_reason}")
            elif active_strategy == 'cameron':
                strategy_name = "CAMERON(Exit)"
                exit_sig, exit_reason, _ = self._calculate_cameron_signal(df, strategy_params)
                raw_exit_long = current_side.lower() == 'long' and exit_sig == 'short'
                raw_exit_short = current_side.lower() == 'short' and exit_sig == 'long'
                if raw_exit_long or raw_exit_short:
                    logger.info(f"[Exit Debug] {symbol} {strategy_name}: {exit_reason}")
            elif active_strategy == 'utbot':
                strategy_name = "UTBOT(Exit)"
                exit_sig, exit_reason, utbot_exit_detail = self._calculate_utbot_signal(df, strategy_params)
                exit_state = str(exit_sig or utbot_exit_detail.get('bias_side') or '').lower()
                base_exit_long = current_side.lower() == 'long' and exit_state == 'short'
                base_exit_short = current_side.lower() == 'short' and exit_state == 'long'
                utbot_exit_eval = await self._evaluate_utbot_filter_pack(
                    symbol,
                    df,
                    strategy_params,
                    tf,
                    'exit'
                )
                self._store_utbot_filter_pack_status(symbol, 'exit', utbot_exit_eval, strategy_params)
                utbot_rsi_exit_eval = self._evaluate_utbot_rsi_momentum_filter(
                    df,
                    strategy_params,
                    'exit'
                )
                self._store_utbot_rsi_momentum_filter_status(
                    symbol,
                    'exit',
                    utbot_rsi_exit_eval,
                    strategy_params
                )

                confirm_selected = utbot_exit_eval.get('confirm_selected', [])
                signal_selected = utbot_exit_eval.get('signal_selected', [])
                confirm_long_pass = bool(utbot_exit_eval.get('confirm_long_pass', True))
                confirm_short_pass = bool(utbot_exit_eval.get('confirm_short_pass', True))
                signal_long_trigger = bool(utbot_exit_eval.get('signal_long_trigger', False))
                signal_short_trigger = bool(utbot_exit_eval.get('signal_short_trigger', False))
                rsi_exit_long_pass = bool(utbot_rsi_exit_eval.get('long_pass', True))
                rsi_exit_short_pass = bool(utbot_rsi_exit_eval.get('short_pass', True))
                rsi_filter_enabled = bool(utbot_rsi_exit_eval.get('enabled', False))

                def _collect_utbot_exit_filter_text(filter_ids, side):
                    reason_key = 'reason_long' if side == 'long' else 'reason_short'
                    pass_key = 'long_pass' if side == 'long' else 'short_pass'
                    parts = []
                    for filter_id in filter_ids:
                        filter_detail = (utbot_exit_eval.get('filters') or {}).get(str(filter_id), {})
                        if filter_detail.get(pass_key, False):
                            parts.append(
                                f"{filter_id}:{filter_detail.get('label', get_utbot_filter_pack_label(filter_id))} "
                                f"({filter_detail.get(reason_key, '-')})"
                            )
                    return " | ".join(parts)

                if rsi_filter_enabled:
                    raw_exit_long = base_exit_long and confirm_long_pass and rsi_exit_long_pass
                    raw_exit_short = base_exit_short and confirm_short_pass and rsi_exit_short_pass
                else:
                    raw_exit_long = (base_exit_long and confirm_long_pass) or signal_long_trigger
                    raw_exit_short = (base_exit_short and confirm_short_pass) or signal_short_trigger

                if current_side.lower() == 'long':
                    exit_reason_parts = []
                    long_trigger_present = base_exit_long if rsi_filter_enabled else (base_exit_long or signal_long_trigger)
                    if base_exit_long:
                        if confirm_selected:
                            if confirm_long_pass:
                                confirm_text = _collect_utbot_exit_filter_text(confirm_selected, 'long')
                                exit_reason_parts.append(
                                    f"{exit_reason} | confirm {format_utbot_filter_pack_logic(utbot_exit_eval.get('logic', 'and'))} "
                                    f"{confirm_text or 'PASS'}"
                                )
                        else:
                            exit_reason_parts.append(exit_reason)
                    if signal_long_trigger and not rsi_filter_enabled:
                        signal_text = _collect_utbot_exit_filter_text(signal_selected, 'long')
                        exit_reason_parts.append(
                            f"UTBOT FILTER SIGNAL EXIT | {format_utbot_filter_pack_logic(utbot_exit_eval.get('logic', 'and'))} "
                            f"{signal_text or 'PASS'}"
                        )
                    if long_trigger_present and utbot_rsi_exit_eval.get('enabled', False) and rsi_exit_long_pass:
                        exit_reason_parts.append(
                            f"RSI Momentum Trend confirm ({utbot_rsi_exit_eval.get('reason_long', 'PASS')})"
                        )
                    if exit_reason_parts:
                        exit_reason = " | ".join(exit_reason_parts)
                elif current_side.lower() == 'short':
                    exit_reason_parts = []
                    short_trigger_present = base_exit_short if rsi_filter_enabled else (base_exit_short or signal_short_trigger)
                    if base_exit_short:
                        if confirm_selected:
                            if confirm_short_pass:
                                confirm_text = _collect_utbot_exit_filter_text(confirm_selected, 'short')
                                exit_reason_parts.append(
                                    f"{exit_reason} | confirm {format_utbot_filter_pack_logic(utbot_exit_eval.get('logic', 'and'))} "
                                    f"{confirm_text or 'PASS'}"
                                )
                        else:
                            exit_reason_parts.append(exit_reason)
                    if signal_short_trigger and not rsi_filter_enabled:
                        signal_text = _collect_utbot_exit_filter_text(signal_selected, 'short')
                        exit_reason_parts.append(
                            f"UTBOT FILTER SIGNAL EXIT | {format_utbot_filter_pack_logic(utbot_exit_eval.get('logic', 'and'))} "
                            f"{signal_text or 'PASS'}"
                        )
                    if short_trigger_present and utbot_rsi_exit_eval.get('enabled', False) and rsi_exit_short_pass:
                        exit_reason_parts.append(
                            f"RSI Momentum Trend confirm ({utbot_rsi_exit_eval.get('reason_short', 'PASS')})"
                        )
                    if exit_reason_parts:
                        exit_reason = " | ".join(exit_reason_parts)
                if raw_exit_long or raw_exit_short:
                    logger.info(f"[Exit Debug] {symbol} {strategy_name}: {exit_reason}")
            elif active_strategy == UTBOT_FILTERED_BREAKOUT_STRATEGY:
                strategy_name = "UTBOT_FILTERED_BREAKOUT_V1(Exit)"
                fb_cfg = self._get_utbot_filtered_breakout_config(strategy_params)
                exit_reason_parts = []
                closed = df.iloc[:-1].copy().reset_index(drop=True)
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    closed[col] = pd.to_numeric(closed[col], errors='coerce')
                closed = closed.dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)

                if bool(fb_cfg.get('opposite_signal_exit_enabled', False)):
                    exit_sig, ut_exit_reason, _ = self._calculate_utbot_signal(
                        df,
                        self._get_utbot_filtered_breakout_ut_params(fb_cfg)
                    )
                    if current_side.lower() == 'long' and exit_sig == 'short':
                        raw_exit_long = True
                        exit_reason_parts.append(f"UT opposite SELL ({ut_exit_reason})")
                    elif current_side.lower() == 'short' and exit_sig == 'long':
                        raw_exit_short = True
                        exit_reason_parts.append(f"UT opposite BUY ({ut_exit_reason})")

                opposite_set_exit, opposite_set_reason = await self._evaluate_utbreakout_opposite_set_exit(
                    symbol,
                    df,
                    fb_cfg,
                    current_side,
                    pos
                )
                if opposite_set_exit:
                    if current_side.lower() == 'long':
                        raw_exit_long = True
                    elif current_side.lower() == 'short':
                        raw_exit_short = True
                    exit_reason_parts.append(opposite_set_reason)

                if bool(fb_cfg.get('ema_rsi_exit_enabled', False)) and len(closed) >= int(fb_cfg['ema_fast']) + int(fb_cfg['rsi_length']) + 2:
                    close_series = closed['close'].astype(float)
                    curr_close = float(close_series.iloc[-1])
                    ema50 = float(close_series.ewm(span=int(fb_cfg['ema_fast']), adjust=False).mean().iloc[-1])
                    rsi_series = self._calculate_wilder_rsi_series(close_series, int(fb_cfg['rsi_length']))
                    rsi_value = float(rsi_series.iloc[-1]) if self._is_valid_number(rsi_series.iloc[-1]) else np.nan
                    if current_side.lower() == 'long' and curr_close < ema50 and self._is_valid_number(rsi_value) and rsi_value < 50.0:
                        raw_exit_long = True
                        exit_reason_parts.append(f"EMA50/RSI exit close={curr_close:.4f} < EMA50={ema50:.4f}, RSI={rsi_value:.2f}")
                    elif current_side.lower() == 'short' and curr_close > ema50 and self._is_valid_number(rsi_value) and rsi_value > 50.0:
                        raw_exit_short = True
                        exit_reason_parts.append(f"EMA50/RSI exit close={curr_close:.4f} > EMA50={ema50:.4f}, RSI={rsi_value:.2f}")

                if bool(fb_cfg.get('adx_donchian_exit_enabled', False)) and len(closed) >= int(fb_cfg['donchian_length']) + int(fb_cfg['adx_length']) * 2 + 5:
                    curr_close = float(closed['close'].astype(float).iloc[-1])
                    adx_value, _, _, _ = self._calculate_utbot_filter_pack_adx_dmi(closed, int(fb_cfg['adx_length']))
                    don_window = closed.iloc[-int(fb_cfg['donchian_length']) - 1:-1]
                    don_high_prev = float(don_window['high'].astype(float).max())
                    don_low_prev = float(don_window['low'].astype(float).min())
                    back_inside = don_low_prev <= curr_close <= don_high_prev
                    adx_low = adx_value is not None and self._is_valid_number(adx_value) and float(adx_value) < float(fb_cfg['adx_threshold'])
                    if current_side.lower() == 'long' and back_inside and adx_low:
                        raw_exit_long = True
                        exit_reason_parts.append(f"ADX/Donchian re-entry exit ADX={float(adx_value):.2f}")
                    elif current_side.lower() == 'short' and back_inside and adx_low:
                        raw_exit_short = True
                        exit_reason_parts.append(f"ADX/Donchian re-entry exit ADX={float(adx_value):.2f}")

                exit_reason = " | ".join(exit_reason_parts) if exit_reason_parts else "SL/TP managed; optional exit filters OFF"
                if raw_exit_long or raw_exit_short:
                    logger.info(f"[Exit Debug] {symbol} {strategy_name}: {exit_reason}")
            elif active_strategy == 'utbb':
                strategy_name = "UTBB(Exit)"
                _, exit_reason, utbb_detail = self._calculate_utbb_signal(None, df, strategy_params)
                exit_ctx = self._evaluate_utbb_exit_context(symbol, utbb_detail, current_side)
                raw_exit_long = bool(exit_ctx.get('raw_exit_long'))
                raw_exit_short = bool(exit_ctx.get('raw_exit_short'))
                if raw_exit_long or raw_exit_short:
                    exit_note = exit_ctx.get('exit_note') or exit_reason
                    exit_reason = f"UTBB EXIT: {exit_note}"
                    logger.info(f"[Exit Debug] {symbol} {strategy_name}: {exit_reason}")
                else:
                    exit_reason = exit_ctx.get('hold_note') or exit_reason
            elif active_strategy in {'utrsi', 'utrsibb'}:
                strategy_name = f"{STRATEGY_DISPLAY_NAMES.get(active_strategy, active_strategy.upper())}(Exit)"
                hybrid_calc = {
                    'utrsibb': self._calculate_utrsibb_signal,
                    'utrsi': self._calculate_utrsi_signal
                }.get(active_strategy)
                _, hybrid_reason, hybrid_detail = hybrid_calc(None, df, strategy_params)
                regime_sig = str(hybrid_detail.get('ut_state') or '').lower()
                timing_label = hybrid_detail.get('timing_label') or 'Signal'
                timing_sig = hybrid_detail.get('effective_timing_signal') or hybrid_detail.get('timing_signal')
                raw_exit_long = current_side.lower() == 'long' and regime_sig == 'short'
                raw_exit_short = current_side.lower() == 'short' and regime_sig == 'long'
                if raw_exit_long or raw_exit_short:
                    exit_reason = f"{strategy_name} EXIT: UT {regime_sig.upper()} state on exit TF"
                    if timing_sig in {'long', 'short'}:
                        exit_reason += f" | {timing_label} {str(timing_sig).upper()}"
                    logger.info(f"[Exit Debug] {symbol} {strategy_name}: {exit_reason}")
                else:
                    exit_reason = hybrid_reason
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

            if bypass_exit_filters:
                if current_side.lower() == 'long' and raw_exit_long:
                    if fixed_bearish_ob_entry and ut_invalidation_long:
                        exit_call_reason = f"{strategy_name}_MultiTriggerLongExit"
                    elif ut_invalidation_long:
                        exit_call_reason = f"{strategy_name}_UTLongInvalidation"
                    elif candidate2_short_exit:
                        exit_call_reason = f"{strategy_name}_Candidate2ShortExit"
                    else:
                        exit_call_reason = f"{strategy_name}_InternalBearishOBEntry"
                    logger.info(f"[Exit {tf}] LONG Exit Triggered: {exit_reason}")
                    await self.exit_position(symbol, exit_call_reason)
                elif current_side.lower() == 'short' and raw_exit_short:
                    if fixed_bullish_ob_entry and ut_invalidation_short:
                        exit_call_reason = f"{strategy_name}_MultiTriggerShortExit"
                    elif ut_invalidation_short:
                        exit_call_reason = f"{strategy_name}_UTShortInvalidation"
                    elif candidate2_long_exit:
                        exit_call_reason = f"{strategy_name}_Candidate2LongExit"
                    else:
                        exit_call_reason = f"{strategy_name}_InternalBullishOBEntry"
                    logger.info(f"[Exit {tf}] SHORT Exit Triggered: {exit_reason}")
                    await self.exit_position(symbol, exit_call_reason)
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
                        if (not self.is_upbit_mode()) and (active_strategy == 'utbot' or active_strategy in UT_HYBRID_STRATEGIES):
                            await self._post_ut_secondary_exit(symbol, active_strategy, current_side)
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
                        if (not self.is_upbit_mode()) and (active_strategy == 'utbot' or active_strategy in UT_HYBRID_STRATEGIES):
                            await self._post_ut_secondary_exit(symbol, active_strategy, current_side)
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

    async def _calculate_strategy_signal(self, symbol, df, strategy_params, active_strategy, allow_utbot_stateful=True, precomputed=None):
        """
        ?꾨왂蹂??좏샇 怨꾩궛

        Returns: (signal, is_bullish, is_bearish, strategy_name, entry_mode, kalman_enabled)
        """
        sig = None
        is_bullish = False
        is_bearish = False
        entry_reason = "신호 대기"
        
        precomputed = precomputed or {}

        # Init state dicts if needed
        self._ensure_runtime_state_containers((
            'vbo_states',
            'fisher_states',
            'cameron_states',
            'kalman_states'
        ))
        if symbol not in self.kalman_states:
            self.kalman_states[symbol] = {'velocity': 0.0, 'direction': None}

        active_strategy = str(active_strategy).lower()
        if active_strategy not in CORE_STRATEGIES:
            logger.warning(f"Unsupported active_strategy '{active_strategy}' in core mode. Using UTBOT.")
            active_strategy = 'utbot'

        entry_mode = active_strategy
        kalman_entry_enabled = False
        self.last_entry_filter_status[symbol] = {}
        self.kalman_states[symbol]['velocity'] = 0.0
        self.kalman_states[symbol]['direction'] = None

        strategy_name = STRATEGY_DISPLAY_NAMES.get(active_strategy, active_strategy.upper())

        if active_strategy == 'utbot':
            entry_mode = 'utbot'
            sig, entry_reason, utbot_detail = precomputed.get('utbot') or self._calculate_utbot_signal(df, strategy_params)
            if sig is None and allow_utbot_stateful:
                bias_sig = utbot_detail.get('bias_side')
                if bias_sig in {'long', 'short'}:
                    sig = bias_sig
            is_bullish = sig == 'long'
            is_bearish = sig == 'short'

        elif active_strategy == 'utsmc':
            entry_mode = 'utsmc'
            sig, entry_reason, utsmc_detail = precomputed.get('utsmc') or self._calculate_utsmc_signal(df, strategy_params)
            ut_state = utsmc_detail.get('ut_state')
            is_bullish = ut_state == 'long'
            is_bearish = ut_state == 'short'

        elif active_strategy == 'utbb':
            entry_mode = 'utbb'
            sig, entry_reason, hybrid_detail = precomputed.get('utbb') or self._calculate_utbb_signal(symbol, df, strategy_params)
            ut_state = hybrid_detail.get('ut_state')
            is_bullish = ut_state == 'long'
            is_bearish = ut_state == 'short'

        elif active_strategy in UT_HYBRID_STRATEGIES:
            entry_mode = active_strategy
            hybrid_calc = {
                'utrsibb': self._calculate_utrsibb_signal,
                'utrsi': self._calculate_utrsi_signal
            }.get(active_strategy)
            sig, entry_reason, hybrid_detail = precomputed.get(active_strategy) or hybrid_calc(symbol, df, strategy_params)
            ut_state = hybrid_detail.get('ut_state')
            is_bullish = ut_state == 'long'
            is_bearish = ut_state == 'short'
        elif active_strategy == 'rsibb':
            entry_mode = 'rsibb'
            sig, entry_reason, _ = precomputed.get('rsibb') or self._calculate_rsibb_signal(df, strategy_params)
            is_bullish = sig == 'long'
            is_bearish = sig == 'short'
        elif active_strategy == UTBOT_FILTERED_BREAKOUT_STRATEGY:
            entry_mode = UTBOT_FILTERED_BREAKOUT_STRATEGY
            sig, entry_reason, _ = await self._calculate_utbot_filtered_breakout_signal(symbol, df, strategy_params)
            is_bullish = sig == 'long'
            is_bearish = sig == 'short'

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
            if not self.is_trade_direction_allowed(side):
                block_reason = self.format_trade_direction_block_reason(side)
                display_symbol = self.ctrl.format_symbol_for_display(symbol)
                logger.info(
                    f"[Signal] Entry blocked by direction filter: "
                    f"{display_symbol} {str(side or '').upper()} vs {self.get_effective_trade_direction().upper()}"
                )
                self.last_entry_reason[symbol] = block_reason
                await self.ctrl.notify(f"⚠️ {display_symbol} {block_reason}")
                return

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
            strategy_params = self.get_runtime_strategy_params()
            active_strategy = strategy_params.get('active_strategy', '').lower()
            filtered_breakout_plan = (
                self._get_utbot_filtered_breakout_entry_plan(symbol, side)
                if active_strategy == UTBOT_FILTERED_BREAKOUT_STRATEGY else None
            )

            def _record_filtered_breakout_entry_block(code, reason, extra=None):
                if active_strategy != UTBOT_FILTERED_BREAKOUT_STRATEGY:
                    return
                fb_cfg = self._get_utbot_filtered_breakout_config(strategy_params)
                status = {
                    'candidate_side': side,
                    'entry_timeframe': fb_cfg.get('entry_timeframe', '15m'),
                    'htf_timeframe': fb_cfg.get('htf_timeframe', '1h'),
                    'reason': reason,
                    'entry_price': price,
                    'entry_plan': dict(filtered_breakout_plan or {})
                }
                payload_extra = {'code': code}
                if isinstance(extra, dict):
                    payload_extra.update(extra)
                self._record_utbreakout_diagnostic_event(
                    symbol,
                    status,
                    event='entry_blocked',
                    extra=payload_extra
                )

            lev_default = 5 if active_strategy == UTBOT_FILTERED_BREAKOUT_STRATEGY else 10
            lev = int(max(1.0, float(cfg.get('leverage', lev_default) or lev_default)))
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
            
            safety_buffer = 0.98
            if active_strategy == UTBOT_FILTERED_BREAKOUT_STRATEGY:
                if not filtered_breakout_plan:
                    _record_filtered_breakout_entry_block(
                        'ENTRY_BLOCKED_MISSING_PLAN',
                        'UTBOT_FILTERED_BREAKOUT_V1 entry blocked: missing risk plan'
                    )
                    await self.ctrl.notify("⚠️ UTBOT_FILTERED_BREAKOUT_V1 진입 계획이 없어 주문을 중단합니다.")
                    return
                planned_qty = float(filtered_breakout_plan.get('qty', 0.0) or 0.0)
                target_notional = planned_qty * float(price)
                margin_to_use = target_notional / max(float(lev), 1e-9)
            else:
                # Position sizing (user-friendly):
                # 1) Use configured % of current free USDT as margin.
                # 2) Apply leverage to that margin to get position notional.
                # 3) Keep a small safety buffer to avoid -2019 by fees/slippage.
                margin_to_use = free * risk_pct
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
                if active_strategy == UTBOT_FILTERED_BREAKOUT_STRATEGY:
                    logger.warning(
                        f"[UTBOT_FILTERED_BREAKOUT_V1] Entry blocked by min notional: "
                        f"target={target_notional:.2f}, min={min_notional:.2f}, risk_plan={filtered_breakout_plan}"
                    )
                    _record_filtered_breakout_entry_block(
                        'ENTRY_BLOCKED_MIN_NOTIONAL',
                        'UTBOT_FILTERED_BREAKOUT_V1 entry blocked by min notional',
                        {
                            'target_notional': target_notional,
                            'min_notional': min_notional,
                            'free_balance': free,
                            'leverage': lev
                        }
                    )
                    await self.ctrl.notify(
                        f"⚠️ UTBOT_FILTERED_BREAKOUT_V1 최소 주문금액 미달: "
                        f"계획 {target_notional:.2f} < 필요 {min_notional:.2f} USDT. "
                        "리스크 기반 수량을 임의 증액하지 않고 진입을 차단합니다."
                    )
                    self._clear_utbot_filtered_breakout_entry_plan(symbol)
                    return
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

            if active_strategy == UTBOT_FILTERED_BREAKOUT_STRATEGY and target_notional > max_notional:
                logger.warning(
                    f"[UTBOT_FILTERED_BREAKOUT_V1] Entry blocked by margin cap: "
                    f"target={target_notional:.2f}, max={max_notional:.2f}, free={free:.2f}, lev={lev}"
                )
                _record_filtered_breakout_entry_block(
                    'ENTRY_BLOCKED_MARGIN_CAP',
                    'UTBOT_FILTERED_BREAKOUT_V1 entry blocked by margin cap',
                    {
                        'target_notional': target_notional,
                        'max_notional': max_notional,
                        'free_balance': free,
                        'leverage': lev
                    }
                )
                await self.ctrl.notify(
                    f"⚠️ UTBOT_FILTERED_BREAKOUT_V1 증거금 부족: 계획 {target_notional:.2f} > 가능 {max_notional:.2f} USDT"
                )
                self._clear_utbot_filtered_breakout_entry_plan(symbol)
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
                if active_strategy == UTBOT_FILTERED_BREAKOUT_STRATEGY:
                    _record_filtered_breakout_entry_block(
                        'ENTRY_BLOCKED_PRECISION_MIN_NOTIONAL',
                        'UTBOT_FILTERED_BREAKOUT_V1 entry blocked after precision min notional check',
                        {
                            'qty_notional': qty_notional,
                            'min_notional': min_notional,
                            'qty': qty
                        }
                    )
                    await self.ctrl.notify(
                        f"⚠️ UTBOT_FILTERED_BREAKOUT_V1 수량 정밀도 후 최소 금액 미달"
                        f"({qty_notional:.2f} < {min_notional:.2f}). 리스크 기반 진입 차단."
                    )
                    self._clear_utbot_filtered_breakout_entry_plan(symbol)
                else:
                    await self.ctrl.notify(
                        f"⚠️ 주문 수량 정밀도 때문에 최소 금액 미달({qty_notional:.2f} < {min_notional:.2f}). "
                        "레버리지/진입비율을 높여주세요."
                    )
                return
            
            if float(qty) <= 0:
                logger.warning(f"Invalid quantity: {qty} (free={free}, risk={risk_pct}, lev={lev}, price={price}, target_notional={target_notional})")
                await self.ctrl.notify(f"⚠️ 주문 수량 계산 오류: {qty} (잔고: {free:.2f})")
                return
            
            if active_strategy != UTBOT_FILTERED_BREAKOUT_STRATEGY and bounded_risk_pct != req_risk_pct:
                await self.ctrl.notify(f"⚠️ 리스크 상한 적용: {req_risk_pct:.1f}% -> {bounded_risk_pct:.1f}%")
            if active_strategy == UTBOT_FILTERED_BREAKOUT_STRATEGY:
                logger.info(
                    f"[UTBOT_FILTERED_BREAKOUT_V1] Entry params: qty={qty}, lev={lev}x, "
                    f"risk_usdt={float(filtered_breakout_plan.get('risk_usdt', 0) or 0):.4f}, "
                    f"risk_distance={float(filtered_breakout_plan.get('risk_distance', 0) or 0):.4f}, "
                    f"target_notional={target_notional:.2f}, margin_to_use={margin_to_use:.2f}"
                )
            else:
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
            logger.info(f"??Entry order success: {order.get('id', 'N/A')}")
            
            # 吏꾩엯 ???ъ????뺤씤?섏뿬 ?뺥솗??吏꾩엯媛 ?뚯븙
            await asyncio.sleep(1)
            self.position_cache = None
            verify_pos = await self.get_server_position(symbol, use_cache=False)
            actual_entry_price = float(verify_pos['entryPrice']) if verify_pos else price
            await self.ctrl.notify(
                self._build_signal_entry_notice(
                    symbol,
                    side,
                    qty,
                    price,
                    actual_entry_price,
                    entry_plan=filtered_breakout_plan if active_strategy == UTBOT_FILTERED_BREAKOUT_STRATEGY else None,
                    leverage=lev,
                    target_notional=target_notional,
                    margin_to_use=margin_to_use
                )
            )
            
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

            elif active_strategy == UTBOT_FILTERED_BREAKOUT_STRATEGY:
                plan = filtered_breakout_plan or self._get_utbot_filtered_breakout_entry_plan(symbol, side)
                if not plan:
                    logger.warning("[UTBOT_FILTERED_BREAKOUT_V1] Missing risk plan after entry; TP/SL placement skipped.")
                    await self.ctrl.notify("⚠️ UTBOT_FILTERED_BREAKOUT_V1 리스크 계획 누락: 보호 주문 설정을 건너뜀")
                else:
                    risk_distance = float(plan.get('risk_distance', 0.0) or 0.0)
                    rr_multiple = float(plan.get('rr_multiple', 2.0) or 2.0)
                    if risk_distance > 0 and rr_multiple > 0:
                        await self._place_tp_sl_orders(
                            symbol,
                            side,
                            actual_entry_price,
                            qty,
                            tp_distance=risk_distance * rr_multiple,
                            sl_distance=risk_distance
                        )
                        logger.info(
                            f"[UTBOT_FILTERED_BREAKOUT_V1] RR protection set: "
                            f"entry={actual_entry_price:.4f}, risk={risk_distance:.4f}, rr={rr_multiple:.2f}"
                        )
                    else:
                        await self.ctrl.notify("⚠️ UTBOT_FILTERED_BREAKOUT_V1 보호 주문 거리 계산 오류")
                if plan:
                    try:
                        requested = float(price)
                        actual = float(actual_entry_price)
                        signed_slippage_pct = (actual - requested) / max(abs(requested), 1e-9) * 100.0
                        adverse_slippage_pct = (
                            signed_slippage_pct if side == 'long' else -signed_slippage_pct
                        )
                    except (TypeError, ValueError):
                        signed_slippage_pct = None
                        adverse_slippage_pct = None
                    self._record_utbreakout_diagnostic_event(
                        symbol,
                        {
                            'candidate_side': side,
                            'entry_timeframe': self._get_utbot_filtered_breakout_config(strategy_params).get('entry_timeframe', '15m'),
                            'htf_timeframe': self._get_utbot_filtered_breakout_config(strategy_params).get('htf_timeframe', '1h'),
                            'reason': 'UTBOT_FILTERED_BREAKOUT_V1 entry filled',
                            'entry_price': actual_entry_price,
                            'entry_plan': dict(plan)
                        },
                        event='entry_filled',
                        extra={
                            'code': 'ENTRY_FILLED',
                            'order_id': order.get('id') if isinstance(order, dict) else None,
                            'requested_price': price,
                            'actual_entry_price': actual_entry_price,
                            'entry_slippage_pct': adverse_slippage_pct,
                            'entry_signed_slippage_pct': signed_slippage_pct,
                            'target_notional': target_notional,
                            'planned_margin': margin_to_use,
                            'leverage': lev
                        }
                    )
                self._clear_utbot_filtered_breakout_entry_plan(symbol)
            
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
            if locals().get('active_strategy') == UTBOT_FILTERED_BREAKOUT_STRATEGY:
                self._clear_utbot_filtered_breakout_entry_plan(symbol)
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

    def _protection_order_info(self, order):
        return order.get('info', {}) if isinstance(order, dict) and isinstance(order.get('info', {}), dict) else {}

    def _protection_bool(self, value):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {'true', '1', 'yes', 'y'}

    def _is_reduce_only_order(self, order):
        if not isinstance(order, dict):
            return False
        info = self._protection_order_info(order)
        return any(
            self._protection_bool(value)
            for value in (
                order.get('reduceOnly'),
                order.get('reduce_only'),
                info.get('reduceOnly'),
                info.get('reduce_only'),
                info.get('closePosition')
            )
        )

    def _protection_order_type(self, order):
        if not isinstance(order, dict):
            return ''
        info = self._protection_order_info(order)
        return str(
            order.get('type')
            or info.get('type')
            or info.get('origType')
            or info.get('orderType')
            or ''
        ).strip().lower()

    def _protection_order_side(self, order):
        if not isinstance(order, dict):
            return ''
        info = self._protection_order_info(order)
        return str(order.get('side') or info.get('side') or '').strip().lower()

    def _classify_protection_order(self, order):
        order_type = self._protection_order_type(order)
        is_reduce_only = self._is_reduce_only_order(order)
        if 'take_profit' in order_type or 'take-profit' in order_type:
            return 'tp'
        if 'stop' in order_type:
            return 'sl'
        if is_reduce_only and ('limit' in order_type or order_type == ''):
            return 'tp'
        return None

    def _is_protection_order(self, order):
        return self._classify_protection_order(order) in {'tp', 'sl'}

    async def _fetch_open_orders_safe(self, symbol):
        try:
            return await asyncio.to_thread(self.exchange.fetch_open_orders, symbol)
        except Exception as e:
            logger.warning(f"Protection audit: fetch_open_orders failed for {symbol}: {e}")
            return None

    async def _cancel_protection_orders(self, symbol, reason='protection cleanup', orders=None):
        if self.is_upbit_mode():
            return 0
        open_orders = orders if orders is not None else await self._fetch_open_orders_safe(symbol)
        if open_orders is None:
            return 0
        cancelled = 0
        for order in open_orders or []:
            if not self._is_protection_order(order):
                continue
            order_id = order.get('id') or self._protection_order_info(order).get('orderId')
            if not order_id:
                continue
            try:
                await asyncio.to_thread(self.exchange.cancel_order, order_id, symbol)
                cancelled += 1
            except Exception as e:
                logger.warning(f"Protection cleanup cancel failed for {symbol} / {order_id}: {e}")
        if cancelled:
            logger.info(f"Protection cleanup: cancelled {cancelled} orders for {symbol} ({reason})")
        return cancelled

    async def _notify_protection_issue(self, symbol, kind, message, cooldown_sec=300):
        key = f"{symbol}:{kind}"
        now_ts = time.time()
        last_ts = float(self.last_protection_alert_ts.get(key, 0.0) or 0.0)
        if now_ts - last_ts < cooldown_sec:
            return
        self.last_protection_alert_ts[key] = now_ts
        try:
            await self.ctrl.notify(message)
        except Exception as e:
            logger.warning(f"Protection alert failed for {symbol}: {e}")

    def _protection_expected_from_config(self, symbol, pos):
        if self.is_upbit_mode() or not pos:
            return False, False
        strategy_params = self.get_runtime_strategy_params()
        active_strategy = str(strategy_params.get('active_strategy', '') or '').lower()
        if active_strategy == UTBOT_FILTERED_BREAKOUT_STRATEGY:
            return True, True
        cfg = self.get_runtime_common_settings()
        master = bool(cfg.get('tp_sl_enabled', True))
        return (
            master and bool(cfg.get('take_profit_enabled', True)),
            master and bool(cfg.get('stop_loss_enabled', True))
        )

    async def _audit_protection_orders(self, symbol, pos=None, expected_tp=None, expected_sl=None, alert=True):
        status = {
            'tp_expected': bool(expected_tp),
            'sl_expected': bool(expected_sl),
            'tp_present': False,
            'sl_present': False,
            'tp_count': 0,
            'sl_count': 0,
            'missing_tp': False,
            'missing_sl': False,
            'orphan_cancelled': 0,
            'mismatch_cancelled': 0,
            'status': 'SKIPPED'
        }
        if self.is_upbit_mode():
            self.last_protection_order_status[symbol] = status
            return status

        if expected_tp is None or expected_sl is None:
            expected_tp, expected_sl = self._protection_expected_from_config(symbol, pos)
            status['tp_expected'] = bool(expected_tp)
            status['sl_expected'] = bool(expected_sl)

        open_orders = await self._fetch_open_orders_safe(symbol)
        if open_orders is None:
            status['status'] = 'ORDER_FETCH_FAILED'
            self.last_protection_order_status[symbol] = status
            return status
        protection_orders = [order for order in (open_orders or []) if self._is_protection_order(order)]

        if not pos:
            status['status'] = 'NO_POSITION'
            if protection_orders:
                status['orphan_cancelled'] = await self._cancel_protection_orders(
                    symbol,
                    reason='no position remains',
                    orders=protection_orders
                )
                status['status'] = 'ORPHAN_CANCELLED'
                if alert and status['orphan_cancelled']:
                    await self._notify_protection_issue(
                        symbol,
                        'orphan_cancelled',
                        f"ℹ️ {self.ctrl.format_symbol_for_display(symbol)} 포지션 없음: 잔존 보호주문 {status['orphan_cancelled']}건 자동 취소"
                    )
            self.last_protection_order_status[symbol] = status
            return status

        pos_side = str(pos.get('side', '')).lower()
        close_side = 'sell' if pos_side == 'long' else 'buy'
        valid_tp = []
        valid_sl = []
        mismatched = []
        for order in protection_orders:
            kind = self._classify_protection_order(order)
            order_side = self._protection_order_side(order)
            if order_side and order_side != close_side:
                mismatched.append(order)
                continue
            if kind == 'tp':
                valid_tp.append(order)
            elif kind == 'sl':
                valid_sl.append(order)

        if mismatched:
            status['mismatch_cancelled'] = await self._cancel_protection_orders(
                symbol,
                reason='wrong close side',
                orders=mismatched
            )

        status['tp_count'] = len(valid_tp)
        status['sl_count'] = len(valid_sl)
        status['tp_present'] = len(valid_tp) > 0
        status['sl_present'] = len(valid_sl) > 0
        status['missing_tp'] = bool(expected_tp) and not status['tp_present']
        status['missing_sl'] = bool(expected_sl) and not status['sl_present']
        if status['missing_sl']:
            status['status'] = 'MISSING_SL'
            if alert:
                await self._notify_protection_issue(
                    symbol,
                    'missing_sl',
                    f"🚨 {self.ctrl.format_symbol_for_display(symbol)} 보호주문 누락: SL 없음. 포지션은 유지 중이니 거래소 주문을 확인하세요."
                )
        elif status['missing_tp']:
            status['status'] = 'MISSING_TP'
            if alert:
                await self._notify_protection_issue(
                    symbol,
                    'missing_tp',
                    f"⚠️ {self.ctrl.format_symbol_for_display(symbol)} 보호주문 누락: TP 없음"
                )
        elif status['mismatch_cancelled']:
            status['status'] = 'MISMATCH_CANCELLED'
        else:
            status['status'] = 'OK'
        self.last_protection_order_status[symbol] = status
        return status

    async def _create_protection_order_with_retries(
        self,
        symbol,
        order_type,
        side,
        qty,
        price,
        params,
        label,
        max_attempts=3
    ):
        last_error = None
        for attempt in range(1, max_attempts + 1):
            try:
                order = await asyncio.to_thread(
                    self.exchange.create_order,
                    symbol,
                    order_type,
                    side,
                    qty,
                    price,
                    params
                )
                if attempt > 1:
                    logger.info(f"{label} protection order succeeded on retry {attempt}: {symbol}")
                return order
            except Exception as e:
                last_error = e
                logger.error(f"{label} order attempt {attempt}/{max_attempts} failed for {symbol}: {e}")
                if attempt < max_attempts:
                    await asyncio.sleep(0.7)
        raise last_error

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
            side = str(side or '').lower()
            entry_price = float(entry_price or 0.0)
            if side not in {'long', 'short'} or entry_price <= 0:
                logger.error(f"Protection placement skipped: invalid side/entry ({symbol}, {side}, {entry_price})")
                return

            pos = await self.get_server_position(symbol, use_cache=False)
            if pos and str(pos.get('side', '')).lower() == side:
                pos_contracts = abs(float(pos.get('contracts', 0) or 0))
                if pos_contracts > 0:
                    qty = self.safe_amount(symbol, pos_contracts)
                pos_entry = float(pos.get('entryPrice') or 0.0)
                if pos_entry > 0:
                    entry_price = pos_entry
            qty = self.safe_amount(symbol, abs(float(qty or 0)))
            if float(qty) <= 0:
                logger.error(f"Protection placement skipped: invalid qty for {symbol}: {qty}")
                return

            await self._cancel_protection_orders(symbol, reason='before new protection placement')

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

            def _valid_price(direction, price_value):
                try:
                    price_float = float(price_value)
                except (TypeError, ValueError):
                    return False
                if direction == 'tp':
                    return price_float > entry_price if side == 'long' else price_float < entry_price
                return price_float < entry_price if side == 'long' else price_float > entry_price

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
                    return
                else:
                    try:
                        sl_order = await self._create_protection_order_with_retries(
                            symbol,
                            'stop_market',
                            sl_side,
                            qty,
                            None,
                            {'stopPrice': sl_price, 'reduceOnly': True},
                            'SL',
                            max_attempts=3
                        )
                        logger.info(f"SL order placed: {sl_side.upper()} @ {sl_price} (stop)")
                    except Exception as sl_e:
                        logger.error(f"SL order failed after retries: {sl_e}")
                        await self._cancel_protection_orders(symbol, reason='SL placement failed')
                        await self._notify_protection_issue(
                            symbol,
                            'sl_place_failed',
                            f"🚨 {self.ctrl.format_symbol_for_display(symbol)} SL 주문 생성 실패(3회 재시도). 포지션은 유지 중이니 거래소에서 수동 확인하세요: {sl_e}",
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
                        return

            # Take Profit 二쇰Ц (吏?뺢? + reduceOnly)
            if tp_price is not None:
                if not _valid_price('tp', tp_price):
                    await self._notify_protection_issue(
                        symbol,
                        'invalid_tp_price',
                        f"⚠️ {self.ctrl.format_symbol_for_display(symbol)} TP 가격 오류: entry {entry_price:.6f}, TP {tp_price}"
                    )
                    tp_price = None
                else:
                    try:
                        tp_order = await self._create_protection_order_with_retries(
                            symbol,
                            'limit',
                            tp_side,
                            qty,
                            tp_price,
                            {'reduceOnly': True},
                            'TP',
                            max_attempts=2
                        )
                        logger.info(f"TP order placed: {tp_side.upper()} @ {tp_price}")
                    except Exception as tp_e:
                        logger.error(f"TP order failed: {tp_e}")
                        await self._notify_protection_issue(
                            symbol,
                            'tp_place_failed',
                            f"⚠️ {self.ctrl.format_symbol_for_display(symbol)} TP 주문 생성 실패. SL은 유지됩니다: {tp_e}",
                            cooldown_sec=60
                        )
            
            notice_parts = []
            if tp_order and tp_price is not None:
                notice_parts.append(f"🎯 TP: `{float(tp_price):.2f}`")
            if sl_order and sl_price is not None:
                notice_parts.append(f"🛑 SL: `{float(sl_price):.2f}`")
            if notice_parts:
                await self.ctrl.notify(" | ".join(notice_parts))

            await self._audit_protection_orders(
                symbol,
                pos=await self.get_server_position(symbol, use_cache=False),
                expected_tp=tp_price is not None,
                expected_sl=sl_price is not None,
                alert=True
            )
            
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
            await self._cancel_protection_orders(symbol, reason='exit requested but no position')
            return
        
        contracts = abs(float(pos['contracts']))
        if contracts <= 0:
            logger.info("No contracts to exit")
            await self._cancel_protection_orders(symbol, reason='exit requested but zero contracts')
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
        await self._cancel_protection_orders(symbol, reason='after exit order success')

        active_strategy = str(self.get_runtime_strategy_params().get('active_strategy', '') or '').lower()
        if active_strategy == 'utsmc' or str(reason or '').startswith('UTSMC'):
            self._clear_utsmc_pending_entry(symbol)
            self._clear_utsmc_entry_invalidation(symbol)
            self._clear_utsmc_fixed_exit_ob(symbol)
            self.utsmc_last_entry_signal_ts.pop(symbol, None)
        
        self.db.log_trade_close(symbol, pnl, pnl_pct, exit_price, reason)
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
        
        market_data_mode = self._get_market_data_exchange_mode(self.exchange_mode)
        self.exchange = self._build_exchange(creds, self.exchange_mode)
        self._configure_exchange_network(self.exchange, self.exchange_mode)
        self.market_data_exchange = self._build_public_market_data_exchange(market_data_mode)
        self._configure_exchange_network(self.market_data_exchange, market_data_mode)
        self.market_data_source_label = self._get_market_data_source_label(self.exchange_mode)
        logger.info(f"Market data source configured: {self.market_data_source_label}")
        
        self.engines = {
            CORE_ENGINE: SignalEngine(self)
        }
        logger.info("Core mode enabled: Signal(SMA/HMA/CAMERON) + risk controls only. Legacy engines archived.")
        self.active_engine = None
        self.tg_app = None
        self.status_data = {}
        self.is_paused = False  # 기본: 재시작/수동 시작 모두 자동 실행 상태로 시작
        if self.prev_paused_state is not None:
            logger.info(
                f"Startup pause override applied: paused={self.is_paused} "
                f"(previous={self.prev_paused_state}, launch_reason={self.launch_reason})"
            )
        self.dashboard_msg_id = None
        self.dashboard_plain_text_mode = True
        self.last_status_snapshot_key = None
        self.blink_state = False
        self.last_hourly_report = 0
        self.last_alt_trend_scan_candle_ts_by_tf = {}
        self.last_alt_trend_alert_sent = {}
        self.last_alt_trend_scan_summary = {}

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

    def _get_market_data_exchange_mode(self, exchange_mode=None):
        mode = exchange_mode or self.get_exchange_mode()
        if mode == BINANCE_TESTNET:
            return BINANCE_MAINNET
        return mode

    def _get_market_data_source_label(self, exchange_mode=None):
        mode = self._get_market_data_exchange_mode(exchange_mode)
        labels = {
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
        elif str(strategy.get('active_strategy', 'utbot')).lower() not in CORE_STRATEGIES:
            strategy['active_strategy'] = 'utbot'
        return strategy

    def get_active_watchlist(self):
        watchlist = self.get_active_trade_config().get('watchlist', [])
        if not isinstance(watchlist, list) or not watchlist:
            return ['BTC/KRW'] if self.is_upbit_mode() else ['BTC/USDT']
        return list(watchlist)

    def _reset_signal_engine_runtime_state(self, *, reset_entry_cache=False, reset_exit_cache=False, reset_stateful_strategy=False):
        signal_engine = self.engines.get('signal')
        if not signal_engine:
            return None

        signal_engine.reset_signal_runtime_state(
            reset_entry_cache=reset_entry_cache,
            reset_exit_cache=reset_exit_cache,
            reset_stateful_strategy=reset_stateful_strategy
        )
        return signal_engine

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

    def _get_alt_trend_alert_settings(self):
        reporting = self.cfg.get('telegram', {}).get('reporting', {}) or {}
        timeframes = normalize_alt_trend_timeframes(
            reporting.get('alt_trend_alert_timeframes', ['1d'])
        ) or ['1d']
        periodic_enabled = bool(reporting.get('periodic_reports_enabled', False))
        return {
            'enabled': periodic_enabled and bool(reporting.get('alt_trend_alert_enabled', False)),
            'timeframes': timeframes,
            'scope': 'binance_futures_all',
            'stage_mode': 'setup_and_confirm',
            'profile': 'conservative',
            'oi_cvd_mode': 'required'
        }

    def _format_alt_trend_alert_item(self, item):
        symbol_label = self.format_symbol_for_display(item.get('symbol', ''))
        cvd_label = "▲" if float(item.get('cvd_delta', 0.0) or 0.0) > 0 else "▼"
        return (
            f"- `{symbol_label}` score `{float(item.get('score', 0.0) or 0.0):.1f}` | "
            f"RSI `{float(item.get('curr_rsi', 0.0) or 0.0):.1f}` | "
            f"Vol `x{float(item.get('volume_ratio', 0.0) or 0.0):.2f}` | "
            f"OI `{float(item.get('oi_delta_pct', 0.0) or 0.0):+.2f}%` | "
            f"CVD `{cvd_label}`"
        )

    def _build_alt_trend_alert_chunks(self, results_by_tf, scanned_timeframes=None):
        if not results_by_tf:
            return []

        scanned_tf_text = format_alt_trend_timeframes(scanned_timeframes or results_by_tf.keys())
        header_lines = [
            "🚀 **알트 급등 알림**",
            f"시각: `{datetime.now().strftime('%m-%d %H:%M:%S')}`",
            f"스캔 TF: `{scanned_tf_text}`"
        ]
        header = "\n".join(header_lines)
        sections = []

        for timeframe in sorted(results_by_tf.keys(), key=lambda tf: ALT_TREND_TIMEFRAME_ORDER.get(tf, 999)):
            tf_result = results_by_tf.get(timeframe, {}) or {}
            confirms = tf_result.get('confirm', []) or []
            setups = tf_result.get('setup', []) or []
            if not confirms and not setups:
                continue

            section_lines = [f"**{timeframe}**"]
            if confirms:
                section_lines.append("🟢 확정")
                section_lines.extend(self._format_alt_trend_alert_item(item) for item in confirms)
            if setups:
                section_lines.append("🟡 준비")
                section_lines.extend(self._format_alt_trend_alert_item(item) for item in setups)
            sections.append("\n".join(section_lines))

        if not sections:
            return []

        chunks = []
        current = header
        for section in sections:
            candidate = f"{current}\n\n{section}".strip()
            if len(candidate) > 3800 and current != header:
                chunks.append(current.strip())
                current = f"{header}\n\n{section}".strip()
            else:
                current = candidate
        if current:
            chunks.append(current.strip())

        if len(chunks) <= 1:
            return chunks

        numbered_chunks = []
        total = len(chunks)
        for idx, chunk in enumerate(chunks, start=1):
            numbered_chunks.append(chunk.replace("🚀 **알트 급등 알림**", f"🚀 **알트 급등 알림** ({idx}/{total})", 1))
        return numbered_chunks

    def _build_binance_futures_rest_symbol(self, symbol):
        text = str(symbol or '').strip().upper()
        if not text:
            return ''
        if '/' in text:
            base, quote = text.split('/', 1)
            quote = quote.split(':', 1)[0]
            return f"{base}{quote}"
        return text.replace(':', '')

    def _fetch_binance_public_json_sync(self, path, params):
        query = urllib.parse.urlencode(params)
        url = f"{BINANCE_FAPI_PUBLIC_BASE_URL}{path}?{query}"
        request = urllib.request.Request(
            url,
            headers={
                'User-Agent': 'Mozilla/5.0',
                'Accept': 'application/json'
            }
        )
        with urllib.request.urlopen(request, timeout=12) as response:
            payload = response.read().decode('utf-8')
        return json.loads(payload)

    async def _fetch_binance_public_json(self, path, params):
        return await asyncio.to_thread(self._fetch_binance_public_json_sync, path, params)

    async def _fetch_utbreakout_futures_context(self, symbol):
        """Fetch lightweight futures context for research logs only.

        This data is intentionally not used as an entry filter. It helps later
        backtest/forward-test review decide whether funding/open-interest
        features are worth promoting into optional strategy inputs.
        """
        if self.is_upbit_mode():
            return {}
        now = time.time()
        cached = self.utbreakout_futures_context_cache.get(symbol)
        if isinstance(cached, dict) and (now - float(cached.get('cached_at', 0) or 0)) < 300:
            return dict(cached.get('data') or {})

        rest_symbol = self._build_binance_futures_rest_symbol(symbol)
        if not rest_symbol:
            return {}

        context = {}
        try:
            premium = await self._fetch_binance_public_json(
                '/fapi/v1/premiumIndex',
                {'symbol': rest_symbol}
            )
            if isinstance(premium, dict):
                context.update({
                    'funding_rate': _safe_float_or_none(premium.get('lastFundingRate')),
                    'next_funding_time': int(premium.get('nextFundingTime') or 0) or None,
                    'mark_price': _safe_float_or_none(premium.get('markPrice')),
                    'index_price': _safe_float_or_none(premium.get('indexPrice')),
                })
        except Exception as e:
            context['futures_context_error'] = f"premiumIndex: {e}"

        try:
            oi = await self._fetch_binance_public_json(
                '/fapi/v1/openInterest',
                {'symbol': rest_symbol}
            )
            if isinstance(oi, dict):
                context['open_interest'] = _safe_float_or_none(oi.get('openInterest'))
                context['open_interest_ts'] = int(oi.get('time') or 0) or None
        except Exception as e:
            if 'futures_context_error' not in context:
                context['futures_context_error'] = f"openInterest: {e}"

        clean_context = {k: v for k, v in context.items() if v is not None}
        self.utbreakout_futures_context_cache[symbol] = {
            'cached_at': now,
            'data': clean_context
        }
        return clean_context

    async def _fetch_alt_trend_oi_cvd_metrics(self, symbol, timeframe):
        rest_symbol = self._build_binance_futures_rest_symbol(symbol)
        if not rest_symbol:
            return None

        try:
            if timeframe in {'5m', '15m', '30m'}:
                oi_rows = await self._fetch_binance_public_json(
                    '/futures/data/openInterestHist',
                    {'symbol': rest_symbol, 'period': timeframe, 'limit': 2}
                )
                cvd_rows = await self._fetch_binance_public_json(
                    '/futures/data/takerlongshortRatio',
                    {'symbol': rest_symbol, 'period': timeframe, 'limit': 2}
                )
                if not isinstance(oi_rows, list) or len(oi_rows) < 2:
                    return None
                if not isinstance(cvd_rows, list) or len(cvd_rows) < 1:
                    return None

                oi_rows = sorted(oi_rows, key=lambda row: int(row.get('timestamp', 0) or 0))
                cvd_rows = sorted(cvd_rows, key=lambda row: int(row.get('timestamp', 0) or 0))
                start_row = oi_rows[-2]
                end_row = oi_rows[-1]
                cvd_window = cvd_rows[-1:]
            else:
                bars_by_timeframe = {
                    '1h': 1,
                    '2h': 2,
                    '4h': 4,
                    '6h': 6,
                    '8h': 8,
                    '1d': 24
                }
                bars = bars_by_timeframe.get(timeframe)
                if not bars:
                    return None

                oi_rows = await self._fetch_binance_public_json(
                    '/futures/data/openInterestHist',
                    {'symbol': rest_symbol, 'period': '1h', 'limit': bars + 1}
                )
                cvd_rows = await self._fetch_binance_public_json(
                    '/futures/data/takerlongshortRatio',
                    {'symbol': rest_symbol, 'period': '1h', 'limit': bars}
                )
                if not isinstance(oi_rows, list) or len(oi_rows) < (bars + 1):
                    return None
                if not isinstance(cvd_rows, list) or len(cvd_rows) < bars:
                    return None

                oi_rows = sorted(oi_rows, key=lambda row: int(row.get('timestamp', 0) or 0))
                cvd_rows = sorted(cvd_rows, key=lambda row: int(row.get('timestamp', 0) or 0))
                oi_window = oi_rows[-(bars + 1):]
                cvd_window = cvd_rows[-bars:]
                start_row = oi_window[0]
                end_row = oi_window[-1]

            start_oi = float(
                start_row.get('sumOpenInterestValue')
                or start_row.get('sumOpenInterest')
                or 0.0
            )
            end_oi = float(
                end_row.get('sumOpenInterestValue')
                or end_row.get('sumOpenInterest')
                or 0.0
            )
            oi_delta_pct = ((end_oi - start_oi) / start_oi * 100.0) if start_oi > 0 else 0.0
            cvd_delta = 0.0
            latest_ts = int(end_row.get('timestamp', 0) or 0)
            for row in cvd_window:
                buy_vol = float(row.get('buyVol') or 0.0)
                sell_vol = float(row.get('sellVol') or 0.0)
                cvd_delta += (buy_vol - sell_vol)
                latest_ts = max(latest_ts, int(row.get('timestamp', 0) or 0))

            return {
                'oi_delta_pct': float(oi_delta_pct),
                'cvd_delta': float(cvd_delta),
                'oi_cvd_pass': bool(oi_delta_pct > 0 and cvd_delta > 0),
                'oi_cvd_ts': latest_ts
            }
        except Exception as e:
            logger.warning(f"Alt trend OI/CVD fetch failed for {symbol} {timeframe}: {e}")
            return None

    async def _get_last_closed_candle_ts_for_alert(self, timeframe):
        try:
            ohlcv = await asyncio.to_thread(
                self.market_data_exchange.fetch_ohlcv,
                'BTC/USDT:USDT',
                timeframe,
                limit=3
            )
            if not ohlcv or len(ohlcv) < 2:
                return 0
            return int(ohlcv[-2][0] or 0)
        except Exception as e:
            logger.warning(f"Alt trend candle sync failed for {timeframe}: {e}")
            return 0

    async def _scan_alt_trend_timeframe(self, timeframe, expected_candle_ts, ranked_symbols):
        signal_engine = self.engines.get('signal')
        if not signal_engine:
            return {'confirm': [], 'setup': []}

        strategy_params = self.get_active_strategy_params()
        price_semaphore = asyncio.Semaphore(8)
        oi_cvd_semaphore = asyncio.Semaphore(4)

        async def _evaluate_price(symbol_row):
            symbol = symbol_row.get('symbol')
            quote_volume = float(symbol_row.get('quote_volume', 0.0) or 0.0)
            try:
                async with price_semaphore:
                    ohlcv = await asyncio.to_thread(
                        self.market_data_exchange.fetch_ohlcv,
                        symbol,
                        timeframe,
                        limit=300
                    )
                if not ohlcv or len(ohlcv) < 60:
                    return None

                last_closed_ts = int(ohlcv[-2][0] or 0) if len(ohlcv) >= 2 else 0
                if expected_candle_ts and last_closed_ts != expected_candle_ts:
                    return None

                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                result = signal_engine._calculate_alt_trend_price_signal(symbol, df, strategy_params)
                if not result:
                    return None
                if expected_candle_ts and int(result.get('candle_ts', 0) or 0) != expected_candle_ts:
                    return None
                result['quote_volume'] = quote_volume
                result['timeframe'] = timeframe
                return result
            except Exception as e:
                logger.warning(f"Alt trend price scan failed for {symbol} {timeframe}: {e}")
                return None

        price_tasks = [_evaluate_price(symbol_row) for symbol_row in ranked_symbols]
        price_candidates = [item for item in await asyncio.gather(*price_tasks) if item]
        price_candidates.sort(
            key=lambda item: (1 if item.get('stage') == 'confirm' else 0, float(item.get('score', 0.0) or 0.0)),
            reverse=True
        )
        shortlist = price_candidates[:12]
        if not shortlist:
            return {'confirm': [], 'setup': []}

        async def _attach_oi_cvd(item):
            try:
                async with oi_cvd_semaphore:
                    metrics = await self._fetch_alt_trend_oi_cvd_metrics(item.get('symbol'), timeframe)
                if not metrics or not metrics.get('oi_cvd_pass', False):
                    return None
                enriched = dict(item)
                enriched.update(metrics)
                return enriched
            except Exception as e:
                logger.warning(f"Alt trend OI/CVD attach failed for {item.get('symbol')} {timeframe}: {e}")
                return None

        final_candidates = [item for item in await asyncio.gather(*[_attach_oi_cvd(item) for item in shortlist]) if item]
        final_candidates.sort(
            key=lambda item: (1 if item.get('stage') == 'confirm' else 0, float(item.get('score', 0.0) or 0.0)),
            reverse=True
        )

        confirms = []
        setups = []
        for item in final_candidates:
            key = (timeframe, item.get('symbol'), item.get('stage'))
            candle_ts = int(item.get('candle_ts', 0) or 0)
            if self.last_alt_trend_alert_sent.get(key) == candle_ts:
                continue
            if item.get('stage') == 'confirm':
                confirms.append(item)
            else:
                setups.append(item)

        confirms = confirms[:5]
        remaining_slots = max(0, 5 - len(confirms))
        setups = setups[:remaining_slots]

        return {
            'confirm': confirms,
            'setup': setups
        }

    async def _alt_trend_alert_loop(self):
        await asyncio.sleep(20)
        while True:
            try:
                settings = self._get_alt_trend_alert_settings()
                if self.is_upbit_mode() or not settings.get('enabled', False):
                    await asyncio.sleep(60)
                    continue

                due_timeframes = {}
                for timeframe in settings.get('timeframes', []):
                    closed_ts = await self._get_last_closed_candle_ts_for_alert(timeframe)
                    if closed_ts <= 0:
                        continue
                    if int(self.last_alt_trend_scan_candle_ts_by_tf.get(timeframe, 0) or 0) >= closed_ts:
                        continue
                    due_timeframes[timeframe] = closed_ts

                if not due_timeframes:
                    await asyncio.sleep(60)
                    continue

                try:
                    tickers = await asyncio.to_thread(self.market_data_exchange.fetch_tickers)
                except Exception as e:
                    logger.warning(f"Alt trend ticker scan failed: {e}")
                    await asyncio.sleep(60)
                    continue

                ranked_symbols = []
                for symbol, data in (tickers or {}).items():
                    if '/USDT:USDT' not in symbol:
                        continue
                    quote_volume = float(data.get('quoteVolume', 0.0) or 0.0)
                    if quote_volume < 15_000_000:
                        continue
                    ranked_symbols.append({
                        'symbol': symbol,
                        'quote_volume': quote_volume
                    })
                ranked_symbols.sort(key=lambda item: item.get('quote_volume', 0.0), reverse=True)
                ranked_symbols = ranked_symbols[:60]

                results_by_tf = {}
                summary = {}
                for timeframe in sorted(due_timeframes.keys(), key=lambda tf: ALT_TREND_TIMEFRAME_ORDER.get(tf, 999)):
                    closed_ts = due_timeframes[timeframe]
                    tf_result = await self._scan_alt_trend_timeframe(timeframe, closed_ts, ranked_symbols)
                    confirm_count = len(tf_result.get('confirm', []) or [])
                    setup_count = len(tf_result.get('setup', []) or [])
                    summary[timeframe] = {
                        'candle_ts': closed_ts,
                        'confirm_count': confirm_count,
                        'setup_count': setup_count,
                        'scanned_symbols': len(ranked_symbols),
                        'scanned_at': int(time.time())
                    }
                    self.last_alt_trend_scan_candle_ts_by_tf[timeframe] = closed_ts
                    if confirm_count or setup_count:
                        results_by_tf[timeframe] = tf_result

                self.last_alt_trend_scan_summary = summary

                if results_by_tf:
                    for chunk in self._build_alt_trend_alert_chunks(results_by_tf, due_timeframes.keys()):
                        await self.notify(chunk)
                    for timeframe, tf_result in results_by_tf.items():
                        for stage in ('confirm', 'setup'):
                            for item in tf_result.get(stage, []) or []:
                                self.last_alt_trend_alert_sent[(timeframe, item.get('symbol'), stage)] = int(item.get('candle_ts', 0) or 0)

                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"Alt trend alert loop error: {e}")
                await asyncio.sleep(60)

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
            self._hourly_report_loop(),
            self._alt_trend_alert_loop(),
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
            market_data_mode = self._get_market_data_exchange_mode(exchange_mode)
            self.exchange = self._build_exchange(creds, exchange_mode)
            network_name = self._configure_exchange_network(self.exchange, exchange_mode)
            self.market_data_exchange = self._build_public_market_data_exchange(market_data_mode)
            self._configure_exchange_network(self.market_data_exchange, market_data_mode)
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
        
        return None

    async def _reply_markdown_safe(self, message, text, reply_markup=None):
        if message is None:
            return
        try:
            await message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
        except BadRequest as md_err:
            if "can't parse entities" in str(md_err).lower():
                plain_text = str(text).replace("**", "").replace("`", "")
                await message.reply_text(plain_text, reply_markup=reply_markup)
            else:
                raise

    def _extract_telegram_command(self, text):
        normalized = str(text or "").strip()
        if not normalized.startswith('/'):
            return None
        first_token = normalized.split(None, 1)[0]
        return first_token.split('@', 1)[0].lower()

    def _build_manual_status_payload(self):
        all_data = self.status_data if isinstance(self.status_data, dict) else {}
        return self._build_dashboard_messages(
            all_data,
            "●",
            " [PAUSED]" if self.is_paused else ""
        )

    def _get_utbot_filter_pack(self, strategy_params=None):
        params = strategy_params
        if not isinstance(params, dict):
            params = self.cfg.get('signal_engine', {}).get('strategy_params', {})
        utbot_cfg = params.get('UTBot', {}) if isinstance(params, dict) else {}
        return normalize_utbot_filter_pack_config(utbot_cfg.get('filter_pack', {}))

    def _format_utbot_filter_pack_selected(self, selected):
        return format_utbot_filter_pack_selected(selected)

    def _format_utbot_filter_pack_logic(self, logic):
        return format_utbot_filter_pack_logic(logic)

    def _format_utbot_filter_pack_mode_map(self, mode_by_filter, selected=None):
        return format_utbot_filter_pack_mode_map(mode_by_filter, selected)

    def _get_utbot_filter_label(self, filter_id):
        return get_utbot_filter_pack_label(filter_id)

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

                tp_distance = (entry_price * tp_pct) if (tp_enabled and tp_pct > 0) else None
                sl_distance = (entry_price * sl_pct) if (sl_enabled and sl_pct > 0) else None
                if tp_distance is not None or sl_distance is not None:
                    await self._place_tp_sl_orders(
                        symbol,
                        side,
                        entry_price,
                        qty,
                        tp_distance=tp_distance,
                        sl_distance=sl_distance
                    )
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
        request_text = ""
        if update and update.message and update.message.text:
            request_text = update.message.text.strip()
        logger.info(
            f"Telegram setup menu rendering: chat_id={update.effective_chat.id if update and update.effective_chat else 'unknown'} "
            f"text={request_text!r} engine={eng} exchange={self.get_exchange_mode()}"
        )
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
            reporting_cfg = self.cfg.get('telegram', {}).get('reporting', {})
            hourly_report_status = "ON" if (
                reporting_cfg.get('periodic_reports_enabled', False)
                and reporting_cfg.get('hourly_report_enabled', False)
            ) else "OFF"

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
            await self._reply_markdown_safe(update.message, msg.strip())
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
        active_strategy = strategy_params.get('active_strategy', 'utbot').upper()
        entry_mode = active_strategy
        ut_entry_timing_mode = str(strategy_params.get('ut_entry_timing_mode', 'next_candle') or 'next_candle').lower()
        ut_entry_timing_label = "다음봉 진입" if ut_entry_timing_mode == 'next_candle' else "신호유지 진입"
        utbot_params = strategy_params.get('UTBot', {})
        utbot_filter_pack = self._get_utbot_filter_pack(strategy_params)
        utbot_entry_filters_text = self._format_utbot_filter_pack_selected(
            utbot_filter_pack.get('entry', {}).get('selected', [])
        )
        utbot_entry_logic_text = self._format_utbot_filter_pack_logic(
            utbot_filter_pack.get('entry', {}).get('logic', 'and')
        )
        utbot_exit_filters_text = self._format_utbot_filter_pack_selected(
            utbot_filter_pack.get('exit', {}).get('selected', [])
        )
        utbot_exit_logic_text = self._format_utbot_filter_pack_logic(
            utbot_filter_pack.get('exit', {}).get('logic', 'and')
        )
        utbot_exit_mode_text = self._format_utbot_filter_pack_mode_map(
            utbot_filter_pack.get('exit', {}).get('mode_by_filter', {}),
            utbot_filter_pack.get('exit', {}).get('selected', [])
        )
        utbot_key = float(utbot_params.get('key_value', 1.0) or 1.0)
        utbot_atr = int(utbot_params.get('atr_period', 10) or 10)
        utbot_ha = "ON" if utbot_params.get('use_heikin_ashi', False) else "OFF"
        utbot_rsi_momentum_enabled = "ON" if bool(utbot_params.get('rsi_momentum_filter_enabled', False)) else "OFF"
        utsmc_cfg = strategy_params.get('UTSMC', {}) or {}
        utsmc_filter_cfg = utsmc_cfg.get('candidate_filter', {})
        utsmc_filter_mode = self._format_utsmc_candidate_filter_mode(utsmc_filter_cfg.get('mode', 'off'))
        utsmc_c2_exit_status = "ON" if bool(utsmc_cfg.get('exit_candidate2_enabled', False)) else "OFF"
        rsibb_params = strategy_params.get('RSIBB', {})
        rsibb_rsi = int(rsibb_params.get('rsi_length', 6) or 6)
        rsibb_bb = int(rsibb_params.get('bb_length', 200) or 200)
        rsibb_mult = float(rsibb_params.get('bb_mult', 2.0) or 2.0)
        rsi_momentum_params = strategy_params.get('RSIMomentumTrend', {}) or {}
        rsi_momentum_rsi = int(rsi_momentum_params.get('rsi_length', 14) or 14)
        rsi_momentum_pos = float(rsi_momentum_params.get('positive_above', 65) or 65)
        rsi_momentum_neg = float(rsi_momentum_params.get('negative_below', 32) or 32)
        rsi_momentum_ema = int(rsi_momentum_params.get('ema_period', 5) or 5)
        watchlist_preview = ", ".join(watchlist[:4]) if isinstance(watchlist, list) and watchlist else symbol
        if isinstance(watchlist, list) and len(watchlist) > 4:
            watchlist_preview += " ..."
        
        # Scanner ?곹깭
        scanner_enabled = sig_common.get('scanner_enabled', True)
        scanner_status = "ON 📡" if scanner_enabled else "OFF"
        scanner_tf = sig_common.get('scanner_timeframe', '15m')
        scanner_exit_tf = sig_common.get('scanner_exit_timeframe', '1h')

        # Hourly Report Status
        reporting_cfg = self.cfg.get('telegram', {}).get('reporting', {})
        hourly_report_status = "ON" if (
            reporting_cfg.get('periodic_reports_enabled', False)
            and reporting_cfg.get('hourly_report_enabled', False)
        ) else "OFF"
        alt_trend_settings = self._get_alt_trend_alert_settings()
        alt_trend_alert_status = "ON 🔔" if alt_trend_settings.get('enabled', False) else "OFF"
        alt_trend_tf_text = format_alt_trend_timeframes(alt_trend_settings.get('timeframes', []))

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
19. UT Bot (`K={utbot_key:.2f}` / `ATR={utbot_atr}` / `HA={utbot_ha}`)
20. RSI+BB 보조설정 (`RSI={rsibb_rsi}` / `BB={rsibb_bb}` / `x{rsibb_mult:.2f}`)
57. RSI Momentum Trend (`RSI={rsi_momentum_rsi}` / `P>{rsi_momentum_pos:.0f}` / `N<{rsi_momentum_neg:.0f}` / `EMA={rsi_momentum_ema}`)
58. UTBot + RSI Momentum (`{utbot_rsi_momentum_enabled}`)
21. UT 전략 안내 (`UTBOT / UTRSI / UTBB / UTRSIBB / UTSMC`)
51. UTBot 진입 필터 (`{utbot_entry_filters_text}`)
52. UTBot 진입 결합모드 (`{utbot_entry_logic_text}`)
53. UTBot 청산 필터 (`{utbot_exit_filters_text}`)
54. UTBot 청산 결합모드 (`{utbot_exit_logic_text}`)
55. UTBot 청산 필터 타입 (`{utbot_exit_mode_text}`)
56. UTBot 필터 안내
26. UT 진입 방식 (`{ut_entry_timing_label}`)
49. UTSMC 후보 필터 (`{utsmc_filter_mode}`)
50. UTSMC C2 청산 (`{utsmc_c2_exit_status}`)
13. TP/SL 자동청산 (`{tp_sl_status}`)
36. ROE 자동청산 (`{tp_status}`)
37. 손절 자동청산 (`{sl_status}`)
23. 거래량 급등 채굴 (`{scanner_status}` / `{scanner_tf}`)
24. 채굴 진입 TF
25. 채굴 청산 TF (`{scanner_exit_tf}`)
59. 알트 상승추세 알림 (`{alt_trend_alert_status}`)
60. 알트 상승추세 알림 TF (`{alt_trend_tf_text}`)
61. 알트 상승추세 알림 안내

**기타**
22. 거래소/네트워크 전환 (`{network_status}`)
42. 시간별 리포트 (`{hourly_report_status}`)
00. 엔진 교체 (현재: `{eng.upper()}`)
9. 매매 시작/중지 (`{status}`)
0. 나가기
"""
        await self._reply_markdown_safe(update.message, msg.strip())

    async def setup_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        request_text = ""
        if update and update.message and update.message.text:
            request_text = update.message.text.strip()
        chat_id = update.effective_chat.id if update and update.effective_chat else 'unknown'
        logger.info(f"Telegram setup menu requested: chat_id={chat_id} text={request_text!r}")
        try:
            await self.show_setup_menu(update)
            return SELECT
        except Exception as e:
            logger.exception(f"Telegram setup menu render failed: chat_id={chat_id} text={request_text!r}")
            if update and update.message:
                await update.message.reply_text(f"❌ 설정 메뉴 표시 실패: {e}")
            return ConversationHandler.END

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
            '11': "📝 **자산 비율(%)** 입력 (예: 50)",
            '12': "📝 **Grid 설정** 입력 (예: on,200 또는 off)",
            '14': "📝 **N Days** 입력 (예: 4)",
            '15': "📝 **K1/K2** 입력 (예: 0.5,0.5)",
            '16': "📝 **전략 선택** (1=UTBOT, 2=UTRSIBB, 3=UTRSI, 4=UTBB, 5=UTSMC)",
            '19': "📝 **UT Bot 설정** 입력 (형식: key,atr,on/off 예: 1,10,off)",
            '20': "RSI/BB 보조설정 입력\n형식: RSI길이,BB길이,BB배수\n예: 6,200,2",
            '21': "ℹ️ **UT 전략 안내**: UTRSI/UTRSIBB는 조합형, UTBB는 비대칭 롱/숏 규칙, UTSMC는 26번 UT 진입 방식 + internal OB/UT 신호봉 무효화 청산을 사용합니다.\n- 57번: RSI Momentum Trend 파라미터 설정\n- 58번: UTBot에 RSI Momentum Trend 보조필터 ON/OFF\n- 49번: UTSMC 진입용 후보 필터(C1/C2)\n- 50번: 필요 시 exit TF 기준 반대 방향 C2를 보조 청산으로 OR 추가",
            '51': "📝 **UTBot 진입 필터** 입력\n`0/off` = OFF\n또는 `1,4,5` 형식으로 번호 조합 입력\n1=CHOP, 2=ADX+DMI, 3=VWAP, 4=HTF Supertrend, 5=BOS/CHoCH",
            '52': "📝 **UTBot 진입 결합모드** 입력\n`and` 또는 `or`",
            '53': "📝 **UTBot 청산 필터** 입력\n`0/off` = OFF\n또는 `2,3` 형식으로 번호 조합 입력\n1=CHOP, 2=ADX+DMI, 3=VWAP, 4=HTF Supertrend, 5=BOS/CHoCH",
            '54': "📝 **UTBot 청산 결합모드** 입력\n`and` 또는 `or`",
            '55': "📝 **UTBot 청산 필터 타입** 입력\n형식: `2:c,3:s,5:c`\n`c=confirm`, `s=signal`",
            '56': "ℹ️ **UTBot 필터 안내**: 1=CHOP, 2=ADX+DMI, 3=VWAP, 4=HTF Supertrend, 5=BOS/CHoCH\n- 진입: 선택한 필터를 52번 AND/OR로 결합\n- 청산: 55번에서 각 필터를 `confirm`(기본 UT 반대신호 확인용) 또는 `signal`(단독 청산 트리거)로 지정\n- 58번 RSI Momentum Trend는 일반 필터팩과 별도로, UTBot 진입/청산을 직접 확인하는 전용 보조필터\n- 예시: 진입 `1,4,5` / 청산 `2,3` / 타입 `2:c,3:s`",
            '57': "📝 **RSI Momentum Trend 설정** 입력\n형식: RSI길이,PositiveAbove,NegativeBelow,EMA길이\n예: 14,65,32,5",
            '58': "📝 **UTBot + RSI Momentum Trend 보조필터** 입력\n`on/off` 또는 `1/0` (`true/false`, `yes/no` 지원)\n- on: UTBot 진입은 둘 다 같은 방향일 때만, 청산/반전은 둘 다 반대 방향일 때만 실행\n- off: 순수 UTBot만 사용",
            '59': "📝 **알트 상승추세 알림** 입력\n`on/off` 또는 `1/0` (`true/false`, `yes/no` 지원)",
            '60': "📝 **알트 상승추세 알림 TF** 입력\n쉼표로 여러 개 선택 가능\n예: `5m,15m,30m,1h,2h,4h,6h,8h,1d`\n허용 TF: 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 1d",
            '61': "ℹ️ **알트 상승추세 알림 안내**\n- 선택한 시간프레임만 스캔합니다.\n- 여러 TF를 선택하면 텔레그램 한 메시지 안에 TF별 섹션으로 묶어 보냅니다.\n- 준비(setup)와 확정(confirm)을 함께 보여줍니다.\n- Binance OI/CVD가 둘 다 양수일 때만 최종 알림에 포함됩니다.",
            '26': "📝 **UT 진입 방식** 입력\n`next` 또는 `1` = 시그널 마감봉 바로 다음봉에만 진입\n`persistent` 또는 `2` = fresh signal이 아니어도 현재 유지 중인 시그널이면 즉시 진입 대기/진입 (기준봉은 마지막 시그널 확정봉)",
            '49': "📝 **UTSMC 후보 필터** 입력\n`0/off` = OFF\n`1/c1` = 후보1 (Squeeze)\n`2/c2` = 후보2 (Breakout)\n`3/c12` = 후보1+2",
            '50': "📝 **UTSMC C2 청산** 입력\n`on/off` 또는 `1/0` (`true/false`, `yes/no` 지원)",
            '22': "📝 **거래소/네트워크 선택** (1=바이낸스 테스트넷, 2=바이낸스 메인넷, 3=업비트 KRW 현물)",
            '23': "📝 **거래량 급등 채굴 기능** (1=ON, 0=OFF)",
            '24': "📝 **채굴 진입 타임프레임** 입력 (예: 5m)\n1m, 5m, 15m, 30m, 1h",
            '25': "📝 **채굴 청산 타임프레임** 입력 (예: 1h)\n1m, 5m, 15m, 30m, 1h, 4h",
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
                '16', '19', '20', '21', '23', '24', '25', '26', '35', '36', '37', '38', '41', '49', '50', '51', '52', '53', '54', '55', '56', '57', '58', '59', '60', '61', '00'
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
        elif text == '56':
            await update.message.reply_text(prompts['56'], parse_mode=ParseMode.MARKDOWN)
            await self.show_setup_menu(update)
            return SELECT
        elif text == '61':
            await update.message.reply_text(prompts['61'], parse_mode=ParseMode.MARKDOWN)
            await self.show_setup_menu(update)
            return SELECT

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
            reporting = self.cfg.get('telegram', {}).get('reporting', {})
            curr = bool(
                reporting.get('periodic_reports_enabled', False)
                and reporting.get('hourly_report_enabled', False)
            )
            new_val = not curr
            await self.cfg.update_value(['telegram', 'reporting', 'hourly_report_enabled'], new_val)
            await self.cfg.update_value(
                ['telegram', 'reporting', 'periodic_reports_enabled'],
                bool(new_val or reporting.get('alt_trend_alert_enabled', False))
            )
            status = "ON" if new_val else "OFF"
            await update.message.reply_text(f"⚙️ 시간별 리포트: {status}")
            await self.show_setup_menu(update)
            return SELECT
        elif text in ('28', '29'):
            await update.message.reply_text("ℹ️ Hurst 필터는 코드에서 제거되었습니다.")
            return SELECT
            
        elif text == '21':
            await update.message.reply_text(
                "ℹ️ UT Hybrid 전략 안내\n"
                "- 26번 UT 진입 방식: `다음봉 진입`은 시그널 마감 직후 다음 진행봉에서만 진입, `신호유지 진입`은 fresh signal이 아니어도 현재 유지 중인 시그널이면 즉시 진입 대기/진입하고 기준봉은 마지막 시그널 확정봉\n"
                "- UTRSI: UT 방향 + RSI 타이밍 조합\n"
                "- UTRSIBB: UT 방향 + RSI/BB 동시 타이밍 조합\n"
                "- 57번 RSI Momentum Trend: Positive/Negative 상태 계산 파라미터 설정\n"
                "- 58번 UTBot + RSI Momentum: UTBot 진입은 RSI Momentum Trend와 같은 방향일 때만, 청산/반전은 둘 다 반대 방향일 때만 실행\n"
                "- UTBB LONG: UT 롱신호와 BB 롱신호가 순서 무관 3봉 내 일치하면 진입, 중간에 UT 숏 또는 BB 숏이 나오면 저장 무효\n"
                "- UTBB LONG 청산: 일반롱은 UT 숏상태 또는 BB 상단 하향 돌파 또는 중간선 위 음봉 2개, 특수롱은 BB 상단 하향 돌파 또는 UT 숏상태\n"
                "- UTBB 특수 LONG: UT 롱상태에서 BB 상단 상향 돌파가 나오면 진입\n"
                "- UTBB SHORT: BB 상단 재진입 셋업 후 UT 숏신호, BB 중간선 하락 돌파 + UT 숏상태, 또는 중간선 아래 반등 실패 + UT 숏상태로 진입\n"
                "- UTBB 특수 SHORT(하단돌파형): 진입봉이 BB 중간선 하향 + BB 하단선 하향 돌파면 BB 하단선 상향 돌파 또는 UT 롱상태로 청산\n"
                "- UTBB 특수 SHORT(반등실패형): 중간선 아래 양봉 1~2개 뒤 저점 이탈 음봉이면 진입, BB 중간선 상향 돌파 또는 UT 롱상태로 청산\n"
                "- UTBB SHORT 청산: 일반은 UT 롱상태 또는 BB 하단선 하향 돌파\n"
                "- UTSMC: 선택한 진입 방식대로 진입, `신호유지 진입`이면 현재 유지 중인 UT 상태에서도 바로 진입 가능하며 기준봉은 마지막 UT 시그널 확정봉, 청산은 exit TF internal OB 또는 UT 신호봉 무효화 마감 기준\n"
                "- UTSMC LONG: UT 롱신호 확정 후 LONG 진입, internal bearish OB 진입 마감 또는 UT 신호봉 저가 이탈 마감 청산\n"
                "- UTSMC SHORT: UT 숏신호 확정 후 SHORT 진입, internal bullish OB 진입 마감 또는 UT 신호봉 고가 돌파 마감 청산\n"
                "- 모든 판단은 확정봉 기준"
            )
            await self.show_setup_menu(update)
            return SELECT

        elif text == '24':
            await update.message.reply_text(prompts['24'])
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
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_exit_cache=True,
                    reset_stateful_strategy=True
                )
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
            removed_strategy_filter_choices = {'10', '17', '18', '27', '30', '31', '32', '33', '34'}
            if choice in removed_strategy_filter_choices:
                await update.message.reply_text("ℹ️ 해당 전략/필터 항목은 현재 코어 UT 설정에서 제거되었습니다.")
                await self.show_setup_menu(update)
                return SELECT

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
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_stateful_strategy=True
                )
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
                    '1': 'utbot',
                    '2': 'utrsibb',
                    '3': 'utrsi',
                    '4': 'utbb',
                    '5': 'utsmc'
                }
                val_lower = val.lower().strip()
                
                # 踰덊샇 ?낅젰 ??蹂??
                if val_lower in strategy_map:
                    val_lower = strategy_map[val_lower]
                
                if val_lower in UT_ONLY_STRATEGIES:
                    await self.cfg.update_value(['signal_engine', 'strategy_params', 'active_strategy'], val_lower)
                    signal_engine = self.engines.get('signal')
                    if signal_engine:
                        self._reset_signal_engine_runtime_state(
                            reset_entry_cache=True,
                            reset_exit_cache=True,
                            reset_stateful_strategy=True
                        )
                    await update.message.reply_text(f"✅ 전략 변경: {val_lower.upper()}")
                else:
                    await update.message.reply_text(
                        "❌ 1~5 또는 utbot/utrsibb/utrsi/utbb/utsmc를 입력하세요.\n"
                        "1=UTBOT, 2=UTRSIBB, 3=UTRSI, 4=UTBB, 5=UTSMC"
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
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_stateful_strategy=True
                )
                await update.message.reply_text(
                    f"✅ RSI+BB 설정 변경: RSI={rsi_length}, BB={bb_length}, x{bb_mult:.2f}"
                )

            elif choice == '57':
                parts = val.replace(' ', '').split(',')
                if len(parts) != 4:
                    await update.message.reply_text("❌ 형식: rsi_length,positive_above,negative_below,ema_period (예: 14,65,32,5)")
                    return SELECT

                rsi_length = int(parts[0])
                positive_above = float(parts[1])
                negative_below = float(parts[2])
                ema_period = int(parts[3])
                if rsi_length < 1 or ema_period < 1:
                    await update.message.reply_text("❌ RSI 기간과 EMA 기간은 1 이상이어야 합니다.")
                    return SELECT
                if not (0 <= negative_below < positive_above <= 100):
                    await update.message.reply_text("❌ 기준값은 `0 <= NegativeBelow < PositiveAbove <= 100` 이어야 합니다.")
                    return SELECT

                await self.cfg.update_value(['signal_engine', 'strategy_params', 'RSIMomentumTrend', 'rsi_length'], rsi_length)
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'RSIMomentumTrend', 'positive_above'], positive_above)
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'RSIMomentumTrend', 'negative_below'], negative_below)
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'RSIMomentumTrend', 'ema_period'], ema_period)
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_exit_cache=True,
                    reset_stateful_strategy=True
                )
                await update.message.reply_text(
                    f"✅ RSI Momentum Trend 설정 변경: RSI={rsi_length}, P>{positive_above:.1f}, N<{negative_below:.1f}, EMA={ema_period}"
                )

            elif choice == '58':
                toggle_raw = str(val or '').strip().lower()
                if toggle_raw in {'1', 'on', 'true', 'yes'}:
                    enabled = True
                elif toggle_raw in {'0', 'off', 'false', 'no'}:
                    enabled = False
                else:
                    await update.message.reply_text("❌ `on/off`, `1/0`, `true/false`, `yes/no` 중 하나를 입력하세요.")
                    return SELECT

                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBot', 'rsi_momentum_filter_enabled'],
                    enabled
                )
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_exit_cache=True,
                    reset_stateful_strategy=True
                )
                await update.message.reply_text(
                    f"✅ UTBot + RSI Momentum Trend: {'ON' if enabled else 'OFF'}"
                )

            elif choice == '59':
                toggle_raw = str(val or '').strip().lower()
                if toggle_raw in {'1', 'on', 'true', 'yes'}:
                    enabled = True
                elif toggle_raw in {'0', 'off', 'false', 'no'}:
                    enabled = False
                else:
                    await update.message.reply_text("❌ `on/off`, `1/0`, `true/false`, `yes/no` 중 하나를 입력하세요.")
                    return SELECT

                await self.cfg.update_value(
                    ['telegram', 'reporting', 'alt_trend_alert_enabled'],
                    enabled
                )
                reporting = self.cfg.get('telegram', {}).get('reporting', {})
                await self.cfg.update_value(
                    ['telegram', 'reporting', 'periodic_reports_enabled'],
                    bool(enabled or reporting.get('hourly_report_enabled', False))
                )
                self.last_alt_trend_scan_candle_ts_by_tf = {}
                self.last_alt_trend_alert_sent = {}
                self.last_alt_trend_scan_summary = {}
                await update.message.reply_text(
                    f"✅ 알트 상승추세 알림: {'ON' if enabled else 'OFF'}"
                )

            elif choice == '60':
                selected_timeframes = normalize_alt_trend_timeframes(val)
                if not selected_timeframes:
                    await update.message.reply_text(
                        "❌ 최소 1개 이상 입력하세요.\n허용 TF: 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 1d"
                    )
                    return SELECT

                await self.cfg.update_value(
                    ['telegram', 'reporting', 'alt_trend_alert_timeframes'],
                    selected_timeframes
                )
                self.last_alt_trend_scan_candle_ts_by_tf = {}
                self.last_alt_trend_alert_sent = {}
                self.last_alt_trend_scan_summary = {}
                await update.message.reply_text(
                    f"✅ 알트 상승추세 알림 TF: {format_alt_trend_timeframes(selected_timeframes)}"
                )

            elif choice in {'51', '53'}:
                raw_val = str(val or '').strip().lower()
                if raw_val in {'0', 'off', 'none'}:
                    selected_filters = []
                else:
                    tokens = [token.strip() for token in str(val or '').split(',') if token.strip()]
                    if not tokens:
                        await update.message.reply_text("❌ `0/off` 또는 `1,4,5` 형식으로 입력하세요.")
                        return SELECT
                    selected_filters = []
                    seen_filters = set()
                    for token in tokens:
                        if not re.fullmatch(r'\d+', token):
                            await update.message.reply_text("❌ 필터 번호는 `1,2,3,4,5` 중에서 입력하세요.")
                            return SELECT
                        filter_id = int(token)
                        if filter_id not in UTBOT_FILTER_PACK_ID_SET:
                            await update.message.reply_text("❌ 사용 가능한 필터 번호는 1,2,3,4,5 입니다.")
                            return SELECT
                        if filter_id in seen_filters:
                            await update.message.reply_text("❌ 같은 필터 번호를 중복해서 입력할 수 없습니다.")
                            return SELECT
                        seen_filters.add(filter_id)
                        selected_filters.append(filter_id)
                    selected_filters = sorted(selected_filters)

                filter_pack = self._get_utbot_filter_pack()
                if choice == '51':
                    filter_pack['entry']['selected'] = selected_filters
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBot', 'filter_pack'],
                        filter_pack
                    )
                    self._reset_signal_engine_runtime_state(
                        reset_entry_cache=True,
                        reset_exit_cache=True,
                        reset_stateful_strategy=True
                    )
                    await update.message.reply_text(
                        f"✅ UTBot 진입 필터: {self._format_utbot_filter_pack_selected(selected_filters)}"
                    )
                else:
                    filter_pack['exit']['selected'] = selected_filters
                    filter_pack['exit']['mode_by_filter'] = {
                        str(filter_id): normalize_utbot_filter_pack_exit_mode(
                            filter_pack.get('exit', {}).get('mode_by_filter', {}).get(str(filter_id), 'confirm')
                        )
                        for filter_id in selected_filters
                    }
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBot', 'filter_pack'],
                        filter_pack
                    )
                    self._reset_signal_engine_runtime_state(
                        reset_entry_cache=True,
                        reset_exit_cache=True,
                        reset_stateful_strategy=True
                    )
                    await update.message.reply_text(
                        f"✅ UTBot 청산 필터: {self._format_utbot_filter_pack_selected(selected_filters)}"
                    )

            elif choice in {'52', '54'}:
                logic_raw = str(val or '').strip().lower()
                if logic_raw not in {'and', 'or'}:
                    await update.message.reply_text("❌ `and` 또는 `or`만 입력하세요.")
                    return SELECT
                filter_pack = self._get_utbot_filter_pack()
                if choice == '52':
                    filter_pack['entry']['logic'] = logic_raw
                    label = '진입'
                else:
                    filter_pack['exit']['logic'] = logic_raw
                    label = '청산'
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBot', 'filter_pack'],
                    filter_pack
                )
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_exit_cache=True,
                    reset_stateful_strategy=True
                )
                await update.message.reply_text(
                    f"✅ UTBot {label} 결합모드: {self._format_utbot_filter_pack_logic(logic_raw)}"
                )

            elif choice == '55':
                filter_pack = self._get_utbot_filter_pack()
                exit_selected = normalize_utbot_filter_pack_selected(
                    filter_pack.get('exit', {}).get('selected', [])
                )
                if not exit_selected:
                    await update.message.reply_text("❌ 먼저 53번에서 UTBot 청산 필터를 선택하세요.")
                    return SELECT

                raw_val = str(val or '').strip().lower()
                if raw_val in {'0', 'off', 'none'}:
                    mode_by_filter = {str(filter_id): 'confirm' for filter_id in exit_selected}
                else:
                    tokens = [token.strip() for token in str(val or '').split(',') if token.strip()]
                    if not tokens:
                        await update.message.reply_text("❌ 형식: `2:c,3:s,5:c`")
                        return SELECT
                    mode_by_filter = {str(filter_id): 'confirm' for filter_id in exit_selected}
                    seen_mode_filters = set()
                    for token in tokens:
                        if ':' not in token:
                            await update.message.reply_text("❌ 형식: `2:c,3:s,5:c`")
                            return SELECT
                        filter_token, mode_token = token.split(':', 1)
                        filter_token = filter_token.strip()
                        mode_token = mode_token.strip().lower()
                        if not re.fullmatch(r'\d+', filter_token):
                            await update.message.reply_text("❌ 필터 번호는 숫자로 입력하세요.")
                            return SELECT
                        filter_id = int(filter_token)
                        if filter_id not in exit_selected:
                            await update.message.reply_text(
                                "❌ 55번에서는 53번에서 선택한 청산 필터만 지정할 수 있습니다."
                            )
                            return SELECT
                        if filter_id in seen_mode_filters:
                            await update.message.reply_text("❌ 같은 필터 타입을 중복 지정할 수 없습니다.")
                            return SELECT
                        if mode_token not in {'c', 'confirm', 's', 'signal'}:
                            await update.message.reply_text("❌ 필터 타입은 `c(confirm)` 또는 `s(signal)`만 가능합니다.")
                            return SELECT
                        seen_mode_filters.add(filter_id)
                        mode_by_filter[str(filter_id)] = normalize_utbot_filter_pack_exit_mode(mode_token)

                filter_pack['exit']['mode_by_filter'] = mode_by_filter
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBot', 'filter_pack'],
                    filter_pack
                )
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_exit_cache=True,
                    reset_stateful_strategy=True
                )
                await update.message.reply_text(
                    f"✅ UTBot 청산 필터 타입: {self._format_utbot_filter_pack_mode_map(mode_by_filter, exit_selected)}"
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
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_stateful_strategy=True
                )
                await update.message.reply_text(
                    f"✅ UT Bot 설정 변경: key={key_value:.2f}, ATR={atr_period}, HA={'ON' if use_ha else 'OFF'}"
                )

            elif choice == '26':
                mode_raw = str(val or '').strip().lower()
                if mode_raw in {'1', 'next', 'next_candle', 'next-candle'}:
                    timing_mode = 'next_candle'
                    timing_label = '다음봉 진입'
                elif mode_raw in {'2', 'persistent', 'hold', 'maintain'}:
                    timing_mode = 'persistent'
                    timing_label = '신호유지 진입'
                else:
                    await update.message.reply_text("❌ `next`(1) 또는 `persistent`(2)만 입력하세요.")
                    return SELECT

                await self.cfg.update_value(['signal_engine', 'strategy_params', 'ut_entry_timing_mode'], timing_mode)
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_stateful_strategy=True
                )
                await update.message.reply_text(f"✅ UT 진입 방식 변경: {timing_label}")

            elif choice == '49':
                filter_mode = self._normalize_utsmc_candidate_filter_mode(val)
                if filter_mode == 'off' and str(val or '').strip().lower() not in {'0', 'off', 'none'}:
                    await update.message.reply_text("❌ `0/off`, `1/c1`, `2/c2`, `3/c12` 중 하나를 입력하세요.")
                    return SELECT

                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTSMC', 'candidate_filter', 'mode'],
                    filter_mode
                )
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_stateful_strategy=True
                )
                await update.message.reply_text(
                    f"✅ UTSMC 후보 필터 변경: {self._format_utsmc_candidate_filter_mode(filter_mode)}"
                )

            elif choice == '50':
                toggle_raw = str(val or '').strip().lower()
                if toggle_raw in {'1', 'on', 'true', 'yes'}:
                    exit_candidate2_enabled = True
                elif toggle_raw in {'0', 'off', 'false', 'no'}:
                    exit_candidate2_enabled = False
                else:
                    await update.message.reply_text("❌ `on/off`, `1/0`, `true/false`, `yes/no` 중 하나를 입력하세요.")
                    return SELECT

                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTSMC', 'exit_candidate2_enabled'],
                    exit_candidate2_enabled
                )
                self._reset_signal_engine_runtime_state(reset_exit_cache=True)
                await update.message.reply_text(
                    f"✅ UTSMC C2 청산: {'ON' if exit_candidate2_enabled else 'OFF'}"
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
                self._reset_signal_engine_runtime_state(reset_exit_cache=True)
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
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_stateful_strategy=True
                )
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
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_stateful_strategy=True
                )
                await update.message.reply_text(f"✅ 업비트 진입 타임프레임 변경: {val}")

            elif choice == '47':
                valid_tf = ['1m', '3m', '5m', '15m', '30m', '1h', '4h', '1d']
                if val not in valid_tf:
                    await update.message.reply_text(f"❌ 유효하지 않은 타임프레임입니다.\n사용 가능: {', '.join(valid_tf)}")
                    return SELECT
                await self.cfg.update_value(['upbit', 'common_settings', 'exit_timeframe'], val)
                self._reset_signal_engine_runtime_state(reset_exit_cache=True)
                await update.message.reply_text(f"✅ 업비트 청산 타임프레임 변경: {val}")

            elif choice == '48':
                limit_krw = float(val)
                if limit_krw <= 0:
                    await update.message.reply_text("❌ 업비트 일일 손실 제한은 0보다 커야 합니다.")
                    return SELECT
                await self.cfg.update_value(['upbit', 'common_settings', 'daily_loss_limit'], limit_krw)
                await update.message.reply_text(f"✅ 업비트 일일 손실 제한 변경: ₩{limit_krw:,.0f}")
            
            # 10~41 success message handled
            if choice not in ['2', '3', '10', '11', '12', '14', '15', '16', '17', '18', '19', '20', '21', '22', '23', '26', '27', '28', '30', '31', '33', '34', '35', '41', '44', '45', '46', '47', '48', '49', '50', '51', '52', '53', '54', '55']:
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
                    self._reset_signal_engine_runtime_state(
                        reset_entry_cache=True,
                        reset_exit_cache=True,
                        reset_stateful_strategy=True
                    )
                    signal_engine.active_symbols.clear()
                    signal_engine.active_symbols.add(symbol)
                    await signal_engine.prime_symbol_to_next_closed_candle(symbol)
                else:
                    self._reset_signal_engine_runtime_state(
                        reset_entry_cache=True,
                        reset_exit_cache=True,
                        reset_stateful_strategy=True
                    )
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
            previous_direction = self.get_effective_trade_direction()
            await self.cfg.update_value(['system_settings', 'trade_direction'], direction)
            if direction != previous_direction:
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_exit_cache=True,
                    reset_stateful_strategy=True
                )
            direction_label = {'both': '양방향', 'long': '롱만', 'short': '숏만'}.get(direction, direction)
            await update.message.reply_text(f"✅ 매매 방향 변경: {direction_label}")
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
            [KeyboardButton("/setup"), KeyboardButton("/utbreakout"), KeyboardButton("/status")],
            [KeyboardButton("/history"), KeyboardButton("/stats")],
            [KeyboardButton("/log"), KeyboardButton("/help")]
        ]
        return ReplyKeyboardMarkup(kb, resize_keyboard=True)

    async def _restore_main_keyboard(self, update: Update):
        """Restore the main keyboard."""
        await update.message.reply_text("📱 메인 메뉴", reply_markup=self._build_main_keyboard())

    async def _setup_telegram(self):
        markup = self._build_main_keyboard()
        text_filter = filters.TEXT & ~filters.COMMAND
        setup_trigger_pattern = r"^/setup(?:@[A-Za-z0-9_]+)?$"
        menu_trigger_pattern = r"^/(status|history|log|help|stats|close|utbreakout)(?:@[A-Za-z0-9_]+)?(?:\s.*)?$"
        setup_text_filter = text_filter & ~filters.Regex(r"^/")

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

        async def status_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            status_text, history_text, snapshot_key = self._build_manual_status_payload()
            self._record_status_snapshot(snapshot_key, history_text)
            await self._reply_markdown_safe(u.message, status_text)

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

        def _format_utbreakout_menu_text():
            sig_cfg = self.cfg.get('signal_engine', {})
            strategy_params = sig_cfg.get('strategy_params', {})
            active_strategy = str(strategy_params.get('active_strategy', 'utbot') or 'utbot').lower()
            cfg = build_default_utbot_filtered_breakout_config()
            raw_cfg = strategy_params.get('UTBotFilteredBreakoutV1', {})
            if isinstance(raw_cfg, dict):
                cfg.update(raw_cfg)
            watchlist = self.get_active_watchlist()
            first_symbol = watchlist[0] if watchlist else 'BTC/USDT'
            engine = self.engines.get('signal')
            diag = {}
            if engine:
                diag = engine.last_utbot_filtered_breakout_status.get(first_symbol, {}) or {}
            active_label = "ON" if active_strategy == UTBOT_FILTERED_BREAKOUT_STRATEGY else f"OFF ({active_strategy.upper()})"
            last_reason = diag.get('reject_code') or diag.get('accepted_code') or diag.get('reason') or '대기'
            diag_summary = format_utbreakout_diagnostic_summary()
            set_id = normalize_utbreakout_set_id(cfg.get('active_set_id') or cfg.get('profile'), UTBREAKOUT_DEFAULT_SET_ID)
            set_info = get_utbreakout_set_definition(set_id)
            mode_label = 'AUTO' if bool(cfg.get('auto_select_enabled', False)) or str(cfg.get('selection_mode', '')).lower() == 'auto' else 'MANUAL'
            auto_set = diag.get('auto_selected_set_id')
            auto_name = diag.get('auto_selected_set_name')
            auto_reason = diag.get('auto_selection_reason') or '아직 AUTO 분석 기록 없음'
            active_set_lines = "\n".join(format_utbreakout_set_brief(i) for i in range(1, 11))
            opposite_pnl_text = (
                f"PnL≥${float(cfg.get('opposite_set_exit_min_pnl_usdt', 0.0) or 0.0):.2f}"
                if cfg.get('opposite_set_exit_min_pnl_enabled') else
                "PnL조건 OFF"
            )
            return f"""
🧭 **UTBOT_FILTERED_BREAKOUT_V1**

상태: `{active_label}`
선택모드: `{mode_label}` | 수동 Set: `Set{set_id} {set_info.get('name')}` | AUTO 최근: `{('Set' + str(auto_set) + ' ' + str(auto_name)) if auto_set else '대기'}`
프로필: `{cfg.get('profile', 'set2')}` | 진입 `{cfg.get('entry_timeframe', '15m')}` / 청산 `{cfg.get('exit_timeframe', cfg.get('entry_timeframe', '15m'))}` / HTF `{cfg.get('htf_timeframe', '1h')}`
UT: `K={float(cfg.get('utbot_key_value', 2.5) or 2.5):.2f}` / `ATR={int(cfg.get('utbot_atr_period', 14) or 14)}`
선택 Set 조건: `{', '.join(set_info.get('entry_filters') or ['UT only'])}`
리스크: `SL {float(cfg.get('stop_atr_multiplier', 1.5) or 1.5):.1f}ATR` | `TP {float(cfg.get('take_profit_r_multiple', 2.0) or 2.0):.1f}R` | `1회 최대손실 ${float(cfg.get('max_risk_per_trade_usdt', 1.0) or 1.0):.2f}`
손실한도: `1회 min(잔고 x {float(cfg.get('risk_per_trade_percent', 1.0) or 1.0):.2f}%, ${float(cfg.get('max_risk_per_trade_usdt', 1.0) or 1.0):.2f})` | `일손실 ${float(cfg.get('daily_max_loss_usdt', 3.0) or 3.0):.2f}`
옵션: 반대Set청산 `{'ON' if cfg.get('opposite_set_exit_enabled') else 'OFF'}` (`{int(float(cfg.get('opposite_set_exit_min_hold_candles', 3) or 0))}봉`, `{opposite_pnl_text}`) | 청산 후 반대진입 `없음`
보조청산: EMA/RSI `{'ON' if cfg.get('ema_rsi_exit_enabled') else 'OFF'}` | RSI과열제외 `{'ON' if cfg.get('exclude_rsi_extreme') else 'OFF'}`

AUTO 최근 선택 이유:
`{auto_reason}`

실거래 연결 Set 1~50 (아래는 빠른 버튼용 Set 1~10 요약):
```
{active_set_lines}
```
Set 11~50도 AUTO 후보/수동 선택에 연결되어 있습니다. 전체 설명은 `/utbreakout sets 1~5`, 수동 선택은 `/utbreakout set 27`처럼 입력하세요.

최근 진단({first_symbol}): `{last_reason}`
진단 요약:
```
{diag_summary}
```

명령:
`/utbreakout on` - 전략 활성화
`/utbreakout off` - UTBOT으로 복귀
`/utbreakout auto on` / `auto off` - AUTO set 선택 ON/OFF
`/utbreakout set 27` 또는 `set27` - Set 1~50 수동 적용
`/utbreakout sets` - 50개 set 설명 보기
`/utbreakout why` - 최근 AUTO 선택 이유 보기
`/utbreakout risk 5` - 1회 최대 손실 5 USDT로 설정
`/utbreakout riskpct 1` - 잔고 대비 손실 기준 1%로 설정
`/utbreakout dailyloss 30` - 하루 최대 손실 30 USDT로 설정
`/utbreakout status` - 롱/숏 조건 신호등
`/utbreakout research` - 최근 7일 리서치 요약
`/utbreakout menu` - 이 메뉴 다시 보기
`/utbreakout log` - 진단 로그 파일 다운로드
`/utbreakout toggle_opposite_set` - 반대 UT + 반대 Set 조건 청산 토글
`/utbreakout opphold 3` - 반대Set청산 최소 보유 3캔들
`/utbreakout opppnl off` - 반대Set청산 PnL 조건 끄기
`/utbreakout opppnl on` - 기존 값으로 PnL 조건 켜기
`/utbreakout opppnl 0` - PnL 조건 켜고 최소 미실현손익 0 USDT
`/utbreakout toggle_ema` - EMA50/RSI 청산 토글
`/utbreakout toggle_extreme` - RSI 과열 제외 토글
""".strip()

        def _build_utbreakout_keyboard():
            return InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("전략 ON", callback_data="utb:on"),
                    InlineKeyboardButton("UTBOT 복귀", callback_data="utb:off")
                ],
                [
                    InlineKeyboardButton("AUTO ON", callback_data="utb:auto:on"),
                    InlineKeyboardButton("AUTO OFF", callback_data="utb:auto:off"),
                    InlineKeyboardButton("AUTO 이유", callback_data="utb:why")
                ],
                [
                    InlineKeyboardButton("Set1", callback_data="utb:set:1"),
                    InlineKeyboardButton("Set2", callback_data="utb:set:2"),
                    InlineKeyboardButton("Set3", callback_data="utb:set:3"),
                    InlineKeyboardButton("Set4", callback_data="utb:set:4"),
                    InlineKeyboardButton("Set5", callback_data="utb:set:5")
                ],
                [
                    InlineKeyboardButton("Set6", callback_data="utb:set:6"),
                    InlineKeyboardButton("Set7", callback_data="utb:set:7"),
                    InlineKeyboardButton("Set8", callback_data="utb:set:8"),
                    InlineKeyboardButton("Set9", callback_data="utb:set:9"),
                    InlineKeyboardButton("Set10", callback_data="utb:set:10")
                ],
                [
                    InlineKeyboardButton("50 Set 설명", callback_data="utb:sets")
                ],
                [
                    InlineKeyboardButton("1회손실 $0.5", callback_data="utb:risk:0.5"),
                    InlineKeyboardButton("1회손실 $1", callback_data="utb:risk:1"),
                    InlineKeyboardButton("1회손실 $2", callback_data="utb:risk:2"),
                    InlineKeyboardButton("1회손실 $5", callback_data="utb:risk:5")
                ],
                [
                    InlineKeyboardButton("1회손실 $10", callback_data="utb:risk:10"),
                    InlineKeyboardButton("1회손실 $25", callback_data="utb:risk:25"),
                    InlineKeyboardButton("1회손실 $50", callback_data="utb:risk:50")
                ],
                [
                    InlineKeyboardButton("Risk 0.5%", callback_data="utb:riskpct:0.5"),
                    InlineKeyboardButton("Risk 1%", callback_data="utb:riskpct:1"),
                    InlineKeyboardButton("Risk 2%", callback_data="utb:riskpct:2")
                ],
                [
                    InlineKeyboardButton("일손실 $3", callback_data="utb:dailyloss:3"),
                    InlineKeyboardButton("일손실 $10", callback_data="utb:dailyloss:10"),
                    InlineKeyboardButton("일손실 $20", callback_data="utb:dailyloss:20"),
                    InlineKeyboardButton("일손실 $30", callback_data="utb:dailyloss:30")
                ],
                [
                    InlineKeyboardButton("일손실 $50", callback_data="utb:dailyloss:50"),
                    InlineKeyboardButton("일손실 $100", callback_data="utb:dailyloss:100")
                ],
                [
                    InlineKeyboardButton("반대Set청산", callback_data="utb:toggle_opposite_set"),
                    InlineKeyboardButton("보유0봉", callback_data="utb:opphold:0"),
                    InlineKeyboardButton("보유3봉", callback_data="utb:opphold:3")
                ],
                [
                    InlineKeyboardButton("PnL조건 OFF", callback_data="utb:opppnl:off"),
                    InlineKeyboardButton("PnL≥$0", callback_data="utb:opppnl:0"),
                    InlineKeyboardButton("PnL≥-$25", callback_data="utb:opppnl:-25")
                ],
                [
                    InlineKeyboardButton("EMA청산", callback_data="utb:toggle_ema"),
                    InlineKeyboardButton("RSI과열", callback_data="utb:toggle_extreme")
                ],
                [
                    InlineKeyboardButton("조건 스테이터스", callback_data="utb:condition_status"),
                    InlineKeyboardButton("리서치 요약", callback_data="utb:research")
                ],
                [
                    InlineKeyboardButton("진단 로그 다운로드", callback_data="utb:download"),
                    InlineKeyboardButton("리서치 다운로드", callback_data="utb:research_download")
                ],
                [
                    InlineKeyboardButton("새로고침", callback_data="utb:status")
                ]
            ])

        async def _edit_utbreakout_menu(query, notice=None):
            text = _format_utbreakout_menu_text()
            if notice:
                text = f"{notice}\n\n{text}"
            try:
                await query.edit_message_text(
                    text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_build_utbreakout_keyboard()
                )
            except BadRequest as md_err:
                message = str(md_err).lower()
                if "message is not modified" in message:
                    return
                if "can't parse entities" in message:
                    plain_text = str(text).replace("**", "").replace("`", "")
                    await query.edit_message_text(
                        plain_text,
                        reply_markup=_build_utbreakout_keyboard()
                    )
                else:
                    raise

        async def _send_utbreakout_log_document(message):
            if message is None:
                return
            path = UTBREAKOUT_DIAGNOSTIC_LOG_FILE
            try:
                if not os.path.exists(path) or os.path.getsize(path) <= 0:
                    await message.reply_text("다운로드할 UT Breakout 진단 로그가 아직 없습니다.")
                    return
                with open(path, 'rb') as fp:
                    await message.reply_document(
                        document=fp,
                        filename=os.path.basename(path),
                        caption="UT Breakout 진단 로그입니다. 후보/거절/승인/진입차단 이벤트만 기록합니다."
                    )
            except Exception as e:
                logger.error(f"UT breakout diagnostic log download failed: {e}")
                await message.reply_text(f"진단 로그 다운로드 실패: {e}")

        def _format_utbreakout_research_text():
            engine = self.engines.get('signal')
            protection_status = engine.last_protection_order_status if engine else {}
            text = format_utbreakout_research_summary(
                protection_status=protection_status,
                days=7
            )
            return "\n".join([
                "📈 UT Breakout 리서치 요약",
                "실거래 조건을 바꾸는 화면이 아니라, 최근 판단 로그로 쏠림/거절/리스크를 보는 화면입니다.",
                "",
                text,
                "",
                "다운로드: /utbreakout research download",
            ])

        async def _send_utbreakout_research_document(message):
            if message is None:
                return
            try:
                report = _format_utbreakout_research_text()
                bio = io.BytesIO(report.encode('utf-8'))
                bio.name = 'utbreakout_research_report.txt'
                await message.reply_document(
                    document=bio,
                    filename='utbreakout_research_report.txt',
                    caption='UT Breakout 최근 7일 리서치 요약입니다.'
                )
            except Exception as e:
                logger.error(f"UT breakout research report download failed: {e}")
                await message.reply_text(f"리서치 리포트 다운로드 실패: {e}")

        def _get_utbreakout_status_symbol():
            watchlist = self.get_active_watchlist()
            return watchlist[0] if watchlist else 'BTC/USDT'

        async def _get_utbreakout_condition_status_text():
            engine = self.engines.get('signal')
            if not engine:
                return "🚦 UT Breakout 조건 스테이터스\n\nSignal 엔진을 찾을 수 없습니다."
            symbol = _get_utbreakout_status_symbol()
            return await engine.build_utbreakout_condition_status_text(symbol)

        async def _send_utbreakout_condition_status(message):
            if message is None:
                return
            text = await _get_utbreakout_condition_status_text()
            await message.reply_text(text, reply_markup=_build_utbreakout_keyboard())

        async def _edit_utbreakout_condition_status(query):
            text = await _get_utbreakout_condition_status_text()
            try:
                await query.edit_message_text(text, reply_markup=_build_utbreakout_keyboard())
            except BadRequest as md_err:
                if "message is not modified" in str(md_err).lower():
                    return
                raise

        def _format_utbreakout_sets_text(page=1):
            try:
                page = int(page or 1)
            except (TypeError, ValueError):
                page = 1
            page = min(5, max(1, page))
            start_id = ((page - 1) * 10) + 1
            end_id = min(50, start_id + 9)
            lines = [
                f"📚 UT Breakout 50-Set 카탈로그 ({page}/5)",
                "Set 1~50 모두 AUTO 후보/수동 선택/실거래 판단에 연결되어 있습니다.",
                "",
            ]
            for set_id in range(start_id, end_id + 1):
                info = get_utbreakout_set_definition(set_id)
                status = "실거래" if info.get('status') == 'active' else "예정"
                filters = ", ".join(info.get('entry_filters') or ['UT only'])
                lines.extend([
                    f"Set{set_id} {info.get('name')} [{status}]",
                    f"장세: {info.get('regime')}",
                    f"조건: {filters} | 빈도: {info.get('frequency_impact')}",
                    f"설명: {info.get('description')}",
                    "",
                ])
            lines.append("다른 페이지: /utbreakout sets 1~5")
            return "\n".join(lines).strip()

        def _format_utbreakout_why_text():
            symbol = _get_utbreakout_status_symbol()
            engine = self.engines.get('signal')
            diag = {}
            if engine:
                diag = engine.last_utbot_filtered_breakout_status.get(symbol, {}) or {}
            cfg = _current_utbreakout_cfg()
            set_id = diag.get('auto_selected_set_id') or cfg.get('active_set_id') or cfg.get('profile') or UTBREAKOUT_DEFAULT_SET_ID
            info = get_utbreakout_set_definition(set_id)
            scores = diag.get('auto_scores') or {}
            if scores:
                score_lines = [
                    f"trend: {scores.get('trend_score', 0)}",
                    f"chop: {scores.get('chop_score', 0)}",
                    f"volatility: {scores.get('volatility_score', 0)}",
                    f"breakout: {scores.get('breakout_score', 0)}",
                    f"momentum: {scores.get('momentum_score', 0)}",
                    f"flow: {scores.get('flow_score', 0)}",
                    f"alignment: {scores.get('alignment_score', 0)}",
                    f"dominant_side: {scores.get('dominant_side', 'n/a')}",
                ]
            else:
                score_lines = ["AUTO 점수 기록 없음. /utbreakout status 또는 다음 판단봉 이후 확인하세요."]
            return "\n".join([
                "🧠 UT Breakout AUTO 선택 이유",
                f"심볼: {symbol}",
                f"선택: Set{info.get('id')} {info.get('name')} ({'실거래' if info.get('status') == 'active' else 'planned'})",
                f"이유: {diag.get('auto_selection_reason') or '수동 선택 또는 아직 분석 기록 없음'}",
                f"실제 진입 조건: {', '.join(info.get('entry_filters') or ['UT only'])}",
                "",
                "점수:",
                *score_lines,
            ])

        def _current_utbreakout_cfg():
            raw = self.cfg.get('signal_engine', {}).get('strategy_params', {}).get('UTBotFilteredBreakoutV1', {})
            return dict(raw) if isinstance(raw, dict) else {}

        def _merge_utbreakout_profile_with_preserved_settings(profile):
            profile_cfg = build_utbot_filtered_breakout_profile(profile)
            current_cfg = _current_utbreakout_cfg()
            preserve_keys = {
                'entry_timeframe',
                'exit_timeframe',
                'htf_timeframe',
                'auto_timeframes',
                'risk_per_trade_percent',
                'max_risk_per_trade_usdt',
                'daily_max_loss_usdt',
                'max_daily_trades',
                'daily_profit_target_enabled',
                'daily_profit_target_usdt',
                'opposite_signal_exit_enabled',
                'opposite_set_exit_enabled',
                'opposite_set_exit_min_hold_candles',
                'opposite_set_exit_min_pnl_enabled',
                'opposite_set_exit_min_pnl_usdt',
                'ema_rsi_exit_enabled',
                'adx_donchian_exit_enabled',
                'exclude_rsi_extreme'
            }
            for key in preserve_keys:
                if key in current_cfg:
                    profile_cfg[key] = current_cfg[key]
            return profile_cfg

        async def utbreakout_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            args = list(getattr(c, 'args', []) or [])
            if not args and u and u.message and u.message.text:
                parts = u.message.text.strip().split()
                args = parts[1:]
            action = str(args[0]).strip().lower() if args else ''

            def _parse_positive_arg(label, minimum=0.0, maximum=None):
                if len(args) < 2:
                    raise ValueError(f"{label} 값을 입력하세요.")
                try:
                    value = float(str(args[1]).replace('$', '').replace(',', '').strip())
                except (TypeError, ValueError):
                    raise ValueError(f"{label} 값은 숫자로 입력하세요.")
                if value <= minimum:
                    raise ValueError(f"{label} 값은 {minimum}보다 커야 합니다.")
                if maximum is not None and value > maximum:
                    raise ValueError(f"{label} 값은 {maximum} 이하로 입력하세요.")
                return value

            def _parse_float_arg(label, minimum=None, maximum=None):
                if len(args) < 2:
                    raise ValueError(f"{label} 값을 입력하세요.")
                try:
                    value = float(str(args[1]).replace('$', '').replace(',', '').strip())
                except (TypeError, ValueError):
                    raise ValueError(f"{label} 값은 숫자로 입력하세요.")
                if minimum is not None and value < minimum:
                    raise ValueError(f"{label} 값은 {minimum} 이상이어야 합니다.")
                if maximum is not None and value > maximum:
                    raise ValueError(f"{label} 값은 {maximum} 이하로 입력하세요.")
                return value

            if action in {'on', 'enable', 'activate', 'start'}:
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'active_strategy'], UTBOT_FILTERED_BREAKOUT_STRATEGY)
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_exit_cache=True,
                    reset_stateful_strategy=True
                )
                await u.message.reply_text("✅ UTBOT_FILTERED_BREAKOUT_V1 활성화 완료")
            elif action in {'off', 'disable', 'utbot'}:
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'active_strategy'], 'utbot')
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_exit_cache=True,
                    reset_stateful_strategy=True
                )
                await u.message.reply_text("✅ 기본 UTBOT 전략으로 복귀")
            elif action in {'auto', 'autoset', 'auto_select'}:
                mode = str(args[1]).strip().lower() if len(args) > 1 else ''
                if mode not in {'on', 'off', 'enable', 'disable'}:
                    await u.message.reply_text("❌ 예: `/utbreakout auto on` 또는 `/utbreakout auto off`", parse_mode=ParseMode.MARKDOWN)
                    return
                enabled = mode in {'on', 'enable'}
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'auto_select_enabled'],
                    enabled
                )
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'selection_mode'],
                    'auto' if enabled else 'manual'
                )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await u.message.reply_text(f"✅ AUTO Set 선택: {'ON' if enabled else 'OFF'}")
            elif action == 'set' or re.fullmatch(r'set\d+', action or '') or action in {'1', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'aggressive', 'conservative'}:
                set_arg = args[1] if action == 'set' and len(args) > 1 else action
                set_text = str(set_arg or '').strip().lower()
                set_id = 1 if set_text == 'aggressive' else 7 if set_text == 'conservative' else normalize_utbreakout_set_id(set_arg, UTBREAKOUT_DEFAULT_SET_ID)
                if set_id > UTBREAKOUT_ACTIVE_SET_MAX:
                    await u.message.reply_text("❌ 현재 선택 가능한 범위는 Set 1~50입니다.")
                    return
                profile_cfg = _merge_utbreakout_profile_with_preserved_settings(f"set{set_id}")
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1'],
                    profile_cfg
                )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                info = get_utbreakout_set_definition(set_id)
                await u.message.reply_text(f"✅ Set{set_id} 적용: {info.get('name')} (AUTO OFF, 수동 선택)")
            elif action in {'risk', 'maxrisk', 'loss', 'max_loss'}:
                try:
                    value = _parse_positive_arg('1회 최대 손실 USDT', minimum=0.0, maximum=100000.0)
                except ValueError as e:
                    await u.message.reply_text(f"❌ {e}\n예: `/utbreakout risk 5`", parse_mode=ParseMode.MARKDOWN)
                    return
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'max_risk_per_trade_usdt'],
                    value
                )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await u.message.reply_text(f"✅ 1회 최대 손실 설정: ${value:.2f}")
            elif action in {'riskpct', 'risk_pct', 'riskpercent', 'risk_percent'}:
                try:
                    value = _parse_positive_arg('잔고 대비 손실 기준(%)', minimum=0.0, maximum=100.0)
                except ValueError as e:
                    await u.message.reply_text(f"❌ {e}\n예: `/utbreakout riskpct 1`", parse_mode=ParseMode.MARKDOWN)
                    return
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'risk_per_trade_percent'],
                    value
                )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await u.message.reply_text(f"✅ 잔고 대비 손실 기준 설정: {value:.2f}%")
            elif action in {'dailyloss', 'daily_loss', 'dayloss', 'day_loss'}:
                try:
                    value = _parse_positive_arg('하루 최대 손실 USDT', minimum=0.0, maximum=1000000.0)
                except ValueError as e:
                    await u.message.reply_text(f"❌ {e}\n예: `/utbreakout dailyloss 30`", parse_mode=ParseMode.MARKDOWN)
                    return
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'daily_max_loss_usdt'],
                    value
                )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await u.message.reply_text(f"✅ 하루 최대 손실 설정: ${value:.2f}")
            elif action in {'toggle_opposite_set', 'opposite_set', 'setexit', 'set_exit'}:
                raw = _current_utbreakout_cfg()
                current = bool(raw.get('opposite_set_exit_enabled', False))
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_set_exit_enabled'],
                    not current
                )
                if not current:
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_signal_exit_enabled'],
                        False
                    )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await u.message.reply_text(
                    f"✅ 반대Set청산: {'ON' if not current else 'OFF'}\n"
                    "조건: 반대 UT 신규신호 + 반대 방향 선택 Set 조건 통과 + 최소 보유 조건. PnL 조건은 별도 옵션이며 기본 OFF입니다. 청산만 하고 반대진입은 하지 않습니다."
                )
            elif action in {'opphold', 'opposite_hold', 'sethold', 'set_hold'}:
                try:
                    raw_value = _parse_float_arg('반대Set청산 최소 보유 캔들', minimum=0.0, maximum=1000.0)
                    if raw_value != int(raw_value):
                        raise ValueError("반대Set청산 최소 보유 캔들은 정수로 입력하세요.")
                    value = int(raw_value)
                except ValueError as e:
                    await u.message.reply_text(f"❌ {e}\n예: `/utbreakout opphold 3`", parse_mode=ParseMode.MARKDOWN)
                    return
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_set_exit_min_hold_candles'],
                    value
                )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await u.message.reply_text(f"✅ 반대Set청산 최소 보유: {value}캔들")
            elif action in {'opppnl', 'opposite_pnl', 'setpnl', 'set_pnl'}:
                mode = str(args[1]).strip().lower() if len(args) > 1 else ''
                if mode in {'off', 'disable', 'false'}:
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_set_exit_min_pnl_enabled'],
                        False
                    )
                    self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                    await u.message.reply_text("✅ 반대Set청산 PnL 조건: OFF")
                    return
                if mode in {'on', 'enable', 'true'}:
                    raw = _current_utbreakout_cfg()
                    value = float(raw.get('opposite_set_exit_min_pnl_usdt', 0.0) or 0.0)
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_set_exit_min_pnl_enabled'],
                        True
                    )
                    self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                    await u.message.reply_text(f"✅ 반대Set청산 PnL 조건: ON / 최소 ${value:.2f}")
                    return
                try:
                    value = _parse_float_arg('반대Set청산 최소 미실현손익 USDT', minimum=-1000000.0, maximum=1000000.0)
                except ValueError as e:
                    await u.message.reply_text(f"❌ {e}\n예: `/utbreakout opppnl off`, `/utbreakout opppnl 0`, `/utbreakout opppnl -25`", parse_mode=ParseMode.MARKDOWN)
                    return
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_set_exit_min_pnl_enabled'],
                    True
                )
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_set_exit_min_pnl_usdt'],
                    value
                )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await u.message.reply_text(f"✅ 반대Set청산 PnL 조건: ON / 최소 ${value:.2f}")
            elif action in {'toggle_opposite', 'opposite'}:
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_signal_exit_enabled'],
                    False
                )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await u.message.reply_text("✅ 단순 반대UT청산(A)은 기각 옵션이라 OFF로 유지합니다. 반대Set청산(B)은 `/utbreakout toggle_opposite_set`으로 켜세요.", parse_mode=ParseMode.MARKDOWN)
            elif action in {'toggle_ema', 'ema'}:
                raw = _current_utbreakout_cfg()
                current = bool(raw.get('ema_rsi_exit_enabled', False))
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'ema_rsi_exit_enabled'],
                    not current
                )
                await u.message.reply_text(f"✅ EMA50/RSI 청산: {'ON' if not current else 'OFF'}")
            elif action in {'toggle_extreme', 'extreme'}:
                raw = _current_utbreakout_cfg()
                current = bool(raw.get('exclude_rsi_extreme', False))
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'exclude_rsi_extreme'],
                    not current
                )
                await u.message.reply_text(f"✅ RSI 과열 제외 옵션: {'ON' if not current else 'OFF'}")
            elif action in {'log', 'logs', 'download'}:
                await _send_utbreakout_log_document(u.message)
                return
            elif action in {'sets', 'catalog', 'list'}:
                page = args[1] if len(args) > 1 else 1
                await u.message.reply_text(_format_utbreakout_sets_text(page), reply_markup=_build_utbreakout_keyboard())
                return
            elif action in {'why', 'reason', 'auto_reason'}:
                await u.message.reply_text(_format_utbreakout_why_text(), reply_markup=_build_utbreakout_keyboard())
                return
            elif action in {'research', 'report'}:
                if len(args) > 1 and str(args[1]).strip().lower() in {'download', 'file', 'log'}:
                    await _send_utbreakout_research_document(u.message)
                else:
                    await u.message.reply_text(_format_utbreakout_research_text(), reply_markup=_build_utbreakout_keyboard())
                return
            elif action in {'status', 'conditions', 'condition_status'}:
                await _send_utbreakout_condition_status(u.message)
                return
            elif action in {'menu', ''}:
                pass
            else:
                await u.message.reply_text("❌ 알 수 없는 UT Breakout 명령입니다. `/utbreakout`로 메뉴를 확인하세요.", parse_mode=ParseMode.MARKDOWN)
                return

            await self._reply_markdown_safe(
                u.message,
                _format_utbreakout_menu_text(),
                reply_markup=_build_utbreakout_keyboard()
            )

        async def utbreakout_callback(u: Update, c: ContextTypes.DEFAULT_TYPE):
            query = u.callback_query
            if not query:
                return
            await query.answer()
            data = str(query.data or '')
            if not data.startswith('utb:'):
                return
            parts = data.split(':')
            action = parts[1] if len(parts) > 1 else 'status'
            value = parts[2] if len(parts) > 2 else None

            if action == 'on':
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'active_strategy'], UTBOT_FILTERED_BREAKOUT_STRATEGY)
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_exit_cache=True,
                    reset_stateful_strategy=True
                )
                await _edit_utbreakout_menu(query, "✅ UTBOT_FILTERED_BREAKOUT_V1 활성화 완료")
                return

            if action == 'off':
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'active_strategy'], 'utbot')
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_exit_cache=True,
                    reset_stateful_strategy=True
                )
                await _edit_utbreakout_menu(query, "✅ 기본 UTBOT 전략으로 복귀")
                return

            if action == 'auto':
                enabled = str(value or '').lower() in {'on', 'enable', '1', 'true'}
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'auto_select_enabled'],
                    enabled
                )
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'selection_mode'],
                    'auto' if enabled else 'manual'
                )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await _edit_utbreakout_menu(query, f"✅ AUTO Set 선택: {'ON' if enabled else 'OFF'}")
                return

            if action == 'set' or re.fullmatch(r'set\d+', action or ''):
                set_arg = value if action == 'set' else action
                set_id = normalize_utbreakout_set_id(set_arg, UTBREAKOUT_DEFAULT_SET_ID)
                if set_id > UTBREAKOUT_ACTIVE_SET_MAX:
                    await _edit_utbreakout_menu(query, "❌ 현재 선택 가능한 범위는 Set 1~50입니다.")
                    return
                profile_cfg = _merge_utbreakout_profile_with_preserved_settings(f"set{set_id}")
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1'],
                    profile_cfg
                )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                info = get_utbreakout_set_definition(set_id)
                await _edit_utbreakout_menu(query, f"✅ Set{set_id} 적용: {info.get('name')} (AUTO OFF)")
                return

            if action == 'sets':
                try:
                    await query.edit_message_text(
                        _format_utbreakout_sets_text(value or 1),
                        reply_markup=_build_utbreakout_keyboard()
                    )
                except BadRequest as md_err:
                    if "message is not modified" in str(md_err).lower():
                        return
                    raise
                return

            if action == 'why':
                try:
                    await query.edit_message_text(
                        _format_utbreakout_why_text(),
                        reply_markup=_build_utbreakout_keyboard()
                    )
                except BadRequest as md_err:
                    if "message is not modified" in str(md_err).lower():
                        return
                    raise
                return

            if action in {'risk', 'riskpct', 'dailyloss'}:
                try:
                    numeric_value = float(str(value or '').replace('$', '').replace(',', '').strip())
                except (TypeError, ValueError):
                    await _edit_utbreakout_menu(query, "❌ 버튼 값 처리 실패")
                    return
                if numeric_value <= 0:
                    await _edit_utbreakout_menu(query, "❌ 손실 설정값은 0보다 커야 합니다.")
                    return
                if action == 'risk':
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'max_risk_per_trade_usdt'],
                        numeric_value
                    )
                    notice = f"✅ 1회 최대 손실 설정: ${numeric_value:.2f}"
                elif action == 'riskpct':
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'risk_per_trade_percent'],
                        numeric_value
                    )
                    notice = f"✅ 잔고 대비 손실 기준 설정: {numeric_value:.2f}%"
                else:
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'daily_max_loss_usdt'],
                        numeric_value
                    )
                    notice = f"✅ 하루 최대 손실 설정: ${numeric_value:.2f}"
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await _edit_utbreakout_menu(query, notice)
                return

            if action in {'opphold', 'opppnl'}:
                if action == 'opppnl' and str(value or '').strip().lower() in {'off', 'disable', 'false'}:
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_set_exit_min_pnl_enabled'],
                        False
                    )
                    self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                    await _edit_utbreakout_menu(query, "✅ 반대Set청산 PnL 조건: OFF")
                    return
                try:
                    numeric_value = float(str(value or '').replace('$', '').replace(',', '').strip())
                except (TypeError, ValueError):
                    await _edit_utbreakout_menu(query, "❌ 버튼 값 처리 실패")
                    return
                if action == 'opphold':
                    if numeric_value < 0 or numeric_value != int(numeric_value):
                        await _edit_utbreakout_menu(query, "❌ 최소 보유 캔들은 0 이상의 정수여야 합니다.")
                        return
                    hold_value = int(numeric_value)
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_set_exit_min_hold_candles'],
                        hold_value
                    )
                    notice = f"✅ 반대Set청산 최소 보유: {hold_value}캔들"
                else:
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_set_exit_min_pnl_enabled'],
                        True
                    )
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_set_exit_min_pnl_usdt'],
                        numeric_value
                    )
                    notice = f"✅ 반대Set청산 PnL 조건: ON / 최소 ${numeric_value:.2f}"
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await _edit_utbreakout_menu(query, notice)
                return

            if action == 'toggle_opposite':
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_signal_exit_enabled'],
                    False
                )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await _edit_utbreakout_menu(query, "✅ 단순 반대UT청산(A)은 기각 옵션이라 OFF로 유지합니다.")
                return

            if action in {'toggle_opposite_set', 'toggle_ema', 'toggle_extreme'}:
                key_map = {
                    'toggle_opposite_set': ('opposite_set_exit_enabled', '반대Set청산'),
                    'toggle_ema': ('ema_rsi_exit_enabled', 'EMA50/RSI 청산'),
                    'toggle_extreme': ('exclude_rsi_extreme', 'RSI 과열 제외 옵션')
                }
                key, label = key_map[action]
                current = bool(_current_utbreakout_cfg().get(key, False))
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', key],
                    not current
                )
                if action == 'toggle_opposite_set' and not current:
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_signal_exit_enabled'],
                        False
                    )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await _edit_utbreakout_menu(query, f"✅ {label}: {'ON' if not current else 'OFF'}")
                return

            if action == 'download':
                await _send_utbreakout_log_document(query.message)
                return

            if action == 'research_download':
                await _send_utbreakout_research_document(query.message)
                return

            if action == 'research':
                try:
                    await query.edit_message_text(
                        _format_utbreakout_research_text(),
                        reply_markup=_build_utbreakout_keyboard()
                    )
                except BadRequest as md_err:
                    if "message is not modified" in str(md_err).lower():
                        return
                    raise
                return

            if action == 'condition_status':
                await _edit_utbreakout_condition_status(query)
                return

            await _edit_utbreakout_menu(query)

        async def help_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            msg = """
📚 **명령어**

/start - 메인 메뉴 표시
/setup - 설정 메뉴
/status - 현재 상태 조회
/history - 지난 상태 조회
/stats - 통계
/utbreakout - UTBOT_FILTERED_BREAKOUT_V1 전용 메뉴
/log - 최근 로그
/close - 긴급 청산

🚨 STOP - 긴급 정지
⏸ PAUSE - 일시정지
▶ RESUME - 재개
"""
            await u.message.reply_text(msg.strip(), parse_mode=ParseMode.MARKDOWN)

        emergency_handler = MessageHandler(filters.Regex(r"STOP|PAUSE|RESUME"), self.global_handler)
        self.tg_app.add_handler(emergency_handler, group=-1)

        self.tg_app.add_handler(CommandHandler("start", start_cmd))
        self.tg_app.add_handler(CommandHandler("strat", strat_cmd))
        self.tg_app.add_handler(CommandHandler("status", status_cmd))
        self.tg_app.add_handler(CommandHandler("history", history_cmd))
        self.tg_app.add_handler(CommandHandler("log", log_cmd))
        self.tg_app.add_handler(CommandHandler("close", close_cmd))
        self.tg_app.add_handler(CommandHandler("stats", stats_cmd))
        self.tg_app.add_handler(CommandHandler("utbreakout", utbreakout_cmd))
        self.tg_app.add_handler(CallbackQueryHandler(utbreakout_callback, pattern=r"^utb:"))
        self.tg_app.add_handler(CommandHandler("help", help_cmd))

        setup_command_handler = CommandHandler('setup', self.setup_entry)
        setup_text_handler = MessageHandler(filters.Regex(setup_trigger_pattern), self.setup_entry)

        conv = ConversationHandler(
            entry_points=[
                setup_command_handler,
                setup_text_handler
            ],
            allow_reentry=True,
            states={
                SELECT: [MessageHandler(setup_text_filter, self.setup_select)],
                INPUT: [MessageHandler(setup_text_filter, self.setup_input)],
                SYMBOL_INPUT: [MessageHandler(setup_text_filter, self.setup_symbol_input)],
                DIRECTION_SELECT: [MessageHandler(setup_text_filter, self.setup_direction_select)],
                ENGINE_SELECT: [MessageHandler(setup_text_filter, self.setup_engine_select)]
            },
            fallbacks=[
                setup_command_handler,
                setup_text_handler,
                emergency_handler
            ]
        )

        self.tg_app.add_handler(conv)

        async def menu_button_handler(u: Update, c: ContextTypes.DEFAULT_TYPE):
            command = self._extract_telegram_command(u.message.text if u and u.message else "")
            if command == "/status":
                return await status_cmd(u, c)
            if command == "/history":
                return await history_cmd(u, c)
            if command == "/log":
                return await log_cmd(u, c)
            if command == "/help":
                return await help_cmd(u, c)
            if command == "/stats":
                return await stats_cmd(u, c)
            if command == "/close":
                return await close_cmd(u, c)
            if command == "/utbreakout":
                return await utbreakout_cmd(u, c)
            return None

        self.tg_app.add_handler(
            MessageHandler(
                filters.Regex(menu_trigger_pattern),
                menu_button_handler
            )
        )

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
                if reporting.get('periodic_reports_enabled', False) and reporting.get('hourly_report_enabled', False):
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
        logger.info("Legacy Telegram dashboard loop disabled; manual /status only.")
        return


    async def _refresh_dashboard(self, force=False):
        logger.info(
            f"Legacy Telegram dashboard refresh ignored (force={force}); manual /status only."
        )
        return False

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
                    tp_expected = bool(protection_cfg.get('tp_expected', protection_cfg.get('tp_enabled', False)))
                    sl_expected = bool(protection_cfg.get('sl_expected', protection_cfg.get('sl_enabled', False)))
                    tp_present = bool(protection_cfg.get('tp_present', False))
                    sl_present = bool(protection_cfg.get('sl_present', False))
                    tp_text = "OK" if tp_expected and tp_present else "누락" if tp_expected else "OFF"
                    sl_text = "OK" if sl_expected and sl_present else "누락" if sl_expected else "OFF"
                    audit_status = protection_cfg.get('audit_status', '-')
                    network = d.get('network', '?')
                    exchange_id = d.get('exchange_id', '?')
                    market_data_exchange_id = d.get('market_data_exchange_id', '?')
                    market_data_source = d.get('market_data_source', 'BINANCE FUTURES MAINNET PUBLIC')
                    msg += f"⏱ TF: 진입 `{e_tf}` / 청산 `{x_tf}`\n"
                    msg += f"🌐 거래소: `{exchange_id}` | 네트워크 `{network}`\n"
                    msg += f"📡 신호데이터: `{market_data_exchange_id}` | `{market_data_source}`\n"
                    msg += f"🛡 보호주문: TP `{tp_text}`({protection_cfg.get('tp_count', 0)}) | SL `{sl_text}`({protection_cfg.get('sl_count', 0)}) | audit `{audit_status}`\n"
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
                        pending_visible = any([
                            stateful_diag.get('ut_pending_source'),
                            stateful_diag.get('ut_pending_side'),
                            stateful_diag.get('ut_pending_state'),
                            stateful_diag.get('ut_pending_execute_ts_human')
                        ])
                        if pending_visible:
                            pending_line = "UT pending: "
                            if stateful_diag.get('ut_pending_source'):
                                pending_line += f"source `{stateful_diag.get('ut_pending_source')}` | "
                            pending_line += (
                                f"side `{stateful_diag.get('ut_pending_side', '-')}` | "
                                f"state `{stateful_diag.get('ut_pending_state', '-')}` | "
                                f"execute `{stateful_diag.get('ut_pending_execute_ts_human', '-')}`"
                            )
                            if stateful_diag.get('ut_pending_expires_ts_human'):
                                pending_line += f" | expires `{stateful_diag.get('ut_pending_expires_ts_human')}`"
                            msg += pending_line + "\n"
                        if stateful_diag.get('ut_flip_target_side'):
                            flip_line = (
                                f"UTBOT flip: target `{stateful_diag.get('ut_flip_target_side')}` | "
                                f"signal `{stateful_diag.get('ut_flip_signal_ts_human', '-')}`"
                            )
                            if stateful_diag.get('ut_flip_execute_ts_human'):
                                flip_line += f" | execute `{stateful_diag.get('ut_flip_execute_ts_human')}`"
                            if stateful_diag.get('ut_flip_expires_ts_human'):
                                flip_line += f" | expires `{stateful_diag.get('ut_flip_expires_ts_human')}`"
                            if stateful_diag.get('ut_flip_retry_count') is not None:
                                flip_line += f" | retry `{stateful_diag.get('ut_flip_retry_count')}`"
                            msg += flip_line + "\n"
                        if stateful_diag.get('ut_flip_block_reason'):
                            msg += f"UTBOT flip 차단사유: `{stateful_diag.get('ut_flip_block_reason')}`\n"
                        if (
                            stateful_diag.get('utsmc_entry_bullish_ob_entry') is not None
                            or stateful_diag.get('utsmc_entry_bearish_ob_entry') is not None
                            or stateful_diag.get('utsmc_entry_ob_tags')
                        ):
                            smc_entry_bull = 'Y' if stateful_diag.get('utsmc_entry_bullish_ob_entry') else 'N'
                            smc_entry_bear = 'Y' if stateful_diag.get('utsmc_entry_bearish_ob_entry') else 'N'
                            smc_entry_tf = stateful_diag.get('utsmc_entry_tf')
                            smc_entry_line = f"UTSMC entry OB{f'[{smc_entry_tf}]' if smc_entry_tf else ''}: bull `{smc_entry_bull}` | bear `{smc_entry_bear}`"
                            if stateful_diag.get('utsmc_entry_ob_tags'):
                                smc_entry_line += f" | tags `{stateful_diag.get('utsmc_entry_ob_tags')}`"
                            msg += smc_entry_line + "\n"
                        if (
                            stateful_diag.get('utsmc_entry_bullish_ob_bottom') is not None
                            or stateful_diag.get('utsmc_entry_bullish_ob_top') is not None
                            or stateful_diag.get('utsmc_entry_bearish_ob_bottom') is not None
                            or stateful_diag.get('utsmc_entry_bearish_ob_top') is not None
                        ):
                            msg += (
                                f"UTSMC entry OB range: "
                                f"bull `{float(stateful_diag.get('utsmc_entry_bullish_ob_bottom') or 0.0):.2f}` ~ "
                                f"`{float(stateful_diag.get('utsmc_entry_bullish_ob_top') or 0.0):.2f}` | "
                                f"bear `{float(stateful_diag.get('utsmc_entry_bearish_ob_bottom') or 0.0):.2f}` ~ "
                                f"`{float(stateful_diag.get('utsmc_entry_bearish_ob_top') or 0.0):.2f}`\n"
                            )
                        if stateful_diag.get('utsmc_entry_smc_reason'):
                            msg += f"UTSMC entry 판정: `{stateful_diag.get('utsmc_entry_smc_reason')}`\n"
                        if stateful_diag.get('utsmc_candidate_filter_mode'):
                            c1_long = 'PASS' if stateful_diag.get('utsmc_candidate1_long_pass') else 'FAIL'
                            c2_long = 'PASS' if stateful_diag.get('utsmc_candidate2_long_pass') else 'FAIL'
                            final_long = 'PASS' if stateful_diag.get('utsmc_candidate_final_long_pass') else 'FAIL'
                            c1_short = 'PASS' if stateful_diag.get('utsmc_candidate1_short_pass') else 'FAIL'
                            c2_short = 'PASS' if stateful_diag.get('utsmc_candidate2_short_pass') else 'FAIL'
                            final_short = 'PASS' if stateful_diag.get('utsmc_candidate_final_short_pass') else 'FAIL'
                            msg += (
                                f"UTSMC 후보필터: mode `{stateful_diag.get('utsmc_candidate_filter_mode')}` | "
                                f"LONG `C1 {c1_long} / C2 {c2_long} / FINAL {final_long}` | "
                                f"SHORT `C1 {c1_short} / C2 {c2_short} / FINAL {final_short}`\n"
                            )
                        if stateful_diag.get('utsmc_signal_high') is not None or stateful_diag.get('utsmc_signal_low') is not None:
                            msg += (
                                f"UTSMC invalidation: high `{float(stateful_diag.get('utsmc_signal_high') or 0.0):.2f}` | "
                                f"low `{float(stateful_diag.get('utsmc_signal_low') or 0.0):.2f}`\n"
                            )
                        if (
                            stateful_diag.get('smc_bullish_ob_entry') is not None
                            or stateful_diag.get('smc_bearish_ob_entry') is not None
                            or stateful_diag.get('smc_ob_tags')
                        ):
                            smc_bull = 'Y' if stateful_diag.get('smc_bullish_ob_entry') else 'N'
                            smc_bear = 'Y' if stateful_diag.get('smc_bearish_ob_entry') else 'N'
                            smc_tf = stateful_diag.get('smc_tf')
                            smc_line = f"UTSMC exit OB{f'[{smc_tf}]' if smc_tf else ''}: bull `{smc_bull}` | bear `{smc_bear}`"
                            if stateful_diag.get('smc_ob_tags'):
                                smc_line += f" | tags `{stateful_diag.get('smc_ob_tags')}`"
                            msg += smc_line + "\n"
                        if (
                            stateful_diag.get('smc_bullish_ob_bottom') is not None
                            or stateful_diag.get('smc_bullish_ob_top') is not None
                            or stateful_diag.get('smc_bearish_ob_bottom') is not None
                            or stateful_diag.get('smc_bearish_ob_top') is not None
                        ):
                            msg += (
                                f"UTSMC exit OB range: "
                                f"bull `{float(stateful_diag.get('smc_bullish_ob_bottom') or 0.0):.2f}` ~ "
                                f"`{float(stateful_diag.get('smc_bullish_ob_top') or 0.0):.2f}` | "
                                f"bear `{float(stateful_diag.get('smc_bearish_ob_bottom') or 0.0):.2f}` ~ "
                                f"`{float(stateful_diag.get('smc_bearish_ob_top') or 0.0):.2f}`\n"
                            )
                        if stateful_diag.get('smc_reason'):
                            msg += f"UTSMC exit 판정: `{stateful_diag.get('smc_reason')}`\n"
                        if stateful_diag.get('smc_reason_ts_human'):
                            msg += f"UTSMC exit 판정시각: `{stateful_diag.get('smc_reason_ts_human')}`\n"
                        diag_note = stateful_diag.get('note')
                        if diag_note:
                            msg += f"진단메모: `{diag_note}`\n"

                    active_strat = d.get('active_strategy', '')
                    utbot_filter_pack = d.get('utbot_filter_pack', {}) or {}
                    utbot_filter_cfg = utbot_filter_pack.get('config', {}) if isinstance(utbot_filter_pack, dict) else {}
                    utbot_rsi_momentum_filter = d.get('utbot_rsi_momentum_filter', {}) or {}
                    utbot_rsi_cfg = utbot_rsi_momentum_filter.get('config', {}) if isinstance(utbot_rsi_momentum_filter, dict) else {}
                    if active_strat == 'UTBOT' and utbot_filter_cfg:
                        utbot_entry_cfg = utbot_filter_cfg.get('entry', {})
                        utbot_exit_cfg = utbot_filter_cfg.get('exit', {})
                        utbot_entry_eval = utbot_filter_pack.get('entry', {}) if isinstance(utbot_filter_pack.get('entry', {}), dict) else {}
                        utbot_exit_eval = utbot_filter_pack.get('exit', {}) if isinstance(utbot_filter_pack.get('exit', {}), dict) else {}
                        entry_selected = normalize_utbot_filter_pack_selected(utbot_entry_cfg.get('selected', []))
                        exit_selected = normalize_utbot_filter_pack_selected(utbot_exit_cfg.get('selected', []))
                        msg += (
                            f"🧪 UTBot 필터(진입): `{format_utbot_filter_pack_selected(entry_selected)}` "
                            f"`{format_utbot_filter_pack_logic(utbot_entry_cfg.get('logic', 'and'))}`\n"
                        )
                        msg += (
                            f"🧪 UTBot 필터(청산): `{format_utbot_filter_pack_selected(exit_selected)}` "
                            f"`{format_utbot_filter_pack_logic(utbot_exit_cfg.get('logic', 'and'))}` | "
                            f"`{format_utbot_filter_pack_mode_map(utbot_exit_cfg.get('mode_by_filter', {}), exit_selected)}`\n"
                        )

                        for filter_id in entry_selected:
                            filter_detail = (utbot_entry_eval.get('filters') or {}).get(str(filter_id), {})
                            label = filter_detail.get('label', get_utbot_filter_pack_label(filter_id))
                            long_status = 'PASS' if filter_detail.get('long_pass') else 'FAIL'
                            short_status = 'PASS' if filter_detail.get('short_pass') else 'FAIL'
                            msg += (
                                f"UTBot 진입 {filter_id} `{label}`: "
                                f"LONG `{long_status}` ({filter_detail.get('reason_long', '-')}) | "
                                f"SHORT `{short_status}` ({filter_detail.get('reason_short', '-')})\n"
                            )

                        exit_mode_map = utbot_exit_cfg.get('mode_by_filter', {})
                        for filter_id in exit_selected:
                            filter_detail = (utbot_exit_eval.get('filters') or {}).get(str(filter_id), {})
                            label = filter_detail.get('label', get_utbot_filter_pack_label(filter_id))
                            mode_label = normalize_utbot_filter_pack_exit_mode(
                                exit_mode_map.get(str(filter_id), exit_mode_map.get(filter_id, 'confirm'))
                            )
                            long_status = 'PASS' if filter_detail.get('long_pass') else 'FAIL'
                            short_status = 'PASS' if filter_detail.get('short_pass') else 'FAIL'
                            msg += (
                                f"UTBot 청산 {filter_id} `{label}`[{mode_label.upper()}]: "
                                f"LONG `{long_status}` ({filter_detail.get('reason_long', '-')}) | "
                                f"SHORT `{short_status}` ({filter_detail.get('reason_short', '-')})\n"
                            )

                    if active_strat == 'UTBOT' and utbot_rsi_cfg.get('enabled'):
                        rsi_entry_eval = utbot_rsi_momentum_filter.get('entry', {}) if isinstance(utbot_rsi_momentum_filter.get('entry', {}), dict) else {}
                        rsi_exit_eval = utbot_rsi_momentum_filter.get('exit', {}) if isinstance(utbot_rsi_momentum_filter.get('exit', {}), dict) else {}
                        msg += (
                            f"🧪 RSI Momentum Trend: `ON` | "
                            f"`RSI {utbot_rsi_cfg.get('rsi_length', 14)}` / "
                            f"`P>{float(utbot_rsi_cfg.get('positive_above', 65) or 65):.1f}` / "
                            f"`N<{float(utbot_rsi_cfg.get('negative_below', 32) or 32):.1f}` / "
                            f"`EMA {utbot_rsi_cfg.get('ema_period', 5)}`\n"
                        )
                        if rsi_entry_eval:
                            long_status = 'PASS' if rsi_entry_eval.get('long_pass') else 'FAIL'
                            short_status = 'PASS' if rsi_entry_eval.get('short_pass') else 'FAIL'
                            msg += (
                                f"RMT 진입: LONG `{long_status}` ({rsi_entry_eval.get('reason_long', '-')}) | "
                                f"SHORT `{short_status}` ({rsi_entry_eval.get('reason_short', '-')})\n"
                            )
                        if rsi_exit_eval:
                            long_status = 'PASS' if rsi_exit_eval.get('long_pass') else 'FAIL'
                            short_status = 'PASS' if rsi_exit_eval.get('short_pass') else 'FAIL'
                            msg += (
                                f"RMT 청산확인: LONG `{long_status}` ({rsi_exit_eval.get('reason_long', '-')}) | "
                                f"SHORT `{short_status}` ({rsi_exit_eval.get('reason_short', '-')})\n"
                            )

                    f_cfg = d.get('filter_config', {})
                    if f_cfg:
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

                    cand_mode = d.get('utsmc_candidate_filter_mode', 'OFF')
                    cand_status = d.get('utsmc_candidate_filter', {}) or {}
                    if active_strat == 'UTSMC' and cand_mode != 'OFF':
                        c1_final = 'PASS' if cand_status.get('candidate1_long_pass') or cand_status.get('candidate1_short_pass') else 'FAIL'
                        c2_final = 'PASS' if cand_status.get('candidate2_long_pass') or cand_status.get('candidate2_short_pass') else 'FAIL'
                        final_long = cand_status.get('candidate_final_long_pass')
                        final_short = cand_status.get('candidate_final_short_pass')
                        final_text = 'PASS' if final_long or final_short else 'FAIL'
                        msg += f"🧪 UTSMC 후보필터: {cand_mode} | C1 {c1_final} | C2 {c2_final} | FINAL {final_text}\n"

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
