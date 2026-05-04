"""Market normalization helpers for Predict.fun responses."""

from __future__ import annotations

import re


CRYPTO_KEYWORDS = {
    "bitcoin",
    "btc",
    "ethereum",
    "eth",
    "crypto",
    "bnb",
    "solana",
    "sol",
    "xrp",
}
MACRO_KEYWORDS = {
    "cpi",
    "inflation",
    "fomc",
    "fed",
    "rate",
    "gdp",
    "jobs",
    "payroll",
    "unemployment",
    "macro",
    "economy",
}
UNSUPPORTED_CATEGORY_HINTS = {
    "sports",
    "esports",
    "politics",
    "entertainment",
    "culture",
}


def _text(value):
    return str(value or "").strip()


def _lower_blob(*values):
    return " ".join(_text(v).lower() for v in values if _text(v))


def _contains_keyword(blob, keywords):
    for word in keywords:
        word = str(word or "").lower()
        if not word:
            continue
        if len(word) <= 4:
            if re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", blob):
                return True
        elif word in blob:
            return True
    return False


def _status_text(value):
    if isinstance(value, dict):
        for key in ("value", "status", "name", "code"):
            if value.get(key):
                return _text(value.get(key)).lower()
    return _text(value).lower()


def detect_market_type(raw):
    title = _text(raw.get("title") or raw.get("question"))
    category = _text(raw.get("categorySlug") or raw.get("category") or raw.get("category_slug"))
    variant = _text(raw.get("marketVariant") or raw.get("variant") or raw.get("market_variant"))
    blob = _lower_blob(title, category, variant, raw.get("description"))
    if _contains_keyword(blob, CRYPTO_KEYWORDS):
        return "crypto"
    if _contains_keyword(blob, MACRO_KEYWORDS):
        return "macro"
    if _contains_keyword(blob, UNSUPPORTED_CATEGORY_HINTS):
        return "unsupported"
    return "unknown"


def normalize_market(raw):
    raw = raw or {}
    market_type = detect_market_type(raw)
    trading_status = _status_text(raw.get("tradingStatus"))
    status = _status_text(raw.get("status"))
    is_visible = raw.get("isVisible", True) is not False
    is_tradable = is_visible and not any(
        word in f"{trading_status} {status}"
        for word in ("closed", "resolved", "paused", "halted", "cancelled", "canceled")
    )
    reject_reasons = []
    if market_type not in {"crypto", "macro"}:
        reject_reasons.append("REJECTED_PREDICTION_CATEGORY")
    if not is_tradable:
        reject_reasons.append("REJECTED_PREDICTION_NOT_TRADABLE")
    return {
        "id": raw.get("id") or raw.get("marketId") or raw.get("market_id"),
        "title": _text(raw.get("title") or raw.get("question")),
        "question": _text(raw.get("question") or raw.get("title")),
        "category_slug": _text(raw.get("categorySlug") or raw.get("category") or raw.get("category_slug")),
        "market_type": market_type,
        "fee_rate_bps": float(raw.get("feeRateBps") or raw.get("fee_rate_bps") or 200),
        "outcomes": list(raw.get("outcomes") or []),
        "is_visible": is_visible,
        "is_tradable": is_tradable,
        "accepted": not reject_reasons,
        "reject_reasons": reject_reasons,
        "raw": raw,
    }
