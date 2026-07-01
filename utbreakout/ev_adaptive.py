"""Cost-aware, regime-adaptive policy for UT Breakout.

This module is intentionally pure.  It classifies an already discovered UT
candidate, describes the exit ladder, and evaluates whether the expected edge
survives trading costs.  It never places orders or mutates runtime state.
"""

from dataclasses import dataclass, replace
from math import isfinite


EV_ADAPTIVE_PROFILE_VERSION = "ev_adaptive_v3_profit_engine"


@dataclass(frozen=True)
class EvExitProfile:
    name: str
    tp1_r: float
    tp1_ratio: float
    tp2_r: float
    tp2_ratio: float
    runner_ratio: float
    trailing_activation_r: float
    trailing_atr_multiplier: float
    single_target_r: float
    time_stop_bars: int = 8
    time_stop_min_mfe_r: float = 0.45


@dataclass(frozen=True)
class EvAdaptiveDecision:
    allowed: bool
    mode: str
    score: float
    win_probability: float
    gross_win_r: float
    expected_r_before_cost: float
    risk_multiplier: float
    exit_profile: EvExitProfile
    reasons: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    signal_age_candles: float | None = None
    mtf_alignment: str = "n/a"
    momentum_alignment: str = "n/a"
    leadership_score: float = 50.0
    reacceleration: bool = False
    extension_atr: float | None = None


@dataclass(frozen=True)
class NetEdgeDecision:
    allowed: bool
    expected_net_r: float
    expected_net_usdt: float
    roundtrip_cost_usdt: float
    cost_r: float
    reason: str


@dataclass(frozen=True)
class ExitFeasibility:
    executable: bool
    mode: str
    profile: EvExitProfile
    reason: str


@dataclass(frozen=True)
class TimeStopDecision:
    should_exit: bool
    reason: str


@dataclass(frozen=True)
class MfeProfitLockDecision:
    active: bool
    stage: int
    lock_r: float
    reason: str


def default_ev_adaptive_config():
    return {
        "enabled": True,
        "min_entry_score": 62.0,
        "min_net_expectancy_r": 0.14,
        "max_spread_pct": 0.08,
        "high_vol_atr_pct": 1.5,
        "extreme_atr_pct": 2.5,
        "panic_rebound_block_pct": 6.0,
        "trend_min_adx": 16.0,
        "strong_trend_min_adx": 25.0,
        "trend_max_chop": 60.0,
        "strong_trend_max_chop": 48.0,
        "trend_min_volume_ratio": 0.60,
        "strong_trend_min_volume_ratio": 1.10,
        "squeeze_percentile_max": 25.0,
        "squeeze_range_expansion_min": 1.05,
        "squeeze_volume_ratio_min": 1.15,
        "entry_fee_rate_pct": 0.04,
        "exit_fee_rate_pct": 0.04,
        "slippage_rate_pct_each_side": 0.02,
        "funding_buffer_pct": 0.01,
        "cost_safety_multiplier": 1.25,
        "continuation_max_signal_age_bars": 8.0,
        "continuation_reacceleration_range_min": 1.10,
        "continuation_reacceleration_volume_min": 1.00,
        "max_extension_atr": 2.40,
        "preferred_extension_atr": 1.60,
        "mtf_min_aligned": 2,
        "leadership_bottom_block_pct": 15.0,
        "conditional_relief_enabled": True,
        "conditional_relief_risk_cap": 0.55,

        "mtf_relief_enabled": True,
        "mtf_relief_min_votes": 1,
        "mtf_relief_min_score": 74.0,
        "mtf_relief_min_adx": 26.0,
        "mtf_relief_min_volume_ratio": 1.15,
        "mtf_relief_min_efficiency": 0.28,

        "stale_relief_enabled": True,
        "stale_relief_max_age_bars": 24.0,
        "stale_relief_min_score": 76.0,
        "stale_relief_min_adx": 26.0,
        "stale_relief_min_volume_ratio": 1.18,
        "stale_relief_requires_reacceleration": True,

        "no_edge_relief_enabled": True,
        "no_edge_relief_min_score": 74.0,
        "no_edge_relief_min_adx": 25.0,
        "no_edge_relief_min_volume_ratio": 1.16,
        "no_edge_relief_min_efficiency": 0.27,
        "no_edge_relief_min_range_expansion": 1.10,
        "short_min_entry_score": 62.0,
        "short_trend_min_adx": 16.0,
        "short_trend_min_volume_ratio": 0.60,
        "short_no_edge_relief_min_score": 72.0,
        "short_no_edge_relief_min_adx": 24.0,
        "short_no_edge_relief_min_volume_ratio": 1.12,
        "short_no_edge_relief_min_efficiency": 0.24,
        "short_no_edge_relief_min_range_expansion": 1.08,
        "short_conditional_relief_risk_cap": 0.25,
        "short_relaxed_signal_risk_cap": 0.25,
        "derivatives_funding_soft": 0.0006,
        "derivatives_funding_hard": 0.0015,
        "derivatives_funding_extreme_percentile": 90.0,
        "derivatives_long_short_long_soft": 1.85,
        "derivatives_long_short_long_hard": 2.35,
        "derivatives_long_short_short_soft": 0.55,
        "derivatives_long_short_short_hard": 0.40,
        "derivatives_oi_confirm_min_pct": 0.35,
        "derivatives_oi_stall_min_pct": 0.75,
        "derivatives_price_confirm_min_pct": 0.15,
        "derivatives_price_stall_abs_pct": 0.10,
        "derivatives_oi_z_extreme": 1.75,
        "derivatives_ofi_min_samples": 3,
        "derivatives_ofi_aligned_min": 2.0,
        "derivatives_ofi_opposite_hard": 6.0,
        "derivatives_taker_long_confirm": 1.03,
        "derivatives_taker_long_against": 0.95,
        "derivatives_taker_short_confirm": 0.97,
        "derivatives_taker_short_against": 1.05,
        "derivatives_basis_soft_pct": 0.15,
        "derivatives_basis_hard_pct": 0.35,
        "derivatives_multi_adverse_block_enabled": True,
        "derivatives_multi_adverse_min_count": 3,
        "derivatives_multi_adverse_min_hard_count": 2,
        "derivatives_multi_adverse_max_risk_multiplier": 0.50,
        "regime_opposition_score_add_btc": 4.0,
        "regime_opposition_score_add_eth": 2.0,
        "regime_strong_opposition_score_add_btc": 7.0,
        "regime_opposition_risk_reduce_btc": 0.85,
        "regime_opposition_risk_reduce_eth": 0.92,
        "regime_strong_opposition_risk_reduce_btc": 0.70,
        "regime_opposition_strong_move_pct": 1.5,
    }


