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
import inspect
import re
import hashlib
import math
import copy
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
from zoneinfo import ZoneInfo
from bot_runtime import controller_emergency as _controller_emergency_module
from bot_runtime.assembly import bind_runtime_methods
from bot_runtime import controller as _controller_module
from bot_runtime import controller_custom_entry as _controller_custom_entry_module
from bot_runtime import controller_exchange as _controller_exchange_module
from bot_runtime import controller_reporting as _controller_reporting_module
from bot_runtime import controller_telegram as _controller_telegram_module
from bot_runtime import controller_telegram_setup as _controller_telegram_setup_module
from bot_runtime import diagnostics as _diagnostics_module
from bot_runtime import live_context as _live_context_module
from bot_runtime import live_execution as _live_execution_module
from bot_runtime import live_orders as _live_orders_module
from bot_runtime import live_position_manager as _live_position_manager_module
from bot_runtime import live_risk as _live_risk_module
from bot_runtime import live_safety as _live_safety_module
from bot_runtime import runtime_profile as _runtime_profile_module
from bot_runtime import strategy_registry as _strategy_registry_module
from bot_runtime import signal_alpha as _signal_alpha_module
from bot_runtime import signal_engine as _signal_engine_module
from bot_runtime import signal_breakout_analysis as _signal_breakout_analysis_module
from bot_runtime import signal_breakout_status as _signal_breakout_status_module
from bot_runtime import signal_candles as _signal_candles_module
from bot_runtime import signal_custom_entry as _signal_custom_entry_module
from bot_runtime import signal_entry as _signal_entry_module
from bot_runtime import signal_exit as _signal_exit_module
from bot_runtime import signal_filters as _signal_filters_module
from bot_runtime import signal_position_lifecycle as _signal_position_lifecycle_module
from bot_runtime import signal_protection as _signal_protection_module
from bot_runtime import signal_rspt as _signal_rspt_module
from bot_runtime import signal_runtime as _signal_runtime_module
from bot_runtime import signal_scanner as _signal_scanner_module
from bot_runtime import signal_secondary_strategies as _signal_secondary_strategies_module
from bot_runtime import signal_ut_entry as _signal_ut_entry_module
from bot_runtime import base_engine as _base_engine_module
from bot_runtime import configuration as _configuration_module
from bot_runtime import database as _database_module
from bot_runtime import legacy_engines as _legacy_engines_module
from bot_runtime.base_engine import BaseEngine
from bot_runtime.configuration import TradingConfig
from bot_runtime.controller import MainController
from bot_runtime.controller_custom_entry import ControllerCustomEntryMixin
from bot_runtime.controller_emergency import ControllerEmergencyMixin
from bot_runtime.controller_exchange import ControllerExchangeMixin
from bot_runtime.controller_reporting import ControllerReportingMixin
from bot_runtime.controller_telegram import ControllerTelegramMixin
from bot_runtime.controller_telegram_setup import TelegramSetupMixin
from bot_runtime.diagnostics import *
from bot_runtime.live_context import *
from bot_runtime.live_execution import *
from bot_runtime.live_orders import *
from bot_runtime.live_position_manager import *
from bot_runtime.live_risk import *
from bot_runtime.live_safety import *
from bot_runtime.runtime_profile import *
from bot_runtime.strategy_registry import *
from bot_runtime.database import DBManager
from bot_runtime.legacy_engines import (
    DualModeFractalEngine,
    DualThrustEngine,
    ShannonEngine,
    TemaEngine,
)
from bot_runtime.signal_alpha import SignalAlphaMixin
from bot_runtime.signal_breakout_analysis import SignalBreakoutAnalysisMixin
from bot_runtime.signal_breakout_status import SignalBreakoutStatusMixin
from bot_runtime.signal_candles import SignalCandleMixin
from bot_runtime.signal_custom_entry import SignalCustomEntryMixin
from bot_runtime.signal_entry import SignalEntryMixin
from bot_runtime.signal_exit import SignalExitMixin
from bot_runtime.signal_filters import SignalFilterMixin
from bot_runtime.signal_position_lifecycle import SignalPositionLifecycleMixin
from bot_runtime.signal_protection import SignalProtectionMixin
from bot_runtime.signal_rspt import SignalRsptMixin
from bot_runtime.signal_runtime import SignalRuntimeMixin
from bot_runtime.signal_scanner import SignalScannerMixin
from bot_runtime.signal_secondary_strategies import SignalSecondaryStrategiesMixin
from bot_runtime.signal_ut_entry import SignalUtEntryMixin
from bot_runtime.signal_engine import SignalEngine
from trading_safety import market_session as _market_session_module
from trading_safety.market_session import *
from bot_runtime.runtime_bindings import (
    bind_runtime_namespace,
    build_runtime_proxy,
    proxy_module_classes,
)
from trading_safety.entry_block import (
    CriticalPauseBlockDecision,
    EntrySubmitOutcome,
    canonical_futures_symbol,
    build_critical_pause_notice_key,
)
from trading_safety.order_gateway import IdempotentOrderGateway
from trading_safety.execution_service import CryptoExecutionService
from trading_safety.trade_accounting import (
    TradeAccountingFinalizer,
    rebuild_engine_performance_stats,
    record_closed_trade_accounting,
    resolve_closed_trade_accounting,
)
from trading_safety.time_utils import (
    timestamp_ms_or_none as _timestamp_ms_or_none,
)
from trading_safety.signal_lifecycle import (
    decision_timestamp as resolve_signal_decision_timestamp,
    records_contain_consumed_decision,
)
from trading_safety.monthly_report import (
    KST as MONTHLY_REPORT_KST,
    build_monthly_trade_report,
    previous_calendar_month,
)
from trading_safety.order_state import (
    ENTRY_BLOCKING_STATES,
    OrderState,
    SQLiteTradingStateStore,
    atomic_write_json,
)
from trading_safety.manual_resume import (
    ManualResumeResult,
    archive_critical_pause,
    archive_processed_request,
    load_manual_resume_request,
    write_manual_resume_request,
    write_manual_resume_result,
)
from trading_safety.reconciliation import reconcile_exchange_state
from trading_safety.user_data_stream import BinanceUserDataStream
from trading_safety.process_lock import ProcessLock, ProcessLockError
from trading_safety.binance_algo_gateway import (
    AlgoLookupStatus,
    BinanceAlgoOrderGateway,
    CONDITIONAL_TYPES,
    ProtectionOrderSnapshot,
    ProtectionOrderLookupUnavailable,
    normalize_futures_market_id,
)
from trading_safety.liquidation_guard import (
    as_decimal,
    estimate_isolated_liquidation_price,
    resolve_liquidation_safety_config,
    validate_stop_against_liquidation,
)
try:
    from dual_mode_fractal_strategy import DualModeFractalStrategy
    DUAL_MODE_AVAILABLE = True
