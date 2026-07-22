"""SignalEngine runtime composition."""

from __future__ import annotations

from .base_engine import BaseEngine
from .signal_alpha import SignalAlphaMixin
from .signal_breakout_analysis import SignalBreakoutAnalysisMixin
from .signal_breakout_status import SignalBreakoutStatusMixin
from .signal_candles import SignalCandleMixin
from .signal_custom_entry import SignalCustomEntryMixin
from .signal_entry import SignalEntryMixin
from .signal_exit import SignalExitMixin
from .signal_filters import SignalFilterMixin
from .signal_position_lifecycle import SignalPositionLifecycleMixin
from .signal_protection import SignalProtectionMixin
from .signal_rspt import SignalRsptMixin
from .signal_runtime import SignalRuntimeMixin
from .signal_scanner import SignalScannerMixin
from .signal_secondary_strategies import SignalSecondaryStrategiesMixin
from .signal_ut_entry import SignalUtEntryMixin

class SignalEngine(
    SignalRuntimeMixin,
    SignalFilterMixin,
    SignalUtEntryMixin,
    SignalBreakoutAnalysisMixin,
    SignalPositionLifecycleMixin,
    SignalRsptMixin,
    SignalAlphaMixin,
    SignalBreakoutStatusMixin,
    SignalSecondaryStrategiesMixin,
    SignalScannerMixin,
    SignalCandleMixin,
    SignalCustomEntryMixin,
    SignalEntryMixin,
    SignalProtectionMixin,
    SignalExitMixin,
    BaseEngine,
):
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
        self.last_utbreakout_no_position_retry_ts = {}
        self.utbreakout_entry_trace = {}
        self.utbreakout_last_ready_ts = {}
        self.utbreakout_last_ready_side = {}
        self.utbreakout_daily_sl_symbol_lockouts = {}
        self.utbreakout_recent_loss_symbol_cooldowns = {}
        self.utbreakout_last_order_attempt_ts = {}
        self.utbreakout_last_watchdog_report_ts = {}
        self.utbreakout_trace_watchdog_enabled = False
        self.utbreakout_auto_entry_bridge_last_attempt_ts = {}
        self.utbreakout_auto_entry_bridge_enabled = True
        self.utbreakout_last_status_symbol = None
        self.utbreakout_last_ready_symbol = None
        self.current_utbreakout_candidate_symbol = None
        self.utbreakout_status_symbol_source = None
        self.utbreakout_status_symbol_detail = None
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
        self.user_custom_entry_lock = asyncio.Lock()

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
        self.utbreakout_trailing_states = {}  # symbol -> partial TP / ATR trailing state
        self.aggressive_growth_positions = {}  # symbol -> aggressive-only sleeve tracking
        self.aggressive_growth_high_watermark = 0.0
        self.utbot_filtered_breakout_failures = {}  # symbol -> side -> recent failed candidate timestamps
        self.utbreakout_futures_context_cache = {}  # symbol -> cached funding/OI context for research logs
        self.utbreakout_orderflow_snapshots = {}  # symbol -> rolling depth/orderflow snapshots
        self.utbreakout_market_regime_cache = {}  # cache key -> BTC/ETH broad market regime
        self.utbreakout_shadow_pending = {}  # key -> pending triple-barrier observation
        self.utbreakout_shadow_resolved_keys = set()  # keys already logged in this runtime
        self.utbreakout_shadow_stats_cache = {}  # cache key -> recent shadow stats
        self.utbreakout_runner_stats_cache = {}  # cache key -> recent runner stats
        self.utbreakout_profit_alpha_meta_stats = {}  # side:engine -> realized R stats
        self.relative_strength_pullback_states = {}  # symbol -> breakout/pullback wait state
        self.relative_strength_pullback_last_decisions = {}  # symbol -> latest shadow/live decision
        self.relative_strength_pullback_eval_cache = {}  # short-lived scanner evaluation cache
        self.dual_alpha_last_status = {}  # symbol -> latest dual alpha selection summary
        self.qh_flow_signal_cache = {}  # symbol:boundary -> live quarter-hour evaluation
        self.qh_flow_last_status = {}  # symbol -> latest QH-Flow status
        self.l2_gate_cache = {}  # symbol -> short-lived shared L2 state
        self.l2_gate_history = {}  # symbol -> dynamic L2 replenishment/depletion samples
        self.crowding_unwind_last_status = {}  # symbol -> latest funding/OI unwind status
        self.liquidation_exhaustion_reversal_last_status = {}  # symbol -> latest LXR status
        self.strategy_allocator_last_status = {}  # strategy -> adaptive risk summary
        self.strategy_allocator_cache = {}  # short-lived finalized trade summaries
        self.triple_alpha_last_status = {}  # symbol -> latest triple strategy summary
        self.quad_alpha_last_status = {}  # symbol -> latest five-strategy agreement summary
        self.last_live_entry_snapshot = {}  # latest confirmed live entry, for status fallback only
        self.utbreakout_adaptive_tf_state = {}  # symbol -> selected TF stability state
        self.utbreakout_adaptive_last_decision_ts = {}  # symbol -> tf -> last closed candle evaluated
        self.utbreakout_last_selected_set_ids = {}  # symbol -> last validated AUTO Set
        self.last_protection_order_status = {}  # symbol -> exchange-side TP/SL audit status
        self.last_protection_alert_ts = {}  # symbol:kind -> last Telegram alert timestamp
        self.protection_missing_candidates = {}  # symbol -> missing TP/SL debounce candidates
        self.last_orphan_protection_sweep_ts = 0.0
        self.orphan_protection_candidates = {}  # normalized symbol -> first-seen orphan order signature
        self.flat_protected_state_candidates = {}  # client ID -> first confirmed exchange-flat observation
        self.ORPHAN_PROTECTION_SWEEP_INTERVAL = 10.0
        self.coin_selector_last_result = {}  # runtime CoinSelector V2 report
        self.coin_selector_symbol_scores = {}  # normalized symbol -> latest selector score
        self.coin_selector_last_run_ts = 0.0
        self.coin_selector_candidate_cooldowns = {}  # normalized symbol -> no-entry miss / cooldown state
        self.coin_selector_analysis_cursor = 0
        self.coin_selector_strategy_cursor = 0
        self.coin_selector_rate_limit_backoff_until = 0.0
        self._load_utbreakout_daily_sl_lockouts()
        self._load_utbreakout_profit_alpha_meta_stats()
        self.micro_auto_last_plan = {}  # symbol -> latest accepted Micro Auto plan
        self.micro_auto_last_rejects = {}  # symbol -> latest Micro Auto reject payload
        self.micro_auto_last_scan = {}  # latest Micro Auto feasibility scan report
        self.last_entry_reason = {}        # symbol -> latest entry decision reason
        self.trading_state_store = None
        self.idempotent_order_gateway = None
        self.user_data_stream = None
        self.crypto_safety_startup_task = None
        self.crypto_entry_lock_reason = 'RECONCILIATION_REQUIRED'

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
        try:
            loop = asyncio.get_running_loop()
            self.crypto_safety_startup_task = loop.create_task(
                self._startup_crypto_safety_reconciliation(),
                name='crypto-trading-safety-startup-reconciliation',
            )
        except RuntimeError:
            logger.warning("Crypto startup reconciliation skipped: no running event loop")

__all__ = (
    'SignalEngine',
)