def _finite(value, default=None):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _clamp(value, low, high):
    return max(float(low), min(float(high), float(value)))


def _first_finite(values, *keys):
    for key in keys:
        parsed = _finite(values.get(key))
        if parsed is not None:
            return parsed
    return None


def _side_config(cfg, side, key):
    side_key = f"{str(side or '').lower()}_{key}"
    if side_key in cfg and cfg.get(side_key) is not None:
        return cfg.get(side_key)
    return cfg.get(key)


def _percentile_100(value):
    parsed = _finite(value)
    if parsed is None:
        return None
    if 0.0 <= parsed <= 1.0:
        return parsed * 100.0
    return parsed


def _derivatives_quality_overlay(side, values, cfg):
    side = str(side or "").lower()
    if side not in {"long", "short"}:
        return {"score_delta": 0.0, "risk_multiplier": 1.0, "blockers": (), "reasons": ()}

    score_delta = 0.0
    risk_multiplier = 1.0
    blockers = []
    reasons = []
    adverse_details = []
    hard_adverse_details = []

    def _adverse(label, *, hard=False):
        adverse_details.append(str(label))
        if hard:
            hard_adverse_details.append(str(label))

    funding = _first_finite(values, "funding_rate")
    funding_pct = max(
        (
            item
            for item in (
                _percentile_100(values.get("funding_percentile_7d")),
                _percentile_100(values.get("funding_percentile_30d")),
            )
            if item is not None
        ),
        default=None,
    )
    long_short = _first_finite(values, "long_short_ratio")
    oi_delta = _first_finite(values, "open_interest_change_1h", "open_interest_delta_pct")
    oi_delta_4h = _first_finite(values, "open_interest_change_4h")
    oi_z = _first_finite(values, "open_interest_delta_z")
    oi_acceleration = _first_finite(values, "open_interest_acceleration")
    price_change = _first_finite(values, "price_change_1h", "momentum_6_pct")
    price_change_4h = _first_finite(values, "price_change_4h", "momentum_12_pct")
    taker = _first_finite(values, "taker_buy_sell_ratio")
    ofi = _first_finite(values, "rolling_ofi_score", "rolling_orderbook_imbalance_pct")
    ofi_samples = int(max(0.0, _finite(values.get("rolling_ofi_samples"), 0.0)))
    liquidation = _first_finite(values, "liquidation_imbalance")
    basis_pct = _first_finite(values, "basis_pct", "mark_index_basis_pct")

    adverse_funding = None if funding is None else (funding if side == "long" else -funding)
    funding_soft = float(cfg["derivatives_funding_soft"])
    funding_hard = float(cfg["derivatives_funding_hard"])
    funding_extreme_pct = float(cfg["derivatives_funding_extreme_percentile"])

    if side == "long":
        crowd_soft = long_short is not None and long_short >= float(cfg["derivatives_long_short_long_soft"])
        crowd_hard = long_short is not None and long_short >= float(cfg["derivatives_long_short_long_hard"])
        taker_confirm = taker is not None and taker >= float(cfg["derivatives_taker_long_confirm"])
        taker_against = taker is not None and taker <= float(cfg["derivatives_taker_long_against"])
    else:
        crowd_soft = long_short is not None and long_short <= float(cfg["derivatives_long_short_short_soft"])
        crowd_hard = long_short is not None and long_short <= float(cfg["derivatives_long_short_short_hard"])
        taker_confirm = taker is not None and taker <= float(cfg["derivatives_taker_short_confirm"])
        taker_against = taker is not None and taker >= float(cfg["derivatives_taker_short_against"])

    oi_rising = oi_delta is not None and oi_delta >= float(cfg["derivatives_oi_stall_min_pct"])
    side_price = None if price_change is None else (price_change if side == "long" else -price_change)
    side_price_4h = None if price_change_4h is None else (price_change_4h if side == "long" else -price_change_4h)
    price_confirming = (
        side_price is not None
        and side_price >= float(cfg["derivatives_price_confirm_min_pct"])
    )
    price_stalling = (
        side_price is not None
        and side_price <= float(cfg["derivatives_price_stall_abs_pct"])
    )

    if adverse_funding is not None:
        if adverse_funding >= funding_hard and (crowd_hard or oi_rising):
            long_short_text = f"{long_short:.2f}" if long_short is not None else "n/a"
            oi_text = f"{oi_delta:.2f}" if oi_delta is not None else "n/a"
            blockers.append(
                f"derivatives crowding: funding {funding:.6f}, "
                f"L/S {long_short_text}, OI {oi_text}%"
            )
            _adverse(f"funding {funding:.6f}", hard=True)
        elif adverse_funding >= funding_hard:
            score_delta -= 8.0
            risk_multiplier *= 0.55
            reasons.append(f"hard adverse funding {funding:.6f}")
            _adverse(f"funding {funding:.6f}", hard=True)
        elif adverse_funding >= funding_soft:
            score_delta -= 4.0
            risk_multiplier *= 0.75
            reasons.append(f"adverse funding {funding:.6f}")
            _adverse(f"funding {funding:.6f}")
        elif adverse_funding <= -funding_soft:
            score_delta += 2.0
            reasons.append(f"funding tailwind {funding:.6f}")
    if (
        funding_pct is not None
        and funding_pct >= funding_extreme_pct
        and adverse_funding is not None
        and adverse_funding > 0
    ):
        score_delta -= 3.0
        risk_multiplier *= 0.85
        reasons.append(f"funding percentile crowded {funding_pct:.0f}")
        _adverse(f"funding percentile {funding_pct:.0f}")

    if crowd_hard and oi_rising and price_stalling:
        blockers.append(
            f"crowded OI stall: L/S {long_short:.2f}, OI {oi_delta:.2f}%, price {side_price:.2f}%"
        )
        _adverse(f"L/S {long_short:.2f} with OI stall", hard=True)
    elif crowd_soft:
        score_delta -= 4.0
        risk_multiplier *= 0.75
        reasons.append(f"crowding reduced L/S {long_short:.2f}")
        _adverse(f"L/S crowding {long_short:.2f}", hard=crowd_hard)

    if oi_delta is not None:
        if oi_delta >= float(cfg["derivatives_oi_confirm_min_pct"]) and price_confirming:
            score_delta += 5.0
            risk_multiplier *= 1.05
            reasons.append(f"OI confirms trend {oi_delta:.2f}%")
        elif oi_rising and price_stalling:
            score_delta -= 5.0
            risk_multiplier *= 0.75
            reasons.append(f"OI rising without price follow-through {oi_delta:.2f}%")
            _adverse(f"OI stall {oi_delta:.2f}%")
        elif oi_delta <= -0.40:
            score_delta -= 3.0
            risk_multiplier *= 0.85
            reasons.append(f"OI leaving trend {oi_delta:.2f}%")
            _adverse(f"OI leaving {oi_delta:.2f}%")
    if oi_delta_4h is not None and side_price_4h is not None:
        if oi_delta_4h >= 1.0 and side_price_4h >= 0.35:
            score_delta += 2.0
            reasons.append(f"4h OI confirms {oi_delta_4h:.2f}%")
        elif oi_delta_4h >= 1.0 and side_price_4h <= 0.0:
            score_delta -= 3.0
            risk_multiplier *= 0.85
            reasons.append(f"4h OI divergence {oi_delta_4h:.2f}%")
            _adverse(f"4h OI divergence {oi_delta_4h:.2f}%")
    if oi_z is not None and oi_z >= float(cfg["derivatives_oi_z_extreme"]) and (crowd_soft or price_stalling):
        score_delta -= 4.0
        risk_multiplier *= 0.80
        reasons.append(f"extreme OI z {oi_z:.2f}")
        _adverse(f"OI z {oi_z:.2f}", hard=True)
    if oi_acceleration is not None and oi_acceleration < -0.30 and not price_confirming:
        score_delta -= 2.0
        risk_multiplier *= 0.90
        reasons.append(f"OI acceleration fading {oi_acceleration:.3f}")
        _adverse(f"OI acceleration fading {oi_acceleration:.3f}")

    if ofi is not None and ofi_samples >= int(cfg["derivatives_ofi_min_samples"]):
        signed_ofi = ofi if side == "long" else -ofi
        aligned = float(cfg["derivatives_ofi_aligned_min"])
        opposite_hard = float(cfg["derivatives_ofi_opposite_hard"])
        if signed_ofi >= aligned:
            score_delta += 4.0
            risk_multiplier *= 1.03
            reasons.append(f"orderflow confirms {signed_ofi:.2f}")
        elif signed_ofi <= -opposite_hard:
            score_delta -= 8.0
            risk_multiplier *= 0.55
            reasons.append(f"hard opposite orderflow reduced {signed_ofi:.2f}")
            _adverse(f"opposite OFI {signed_ofi:.2f}", hard=True)
        elif signed_ofi <= -aligned:
            score_delta -= 5.0
            risk_multiplier *= 0.75
            reasons.append(f"orderflow against {signed_ofi:.2f}")
            _adverse(f"opposite OFI {signed_ofi:.2f}")

    if taker_confirm:
        score_delta += 3.0
        reasons.append(f"taker flow confirms {taker:.3f}")
    elif taker_against:
        score_delta -= 4.0
        risk_multiplier *= 0.85
        reasons.append(f"taker flow against {taker:.3f}")
        _adverse(f"taker {taker:.3f}")

    if basis_pct is not None:
        adverse_basis = basis_pct if side == "long" else -basis_pct
        basis_soft = float(cfg.get("derivatives_basis_soft_pct", 0.15))
        basis_hard = float(cfg.get("derivatives_basis_hard_pct", 0.35))
        if adverse_basis >= basis_hard:
            score_delta -= 6.0
            risk_multiplier *= 0.60
            reasons.append(f"hard adverse basis {basis_pct:.3f}%")
            _adverse(f"basis {basis_pct:.3f}%", hard=True)
        elif adverse_basis >= basis_soft:
            score_delta -= 3.0
            risk_multiplier *= 0.80
            reasons.append(f"adverse basis {basis_pct:.3f}%")
            _adverse(f"basis {basis_pct:.3f}%")
        elif adverse_basis <= -basis_soft:
            score_delta += 1.0
            reasons.append(f"basis tailwind {basis_pct:.3f}%")

    if liquidation is not None:
        signed_liq = liquidation if side == "long" else -liquidation
        if signed_liq > 0.25:
            score_delta += 2.0
            reasons.append("liquidation tailwind")
        elif signed_liq < -0.50:
            score_delta -= 4.0
            risk_multiplier *= 0.80
            reasons.append("liquidation pressure")
            _adverse("liquidation pressure")

    adverse_count = len(adverse_details)
    hard_count = len(hard_adverse_details)
    if bool(cfg.get("derivatives_multi_adverse_block_enabled", True)):
        min_count = int(cfg.get("derivatives_multi_adverse_min_count", 3) or 3)
        min_hard_count = int(cfg.get("derivatives_multi_adverse_min_hard_count", 2) or 2)
        max_risk = float(cfg.get("derivatives_multi_adverse_max_risk_multiplier", 0.50) or 0.50)
        has_hard_orderflow = any("opposite OFI" in item for item in hard_adverse_details)
        has_non_flow_adverse = any(
            not (item.startswith("opposite OFI") or item.startswith("taker "))
            for item in adverse_details
        )
        if has_hard_orderflow and adverse_count >= 2 and has_non_flow_adverse:
            blockers.append(
                "hard opposite orderflow with adverse derivatives: "
                + "; ".join(adverse_details[:5])
            )
        elif (
            adverse_count >= min_count
            and (risk_multiplier <= max_risk or hard_count >= min_hard_count)
        ):
            blockers.append(
                "derivatives multi-adverse: " + "; ".join(adverse_details[:5])
            )

    return {
        "score_delta": score_delta,
        "risk_multiplier": _clamp(risk_multiplier, 0.0, 1.15),
        "blockers": tuple(blockers),
        "reasons": tuple(reasons),
    }