except ImportError:
    DUAL_MODE_AVAILABLE = False
    logging.warning("?좑툘 dual_mode_fractal_strategy.py ?뚯씪???놁뒿?덈떎. ?대떦 ?꾨왂???ъ슜?섎젮硫??뚯씪??蹂듦뎄?섏꽭??")

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.error import BadRequest, TimedOut, RetryAfter
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    MessageHandler, filters, ConversationHandler, CallbackQueryHandler
)
from utbreakout.indicators import (
    bollinger_width_percentile,
    keltner_squeeze_state,
    previous_donchian,
)
from utbreakout.coinselector import (
    HARD_MIN_QUOTE_VOLUME_USDT as COIN_SELECTOR_HARD_MIN_QUOTE_VOLUME_USDT,
    build_base_candidate as build_coin_selector_base_candidate,
    build_selection_report as build_coin_selector_report,
    default_coin_selector_config,
    finalize_candidate as finalize_coin_selector_candidate,
    market_is_tradifi_perpetual as coin_selector_market_is_tradifi_perpetual,
    market_is_usdt_perpetual as coin_selector_market_is_usdt_perpetual,
    normalize_custom_symbols as normalize_coin_selector_custom_symbols,
    sector_tags_for_symbol as coin_selector_sector_tags_for_symbol,
)
from utbreakout.research import format_research_summary
from utbreakout.risk import (
    DEFAULT_MAX_RISK_PER_TRADE_PERCENT,
    DEFAULT_MIN_RISK_PER_TRADE_PERCENT,
    DEFAULT_RISK_PER_TRADE_PERCENT,
    calculate_risk_plan,
    normalize_risk_percent,
)
from utbreakout.risk_budget import (
    UTBREAKOUT_MAX_RISK_PER_TRADE_PERCENT,
    cap_utbreakout_risk_plan_to_margin,
    reconcile_utbreakout_risk_plan_to_order_qty,
    resolve_utbreakout_bridge_ready_age_sec,
    resolve_utbreakout_risk_budget,
)
from utbreakout.filter_config import (
    ALT_TREND_TIMEFRAME_ORDER,
    UTBOT_FILTER_PACK_ID_SET,
    UTBOT_FILTER_PACK_LABELS,
    format_alt_trend_timeframes,
    format_utbot_filter_pack_logic,
    format_utbot_filter_pack_mode_map,
    format_utbot_filter_pack_selected,
    get_utbot_filter_pack_label,
    normalize_alt_trend_timeframes,
    normalize_utbot_filter_pack_config,
    normalize_utbot_filter_pack_exit_mode,
    normalize_utbot_filter_pack_logic,
    normalize_utbot_filter_pack_selected,
)
from utbreakout.position_lifecycle import full_post_entry_closed_bars
from utbreakout.sizing import (
    build_aggressive_growth_overlay_plan,
    build_aggressive_growth_pyramid_plan,
    build_position_risk_multiplier,
    calculate_aggressive_sleeve_notional_cap,
    choose_aggressive_exit_split,
    evaluate_derivatives_growth_score,
    is_aggressive_symbol_trend_bullish,
)
from utbreakout.timeframe import HTF_MAP as UTBREAKOUT_HTF_MAP, select_adaptive_timeframe
from utbreakout.continuation_entry import evaluate_trend_continuation_entry
from utbreakout.adaptive import (
    build_dynamic_chandelier_stop,
    build_strategy_adaptation,
    build_strategy_quality_score,
    build_trend_health_score,
    evaluate_shadow_runner_exit,
    evaluate_shadow_triple_barrier,
    summarize_runner_outcomes,
    summarize_shadow_outcomes,
)
from utbreakout.micro_auto import (
    MICRO_AUTO_STRATEGY_KEY,
    assess_micro_market_feasibility,
    build_micro_entry_plan,
    default_micro_auto_config,
    normalize_micro_auto_config,
)
from utbreakout.engine_router import (
    evaluate_final_trade_decision,
    TradeDecision,
    LadderTP,
    should_disable_engine,
)
from utbreakout.relative_strength_pullback import (
    ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
    ENTRY_STRATEGY_UT_BREAKOUT,
    completed_candle_rows,
    default_relative_strength_pullback_config,
    evaluate_relative_strength_pullback_trend,
    resolve_entry_strategy,
)
from utbreakout.qh_flow import (
    QH_FLOW_STRATEGY,
    TRIPLE_ALPHA_STRATEGY,
    QUAD_ALPHA_STRATEGY,
    boundary_phase as qh_boundary_phase,
    default_qh_flow_config,
    evaluate_l2_gate,
    evaluate_qh_flow,
    quarter_hour_boundary_ms,
    summarize_agg_trades,
)
from utbreakout.crowding_unwind import (
    CROWDING_UNWIND_STRATEGY,
    default_crowding_unwind_config,
    evaluate_crowding_unwind,
)
from utbreakout.liquidation_exhaustion_reversal import (
    LXR_STRATEGY,
    default_liquidation_exhaustion_reversal_config,
    evaluate_liquidation_exhaustion_reversal,
)
from utbreakout.strategy_allocator import (
    default_strategy_allocator_config,
    evaluate_strategy_allocation,
    scale_plan_risk,
    summarize_strategy_trades,
)
from utbreakout.market_context import build_market_context
from utbreakout.exit_policy import evaluate_time_stop, evaluate_signal_invalid_exit
from utbreakout.ev_adaptive import (
    EV_ADAPTIVE_PROFILE_VERSION,
    adapt_exit_for_quantity,
    evaluate_ev_adaptive_entry,
    evaluate_mfe_profit_lock,
    evaluate_ev_time_stop,
    evaluate_net_edge,
    profile_gross_win_r,
    scale_atr_percent_threshold,
)
from utbreakout.alpha_engine import (
    EntryEdgeDecision,
    ProfitAlphaDecision,
    apply_profit_alpha_exit_overrides,
    build_entry_edge_decision,
    default_profit_alpha_config,
    evaluate_alpha_follow_through_exit,
    evaluate_profit_alpha,
)
from utbreakout.intelligence import evaluate_protection_health_engine
from utbreakout.macro_guard import is_macro_risk_window
from prediction import (
    PREDICTION_STRATEGY_CATALOG,
    PaperLedger,
    PredictAuthRequired,
    PredictClient,
    PredictionLiveCredentials,
    PredictionLiveOrderError,
    analyze_orderbook,
    build_prediction_micro_plan,
    check_live_preflight,
    default_prediction_micro_config,
    evaluate_paper_position_exit,
    estimate_crypto_up_probability,
    evaluate_prediction_edge,
    format_prediction_report,
    format_prediction_research_report,
    normalize_market,
    normalize_prediction_micro_config,
    prediction_reject_counts,
    provider_label,
    score_prediction_candidate,
    submit_live_market_order,
)

