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
                [
                    InlineKeyboardButton("🎯 코인 선택", callback_data="uce:choose"),
                    InlineKeyboardButton("📊 실포지션 상태", callback_data="uce:position"),
                ],
                [InlineKeyboardButton("상태 새로고침", callback_data="uce:status")],
            ]
        )

    @staticmethod
    def _build_user_custom_direction_keyboard(symbol):
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📈 LONG", callback_data="uce:side:long"),
                    InlineKeyboardButton("📉 SHORT", callback_data="uce:side:short"),
                ],
                [
                    InlineKeyboardButton("다른 코인 입력", callback_data="uce:choose"),
                    InlineKeyboardButton("취소", callback_data="uce:cancel_input"),
                ],
            ]
        )

    @staticmethod
    def _build_user_custom_result_keyboard():
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📊 실포지션 상태", callback_data="uce:position"),
                    InlineKeyboardButton("🎯 새 진입 설정", callback_data="uce:choose"),
                ],
                [InlineKeyboardButton("커스텀 메뉴", callback_data="uce:status")],
            ]
        )

    @staticmethod
    def _clear_user_custom_entry_context(context):
        user_data = getattr(context, "user_data", None)
        if user_data is None:
            return
        for key in (
            "user_custom_entry_pending",
            "user_custom_entry_waiting_symbol",
            "user_custom_entry_symbol",
        ):
            user_data.pop(key, None)

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
            "버튼 사용 순서:",
            "모드 ON → 코인 선택 → 코인명 입력 → LONG/SHORT → 주문 확인",
            "실포지션 상태는 거래소를 직접 조회합니다.",
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
        if status == "USER_CUSTOM_POSITION_CONFIRMED":
            plan = result.get("plan")
            position = result.get("confirmed_position") or {}
            return "\n".join(
                [
                    "✅ 사용자 커스텀 실포지션 확인 완료",
                    f"Symbol: {plan.symbol}",
                    f"Direction: {str(plan.side).upper()}",
                    f"Entry: {float(position.get('entryPrice') or plan.entry_price):.10g}",
                    f"Qty: {abs(float(position.get('contracts') or plan.qty)):.10g}",
                    f"SL: {float(plan.initial_sl_price):.10g}",
                    f"TP 보호주문: {len(result.get('tp_orders') or [])}/{len(plan.tp_orders or [])}",
                ]
            )
        return (
            "⛔ 사용자 커스텀 진입 미완료\n"
            f"status={status}\n"
            f"reason={result.get('error') or result.get('reason') or '-'}"
        )

    @staticmethod
    def _format_user_custom_position_status(result):
        if not isinstance(result, dict) or not result.get("fetch_ok"):
            return (
                "⛔ 거래소 실포지션 조회 실패\n"
                f"reason={(result or {}).get('error') or 'unknown'}"
            )
        positions = list(result.get("positions") or [])
        lines = ["📊 거래소 실포지션 상태"]
        if not positions:
            lines.append("⚪ 현재 열린 포지션 없음")
            return "\n".join(lines)
        for position in positions:
            symbol = str(position.get("symbol") or "unknown")
            side = str(position.get("side") or "unknown").upper()
            qty = abs(float(position.get("contracts") or 0.0))
            entry = float(position.get("entryPrice") or 0.0)
            mark = float(position.get("markPrice") or 0.0)
            pnl = float(position.get("unrealizedPnl") or 0.0)
            lines.extend(
                [
                    f"✅ {symbol} {side}",
                    f"수량: {qty:.10g}",
                    f"진입가: {entry:.10g} | 현재가: {mark:.10g}",
                    f"미실현손익: {pnl:+.4f} USDT",
                ]
            )
        return "\n".join(lines)

    async def _handle_user_custom_symbol_input(self, update, context, raw_text):
        """Consume a symbol after the owner presses the custom-entry choose button."""

        user_data = getattr(context, "user_data", None)
        message = getattr(update, "message", None)
        if user_data is None or message is None:
            return False
        if not user_data.pop("user_custom_entry_waiting_symbol", False):
            return False

        symbol = str(raw_text or "").strip().upper()
        if not re.fullmatch(
            r"[A-Z0-9]{2,24}(?:[/_-][A-Z0-9]{2,12})?(?::[A-Z0-9]{2,12})?",
            symbol,
        ):
            user_data["user_custom_entry_waiting_symbol"] = True
            await message.reply_text(
                "⛔ 코인명을 인식하지 못했습니다. 예: KORUUSDT, BTCUSDT, QQQUSDT"
            )
            return True
        if not self._is_user_custom_entry_enabled():
            await message.reply_text(
                self._format_user_custom_entry_status(
                    "먼저 사용자 커스텀 모드를 ON으로 켜세요."
                ),
                reply_markup=self._build_user_custom_entry_keyboard(),
            )
            return True
        try:
            resolved = await self._user_custom_entry_engine().resolve_user_custom_entry_symbol(
                symbol
            )
        except Exception as exc:
            user_data["user_custom_entry_waiting_symbol"] = True
            await message.reply_text(
                "⛔ 거래 가능한 코인으로 확인되지 않았습니다.\n"
                f"{type(exc).__name__}: {exc}\n"
                "코인명을 다시 입력하세요."
            )
            return True

        user_data["user_custom_entry_symbol"] = resolved
        user_data.pop("user_custom_entry_pending", None)
        await message.reply_text(
            f"🎯 선택 코인: {resolved}\n진입 방향을 선택하세요.",
            reply_markup=self._build_user_custom_direction_keyboard(resolved),
        )
        return True

    def _register_user_custom_entry_handlers(self, owner_only, text_filter):
        async def _reply_menu(message, notice=None):
            await message.reply_text(
                self._format_user_custom_entry_status(notice),
                reply_markup=self._build_user_custom_entry_keyboard(),
            )

        async def _execute(message, context, parsed):
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
            context.user_data["user_custom_entry_symbol"] = parsed["symbol"]
            await message.reply_text(
                self._format_user_custom_execution_result(result),
                reply_markup=self._build_user_custom_result_keyboard(),
            )

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
            context.user_data["user_custom_entry_symbol"] = prepared["symbol"]
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
                    ],
                    [
                        InlineKeyboardButton(
                            "방향 다시 선택",
                            callback_data="uce:direction",
                        )
                    ],
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
                await _execute(message, context, parsed)
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
                self._clear_user_custom_entry_context(context)
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
                self._clear_user_custom_entry_context(context)
                await query.edit_message_text(
                    self._format_user_custom_entry_status(
                        "커스텀 모드 OFF: 자동전략 신규 진입을 다시 허용했습니다."
                    ),
                    reply_markup=self._build_user_custom_entry_keyboard(),
                )
                return
            if action == "choose":
                if not self._is_user_custom_entry_enabled():
                    await query.edit_message_text(
                        self._format_user_custom_entry_status(
                            "먼저 사용자 커스텀 모드를 ON으로 켜세요."
                        ),
                        reply_markup=self._build_user_custom_entry_keyboard(),
                    )
                    return
                context.user_data.pop("user_custom_entry_pending", None)
                context.user_data.pop("user_custom_entry_symbol", None)
                context.user_data["user_custom_entry_waiting_symbol"] = True
                await query.edit_message_text(
                    "🎯 진입할 코인 이름만 입력하세요.\n"
                    "예: KORUUSDT, BTCUSDT, QQQUSDT",
                    reply_markup=InlineKeyboardMarkup(
                        [[
                            InlineKeyboardButton(
                                "취소",
                                callback_data="uce:cancel_input",
                            )
                        ]]
                    ),
                )
                return
            if action == "cancel_input":
                self._clear_user_custom_entry_context(context)
                await query.edit_message_text(
                    self._format_user_custom_entry_status(
                        "코인 선택을 취소했습니다."
                    ),
                    reply_markup=self._build_user_custom_entry_keyboard(),
                )
                return
            if action == "position":
                symbol = context.user_data.get("user_custom_entry_symbol")
                result = await self._user_custom_entry_engine().get_user_custom_position_status(
                    symbol
                )
                await query.edit_message_text(
                    self._format_user_custom_position_status(result),
                    reply_markup=self._build_user_custom_result_keyboard(),
                )
                return
            if action == "direction":
                symbol = context.user_data.get("user_custom_entry_symbol")
                if not symbol:
                    context.user_data["user_custom_entry_waiting_symbol"] = True
                    await query.edit_message_text(
                        "🎯 코인 이름을 먼저 입력하세요.",
                        reply_markup=InlineKeyboardMarkup(
                            [[InlineKeyboardButton("취소", callback_data="uce:cancel_input")]]
                        ),
                    )
                    return
                context.user_data.pop("user_custom_entry_pending", None)
                await query.edit_message_text(
                    f"🎯 선택 코인: {symbol}\n진입 방향을 선택하세요.",
                    reply_markup=self._build_user_custom_direction_keyboard(symbol),
                )
                return
            if action == "side":
                symbol = context.user_data.get("user_custom_entry_symbol")
                side = parts[2] if len(parts) > 2 else ""
                if not symbol or side not in {"long", "short"}:
                    await query.edit_message_text(
                        "⛔ 코인 또는 방향 선택이 만료됐습니다.",
                        reply_markup=self._build_user_custom_entry_keyboard(),
                    )
                    return
                await _preview(
                    query.message,
                    context,
                    {
                        "symbol": symbol,
                        "side": side,
                        "immediate": False,
                        "order_type": "market",
                    },
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
                await query.edit_message_text(
                    "사용자 커스텀 진입 요청을 취소했습니다.",
                    reply_markup=self._build_user_custom_result_keyboard(),
                )
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
                    self._format_user_custom_execution_result(result),
                    reply_markup=self._build_user_custom_result_keyboard(),
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