def _regime_opposition_overlay(side, values, cfg):
    side = str(side or "").lower()
    if side not in {"long", "short"}:
        return {"score_threshold_add": 0.0, "risk_multiplier": 1.0, "reasons": ()}

    regime = values.get("market_regime_context")
    if not isinstance(regime, dict):
        return {"score_threshold_add": 0.0, "risk_multiplier": 1.0, "reasons": ()}

    items = regime.get("items") if isinstance(regime.get("items"), dict) else {}
    if not items:
        return {"score_threshold_add": 0.0, "risk_multiplier": 1.0, "reasons": ()}

    score_threshold_add = 0.0
    risk_multiplier = 1.0
    reasons = []
    strong_move = float(cfg.get("regime_opposition_strong_move_pct", 1.5) or 1.5)
    opposite_direction = "short" if side == "long" else "long"

    for raw_symbol, item in items.items():
        if not isinstance(item, dict):
            continue
        direction = str(item.get("direction") or "").lower()
        if direction != opposite_direction:
            continue
        symbol_label = str(raw_symbol or "").upper()
        ret_value = _finite(item.get("return_lookback_pct"), 0.0)
        strong_opposite = (
            (side == "long" and ret_value <= -strong_move)
            or (side == "short" and ret_value >= strong_move)
        )
        is_btc = symbol_label.startswith("BTC")
        is_eth = symbol_label.startswith("ETH")
        if is_btc and strong_opposite:
            add = float(cfg.get("regime_strong_opposition_score_add_btc", 7.0) or 7.0)
            reduce = float(cfg.get("regime_strong_opposition_risk_reduce_btc", 0.70) or 0.70)
            reasons.append(f"BTC strong opposite regime {direction.upper()} {ret_value:.2f}%")
        elif is_btc:
            add = float(cfg.get("regime_opposition_score_add_btc", 4.0) or 4.0)
            reduce = float(cfg.get("regime_opposition_risk_reduce_btc", 0.85) or 0.85)
            reasons.append(f"BTC opposite regime {direction.upper()}")
        elif is_eth:
            add = float(cfg.get("regime_opposition_score_add_eth", 2.0) or 2.0)
            reduce = float(cfg.get("regime_opposition_risk_reduce_eth", 0.92) or 0.92)
            reasons.append(f"ETH opposite regime {direction.upper()}")
        else:
            add = 1.0
            reduce = 0.95
            reasons.append(f"{symbol_label} opposite regime {direction.upper()}")
        score_threshold_add += max(0.0, add)
        risk_multiplier *= _clamp(reduce, 0.0, 1.0)

    return {
        "score_threshold_add": score_threshold_add,
        "risk_multiplier": _clamp(risk_multiplier, 0.0, 1.0),
        "reasons": tuple(reasons),
    }