TELEGRAM_EMERGENCY_PATTERN = (
    r"(?i)^\s*(?:[^\w/]+\s*)?"
    r"(?:/(?:stop|pause|resume)(?:@[A-Za-z0-9_]+)?|STOP|PAUSE|RESUME)\s*$"
)
TELEGRAM_MENU_COMMAND_PATTERN = (
    r"^/(status|history|log|help|stats|close|utbreak|utbreakout|utbot|setup|"
    r"coinscan|customcoins|microauto|prediction|customentry|custom)(?:@[A-Za-z0-9_]+)?(?:\s.*)?$"
)
TELEGRAM_UTBREAK_INTEGRATED_COMMANDS = frozenset({
    "/utbot",
    "/coinscan",
    "/customcoins",
    "/microauto",
})
UTBREAKOUT_VISIBLE_CALLBACK_ACTIONS = frozenset({
    "on",
    "off",
    "condition_status",
    "dual_status",
    "rsp_status",
    "watchlist",
})
UTBREAKOUT_CALLBACK_ACTIONS = UTBREAKOUT_VISIBLE_CALLBACK_ACTIONS | frozenset({
    "pause",
    "resume",
    "fixed",
    "fixed_off",
    "auto_scan",
    "auto_scan_off",
    "coin_auto",
    "lev",
    "tf",
    "exit_tf",
    "target",
    "stop",
    "micro",
    "auto",
    "auto_bundle",
    "adaptive",
    "crowd",
    "crowding",
    "crowding_status",
    "lxr",
    "liquidation_reversal",
    "lxr_status",
    "dual",
    "dualt",
    "dual_status",
    "qh",
    "qhflow",
    "qh_status",
    "rsp",
    "rspt",
    "rsp_status",
    "triple",
    "triplet",
    "triple_status",
    "quad",
    "quadalpha",
    "quad_status",
    "qselect",
    "qsel",
    "set",
    "sets",
    "why",
    "dailytrades",
    "risk",
    "riskpct",
    "dailyloss",
    "opphold",
    "opppnl",
    "toggle_opposite",
    "toggle_opposite_set",
    "toggle_ema",
    "toggle_extreme",
    "download",
    "research_download",
    "research",
    "entry_analyze",
    "conditions",
    "status",
    "menu",
})

