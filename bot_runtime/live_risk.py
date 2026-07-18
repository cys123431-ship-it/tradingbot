"""Live activation, pause state, risk controls, and order-plan models."""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
import math

@dataclass
class TakeProfitOrderPlan:
    tp_name: str
    side: str
    price: float
    qty: float
    reduce_only: bool
    close_position: bool
    r_multiple: float
    pct: float
    tp_index: int | None = None
    tp_label: str | None = None
    order_id: str | None = None
    client_order_id: str | None = None

@dataclass
class LiveOrderPlan:
    symbol: str
    side: str
    entry_type: str
    entry_price: float | None
    qty: float
    risk_pct: float
    initial_sl_price: float
    tp_orders: list
    ladder: object | None
    reduce_only: bool
    engine: str
    regime: str
    confidence: float
    expected_r: float
    reasons: list[str]

def apply_runtime_safety_defaults(cfg):
    if not isinstance(cfg, dict):
        return cfg
    mode = str(cfg.get("mode", "")).lower()
    is_live = mode in {"live", "real", "production"} or bool(cfg.get("live_trading", False))
    is_paper_or_testnet = mode in {"paper", "testnet", "dry_run"} or bool(cfg.get("testnet", False))

    if is_live:
        cfg["advanced_alpha_engine_enabled"] = bool(cfg.get("advanced_alpha_live_opt_in", False))
        cfg["live_parity_signal_enabled"] = bool(cfg.get("live_parity_signal_live_opt_in", False))
        cfg["adaptive_ladder_tp_enabled"] = bool(cfg.get("adaptive_ladder_tp_live_opt_in", False))

        if cfg.get("advanced_alpha_engine_enabled"):
            cfg["max_risk_per_trade_pct"] = min(
                float(cfg.get("max_risk_per_trade_pct", 0.25)),
                float(cfg.get("live_advanced_alpha_initial_max_risk_pct", 0.25)),
            )
    elif is_paper_or_testnet:
        cfg["advanced_alpha_engine_enabled"] = bool(cfg.get("paper_testnet_advanced_alpha_default_enabled", True))
        cfg["live_parity_signal_enabled"] = bool(cfg.get("paper_testnet_live_parity_default_enabled", True))
        cfg["adaptive_ladder_tp_enabled"] = bool(cfg.get("paper_testnet_ladder_tp_default_enabled", True))

    cfg["runner_exit_enabled"] = False
    cfg["shadow_runner_exit_enabled"] = False
    cfg["runner_chandelier_enabled"] = False
    return cfg

LIVE_REAL_CONFIRM_TEXT = "I_CONFIRM_REAL_BINANCE_FUTURES_TRADING"
LIVE_REAL_ALLOWED_SYMBOLS_DEFAULT = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
LIVE_REAL_BLOCKED_SYMBOL_FRAGMENTS = (
    "UP/",
    "DOWN/",
    "BULL/",
    "BEAR/",
    "1000",
    "AAPL",
    "AMZN",
    "GOOGL",
    "GOOG",
    "META",
    "MSFT",
    "NFLX",
    "NVDA",
    "TSLA",
)


def _normalize_live_real_stage(stage):
    return str(stage or "").strip().upper()


def _normalize_live_real_symbol(symbol):
    text = str(symbol or "").strip().upper()
    if not text:
        return ""
    for suffix in (":USDT", ":USDC", ":BUSD"):
        text = text.replace(suffix, "")
    text = text.replace("-", "/").replace("_", "/")
    if "/" not in text:
        for quote in ("USDT", "USDC", "BUSD"):
            if text.endswith(quote) and len(text) > len(quote):
                text = f"{text[:-len(quote)]}/{quote}"
                break
    return text


def enforce_activation_stage(cfg):
    if not isinstance(cfg, dict):
        return cfg

    mode = str(cfg.get("mode", "")).lower()
    is_live = mode in {"live", "real", "production"} or bool(cfg.get("live_trading", False))
    is_paper_or_testnet = mode in {"paper", "testnet", "dry_run"} or bool(cfg.get("testnet", False))

    default_stage = "DISABLED" if is_live else "TESTNET_ONLY" if is_paper_or_testnet else "DISABLED"
    stage = _normalize_live_real_stage(cfg.get("live_activation_stage", default_stage))
    cfg["live_activation_stage"] = stage
    cfg["runner_exit_enabled"] = False
    cfg["shadow_runner_exit_enabled"] = False
    cfg["runner_chandelier_enabled"] = False

    if stage == "DISABLED":
        cfg["advanced_alpha_engine_enabled"] = False
        cfg["live_parity_signal_enabled"] = False
        cfg["adaptive_ladder_tp_enabled"] = False
        cfg["real_order_enabled"] = False
        cfg["live_trading"] = False
    elif stage == "PAPER_ONLY":
        cfg["advanced_alpha_engine_enabled"] = True
        cfg["live_parity_signal_enabled"] = True
        cfg["adaptive_ladder_tp_enabled"] = True
        cfg["real_order_enabled"] = False
        cfg["live_trading"] = False
    elif stage == "TESTNET_ONLY":
        cfg["advanced_alpha_engine_enabled"] = True
        cfg["live_parity_signal_enabled"] = True
        cfg["adaptive_ladder_tp_enabled"] = True
        cfg["real_order_enabled"] = True
        cfg["testnet"] = True
        cfg["live_trading"] = False
        cfg["max_leverage"] = min(float(cfg.get("max_leverage", cfg.get("leverage", 2)) or 2), 2.0)
        cfg["leverage"] = min(float(cfg.get("leverage", cfg.get("max_leverage", 2)) or 2), cfg["max_leverage"])
    elif stage == "SMALL_LIVE_025":
        if is_live and not bool(cfg.get("advanced_alpha_live_opt_in", False)):
            raise ValueError("SMALL_LIVE_025 requires advanced_alpha_live_opt_in=True for live trading")
        cfg["advanced_alpha_engine_enabled"] = True
        cfg["live_parity_signal_enabled"] = True
        cfg["adaptive_ladder_tp_enabled"] = True
        cfg["max_risk_per_trade_pct"] = min(float(cfg.get("max_risk_per_trade_pct", 0.25)), 0.25)
    elif stage == "LIVE_050":
        if is_live and not bool(cfg.get("advanced_alpha_live_opt_in", False)):
            raise ValueError("LIVE_050 requires advanced_alpha_live_opt_in=True for live trading")
        cfg["advanced_alpha_engine_enabled"] = True
        cfg["live_parity_signal_enabled"] = True
        cfg["adaptive_ladder_tp_enabled"] = True
        cfg["max_risk_per_trade_pct"] = min(float(cfg.get("max_risk_per_trade_pct", 0.5)), 0.5)
    elif stage == "LIVE_REAL_SMALL_CAP":
        if cfg.get("real_live_confirm") != LIVE_REAL_CONFIRM_TEXT:
            raise ValueError("LIVE_REAL_SMALL_CAP requires exact real_live_confirm text")
        cfg["advanced_alpha_engine_enabled"] = True
        cfg["live_parity_signal_enabled"] = True
        cfg["adaptive_ladder_tp_enabled"] = True
        cfg["real_order_enabled"] = True
        cfg["testnet"] = False
        cfg["live_trading"] = True
        cfg["account_reference_equity_usdt"] = float(
            cfg.get(
                "account_reference_equity_usdt",
                LIVE_REAL_SMALL_CAP_DEFAULTS["account_reference_equity_usdt"],
            )
            or LIVE_REAL_SMALL_CAP_DEFAULTS["account_reference_equity_usdt"]
        )
        cfg["max_leverage"] = min(int(cfg.get("max_leverage", 5) or 5), 5)
        cfg["leverage"] = min(float(cfg.get("leverage", cfg["max_leverage"]) or cfg["max_leverage"]), cfg["max_leverage"])
        for key, value in LIVE_REAL_SMALL_CAP_DEFAULTS.items():
            cfg.setdefault(key, value)
        cfg["max_real_risk_pct"] = max(
            _safe_float_value(cfg.get("max_real_risk_pct"), 0.0),
            LIVE_REAL_SMALL_CAP_DEFAULTS["max_real_risk_pct"],
        )
        cfg["scale_notional_cap_with_risk_pct"] = True
        cfg["max_position_notional_pct_hard_limit"] = max(
            _safe_float_value(cfg.get("max_position_notional_pct_hard_limit"), 0.0),
            LIVE_REAL_SMALL_CAP_DEFAULTS["max_position_notional_pct_hard_limit"],
        )
        cfg["max_open_positions"] = min(int(cfg.get("max_open_positions", 1) or 1), 1)
        cfg["max_same_direction_positions"] = min(int(cfg.get("max_same_direction_positions", 1) or 1), 1)
        apply_live_small_cap_limits_to_config(cfg, cfg["account_reference_equity_usdt"])
        cfg.setdefault("live_real_allowlist", list(LIVE_REAL_ALLOWED_SYMBOLS_DEFAULT))
        cfg.setdefault("live_real_blocklist", [])
        cfg.setdefault("min_notional_usdt", 5.0)
        cfg.setdefault("require_derivatives_data_for_advanced_alpha", False)
    elif stage == "LIVE_REAL_FULL_LOCKED":
        raise ValueError("LIVE_REAL_FULL_LOCKED is intentionally locked; use LIVE_REAL_SMALL_CAP only")
    else:
        raise ValueError(f"Unknown live_activation_stage: {stage}")
    return cfg