def _profile(mode):
    if mode == "STRONG_TREND":
        return EvExitProfile(
            name=mode,
            tp1_r=1.0,
            tp1_ratio=0.15,
            tp2_r=4.20,
            tp2_ratio=0.30,
            runner_ratio=0.55,
            trailing_activation_r=1.50,
            trailing_atr_multiplier=3.60,
            single_target_r=2.80,
            time_stop_bars=12,
        )
    if mode == "SQUEEZE_BREAKOUT":
        return EvExitProfile(
            name=mode,
            tp1_r=1.20,
            tp1_ratio=0.20,
            tp2_r=3.20,
            tp2_ratio=0.35,
            runner_ratio=0.45,
            trailing_activation_r=1.40,
            trailing_atr_multiplier=3.30,
            single_target_r=2.30,
            time_stop_bars=9,
        )
    return EvExitProfile(
        name="TREND",
        tp1_r=1.0,
        tp1_ratio=0.25,
        tp2_r=2.40,
        tp2_ratio=0.35,
        runner_ratio=0.40,
        trailing_activation_r=1.10,
        trailing_atr_multiplier=3.00,
        single_target_r=1.90,
        time_stop_bars=9,
    )


def _side_aligned(side, values):
    close = _finite(values.get("entry_price"), 0.0)
    ema = _finite(values.get("ema50"))
    ema_prev = _finite(values.get("ema50_prev"))
    plus_di = _finite(values.get("plus_di"))
    minus_di = _finite(values.get("minus_di"))
    htf_close = _finite(values.get("htf_close"))
    htf_fast = _finite(values.get("htf_ema_fast"))
    htf_slow = _finite(values.get("htf_ema_slow"))
    supertrend = str(values.get("htf_supertrend_direction") or "").lower()

    votes = 0
    if ema is not None and ema_prev is not None:
        votes += int(close >= ema >= ema_prev) if side == "long" else int(close <= ema <= ema_prev)
    if plus_di is not None and minus_di is not None:
        votes += int(plus_di > minus_di) if side == "long" else int(minus_di > plus_di)
    if htf_close is not None and htf_fast is not None and htf_slow is not None:
        votes += int(htf_close > htf_slow and htf_fast > htf_slow) if side == "long" else int(htf_close < htf_slow and htf_fast < htf_slow)
    elif supertrend in {"long", "short"}:
        votes += int(supertrend == side)
    return votes


def _directional_value(side, value):
    parsed = _finite(value)
    if parsed is None:
        return None
    return parsed if side == "long" else -parsed


def _momentum_alignment(side, values):
    available = 0
    aligned = 0
    for key in ("momentum_6_pct", "momentum_12_pct", "momentum_24_pct"):
        directional = _directional_value(side, values.get(key))
        if directional is None:
            continue
        available += 1
        aligned += int(directional > 0)
    return aligned, available


def _metric_bias(item):
    if not isinstance(item, dict):
        return None
    bias = str(item.get("ema_bias") or "").lower()
    if bias in {"long", "short"}:
        return bias
    close = _finite(item.get("close"))
    fast = _finite(item.get("ema_fast"))
    slow = _finite(item.get("ema_slow"))
    if close is None or fast is None or slow is None:
        return None
    if close > slow and fast > slow:
        return "long"
    if close < slow and fast < slow:
        return "short"
    return None


