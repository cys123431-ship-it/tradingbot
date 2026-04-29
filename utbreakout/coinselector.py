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

DEFAULT_COIN_SELECTOR_CONFIG = {
    "enabled": True,
    "auto_apply_watchlist": False,
    "min_quote_volume_usdt": 100_000_000.0,
    "ideal_quote_volume_usdt": 1_000_000_000.0,
    "min_trade_count": 20_000,
    "ideal_trade_count": 500_000,
    "max_spread_pct": 0.08,
    "max_abs_price_change_pct": 18.0,
    "analysis_limit": 20,
    "top_n": 10,
    "min_final_score": 55.0,
    "refresh_interval_seconds": 300,
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
        and contract_type == "PERPETUAL"
        and status == "TRADING"
        and active is not False
        and swap_like
    )


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


def build_base_candidate(symbol, ticker, market=None, cfg=None, sector_tags=None):
    cfg = {**default_coin_selector_config(), **(cfg or {})}
    metrics = extract_ticker_metrics(symbol, ticker)
    sector_tags = sorted(set(sector_tags if sector_tags is not None else sector_tags_for_symbol(symbol, cfg.get("sector_overrides"))))
    excluded_sectors = {str(item).strip().lower() for item in cfg.get("excluded_sectors", [])}
    blacklist = {normalize_symbol(item) for item in cfg.get("blacklist", [])}
    reject_reasons = []

    if not market_is_usdt_perpetual(symbol, market):
        reject_reasons.append("REJECTED_NOT_USDT_PERPETUAL_TRADING")
    if metrics["normalized_symbol"] in blacklist or metrics["base"] in blacklist:
        reject_reasons.append("REJECTED_BLACKLIST")
    if metrics["quote_volume"] < finite_float(cfg.get("min_quote_volume_usdt"), 100_000_000.0):
        reject_reasons.append("REJECTED_VOLUME_LOW")
    min_count = finite_int(cfg.get("min_trade_count"), 0)
    if min_count > 0 and metrics["trade_count"] > 0 and metrics["trade_count"] < min_count:
        reject_reasons.append("REJECTED_TRADE_COUNT_LOW")
    max_spread = finite_float(cfg.get("max_spread_pct"), 0.08)
    if metrics["spread_pct"] is not None and metrics["spread_pct"] > max_spread:
        reject_reasons.append("REJECTED_SPREAD_WIDE")
    max_abs_change = finite_float(cfg.get("max_abs_price_change_pct"), 0.0)
    if max_abs_change > 0 and abs(metrics["percentage"]) > max_abs_change:
        reject_reasons.append("REJECTED_PRICE_CHANGE_EXTREME")
    if excluded_sectors.intersection(sector_tags):
        reject_reasons.append("REJECTED_EXCLUDED_SECTOR")

    metrics.update({
        "exchange_symbol": symbol,
        "sector_tags": sector_tags,
        "accepted": not reject_reasons,
        "reject_reasons": reject_reasons,
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
    return round(raw * 0.25, 2)


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
    return round(raw * 0.30, 2)


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
    return round(clamp(raw) * 0.20, 2)


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
    return round(clamp(selected_score) * 0.10, 2)


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
    return round((funding_score * 0.65 + oi_score * 0.35) * 0.10, 2)


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
    cfg=None,
):
    cfg = {**default_coin_selector_config(), **(cfg or {})}
    auto_scores = (auto_analysis or {}).get("scores", {}) if isinstance(auto_analysis, dict) else {}
    components = {
        "liquidity_cost": score_liquidity(candidate, cfg),
        "utbreakout_regime": score_ut_regime(auto_scores),
        "auto_set": score_set_suitability(auto_scores, selected_set_id, selected_set_info),
        "adaptive_tf": score_adaptive_timeframe(adaptive_decision),
        "futures_health": score_futures_context(futures_context),
        "sector_risk": score_sector(candidate),
    }
    total = round(sum(components.values()), 2)
    result = dict(candidate)
    set_info = selected_set_info or {}
    adaptive_decision = adaptive_decision or {}
    result.update({
        "score": total,
        "component_scores": components,
        "selection_state": "SELECTED" if candidate.get("accepted") and total >= finite_float(cfg.get("min_final_score"), 55.0) else "WATCH_ONLY",
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
    if result["adaptive_tf"] == "NO_TRADE":
        result.setdefault("soft_warnings", []).append("ADAPTIVE_NO_TRADE")
    if result["auto_set_id"] in {49, 50}:
        result.setdefault("soft_warnings", []).append("AUTO_FALLBACK_OR_EMERGENCY")
    return result


def rank_candidates(candidates, top_n=10):
    accepted = [item for item in candidates if item.get("accepted")]
    accepted.sort(key=lambda item: (finite_float(item.get("score"), 0.0), finite_float(item.get("quote_volume"), 0.0)), reverse=True)
    return accepted[:max(1, int(top_n or 10))]


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
    reject_counts = Counter()
    for item in rejects or []:
        for reason in item.get("reject_reasons", []) or ["UNKNOWN"]:
            reject_counts[reason] += 1
    concentration = detect_concentration(selected)
    return {
        "selected": selected,
        "reject_counts": dict(reject_counts),
        "concentration_warning": concentration,
        "total_scored": len(candidates),
        "total_rejected": len(rejects or []),
    }
