"""Shared public-market context for UT Breakout research and live gates."""

from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite, sqrt
from statistics import pstdev

from .liquidity import bad_liquidity, nearest_levels


@dataclass(frozen=True)
class MarketContextQuality:
    derivatives_data_available: bool
    bad_liquidity: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class MarketContext:
    symbol: str = "UNKNOWN"
    timeframe: str = "15m"
    close: float = 0.0
    high: float = 0.0
    low: float = 0.0
    open: float = 0.0
    volume: float = 0.0
    utbot_direction: str = ""
    donchian_high: float = 0.0
    donchian_low: float = 0.0
    donchian_mid: float = 0.0
    donchian_high_previous: float = 0.0
    donchian_low_previous: float = 0.0
    adx: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0
    htf_trend: str = "FLAT"
    supertrend_direction: str = "FLAT"
    atr: float = 0.0
    atr_percentile: float | None = None
    realized_volatility: float = 0.0
    volume_ratio: float = 1.0
    range_expansion: float = 1.0
    squeeze_percentile: float | None = None
    funding_rate: float | None = None
    funding_delta: float | None = None
    oi_change_pct: float | None = None
    long_short_ratio: float | None = None
    taker_buy_sell_ratio: float | None = None
    liquidation_spike_score: float | None = None
    spread_bps: float | None = None
    orderbook_imbalance: float | None = None
    support_levels: tuple[float, ...] = ()
    resistance_levels: tuple[float, ...] = ()
    pivot_levels: dict = field(default_factory=dict)
    macro_risk_flag: bool = False
    news_spike_flag: bool = False
    now: datetime | None = None
    macro_events: tuple = ()
    account: dict = field(default_factory=dict)
    portfolio: dict = field(default_factory=dict)
    quality: MarketContextQuality | None = None

    @property
    def htf_trend_aligned(self):
        direction = self.utbot_direction.upper()
        return (direction == "LONG" and self.htf_trend.upper() == "UP") or (
            direction == "SHORT" and self.htf_trend.upper() == "DOWN"
        )


