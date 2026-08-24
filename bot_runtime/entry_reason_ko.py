"""Korean, user-facing summaries for automatic-entry diagnostics.

The engine keeps stable English reason codes for logs and tests. This module
translates those codes only at presentation boundaries; it never makes a trade
decision or places an order.
"""

from __future__ import annotations

import re


_EXACT_REASONS = {
    "insufficient_completed_candles": "완료된 봉이 아직 부족해 추세를 평가하지 못했습니다.",
    "atr_unavailable": "변동성(ATR)을 계산할 데이터가 부족해 진입하지 않았습니다.",
    "realized_volatility_unavailable": "실현 변동성을 계산할 데이터가 부족해 진입하지 않았습니다.",
    "multi_horizon_direction_not_aligned": "단기·중기·장기 추세 방향이 충분히 일치하지 않았습니다.",
    "momentum_strength_too_low": "추세 방향은 보이지만 모멘텀이 약해 진입하지 않았습니다.",
    "slow_horizon_conflict": "장기 추세가 현재 진입 방향과 충돌했습니다.",
    "trend_structure_not_aligned": "가격과 이동평균의 추세 구조가 진입 방향과 일치하지 않았습니다.",
    "fast_slow_turning_conflict": "단기 추세가 장기 추세의 반대 방향으로 꺾여 진입하지 않았습니다.",
    "waiting_for_weighted_trend_entry": "추세는 감지됐지만 교차·돌파·재가속 진입 조건이 아직 완성되지 않았습니다.",
    "trend_efficiency_too_low": "가격 움직임이 한 방향으로 충분히 효율적이지 않아 진입하지 않았습니다.",
    "volatility_shock": "단기 변동성이 갑자기 커져 추격 진입을 피했습니다.",
    "latest_bar_extreme_range": "최근 봉의 변동폭이 과도해 진입하지 않았습니다.",
    "l2_stressed": "호가창 유동성 상태가 불안정해 진입하지 않았습니다.",
    "score_below_threshold": "종합 추세 점수가 진입 기준에 미달했습니다.",
}


def _contains_hangul(value):
    return bool(re.search(r"[가-힣]", str(value or "")))


def _number(data, key):
    try:
        return float((data or {}).get(key))
    except (TypeError, ValueError):
        return None


