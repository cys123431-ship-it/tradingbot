"""MainController runtime composition."""

from __future__ import annotations

from pathlib import Path

from .controller_automatic_controls import ControllerAutomaticTradingControlsMixin
from .controller_custom_entry import ControllerCustomEntryMixin
from .controller_emergency import ControllerEmergencyMixin
from .controller_exchange import ControllerExchangeMixin
from .controller_reporting import ControllerReportingMixin
from .controller_telegram import ControllerTelegramMixin
from .controller_telegram_setup import TelegramSetupMixin

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


class MainController(
    ControllerExchangeMixin,
    ControllerTelegramMixin,
    ControllerAutomaticTradingControlsMixin,
    ControllerCustomEntryMixin,
    TelegramSetupMixin,
    ControllerReportingMixin,
    ControllerEmergencyMixin,
):
    def __init__(self):
        # Keep paths compatible with the original repository-root ``emas.py``.
        self.base_dir = str(REPOSITORY_ROOT)
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
        try:
            signal_engine = self.engines[CORE_ENGINE]
            effective_cfg = signal_engine._get_utbot_filtered_breakout_config(
                signal_engine.get_runtime_strategy_params()
            )
            logger.warning(
                "UTBREAK_EFFECTIVE_STATUS_CONTRACT | %s",
                " | ".join(build_utbreakout_effective_status_contract(effective_cfg)),
            )
        except Exception as exc:
            logger.warning("UTBreak effective status contract startup check failed: %s", exc)
        logger.info("Core mode enabled: Signal(SMA/HMA/CAMERON) + risk controls only. Legacy engines archived.")
        self.active_engine = None
        self.tg_app = None
        self.status_data = {}
        self.is_paused = bool(self.prev_paused_state) if self.prev_paused_state is not None else False
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
        self.utbreakout_futures_context_cache = {}
        self.utbreakout_orderflow_snapshots = {}
        self.exchange_switch_lock = asyncio.Lock()
        self.last_exchange_switch_ts = 0.0
        self.exchange_switch_cooldown_seconds = 30

__all__ = (
    'MainController',
)