PAUSE_STATE_FILE = "runtime/critical_pause_state.json"
LIVE_REAL_RISK_STATE_FILE = "runtime/live_real_risk_state.json"
LIVE_REAL_SMALL_CAP_DEFAULTS = {
    "account_reference_equity_usdt": 62.0,
    "max_real_position_notional_pct_of_equity": 0.09,
    "default_real_risk_pct": 0.005,
    "min_real_risk_pct": 0.001,
    "max_real_risk_pct": 0.10,
    "max_daily_real_loss_pct_of_equity": 0.036,
    "max_weekly_real_loss_pct_of_equity": 0.18,
    "max_risk_pct_increases_per_day": 2,
    "risk_pct_change_reset_timezone": "Asia/Seoul",
    "scale_notional_cap_with_risk_pct": True,
    "max_position_notional_pct_hard_limit": 1.00,
}

def evaluate_critical_pause_block(*, state: dict | None, requested_symbol: str) -> CriticalPauseBlockDecision:
    if not state:
        return CriticalPauseBlockDecision(blocked=False)

    raw_status = str(state.get("status") or "").upper().strip()
    if raw_status != "CRITICAL_PAUSED":
        return CriticalPauseBlockDecision(blocked=False)

    raw_scope = str(state.get("scope") or "GLOBAL").upper().strip()
    if raw_scope not in {"GLOBAL", "SYMBOL"}:
        raw_scope = "GLOBAL"

    reason_code = state.get("reason_code") or state.get("reason") or "CRITICAL_PAUSE"
    origin_symbol = state.get("origin_symbol") or state.get("symbol")

    def _valid_canonical_symbol(value: str) -> bool:
        return bool(
            re.fullmatch(
                r"[A-Z0-9]+/(USDT|USDC|BUSD):(USDT|USDC|BUSD)",
                str(value or "").upper(),
            )
        )

    if raw_scope == "GLOBAL":
        origin_symbol = origin_symbol or "*"
    else:
        try:
            canonical_origin = canonical_futures_symbol(origin_symbol) if origin_symbol else ""
            canonical_requested = canonical_futures_symbol(requested_symbol)
        except Exception:
            canonical_origin = ""
            canonical_requested = ""

        if not _valid_canonical_symbol(canonical_origin) or not _valid_canonical_symbol(canonical_requested):
            raw_scope = "GLOBAL"
            origin_symbol = "*"
        else:
            origin_symbol = canonical_origin

    created_at = state.get("created_at") or state.get("timestamp") or state.get("updated_at")
    decision = CriticalPauseBlockDecision(
        blocked=True,
        reason_code=str(reason_code),
        pause_id=state.get("pause_id"),
        scope=raw_scope,
        origin_symbol=str(origin_symbol or "*"),
        created_at=created_at,
    )

    if raw_scope == "GLOBAL":
        return decision

    clean_requested = canonical_futures_symbol(requested_symbol)
    return decision if clean_requested == origin_symbol else CriticalPauseBlockDecision(blocked=False)

def write_critical_pause_state(symbol, reason, exception, cfg=None, *, scope="GLOBAL", reason_code=None, origin_symbol=None):
    import uuid
    resolved_reason_code = reason_code or reason
    resolved_origin_symbol = origin_symbol or symbol or "*"
    resolved_scope = str(scope or "GLOBAL").upper().strip()
    if resolved_scope not in {"GLOBAL", "SYMBOL"}:
        resolved_scope = "GLOBAL"

    now_str = datetime.now(timezone.utc).isoformat()

    if os.path.exists(PAUSE_STATE_FILE):
        try:
            with open(PAUSE_STATE_FILE, "r", encoding="utf-8") as f:
                raw_content = f.read()
            existing = json.loads(raw_content)
        except Exception:
            raise RuntimeError(
                "CRITICAL_PAUSE_STATE_WRITE_BLOCKED_BY_UNREADABLE_EXISTING_FILE"
            )

        if not isinstance(existing, dict):
            raise RuntimeError(
                "CRITICAL_PAUSE_STATE_WRITE_BLOCKED_BY_UNREADABLE_EXISTING_FILE"
            )

        existing_status = existing.get("status")
        legacy_active = (
            existing_status is None
            and bool(existing.get("symbol"))
            and any(
                key in existing
                for key in ("reason", "reason_code", "manual_resume_required")
            )
        )
        if str(existing_status).upper().strip() == "CRITICAL_PAUSED" or legacy_active:
            existing_reason_code = existing.get("reason_code") or existing.get("reason")
            existing_scope = str(existing.get("scope") or "GLOBAL").upper().strip()
            if existing_scope not in {"GLOBAL", "SYMBOL"}:
                existing_scope = "GLOBAL"
            existing_origin_symbol = existing.get("origin_symbol") or existing.get("symbol") or "*"
            existing_created_at = existing.get("created_at") or existing.get("timestamp") or existing.get("updated_at")

            same_reason = (existing_reason_code == resolved_reason_code)
            same_scope = (existing_scope == resolved_scope)
            same_origin = (canonical_futures_symbol(existing_origin_symbol) == canonical_futures_symbol(resolved_origin_symbol))

            if same_reason and same_scope and same_origin:
                pause_id = existing.get("pause_id") or uuid.uuid4().hex
                created_at = existing_created_at or now_str
                try:
                    count = int(existing.get("occurrence_count", 1)) + 1
                except Exception:
                    count = 2

                state = {
                    "status": "CRITICAL_PAUSED",
                    "symbol": symbol,
                    "reason": reason,
                    "exception": repr(exception),
                    "timestamp": now_str,
                    "manual_resume_required": True,
                    "pause_id": pause_id,
                    "origin_symbol": resolved_origin_symbol,
                    "scope": resolved_scope,
                    "reason_code": resolved_reason_code,
                    "created_at": created_at,
                    "updated_at": now_str,
                    "occurrence_count": count,
                }
                atomic_write_json(PAUSE_STATE_FILE, state, indent=4)
                return state

        pause_id = uuid.uuid4().hex
        state = {
            "status": "CRITICAL_PAUSED",
            "symbol": symbol,
            "reason": reason,
            "exception": repr(exception),
            "timestamp": now_str,
            "manual_resume_required": True,
            "pause_id": pause_id,
            "origin_symbol": resolved_origin_symbol,
            "scope": resolved_scope,
            "reason_code": resolved_reason_code,
            "created_at": now_str,
            "updated_at": now_str,
            "occurrence_count": 1,
        }
        atomic_write_json(PAUSE_STATE_FILE, state, indent=4)
        return state
    else:
        pause_id = uuid.uuid4().hex
        state = {
            "status": "CRITICAL_PAUSED",
            "symbol": symbol,
            "reason": reason,
            "exception": repr(exception),
            "timestamp": now_str,
            "manual_resume_required": True,
            "pause_id": pause_id,
            "origin_symbol": resolved_origin_symbol,
            "scope": resolved_scope,
            "reason_code": resolved_reason_code,
            "created_at": now_str,
            "updated_at": now_str,
            "occurrence_count": 1,
        }
        atomic_write_json(PAUSE_STATE_FILE, state, indent=4)
        return state

