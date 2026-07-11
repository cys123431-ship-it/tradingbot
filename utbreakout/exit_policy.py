"""Exit-policy helpers for UT Breakout research and backtests.

The functions here are deliberately pure. They describe policy intent and
evaluate bar/context state, but they never place or cancel orders.
"""

from dataclasses import dataclass
from math import isfinite


DEFAULT_EXIT_POLICY = "HYBRID_DEFENSIVE_FIXED_TP"

EXIT_POLICY_CANDIDATES = (
    "FIXED_TP",
    "FIXED_TP_TIME_STOP",
    "FIXED_TP_SIGNAL_INVALID",
    "VOL_ADAPTIVE_TP",
    "HYBRID_DEFENSIVE",
)


@dataclass(frozen=True)
class ExitPolicy:
    name: str
    tp1_r: float = 1.5
    tp1_size_pct: float = 50.0
    tp2_r: float = 2.0
    move_sl_to_be_after_tp1: bool = True
    runner_enabled: bool = False
    time_stop_enabled: bool = True
    signal_invalid_exit_enabled: bool = True
    volatility_adaptive_tp_enabled: bool = True


@dataclass(frozen=True)
class ExitSignal:
    should_exit: bool = False
    exit_type: str = "NONE"
    reason: str = "NO_EXIT"
    policy_name: str | None = None

    @classmethod
    def none(cls):
        return cls()


@dataclass(frozen=True)
class TpConfig:
    tp1_r: float
    tp2_r: float
    risk_multiplier: float = 1.0
    reason: str = "NORMAL_VOL_DEFAULT_TP"


@dataclass(frozen=True)
class PolicyStats:
    trade_count: int = 0
    expectancy_r: float = 0.0
    oos_expectancy_r: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    max_consecutive_losses: int = 0
    fee_burden: float = 0.0
    total_return_pct: float = 0.0


class PolicyStatsStore:
    """Small adapter for dict-backed policy statistics."""

    def __init__(self, rows=None):
        self.rows = rows or {}

    def get_stats(self, *, policy_name, symbol=None, timeframe=None, regime=None, side=None):
        keys = [
            (policy_name, symbol, timeframe, regime, side),
            (policy_name, symbol, timeframe, regime),
            (policy_name, symbol, timeframe),
            policy_name,
        ]
        for key in keys:
            if key in self.rows:
                return coerce_policy_stats(self.rows[key])
        return PolicyStats()


