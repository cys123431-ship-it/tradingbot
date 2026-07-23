"""Telegram controls for automatic-strategy daily limits and scan scope."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler


AUTOMATIC_DAILY_TRADE_LIMIT_BASE = 5
AUTOMATIC_DAILY_TRADE_LIMIT_EXTENDED = 10
AUTOMATIC_SCAN_SCOPE_ALL = "all_allowed"
AUTOMATIC_SCAN_SCOPE_TRADIFI = "tradfi_only"
AUTOMATIC_SCAN_SCOPE_CRYPTO = "crypto_only"
AUTOMATIC_SCAN_SCOPES = {
    AUTOMATIC_SCAN_SCOPE_ALL,
    AUTOMATIC_SCAN_SCOPE_TRADIFI,
    AUTOMATIC_SCAN_SCOPE_CRYPTO,
}
AUTOMATIC_SCAN_SCOPE_LABELS = {
    AUTOMATIC_SCAN_SCOPE_ALL: "전체 허용",
    AUTOMATIC_SCAN_SCOPE_TRADIFI: "TradFi ONLY",
    AUTOMATIC_SCAN_SCOPE_CRYPTO: "순수 코인 ONLY",
}


class ControllerAutomaticTradingControlsMixin:
    """Persist and expose controls that apply only to automatic strategies."""

    @staticmethod
    def _automatic_trading_today_key(now=None):
        value = now if now is not None else datetime.now(timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d")

    def _automatic_trading_controls_cfg(self):
        signal_cfg = self.cfg.get("signal_engine", {}) or {}
        controls = signal_cfg.get("automatic_trading_controls", {}) or {}
        return controls if isinstance(controls, dict) else {}

    def get_automatic_scan_scope(self):
        signal_cfg = self.cfg.get("signal_engine", {}) or {}
        coin_cfg = signal_cfg.get("coin_selector", {}) or {}
        scope = str(coin_cfg.get("scan_scope", AUTOMATIC_SCAN_SCOPE_ALL) or "").strip().lower()
        return scope if scope in AUTOMATIC_SCAN_SCOPES else AUTOMATIC_SCAN_SCOPE_ALL

    def get_effective_automatic_daily_trade_limit(self, *, now=None):
        controls = self._automatic_trading_controls_cfg()
        extension_date = str(
            controls.get("daily_trade_limit_extension_utc_date", "") or ""
        ).strip()
        if extension_date == self._automatic_trading_today_key(now):
            return AUTOMATIC_DAILY_TRADE_LIMIT_EXTENDED
        return AUTOMATIC_DAILY_TRADE_LIMIT_BASE

    def get_automatic_trading_control_status(self, *, now=None):
        today = self._automatic_trading_today_key(now)
        limit = self.get_effective_automatic_daily_trade_limit(now=now)
        controls = self._automatic_trading_controls_cfg()
        extension_used = str(
            controls.get("daily_trade_limit_extension_utc_date", "") or ""
        ).strip() == today
        try:
            if hasattr(self.db, "get_daily_automatic_entry_count"):
                entries = int(self.db.get_daily_automatic_entry_count())
            else:
                entries = int(self.db.get_daily_entry_count())
        except Exception:
            entries = 0
        return {
            "utc_date": today,
            "base_limit": AUTOMATIC_DAILY_TRADE_LIMIT_BASE,
            "extended_limit": AUTOMATIC_DAILY_TRADE_LIMIT_EXTENDED,
            "effective_limit": limit,
            "entries": max(0, entries),
            "remaining": max(0, limit - max(0, entries)),
            "extension_used": extension_used,
            "scan_scope": self.get_automatic_scan_scope(),
        }

    async def extend_automatic_daily_trade_limit_for_today(self, *, now=None):
        lock = getattr(self, "_automatic_daily_limit_extension_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._automatic_daily_limit_extension_lock = lock
        async with lock:
            today = self._automatic_trading_today_key(now)
            controls = self._automatic_trading_controls_cfg()
            previous = str(
                controls.get("daily_trade_limit_extension_utc_date", "") or ""
            ).strip()
            if previous == today:
                return False, self.get_automatic_trading_control_status(now=now)
            await self._update_config_value(
                [
                    "signal_engine",
                    "automatic_trading_controls",
                    "daily_trade_limit_extension_utc_date",
                ],
                today,
            )
            return True, self.get_automatic_trading_control_status(now=now)

    async def set_automatic_scan_scope(self, scope):
        normalized = str(scope or "").strip().lower()
        if normalized not in AUTOMATIC_SCAN_SCOPES:
            raise ValueError(f"unsupported automatic scan scope: {scope}")
        await self._update_config_value(
            ["signal_engine", "coin_selector", "scan_scope"],
            normalized,
        )
        # Keep the legacy flag coherent for older status/reporting paths. The
        # scope itself remains the authoritative automatic-strategy control.
        await self._update_config_value(
            ["signal_engine", "coin_selector", "include_tradifi_universe"],
            normalized != AUTOMATIC_SCAN_SCOPE_CRYPTO,
        )
        engine = (getattr(self, "engines", {}) or {}).get("signal")
        if engine is not None:
            engine.coin_selector_last_result = {}
            engine.coin_selector_symbol_scores = {}
            engine.coin_selector_last_run_ts = 0.0
            engine.coin_selector_analysis_cursor = 0
            engine.coin_selector_strategy_cursor = 0
        return normalized

    def _format_automatic_trading_controls_status(self, notice=None):
        status = self.get_automatic_trading_control_status()
        scope = status["scan_scope"]
        lines = [
            "⚙️ 자동매매 운영 설정",
            "",
            f"UTC 기준일: {status['utc_date']}",
            f"오늘 자동매매 진입: {status['entries']}회",
            f"현재 일일 한도: {status['effective_limit']}회",
            f"남은 자동매매 진입: {status['remaining']}회",
            (
                "오늘 10회 확장: 사용 완료"
                if status["extension_used"]
                else "오늘 10회 확장: 사용 가능"
            ),
            "",
            f"자동 스캔 범위: {AUTOMATIC_SCAN_SCOPE_LABELS[scope]}",
            "원자재 TradFi: 항상 제외",
            "적용 대상: 자동매매전략만",
        ]
        if scope == AUTOMATIC_SCAN_SCOPE_TRADIFI:
            lines.append("허용: 주식·ETF·주가지수 TradFi")
        elif scope == AUTOMATIC_SCAN_SCOPE_CRYPTO:
            lines.append("허용: TradFi가 아닌 순수 암호화폐 선물")
        else:
            lines.append("허용: 순수 코인 + 주식·ETF·주가지수 TradFi")
        if notice:
            lines.extend(["", str(notice)])
        return "\n".join(lines)

    def _build_automatic_trading_controls_keyboard(self):
        status = self.get_automatic_trading_control_status()
        scope = status["scan_scope"]
        limit_label = (
            "✅ 오늘 10회 적용됨"
            if status["extension_used"]
            else "오늘 한도 10회로 확장"
        )

        def scope_label(value, label):
            return f"✅ {label}" if scope == value else label

        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(limit_label, callback_data="atc:limit:extend")],
                [
                    InlineKeyboardButton(
                        scope_label(AUTOMATIC_SCAN_SCOPE_TRADIFI, "TradFi ONLY"),
                        callback_data="atc:scope:tradfi_only",
                    ),
                    InlineKeyboardButton(
                        scope_label(AUTOMATIC_SCAN_SCOPE_CRYPTO, "순수 코인 ONLY"),
                        callback_data="atc:scope:crypto_only",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        scope_label(AUTOMATIC_SCAN_SCOPE_ALL, "전체 허용"),
                        callback_data="atc:scope:all_allowed",
                    )
                ],
                [InlineKeyboardButton("상태 새로고침", callback_data="atc:status")],
            ]
        )

    async def _send_automatic_trading_controls(self, target, notice=None, *, edit=False):
        text = self._format_automatic_trading_controls_status(notice)
        keyboard = self._build_automatic_trading_controls_keyboard()
        if edit and hasattr(target, "edit_message_text"):
            try:
                await target.edit_message_text(text, reply_markup=keyboard)
                return
            except Exception as exc:
                if "message is not modified" in str(exc).lower():
                    return
        if hasattr(target, "reply_text"):
            await target.reply_text(text, reply_markup=keyboard)

    def _register_automatic_trading_control_handlers(self, owner_only):
        async def automatic_controls_cmd(update, context):
            if update and update.message:
                await self._send_automatic_trading_controls(update.message)

        async def automatic_controls_callback(update, context):
            query = getattr(update, "callback_query", None)
            if query is None:
                return
            try:
                await query.answer()
            except Exception:
                pass
            data = str(getattr(query, "data", "") or "")
            notice = None
            if data == "atc:limit:extend":
                changed, _ = await self.extend_automatic_daily_trade_limit_for_today()
                notice = (
                    "✅ 오늘 자동매매 한도를 10회로 확장했습니다."
                    if changed
                    else "⛔ 오늘은 이미 10회 확장을 사용했습니다."
                )
            elif data.startswith("atc:scope:"):
                scope = data.split(":", 2)[2]
                normalized = await self.set_automatic_scan_scope(scope)
                notice = (
                    "✅ 자동매매 스캔 범위를 "
                    f"{AUTOMATIC_SCAN_SCOPE_LABELS[normalized]}로 변경했습니다."
                )
            elif data != "atc:status":
                return
            await self._send_automatic_trading_controls(
                query,
                notice,
                edit=True,
            )

        self.tg_app.add_handler(
            CommandHandler("autotrade", owner_only(automatic_controls_cmd))
        )
        self.tg_app.add_handler(
            CommandHandler("autocontrol", owner_only(automatic_controls_cmd))
        )
        self.tg_app.add_handler(
            CallbackQueryHandler(
                owner_only(automatic_controls_callback),
                pattern=r"^atc:",
            )
        )


__all__ = (
    "AUTOMATIC_DAILY_TRADE_LIMIT_BASE",
    "AUTOMATIC_DAILY_TRADE_LIMIT_EXTENDED",
    "AUTOMATIC_SCAN_SCOPE_ALL",
    "AUTOMATIC_SCAN_SCOPE_TRADIFI",
    "AUTOMATIC_SCAN_SCOPE_CRYPTO",
    "AUTOMATIC_SCAN_SCOPES",
    "AUTOMATIC_SCAN_SCOPE_LABELS",
    "ControllerAutomaticTradingControlsMixin",
)