# ConversationHandler state IDs were process-wide globals in the original
# monolith.  Keep them in the composition root so every extracted Telegram
# mixin receives the same values through runtime namespace binding.
SELECT, INPUT, SYMBOL_INPUT, DIRECTION_SELECT, ENGINE_SELECT = range(5)

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


_LIVE_RUNTIME_MODULES = (
    _live_risk_module,
    _live_context_module,
    _live_execution_module,
    _live_safety_module,
    _live_orders_module,
    _live_position_manager_module,
)
for _live_runtime_module in _LIVE_RUNTIME_MODULES:
    for _live_runtime_name in _live_runtime_module.__all__:
        _live_runtime_value = globals().get(_live_runtime_name)
        if (
            inspect.isfunction(_live_runtime_value)
            and _live_runtime_value.__module__ == _live_runtime_module.__name__
        ):
            globals()[_live_runtime_name] = build_runtime_proxy(
                _live_runtime_value,
                modules=(_live_runtime_module,),
                namespace=globals(),
            )

for _runtime_profile_name in _runtime_profile_module.__all__:
    _runtime_profile_value = globals().get(_runtime_profile_name)
    if (
        inspect.isfunction(_runtime_profile_value)
        and _runtime_profile_value.__module__ == _runtime_profile_module.__name__
    ):
        globals()[_runtime_profile_name] = build_runtime_proxy(
            _runtime_profile_value,
            modules=(_runtime_profile_module,),
            namespace=globals(),
        )