def load_critical_pause_state():
    if not os.path.exists(PAUSE_STATE_FILE):
        return None

    mtime_ns = 0
    size = 0
    try:
        st = os.stat(PAUSE_STATE_FILE)
        mtime_ns = st.st_mtime_ns
        size = st.st_size
    except Exception:
        pass

    normalized_path = os.path.abspath(PAUSE_STATE_FILE).replace("\\", "/")

    def make_unreadable_state(err_type_str):
        material = f"{normalized_path}|{mtime_ns}|{size}|{err_type_str}"
        pause_id = "unreadable:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
        try:
            mtime_utc = datetime.fromtimestamp(mtime_ns / 1e9, tz=timezone.utc).isoformat()
        except Exception:
            mtime_utc = datetime.now(timezone.utc).isoformat()

        return {
            "status": "CRITICAL_PAUSED",
            "scope": "GLOBAL",
            "origin_symbol": "*",
            "symbol": "*",
            "reason_code": "CRITICAL_PAUSE_STATE_UNREADABLE",
            "reason": "CRITICAL_PAUSE_STATE_UNREADABLE",
            "manual_resume_required": True,
            "pause_id": pause_id,
            "created_at": mtime_utc,
            "updated_at": mtime_utc,
            "timestamp": mtime_utc,
        }

    try:
        with open(PAUSE_STATE_FILE, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        logger.exception("Failed to read critical pause state file")
        return make_unreadable_state(e.__class__.__name__)

    try:
        payload = json.loads(content)
    except Exception as e:
        logger.exception("Failed to parse critical pause state file as JSON")
        return make_unreadable_state(e.__class__.__name__)

    if not isinstance(payload, dict):
        logger.error("Loaded critical pause payload is not a dict")
        return make_unreadable_state("MALFORMED_CRITICAL_PAUSE_STATE")

    status = payload.get("status")
    is_legacy = False
    if status is None:
        has_symbol = "symbol" in payload
        has_reason_or_code = "reason" in payload or "reason_code" in payload or "manual_resume_required" in payload
        if has_symbol and has_reason_or_code:
            is_legacy = True

    if is_legacy:
        res = dict(payload)
        res["status"] = "CRITICAL_PAUSED"
        if "scope" not in res:
            res["scope"] = "GLOBAL"
        if "origin_symbol" not in res:
            res["origin_symbol"] = res["symbol"]
        if "reason_code" not in res:
            res["reason_code"] = res.get("reason", "UNKNOWN")

        reason = res.get("reason_code") or "UNKNOWN"
        scope = str(res.get("scope") or "GLOBAL").upper().strip()
        origin = res.get("origin_symbol") or res.get("symbol") or "*"
        material = f"{normalized_path}|{mtime_ns}|{size}|{reason}|{scope}|{origin}"
        if not res.get("pause_id"):
            res["pause_id"] = "legacy:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
        return res

    if status is None:
        logger.error("Loaded critical pause status is missing and not legacy")
        return make_unreadable_state("MALFORMED_CRITICAL_PAUSE_STATE")

    if str(status).upper().strip() != "CRITICAL_PAUSED":
        logger.error("Loaded critical pause status is invalid: %s", status)
        return make_unreadable_state("UNKNOWN_CRITICAL_PAUSE_STATUS")

    res = dict(payload)
    if not res.get("pause_id"):
        reason = res.get("reason_code") or res.get("reason") or "UNKNOWN"
        scope = str(res.get("scope") or "GLOBAL").upper().strip()
        origin = res.get("origin_symbol") or res.get("symbol") or "*"
        material = f"{normalized_path}|{mtime_ns}|{size}|{reason}|{scope}|{origin}"
        res["pause_id"] = "legacy:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]

    return res

def assert_trading_allowed(symbol, cfg=None, *, include_critical_pause=True):
    if include_critical_pause:
        state = load_critical_pause_state()
        decision = evaluate_critical_pause_block(state=state, requested_symbol=symbol)
        if decision.blocked:
            raise RuntimeError(f"TRADING_CRITICAL_PAUSED: Trading safety conflict (critical pause): {decision.reason_code}")
    if cfg and bool(cfg.get("global_trading_paused", False)):
        raise RuntimeError("TRADING_GLOBAL_PAUSED")
    return True

def manual_resume_trading(symbol, confirm_text):
    request = write_manual_resume_request(
        symbol,
        confirm_text,
        requested_by="compatibility_wrapper",
    )
    return {
        "status": "RESUME_REQUESTED",
        "symbol": symbol,
        "request_id": request.request_id,
    }

class InvalidOrderPlan(Exception):
    pass


class TradingSafetyError(Exception):
    pass


class TradingPausedError(TradingSafetyError):
    pass


class LiveRealRiskStateUnreadable(TradingPausedError):
    """Raised when persisted live loss state cannot be trusted."""

    pass


def _safe_float_value(value, default=0.0):
    try:
        number = float(value)
        if math.isfinite(number):
            return number
    except (TypeError, ValueError):
        pass
    return float(default)


def _bounded_fraction(value, default, *, minimum=0.0, maximum=1.0):
    number = _safe_float_value(value, default)
    if number > 1.0:
        number /= 100.0
    return max(float(minimum), min(float(maximum), float(number)))


def _live_real_config_float(cfg, key, default):
    cfg = cfg if isinstance(cfg, dict) else {}
    return _safe_float_value(cfg.get(key), default)


def _live_real_timezone(cfg=None):
    cfg = cfg if isinstance(cfg, dict) else {}
    tz_name = str(
        cfg.get('risk_pct_change_reset_timezone')
        or LIVE_REAL_SMALL_CAP_DEFAULTS['risk_pct_change_reset_timezone']
    )
    try:
        return tz_name, ZoneInfo(tz_name)
    except Exception:
        fallback = LIVE_REAL_SMALL_CAP_DEFAULTS['risk_pct_change_reset_timezone']
        return fallback, ZoneInfo(fallback)


def _live_real_kst_date_key(cfg=None, now=None):
    _, tz = _live_real_timezone(cfg)
    current = now or datetime.now(timezone.utc)
    if getattr(current, 'tzinfo', None) is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(tz).strftime('%Y-%m-%d')


def _live_real_min_max_risk_fraction(cfg=None):
    cfg = cfg if isinstance(cfg, dict) else {}
    min_frac = _bounded_fraction(
        cfg.get("min_real_risk_pct"),
        LIVE_REAL_SMALL_CAP_DEFAULTS["min_real_risk_pct"],
        minimum=0.0001,
        maximum=1.0,
    )
    max_frac = _bounded_fraction(
        cfg.get("max_real_risk_pct"),
        LIVE_REAL_SMALL_CAP_DEFAULTS["max_real_risk_pct"],
        minimum=min_frac,
        maximum=1.0,
    )
    return min_frac, max_frac


def _live_real_risk_fraction_from_cfg(cfg=None):
    cfg = cfg if isinstance(cfg, dict) else {}
    min_frac, max_frac = _live_real_min_max_risk_fraction(cfg)
    default_frac = _bounded_fraction(
        cfg.get("default_real_risk_pct"),
        LIVE_REAL_SMALL_CAP_DEFAULTS["default_real_risk_pct"],
        minimum=min_frac,
        maximum=max_frac,
    )
    if cfg.get("live_real_risk_pct_user") is not None:
        raw = _safe_float_value(cfg.get("live_real_risk_pct_user"), default_frac)
        if raw > max_frac and raw <= 100.0:
            raw /= 100.0
        return max(min_frac, min(max_frac, raw))
    if cfg.get("risk_per_trade_pct") is not None:
        return max(min_frac, min(max_frac, _safe_float_value(cfg.get("risk_per_trade_pct"), default_frac * 100.0) / 100.0))
    if cfg.get("risk_per_trade_percent") is not None:
        return max(min_frac, min(max_frac, _safe_float_value(cfg.get("risk_per_trade_percent"), default_frac * 100.0) / 100.0))
    return default_frac


def parse_live_real_risk_pct_input(value, cfg=None):
    text = str(value or "").strip().lower().replace("%", "")
    min_frac, max_frac = _live_real_min_max_risk_fraction(cfg)
    if text in {"safe", "min", "minimum", "low", "lowest"}:
        return min_frac
    if not text:
        raise ValueError("risk percent is required")
    try:
        percent = float(text)
    except (TypeError, ValueError) as exc:
        raise ValueError("risk percent must be numeric, safe, or min") from exc
    fraction = percent / 100.0
    if fraction < min_frac or fraction > max_frac:
        raise ValueError(f"risk percent must be between {min_frac * 100.0:.1f}% and {max_frac * 100.0:.1f}%")
    return fraction


def normalize_live_real_risk_state(state, cfg=None, now=None):
    state = dict(state) if isinstance(state, dict) else {}
    today, week = _live_real_period_keys(now, cfg)
    timezone_name, _ = _live_real_timezone(cfg)
    state.setdefault('date', today)
    state.setdefault('week', week)
    if state.get('date') != today:
        state['date'] = today
        state['daily_realized_pnl_usdt'] = 0.0
        state['daily_loss_usdt'] = 0.0
    if state.get('week') != week:
        state['week'] = week
        state['weekly_realized_pnl_usdt'] = 0.0
        state['weekly_loss_usdt'] = 0.0
    if state.get('risk_pct_change_date_kst') != today:
        state['risk_pct_change_date_kst'] = today
        state['risk_pct_increases_today'] = 0
    state.setdefault('daily_realized_pnl_usdt', 0.0)
    state.setdefault('weekly_realized_pnl_usdt', 0.0)
    state.setdefault('risk_pct_increases_today', 0)
    state['loss_period_timezone'] = timezone_name
    return state


def apply_live_real_risk_pct_change(state, current_fraction, requested_fraction, cfg=None, now=None):
    cfg = cfg if isinstance(cfg, dict) else {}
    state = normalize_live_real_risk_state(state, cfg, now)
    current = _live_real_risk_fraction_from_cfg({**cfg, "live_real_risk_pct_user": current_fraction})
    requested = _live_real_risk_fraction_from_cfg({**cfg, "live_real_risk_pct_user": requested_fraction})
    if abs(requested - current) <= 1e-12:
        state["live_real_risk_pct_user"] = requested
        return {"allowed": True, "changed": False, "state": state, "reason": "same_value"}
    increasing = requested > current
    limit = int(cfg.get("max_risk_pct_increases_per_day", cfg.get("max_risk_pct_changes_per_day", LIVE_REAL_SMALL_CAP_DEFAULTS["max_risk_pct_increases_per_day"])) or 0)
    used = int(state.get("risk_pct_increases_today", 0) or 0)
    if increasing and limit > 0 and used >= limit:
        return {
            "allowed": False,
            "changed": False,
            "state": state,
            "reason": "daily_risk_increase_limit",
            "limit": limit,
            "used": used,
        }
    state["live_real_risk_pct_user"] = requested
    state["last_live_real_risk_pct_user"] = current
    changed_at = now or datetime.now(timezone.utc)
    if getattr(changed_at, "tzinfo", None) is None:
        changed_at = changed_at.replace(tzinfo=timezone.utc)
    state["last_risk_pct_changed_at"] = changed_at.isoformat()
    if increasing:
        state["risk_pct_increases_today"] = used + 1
    history = state.get("risk_pct_change_history")
    if not isinstance(history, list):
        history = []
    history.append({
        "at": changed_at.isoformat(),
        "from": current,
        "to": requested,
        "source": str(cfg.get("_risk_change_source") or "unknown"),
        "reason": "risk_increased" if increasing else "risk_decreased",
    })
    state["risk_pct_change_history"] = history[-20:]
    return {
        "allowed": True,
        "changed": True,
        "state": state,
        "reason": "risk_increased" if increasing else "risk_decreased",
        "limit": limit,
        "used": int(state.get("risk_pct_increases_today", 0) or 0),
    }


def resolve_live_small_cap_limits(cfg=None, account_equity=None):
    cfg = cfg if isinstance(cfg, dict) else {}
    equity = _safe_float_value(
        account_equity if account_equity is not None else cfg.get('account_reference_equity_usdt'),
        LIVE_REAL_SMALL_CAP_DEFAULTS['account_reference_equity_usdt'],
    )
    equity = max(equity, 0.0)
    min_frac, max_frac = _live_real_min_max_risk_fraction(cfg)
    risk_frac = _live_real_risk_fraction_from_cfg(cfg)
    default_risk_frac = _bounded_fraction(
        cfg.get('default_real_risk_pct'),
        LIVE_REAL_SMALL_CAP_DEFAULTS['default_real_risk_pct'],
        minimum=min_frac,
        maximum=max_frac,
    )
    hard_notional_pct = _bounded_fraction(
        cfg.get('max_position_notional_pct_hard_limit'),
        LIVE_REAL_SMALL_CAP_DEFAULTS['max_position_notional_pct_hard_limit'],
        minimum=0.01,
        maximum=1.0,
    )
    notional_pct = _bounded_fraction(
        cfg.get('max_real_position_notional_pct_of_equity'),
        LIVE_REAL_SMALL_CAP_DEFAULTS['max_real_position_notional_pct_of_equity'],
        minimum=0.0,
        maximum=hard_notional_pct,
    )
    if bool(cfg.get('scale_notional_cap_with_risk_pct', LIVE_REAL_SMALL_CAP_DEFAULTS['scale_notional_cap_with_risk_pct'])):
        scale = risk_frac / max(default_risk_frac, 1e-12)
        notional_pct = min(hard_notional_pct, notional_pct * scale)
    daily_pct = _bounded_fraction(
        cfg.get('max_daily_real_loss_pct_of_equity'),
        LIVE_REAL_SMALL_CAP_DEFAULTS['max_daily_real_loss_pct_of_equity'],
        minimum=0.0,
        maximum=1.0,
    )
    weekly_pct = _bounded_fraction(
        cfg.get('max_weekly_real_loss_pct_of_equity'),
        LIVE_REAL_SMALL_CAP_DEFAULTS['max_weekly_real_loss_pct_of_equity'],
        minimum=0.0,
        maximum=1.0,
    )

    percentage_notional_cap = equity * notional_pct
    percentage_loss_cap = equity * risk_frac

    def _positive_optional(key):
        if cfg.get(key) is None:
            return None
        value = _safe_float_value(cfg.get(key), 0.0)
        return value if value > 0 else None

    absolute_notional_cap = _positive_optional('live_real_absolute_max_notional_usdt')
    absolute_loss_cap = _positive_optional('live_real_absolute_max_loss_usdt')
    effective_notional_cap = (
        min(percentage_notional_cap, absolute_notional_cap)
        if absolute_notional_cap is not None
        else percentage_notional_cap
    )
    effective_loss_cap = (
        min(percentage_loss_cap, absolute_loss_cap)
        if absolute_loss_cap is not None
        else percentage_loss_cap
    )
    timezone_name, _ = _live_real_timezone(cfg)
    limits = {
        'account_equity_usdt': equity,
        'risk_fraction': risk_frac,
        'risk_pct': risk_frac * 100.0,
        'min_risk_fraction': min_frac,
        'max_risk_fraction': max_frac,
        'max_position_notional_pct': notional_pct,
        'percentage_max_position_notional_usdt': percentage_notional_cap,
        'percentage_max_loss_per_trade_usdt': percentage_loss_cap,
        'requested_absolute_max_notional_usdt': absolute_notional_cap,
        'requested_absolute_max_loss_usdt': absolute_loss_cap,
        'absolute_notional_cap_applied': (
            absolute_notional_cap is not None and absolute_notional_cap < percentage_notional_cap
        ),
        'absolute_loss_cap_applied': (
            absolute_loss_cap is not None and absolute_loss_cap < percentage_loss_cap
        ),
        'max_position_notional_usdt': effective_notional_cap,
        'max_loss_per_trade_usdt': effective_loss_cap,
        'max_daily_loss_usdt': equity * daily_pct,
        'max_weekly_loss_usdt': equity * weekly_pct,
        'daily_loss_pct': daily_pct,
        'weekly_loss_pct': weekly_pct,
        'loss_period_timezone': timezone_name,
        'notional_scaled_by_risk': bool(cfg.get('scale_notional_cap_with_risk_pct', False)),
    }
    return limits


def apply_live_small_cap_limits_to_config(cfg, account_equity=None):
    if not isinstance(cfg, dict):
        return {}
    limits = resolve_live_small_cap_limits(cfg, account_equity)
    cfg["account_reference_equity_usdt"] = limits["account_equity_usdt"]
    cfg["live_real_risk_pct_user"] = limits["risk_fraction"]
    cfg["risk_per_trade_pct"] = limits["risk_pct"]
    cfg["risk_per_trade_percent"] = limits["risk_pct"]
    cfg["max_risk_per_trade_pct"] = limits["max_risk_fraction"] * 100.0
    cfg["max_real_position_notional_usdt"] = limits["max_position_notional_usdt"]
    cfg["max_real_loss_per_trade_usdt"] = limits["max_loss_per_trade_usdt"]
    cfg["max_daily_real_loss_usdt"] = limits["max_daily_loss_usdt"]
    cfg["max_weekly_real_loss_usdt"] = limits["max_weekly_loss_usdt"]
    cfg["live_real_effective_limits"] = dict(limits)
    return limits


LIVE_REAL_RISK_BUTTON_PRESETS = (
    ("0.25%", "risk:set:0.0025"),
    ("0.5%", "risk:set:0.005"),
    ("1%", "risk:set:0.01"),
    ("2%", "risk:set:0.02"),
    ("3%", "risk:set:0.03"),
    ("5%", "risk:set:0.05"),
    ("10%", "risk:set:0.10"),
)


def build_live_real_risk_config(signal_engine_cfg):
    sig_cfg = signal_engine_cfg if isinstance(signal_engine_cfg, dict) else {}
    common_cfg = dict(sig_cfg.get("common_settings", {}) or {})
    strategy_params = sig_cfg.get("strategy_params", {}) or {}
    ut_cfg = dict(strategy_params.get("UTBotFilteredBreakoutV1", {}) or {})
    cfg_for_risk = dict(common_cfg)
    for key in (
        "default_real_risk_pct",
        "min_real_risk_pct",
        "max_real_risk_pct",
        "max_risk_pct_increases_per_day",
        "max_risk_pct_changes_per_day",
        "risk_pct_change_reset_timezone",
        "account_reference_equity_usdt",
        "max_real_position_notional_pct_of_equity",
        "max_daily_real_loss_pct_of_equity",
        "max_weekly_real_loss_pct_of_equity",
        "scale_notional_cap_with_risk_pct",
        "max_position_notional_pct_hard_limit",
        "live_real_absolute_max_notional_usdt",
        "live_real_absolute_max_loss_usdt",
    ):
        if key in ut_cfg and key not in cfg_for_risk:
            cfg_for_risk[key] = ut_cfg[key]
    cfg_for_risk["max_real_risk_pct"] = max(
        _safe_float_value(cfg_for_risk.get("max_real_risk_pct"), 0.0),
        LIVE_REAL_SMALL_CAP_DEFAULTS["max_real_risk_pct"],
    )
    cfg_for_risk["scale_notional_cap_with_risk_pct"] = True
    cfg_for_risk["max_position_notional_pct_hard_limit"] = max(
        _safe_float_value(cfg_for_risk.get("max_position_notional_pct_hard_limit"), 0.0),
        LIVE_REAL_SMALL_CAP_DEFAULTS["max_position_notional_pct_hard_limit"],
    )
    return cfg_for_risk


def build_live_real_risk_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("0.25%", callback_data="risk:set:0.0025"),
            InlineKeyboardButton("0.5%", callback_data="risk:set:0.005"),
            InlineKeyboardButton("1%", callback_data="risk:set:0.01"),
        ],
        [
            InlineKeyboardButton("2%", callback_data="risk:set:0.02"),
            InlineKeyboardButton("3%", callback_data="risk:set:0.03"),
            InlineKeyboardButton("5%", callback_data="risk:set:0.05"),
        ],
        [
            InlineKeyboardButton("10%", callback_data="risk:set:0.10"),
            InlineKeyboardButton("SAFE 0.1%", callback_data="risk:safe"),
            InlineKeyboardButton("직접 입력", callback_data="risk:manual"),
        ],
        [InlineKeyboardButton("뒤로", callback_data="risk:back:utbreak")],
    ])