def explain_entry_reason_ko(reason=None, *, code=None, data=None):
    """Return one concise Korean explanation without changing engine state."""

    raw_reason = str(reason or "").strip()
    raw_code = str(code or "").strip()
    combined = f"{raw_code} {raw_reason}".strip()
    lowered = combined.lower()
    uppered = combined.upper()

    for token, message in _EXACT_REASONS.items():
        if token in lowered:
            return message

    if "READY_TOO_OLD" in uppered or "STATUS_READY EVENT IS TOO OLD" in uppered:
        ready_age = _number(data, "ready_age_sec")
        max_age = _number(data, "max_ready_age_sec")
        if ready_age is not None and max_age is not None:
            return (
                "진입 신호가 오래되어 추격 주문을 하지 않았습니다. "
                f"신호 경과 {ready_age / 60.0:.1f}분 / 허용 {max_age / 60.0:.1f}분"
            )
        return "진입 신호가 오래되어 추격 주문을 하지 않았습니다."
    if "NO_STATUS_READY" in uppered or "NO LIVE STATUS_READY" in uppered:
        return "아직 주문으로 연결할 수 있는 새로운 진입 신호가 없습니다."
    if "NO STATUS_READY" in uppered:
        return "아직 주문으로 연결할 수 있는 확정 진입 신호가 없습니다."
    if "DECISION_ALREADY_CONSUMED" in uppered or "ALREADY ENTERED ONCE" in uppered:
        return "같은 완료봉 신호로 이미 한 번 진입했기 때문에 중복 진입하지 않았습니다."
    if "MISSING_PLAN" in uppered or "ENTRY PLAN IS MISSING" in uppered:
        return "신호는 있었지만 주문 수량·손절 계획이 없어 진입하지 않았습니다."
    if "NO_READY_SIDE" in uppered or "READY SIDE IS MISSING" in uppered:
        return "진입 방향이 확정되지 않아 주문하지 않았습니다."
    if "DUPLICATE BRIDGE ATTEMPT COOLDOWN" in uppered or raw_code.upper() == "COOLDOWN":
        return "동일 신호의 중복 주문을 막기 위한 짧은 재시도 대기 중입니다."
    if "RECENT_LOSS_COOLDOWN" in uppered or "RECENT LOSS" in uppered:
        return "최근 손실 뒤 재진입 대기 시간이 남아 있어 진입하지 않았습니다."
    if "DAILY_SL_LOCKOUT" in uppered or "DAILY SL LOCKOUT" in uppered:
        return "당일 손절 후 재진입 잠금이 적용되어 진입하지 않았습니다."
    if "DAILY_TRADE_LIMIT" in uppered or "DAILY TRADE COUNT" in uppered:
        return "자동매매 일일 진입 횟수 한도에 도달했습니다."
    if "DAILY_LOSS" in uppered or "DAILY PNL" in uppered:
        return "일일 손실 한도에 도달해 신규 진입을 중단했습니다."
    if "USER_CUSTOM_MODE_ACTIVE" in uppered:
        return "사용자 커스텀 모드가 켜져 있어 자동 전략 진입을 쉬고 있습니다."
    if "PAUSE" in uppered or "CRITICAL_PAUSE" in uppered:
        return "봇이 일시정지 상태라 신규 진입하지 않았습니다."
    if "DIRECTION_FILTER" in uppered or "TRADE DIRECTION" in uppered:
        return "현재 허용된 매매 방향과 신호 방향이 달라 진입하지 않았습니다."
    if "SMALL_ACCOUNT_FAST_TREND_DECAY" in uppered:
        return "장기 추세는 남아 있지만 단기 추세 힘이 빠르게 약해져 재가속을 기다립니다."
    if "SMALL_ACCOUNT_SIGNAL_INVALIDATED" in uppered:
        return "완료봉 신호 뒤 가격이 반대 방향으로 크게 움직여 해당 진입 신호를 무효화했습니다."
    if "SMALL_ACCOUNT_CROSSOVER_EXTENSION" in uppered:
        return "이동평균 교차 직후 가격이 이미 과도하게 벌어져 눌림 또는 새 신호를 기다립니다."
    if "SMALL_ACCOUNT_LOWER_TIMEFRAME_CONFLICT" in uppered:
        return "상위 추세와 실제 진입 시간대 방향이 충돌해 방향이 다시 맞을 때까지 기다립니다."
    if "SMALL_ACCOUNT_WEAK_MATURE_CONTINUATION" in uppered:
        return "성숙한 추세의 추진력이 약해 눌림 후 재개 또는 거래량 동반 돌파를 기다립니다."
    if "SMALL_ACCOUNT_CROWDED_EXTENSION" in uppered:
        return "가격이 평균선에서 벌어진 상태에서 펀딩·베이시스 과열까지 겹쳐 추격 진입하지 않습니다."
    if "CHANGE_POINT_FLOW_CONFLICT" in uppered:
        return "가격 방향과 실제 호가·시장가 주문 흐름이 강하게 충돌해 신규 진입을 기다립니다."
    if "CHANGE_POINT_FLOW_NO_EDGE" in uppered:
        return "최근 가격 체제 전환이나 지속적인 주문 흐름 우위가 아직 뚜렷하지 않습니다."
    if "CHANGE_POINT_FLOW_SIDE" in uppered:
        return "체제 전환·주문 흐름 평가에 필요한 진입 방향이 확정되지 않았습니다."
    if "INDEPENDENT_EVENT_HTF_EXTENSION" in uppered:
        return "빠른 이벤트 신호는 생겼지만 1시간 평균선에서 이미 너무 멀어 추격 진입하지 않습니다."
    if "INDEPENDENT_EVENT_BROAD_TREND_CONFLICT" in uppered:
        return "빠른 이벤트 방향이 중기·장기 추세 방향과 반대라 신규 진입을 기다립니다."
    if "INDEPENDENT_EVENT_EXTREME_RANGE" in uppered:
        return "빠른 이벤트 발생 전후의 1시간 봉 변동폭이 과도해 진입하지 않습니다."
    if "INDEPENDENT_EVENT_VOLATILITY_SHOCK" in uppered:
        return "빠른 이벤트와 함께 변동성 충격이 발생해 가격이 안정될 때까지 기다립니다."
    if "TRADFI_PREMARKET_CONTRACT" in uppered:
        return "아직 상장 전 기초자산을 추적하는 TradFi 종목이라 신규 진입하지 않습니다."
    if "TRADFI_UNDERLYING_CLOSED" in uppered:
        return "기초 주식시장이 쉬어 기준가격 발견이 멈춘 시간이라 신규 진입하지 않습니다."
    if "TRADFI_EXTENDED_EVENT_ONLY" in uppered:
        return "주식 정규장 밖에서 나온 단기 주문흐름 신호만으로는 진입하지 않고 추세 확인을 기다립니다."
    if "TRADFI_BASIS_DISLOCATION" in uppered:
        return "TradFi 선물 가격이 기초자산 기준가격보다 불리하게 과도하게 벌어져 진입하지 않습니다."
    if "TREND_EVENT_CANDIDATE" in uppered or "INDEPENDENT_DIRECTION_AMBIGUOUS" in uppered:
        return "기존 추세와 새 체제·주문 흐름 후보가 충돌하거나 어느 방향도 충분히 강하지 않습니다."
    if "REGION_RESTRICTED" in uppered or "RESTRICTED_SYMBOL" in uppered:
        return "현재 계정에서 제한된 종목이라 진입하지 않았습니다."
    if "INVALID_MARKET" in uppered or "SYMBOL_PREFLIGHT" in uppered:
        return "현재 거래소 모드에서 주문할 수 없는 종목이라 진입하지 않았습니다."
    if "LOW_QUOTE_VOLUME" in uppered or "QUOTE VOLUME" in uppered:
        return "24시간 거래대금이 유동성 기준에 미달해 진입하지 않았습니다."
    if "MARKET_QUALITY" in uppered:
        return "스프레드·변동성 등 시장 품질이 기준에 미달해 진입하지 않았습니다."
    if "L2" in uppered or "ORDER BOOK" in uppered or "DEPTH" in uppered:
        return "호가 스프레드·깊이 조건이 안전 기준에 미달해 진입하지 않았습니다."
    if "INSUFFICIENT" in uppered and any(
        token in uppered for token in ("BALANCE", "MARGIN", "USDT", "FUNDS")
    ):
        return "주문에 필요한 가용 증거금이 부족해 진입하지 않았습니다."
    if "MIN_NOTIONAL" in uppered or "MINIMUM NOTIONAL" in uppered:
        return "계산된 주문 금액이 거래소 최소 주문금액보다 작아 진입하지 않았습니다."
    if "LIQUIDATION" in uppered:
        return "손절가와 예상 청산가 사이의 안전거리가 부족해 진입하지 않았습니다."
    if "RISK" in uppered and any(
        token in uppered for token in ("BLOCK", "REJECT", "UNSAFE", "LIMIT")
    ):
        return "예상 손실이 현재 위험 한도를 넘어 진입하지 않았습니다."
    if any(token in uppered for token in ("OHLCV", "DATA UNAVAILABLE", "ATR UNAVAILABLE")):
        return "시세·봉 데이터가 부족하거나 최신 상태가 아니어서 진입하지 않았습니다."
    if "STALE" in uppered or "TOO OLD" in uppered or "TOO LATE" in uppered:
        return "신호가 너무 오래됐거나 가격이 이미 움직여 추격 진입을 피했습니다."
    if "ENTRY MOVED" in uppered or "CHASE" in uppered or "EXTENDED" in uppered:
        return "가격이 신호 지점에서 너무 멀어져 추격 진입하지 않았습니다."
    if "STRUCTURE INVALIDATED" in uppered or "INVALIDATED BEFORE ORDER" in uppered:
        return "주문 전에 추세 구조가 무효화되어 진입하지 않았습니다."
    if "ACCEPTED_ENTRY" in uppered:
        return "진입 조건은 충족됐으며 주문 실행 여부를 최종 확인 중입니다."
    if "POSITION" in uppered and ("OPEN" in uppered or "보유" in raw_reason):
        return "이미 열린 포지션이 있어 신규 진입하지 않습니다."

    if _contains_hangul(raw_reason):
        return raw_reason
    if any(token in uppered for token in ("REJECT", "BLOCK", "FAILED", "ERROR")):
        return "진입 전 안전 확인을 통과하지 못해 주문하지 않았습니다."
    if "WAIT" in uppered or "NO SIGNAL" in uppered or not combined:
        return "아직 유효한 신규 진입 조건이 완성되지 않았습니다."
    return "현재 조건을 평가 중이며 아직 신규 진입이 확정되지 않았습니다."