_CLASS_RUNTIME_MODULES = (
    _configuration_module,
    _database_module,
    _base_engine_module,
    _legacy_engines_module,
    _signal_engine_module,
    _controller_module,
    _signal_runtime_module,
    _signal_filters_module,
    _signal_ut_entry_module,
    _signal_breakout_analysis_module,
    _signal_position_lifecycle_module,
    _signal_rspt_module,
    _signal_alpha_module,
    _signal_breakout_status_module,
    _signal_secondary_strategies_module,
    _signal_scanner_module,
    _signal_candles_module,
    _signal_custom_entry_module,
    _signal_entry_module,
    _signal_protection_module,
    _signal_exit_module,
    _controller_exchange_module,
    _controller_telegram_module,
    _controller_custom_entry_module,
    _controller_telegram_setup_module,
    _controller_reporting_module,
    _controller_emergency_module,
)
for _class_runtime_module in _CLASS_RUNTIME_MODULES:
    proxy_module_classes(_class_runtime_module, namespace=globals())

for _runtime_module in (
    *_LIVE_RUNTIME_MODULES,
    _runtime_profile_module,
    _configuration_module,
    _database_module,
    _base_engine_module,
    _legacy_engines_module,
    _signal_engine_module,
    _controller_module,
    _signal_runtime_module,
    _signal_filters_module,
    _signal_ut_entry_module,
    _signal_breakout_analysis_module,
    _signal_position_lifecycle_module,
    _signal_rspt_module,
    _signal_alpha_module,
    _signal_breakout_status_module,
    _signal_secondary_strategies_module,
    _signal_scanner_module,
    _signal_candles_module,
    _signal_custom_entry_module,
    _signal_entry_module,
    _signal_protection_module,
    _signal_exit_module,
    _controller_exchange_module,
    _controller_telegram_module,
    _controller_custom_entry_module,
    _controller_telegram_setup_module,
    _controller_reporting_module,
    _controller_emergency_module,
):
    bind_runtime_namespace(_runtime_module, globals())
bind_runtime_methods(globals(), logger=logger)


if __name__ == "__main__":
    if os.getenv('TRADINGBOT_OFFICIAL_LAUNCHER') != '1':
        raise SystemExit(
            'Direct execution is disabled. Use: python3 scripts/launch_emas.py'
        )
    controller = None
    direct_process_lock = None

    def _handle_exit_signal(signum, frame):
        signame = signal.Signals(signum).name
        if controller:
            controller.record_exit_marker(f"signal_{signame.lower()}", f"received {signame}")
        raise SystemExit(128 + signum)

    try:
        if os.getenv('TRADINGBOT_PROCESS_LOCK_HELD') != '1':
            direct_process_lock = ProcessLock('runtime/crypto_trading_bot.lock')
            try:
                direct_process_lock.acquire()
            except ProcessLockError as exc:
                logger.critical("DUPLICATE_PROCESS_DETECTED: %s", exc)
                raise SystemExit(73) from exc
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
        if direct_process_lock is not None:
            direct_process_lock.release()
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
