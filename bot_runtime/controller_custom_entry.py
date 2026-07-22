"""Telegram controls for user-directed Binance Futures entries."""

from __future__ import annotations

import re
import secrets

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters


USER_CUSTOM_ENTRY_TEXT_PATTERN = (
    r"(?i)^\s*[A-Z0-9]{2,24}(?:[/_-][A-Z0-9]{2,12})?(?::[A-Z0-9]{2,12})?\s+"
    r"(?:LONG|SHORT|롱|숏)\s+(?:시장가(?:로)?\s+)?(?:바로\s+|즉시\s+)?진입\s*$"
)


def parse_user_custom_entry_text(text):
    """Parse an intentionally narrow direct-entry command."""

    raw = str(text or "").strip()
    if not raw:
        return None

    command_match = re.match(
        r"(?i)^/(?:customentry|custom)(?:@[A-Za-z0-9_]+)?(?:\s+|$)",
        raw,
    )
    from_command = bool(command_match)
    if command_match:
        raw = raw[command_match.end() :].strip()
    if not raw:
        return None

    tokens = raw.split()
    if len(tokens) < 2:
        return None
    symbol = tokens[0].strip().upper()
    if not re.fullmatch(
        r"[A-Z0-9]{2,24}(?:[/_-][A-Z0-9]{2,12})?(?::[A-Z0-9]{2,12})?",
        symbol,
    ):
        return None

    side_token = tokens[1].strip().lower()
    side_map = {"long": "long", "롱": "long", "short": "short", "숏": "short"}
    side = side_map.get(side_token)
    if side is None:
        return None

    suffix = " ".join(tokens[2:]).strip().lower()
    if not from_command and not re.search(r"진입\s*$", suffix):
        return None
    if suffix and not re.fullmatch(
        r"(?:(?:시장가|시장가로)\s*)?(?:(?:바로|즉시|now)\s*)?(?:진입)?",
        suffix,
    ):
        return None

    immediate = bool(re.search(r"(?:바로|즉시|now)", suffix))
    return {
        "symbol": symbol,
        "side": side,
        "immediate": immediate,
        "order_type": "market",
    }