def build_live_real_risk_status_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("리스크 설정", callback_data="risk:menu")],
        [InlineKeyboardButton("뒤로", callback_data="risk:back:utbreak")],
    ])


def build_live_real_risk_confirm_keyboard(fraction):
    percent = float(fraction) * 100.0
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"예, {percent:g}%로 변경", callback_data=f"risk:confirm:set:{float(fraction):.4g}"),
            InlineKeyboardButton("취소", callback_data="risk:cancel"),
        ]
    ])


def append_live_real_risk_buttons_to_utbreakout_rows(rows):
    new_rows = [list(row) for row in (rows or [])]
    risk_row = [
        InlineKeyboardButton("리스크 설정", callback_data="risk:menu"),
        InlineKeyboardButton("리스크 상태", callback_data="risk:status"),
    ]
    insert_at = max(0, len(new_rows) - 1)
    if any(
        getattr(button, "callback_data", None) in {"risk:menu", "risk:status"}
        for row in new_rows
        for button in row
    ):
        return new_rows
    new_rows.insert(insert_at, risk_row)
    return new_rows


def _format_live_real_risk_percent(fraction):
    return f"{float(fraction) * 100.0:.2f}%"


def _format_live_real_risk_history(state, cfg=None, *, limit=5):
    history = state.get("risk_pct_change_history") if isinstance(state, dict) else None
    if not isinstance(history, list) or not history:
        return ["- 없음"]
    _, tz = _live_real_timezone(cfg)
    lines = []
    for item in history[-limit:]:
        if not isinstance(item, dict):
            continue
        try:
            at = datetime.fromisoformat(str(item.get("at"))).astimezone(tz).strftime("%H:%M")
        except Exception:
            at = "??:??"
        old = _safe_float_value(item.get("from"), 0.0) * 100.0
        new = _safe_float_value(item.get("to"), 0.0) * 100.0
        lines.append(f"- {at} {old:.2f}% -> {new:.2f}%")
    return lines or ["- 없음"]