def _mtf_alignment(side, values):
    metrics = values.get("mtf_metrics")
    if not isinstance(metrics, dict):
        return 0, 0
    aligned = 0
    available = 0
    preferred = ("15m", "30m", "1h")
    items = [metrics.get(key) for key in preferred if key in metrics]
    if not items:
        items = list(metrics.values())
    for item in items:
        bias = _metric_bias(item)
        if bias not in {"long", "short"}:
            continue
        available += 1
        aligned += int(bias == side)
    return aligned, available


def _extension_atr(values):
    explicit = _finite(values.get("extension_atr"))
    if explicit is not None:
        return max(0.0, explicit)
    close = _finite(values.get("entry_price"))
    atr_pct = _finite(values.get("atr_pct"))
    if close is None or close <= 0 or atr_pct is None or atr_pct <= 0:
        return None
    extensions = []
    for key in ("ema50", "vwap", "bb_mid"):
        reference = _finite(values.get(key))
        if reference is None or reference <= 0:
            continue
        distance_pct = abs(close - reference) / close * 100.0
        extensions.append(distance_pct / atr_pct)
    return min(extensions) if extensions else None


def _leadership_score(side, values):
    components = []
    rank_pct = _finite(values.get("cross_sectional_rank_pct"))
    if rank_pct is not None:
        if rank_pct <= 1.0:
            rank_pct *= 100.0
        components.append(_clamp(rank_pct, 0.0, 100.0))

    lookback_return = _directional_value(
        side,
        values.get("selector_return_lookback_pct", values.get("return_lookback_pct")),
    )
    if lookback_return is not None:
        components.append(_clamp(50.0 + lookback_return * 5.0, 0.0, 100.0))

    sharpe = _directional_value(
        side,
        values.get("selector_rolling_sharpe", values.get("rolling_sharpe")),
    )
    if sharpe is not None:
        components.append(_clamp(50.0 + sharpe * 22.0, 0.0, 100.0))

    consistency = _finite(
        values.get("selector_momentum_consistency", values.get("momentum_consistency"))
    )
    if consistency is not None:
        components.append(_clamp((consistency - 0.40) / 0.30 * 100.0, 0.0, 100.0))

    selector_efficiency = _finite(values.get("selector_directional_efficiency"))
    if selector_efficiency is not None:
        components.append(_clamp((selector_efficiency - 0.08) / 0.42 * 100.0, 0.0, 100.0))

    if not components:
        return 50.0, False
    return sum(components) / len(components), True


def _calibrated_win_probability(score, mode, leadership, reacceleration):
    if score >= 88.0:
        probability = 0.55
    elif score >= 82.0:
        probability = 0.52
    elif score >= 76.0:
        probability = 0.49
    elif score >= 70.0:
        probability = 0.46
    elif score >= 64.0:
        probability = 0.43
    elif score >= 58.0:
        probability = 0.40
    else:
        probability = 0.37
    if mode == "STRONG_TREND":
        probability += 0.01
    elif mode == "SQUEEZE_BREAKOUT":
        probability += 0.005
    if leadership >= 72.0:
        probability += 0.01
    elif leadership < 35.0:
        probability -= 0.015
    if reacceleration:
        probability += 0.005
    return _clamp(probability, 0.34, 0.58)


def profile_gross_win_r(profile):
    runner_expectation = max(profile.tp2_r, profile.trailing_activation_r + 0.5)
    return (
        profile.tp1_r * profile.tp1_ratio
        + profile.tp2_r * profile.tp2_ratio
        + runner_expectation * profile.runner_ratio
    )


def _remove_blocker(blockers, blocker):
    if not blocker:
        return False
    try:
        blockers.remove(blocker)
        return True
    except ValueError:
        return False