class ControllerCustomEntryMixin:
    def _user_custom_entry_engine(self):
        engine = (getattr(self, "engines", {}) or {}).get("signal")
        if engine is None or not hasattr(engine, "prepare_user_custom_entry"):
            raise RuntimeError("SignalEngine user custom entry runtime is unavailable")
        return engine

    def _is_user_custom_entry_enabled(self):
        signal_cfg = self.cfg.get("signal_engine", {}) or {}
        custom_cfg = signal_cfg.get("user_custom_entry", {}) or {}
        return bool(custom_cfg.get("enabled", False))

    async def _set_user_custom_entry_mode(self, enabled):
        await self.cfg.update_value(
            ["signal_engine", "user_custom_entry", "enabled"],
            bool(enabled),
        )

    def _build_user_custom_entry_keyboard(self):
        enabled = self._is_user_custom_entry_enabled()
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ 모드 ON" if enabled else "모드 ON",
                        callback_data="uce:on",
                    ),
                    InlineKeyboardButton(
                        "모드 OFF" if enabled else "✅ 모드 OFF",
                        callback_data="uce:off",
                    ),
                ],
                [InlineKeyboardButton("상태 새로고침", callback_data="uce:status")],
            ]
        )

    def _format_user_custom_entry_status(self, notice=None):
        enabled = self._is_user_custom_entry_enabled()
        lines = [
            "🧭 사용자 커스텀 모드",
            f"상태: {'ON' if enabled else 'OFF'}",
            (
                "자동전략 신규 진입: 일시 차단"
                if enabled
                else "자동전략 신규 진입: 정상"
            ),
            "사용자 진입 횟수: 제한 없음",
            "진입 방식: 시장가",
            "유지 보호: 일일손실·단일 포지션·유동성·실잔고 리스크·청산가·SL·TP1·TP2",
            "",
            "사용 예:",
            "KORUUSDT 숏 시장가로 진입  → 주문 전 확인",
            "KORUUSDT 숏 시장가로 바로 진입  → 즉시 안전검사 후 주문",
            "/customentry KORUUSDT short now",
        ]
        if notice:
            lines.extend(["", str(notice)])
        return "\n".join(lines)

    @staticmethod
    def _format_user_custom_plan_preview(prepared):
        plan = prepared["plan"]
        risk_usdt = float(plan.qty) * abs(
            float(prepared["price"]) - float(plan.initial_sl_price)
        )
        tp_lines = [
            f"{tp.tp_label or tp.tp_name}: {float(tp.price):.10g} / qty {float(tp.qty):.10g}"
            for tp in list(plan.tp_orders or [])
        ]
        spread = prepared.get("spread_pct")
        spread_text = "ticker 미제공(L2 통과)" if spread is None else f"{float(spread):.4f}%"
        return "\n".join(
            [
                "📋 사용자 커스텀 진입 계획",
                f"Symbol: {prepared['symbol']}",
                f"Direction: {prepared['side'].upper()}",
                f"Entry: MARKET (reference {float(prepared['price']):.10g})",
                f"Qty: {float(plan.qty):.10g}",
                f"예상 증거금: {float(plan.qty) * float(prepared['price']) / max(1.0, float(prepared['cfg'].get('leverage', 1) or 1)):.4f} USDT",
                f"손절 예산: 약 {risk_usdt:.4f} USDT",
                f"SL: {float(plan.initial_sl_price):.10g}",
                *tp_lines,
                f"Spread: {spread_text}",
                "거래횟수 한도: 미적용",
                "",
                "확인 시 가격·잔고·포지션·유동성을 다시 검사한 뒤 주문합니다.",
            ]
        )

    @staticmethod
    def _format_user_custom_execution_result(result):
        status = str((result or {}).get("status") or "UNKNOWN")
        if status == "LIVE_ORDER_PLAN_EXECUTED":
            plan = result.get("plan")
            return "\n".join(
                [
                    "✅ 사용자 커스텀 진입 완료",
                    f"Symbol: {plan.symbol}",
                    f"Direction: {str(plan.side).upper()}",
                    f"Entry: {float(plan.entry_price):.10g}",
                    f"Qty: {float(plan.qty):.10g}",
                    f"SL: {float(plan.initial_sl_price):.10g}",
                    f"TP 보호주문: {len(result.get('tp_orders') or [])}/{len(plan.tp_orders or [])}",
                ]
            )
        return (
            "⛔ 사용자 커스텀 진입 미완료\n"
            f"status={status}\n"
            f"reason={result.get('error') or result.get('reason') or '-'}"
        )

    def _register_user_custom_entry_handlers(self, owner_only, text_filter):
        async def _reply_menu(message, notice=None):
            await message.reply_text(
                self._format_user_custom_entry_status(notice),
                reply_markup=self._build_user_custom_entry_keyboard(),
            )

        async def _execute(message, parsed):
            engine = self._user_custom_entry_engine()
            try:
                result = await engine.execute_user_custom_entry(
                    parsed["symbol"],
                    parsed["side"],
                )
            except Exception as exc:
                await message.reply_text(
                    "⛔ 사용자 커스텀 진입 차단\n"
                    f"{type(exc).__name__}: {exc}"
                )
                return
            await message.reply_text(self._format_user_custom_execution_result(result))

        async def _preview(message, context, parsed):
            engine = self._user_custom_entry_engine()
            try:
                prepared = await engine.prepare_user_custom_entry(
                    parsed["symbol"],
                    parsed["side"],
                )
            except Exception as exc:
                await message.reply_text(
                    "⛔ 사용자 커스텀 진입 계획 차단\n"
                    f"{type(exc).__name__}: {exc}"
                )
                return
            request_id = secrets.token_hex(4)
            context.user_data["user_custom_entry_pending"] = {
                "request_id": request_id,
                "symbol": prepared["symbol"],
                "side": prepared["side"],
            }
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "주문 확인",
                            callback_data=f"uce:confirm:{request_id}",
                        ),
                        InlineKeyboardButton(
                            "취소",
                            callback_data=f"uce:cancel:{request_id}",
                        ),
                    ]
                ]
            )
            await message.reply_text(
                self._format_user_custom_plan_preview(prepared),
                reply_markup=keyboard,
            )

        async def _handle_parsed(message, context, parsed):
            if not self._is_user_custom_entry_enabled():
                await _reply_menu(message, "먼저 사용자 커스텀 모드를 ON으로 켜세요.")
                return
            if parsed["immediate"]:
                await _execute(message, parsed)
            else:
                await _preview(message, context, parsed)

        async def customentry_cmd(update, context):
            args = list(getattr(context, "args", []) or [])
            action = str(args[0] if args else "").strip().lower()
            if not args or action in {"status", "menu"}:
                await _reply_menu(update.message)
                return
            if action in {"on", "start", "enable"}:
                await self._set_user_custom_entry_mode(True)
                await _reply_menu(
                    update.message,
                    "커스텀 모드가 켜졌습니다. 기존 포지션 관리는 계속되며 자동전략 신규 진입은 멈춥니다.",
                )
                return
            if action in {"off", "stop", "disable"}:
                await self._set_user_custom_entry_mode(False)
                context.user_data.pop("user_custom_entry_pending", None)
                await _reply_menu(
                    update.message,
                    "커스텀 모드가 꺼졌습니다. 자동전략 신규 진입이 다시 허용됩니다.",
                )
                return
            parsed = parse_user_custom_entry_text(
                "/customentry " + " ".join(args)
            )
            if parsed is None:
                await _reply_menu(update.message, "명령 형식을 확인하세요.")
                return
            await _handle_parsed(update.message, context, parsed)

        async def customentry_text(update, context):
            parsed = parse_user_custom_entry_text(update.message.text)
            if parsed is not None:
                await _handle_parsed(update.message, context, parsed)

        async def customentry_callback(update, context):
            query = update.callback_query
            if query is None:
                return
            await query.answer()
            data = str(query.data or "")
            parts = data.split(":")
            action = parts[1] if len(parts) > 1 else "status"
            if action == "on":
                await self._set_user_custom_entry_mode(True)
                await query.edit_message_text(
                    self._format_user_custom_entry_status(
                        "커스텀 모드 ON: 자동전략 신규 진입을 일시 차단했습니다."
                    ),
                    reply_markup=self._build_user_custom_entry_keyboard(),
                )
                return
            if action == "off":
                await self._set_user_custom_entry_mode(False)
                context.user_data.pop("user_custom_entry_pending", None)
                await query.edit_message_text(
                    self._format_user_custom_entry_status(
                        "커스텀 모드 OFF: 자동전략 신규 진입을 다시 허용했습니다."
                    ),
                    reply_markup=self._build_user_custom_entry_keyboard(),
                )
                return
            if action == "status":
                await query.edit_message_text(
                    self._format_user_custom_entry_status(),
                    reply_markup=self._build_user_custom_entry_keyboard(),
                )
                return

            pending = context.user_data.get("user_custom_entry_pending") or {}
            request_id = parts[2] if len(parts) > 2 else ""
            if request_id != pending.get("request_id"):
                await query.edit_message_text("⛔ 만료되었거나 다른 사용자 진입 요청입니다.")
                return
            if action == "cancel":
                context.user_data.pop("user_custom_entry_pending", None)
                await query.edit_message_text("사용자 커스텀 진입 요청을 취소했습니다.")
                return
            if action != "confirm":
                return

            context.user_data.pop("user_custom_entry_pending", None)
            try:
                result = await self._user_custom_entry_engine().execute_user_custom_entry(
                    pending["symbol"],
                    pending["side"],
                )
                await query.edit_message_text(
                    self._format_user_custom_execution_result(result)
                )
            except Exception as exc:
                await query.edit_message_text(
                    "⛔ 사용자 커스텀 진입 차단\n"
                    f"{type(exc).__name__}: {exc}"
                )

        self.tg_app.add_handler(
            CommandHandler("customentry", owner_only(customentry_cmd))
        )
        self.tg_app.add_handler(CommandHandler("custom", owner_only(customentry_cmd)))
        self.tg_app.add_handler(
            CallbackQueryHandler(
                owner_only(customentry_callback),
                pattern=r"^uce:",
            )
        )
        self.tg_app.add_handler(
            MessageHandler(
                text_filter & filters.Regex(USER_CUSTOM_ENTRY_TEXT_PATTERN),
                owner_only(customentry_text),
            )
        )


__all__ = (
    "ControllerCustomEntryMixin",
    "USER_CUSTOM_ENTRY_TEXT_PATTERN",
    "parse_user_custom_entry_text",
)