def render_live_real_risk_status_text(
    cfg,
    state,
    limits,
    *,
    equity_source="reference",
    menu=False,
    notice=None,
):
    cfg = cfg if isinstance(cfg, dict) else {}
    state = normalize_live_real_risk_state(state, cfg)
    limits = limits if isinstance(limits, dict) else resolve_live_small_cap_limits(cfg)
    current = _live_real_risk_fraction_from_cfg(cfg)
    limit = int(cfg.get(
        "max_risk_pct_increases_per_day",
        cfg.get(
            "max_risk_pct_changes_per_day",
            LIVE_REAL_SMALL_CAP_DEFAULTS["max_risk_pct_increases_per_day"],
        ),
    ) or 0)
    title = "🧭 리스크 설정" if menu else "📊 현재 리스크 상태"
    equity_label = "actual equity" if equity_source == "actual" else "reference equity"
    lines = []
    if notice:
        lines.extend([str(notice), ""])
    lines.extend([
        title,
        "",
        f"현재 1회 리스크: {current * 100.0:.2f}%",
        f"내부 저장값: {current:.6f}",
        f"계좌 equity: {float(limits.get('account_equity_usdt', 0.0) or 0.0):.2f} USDT ({equity_label} 기준)",
        f"1회 손실 예산: {float(limits.get('max_loss_per_trade_usdt', 0.0) or 0.0):.4f} USDT",
        f"포지션 명목가 상한: {float(limits.get('max_position_notional_usdt', 0.0) or 0.0):.4f} USDT",
        f"일 손실 한도: {float(limits.get('max_daily_loss_usdt', 0.0) or 0.0):.4f} USDT",
        f"주 손실 한도: {float(limits.get('max_weekly_loss_usdt', 0.0) or 0.0):.4f} USDT",
        f"오늘 리스크 상향 변경 횟수: {int(state.get('risk_pct_increases_today', 0) or 0)}/{limit} KST",
        f"손실·리스크 집계 초기화 기준: "
        f"{str(limits.get('loss_period_timezone') or 'Asia/Seoul')} 자정",
    ])
    if limits.get('notional_scaled_by_risk'):
        lines.append(
            f"포지션 상한 연동: ON / equity의 "
            f"{float(limits.get('max_position_notional_pct', 0.0) or 0.0) * 100.0:.1f}%"
        )
    if float(limits.get('max_loss_per_trade_usdt', 0.0) or 0.0) > float(limits.get('max_daily_loss_usdt', 0.0) or 0.0):
        lines.append("주의: 1회 최대 손실예산이 일 손실한도보다 커서 한 번의 완전 SL 후 당일 거래가 중단될 수 있습니다.")
    if menu:
        lines.extend([
            "",
            "원하는 리스크를 선택하세요.",
            "리스크는 1회 최대 허용 손실 기준이며 실제 수량은 market quality, risk multiplier, cap에 따라 더 작아질 수 있습니다.",
        ])
    else:
        lines.extend(["", "최근 변경 기록:", *_format_live_real_risk_history(state, cfg)])
    return "\n".join(lines)


def render_live_real_risk_manual_text():
    return "\n".join([
        "직접 입력은 아래처럼 입력하세요.",
        "",
        "/risk 1.5",
        "",
        "입력 가능 범위: 0.1% ~ 10%",
    ])