def _finite_float(value, default=None):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _field(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _bool_cfg(config, key, default=True):
    value = (config or {}).get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _policy_name(name):
    name = str(name or DEFAULT_EXIT_POLICY).upper()
    if name == DEFAULT_EXIT_POLICY:
        return "HYBRID_DEFENSIVE"
    if name == "HYBRID_DEFENSIVE_FIXED_TP":
        return "HYBRID_DEFENSIVE"
    return name


def build_volatility_adaptive_tp(context=None, config=None):
    config = config or {}
    atr_pctile = _finite_float(
        _field(context, "atr_percentile", _field(context, "atr_pctile", None))
    )
    if atr_pctile is None:
        return TpConfig(
            tp1_r=_finite_float(config.get("normal_vol_tp1_r"), 1.5),
            tp2_r=_finite_float(config.get("normal_vol_tp2_r"), 2.0),
            risk_multiplier=1.0,
        )

    if atr_pctile <= 20:
        tp1 = _finite_float(config.get("low_vol_tp1_r"), 1.0)
        tp2 = _finite_float(config.get("low_vol_tp2_r"), 1.5)
        return TpConfig(
            tp1_r=tp1,
            tp2_r=max(tp2, tp1 + 0.1),
            risk_multiplier=_finite_float(config.get("low_vol_risk_mult"), 0.7),
            reason="LOW_VOL_TP_SHORTENED",
        )
    if atr_pctile >= 85:
        tp1 = _finite_float(config.get("high_vol_tp1_r"), 1.2)
        tp2 = _finite_float(config.get("high_vol_tp2_r"), 1.8)
        return TpConfig(
            tp1_r=tp1,
            tp2_r=max(tp2, tp1 + 0.1),
            risk_multiplier=_finite_float(config.get("high_vol_risk_mult"), 0.5),
            reason="HIGH_VOL_RISK_REDUCED",
        )

    tp1 = _finite_float(config.get("normal_vol_tp1_r"), 1.5)
    tp2 = _finite_float(config.get("normal_vol_tp2_r"), 2.0)
    return TpConfig(
        tp1_r=tp1,
        tp2_r=max(tp2, tp1 + 0.1),
        risk_multiplier=1.0,
        reason="NORMAL_VOL_DEFAULT_TP",
    )


def build_exit_policy(name=None, context=None, config=None):
    config = dict(config or {})
    canonical = _policy_name(name or config.get("default_exit_policy"))
    if canonical not in EXIT_POLICY_CANDIDATES:
        canonical = "HYBRID_DEFENSIVE"

    time_stop = canonical in {"FIXED_TP_TIME_STOP", "HYBRID_DEFENSIVE"}
    invalid_exit = canonical in {"FIXED_TP_SIGNAL_INVALID", "HYBRID_DEFENSIVE"}
    vol_adaptive = canonical in {"VOL_ADAPTIVE_TP", "HYBRID_DEFENSIVE"}

    tp1_r = _finite_float(config.get("tp1_r", config.get("partial_take_profit_r_multiple")), 1.5)
    tp2_r = _finite_float(config.get("tp2_r", config.get("second_take_profit_r_multiple")), 2.0)
    if vol_adaptive:
        tp_config = build_volatility_adaptive_tp(context, config)
        tp1_r = tp_config.tp1_r
        tp2_r = tp_config.tp2_r
    tp2_r = max(tp2_r, tp1_r + 0.1)

    return ExitPolicy(
        name=DEFAULT_EXIT_POLICY if canonical == "HYBRID_DEFENSIVE" else canonical,
        tp1_r=tp1_r,
        tp1_size_pct=_finite_float(config.get("tp1_size_pct", config.get("partial_take_profit_ratio", 0.5) * 100.0), 50.0),
        tp2_r=tp2_r,
        move_sl_to_be_after_tp1=True,
        runner_enabled=False,
        time_stop_enabled=time_stop,
        signal_invalid_exit_enabled=invalid_exit,
        volatility_adaptive_tp_enabled=vol_adaptive,
    )


def build_default_exit_policy(config=None):
    return build_exit_policy(DEFAULT_EXIT_POLICY, config=config)


def _timeframe_default_max_bars(timeframe):
    return {
        "5m": 12,
        "15m": 8,
        "30m": 6,
        "1h": 4,
    }.get(str(timeframe or "").lower(), 8)


def evaluate_time_stop(position, current_bar, config=None):
    config = config or {}
    if position is None:
        return ExitSignal.none()
    if not _bool_cfg(config, "time_stop_enabled", True):
        return ExitSignal.none()
    if bool(_field(position, "tp1_filled", _field(position, "partial_filled", False))):
        return ExitSignal.none()

    timeframe = _field(position, "timeframe", config.get("timeframe", "15m"))
    default_max = _timeframe_default_max_bars(timeframe)
    raw_max = config.get("max_bars_to_tp1", default_max)
    if isinstance(raw_max, dict):
        raw_max = raw_max.get(str(timeframe), raw_max.get(str(timeframe).lower(), default_max))
    max_bars = max(1, int(_finite_float(raw_max, default_max) or default_max))
    entry_index = int(_finite_float(_field(position, "entry_bar_index", _field(position, "entry_idx", 0)), 0) or 0)
    current_index = int(_finite_float(_field(current_bar, "index", _field(current_bar, "idx", 0)), 0) or 0)
    if current_index - entry_index >= max_bars:
        current_r = _finite_float(_field(position, "current_r", None))
        max_current_r = _finite_float(config.get("time_stop_max_current_r"), 0.0)
        if current_r is not None and current_r > max_current_r:
            return ExitSignal.none()
        return ExitSignal(True, "MARKET", "TIME_STOP_NO_FOLLOW_THROUGH")
    return ExitSignal.none()


def evaluate_signal_invalid_exit(position, context, config=None):
    config = config or {}
    if position is None:
        return ExitSignal.none()
    if not _bool_cfg(config, "signal_invalid_exit_enabled", True):
        return ExitSignal.none()

    side = str(_field(position, "side", "") or "").upper()
    if side not in {"LONG", "SHORT"}:
        side = str(_field(position, "side", "") or "").lower()
        side = "LONG" if side == "long" else "SHORT" if side == "short" else side
    if side not in {"LONG", "SHORT"}:
        return ExitSignal.none()

    utbot_direction = str(_field(context, "utbot_direction", "") or "").upper()
    plus_di = _finite_float(_field(context, "plus_di", None))
    minus_di = _finite_float(_field(context, "minus_di", None))
    close = _finite_float(_field(context, "close", None))
    donchian_mid = _finite_float(_field(context, "donchian_mid", None))
    htf_trend = str(_field(context, "htf_trend", "") or "").upper()
    reasons = []

    if side == "LONG":
        if utbot_direction == "SHORT":
            reasons.append("UTBOT_REVERSAL")
        if plus_di is not None and minus_di is not None and minus_di > plus_di:
            reasons.append("DMI_REVERSAL")
        if close is not None and donchian_mid is not None and close < donchian_mid:
            reasons.append("DONCHIAN_MID_LOST")
        if _bool_cfg(config, "htf_invalid_exit_enabled", True) and htf_trend and htf_trend != "UP":
            reasons.append("HTF_TREND_LOST")
    else:
        if utbot_direction == "LONG":
            reasons.append("UTBOT_REVERSAL")
        if plus_di is not None and minus_di is not None and plus_di > minus_di:
            reasons.append("DMI_REVERSAL")
        if close is not None and donchian_mid is not None and close > donchian_mid:
            reasons.append("DONCHIAN_MID_LOST")
        if _bool_cfg(config, "htf_invalid_exit_enabled", True) and htf_trend and htf_trend != "DOWN":
            reasons.append("HTF_TREND_LOST")

    if reasons:
        return ExitSignal(True, "MARKET", "SIGNAL_INVALID_" + "_".join(reasons))
    return ExitSignal.none()


def coerce_policy_stats(value):
    if isinstance(value, PolicyStats):
        return value
    value = dict(value or {})
    expectancy = _finite_float(value.get("expectancy_r", value.get("average_R")), 0.0)
    return PolicyStats(
        trade_count=int(_finite_float(value.get("trade_count", value.get("trades")), 0) or 0),
        expectancy_r=expectancy,
        oos_expectancy_r=_finite_float(value.get("oos_expectancy_r"), expectancy),
        profit_factor=_finite_float(value.get("profit_factor"), 0.0),
        max_drawdown_pct=_finite_float(value.get("max_drawdown_pct"), 0.0),
        max_consecutive_losses=int(_finite_float(value.get("max_consecutive_losses"), 0) or 0),
        fee_burden=_finite_float(value.get("fee_burden", value.get("fee_burden_pct")), 0.0),
        total_return_pct=_finite_float(value.get("total_return_pct"), 0.0),
    )


def _normalize_positive(value, scale):
    value = _finite_float(value, 0.0)
    return max(0.0, min(1.0, value / max(float(scale), 1e-9)))


def choose_exit_policy(signal, context, historical_policy_stats=None, config=None):
    config = dict(config or {})
    forced = config.get("forced_exit_policy")
    if forced:
        return build_exit_policy(forced, context, config)
    if not _bool_cfg(config, "exit_policy_selector_enabled", True):
        return build_exit_policy("HYBRID_DEFENSIVE", context, config)

    stats_store = historical_policy_stats
    if not hasattr(stats_store, "get_stats"):
        stats_store = PolicyStatsStore(stats_store or {})

    ranked = []
    for policy_name in EXIT_POLICY_CANDIDATES:
        stats = stats_store.get_stats(
            policy_name=policy_name,
            symbol=_field(context, "symbol"),
            timeframe=_field(context, "timeframe"),
            regime=_field(context, "regime"),
            side=_field(signal, "side"),
        )
        if stats.trade_count < int(config.get("min_policy_trade_count", 30) or 30):
            continue
        if stats.oos_expectancy_r <= 0:
            continue
        if stats.profit_factor < _finite_float(config.get("min_policy_profit_factor"), 1.05):
            continue
        if stats.max_drawdown_pct > _finite_float(config.get("max_policy_drawdown_pct"), 15.0):
            continue
        score = (
            0.35 * _normalize_positive(stats.expectancy_r, 2.0)
            + 0.25 * _normalize_positive(stats.profit_factor - 1.0, 2.0)
            - 0.20 * _normalize_positive(stats.max_drawdown_pct, 30.0)
            - 0.10 * _normalize_positive(stats.max_consecutive_losses, 10.0)
            - 0.10 * _normalize_positive(stats.fee_burden, 0.5)
        )
        ranked.append((score, policy_name))

    if not ranked:
        return build_exit_policy("HYBRID_DEFENSIVE", context, config)
    ranked.sort(reverse=True)
    return build_exit_policy(ranked[0][1], context, config)


def rank_exit_policies(results, config=None):
    config = dict(config or {})
    ranked = []
    for name, report in (results or {}).items():
        stats = coerce_policy_stats(report)
        if stats.trade_count < int(config.get("min_policy_trade_count", 30) or 30):
            continue
        if stats.expectancy_r <= 0:
            continue
        if stats.profit_factor < _finite_float(config.get("min_policy_profit_factor"), 1.05):
            continue
        score = (
            0.35 * _normalize_positive(stats.expectancy_r, 2.0)
            + 0.25 * _normalize_positive(stats.profit_factor - 1.0, 2.0)
            - 0.20 * _normalize_positive(stats.max_drawdown_pct, 30.0)
            - 0.10 * _normalize_positive(stats.max_consecutive_losses, 10.0)
            - 0.10 * _normalize_positive(stats.fee_burden, 0.5)
        )
        ranked.append((round(score, 8), name, report))
    ranked.sort(reverse=True)
    return ranked
