"""TradFi-specific guardrails for the aggressive sub-1,000 USDT profile.

The small-account strategy remains trend-first and intentionally aggressive.
These helpers only remove risks that are unique to equity perpetuals: a
stale/fixed underlying index, an unanchored pre-listing contract, adverse
mark/index dislocation, and leverage stacked on top of a geared ETP.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Mapping


TRADFI_SMALL_ACCOUNT_PROFILE_VERSION = "tradfi_small_account_v2"

# Current geared ETPs available in Binance's TradFi universe.  The values are
# the funds' stated *daily* exposure targets, not expected multi-day returns.
TRADFI_GEARED_ETP_DAILY_MULTIPLIERS = {
    "KORU": 3.0,
    "SOXL": 3.0,
    "SOXS": -3.0,
    "SQQQ": -3.0,
    "TMF": 3.0,
    "TQQQ": 3.0,
    "TZA": -3.0,
    "UVXY": 1.5,
}

_RISK_TIER_ORDER = {"base": 0, "strong": 1, "elite": 2}


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _symbol_base(symbol: Any) -> str:
    value = str(symbol or "").strip().upper().replace(":USDT", "")
    if "/" in value:
        return value.split("/", 1)[0]
    if value.endswith("USDT"):
        return value[:-4]
    return value


def classify_tradfi_instrument(
    symbol: Any,
    underlying_type: Any = None,
) -> dict[str, Any]:
    """Return deterministic instrument traits needed by live and scanner paths."""

    base = _symbol_base(symbol)
    normalized_type = str(underlying_type or "EQUITY").strip().upper() or "EQUITY"
    daily_multiplier = TRADFI_GEARED_ETP_DAILY_MULTIPLIERS.get(base)
    geared = daily_multiplier is not None and abs(daily_multiplier) > 1.0
    return {
        "profile": TRADFI_SMALL_ACCOUNT_PROFILE_VERSION,
        "base": base,
        "underlying_type": normalized_type,
        "premarket_contract": normalized_type == "PREMARKET",
        "geared_etp": geared,
        "embedded_daily_leverage": daily_multiplier,
        "inverse_etp": bool(daily_multiplier is not None and daily_multiplier < 0.0),
        # A 3x fund at 4x futures leverage is already roughly 12x directional
        # daily exposure.  Ordinary equities retain the strategy's 7x ceiling.
        "small_account_leverage_ceiling": 4 if geared else 7,
    }


def _lower_tier(left: str | None, right: str | None) -> str | None:
    values = [
        value
        for value in (str(left or "").lower(), str(right or "").lower())
        if value in _RISK_TIER_ORDER
    ]
    if not values:
        return None
    return min(values, key=lambda value: _RISK_TIER_ORDER[value])


def cap_tradfi_risk_tier(risk_tier: Any, ceiling: Any) -> str:
    tier = str(risk_tier or "base").strip().lower()
    if tier not in _RISK_TIER_ORDER:
        tier = "base"
    cap = str(ceiling or "").strip().lower()
    if cap not in _RISK_TIER_ORDER:
        return tier
    return min((tier, cap), key=lambda value: _RISK_TIER_ORDER[value])


def evaluate_tradfi_small_account_guardrails(
    *,
    symbol: Any,
    side: Any,
    candidate_source: Any,
    session_status: Mapping[str, Any] | None = None,
    futures_context: Mapping[str, Any] | None = None,
    underlying_type: Any = None,
    instrument_profile: Mapping[str, Any] | None = None,
    basis_soft_limit_pct: float = 0.75,
    basis_hard_limit_pct: float = 1.50,
) -> dict[str, Any]:
    """Evaluate only TradFi risks that do not exist in ordinary crypto perps.

    Weekday extended-hours trend entries remain eligible.  A fast event may
    corroborate such an entry, but cannot be the sole reason to enter while the
    underlying market is closed.
    """

    instrument = dict(
        instrument_profile
        or classify_tradfi_instrument(symbol, underlying_type)
    )
    session = dict(session_status or {})
    context = dict(futures_context or {})
    direction = str(side or "").strip().lower()
    source = str(candidate_source or "trend_only").strip().lower()
    session_open = bool(session.get("open"))
    session_reason = str(session.get("reason") or "unknown").strip().lower()
    risk_tier_ceiling: str | None = None

    result = {
        "profile": TRADFI_SMALL_ACCOUNT_PROFILE_VERSION,
        "allowed": True,
        "code": "TRADFI_SMALL_ACCOUNT_CONTEXT_ALLOWED",
        "reason": "TradFi small-account context allowed",
        "session_open": session_open,
        "session_reason": session_reason,
        "candidate_source": source,
        "underlying_type": instrument.get("underlying_type"),
        "geared_etp": bool(instrument.get("geared_etp")),
        "embedded_daily_leverage": instrument.get("embedded_daily_leverage"),
        "leverage_ceiling": int(
            instrument.get("small_account_leverage_ceiling", 7) or 7
        ),
        "risk_tier_ceiling": None,
    }

    if instrument.get("premarket_contract"):
        result.update({
            "allowed": False,
            "code": "REJECTED_TRADFI_PREMARKET_CONTRACT",
            "reason": "pre-listing TradFi contract has no mature listed underlying",
        })
        return result

    # Binance can fix the underlying index on weekends/holidays.  A fresh
    # order-book move then cannot be independently verified against cash-market
    # price discovery, so no new aggressive position is opened.
    if session_reason in {"weekend", "holiday"}:
        result.update({
            "allowed": False,
            "code": "REJECTED_TRADFI_UNDERLYING_CLOSED",
            "reason": f"underlying market price discovery is closed ({session_reason})",
        })
        return result

    if not session_open:
        risk_tier_ceiling = _lower_tier(risk_tier_ceiling, "strong")
        if source in {"event_only", "event_conflict_winner"}:
            result.update({
                "allowed": False,
                "code": "REJECTED_TRADFI_EXTENDED_EVENT_ONLY",
                "reason": "extended-hours flow event lacks a regular-session trend anchor",
                "risk_tier_ceiling": risk_tier_ceiling,
            })
            return result

    basis_pct = _finite(context.get("basis_pct"))
    adverse_basis_pct = None
    if basis_pct is not None and direction in {"long", "short"}:
        adverse_basis_pct = basis_pct if direction == "long" else -basis_pct
    result.update({
        "basis_pct": basis_pct,
        "adverse_basis_pct": adverse_basis_pct,
        "basis_soft_limit_pct": float(basis_soft_limit_pct),
        "basis_hard_limit_pct": float(basis_hard_limit_pct),
    })
    if adverse_basis_pct is not None and adverse_basis_pct >= float(basis_hard_limit_pct):
        result.update({
            "allowed": False,
            "code": "REJECTED_TRADFI_BASIS_DISLOCATION",
            "reason": (
                f"adverse mark/index basis {adverse_basis_pct:.3f}% exceeds "
                f"{float(basis_hard_limit_pct):.3f}%"
            ),
        })
        return result
    if adverse_basis_pct is not None and adverse_basis_pct >= float(basis_soft_limit_pct):
        risk_tier_ceiling = _lower_tier(risk_tier_ceiling, "base")
        result["code"] = "TRADFI_BASIS_RISK_TIER_CAPPED"
        result["reason"] = (
            f"adverse mark/index basis {adverse_basis_pct:.3f}%: base tier only"
        )

    result["risk_tier_ceiling"] = risk_tier_ceiling
    return result


__all__ = (
    "TRADFI_GEARED_ETP_DAILY_MULTIPLIERS",
    "TRADFI_SMALL_ACCOUNT_PROFILE_VERSION",
    "cap_tradfi_risk_tier",
    "classify_tradfi_instrument",
    "evaluate_tradfi_small_account_guardrails",
)