def build_utbreakout_order_path_summary(
    cfg,
    *,
    exchange_mode=None,
    micro_cfg=None,
):
    cfg = cfg if isinstance(cfg, dict) else {}
    micro_cfg = micro_cfg if isinstance(micro_cfg, dict) else {}
    mode = str(exchange_mode or cfg.get("exchange_mode") or "unknown")
    stage = _normalize_live_real_stage(cfg.get("live_activation_stage"))
    dry_run = bool(cfg.get("dry_run", False)) or (
        bool(micro_cfg.get("enabled", False)) and bool(micro_cfg.get("dry_run", False))
    )
    live_order_enabled = (
        stage == "LIVE_REAL_SMALL_CAP"
        and bool(cfg.get("real_order_enabled", False))
        and bool(cfg.get("live_trading", False))
        and not bool(cfg.get("testnet", False))
    )
    demo_order_enabled = (
        mode == BINANCE_TESTNET
        or stage == "TESTNET_ONLY"
        or bool(cfg.get("testnet", False))
    ) and not dry_run
    paper_order_enabled = dry_run or stage in {"PAPER_ONLY", "DISABLED"}
    order_executor = (
        "execute_live_order_plan"
        if bool(cfg.get("live_parity_signal_enabled", False))
        else "legacy_signal_entry"
    )
    if dry_run:
        order_action = "signal_only"
        reason = "dry_run_or_micro_auto_live_locked"
    elif live_order_enabled:
        order_action = "ready_to_order"
        reason = "LIVE_REAL_SMALL_CAP preflight still required"
    elif demo_order_enabled:
        order_action = "testnet_or_demo_order"
        reason = "demo/testnet exchange path"
    else:
        order_action = "signal_only"
        reason = "live order path not enabled"
    return {
        "exchange_mode": mode,
        "trading_mode": stage,
        "live_order_enabled": bool(live_order_enabled),
        "paper_order_enabled": bool(paper_order_enabled),
        "demo_order_enabled": bool(demo_order_enabled),
        "dry_run": bool(dry_run),
        "order_executor": order_executor,
        "order_action": order_action,
        "reason": reason,
    }


def format_utbreakout_order_path_summary(summary):
    summary = summary if isinstance(summary, dict) else {}
    return (
        "Order Path: "
        f"exchange_mode={summary.get('exchange_mode')} / "
        f"trading_mode={summary.get('trading_mode')} / "
        f"live_order_enabled={str(bool(summary.get('live_order_enabled'))).lower()} / "
        f"paper_order_enabled={str(bool(summary.get('paper_order_enabled'))).lower()} / "
        f"demo_order_enabled={str(bool(summary.get('demo_order_enabled'))).lower()} / "
        f"dry_run={str(bool(summary.get('dry_run'))).lower()} / "
        f"order_executor={summary.get('order_executor')} / "
        f"order_action={summary.get('order_action')} / "
        f"reason={summary.get('reason')}"
    )


def _normalize_tp_plan_label(value, fallback=None):
    text = str(value or fallback or '').strip().upper()
    compact = re.sub(r'[^A-Z0-9]', '', text)
    if compact in {'TP1', 'TAKEPROFIT1'}:
        return 'TP1'
    if compact in {'TP2', 'TAKEPROFIT2'}:
        return 'TP2'
    if compact in {'TP3', 'TAKEPROFIT3'}:
        return 'TP3'
    return text or str(fallback or '').strip().upper()


def _calculate_residual_tp_quantities(symbol, total_qty, raw_quantities, safe_amount_fn):
    total = _safe_float_value(safe_amount_fn(symbol, abs(_safe_float_value(total_qty))), 0.0)
    raw_list = [max(0.0, _safe_float_value(qty)) for qty in (raw_quantities or [])]
    if total <= 0 or not raw_list:
        return []
    if len(raw_list) == 1:
        single_qty = min(raw_list[0], total)
        return [max(0.0, _safe_float_value(safe_amount_fn(symbol, single_qty), 0.0))]

    quantities = []
    remaining = total
    for index, raw_qty in enumerate(raw_list):
        is_final = index == len(raw_list) - 1
        desired = remaining if is_final else min(raw_qty, remaining)
        qty = _safe_float_value(safe_amount_fn(symbol, max(0.0, desired)), 0.0)
        if qty > remaining:
            qty = _safe_float_value(safe_amount_fn(symbol, remaining), 0.0)
        quantities.append(max(0.0, qty))
        remaining = max(0.0, remaining - quantities[-1])

    total_after = sum(quantities)
    tolerance = max(1e-12, total * 1e-8)
    if total_after > total + tolerance and quantities:
        preceding = sum(quantities[:-1])
        quantities[-1] = max(0.0, _safe_float_value(safe_amount_fn(symbol, max(0.0, total - preceding)), 0.0))
    return quantities


def configure_binance_futures_environment(exchange, cfg):
    if exchange is None:
        raise TradingSafetyError("exchange is required")
    cfg = cfg if isinstance(cfg, dict) else {}
    stage = _normalize_live_real_stage(cfg.get("live_activation_stage"))
    if stage == "LIVE_REAL_SMALL_CAP" and bool(cfg.get("testnet", False)):
        raise TradingSafetyError("LIVE_REAL_SMALL_CAP must not use Binance testnet/sandbox")

    options = dict(getattr(exchange, "options", {}) or {})
    options["defaultType"] = "future"
    options["adjustForTimeDifference"] = True
    exchange.options = options

    sandbox = stage == "TESTNET_ONLY" or bool(cfg.get("testnet", False))
    if hasattr(exchange, "set_sandbox_mode"):
        exchange.set_sandbox_mode(bool(sandbox))
    return {
        "defaultType": exchange.options.get("defaultType"),
        "adjustForTimeDifference": exchange.options.get("adjustForTimeDifference"),
        "sandbox": bool(sandbox),
    }


def _parse_binance_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    return None


async def assert_binance_futures_one_way_mode(self, cfg):
    cfg = cfg if isinstance(cfg, dict) else {}
    if _normalize_live_real_stage(cfg.get("live_activation_stage")) != "LIVE_REAL_SMALL_CAP":
        return {"status": "SKIPPED"}

    exchange = getattr(self, "exchange", None)
    if exchange is None:
        raise TradingSafetyError("could not verify Binance Futures position mode: exchange is unavailable")

    method = getattr(exchange, "fapiPrivateGetPositionSideDual", None)
    if method is None:
        method = getattr(exchange, "fapiPrivate_get_position_side_dual", None)
    if method is None or not callable(method):
        raise TradingSafetyError("could not verify Binance Futures position mode")

    try:
        response = await asyncio.to_thread(method)
    except Exception as exc:
        raise TradingSafetyError(f"could not verify Binance Futures position mode: {exc}") from exc

    raw = None
    if isinstance(response, dict):
        raw = response.get("dualSidePosition")
        if raw is None and isinstance(response.get("info"), dict):
            raw = response["info"].get("dualSidePosition")
    hedged = _parse_binance_bool(raw)
    if hedged is None:
        raise TradingSafetyError(f"could not verify Binance Futures position mode: {response}")
    if hedged:
        raise TradingSafetyError(
            "LIVE trading blocked: Binance Futures account is in hedge mode. "
            "Switch to one-way mode or enable hedge-mode support."
        )
    return {"status": "OK", "hedge_mode": False}


def assert_symbol_allowed_for_live_real(symbol, cfg):
    cfg = cfg if isinstance(cfg, dict) else {}
    stage = _normalize_live_real_stage(cfg.get("live_activation_stage"))
    if stage != "LIVE_REAL_SMALL_CAP":
        return True

    normalized = _normalize_live_real_symbol(symbol)
    allowlist = cfg.get("live_real_allowlist", LIVE_REAL_ALLOWED_SYMBOLS_DEFAULT)
    blocklist = cfg.get("live_real_blocklist", [])
    allowed = {_normalize_live_real_symbol(item) for item in (allowlist or []) if _normalize_live_real_symbol(item)}
    blocked = {_normalize_live_real_symbol(item) for item in (blocklist or []) if _normalize_live_real_symbol(item)}

    if normalized in blocked:
        raise TradingSafetyError(f"{symbol} is blocked for LIVE_REAL_SMALL_CAP")
    if allowed and normalized not in allowed:
        raise TradingSafetyError(f"{symbol} is not in LIVE_REAL_SMALL_CAP allowlist")

    compact = normalized.replace("/", "")
    if any(fragment in normalized or fragment.replace("/", "") in compact for fragment in LIVE_REAL_BLOCKED_SYMBOL_FRAGMENTS):
        raise TradingSafetyError(f"{symbol} looks like a leveraged token or tokenized stock and is blocked")
    return True


def _live_real_period_keys(now=None, cfg=None):
    _, tz = _live_real_timezone(cfg)
    current = now or datetime.now(timezone.utc)
    if getattr(current, 'tzinfo', None) is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(tz)
    iso = local.isocalendar()
    return local.strftime('%Y-%m-%d'), f'{iso.year}-W{iso.week:02d}'