_DIAGNOSTIC_STAGES = {
    "AUTO_ENTRY_BRIDGE_BLOCKED",
    "ENTRY_BLOCKED",
    "ORDER_FAILED",
    "NO_POSITION_AFTER_ENTRY",
    "STATUS_NOT_READY",
    "STATUS_EVALUATED",
    "SIGNAL_CALCULATED",
    "EXECUTION_SAFETY_GATE",
}


def build_entry_diagnostic(engine, symbol=None, *, fallback_reason=None, status=None):
    """Build a read-only diagnostic from the latest signal/entry breadcrumb."""

    status = status if isinstance(status, dict) else {}
    selected_event = None
    events = []
    recent = getattr(engine, "_utbreakout_recent_trace_events", None)
    if callable(recent):
        try:
            events = recent(symbol, limit=80)
        except Exception:
            events = []
        for event in reversed(events or []):
            if not isinstance(event, dict):
                continue
            stage = str(event.get("stage") or "").upper()
            event_status = str(event.get("status") or "").upper()
            if stage not in _DIAGNOSTIC_STAGES:
                continue
            if stage == "EXECUTION_SAFETY_GATE" and event_status == "PASS":
                continue
            selected_event = event
            break

    # NO_STATUS_READY is normally a downstream consequence of a strategy
    # decision that produced no signal. In that case the immediately preceding
    # strategy reason is the useful answer to "why no entry?". Keep genuine
    # execution blockers (stale signal, risk, balance, order failure, etc.) at
    # their existing higher priority.
    if (
        selected_event
        and str(selected_event.get("stage") or "").upper()
        == "AUTO_ENTRY_BRIDGE_BLOCKED"
        and str(selected_event.get("status") or "").upper()
        == "NO_STATUS_READY"
    ):
        blocked_ts = float(selected_event.get("ts") or 0.0)
        for event in reversed(events or []):
            if not isinstance(event, dict) or event is selected_event:
                continue
            event_ts = float(event.get("ts") or 0.0)
            if blocked_ts > 0 and not 0.0 <= blocked_ts - event_ts <= 120.0:
                continue
            stage = str(event.get("stage") or "").upper()
            if stage not in {
                "SIGNAL_CALCULATED",
                "STATUS_NOT_READY",
                "STATUS_EVALUATED",
            }:
                continue
            data = event.get("data")
            data = data if isinstance(data, dict) else {}
            reason = str(data.get("reason") or "").strip()
            if reason and "ACCEPTED_ENTRY" not in reason.upper():
                selected_event = event
                break
        else:
            status_reason = str(status.get("reason") or "").strip()
            if status_reason and "ACCEPTED_ENTRY" not in status_reason.upper():
                selected_event = None
                fallback_reason = status_reason

    if selected_event:
        data = selected_event.get("data")
        data = data if isinstance(data, dict) else {}
        code = str(selected_event.get("status") or selected_event.get("stage") or "")
        raw_reason = str(data.get("reason") or data.get("blockers") or code)
        return {
            "symbol": str(selected_event.get("symbol") or symbol or ""),
            "message": explain_entry_reason_ko(raw_reason, code=code, data=data),
            "code": code,
            "raw_reason": raw_reason,
            "stage": str(selected_event.get("stage") or ""),
            "epoch": int(float(selected_event.get("ts") or 0.0)),
        }

    raw_reason = str(
        fallback_reason
        or status.get("reason")
        or status.get("reject_code")
        or ""
    )
    code = str(status.get("reject_code") or status.get("accepted_code") or "")
    return {
        "symbol": str(status.get("symbol") or symbol or ""),
        "message": explain_entry_reason_ko(raw_reason, code=code, data=status),
        "code": code,
        "raw_reason": raw_reason,
        "stage": str(status.get("stage") or ""),
        "epoch": 0,
    }


__all__ = ("build_entry_diagnostic", "explain_entry_reason_ko")
