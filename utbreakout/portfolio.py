"""Rule-based portfolio risk guard for UT Breakout candidates."""

from math import isfinite


def _finite_float(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def default_portfolio_config():
    return {
        "max_total_open_risk_usdt": 100.0,
        "max_same_direction_positions": 3,
        "max_sector_positions": 2,
        "max_correlated_positions": 1,
        "daily_loss_limit_usdt": 50.0,
        "weekly_loss_limit_usdt": 150.0,
        "max_consecutive_losses": 5,
        "cooldown_remaining_sec": 0,
    }


def _norm_symbol(symbol):
    return str(symbol or "").upper().replace(":USDT", "").replace("/", "")


def evaluate_portfolio_risk(candidate=None, open_positions=None, account_state=None, cfg=None):
    cfg = {**default_portfolio_config(), **(cfg or {})}
    candidate = dict(candidate or {})
    account_state = dict(account_state or {})
    positions = [dict(pos or {}) for pos in (open_positions or [])]
    reasons = []

    symbol = _norm_symbol(candidate.get("symbol"))
    side = str(candidate.get("side") or "").lower()
    risk_usdt = max(0.0, _finite_float(candidate.get("risk_usdt"), 0.0))
    sector = str(candidate.get("sector") or "").lower()
    correlated = {_norm_symbol(item) for item in candidate.get("correlated_symbols") or []}

    for pos in positions:
        if _norm_symbol(pos.get("symbol")) == symbol:
            reasons.append("DUPLICATE_SYMBOL")
            break

    current_risk = sum(max(0.0, _finite_float(pos.get("risk_usdt"), 0.0)) for pos in positions)
    if current_risk + risk_usdt > _finite_float(cfg["max_total_open_risk_usdt"], 100.0):
        reasons.append("TOTAL_RISK_LIMIT")

    same_side = sum(1 for pos in positions if str(pos.get("side") or "").lower() == side)
    if side and same_side >= int(cfg.get("max_same_direction_positions", 3) or 3):
        reasons.append("SAME_DIRECTION_LIMIT")

    if sector:
        sector_count = sum(1 for pos in positions if str(pos.get("sector") or "").lower() == sector)
        if sector_count >= int(cfg.get("max_sector_positions", 2) or 2):
            reasons.append("SECTOR_LIMIT")

    if correlated:
        corr_count = sum(1 for pos in positions if _norm_symbol(pos.get("symbol")) in correlated)
        if corr_count >= int(cfg.get("max_correlated_positions", 1) or 1):
            reasons.append("CORRELATION_LIMIT")

    if abs(_finite_float(account_state.get("daily_pnl_usdt"), 0.0)) >= _finite_float(cfg["daily_loss_limit_usdt"], 50.0) and _finite_float(account_state.get("daily_pnl_usdt"), 0.0) < 0:
        reasons.append("DAILY_LOSS_LIMIT")
    if abs(_finite_float(account_state.get("weekly_pnl_usdt"), 0.0)) >= _finite_float(cfg["weekly_loss_limit_usdt"], 150.0) and _finite_float(account_state.get("weekly_pnl_usdt"), 0.0) < 0:
        reasons.append("WEEKLY_LOSS_LIMIT")
    if int(_finite_float(account_state.get("consecutive_losses"), 0)) >= int(cfg.get("max_consecutive_losses", 5) or 5):
        reasons.append("CONSECUTIVE_LOSS_LIMIT")
    if _finite_float(account_state.get("cooldown_remaining_sec"), _finite_float(cfg.get("cooldown_remaining_sec"), 0.0)) > 0:
        reasons.append("COOLDOWN_ACTIVE")

    allowed = not reasons
    return {
        "allowed": allowed,
        "blocked": not allowed,
        "reasons": reasons,
        "current_risk_usdt": round(current_risk, 8),
        "candidate_risk_usdt": round(risk_usdt, 8),
        "projected_risk_usdt": round(current_risk + risk_usdt, 8),
        "risk_multiplier": 1.0 if allowed else 0.0,
    }