def load_live_real_risk_state(cfg=None, now=None, *, fail_closed=True):
    today, week = _live_real_period_keys(now, cfg)
    default_state = normalize_live_real_risk_state({
        'date': today,
        'week': week,
        'daily_realized_pnl_usdt': 0.0,
        'weekly_realized_pnl_usdt': 0.0,
    }, cfg, now)
    if not os.path.exists(LIVE_REAL_RISK_STATE_FILE):
        return default_state

    try:
        with open(LIVE_REAL_RISK_STATE_FILE, 'r', encoding='utf-8') as f:
            state = json.load(f)
        if not isinstance(state, dict):
            raise ValueError('risk state root must be a JSON object')
        numeric_fields = (
            'daily_realized_pnl_usdt',
            'weekly_realized_pnl_usdt',
            'daily_loss_usdt',
            'weekly_loss_usdt',
            'risk_pct_increases_today',
        )
        for key in numeric_fields:
            if key not in state:
                continue
            value = float(state[key])
            if not math.isfinite(value):
                raise ValueError(f'risk state field {key} is not finite')
    except Exception as exc:
        message = (
            'LIVE_REAL_RISK_STATE_UNREADABLE: persisted loss state cannot be trusted; '
            f'new entries are blocked ({type(exc).__name__}: {exc})'
        )
        logger.critical(message)
        if fail_closed:
            raise LiveRealRiskStateUnreadable(message) from exc
        blocked = dict(default_state)
        blocked.update({
            'risk_state_unreadable': True,
            'trading_blocked': True,
            'reason_code': 'LIVE_REAL_RISK_STATE_UNREADABLE',
            'error': f'{type(exc).__name__}: {exc}',
        })
        return blocked
    return normalize_live_real_risk_state(state, cfg, now)


def save_live_real_risk_state(state):
    atomic_write_json(
        LIVE_REAL_RISK_STATE_FILE,
        state if isinstance(state, dict) else {},
        indent=4,
    )
    return state


def record_bot_realized_pnl(pnl_usdt, *, now=None, cfg=None):
    """Persist bot-confirmed realized PnL once the matching DB trade is closed."""
    state = normalize_live_real_risk_state(
        load_live_real_risk_state(cfg, now),
        cfg,
        now,
    )
    pnl = _safe_float_value(pnl_usdt, 0.0)
    state['daily_realized_pnl_usdt'] = (
        _safe_float_value(state.get('daily_realized_pnl_usdt'), 0.0) + pnl
    )
    state['weekly_realized_pnl_usdt'] = (
        _safe_float_value(state.get('weekly_realized_pnl_usdt'), 0.0) + pnl
    )
    state['last_realized_pnl_usdt'] = pnl
    changed_at = now or datetime.now(timezone.utc)
    if getattr(changed_at, 'tzinfo', None) is None:
        changed_at = changed_at.replace(tzinfo=timezone.utc)
    state['last_realized_pnl_at'] = changed_at.isoformat()
    save_live_real_risk_state(state)
    return state


def _realized_loss_from_state(state, pnl_key, loss_key):
    loss = 0.0
    try:
        pnl = float((state or {}).get(pnl_key, 0.0) or 0.0)
        if pnl < 0:
            loss = max(loss, abs(pnl))
    except Exception:
        pass
    try:
        explicit_loss = float((state or {}).get(loss_key, 0.0) or 0.0)
        if explicit_loss > 0:
            loss = max(loss, explicit_loss)
    except Exception:
        pass
    return loss


def assert_live_real_loss_limits_not_exceeded(cfg):
    cfg = cfg if isinstance(cfg, dict) else {}
    if _normalize_live_real_stage(cfg.get("live_activation_stage")) != "LIVE_REAL_SMALL_CAP":
        return True
    apply_live_small_cap_limits_to_config(cfg, cfg.get("account_reference_equity_usdt"))
    state = load_live_real_risk_state()
    daily_loss = _realized_loss_from_state(state, "daily_realized_pnl_usdt", "daily_loss_usdt")
    weekly_loss = _realized_loss_from_state(state, "weekly_realized_pnl_usdt", "weekly_loss_usdt")
    max_daily = float(cfg.get("max_daily_real_loss_usdt", 0.0) or 0.0)
    max_weekly = float(cfg.get("max_weekly_real_loss_usdt", 0.0) or 0.0)
    if daily_loss >= max_daily:
        raise TradingPausedError(f"daily real loss limit reached: {daily_loss:.4f} >= {max_daily:.4f}")
    if weekly_loss >= max_weekly:
        raise TradingPausedError(f"weekly real loss limit reached: {weekly_loss:.4f} >= {max_weekly:.4f}")
    return True


def validate_min_notional_against_live_cap(min_notional, cfg):
    cfg = cfg if isinstance(cfg, dict) else {}
    if _normalize_live_real_stage(cfg.get("live_activation_stage")) != "LIVE_REAL_SMALL_CAP":
        return True
    limits = resolve_live_small_cap_limits(cfg, cfg.get("account_reference_equity_usdt"))
    cap = float(limits.get("max_position_notional_usdt") or 0.0)
    if float(min_notional) > cap:
        raise InvalidOrderPlan(f"minNotional {float(min_notional):.4f} exceeds live real notional cap {cap:.4f}")
    return True


def _live_real_entry_reference(plan, cfg):
    entry_ref = getattr(plan, "entry_price", None) or (cfg or {}).get("last_price") or (cfg or {}).get("last_context_close") or 0.0
    try:
        return float(entry_ref)
    except Exception:
        return 0.0


def _scale_live_order_plan_qty(plan, new_qty):
    old_qty = float(getattr(plan, "qty", 0.0) or 0.0)
    new_qty = float(new_qty)
    if new_qty <= 0:
        raise InvalidOrderPlan("qty <= 0 after LIVE_REAL_SMALL_CAP scaling")
    ratio = new_qty / old_qty if old_qty > 0 else 0.0
    plan.qty = new_qty
    for tp in getattr(plan, "tp_orders", []) or []:
        tp.qty = float(tp.qty) * ratio
    return plan


def enforce_live_real_order_caps(plan, context, cfg):
    cfg = cfg if isinstance(cfg, dict) else {}
    if _normalize_live_real_stage(cfg.get("live_activation_stage")) != "LIVE_REAL_SMALL_CAP":
        return plan
    limits = apply_live_small_cap_limits_to_config(cfg, cfg.get("account_reference_equity_usdt"))
    entry_ref = float(getattr(plan, "entry_price", None) or getattr(context, "close", 0.0) or cfg.get("last_price") or 0.0)
    if entry_ref <= 0:
        raise InvalidOrderPlan("missing entry reference price for LIVE_REAL_SMALL_CAP cap check")
    original_qty = float(plan.qty)
    qty = original_qty
    notional = qty * entry_ref
    notional_cap = float(limits.get("max_position_notional_usdt") or 0.0)
    cap_reasons = []
    if notional > notional_cap:
        qty = notional_cap / entry_ref
        cap_reasons.append("notional_cap")

    stop_ref = float(plan.initial_sl_price)
    risk_per_unit = abs(entry_ref - stop_ref)
    loss_cap = float(limits.get("max_loss_per_trade_usdt") or 0.0)
    if risk_per_unit <= 0:
        raise InvalidOrderPlan("invalid SL distance for LIVE_REAL_SMALL_CAP cap check")
    if qty * risk_per_unit > loss_cap:
        qty = loss_cap / risk_per_unit
        cap_reasons.append("loss_cap")
    plan = _scale_live_order_plan_qty(plan, qty)
    plan.live_real_effective_limits = dict(limits)
    plan.live_real_cap_reasons = cap_reasons
    plan.live_real_original_qty = original_qty
    return plan


def validate_live_real_order_caps_after_rounding(plan, cfg):
    cfg = cfg if isinstance(cfg, dict) else {}
    if _normalize_live_real_stage(cfg.get("live_activation_stage")) != "LIVE_REAL_SMALL_CAP":
        return True
    limits = apply_live_small_cap_limits_to_config(cfg, cfg.get("account_reference_equity_usdt"))
    entry_ref = _live_real_entry_reference(plan, cfg)
    if entry_ref <= 0:
        raise InvalidOrderPlan("missing entry reference price for LIVE_REAL_SMALL_CAP rounded cap check")
    qty = float(plan.qty)
    notional_cap = float(limits.get("max_position_notional_usdt") or 0.0)
    loss_cap = float(limits.get("max_loss_per_trade_usdt") or 0.0)
    notional = qty * entry_ref
    loss = qty * abs(entry_ref - float(plan.initial_sl_price))
    if notional > notional_cap * 1.001:
        raise InvalidOrderPlan(f"LIVE_REAL_SMALL_CAP notional cap exceeded after rounding: {notional:.4f} > {notional_cap:.4f}")
    if loss > loss_cap * 1.001:
        raise InvalidOrderPlan(f"LIVE_REAL_SMALL_CAP loss cap exceeded after rounding: {loss:.4f} > {loss_cap:.4f}")
    return True

