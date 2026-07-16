"""CoinSelector V2 pure scoring helpers.

This module ranks markets to watch. It does not generate entries or exits.
Live trading decisions remain owned by the strategy engine.
"""

from collections import Counter
from math import isfinite, log10


DEFAULT_EXCLUDED_SECTORS = {
    "meme",
    "fan-token",
    "stablecoin",
    "wrapped",
    "rebase",
    "gambling",
    "leveraged-token",
}

HARD_MIN_QUOTE_VOLUME_USDT = 100_000_000.0

DEFAULT_COIN_SELECTOR_CONFIG = {
    "enabled": True,
    "auto_apply_watchlist": False,
    "custom_universe_enabled": False,
    "custom_symbols": [],
    "custom_relax_discovery": True,
    "min_quote_volume_usdt": HARD_MIN_QUOTE_VOLUME_USDT,
    "ideal_quote_volume_usdt": 1_000_000_000.0,
    "min_trade_count": 20_000,
    "ideal_trade_count": 500_000,
    "max_spread_pct": 0.08,
    "max_abs_price_change_pct": 18.0,
    "analysis_limit": 20,
    "analysis_batch_size": 12,
    "top_n": 10,
    "max_strategy_evaluations_per_cycle": 3,
    "min_final_score": 55.0,
    "refresh_interval_seconds": 300,
    "rate_limit_backoff_seconds": 120,
    "candidate_cooldown_enabled": True,
    "candidate_cooldown_misses": 3,
    "candidate_cooldown_seconds": 1800,
    "selection_quality_enabled": True,
    "include_tradifi_universe": True,
    "tradifi_universe_max_candidates": 20,
    "selection_return_lookback_bars": 96,
    "selection_target_realized_vol_pct": 0.65,
    "selection_max_drawdown_pct": 18.0,
    "selection_max_rebound_pct": 18.0,
    "selection_max_dispersion_pct": 9.0,
    "excluded_sectors": sorted(DEFAULT_EXCLUDED_SECTORS),
    "blacklist": [],
    "sector_overrides": {},
}

KNOWN_SECTOR_TAGS = {
    "DOGE": {"meme"},
    "SHIB": {"meme"},
    "PEPE": {"meme"},
    "BONK": {"meme"},
    "FLOKI": {"meme"},
    "WIF": {"meme"},
    "MEME": {"meme"},
    "TURBO": {"meme"},
    "DOGS": {"meme"},
    "PNUT": {"meme"},
    "NEIRO": {"meme"},
    "1000SATS": {"meme"},
    "USDC": {"stablecoin"},
    "USDT": {"stablecoin"},
    "DAI": {"stablecoin"},
    "FDUSD": {"stablecoin"},
    "TUSD": {"stablecoin"},
    "USDE": {"stablecoin"},
    "WBTC": {"wrapped"},
    "WETH": {"wrapped"},
    "ACM": {"fan-token"},
    "ASR": {"fan-token"},
    "ATM": {"fan-token"},
    "BAR": {"fan-token"},
    "CITY": {"fan-token"},
    "JUV": {"fan-token"},
    "LAZIO": {"fan-token"},
    "PORTO": {"fan-token"},
    "PSG": {"fan-token"},
    "SANTOS": {"fan-token"},
}


def default_coin_selector_config():
    return {
        key: list(value) if isinstance(value, list) else dict(value) if isinstance(value, dict) else value
        for key, value in DEFAULT_COIN_SELECTOR_CONFIG.items()
    }