def _finite_float(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _optional_float(value):
    if value is None or value == "":
        return None
    parsed = _finite_float(value, None)
    return parsed


def _bar_value(row, key, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _true_range(rows, idx):
    row = rows[idx]
    high = _finite_float(_bar_value(row, "high"))
    low = _finite_float(_bar_value(row, "low"))
    prev_close = _finite_float(_bar_value(rows[idx - 1], "close"), _finite_float(_bar_value(row, "close"))) if idx > 0 else _finite_float(_bar_value(row, "close"))
    return max(abs(high - low), abs(high - prev_close), abs(low - prev_close))


def _rolling_atr(rows, idx, length=14):
    if not rows:
        return 0.0
    start = max(0, idx - max(1, int(length or 14)) + 1)
    values = [_true_range(rows, item_idx) for item_idx in range(start, idx + 1)]
    return sum(values) / len(values) if values else 0.0


def _percentile_rank(values, value):
    sample = [item for item in values if item is not None]
    if not sample or value is None:
        return None
    below = sum(1 for item in sample if item <= value)
    return below / len(sample) * 100.0


def _volume_ratio(rows, idx, lookback=20):
    volume = _finite_float(_bar_value(rows[idx], "volume"))
    start = max(0, idx - max(1, int(lookback or 20)) + 1)
    sample = [_finite_float(_bar_value(row, "volume")) for row in rows[start:idx + 1]]
    sample = [value for value in sample if value > 0]
    average = sum(sample) / len(sample) if sample else 0.0
    return volume / average if average > 0 else 1.0


def _realized_volatility(rows, idx, lookback=20):
    start = max(1, idx - max(2, int(lookback or 20)) + 1)
    returns = []
    for item_idx in range(start, idx + 1):
        prev_close = _finite_float(_bar_value(rows[item_idx - 1], "close"))
        close = _finite_float(_bar_value(rows[item_idx], "close"))
        if prev_close > 0 and close > 0:
            returns.append((close - prev_close) / prev_close)
    if len(returns) < 2:
        return 0.0
    return pstdev(returns) * sqrt(len(returns))


def build_market_context(rows=None, idx=None, *, symbol="UNKNOWN", timeframe="15m", values=None, config=None):
    rows = list(rows or [])
    values = dict(values or {})
    config = config or {}
    if not rows:
        row = values
        idx = 0
    else:
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            idx = len(rows) - 1
        idx = max(0, min(idx, len(rows) - 1))
        row = rows[idx]

    close = _finite_float(values.get("close", _bar_value(row, "close")))
    high = _finite_float(values.get("high", _bar_value(row, "high", close)))
    low = _finite_float(values.get("low", _bar_value(row, "low", close)))
    open_price = _finite_float(values.get("open", _bar_value(row, "open", close)))
    volume = _finite_float(values.get("volume", _bar_value(row, "volume")))

    lookback = int(config.get("donchian_length", 20) or 20)
    previous = rows[max(0, idx - lookback):idx] if rows else []
    if previous:
        prev_high = max(_finite_float(_bar_value(item, "high")) for item in previous)
        prev_low = min(_finite_float(_bar_value(item, "low")) for item in previous)
    else:
        prev_high = _finite_float(values.get("donchian_high_previous"), high)
        prev_low = _finite_float(values.get("donchian_low_previous"), low)
    donchian_high = _finite_float(values.get("donchian_high", prev_high))
    donchian_low = _finite_float(values.get("donchian_low", prev_low))
    donchian_mid = _finite_float(values.get("donchian_mid"), (donchian_high + donchian_low) / 2.0)

    atr = _finite_float(values.get("atr"), _rolling_atr(rows, idx, config.get("atr_length", 14)) if rows else 0.0)
    atr_values = [_rolling_atr(rows, item_idx, config.get("atr_length", 14)) for item_idx in range(max(0, idx - 99), idx + 1)] if rows else []
    atr_pctile = values.get("atr_percentile")
    if atr_pctile is None:
        atr_pctile = _percentile_rank(atr_values, atr) if atr_values else None
    volume_ratio = _finite_float(values.get("volume_ratio"), _volume_ratio(rows, idx, config.get("volume_lookback", 20)) if rows else 1.0)
    ranges = [
        max(_finite_float(_bar_value(item, "high")) - _finite_float(_bar_value(item, "low")), 0.0)
        for item in (rows[max(0, idx - 19):idx + 1] if rows else [])
    ]
    avg_range = sum(ranges) / len(ranges) if ranges else max(high - low, 1e-9)
    range_expansion = _finite_float(values.get("range_expansion", values.get("range_expansion_ratio")), max(high - low, 0.0) / max(avg_range, 1e-9))
    levels = nearest_levels(rows, idx, lookback) if rows else {"support_levels": [], "resistance_levels": [], "pivot_levels": {}}

    direction = str(values.get("utbot_direction") or values.get("side") or "").upper()
    if direction.lower() == "long":
        direction = "LONG"
    elif direction.lower() == "short":
        direction = "SHORT"
    htf_missing = values.get("htf_trend") is None
    dmi_missing = values.get("plus_di") is None or values.get("minus_di") is None
    adx_missing = values.get("adx") is None

    htf_trend = str(values.get("htf_trend") or "FLAT").upper()
    supertrend = str(values.get("supertrend_direction") or htf_trend or "FLAT").upper()

    plus_di = _finite_float(values.get("plus_di"), 0.0)
    minus_di = _finite_float(values.get("minus_di"), 0.0)
    spread_bps = _optional_float(values.get("spread_bps", _bar_value(row, "spread_bps", None)))

    derivatives_available = all(
        values.get(key, _bar_value(row, key, None)) is not None
        for key in ("funding_rate", "oi_change_pct", "long_short_ratio")
    )
    temp = {"spread_bps": spread_bps}
    reasons = []
    is_bad_liquidity = bad_liquidity(temp, config)
    if is_bad_liquidity:
        reasons.append("BAD_LIQUIDITY")
    if not derivatives_available:
        reasons.append("MISSING_DERIVATIVES_DATA")
    if htf_missing:
        reasons.append("MISSING_HTF_TREND")
    if dmi_missing:
        reasons.append("MISSING_DMI")
    if adx_missing:
        reasons.append("MISSING_ADX")
    quality = MarketContextQuality(derivatives_available, is_bad_liquidity, tuple(reasons))

    return MarketContext(
        symbol=str(values.get("symbol") or symbol or "UNKNOWN"),
        timeframe=str(values.get("timeframe") or timeframe or "15m"),
        close=close,
        high=high,
        low=low,
        open=open_price,
        volume=volume,
        utbot_direction=direction,
        donchian_high=donchian_high,
        donchian_low=donchian_low,
        donchian_mid=donchian_mid,
        donchian_high_previous=_finite_float(values.get("donchian_high_previous"), prev_high),
        donchian_low_previous=_finite_float(values.get("donchian_low_previous"), prev_low),
        adx=_finite_float(values.get("adx"), 0.0),
        plus_di=plus_di,
        minus_di=minus_di,
        htf_trend=htf_trend,
        supertrend_direction=supertrend,
        atr=atr,
        atr_percentile=_optional_float(atr_pctile),
        realized_volatility=_finite_float(values.get("realized_volatility"), _realized_volatility(rows, idx) if rows else 0.0),
        volume_ratio=volume_ratio,
        range_expansion=range_expansion,
        squeeze_percentile=_optional_float(values.get("squeeze_percentile", atr_pctile)),
        funding_rate=_optional_float(values.get("funding_rate", _bar_value(row, "funding_rate", None))),
        funding_delta=_optional_float(values.get("funding_delta", _bar_value(row, "funding_delta", None))),
        oi_change_pct=_optional_float(values.get("oi_change_pct", _bar_value(row, "oi_change_pct", None))),
        long_short_ratio=_optional_float(values.get("long_short_ratio", _bar_value(row, "long_short_ratio", None))),
        taker_buy_sell_ratio=_optional_float(values.get("taker_buy_sell_ratio", _bar_value(row, "taker_buy_sell_ratio", None))),
        liquidation_spike_score=_optional_float(values.get("liquidation_spike_score", _bar_value(row, "liquidation_spike_score", None))),
        spread_bps=spread_bps,
        orderbook_imbalance=_optional_float(values.get("orderbook_imbalance", _bar_value(row, "orderbook_imbalance", None))),
        support_levels=tuple(values.get("support_levels") or levels["support_levels"]),
        resistance_levels=tuple(values.get("resistance_levels") or levels["resistance_levels"]),
        pivot_levels=dict(values.get("pivot_levels") or levels["pivot_levels"]),
        macro_risk_flag=bool(values.get("macro_risk_flag", False)),
        news_spike_flag=bool(values.get("news_spike_flag", False)),
        now=values.get("now"),
        macro_events=tuple(values.get("macro_events") or ()),
        account=dict(values.get("account") or {}),
        portfolio=dict(values.get("portfolio") or {}),
        quality=quality,
    )