def calculate_initial_stop_price(side, entry_price, atr, config, engine="NONE"):
    stop_distance = float(config.get("initial_stop_atr_distance", 2.0) or 2.0) * float(atr)
    if side == "LONG":
        return float(entry_price) - stop_distance
    else:
        return float(entry_price) + stop_distance

def calculate_position_qty_by_risk(equity, risk_pct, entry_price, stop_price, symbol, config):
    risk_usdt = float(equity) * (float(risk_pct) / 100.0)
    price_distance = abs(float(entry_price) - float(stop_price))
    if price_distance <= 0:
        return 0.0
    qty = risk_usdt / price_distance
    return qty

def calculate_tp_price(side, entry_price, stop_price, r_multiple):
    price_distance = abs(float(entry_price) - float(stop_price))
    if side == "LONG":
        return float(entry_price) + (price_distance * float(r_multiple))
    else:
        return float(entry_price) - (price_distance * float(r_multiple))

def build_ladder_tp_orders(side, entry_price, qty, ladder, config):
    if ladder is None:
        ladder = LadderTP(1.0, 50.0, 1.6, 50.0)

    exit_side = "sell" if side == "LONG" else "buy"
    targets = []
    targets.append(("TP1", ladder.tp1_r, ladder.tp1_pct))
    targets.append(("TP2", ladder.tp2_r, ladder.tp2_pct))
    if getattr(ladder, "tp3_r", None) is not None and getattr(ladder, "tp3_pct", None) is not None:
        targets.append(("TP3", ladder.tp3_r, ladder.tp3_pct))

    total_pct = sum(pct for _, _, pct in targets)
    if abs(total_pct - 100.0) > 0.001:
        raise InvalidOrderPlan("ladder TP percentages must sum to 100")

    orders = []
    for index, (name, r_mult, pct) in enumerate(targets, 1):
        tp_price = calculate_tp_price(side, entry_price, config["initial_stop_price"], r_mult)
        tp_qty = qty * (pct / 100.0)

        orders.append(TakeProfitOrderPlan(
            tp_name=name,
            side=exit_side,
            price=tp_price,
            qty=tp_qty,
            reduce_only=True,
            close_position=False,
            r_multiple=r_mult,
            pct=pct,
            tp_index=index,
            tp_label=name,
        ))
    return orders

def build_live_order_plan_from_decision(symbol, decision, context, cfg, *, account_equity):
    if account_equity is None or float(account_equity) <= 0:
        raise InvalidOrderPlan("live order plan requires real account equity")
    equity = float(account_equity)
    limits = None
    if _normalize_live_real_stage((cfg or {}).get("live_activation_stage")) == "LIVE_REAL_SMALL_CAP":
        limits = apply_live_small_cap_limits_to_config(cfg, equity)

    requested_risk_pct = float((limits or {}).get("risk_pct") or cfg.get("risk_per_trade_pct") or cfg.get("risk_per_trade_percent") or decision.risk_pct)
    decision_risk_pct = _safe_float_value(getattr(decision, "risk_pct", None), requested_risk_pct)
    if decision_risk_pct <= 0:
        decision_risk_pct = requested_risk_pct
    risk_pct = min(
        float(decision_risk_pct),
        float(requested_risk_pct),
        float(cfg.get("max_risk_per_trade_pct", requested_risk_pct)),
    )
    if risk_pct <= 0:
        raise InvalidOrderPlan("risk_pct <= 0")

    sl_price = calculate_initial_stop_price(
        side=decision.side,
        entry_price=context.close,
        atr=context.atr,
        config=cfg,
        engine=decision.engine,
    )

    qty = calculate_position_qty_by_risk(
        equity=equity,
        risk_pct=risk_pct,
        entry_price=context.close,
        stop_price=sl_price,
        symbol=symbol,
        config=cfg,
    )

    if qty <= 0:
        raise InvalidOrderPlan("qty <= 0")

    tp_config = dict(cfg)
    tp_config["symbol"] = symbol
    tp_config["initial_stop_price"] = sl_price

    tp_orders = build_ladder_tp_orders(
        side=decision.side,
        entry_price=context.close,
        qty=qty,
        ladder=decision.ladder_tp,
        config=tp_config,
    )

    plan = LiveOrderPlan(
        symbol=symbol,
        side=decision.side,
        entry_type=cfg.get("entry_order_type", "MARKET"),
        entry_price=None if cfg.get("entry_order_type", "MARKET") == "MARKET" else context.close,
        qty=qty,
        risk_pct=risk_pct,
        initial_sl_price=sl_price,
        tp_orders=tp_orders,
        ladder=decision.ladder_tp,
        reduce_only=False,
        engine=decision.engine,
        regime=decision.regime,
        confidence=decision.confidence,
        expected_r=decision.expected_r,
        reasons=decision.reasons,
    )
    if limits is not None:
        plan.live_real_effective_limits = dict(limits)
    return plan

def validate_live_order_plan(plan, cfg):
    if plan.qty <= 0:
        raise InvalidOrderPlan("qty <= 0")
    if plan.initial_sl_price <= 0:
        raise InvalidOrderPlan("invalid SL price")
    if not plan.tp_orders:
        raise InvalidOrderPlan("no TP orders")
    if plan.risk_pct <= 0:
        raise InvalidOrderPlan("risk_pct <= 0")
    if plan.risk_pct > float(cfg.get("max_risk_per_trade_pct", 1.0)):
        raise InvalidOrderPlan("risk exceeds max")

    tp_qty_sum = sum(tp.qty for tp in plan.tp_orders)
    if tp_qty_sum > plan.qty * 1.0001:
        raise InvalidOrderPlan("TP qty exceeds position qty")

    for tp in plan.tp_orders:
        if not tp.reduce_only:
            raise InvalidOrderPlan("TP must be reduceOnly")
        if tp.close_position:
            raise InvalidOrderPlan("partial TP must not use closePosition")
    return True

__all__ = (
    'TakeProfitOrderPlan',
    'LiveOrderPlan',
    'apply_runtime_safety_defaults',
    'LIVE_REAL_CONFIRM_TEXT',
    'LIVE_REAL_ALLOWED_SYMBOLS_DEFAULT',
    'LIVE_REAL_BLOCKED_SYMBOL_FRAGMENTS',
    '_normalize_live_real_stage',
    '_normalize_live_real_symbol',
    'enforce_activation_stage',
    'PAUSE_STATE_FILE',
    'LIVE_REAL_RISK_STATE_FILE',
    'LIVE_REAL_SMALL_CAP_DEFAULTS',
    'evaluate_critical_pause_block',
    'write_critical_pause_state',
    'load_critical_pause_state',
    'assert_trading_allowed',
    'manual_resume_trading',
    'InvalidOrderPlan',
    'TradingSafetyError',
    'TradingPausedError',
    'LiveRealRiskStateUnreadable',
    '_safe_float_value',
    '_bounded_fraction',
    '_live_real_config_float',
    '_live_real_timezone',
    '_live_real_kst_date_key',
    '_live_real_min_max_risk_fraction',
    '_live_real_risk_fraction_from_cfg',
    'parse_live_real_risk_pct_input',
    'normalize_live_real_risk_state',
    'apply_live_real_risk_pct_change',
    'resolve_live_small_cap_limits',
    'apply_live_small_cap_limits_to_config',
    'LIVE_REAL_RISK_BUTTON_PRESETS',
    'build_live_real_risk_config',
    'build_live_real_risk_menu_keyboard',
    'build_live_real_risk_status_keyboard',
    'build_live_real_risk_confirm_keyboard',
    'append_live_real_risk_buttons_to_utbreakout_rows',
    '_format_live_real_risk_percent',
    '_format_live_real_risk_history',
    'render_live_real_risk_status_text',
    'render_live_real_risk_manual_text',
    'build_utbreakout_order_path_summary',
    'format_utbreakout_order_path_summary',
    '_normalize_tp_plan_label',
    '_calculate_residual_tp_quantities',
    'configure_binance_futures_environment',
    '_parse_binance_bool',
    'assert_binance_futures_one_way_mode',
    'assert_symbol_allowed_for_live_real',
    '_live_real_period_keys',
    'load_live_real_risk_state',
    'save_live_real_risk_state',
    'record_bot_realized_pnl',
    '_realized_loss_from_state',
    'assert_live_real_loss_limits_not_exceeded',
    'validate_min_notional_against_live_cap',
    '_live_real_entry_reference',
    '_scale_live_order_plan_qty',
    'enforce_live_real_order_caps',
    'validate_live_real_order_caps_after_rounding',
    'calculate_initial_stop_price',
    'calculate_position_qty_by_risk',
    'calculate_tp_price',
    'build_ladder_tp_orders',
    'build_live_order_plan_from_decision',
    'validate_live_order_plan',
)