def evaluate_ev_adaptive_entry(*, side, candidate_type, values=None, config=None):
    cfg = {**default_ev_adaptive_config(), **(config or {})}
    values = dict(values or {})
    side = str(side or "").lower()
    blockers = []
    reasons = []
    if side not in {"long", "short"}:
        blockers.append("invalid side")

    spread = _finite(values.get("futures_spread_pct"))
    atr_pct = _finite(values.get("atr_pct"))
    if spread is not None and spread > float(cfg["max_spread_pct"]):
        blockers.append(f"spread {spread:.4f}%>{float(cfg['max_spread_pct']):.4f}%")
    if atr_pct is not None and atr_pct >= float(cfg["extreme_atr_pct"]):
        blockers.append(f"extreme volatility {atr_pct:.3f}%")
    rebound = _finite(values.get("recent_rebound_pct"), 0.0)
    if side == "short" and rebound >= float(cfg["panic_rebound_block_pct"]):
        blockers.append(f"panic rebound {rebound:.2f}%")

    votes = _side_aligned(side, values) if not blockers else 0
    adx = _finite(values.get("adx"), 0.0)
    chop = _finite(values.get("chop"), 50.0)
    volume = _finite(values.get("volume_ratio"), 1.0)
    efficiency = _finite(values.get("directional_efficiency"), 0.0)
    momentum = _finite(values.get("momentum_12_pct"), 0.0)
    momentum_aligned = momentum >= 0 if side == "long" else momentum <= 0
    momentum_votes, momentum_available = _momentum_alignment(side, values)
    mtf_votes, mtf_available = _mtf_alignment(side, values)
    leadership, leadership_available = _leadership_score(side, values)

    percentile = _finite(values.get("bb_width_percentile"))
    range_expansion = _finite(values.get("range_expansion_ratio"), 1.0)
    close = _finite(values.get("entry_price"), 0.0)
    breakout_level = _finite(
        values.get("donchian_high_prev") if side == "long" else values.get("donchian_low_prev")
    )
    price_breakout = (
        breakout_level is not None
        and ((close > breakout_level) if side == "long" else (close < breakout_level))
    )
    reacceleration = (
        price_breakout
        and range_expansion >= float(cfg["continuation_reacceleration_range_min"])
        and volume >= float(cfg["continuation_reacceleration_volume_min"])
        and (momentum_available < 2 or momentum_votes >= 2)
    )
    signal_age = _finite(values.get("signal_age_candles"))
    continuation = str(candidate_type) != "fresh_signal"
    stale_blocker = None
    if continuation:
        if signal_age is None:
            blockers.append("continuation signal age missing")
        elif (
            signal_age > float(cfg["continuation_max_signal_age_bars"])
            and not reacceleration
        ):
            stale_blocker = (
                f"stale continuation {signal_age:.1f}>"
                f"{float(cfg['continuation_max_signal_age_bars']):.1f} bars without reacceleration"
            )
            blockers.append(stale_blocker)
        elif signal_age > float(cfg["continuation_max_signal_age_bars"]):
            reasons.append(f"stale signal renewed by reacceleration at {signal_age:.1f} bars")

    mtf_blocker = None
    if mtf_available >= 2 and mtf_votes < int(cfg["mtf_min_aligned"]):
        mtf_blocker = f"MTF alignment {mtf_votes}/{mtf_available}"
        blockers.append(mtf_blocker)
    if momentum_available >= 2 and momentum_votes < 2:
        blockers.append(f"multi-horizon momentum {momentum_votes}/{momentum_available}")

    extension = _extension_atr(values)
    if extension is not None and extension > float(cfg["max_extension_atr"]) and not reacceleration:
        blockers.append(
            f"extension {extension:.2f}ATR>{float(cfg['max_extension_atr']):.2f}ATR"
        )
    if (
        leadership_available
        and _finite(values.get("cross_sectional_rank_pct")) is not None
        and leadership < float(cfg["leadership_bottom_block_pct"])
    ):
        blockers.append(f"cross-sectional leadership {leadership:.1f}")
    squeeze = (
        percentile is not None
        and percentile <= float(cfg["squeeze_percentile_max"])
        and range_expansion >= float(cfg["squeeze_range_expansion_min"])
        and volume >= float(cfg["squeeze_volume_ratio_min"])
        and price_breakout
        and votes >= 2
    )

    trend_min_adx = float(_side_config(cfg, side, "trend_min_adx"))
    trend_min_volume_ratio = float(_side_config(cfg, side, "trend_min_volume_ratio"))
    strong_trend_min_adx = float(_side_config(cfg, side, "strong_trend_min_adx"))
    strong_trend_min_volume_ratio = float(_side_config(cfg, side, "strong_trend_min_volume_ratio"))

    trend = (
        votes >= 2
        and adx >= trend_min_adx
        and chop <= float(cfg["trend_max_chop"])
        and volume >= trend_min_volume_ratio
        and momentum_aligned
    )
    strong = (
        trend
        and votes >= 3
        and adx >= strong_trend_min_adx
        and chop <= float(cfg["strong_trend_max_chop"])
        and volume >= strong_trend_min_volume_ratio
        and efficiency >= 0.25
    )

    no_edge_blocker = None
    if squeeze:
        mode = "SQUEEZE_BREAKOUT"
    elif strong:
        mode = "STRONG_TREND"
    elif trend:
        mode = "TREND"
    else:
        mode = "NO_TRADE"
        no_edge_blocker = "no trend or squeeze edge"
        blockers.append(no_edge_blocker)

    profile = _profile(mode)
    score = 30.0
    score += votes * 8.0
    score += _clamp((adx - 12.0) / 20.0, 0.0, 1.0) * 12.0
    score += _clamp(efficiency / 0.45, 0.0, 1.0) * 10.0
    score += _clamp((volume - 0.60) / 1.20, 0.0, 1.0) * 8.0
    score += 6.0 if str(candidate_type) == "fresh_signal" else 2.0
    score += 8.0 if squeeze else 0.0
    if momentum_available:
        score += 8.0 * momentum_votes / momentum_available
    if mtf_available:
        score += 8.0 * mtf_votes / mtf_available
    if leadership_available:
        score += _clamp((leadership - 50.0) / 50.0, -1.0, 1.0) * 8.0
    if reacceleration and continuation:
        score += 5.0
    if signal_age is not None and continuation:
        score -= _clamp(
            signal_age / max(float(cfg["continuation_max_signal_age_bars"]), 1.0),
            0.0,
            2.0,
        ) * 4.0
    if extension is not None and extension > float(cfg["preferred_extension_atr"]):
        score -= min(
            10.0,
            (extension - float(cfg["preferred_extension_atr"])) * 5.0,
        )
    adverse_wick = _finite(
        values.get("upper_wick_ratio") if side == "long" else values.get("lower_wick_ratio")
    )
    body_ratio = _finite(values.get("body_ratio"))
    if adverse_wick is not None and adverse_wick > 0.45:
        score -= min(10.0, (adverse_wick - 0.45) * 25.0)
        if adverse_wick >= 0.65 and body_ratio is not None and body_ratio < 0.30:
            blockers.append(f"exhaustion wick {adverse_wick:.2f}")
    if chop > 55.0:
        score -= min(12.0, (chop - 55.0) * 1.5)

    derivatives_overlay = _derivatives_quality_overlay(side, values, cfg)
    score += derivatives_overlay["score_delta"]
    blockers.extend(derivatives_overlay["blockers"])
    reasons.extend(derivatives_overlay["reasons"])
    regime_overlay = _regime_opposition_overlay(side, values, cfg)
    reasons.extend(regime_overlay["reasons"])

    score = _clamp(score, 0.0, 100.0)

    # Apply conditional OR-style relief for over-filtered signals
    relief_risk_cap = 1.0
    conditional_relief = bool(cfg.get("conditional_relief_enabled", True))

    # 1. MTF relief
    mtf_relief_ok = (
        conditional_relief
        and bool(cfg.get("mtf_relief_enabled", True))
        and mtf_blocker is not None
        and mtf_votes >= int(cfg.get("mtf_relief_min_votes", 1))
        and score >= float(cfg.get("mtf_relief_min_score", 70.0))
        and adx >= float(cfg.get("mtf_relief_min_adx", 26.0))
        and volume >= float(cfg.get("mtf_relief_min_volume_ratio", 1.15))
        and efficiency >= float(cfg.get("mtf_relief_min_efficiency", 0.28))
        and momentum_available >= 2
        and momentum_votes >= 2
        and (price_breakout or reacceleration)
    )
    if mtf_relief_ok and _remove_blocker(blockers, mtf_blocker):
        reasons.append(
            f"OR relief: MTF {mtf_votes}/{mtf_available} accepted by breakout/reacceleration quality"
        )
        relief_risk_cap = min(
            relief_risk_cap,
            float(_side_config(cfg, side, "conditional_relief_risk_cap")),
        )

    # 2. Stale continuation relief
    stale_relief_ok = (
        conditional_relief
        and bool(cfg.get("stale_relief_enabled", True))
        and stale_blocker is not None
        and signal_age is not None
        and signal_age <= float(cfg.get("stale_relief_max_age_bars", 24.0))
        and score >= float(cfg.get("stale_relief_min_score", 70.0))
        and adx >= float(cfg.get("stale_relief_min_adx", 24.0))
        and volume >= float(cfg.get("stale_relief_min_volume_ratio", 1.10))
        and momentum_available >= 2
        and momentum_votes >= 2
        and mtf_votes >= 1
        and (price_breakout or range_expansion >= 1.15)
        and (
            not bool(cfg.get("stale_relief_requires_reacceleration", True))
            or reacceleration
        )
    )
    if stale_relief_ok and _remove_blocker(blockers, stale_blocker):
        reasons.append(
            f"OR relief: stale continuation {signal_age:.1f} bars accepted by substitute momentum/breakout evidence"
        )
        relief_risk_cap = min(
            relief_risk_cap,
            float(_side_config(cfg, side, "conditional_relief_risk_cap")),
        )

    # 3. No-edge relief
    no_edge_relief_ok = (
        conditional_relief
        and bool(cfg.get("no_edge_relief_enabled", True))
        and no_edge_blocker is not None
        and score >= float(_side_config(cfg, side, "no_edge_relief_min_score"))
        and adx >= float(_side_config(cfg, side, "no_edge_relief_min_adx"))
        and volume >= float(_side_config(cfg, side, "no_edge_relief_min_volume_ratio"))
        and efficiency >= float(_side_config(cfg, side, "no_edge_relief_min_efficiency"))
        and range_expansion >= float(_side_config(cfg, side, "no_edge_relief_min_range_expansion"))
        and price_breakout
        and (not continuation or reacceleration)
        and momentum_available >= 2
        and momentum_votes >= 2
        and mtf_votes >= 1
    )
    if no_edge_relief_ok and _remove_blocker(blockers, no_edge_blocker):
        mode = "TREND"
        profile = _profile(mode)
        reasons.append(
            "OR relief: no trend/squeeze edge accepted as TREND by breakout + momentum + MTF substitute"
        )
        relief_risk_cap = min(
            relief_risk_cap,
            float(_side_config(cfg, side, "conditional_relief_risk_cap")),
        )

    win_probability = _calibrated_win_probability(
        score,
        mode,
        leadership,
        reacceleration,
    )
    gross_win_r = profile_gross_win_r(profile)
    expected_before_cost = win_probability * gross_win_r - (1.0 - win_probability)

    min_entry_score = float(_side_config(cfg, side, "min_entry_score"))
    regime_score_add = float(regime_overlay.get("score_threshold_add", 0.0) or 0.0)
    adjusted_min_entry_score = min_entry_score + regime_score_add
    if score < adjusted_min_entry_score:
        if regime_score_add > 0:
            blockers.append(
                f"score {score:.1f}<{adjusted_min_entry_score:.1f} "
                f"(regime adjusted +{regime_score_add:.1f})"
            )
        else:
            blockers.append(f"score {score:.1f}<{min_entry_score:.1f}")
    if score >= 82.0:
        risk_multiplier = 1.0
    elif score >= 74.0:
        risk_multiplier = 0.85
    elif score >= 66.0:
        risk_multiplier = 0.70
    else:
        risk_multiplier = 0.55
    if continuation and signal_age is not None:
        age_ratio = signal_age / max(float(cfg["continuation_max_signal_age_bars"]), 1.0)
        if age_ratio > 1.0:
            risk_multiplier *= 0.70
        elif age_ratio > 0.60:
            risk_multiplier *= 0.85
    if leadership_available:
        if leadership < 40.0:
            risk_multiplier *= 0.75
        elif leadership < 55.0:
            risk_multiplier *= 0.90
    if atr_pct is not None and atr_pct >= float(cfg["high_vol_atr_pct"]):
        risk_multiplier *= 0.60
        reasons.append("high volatility risk reduction")
    if side == "short":
        risk_multiplier *= 0.60
        reasons.append("short risk asymmetry")
        if (
            score < adjusted_min_entry_score
            or adx < trend_min_adx
            or volume < trend_min_volume_ratio
        ):
            risk_multiplier = min(
                risk_multiplier,
                float(cfg.get("short_relaxed_signal_risk_cap", 0.45)),
            )
            reasons.append("short relaxation risk cap")
    risk_multiplier *= float(derivatives_overlay["risk_multiplier"])
    risk_multiplier *= float(regime_overlay.get("risk_multiplier", 1.0) or 1.0)
    risk_multiplier = min(risk_multiplier, relief_risk_cap)

    allowed = not blockers
    return EvAdaptiveDecision(
        allowed=allowed,
        mode=mode,
        score=round(score, 4),
        win_probability=round(win_probability, 6),
        gross_win_r=round(gross_win_r, 6),
        expected_r_before_cost=round(expected_before_cost, 6),
        risk_multiplier=round(_clamp(risk_multiplier if allowed else 0.0, 0.0, 1.0), 6),
        exit_profile=profile,
        reasons=tuple(reasons),
        blockers=tuple(blockers),
        signal_age_candles=round(signal_age, 4) if signal_age is not None else None,
        mtf_alignment=f"{mtf_votes}/{mtf_available}" if mtf_available else "n/a",
        momentum_alignment=(
            f"{momentum_votes}/{momentum_available}" if momentum_available else "n/a"
        ),
        leadership_score=round(leadership, 4),
        reacceleration=bool(reacceleration),
        extension_atr=round(extension, 4) if extension is not None else None,
    )


