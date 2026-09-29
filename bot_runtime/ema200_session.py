"""EMA200 small-account session status and the morning entry-state reset.

The morning reset returns automatic entries to the state of the first entry of
the Korea calendar day: the daily automatic entry count, the EMA200 loss-streak
stage (margin ladder) and today's EMA200 daily-loss baseline.  Trade history is
never deleted and the 7-day loss limit is untouched.
"""

from __future__ import annotations

from datetime import datetime, timezone

from trading_safety.order_state import DAILY_LOSS_ENTRY_LOCK_KEY

from .decision_journal import OPERATIONS, journal_event
from .ema200_utbot_rsi import (
    EMA200_CONSECUTIVE_LOSS_RESET_STATE_KEY,
    EMA200_DAILY_LOSS_RESET_STATE_KEY,
    EMA200_KST,
    EMA200_UTBOT_RSI_STRATEGY,
)

EMA200_MORNING_RESET_STATE_KEY = "ema200_morning_entry_state_reset"
EMA200_MORNING_RESET_CUTOFF_HOUR_KST = 12


def _utc_now(now=None):
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _kst_date(now=None):
    return _utc_now(now).astimezone(EMA200_KST).date().isoformat()


def ema200_morning_reset_availability(store, now=None):
    """Return (allowed, reason) for the once-per-day morning reset."""
    local_now = _utc_now(now).astimezone(EMA200_KST)
    if local_now.hour >= EMA200_MORNING_RESET_CUTOFF_HOUR_KST:
        return False, (
            "오전(한국시간 00:00~11:59)에만 사용할 수 있습니다. "
            f"현재 {local_now:%H:%M}"
        )
    try:
        previous = store.get_runtime_state(EMA200_MORNING_RESET_STATE_KEY)
    except Exception as exc:
        return False, f"초기화 기록 확인 실패: {type(exc).__name__}: {exc}"
    if isinstance(previous, dict) and previous.get("kst_date") == _kst_date(now):
        return False, (
            "오늘 이미 사용했습니다 "
            f"({_format_kst(previous.get('reset_at'))})"
        )
    return True, "사용 가능"


def perform_ema200_morning_entry_reset(db, store, *, now=None, reason="telegram"):
    """Reset today's entry count, loss streak and daily-loss baseline.

    Raises ``ValueError`` when the reset is outside the morning window or was
    already used today, so a caller can never apply it twice by accident.
    """
    allowed, why = ema200_morning_reset_availability(store, now)
    if not allowed:
        raise ValueError(why)
    reset_at = _utc_now(now).isoformat()
    today = _kst_date(now)
    raw_streak = int(db.get_consecutive_strategy_losses(EMA200_UTBOT_RSI_STRATEGY))
    trade_count, daily_pnl = db.get_daily_stats()
    entries_before = int(db.get_daily_automatic_entry_count())

    store.set_runtime_state(
        EMA200_CONSECUTIVE_LOSS_RESET_STATE_KEY,
        {
            "reset_at": reset_at,
            "raw_consecutive_losses_at_reset": raw_streak,
            "reason": f"morning_entry_reset:{reason}",
        },
    )
    store.set_runtime_state(
        EMA200_DAILY_LOSS_RESET_STATE_KEY,
        {
            "date": today,
            "baseline_realized_pnl": float(daily_pnl or 0.0),
            "trade_count_at_reset": int(trade_count or 0),
            "reset_at": reset_at,
            "reason": f"morning_entry_reset:{reason}",
        },
    )
    store.delete_runtime_state(DAILY_LOSS_ENTRY_LOCK_KEY)
    payload = {
        "kst_date": today,
        "reset_at": reset_at,
        "automatic_entries_before": entries_before,
        "consecutive_losses_before": raw_streak,
        "daily_realized_pnl_before": float(daily_pnl or 0.0),
        "reason": str(reason or "telegram"),
    }
    store.set_runtime_state(EMA200_MORNING_RESET_STATE_KEY, payload)
    journal_event(
        OPERATIONS, 'morning_entry_reset', **payload,
        code_ref='ema200_session.py:perform_ema200_morning_entry_reset',
    )
    return payload


def ema200_entry_count_since(store, now=None):
    """Return today's morning-reset timestamp for the daily entry count."""
    if store is None:
        return None
    try:
        payload = store.get_runtime_state(EMA200_MORNING_RESET_STATE_KEY)
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("kst_date") != _kst_date(now):
        return None
    return payload.get("reset_at") or None


def _format_kst(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return "-"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(EMA200_KST).strftime("%m-%d %H:%M")


def _summary_lines(title, summary):
    closed = int(summary.get("closed") or 0)
    wins = int(summary.get("wins") or 0)
    losses = int(summary.get("losses") or 0)
    win_rate = f"{wins / closed * 100:.0f}%" if closed else "-"
    return [
        title,
        f"  진입 {int(summary.get('entries') or 0)}회 / 청산 {closed}회 "
        f"(보유 {int(summary.get('open') or 0)})",
        f"  승 {wins} · 패 {losses} · 본전 {int(summary.get('flat') or 0)} "
        f"(승률 {win_rate})",
        f"  실현손익 {float(summary.get('pnl_usdt') or 0.0):+.4f} USDT",
    ]


def build_ema200_session_status_text(
    *,
    since_reset,
    today,
    reset_payload,
    availability,
    loss_streak,
    next_margin_percent,
    stop_required,
    equity,
    small_account_threshold,
    daily_limit_text,
    positions,
):
    lines = ["📊 EMA200 + UT + RSI 소액계좌 상태", ""]
    if isinstance(reset_payload, dict):
        lines += _summary_lines(
            f"🌅 초기화 이후 ({_format_kst(reset_payload.get('reset_at'))}~)",
            since_reset,
        )
    else:
        lines.append("🌅 초기화 기록 없음")
    lines.append("")
    lines += _summary_lines("📅 오늘 (한국시간)", today)
    lines.append("")
    small = equity is not None and 0 < equity <= small_account_threshold
    lines.append(
        f"💰 Equity {equity:.2f} USDT ({'소액계좌' if small else '위험예산 모드'})"
        if equity is not None
        else "💰 Equity 확인 불가"
    )
    lines.append(
        f"📉 연속손실 {int(loss_streak)}회 → 다음 진입 증거금 "
        f"{float(next_margin_percent):.0f}% / 5x / "
        f"{'비상 SL 적용' if stop_required else '첫 단계(거래소 SL 없음)'}"
    )
    lines.append(f"🛑 {daily_limit_text}")
    if positions:
        for item in positions:
            lines.append(
                f"📌 보유: {item['symbol']} {item['side']} {item['qty']} "
                f"(미실현 {item['upnl']:+.4f} USDT)"
            )
    else:
        lines.append("📌 보유 포지션 없음")
    lines.append("")
    lines.append(
        "🌅 오전 진입초기화: "
        + ("사용 가능" if availability[0] else availability[1])
    )
    return "\n".join(lines)


__all__ = (
    "EMA200_MORNING_RESET_STATE_KEY",
    "EMA200_MORNING_RESET_CUTOFF_HOUR_KST",
    "build_ema200_session_status_text",
    "ema200_entry_count_since",
    "ema200_morning_reset_availability",
    "perform_ema200_morning_entry_reset",
)