def finite_float(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def finite_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def clamp(value, low=0.0, high=100.0):
    return max(low, min(high, finite_float(value, low)))


def normalize_symbol(symbol):
    text = str(symbol or "").strip().upper()
    if not text:
        return ""
    text = text.replace(":USDT", "")
    if "/" in text:
        base, quote = text.split("/", 1)
        return f"{base}/{quote.split(':', 1)[0]}"
    if text.endswith("USDT"):
        return f"{text[:-4]}/USDT"
    return text


def symbol_base(symbol):
    normalized = normalize_symbol(symbol)
    return normalized.split("/", 1)[0] if "/" in normalized else normalized


def normalize_custom_symbol(symbol, default_quote="USDT"):
    text = str(symbol or "").strip().upper()
    if not text:
        return ""
    quote = str(default_quote or "USDT").strip().upper() or "USDT"
    normalized = normalize_symbol(text)
    if not normalized:
        return ""
    if "/" not in normalized:
        normalized = f"{normalized}/{quote}"
    base, raw_quote = normalized.split("/", 1)
    raw_quote = raw_quote.split(":", 1)[0]
    return f"{base}/{raw_quote}"


def normalize_custom_symbols(symbols, default_quote="USDT"):
    if symbols is None:
        raw_items = []
    elif isinstance(symbols, str):
        text = symbols
        for sep in (",", ";", "\n", "\t"):
            text = text.replace(sep, " ")
        raw_items = text.split()
    elif isinstance(symbols, (list, tuple, set)):
        raw_items = list(symbols)
    else:
        raw_items = [symbols]

    result = []
    seen = set()
    for item in raw_items:
        normalized = normalize_custom_symbol(item, default_quote)
        if normalized and normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return result


def _info_value(container, key, default=None):
    if not isinstance(container, dict):
        return default
    if key in container:
        return container.get(key)
    info = container.get("info")
    if isinstance(info, dict) and key in info:
        return info.get(key)
    return default


def sector_tags_for_symbol(symbol, overrides=None):
    base = symbol_base(symbol)
    tags = set(KNOWN_SECTOR_TAGS.get(base, set()))
    overrides = overrides or {}
    for key in (base, normalize_symbol(symbol), str(symbol or "").upper()):
        raw_tags = overrides.get(key)
        if isinstance(raw_tags, str):
            tags.update({item.strip().lower() for item in raw_tags.split(",") if item.strip()})
        elif isinstance(raw_tags, (list, tuple, set)):
            tags.update({str(item).strip().lower() for item in raw_tags if str(item).strip()})
    return sorted(tags)


def market_is_usdt_perpetual(symbol, market):
    normalized = normalize_symbol(symbol)
    if "/USDT" not in normalized:
        return False
    if not isinstance(market, dict):
        return True

    quote = str(market.get("quote") or _info_value(market, "quoteAsset", "")).upper()
    settle = str(market.get("settle") or _info_value(market, "marginAsset", "USDT")).upper()
    contract_type = str(_info_value(market, "contractType", "PERPETUAL")).upper()
    market_type = str(market.get("type") or "").lower()
    active = market.get("active", True)
    status = str(_info_value(market, "status", "TRADING")).upper()
    swap_like = bool(market.get("swap", False)) or market_type in {"swap", "future"}
    return (
        quote == "USDT"
        and settle == "USDT"
        and contract_type in {"PERPETUAL", "TRADIFI_PERPETUAL"}
        and status == "TRADING"
        and active is not False
        and swap_like
    )


def market_is_tradifi_perpetual(symbol, market):
    if not market_is_usdt_perpetual(symbol, market):
        return False
    if not isinstance(market, dict):
        return False
    contract_type = str(_info_value(market, "contractType", "")).upper()
    return contract_type == "TRADIFI_PERPETUAL"


def extract_ticker_metrics(symbol, ticker):
    ticker = ticker or {}
    quote_volume = finite_float(
        ticker.get("quoteVolume", _info_value(ticker, "quoteVolume", 0.0)),
        0.0,
    )
    percentage = finite_float(
        ticker.get("percentage", _info_value(ticker, "priceChangePercent", 0.0)),
        0.0,
    )
    trade_count = finite_int(
        ticker.get("count", _info_value(ticker, "count", 0)),
        0,
    )
    bid = finite_float(ticker.get("bid", _info_value(ticker, "bidPrice", 0.0)), 0.0)
    ask = finite_float(ticker.get("ask", _info_value(ticker, "askPrice", 0.0)), 0.0)
    spread_pct = None
    if bid > 0 and ask > 0 and ask >= bid:
        mid = (bid + ask) / 2.0
        spread_pct = ((ask - bid) / mid) * 100.0 if mid > 0 else None
    return {
        "symbol": symbol,
        "normalized_symbol": normalize_symbol(symbol),
        "base": symbol_base(symbol),
        "quote_volume": quote_volume,
        "percentage": percentage,
        "trade_count": trade_count,
        "bid": bid,
        "ask": ask,
        "spread_pct": spread_pct,
    }


def _scanner_hard_reject_reason(candidate: dict, cfg: dict) -> str | None:
    # A. Invalid market:
    market = candidate.get("market")
    symbol = candidate.get("symbol") or candidate.get("exchange_symbol")
    if not market_is_usdt_perpetual(symbol, market):
        return "INVALID_MARKET"
    # B. Low 24h quote volume:
    min_vol = max(
        HARD_MIN_QUOTE_VOLUME_USDT,
        finite_float(
            cfg.get("min_quote_volume_usdt"),
            HARD_MIN_QUOTE_VOLUME_USDT,
        ),
    )
    if finite_float(candidate.get("quote_volume"), 0.0) < min_vol:
        return "LOW_QUOTE_VOLUME"
    # C. Low 24h trade count:
    min_count = finite_int(cfg.get("min_trade_count"), 0)
    trade_count = finite_int(candidate.get("trade_count"), 0)
    if min_count > 0 and trade_count > 0 and trade_count < min_count:
        return "LOW_TRADE_COUNT"
    # D. Bad spread:
    max_spread = finite_float(cfg.get("max_spread_pct"), 0.08)
    spread = candidate.get("spread_pct")
    if spread is not None and spread > max_spread:
        return "BAD_SPREAD"
    return None


def build_base_candidate(symbol, ticker, market=None, cfg=None, sector_tags=None):
    cfg = {**default_coin_selector_config(), **(cfg or {})}
    metrics = extract_ticker_metrics(symbol, ticker)
    sector_tags = sorted(set(sector_tags if sector_tags is not None else sector_tags_for_symbol(symbol, cfg.get("sector_overrides"))))

    # Scanner hard filters are intentionally limited to:
    # valid USDT futures market, quote volume, trade count, and spread.
    # Sector/category is informational only at scanner stage.
    temp_cand = {**metrics, "market": market}
    reject_reason = _scanner_hard_reject_reason(temp_cand, cfg)

    reject_reasons = []
    if reject_reason:
        reject_reasons.append(reject_reason)

    metrics.update({
        "exchange_symbol": symbol,
        "tradifi_perpetual": market_is_tradifi_perpetual(symbol, market),
        "sector_tags": sector_tags,
        "accepted": not reject_reasons,
        "reject_reasons": reject_reasons,
        "scanner_accepted": not reject_reasons,
        "scanner_reject_reason": reject_reason,
    })
    return metrics


def _scaled_log_score(value, low, high):
    value = finite_float(value, 0.0)
    low = max(1.0, finite_float(low, 1.0))
    high = max(low * 1.01, finite_float(high, low * 10.0))
    if value <= low:
        return 0.0
    return clamp((log10(value) - log10(low)) / (log10(high) - log10(low)) * 100.0)


def score_liquidity(candidate, cfg=None):
    cfg = {**default_coin_selector_config(), **(cfg or {})}
    volume_score = _scaled_log_score(
        candidate.get("quote_volume"),
        cfg.get("min_quote_volume_usdt"),
        cfg.get("ideal_quote_volume_usdt"),
    )
    count_score = _scaled_log_score(
        max(candidate.get("trade_count") or 0, 1),
        cfg.get("min_trade_count"),
        cfg.get("ideal_trade_count"),
    )
    spread = candidate.get("spread_pct")
    if spread is None:
        spread_score = 70.0
    else:
        spread_score = clamp(100.0 - (finite_float(spread) / max(finite_float(cfg.get("max_spread_pct"), 0.08), 0.001) * 100.0))
    raw = (volume_score * 0.58) + (count_score * 0.22) + (spread_score * 0.20)
    return round(raw * 0.23, 2)


def score_ut_regime(auto_scores):
    auto_scores = auto_scores or {}
    trend = clamp(auto_scores.get("trend_score", 0.0))
    chop_avoid = 100.0 - clamp(auto_scores.get("chop_score", 50.0))
    volatility = clamp(auto_scores.get("volatility_score", 0.0))
    breakout = clamp(auto_scores.get("breakout_score", 0.0))
    momentum = clamp(auto_scores.get("momentum_score", 0.0))
    flow = clamp(auto_scores.get("flow_score", 0.0))
    alignment = clamp(auto_scores.get("alignment_score", 0.0))
    mtf_momentum = clamp(auto_scores.get("mtf_momentum_score", momentum))
    mtf_volatility = clamp(auto_scores.get("mtf_volatility_score", volatility))
    raw = (
        trend * 0.18
        + chop_avoid * 0.12
        + volatility * 0.14
        + breakout * 0.14
        + momentum * 0.14
        + flow * 0.10
        + alignment * 0.10
        + mtf_momentum * 0.04
        + mtf_volatility * 0.04
    )
    return round(raw * 0.27, 2)


def score_set_suitability(auto_scores, selected_set_id=None, selected_set_info=None):
    auto_scores = auto_scores or {}
    selected_set_id = finite_int(selected_set_id or auto_scores.get("auto_final_set_id"), 0)
    selected_score = clamp(auto_scores.get("auto_selected_score", 50.0))
    margin = finite_float(auto_scores.get("auto_score_margin"), 0.0)
    margin_score = clamp(margin / 12.0 * 100.0)
    confidence = str(auto_scores.get("auto_confidence") or "normal").lower()
    family = str((selected_set_info or {}).get("family") or "").lower()

    raw = selected_score * 0.68 + margin_score * 0.22 + 62.0 * 0.10
    if confidence == "weak":
        raw -= 12.0
    if selected_set_id == 50:
        raw -= 35.0
    elif selected_set_id == 49:
        raw -= 18.0
    if "fallback" in family:
        raw -= 10.0
    if selected_set_id <= 0:
        raw = min(raw, 35.0)
    return round(clamp(raw) * 0.17, 2)


def score_adaptive_timeframe(decision):
    decision = decision or {}
    selected_tf = decision.get("selected_tf")
    state = str(decision.get("decision") or "").upper()
    if not selected_tf or state == "NO_TRADE":
        return 0.0
    selected_score = clamp(decision.get("selected_score", 50.0))
    if state == "HYSTERESIS_KEEP":
        selected_score += 4.0
    elif state == "POSITION_LOCKED":
        selected_score += 2.0
    return round(clamp(selected_score) * 0.08, 2)


def score_futures_context(context):
    context = context or {}
    funding = context.get("funding_rate", context.get("lastFundingRate"))
    funding = finite_float(funding, None)
    if funding is None:
        funding_score = 65.0
    else:
        funding_pct = abs(funding) * 100.0
        funding_score = 100.0 if funding_pct <= 0.03 else clamp(100.0 - ((funding_pct - 0.03) / 0.17 * 100.0))

    oi = context.get("open_interest_usdt", context.get("open_interest", context.get("openInterest")))
    oi = finite_float(oi, 0.0)
    oi_score = 70.0 if oi <= 0 else clamp(_scaled_log_score(oi, 50_000_000.0, 1_000_000_000.0))
    return round((funding_score * 0.65 + oi_score * 0.35) * 0.08, 2)


def score_selection_quality(candidate, auto_scores=None, cfg=None):
    """Score implementable momentum quality without adding hard filters.

    This is intentionally a soft component. It rewards liquid, persistent
    trends with tolerable realized volatility and penalizes crash/rebound
    profiles that are especially harmful to crypto momentum shorts.
    """
    cfg = {**default_coin_selector_config(), **(cfg or {})}
    if not bool(cfg.get("selection_quality_enabled", True)):
        return 6.6
    auto_scores = auto_scores or {}
    metrics = candidate.get("selection_metrics")
    if not isinstance(metrics, dict):
        metrics = candidate

    sharpe = finite_float(metrics.get("rolling_sharpe"), None)
    consistency = finite_float(metrics.get("momentum_consistency"), None)
    efficiency = finite_float(metrics.get("directional_efficiency"), None)
    realized_vol = finite_float(metrics.get("realized_vol_pct"), None)
    lookback_return = finite_float(metrics.get("return_lookback_pct"), finite_float(candidate.get("percentage"), 0.0))
    max_drawdown = finite_float(metrics.get("max_drawdown_pct"), None)
    max_rebound = finite_float(metrics.get("max_rebound_pct"), None)
    dispersion = finite_float(metrics.get("cross_sectional_dispersion_pct"), None)
    dominant_side = str(auto_scores.get("dominant_side") or "").lower()

    if sharpe is None and consistency is None and efficiency is None and realized_vol is None:
        return 6.6

    sharpe_score = 55.0 if sharpe is None else clamp((sharpe + 0.20) / 1.40 * 100.0)
    consistency_score = 55.0 if consistency is None else clamp((consistency - 0.45) / 0.25 * 100.0)
    efficiency_score = 55.0 if efficiency is None else clamp((efficiency - 0.12) / 0.45 * 100.0)

    target_vol = max(0.05, finite_float(cfg.get("selection_target_realized_vol_pct"), 0.65))
    if realized_vol is None or realized_vol <= 0:
        vol_score = 55.0
    else:
        vol_score = clamp(100.0 - abs(realized_vol - target_vol) / max(target_vol, 1e-9) * 100.0)
        if realized_vol > target_vol * 2.8:
            vol_score *= 0.55

    max_dd_limit = max(1.0, finite_float(cfg.get("selection_max_drawdown_pct"), 18.0))
    max_rebound_limit = max(1.0, finite_float(cfg.get("selection_max_rebound_pct"), 18.0))
    drawdown_score = 65.0 if max_drawdown is None else clamp(100.0 - max_drawdown / max_dd_limit * 100.0)
    rebound_score = 65.0 if max_rebound is None else clamp(100.0 - max_rebound / max_rebound_limit * 100.0)
    tail_score = rebound_score if dominant_side == "short" else drawdown_score

    abs_return = abs(finite_float(lookback_return, 0.0))
    max_abs_change = max(3.0, finite_float(cfg.get("max_abs_price_change_pct"), 18.0))
    if abs_return < 0.8:
        extension_score = clamp(abs_return / 0.8 * 70.0)
    elif abs_return <= 10.0:
        extension_score = 100.0
    else:
        extension_score = clamp(100.0 - (abs_return - 10.0) / max(max_abs_change - 10.0, 1e-9) * 65.0)

    raw = (
        sharpe_score * 0.25
        + consistency_score * 0.20
        + efficiency_score * 0.15
        + vol_score * 0.15
        + tail_score * 0.15
        + extension_score * 0.10
    )

    if dominant_side == "short":
        if lookback_return is not None and lookback_return > 0:
            raw -= 12.0
        if max_rebound is not None and max_rebound >= max_rebound_limit * 0.75:
            raw -= 10.0
    elif dominant_side == "long" and lookback_return is not None and lookback_return < 0:
        raw -= 8.0

    max_dispersion = max(1.0, finite_float(cfg.get("selection_max_dispersion_pct"), 9.0))
    if dispersion is not None and dispersion > max_dispersion:
        raw *= clamp(1.0 - (dispersion - max_dispersion) / max_dispersion * 0.35, 0.60, 1.0)

    return round(clamp(raw) * 0.12, 2)


def score_sector(candidate):
    tags = set(candidate.get("sector_tags") or [])
    if not tags:
        return 3.5
    if DEFAULT_EXCLUDED_SECTORS.intersection(tags):
        return 0.0
    return 5.0


def finalize_candidate(
    candidate,
    *,
    auto_analysis=None,
    selected_set_id=None,
    selected_set_info=None,
    adaptive_decision=None,
    futures_context=None,
    selection_metrics=None,
    cfg=None,
):
    cfg = {**default_coin_selector_config(), **(cfg or {})}
    cfg.setdefault("scanner_min_score", 0.0)
    auto_scores = (auto_analysis or {}).get("scores", {}) if isinstance(auto_analysis, dict) else {}
    enriched_candidate = dict(candidate)
    if isinstance(selection_metrics, dict):
        enriched_candidate["selection_metrics"] = dict(selection_metrics)
    components = {
        "liquidity_cost": score_liquidity(enriched_candidate, cfg),
        "utbreakout_regime": score_ut_regime(auto_scores),
        "auto_set": score_set_suitability(auto_scores, selected_set_id, selected_set_info),
        "adaptive_tf": score_adaptive_timeframe(adaptive_decision),
        "futures_health": score_futures_context(futures_context),
        "selection_quality": score_selection_quality(enriched_candidate, auto_scores, cfg),
        "sector_risk": score_sector(enriched_candidate),
    }
    total = round(sum(components.values()), 2)
    result = dict(enriched_candidate)
    set_info = selected_set_info or {}
    adaptive_decision = adaptive_decision or {}
    accepted_flag = candidate.get("accepted", False)
    result.update({
        "score": total,
        "component_scores": components,
        "selection_state": "SELECTED" if accepted_flag else "WATCH_ONLY",
        "scanner_accepted": accepted_flag,
        "scanner_reject_reason": candidate.get("scanner_reject_reason"),
        "auto_set_id": finite_int(selected_set_id or auto_scores.get("auto_final_set_id"), None),
        "auto_set_name": set_info.get("name"),
        "auto_set_family": set_info.get("family"),
        "auto_confidence": auto_scores.get("auto_confidence", "n/a"),
        "auto_score_margin": finite_float(auto_scores.get("auto_score_margin"), 0.0),
        "adaptive_tf": adaptive_decision.get("selected_tf") or "NO_TRADE",
        "adaptive_decision": adaptive_decision.get("decision") or "WAIT",
        "adaptive_reason": adaptive_decision.get("reason") or "",
        "funding_rate": (futures_context or {}).get("funding_rate", (futures_context or {}).get("lastFundingRate")),
        "open_interest_usdt": (futures_context or {}).get("open_interest_usdt", (futures_context or {}).get("open_interest")),
    })
    metrics = result.get("selection_metrics") if isinstance(result.get("selection_metrics"), dict) else {}
    result.update({
        "rolling_sharpe": metrics.get("rolling_sharpe"),
        "return_lookback_pct": metrics.get("return_lookback_pct"),
        "realized_vol_pct": metrics.get("realized_vol_pct"),
        "momentum_consistency": metrics.get("momentum_consistency"),
        "directional_efficiency": metrics.get("directional_efficiency"),
        "max_drawdown_pct": metrics.get("max_drawdown_pct"),
        "max_rebound_pct": metrics.get("max_rebound_pct"),
        "cross_sectional_dispersion_pct": metrics.get("cross_sectional_dispersion_pct"),
    })
    if result["adaptive_tf"] == "NO_TRADE":
        result.setdefault("soft_warnings", []).append("ADAPTIVE_NO_TRADE")
    if result["auto_set_id"] in {49, 50}:
        result.setdefault("soft_warnings", []).append("AUTO_FALLBACK_OR_EMERGENCY")
    if metrics:
        if finite_float(metrics.get("cross_sectional_dispersion_pct"), 0.0) > finite_float(cfg.get("selection_max_dispersion_pct"), 9.0):
            result.setdefault("soft_warnings", []).append("SELECTION_HIGH_DISPERSION")
        if finite_float(metrics.get("max_drawdown_pct"), 0.0) > finite_float(cfg.get("selection_max_drawdown_pct"), 18.0):
            result.setdefault("soft_warnings", []).append("SELECTION_DRAWDOWN_RISK")
        if (
            str(auto_scores.get("dominant_side") or "").lower() == "short"
            and finite_float(metrics.get("max_rebound_pct"), 0.0) > finite_float(cfg.get("selection_max_rebound_pct"), 18.0) * 0.75
        ):
            result.setdefault("soft_warnings", []).append("SELECTION_SHORT_REBOUND_RISK")
    return result


def rank_candidates(candidates, top_n=10):
    eligible = [
        item for item in candidates
        if item.get("scanner_accepted", item.get("accepted", False))
    ]
    use_ev_edge = any(item.get("ev_net_edge_r") is not None for item in eligible)
    eligible.sort(
        key=lambda item: (
            finite_float(item.get("ev_net_edge_r"), float("-inf"))
            if use_ev_edge
            else finite_float(item.get("score"), 0.0),
            finite_float(item.get("score"), 0.0),
            finite_float(item.get("rolling_sharpe"), 0.0),
            finite_float(item.get("quote_volume"), 0.0),
        ),
        reverse=True,
    )
    selected = eligible[:max(1, int(top_n or 10))]
    for item in selected:
        item["selection_state"] = "SELECTED"
    return selected


def detect_concentration(candidates, key="auto_set_id", threshold_pct=50.0):
    values = [item.get(key) for item in candidates if item.get(key) not in (None, "", 0)]
    if not values:
        return None
    counts = Counter(values)
    value, count = counts.most_common(1)[0]
    share = (count / len(values)) * 100.0
    if share >= threshold_pct:
        return {
            "key": key,
            "value": value,
            "count": count,
            "total": len(values),
            "share_pct": round(share, 2),
        }
    return None


def build_selection_report(candidates, rejects=None, *, top_n=10):
    selected = rank_candidates(candidates, top_n)
    selected_ids = {id(item) for item in selected}
    watch_only = [
        item
        for item in candidates
        if id(item) not in selected_ids and item.get("accepted")
    ]
    watch_only.sort(
        key=lambda item: (
            finite_float(item.get("score"), 0.0),
            finite_float(item.get("rolling_sharpe"), 0.0),
            finite_float(item.get("quote_volume"), 0.0),
        ),
        reverse=True,
    )
    actionability_counts = Counter()
    watch_only_reason_counts = Counter()
    for item in candidates:
        state = item.get("selection_state") or ("ACCEPTED" if item.get("accepted") else "REJECTED")
        actionability_counts[state] += 1
        if item.get("accepted") and id(item) not in selected_ids:
            reasons = []
            if item.get("ev_allowed") is False:
                reasons.append("EV_EDGE_NOT_ACTIONABLE")
            reasons.extend(item.get("soft_warnings") or [])
            if item.get("ev_reason"):
                reasons.append(str(item.get("ev_reason"))[:160])
            if not reasons:
                reasons.append(state)
            for reason in dict.fromkeys(reasons):
                watch_only_reason_counts[reason] += 1
    reject_counts = Counter()
    for item in rejects or []:
        for reason in item.get("reject_reasons", []) or ["UNKNOWN"]:
            reject_counts[reason] += 1
    concentration = detect_concentration(selected)
    return {
        "selected": selected,
        "watch_only": watch_only[:max(1, int(top_n or 10))],
        "actionability_counts": dict(actionability_counts),
        "watch_only_reason_counts": dict(watch_only_reason_counts),
        "reject_counts": dict(reject_counts),
        "concentration_warning": concentration,
        "total_scored": len(candidates),
        "total_rejected": len(rejects or []),
        "scanner_rules": [
            "valid_usdt_perp",
            "min_quote_volume",
            "min_trade_count",
            "max_spread",
        ],
    }