def evaluate_mfe_profit_lock(*, mfe_r, mode="TREND", config=None):
    cfg = dict(config or {})
    if not bool(cfg.get("ev_mfe_profit_lock_enabled", True)):
        return MfeProfitLockDecision(False, 0, 0.0, "MFE profit lock disabled")
    progress = max(0.0, _finite(mfe_r, 0.0))
    mode = str(mode or "TREND").upper()
    defaults = {
        "STRONG_TREND": ((1.50, 0.30), (2.20, 0.85), (3.20, 1.70)),
        "SQUEEZE_BREAKOUT": ((1.50, 0.45), (2.20, 1.10), (3.20, 1.90)),
        "TREND": ((1.50, 0.40), (2.20, 1.00), (3.20, 1.80)),
    }
    levels = defaults.get(mode, defaults["TREND"])
    active_stage = 0
    active_lock = 0.0
    active_trigger = 0.0
    for stage, (default_trigger, default_lock) in enumerate(levels, start=1):
        trigger = max(
            0.0,
            _finite(cfg.get(f"ev_mfe_lock_trigger_{stage}_r"), default_trigger),
        )
        lock = max(
            0.0,
            _finite(cfg.get(f"ev_mfe_lock_stage_{stage}_r"), default_lock),
        )
        if progress >= trigger:
            active_stage = stage
            active_lock = min(lock, progress)
            active_trigger = trigger
    if active_stage <= 0:
        return MfeProfitLockDecision(
            False,
            0,
            0.0,
            f"MFE {progress:.2f}R below first lock",
        )
    return MfeProfitLockDecision(
        True,
        active_stage,
        round(active_lock, 6),
        f"MFE {progress:.2f}R locks {active_lock:.2f}R at stage {active_stage} "
        f"(trigger {active_trigger:.2f}R)",
    )


