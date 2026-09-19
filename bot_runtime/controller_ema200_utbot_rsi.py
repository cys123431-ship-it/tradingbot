"""Telegram controls for the EMA200 + UT Bot + RSI 2H strategy."""

from __future__ import annotations

import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
)

from .ema200_utbot_rsi import (
    EMA200_UTBOT_RSI_CONFIG_KEY,
    EMA200_UTBOT_RSI_DISPLAY_NAME,
    EMA200_UTBOT_RSI_STRATEGY,
    normalize_ema200_utbot_rsi_config,
)


class ControllerEMA200UTBotRSIMixin:
    def _ema200_utbot_rsi_config(self):
        section = self.get_active_trade_section()
        raw = (
            self.cfg.get(section, {})
            .get("strategy_params", {})
            .get(EMA200_UTBOT_RSI_CONFIG_KEY, {})
        )
        return normalize_ema200_utbot_rsi_config(raw)

    async def _update_ema200_utbot_rsi_value(self, key, value):
        section = self.get_active_trade_section()
        await self.cfg.update_value(
            [section, "strategy_params", EMA200_UTBOT_RSI_CONFIG_KEY, key],
            value,
        )

    def _build_ema200_utbot_rsi_keyboard(self):
        cfg = self._ema200_utbot_rsi_config()
        enabled = bool(cfg.get("enabled", True))
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ 이 전략 선택",
                    callback_data="e2h:activate",
                ),
                InlineKeyboardButton(
                    "⏸ 신규진입 OFF" if enabled else "▶ 신규진입 ON",
                    callback_data="e2h:entry_toggle",
                ),
            ],
            [
                InlineKeyboardButton("📊 상태", callback_data="e2h:status"),
                InlineKeyboardButton("📘 전략 설명", callback_data="e2h:guide"),
            ],
            [
                InlineKeyboardButton("위험 0.25%", callback_data="e2h:risk:0.25"),
                InlineKeyboardButton("위험 0.50%", callback_data="e2h:risk:0.50"),
                InlineKeyboardButton("위험 1.00%", callback_data="e2h:risk:1.00"),
            ],
            [
                InlineKeyboardButton("✍️ 위험 직접입력", callback_data="e2h:custom:risk"),
                InlineKeyboardButton("❓ 위험 설명", callback_data="e2h:help:risk"),
            ],
            [
                InlineKeyboardButton("비상탈출 3%", callback_data="e2h:emergency:3"),
                InlineKeyboardButton("비상탈출 5%", callback_data="e2h:emergency:5"),
                InlineKeyboardButton("비상탈출 8%", callback_data="e2h:emergency:8"),
            ],
            [
                InlineKeyboardButton("✍️ 비상탈출 직접입력", callback_data="e2h:custom:emergency"),
                InlineKeyboardButton("❓ 비상탈출 설명", callback_data="e2h:help:emergency"),
            ],
            [
                InlineKeyboardButton("레버리지 2x", callback_data="e2h:lev:2"),
                InlineKeyboardButton("3x", callback_data="e2h:lev:3"),
                InlineKeyboardButton("5x", callback_data="e2h:lev:5"),
            ],
            [
                InlineKeyboardButton("✍️ 레버리지 직접입력", callback_data="e2h:custom:leverage"),
                InlineKeyboardButton("❓ 레버리지 설명", callback_data="e2h:help:leverage"),
            ],
            [
                InlineKeyboardButton("일손실 1.5%", callback_data="e2h:daily:1.5"),
                InlineKeyboardButton("2%", callback_data="e2h:daily:2"),
                InlineKeyboardButton("3%", callback_data="e2h:daily:3"),
            ],
            [
                InlineKeyboardButton("✍️ 일손실 직접입력", callback_data="e2h:custom:daily"),
                InlineKeyboardButton("❓ 일손실 설명", callback_data="e2h:help:daily"),
            ],
            [
                InlineKeyboardButton("주손실 4%", callback_data="e2h:weekly:4"),
                InlineKeyboardButton("5%", callback_data="e2h:weekly:5"),
                InlineKeyboardButton("6%", callback_data="e2h:weekly:6"),
            ],
            [
                InlineKeyboardButton("✍️ 주손실 직접입력", callback_data="e2h:custom:weekly"),
                InlineKeyboardButton("❓ 주손실 설명", callback_data="e2h:help:weekly"),
            ],
        ])

    async def _ema200_utbot_rsi_status_text(self):
        cfg = self._ema200_utbot_rsi_config()
        section = self.get_active_trade_section()
        active = str(
            self.cfg.get(section, {})
            .get("strategy_params", {})
            .get("active_strategy", "")
            or ""
        ).lower() == EMA200_UTBOT_RSI_STRATEGY

        daily_count = weekly_count = 0
        daily_pnl = weekly_pnl = 0.0
        try:
            daily_count, daily_pnl = self.db.get_daily_stats()
            weekly_count, weekly_pnl = self.db.get_weekly_stats()
        except Exception:
            pass

        return (
            f"🎛 {EMA200_UTBOT_RSI_DISPLAY_NAME}\n\n"
            f"전략 선택: {'✅ ACTIVE' if active else '⬜ 미선택'}\n"
            f"신규 진입: {'ON' if cfg['enabled'] else 'OFF'}\n"
            "시간봉: 2시간 완료봉 고정\n"
            "추세: 종가 > EMA200=롱 허용 / 종가 < EMA200=숏 허용\n"
            f"RSI: {cfg['rsi_length']}기간, 50선 돌파\n\n"
            f"1회 위험예산: 계좌의 {cfg['risk_per_trade_percent']:.2f}%\n"
            f"레버리지: {cfg['leverage']}x\n"
            f"비상탈출: 진입가에서 불리하게 {cfg['emergency_exit_percent']:.2f}%\n"
            f"일일 손실한도: {cfg['daily_loss_limit_percent']:.2f}%\n"
            f"최근 7일 손실한도: {cfg['weekly_loss_limit_percent']:.2f}%\n\n"
            f"오늘 실현손익: {float(daily_pnl):+.4f} USDT / {daily_count}건\n"
            f"최근 7일 실현손익: {float(weekly_pnl):+.4f} USDT / {weekly_count}건\n\n"
            "중요: 정상 청산은 UT Bot 반대 신호입니다. "
            "비상탈출은 정상 청산을 대신하는 TP/SL 전략이 아니라 "
            "극단적인 불리한 움직임에서 계좌를 보호하는 최후 안전장치입니다."
        )

    @staticmethod
    def _ema200_utbot_rsi_help_text(kind):
        if kind == "risk":
            return (
                "📘 1회 위험예산이란?\n\n"
                "한 번의 거래가 비상탈출 가격까지 불리하게 움직였을 때 "
                "계좌에서 최대 얼마 정도를 잃도록 포지션 크기를 잡을지 정하는 값입니다.\n\n"
                "예시) 계좌 1,000 USDT, 위험 0.5%, 비상탈출 5%\n"
                "• 허용 손실예산 = 1,000 × 0.5% = 약 5 USDT\n"
                "• 가격이 5% 불리하게 움직일 때 약 5 USDT 손실이 되도록 "
                "포지션 명목금액을 약 100 USDT로 계산합니다.\n"
                "• 따라서 단순히 '레버리지 5배니까 크게 산다'가 아닙니다. "
                "먼저 손실예산을 정하고 수량을 역산합니다.\n\n"
                "권장 시작값은 0.50%입니다. 1%를 넘기면 연속 손실 시 "
                "계좌 감소가 빨라지므로 경고는 표시하지만, 허용 범위 안에서는 직접 설정할 수 있습니다.\n\n"
                "아래 '위험 직접입력'을 누른 뒤 숫자만 보내세요. 예: 0.5"
            )
        if kind == "emergency":
            return (
                "🛟 비상탈출이란?\n\n"
                "정상 청산 규칙은 그대로입니다.\n"
                "• LONG: 2시간봉 UT Bot Sell 신호에서 정상 청산\n"
                "• SHORT: 2시간봉 UT Bot Buy 신호에서 정상 청산\n\n"
                "비상탈출은 그 신호를 기다리기 위험할 정도의 급격한 역방향 움직임에 대비한 "
                "거래소 측 보호용 Stop입니다.\n\n"
                "예시) 100에 LONG 진입, 비상탈출 5%라면 약 95 부근이 최후 안전선입니다.\n"
                "100에 SHORT 진입이라면 약 105 부근이 최후 안전선입니다.\n\n"
                "이 값은 일반 손절 타이밍을 최적화하려는 것이 아니라 '안전벨트'입니다. "
                "그래서 완전히 OFF할 수 없고 거리만 조정할 수 있습니다.\n\n"
                "설정 변경은 새로 진입하는 포지션부터 적용합니다. 이미 보유 중인 포지션의 "
                "보호 주문을 텔레그램 설정 변경만으로 몰래 교체하지 않습니다.\n\n"
                "아래 '비상탈출 직접입력'을 누른 뒤 가격 변동률 숫자만 보내세요. 예: 5"
            )
        if kind == "leverage":
            return (
                "⚙️ 레버리지란?\n\n"
                "이 전략에서는 레버리지를 먼저 정해서 손실을 키우는 방식이 아닙니다. "
                "1회 위험예산과 비상탈출 거리를 먼저 계산하고, 그 포지션을 유지하는 데 "
                "필요한 증거금 크기에 레버리지가 영향을 줍니다.\n\n"
                "예시) 명목 포지션 100 USDT라면 5x에서 필요한 초기 증거금은 대략 20 USDT입니다.\n"
                "레버리지가 높을수록 청산가가 가까워질 수 있으므로 기존 청산가 안전검사가 "
                "비상탈출보다 청산 위험이 앞서는 설정을 차단하거나 더 안전한 값으로 제한합니다.\n\n"
                "초기 기본값은 5x이며 1~10x 범위에서 이 전략 전용으로 조정합니다."
            )
        if kind == "daily":
            return (
                "🗓 일일 손실한도란?\n\n"
                "오늘의 확정(실현) 손실이 계좌 기준 한도에 도달하면 그날 새 포지션 진입만 막습니다.\n"
                "이미 보유 중인 포지션은 이 한도 때문에 억지로 청산하지 않습니다. "
                "보유 포지션은 원래 UT 반대 신호 또는 비상탈출로 종료됩니다.\n\n"
                "예시) 계좌 1,000 USDT, 일일 한도 2%라면 오늘 실현손익이 약 -20 USDT 이하가 된 뒤 "
                "추가 신규진입을 멈춥니다.\n\n"
                "직접입력 예: 2"
            )
        if kind == "weekly":
            return (
                "📅 최근 7일 손실한도란?\n\n"
                "최근 7일의 확정(실현) 손실이 계좌 기준 한도에 도달하면 신규진입을 막습니다. "
                "일일 한도와 마찬가지로 이미 보유한 포지션을 강제청산하지 않습니다.\n\n"
                "예시) 계좌 1,000 USDT, 7일 한도 5%라면 최근 7일 실현손익이 약 -50 USDT 이하일 때 "
                "새 거래를 중지합니다.\n\n"
                "7일 한도는 일일 한도보다 작게 설정할 수 없습니다. 직접입력 예: 5"
            )
        return (
            "📘 전략 동작 순서\n\n"
            "LONG: 2시간 완료봉 종가가 EMA200 위 → UT Bot Buy 발생 → 그 LONG 상태가 유지되는 동안 "
            "RSI가 50을 아래에서 위로 돌파 → 진입. 이후 UT Bot Sell이 발생하면 정상 청산합니다.\n\n"
            "SHORT: 정확히 반대입니다. EMA200 아래 → UT Bot Sell → SHORT 상태 유지 중 RSI 50 하향돌파 "
            "→ 진입. 이후 UT Bot Buy에서 정상 청산합니다.\n\n"
            "RSI가 먼저 50을 통과한 뒤 나중에 UT 신호가 나온 경우는 인정하지 않습니다. "
            "완료된 2시간봉만 사용해 진행 중 봉의 흔들림으로 인한 가짜 돌파를 피합니다.\n\n"
            "리스크 기능은 전략의 정상 청산을 바꾸지 않습니다. 포지션 수량, 신규진입 손실한도, "
            "최후 비상탈출만 담당합니다."
        )

    async def _ema200_utbot_rsi_has_open_position(self):
        try:
            positions = await asyncio.to_thread(self.exchange.fetch_positions)
        except Exception as exc:
            return None, f"포지션 조회 실패: {type(exc).__name__}: {exc}"
        for pos in positions or []:
            try:
                if abs(float(pos.get("contracts", 0.0) or 0.0)) > 0:
                    return True, str(pos.get("symbol") or "unknown")
            except (TypeError, ValueError):
                continue
        return False, None

    def _register_ema200_utbot_rsi_handlers(self, owner_only, text_filter):
        async def menu_cmd(update, context):
            if self.is_upbit_mode():
                await update.message.reply_text(
                    "이 전략은 Binance 선물용입니다. Upbit 모드에서는 활성화할 수 없습니다."
                )
                return
            await update.message.reply_text(
                await self._ema200_utbot_rsi_status_text(),
                reply_markup=self._build_ema200_utbot_rsi_keyboard(),
            )

        async def callback(update, context):
            query = update.callback_query
            if not query:
                return
            await query.answer()
            data = str(query.data or "")
            parts = data.split(":")
            action = parts[1] if len(parts) > 1 else ""

            if action == "activate":
                if self.is_upbit_mode():
                    await query.edit_message_text(
                        "이 전략은 Binance 선물용입니다. Upbit 모드에서는 활성화할 수 없습니다."
                    )
                    return
                has_pos, detail = await self._ema200_utbot_rsi_has_open_position()
                if has_pos is None:
                    await query.edit_message_text(
                        f"안전을 위해 전략 변경을 중단했습니다. {detail}",
                        reply_markup=self._build_ema200_utbot_rsi_keyboard(),
                    )
                    return
                if has_pos:
                    await query.edit_message_text(
                        "⚠️ 현재 열린 포지션이 있어 전략을 바꾸지 않았습니다.\n"
                        f"보유: {detail}\n\n"
                        "포지션 보유 중 전략을 바꾸면 청산 규칙이 달라질 수 있으므로 "
                        "포지션이 완전히 종료된 뒤 다시 선택하세요.",
                        reply_markup=self._build_ema200_utbot_rsi_keyboard(),
                    )
                    return
                section = self.get_active_trade_section()
                await self.cfg.update_value(
                    [section, "strategy_params", "active_strategy"],
                    EMA200_UTBOT_RSI_STRATEGY,
                )
                await query.edit_message_text(
                    "✅ EMA200 + UT Bot + RSI (2H)를 활성 전략으로 선택했습니다.\n"
                    "2시간 완료봉 기준으로만 새 진입을 판단합니다.",
                    reply_markup=self._build_ema200_utbot_rsi_keyboard(),
                )
                return

            if action == "entry_toggle":
                cfg = self._ema200_utbot_rsi_config()
                await self._update_ema200_utbot_rsi_value("enabled", not cfg["enabled"])
                await query.edit_message_text(
                    (
                        "▶ 신규진입 ON" if not cfg["enabled"]
                        else "⏸ 신규진입 OFF — 이미 보유한 포지션의 정상청산/비상보호는 유지됩니다."
                    ),
                    reply_markup=self._build_ema200_utbot_rsi_keyboard(),
                )
                return

            if action == "status":
                await query.edit_message_text(
                    await self._ema200_utbot_rsi_status_text(),
                    reply_markup=self._build_ema200_utbot_rsi_keyboard(),
                )
                return

            if action == "guide":
                await query.edit_message_text(
                    self._ema200_utbot_rsi_help_text("guide"),
                    reply_markup=self._build_ema200_utbot_rsi_keyboard(),
                )
                return

            if action == "help" and len(parts) > 2:
                await query.edit_message_text(
                    self._ema200_utbot_rsi_help_text(parts[2]),
                    reply_markup=self._build_ema200_utbot_rsi_keyboard(),
                )
                return

            field_map = {
                "risk": "risk_per_trade_percent",
                "emergency": "emergency_exit_percent",
                "daily": "daily_loss_limit_percent",
                "weekly": "weekly_loss_limit_percent",
                "leverage": "leverage",
            }
            if action in field_map and len(parts) > 2:
                raw = parts[2]
                value = int(raw) if action == "leverage" else float(raw)
                await self._update_ema200_utbot_rsi_value(field_map[action], value)
                await query.edit_message_text(
                    f"✅ 설정 변경: {action} = {value}{'x' if action == 'leverage' else '%'}\n\n"
                    "설정은 새로 진입하는 포지션부터 적용됩니다.",
                    reply_markup=self._build_ema200_utbot_rsi_keyboard(),
                )
                return

            if action == "custom" and len(parts) > 2:
                kind = parts[2]
                if kind not in field_map:
                    return
                context.user_data["ema200_utbot_rsi_custom"] = kind
                ranges = {
                    "risk": "0.10~5.00 (%)",
                    "emergency": "0.5~30 (%)",
                    "daily": "0.5~20 (%)",
                    "weekly": "1~40 (%)",
                    "leverage": "1~10 (배)",
                }
                await query.edit_message_text(
                    self._ema200_utbot_rsi_help_text(kind)
                    + f"\n\n✍️ 지금 숫자만 입력해서 보내세요. 허용범위: {ranges[kind]}\n"
                    + ("예: 5" if kind != "risk" else "예: 0.5"),
                    reply_markup=self._build_ema200_utbot_rsi_keyboard(),
                )
                return

        async def custom_input(update, context):
            kind = context.user_data.get("ema200_utbot_rsi_custom")
            if not kind:
                return
            if not update.message:
                return
            raw = str(update.message.text or "").strip().replace("%", "").lower().replace("x", "")
            context.user_data.pop("ema200_utbot_rsi_custom", None)
            try:
                number = float(raw)
            except (TypeError, ValueError):
                await update.message.reply_text(
                    "숫자로 입력해야 합니다. 예: 0.5 또는 5\n"
                    "다시 /ema200 메뉴에서 직접입력 버튼을 눌러 주세요."
                )
                raise ApplicationHandlerStop

            cfg = self._ema200_utbot_rsi_config()
            bounds = {
                "risk": (cfg["min_risk_per_trade_percent"], cfg["max_risk_per_trade_percent"]),
                "emergency": (cfg["min_emergency_exit_percent"], cfg["max_emergency_exit_percent"]),
                "daily": (cfg["min_daily_loss_limit_percent"], cfg["max_daily_loss_limit_percent"]),
                "weekly": (cfg["min_weekly_loss_limit_percent"], cfg["max_weekly_loss_limit_percent"]),
                "leverage": (cfg["min_leverage"], cfg["max_leverage"]),
            }
            low, high = bounds[kind]
            if number < float(low) or number > float(high):
                await update.message.reply_text(
                    f"허용범위를 벗어났습니다: {low} ~ {high}\n"
                    "설정은 변경하지 않았습니다. /ema200에서 다시 시도하세요."
                )
                raise ApplicationHandlerStop
            if kind == "leverage" and not float(number).is_integer():
                await update.message.reply_text(
                    "레버리지는 정수로 입력하세요. 예: 3 또는 5"
                )
                raise ApplicationHandlerStop
            if kind == "weekly" and number < float(cfg["daily_loss_limit_percent"]):
                await update.message.reply_text(
                    f"최근 7일 손실한도는 일일 손실한도({cfg['daily_loss_limit_percent']:.2f}%)보다 "
                    "작게 설정할 수 없습니다. 설정은 변경하지 않았습니다."
                )
                raise ApplicationHandlerStop

            field_map = {
                "risk": "risk_per_trade_percent",
                "emergency": "emergency_exit_percent",
                "daily": "daily_loss_limit_percent",
                "weekly": "weekly_loss_limit_percent",
                "leverage": "leverage",
            }
            value = int(number) if kind == "leverage" else float(number)
            await self._update_ema200_utbot_rsi_value(field_map[kind], value)

            extra = ""
            if kind == "daily" and number > float(cfg["weekly_loss_limit_percent"]):
                await self._update_ema200_utbot_rsi_value(
                    "weekly_loss_limit_percent",
                    float(number),
                )
                extra = (
                    f"\n최근 7일 한도가 일일 한도보다 작아지지 않도록 "
                    f"7일 한도도 {number:.2f}%로 맞췄습니다."
                )
            if kind == "risk" and number > 1.0:
                extra += (
                    "\n⚠️ 1회 위험이 1%를 넘습니다. 연속 손실에서 계좌 감소 속도가 "
                    "빠르게 커질 수 있으니 의도한 값인지 확인하세요."
                )
            suffix = "x" if kind == "leverage" else "%"
            await update.message.reply_text(
                f"✅ 직접설정 완료: {kind} = {value}{suffix}\n"
                "새 포지션부터 적용됩니다."
                + extra,
                reply_markup=self._build_ema200_utbot_rsi_keyboard(),
            )
            raise ApplicationHandlerStop

        self.tg_app.add_handler(
            CommandHandler("ema200", owner_only(menu_cmd))
        )
        self.tg_app.add_handler(
            CallbackQueryHandler(owner_only(callback), pattern=r"^e2h:")
        )
        # Run before ordinary text/menu handlers only when a custom-input state exists.
        self.tg_app.add_handler(
            MessageHandler(text_filter, owner_only(custom_input)),
            group=-2,
        )


__all__ = ("ControllerEMA200UTBotRSIMixin",)