def evaluate_net_edge(
    *,
    risk_usdt,
    planned_notional,
    win_probability,
    gross_win_r,
    config=None,
):
    """Return the expected campaign edge after commission, slippage and funding."""
    cfg = {**default_ev_adaptive_config(), **(config or {})}
    risk = max(0.0, _finite(risk_usdt, 0.0))
    notional = max(0.0, _finite(planned_notional, 0.0))
    probability = _clamp(_finite(win_probability, 0.0), 0.0, 1.0)
    win_r = max(0.0, _finite(gross_win_r, 0.0))
    total_cost_pct = (
        max(0.0, _finite(cfg.get("entry_fee_rate_pct"), 0.0))
        + max(0.0, _finite(cfg.get("exit_fee_rate_pct"), 0.0))
        + 2.0 * max(0.0, _finite(cfg.get("slippage_rate_pct_each_side"), 0.0))
        + max(0.0, _finite(cfg.get("funding_buffer_pct"), 0.0))
    )
    cost = (
        notional
        * total_cost_pct
        / 100.0
        * max(1.0, _finite(cfg.get("cost_safety_multiplier"), 1.0))
    )
    cost_r = cost / risk if risk > 0 else float("inf")
    expected_before_cost = probability * win_r - (1.0 - probability)
    expected_net_r = expected_before_cost - cost_r
    min_edge = max(0.0, _finite(cfg.get("min_net_expectancy_r"), 0.08))
    allowed = risk > 0 and expected_net_r >= min_edge
    reason = (
        f"net edge {expected_net_r:.3f}R >= {min_edge:.3f}R"
        if allowed
        else f"net edge {expected_net_r:.3f}R < {min_edge:.3f}R"
    )
    return NetEdgeDecision(
        allowed=allowed,
        expected_net_r=round(expected_net_r, 6),
        expected_net_usdt=round(expected_net_r * risk, 8) if risk > 0 else 0.0,
        roundtrip_cost_usdt=round(cost, 8),
        cost_r=round(cost_r, 6) if isfinite(cost_r) else float("inf"),
        reason=reason,
    )


def adapt_exit_for_quantity(profile, *, total_qty, min_amount):
    """Keep a real runner only when every planned leg is exchange-executable."""
    qty = max(0.0, _finite(total_qty, 0.0))
    minimum = max(0.0, _finite(min_amount, 0.0))
    if qty <= 0:
        return ExitFeasibility(False, "UNEXECUTABLE", profile, "quantity is zero")
    if minimum <= 0:
        return ExitFeasibility(True, "LADDER", profile, "minimum amount unavailable")
    if qty + 1e-12 < minimum:
        return ExitFeasibility(False, "UNEXECUTABLE", profile, "quantity below exchange minimum")

    legs = [
        qty * profile.tp1_ratio,
        qty * profile.tp2_ratio,
        qty * profile.runner_ratio,
    ]
    required = [leg for leg in legs if leg > 0]
    if required and all(leg + 1e-12 >= minimum for leg in required):
        return ExitFeasibility(True, "LADDER", profile, "all exit legs executable")

    single = replace(
        profile,
        name=f"{profile.name}_SINGLE_TARGET",
        tp1_r=profile.single_target_r,
        tp1_ratio=1.0,
        tp2_r=profile.single_target_r,
        tp2_ratio=0.0,
        runner_ratio=0.0,
        trailing_activation_r=profile.single_target_r,
    )
    return ExitFeasibility(
        True,
        "SINGLE_TARGET",
        single,
        "position too small for TP1/TP2/runner ladder",
    )


def evaluate_ev_time_stop(
    *,
    bars_held,
    mfe_r,
    tp1_filled,
    max_bars,
    min_mfe_r,
):
    if bool(tp1_filled):
        return TimeStopDecision(False, "TP1 already filled")
    bars = max(0, int(_finite(bars_held, 0.0)))
    limit = max(1, int(_finite(max_bars, 8.0)))
    progress = max(0.0, _finite(mfe_r, 0.0))
    threshold = max(0.0, _finite(min_mfe_r, 0.45))
    if bars >= limit and progress < threshold:
        return TimeStopDecision(
            True,
            f"no follow-through: {progress:.2f}R<{threshold:.2f}R after {bars} bars",
        )
    return TimeStopDecision(False, "trade still progressing")


def rank_ev_candidates(candidates):
    allowed = [
        dict(item)
        for item in (candidates or [])
        if isinstance(item, dict) and bool(item.get("ev_allowed", True))
    ]
    allowed.sort(
        key=lambda item: (
            _finite(item.get("ev_net_edge_r"), float("-inf")),
            _finite(item.get("score"), 0.0),
            _finite(item.get("quote_volume"), 0.0),
        ),
        reverse=True,
    )
    return allowed
