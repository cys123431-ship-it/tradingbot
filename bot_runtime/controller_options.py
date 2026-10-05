"""Telegram controls for the isolated Binance European Options sleeve."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler

from options_trading import OptionsTradingService
from options_trading.config import (
    BTC_DTE_PRESET_LABELS,
    BTC_DTE_PRESETS,
    BTC_ONLY_UNDERLYING,
    BTC_SPREAD_CHOICES,
    options_budget_text,
    multi_underlying_restore_values,
    normalize_btc_spread_choice,
)


logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")


class ControllerOptionsMixin:
    def _options_service(self):
        service = getattr(self, "options_trading_service", None)
        if service is not None:
            service.market_data_exchange = self.market_data_exchange
            return service

        def config_getter():
            return self.cfg.get("options_trading", {}) or {}

        def credentials_getter():
            api_cfg = self.cfg.get("api", {}) or {}
            return api_cfg.get("mainnet", {}) or {}

        service = OptionsTradingService(
            config_getter=config_getter,
            credentials_getter=credentials_getter,
            market_data_exchange=self.market_data_exchange,
            state_path=os.path.join(self.runtime_dir, "options_trading_state.json"),
            notifier=self.notify_plain,
        )
        self.options_trading_service = service
        return service

    @staticmethod
    def _build_options_keyboard(*, confirming_on=False, confirming_close=False):
        rows = [
            [
                InlineKeyboardButton("▶️ 옵션 ON", callback_data="op:on"),
                InlineKeyboardButton("⏹ 옵션 OFF", callback_data="op:off"),
            ],
            [
                InlineKeyboardButton("📊 상태", callback_data="op:status"),
                InlineKeyboardButton("🔎 지금 스캔", callback_data="op:scan"),
            ],
            [
                InlineKeyboardButton("📈 전략 설명", callback_data="op:strategy"),
                InlineKeyboardButton(
                    "💰 예산",
                    callback_data="op:budget",
                ),
            ],
            [InlineKeyboardButton("🔻 봇 옵션 포지션 청산", callback_data="op:close")],
        ]
        if confirming_on:
            rows.insert(
                0,
                [
                    InlineKeyboardButton("✅ 실주문 시작 확인", callback_data="op:confirm_on"),
                    InlineKeyboardButton("취소", callback_data="op:status"),
                ],
            )
        if confirming_close:
            rows.insert(
                0,
                [
                    InlineKeyboardButton("✅ 청산 확인", callback_data="op:confirm_close"),
                    InlineKeyboardButton("취소", callback_data="op:status"),
                ],
            )
        return InlineKeyboardMarkup(rows)

    async def _format_options_status(self, *, refresh=True):
        status = await self._options_service().status_snapshot(refresh=refresh)
        active = status.get("active_position") or {}
        candidate = status.get("last_candidate") or {}
        balance = status.get("balance") or {}
        lines = [
            "🟣 Binance European Options",
            "네트워크: MAINNET (선물 테스트넷/메인넷 설정과 별도)",
            f"자동매매: {'ON' if status.get('enabled') else 'OFF'}",
            f"API 연결: {'정상' if status.get('api_ok') else '실패'}",
            f"옵션 주문 권한: {('허용' if status.get('can_trade') else '차단') if status.get('can_trade') is not None else '확인 불가'}",
            "운용 방식: 옵션 매수 전용 · 네이키드 매도 금지",
            f"대상 기초자산: {_underlyings_text(self._options_service().config())}",
            f"운용 예산: {status.get('budget_text') or options_budget_text(self._options_service().config())}",
            f"봇 옵션 누적 손익: {_safe_number(status.get('realized_pnl_usdt')):+.4f} USDT",
            f"옵션 계좌: 가용 {_safe_number(balance.get('available')):.4f} / 평가 {_safe_number(balance.get('equity')):.4f} USDT",
            f"거래소 포지션/주문: {status.get('exchange_positions', 0)} / {status.get('exchange_orders', 0)}",
            f"관리 API 연속 오류: {int(status.get('manage_error_streak') or 0)}회",
        ]
        if active:
            lines.extend(
                [
                    "",
                    "보유 중:",
                    f"{active.get('symbol')} {active.get('side')} · 수량 {_safe_number(active.get('quantity')):g}",
                    f"진입 {_safe_number(active.get('entry_price')):.4f} · 최근 {_safe_number(active.get('last_mark')):.4f}",
                    f"프리미엄 손익률 {_safe_number(active.get('last_pnl_pct')) * 100:+.1f}%",
                ]
            )
            if active.get("last_bid") is not None:
                lines.append(
                    f"Bid {_safe_number(active.get('last_bid')):.4f} · "
                    f"즉시 청산 기준 손익률 {_safe_number(active.get('last_bid_pnl_pct')) * 100:+.1f}%"
                )
        else:
            lines.extend(["", "보유 중: 없음"])
        if candidate and _candidate_expired(candidate):
            candidate = {}
        if candidate:
            lines.extend(
                [
                    "",
                    f"최근 후보 ({_candidate_found_text(candidate)}):",
                    f"{candidate.get('symbol')} · {candidate.get('strategy') or 'ADAPTIVE_TREND'} · 신호 {_safe_number(candidate.get('signal_score')):+.2f}",
                    f"Delta {_safe_number(candidate.get('delta')):.2f}/{_safe_number(candidate.get('target_delta')):.2f} · DTE {_safe_number(candidate.get('dte_days')):.1f}/{_safe_number(candidate.get('target_dte_days')):.1f}일",
                    f"Spread {_safe_number(candidate.get('spread_pct')) * 100:.1f}% · IV/RV {_safe_number(candidate.get('iv_to_realized')):.2f} · 순기대수익 {_safe_number(candidate.get('net_expected_edge_pct')) * 100:+.1f}%",
                    f"IV표면 프리미엄 {_safe_number(candidate.get('surface_iv_premium_pct')) * 100:+.1f}% · 흐름 {_safe_number(candidate.get('flow_score')):+.2f} · 투입 {_safe_number(candidate.get('entry_fraction')) * 100:.0f}%/{_safe_number(candidate.get('planned_cost_usdt')):.2f} USDT",
                ]
            )

        stats = status.get("scan_rejection_stats") or {}
        labels = status.get("scan_outcome_labels") or {}
        window = int(status.get("scan_outcomes_window") or 0)
        if window:
            lines.extend(["", f"최근 {window}회 옵션 스캔 결과:"])
            order = [
                "DIRECTION_SIGNAL",
                "DTE",
                "DELTA",
                "IV",
                "EDGE",
                "FLOW",
                "SPREAD",
                "LIQUIDITY",
                "BUDGET",
                "EXISTING_POSITION",
                "OPEN_ORDER",
                "CAN_TRADE",
                "API",
                "OTHER",
                "ORDERABLE_CANDIDATE",
            ]
            for code in order:
                count = int(stats.get(code) or 0)
                if count:
                    lines.append(f"- {labels.get(code) or code}: {count}")

        lines.extend(["", f"최근 판단: {status.get('last_reason') or '없음'}"])
        if status.get("api_error"):
            lines.append(f"API 오류: {status.get('api_error')}")
        if status.get("last_error") and status.get("last_error") != status.get("api_error"):
            lines.append(f"운영 오류: {status.get('last_error')}")
        lines.append("OFF는 신규 진입만 중단하며 이미 보유한 봇 옵션의 손절·익절 관리는 계속됩니다.")
        return "\n".join(lines)

    async def _edit_options_message(self, query, text, *, keyboard=None):
        keyboard = keyboard or self._build_options_keyboard()
        try:
            await query.edit_message_text(text, reply_markup=keyboard)
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    # ----- interplay with futures strategies and the STOP button -----

    async def _turn_off_options_for_futures_strategy(self):
        """Activating a futures strategy switches option new entries off.

        Held bot options keep their stop/take-profit management.
        """
        try:
            options_cfg = self.cfg.get("options_trading", {}) or {}
            if not options_cfg.get("enabled"):
                return False
            await self.cfg.update_value(["options_trading", "enabled"], False)
        except Exception:
            logger.exception("Could not switch options off for a futures strategy")
            return False
        try:
            await self.notify_plain(
                "⏹ 선물 전략을 켜서 옵션 자동 신규진입을 OFF 했습니다.\n"
                "보유 중인 봇 옵션은 손절·익절 관리를 계속합니다. 다시 켜려면 /btcoptions"
            )
        except Exception:
            logger.exception("Options-off notification failed")
        return True

    async def _stop_all_auxiliary_trading(self):
        """STOP button: options off + close bot options, Prediction auto off."""
        lines = []
        try:
            options_cfg = self.cfg.get("options_trading", {}) or {}
            was_on = bool(options_cfg.get("enabled"))
            await self.cfg.update_value(["options_trading", "enabled"], False)
            service = self._options_service()
            active = (getattr(service, "state", None) or {}).get("active_position") or {}
            if active.get("symbol"):
                result = await service.run_cycle(force_exit=True)
                lines.append(
                    f"옵션: 신규진입 OFF · 봇 옵션 {active.get('symbol')} 청산 요청 — "
                    f"{result.get('reason') or result.get('action')}"
                )
            else:
                lines.append("옵션: 신규진입 OFF" + ("" if was_on else " (이미 꺼져 있었음)"))
        except Exception as exc:
            logger.exception("Options emergency stop failed")
            lines.append(f"⚠️ 옵션 정지 처리 오류: {exc}")
        try:
            prediction_cfg = self.cfg.get("prediction_micro_auto", {}) or {}
            if isinstance(prediction_cfg, dict) and prediction_cfg.get("enabled"):
                await self.cfg.update_value(["prediction_micro_auto", "enabled"], False)
                lines.append("Prediction Micro Auto: OFF")
        except Exception as exc:
            logger.exception("Prediction emergency stop failed")
            lines.append(f"⚠️ Prediction 정지 처리 오류: {exc}")
        return lines

    async def _emergency_stop_everything(self):
        """Futures emergency stop first, then every other automatic feature."""
        result = await self.emergency_stop()
        text = self._format_emergency_stop_reply(result)
        extra = await self._stop_all_auxiliary_trading()
        stop_pullback = getattr(self, "_stop_btc_pullback_for_emergency", None)
        if callable(stop_pullback):
            extra = list(extra) + await stop_pullback()
        if extra:
            text += "\n\n" + "\n".join(extra)
        return text

    # ----- BTC-only options menu (/btcoptions, callbacks "bo:") -----

    @staticmethod
    def _build_btc_options_keyboard(
        cfg, *, confirming_on=False, confirming_close=False, confirming_multi=False
    ):
        preset = cfg.get("btc_dte_preset")
        spread = _safe_number(cfg.get("btc_max_spread_pct"))

        def mark(selected, label):
            return f"✅ {label}" if selected else label

        rows = [
            [
                InlineKeyboardButton("▶️ BTC 옵션 ON", callback_data="bo:on"),
                InlineKeyboardButton("⏹ 신규진입 OFF", callback_data="bo:off"),
            ],
            [
                InlineKeyboardButton("📊 상태", callback_data="bo:status"),
                InlineKeyboardButton("🔎 지금 스캔", callback_data="bo:scan"),
            ],
            [
                InlineKeyboardButton(
                    mark(preset == key, BTC_DTE_PRESET_LABELS[key].split(" ")[0]),
                    callback_data=f"bo:dte:{key}",
                )
                for key in BTC_DTE_PRESETS
            ],
            [
                InlineKeyboardButton(
                    mark(abs(spread - choice) < 1e-9, f"스프레드 {choice * 100:.0f}%"),
                    callback_data=f"bo:spr:{choice:g}",
                )
                for choice in BTC_SPREAD_CHOICES
            ],
            [
                InlineKeyboardButton("📘 운용 규칙", callback_data="bo:help"),
                InlineKeyboardButton("🔁 멀티코인 복귀", callback_data="bo:multi"),
            ],
            [InlineKeyboardButton("🔻 봇 옵션 포지션 청산", callback_data="bo:close")],
        ]
        confirm = None
        if confirming_on:
            confirm = ("✅ BTC 옵션 실주문 시작", "bo:confirm_on")
        elif confirming_close:
            confirm = ("✅ 청산 확인", "bo:confirm_close")
        elif confirming_multi:
            confirm = ("✅ BTC 전용 해제", "bo:confirm_multi")
        if confirm:
            rows.insert(
                0,
                [
                    InlineKeyboardButton(confirm[0], callback_data=confirm[1]),
                    InlineKeyboardButton("취소", callback_data="bo:status"),
                ],
            )
        return InlineKeyboardMarkup(rows)

    def _btc_options_header_lines(self, cfg):
        preset = cfg.get("btc_dte_preset")
        lines = [
            "₿ BTC 전용 옵션 메뉴",
            f"BTC 전용 모드: {'ON' if cfg.get('btc_only') else 'OFF (멀티코인 설정 사용 중)'}",
            f"옵션 자동 신규진입: {'ON' if cfg.get('enabled') else 'OFF'}",
            (
                f"만기 범위: {BTC_DTE_PRESET_LABELS.get(preset, preset)} · "
                f"목표 {_safe_number(BTC_DTE_PRESETS.get(preset, (0, 0, 0))[1]):.0f}일"
            ),
            (
                f"최대 스프레드: {_safe_number(cfg.get('btc_max_spread_pct')) * 100:.0f}% · "
                f"최소 24h 거래대금: {_safe_number(cfg.get('min_quote_volume_usdt')):.0f} USDT"
            ),
            (
                "익절·추적청산 판단: Bid 기준 (실제로 팔리는 가격)"
                if cfg.get("profit_trigger_on_bid")
                else "익절·추적청산 판단: Mark 기준"
            ),
        ]
        active = (self._options_service().state or {}).get("active_position") or {}
        symbol = str(active.get("symbol") or "")
        if symbol and not symbol.upper().startswith("BTC-"):
            lines.append(
                f"⚠️ 기존 {symbol} 포지션은 청산될 때까지 계속 관리합니다 (신규 진입만 BTC)."
            )
        return lines

    def _btc_options_settings_text(self):
        # Setting buttons confirm from config only: no exchange round-trips,
        # so a press is never slowed down (or timed out) by the API.
        return (
            "\n".join(self._btc_options_header_lines(self._options_service().config()))
            + "\n\n✅ 저장됨 · 계좌·후보 현황은 📊 상태 버튼으로 확인하세요."
        )

    async def _format_btc_options_status(self):
        cfg = self._options_service().config()
        body = await self._format_options_status(refresh=True)
        return "\n".join(self._btc_options_header_lines(cfg)) + "\n\n" + body

    def _btc_options_rules_text(self, cfg):
        return (
            "📘 BTC 전용 옵션 운용 규칙\n"
            "• 기초자산: BTC 옵션만 거래합니다. 알트 옵션은 호가가 얇아 익절 체결이 늦기 때문에 제외합니다.\n"
            "• 진입: 기존 Adaptive Convexity v2 신호(1h·4h 추세, 저IV 압축 돌파)로 CALL/PUT 매수만 합니다.\n"
            f"• 유동성 필터: 스프레드 {_safe_number(cfg.get('btc_max_spread_pct')) * 100:.0f}% 이하, "
            f"24h 거래대금 {_safe_number(cfg.get('min_quote_volume_usdt')):.0f} USDT 이상, 양쪽 호가 수량이 있어야 합니다.\n"
            "• 익절·추적청산: Mark가 아니라 Bid로 판단합니다. 신호가 나면 그 Bid에 IOC 매도가 바로 나갑니다.\n"
            f"• 손절: 프리미엄 -{_safe_number(cfg.get('stop_loss_pct')) * 100:.0f}% (Mark 기준). "
            f"만기 {_safe_number(cfg.get('expiry_exit_hours')):.0f}시간 전 정리, 최대 보유 {_safe_number(cfg.get('max_hold_hours')):.0f}시간.\n"
            f"• 예산: {options_budget_text(cfg)}. 바이낸스 옵션 지갑에 있는 USDT만 씁니다(선물 지갑과 별개). 네이키드 매도는 하지 않습니다.\n"
            "• 만기 버튼: 주간은 회전이 빠르지만 시간가치 감소가 크고, 월간은 느리지만 감소가 완만합니다.\n"
            "• OFF는 신규 진입만 멈춥니다. 보유 중인 옵션은 손절·익절 관리를 계속합니다."
        )

    async def _handle_btc_options_action(self, action):
        """Return (text, keyboard) for one /btcoptions button press."""
        service = self._options_service()
        cfg = service.config()

        def keyboard(**flags):
            return self._build_btc_options_keyboard(service.config(), **flags)

        if action == "on":
            preflight = await service.preflight()
            if not preflight.get("ok") or preflight.get("can_trade") is False:
                return (
                    "❌ 옵션 API 사전점검 실패\n"
                    f"{preflight.get('error') or 'European Options 주문 권한이 비활성 상태입니다.'}\n\n"
                    "Reading·European Options 권한과 서버 IP 제한을 확인하세요.",
                    keyboard(),
                )
            return (
                "⚠️ BTC 옵션 실주문을 시작하시겠습니까?\n"
                f"대상: {BTC_ONLY_UNDERLYING} 옵션만 · 예산: {options_budget_text(cfg)} "
                "(프리미엄+예상 수수료 기준) · 네이키드 매도 없음.\n"
                "옵션은 만기까지 시간가치가 줄어 프리미엄 전액을 잃을 수 있습니다.",
                keyboard(confirming_on=True),
            )
        if action == "confirm_on":
            await self.cfg.update_value(["options_trading", "btc_only"], True)
            await self.cfg.update_value(["options_trading", "enabled"], True)
            result = await service.run_cycle(force_scan=True)
            return (
                "✅ BTC 옵션 자동매매 ON\n"
                f"첫 판단: {result.get('reason') or result.get('action')}\n\n"
                + await self._format_btc_options_status(),
                keyboard(),
            )
        if action == "off":
            await self.cfg.update_value(["options_trading", "enabled"], False)
            return (
                "⏹ 옵션 신규 진입 OFF\n"
                "보유 중인 봇 옵션은 기존 손절·익절 규칙으로 계속 관리합니다.\n\n"
                + await self._format_btc_options_status(),
                keyboard(),
            )
        if action == "scan":
            result = await service.run_cycle(force_scan=True)
            return (
                f"🔎 즉시 점검: {result.get('reason') or result.get('action')}\n\n"
                + await self._format_btc_options_status(),
                keyboard(),
            )
        if action.startswith("dte:"):
            preset = action.split(":", 1)[1]
            if preset in BTC_DTE_PRESETS:
                await self.cfg.update_value(["options_trading", "btc_dte_preset"], preset)
                prefix = f"🗓 만기 범위: {BTC_DTE_PRESET_LABELS[preset]}"
            else:
                prefix = "알 수 없는 만기 설정입니다."
            return prefix + "\n\n" + self._btc_options_settings_text(), keyboard()
        if action.startswith("spr:"):
            choice = normalize_btc_spread_choice(action.split(":", 1)[1])
            await self.cfg.update_value(["options_trading", "btc_max_spread_pct"], choice)
            return (
                f"↔️ 최대 스프레드: {choice * 100:.0f}%\n\n"
                + self._btc_options_settings_text(),
                keyboard(),
            )
        if action == "help":
            return self._btc_options_rules_text(cfg), keyboard()
        if action == "multi":
            return (
                "⚠️ BTC 전용 모드를 해제하시겠습니까?\n"
                "안전을 위해 옵션 신규 진입도 함께 OFF 됩니다. 멀티코인으로 다시 켜려면 /options 메뉴를 쓰세요.",
                keyboard(confirming_multi=True),
            )
        if action == "confirm_multi":
            await self.cfg.update_value(["options_trading", "enabled"], False)
            await self.cfg.update_value(["options_trading", "btc_only"], False)
            for key, value in multi_underlying_restore_values().items():
                await self.cfg.update_value(["options_trading", key], value)
            return (
                "🔁 BTC 전용 해제 · 옵션 신규 진입 OFF\n\n"
                + await self._format_btc_options_status(),
                keyboard(),
            )
        if action == "close":
            return (
                "⚠️ 봇이 보유한 옵션 포지션을 IOC 지정가로 청산하시겠습니까?\n"
                "수동 옵션 포지션은 건드리지 않습니다.",
                keyboard(confirming_close=True),
            )
        if action == "confirm_close":
            result = await service.run_cycle(force_exit=True)
            return (
                f"🔻 옵션 청산 요청: {result.get('reason') or result.get('action')}\n\n"
                + await self._format_btc_options_status(),
                keyboard(),
            )
        return await self._format_btc_options_status(), keyboard()

    def _register_options_trading_handlers(self, owner_only):
        async def btc_options_cmd(update, context):
            await update.message.reply_text(
                await self._format_btc_options_status(),
                reply_markup=self._build_btc_options_keyboard(
                    self._options_service().config()
                ),
            )

        async def btc_options_callback(update, context):
            query = update.callback_query
            if not query:
                return
            await _answer_quietly(query)
            action = str(query.data or "").split(":", 1)[-1]
            text, keyboard = await self._handle_btc_options_action(action)
            await self._edit_options_message(query, text, keyboard=keyboard)

        self.tg_app.add_handler(CommandHandler("btcoptions", owner_only(btc_options_cmd)))
        self.tg_app.add_handler(
            CallbackQueryHandler(owner_only(btc_options_callback), pattern=r"^bo:")
        )

        async def options_cmd(update, context):
            await update.message.reply_text(
                await self._format_options_status(refresh=True),
                reply_markup=self._build_options_keyboard(),
            )

        async def options_callback(update, context):
            query = update.callback_query
            if not query:
                return
            await _answer_quietly(query)
            action = str(query.data or "").split(":", 1)[-1]
            if action == "on":
                preflight = await self._options_service().preflight()
                if not preflight.get("ok") or preflight.get("can_trade") is False:
                    await self._edit_options_message(
                        query,
                        "❌ 옵션 API 사전점검 실패\n"
                        f"{preflight.get('error') or 'European Options 주문 권한이 비활성 상태입니다.'}\n\n"
                        "Reading·European Options 권한과 서버 IP 제한을 확인하세요.",
                    )
                    return
                await self._edit_options_message(
                    query,
                    "⚠️ 옵션 실주문을 시작하시겠습니까?\n"
                    "매수 프리미엄·예상 수수료 합계는 옵션 지갑 가용 잔고 안에서만 쓰며 "
                    "네이키드 매도는 하지 않습니다.",
                    keyboard=self._build_options_keyboard(confirming_on=True),
                )
                return
            if action == "confirm_on":
                await self.cfg.update_value(["options_trading", "enabled"], True)
                result = await self._options_service().run_cycle(force_scan=True)
                await self._edit_options_message(
                    query,
                    "✅ 옵션 자동매매 ON\n"
                    f"첫 판단: {result.get('reason') or result.get('action')}\n\n"
                    + await self._format_options_status(refresh=True),
                )
                return
            if action == "off":
                await self.cfg.update_value(["options_trading", "enabled"], False)
                await self._edit_options_message(
                    query,
                    "⏹ 옵션 신규 진입 OFF\n"
                    "보유 중인 봇 옵션은 기존 손절·익절 규칙으로 계속 관리합니다.\n\n"
                    + await self._format_options_status(refresh=True),
                )
                return
            if action == "status":
                await self._edit_options_message(query, await self._format_options_status(refresh=True))
                return
            if action == "scan":
                result = await self._options_service().run_cycle(force_scan=True)
                await self._edit_options_message(
                    query,
                    f"🔎 즉시 점검: {result.get('reason') or result.get('action')}\n\n"
                    + await self._format_options_status(refresh=True),
                )
                return
            if action == "strategy":
                await self._edit_options_message(
                    query,
                    "📈 Adaptive Convexity Trend v2\n"
                    "1시간·4시간 다중속도 추세와 Low-IV Squeeze를 결합하고, HAR식 다중기간 실현변동성으로 신호강도에 맞는 DTE·Delta를 고릅니다.\n"
                    "Low-IV Squeeze는 ATR/실현변동성 압축 뒤 거래량·모멘텀을 동반한 상·하방 돌파만 CALL/PUT 후보로 봅니다.\n"
                    "옵션 지갑 잔고·최소수량·수수료를 먼저 통과한 계약만 "
                    "순기대수익·Delta·DTE·IV/RV·IV표면·skew·Spread·최근 체결흐름·유동성·Greeks로 비교합니다.\n"
                    "메이커 우선 체결 뒤 순기대수익이 남을 때만 IOC로 전환합니다. 고정 +80% 익절 대신 단계형 추적청산으로 큰 수익을 열어 두며 -55% 프리미엄 손절과 만기·시간 제한은 유지합니다.\n"
                    "옵션 매수만 허용하므로 신규 진입용 SELL/네이키드 매도는 만들지 않습니다.",
                )
                return
            if action == "budget":
                status = await self._options_service().status_snapshot(refresh=True)
                await self._edit_options_message(
                    query,
                    f"💰 옵션 예산: {status.get('budget_text') or '-'}\n"
                    f"옵션 지갑 가용: {_safe_number((status.get('balance') or {}).get('available')):.4f} USDT\n"
                    f"봇 옵션 누적 손익: {_safe_number(status.get('realized_pnl_usdt')):+.4f} USDT\n"
                    "한 번의 진입은 진입 시점의 옵션 지갑 가용 잔고 안에서 프리미엄과 예상 수수료를 합쳐 계산합니다.\n"
                    "입금하면 바로 예산이 늘고, 손실이 나면 줄어든 잔고만큼만 씁니다.\n"
                    "선물 지갑·선물 리스크·일일손실 한도와는 완전히 별개입니다 (옵션 지갑으로 이체 필요).",
                )
                return
            if action == "close":
                await self._edit_options_message(
                    query,
                    "⚠️ 봇이 보유한 옵션 포지션을 IOC 지정가로 청산하시겠습니까?\n"
                    "수동 옵션 포지션은 건드리지 않습니다.",
                    keyboard=self._build_options_keyboard(confirming_close=True),
                )
                return
            if action == "confirm_close":
                result = await self._options_service().run_cycle(force_exit=True)
                await self._edit_options_message(
                    query,
                    f"🔻 옵션 청산 요청: {result.get('reason') or result.get('action')}\n\n"
                    + await self._format_options_status(refresh=True),
                )
                return
            await self._edit_options_message(query, await self._format_options_status(refresh=True))

        self.tg_app.add_handler(CommandHandler("options", owner_only(options_cmd)))
        self.tg_app.add_handler(
            CallbackQueryHandler(owner_only(options_callback), pattern=r"^op:")
        )

    async def _options_trading_loop(self):
        """Run independently of PTB's optional JobQueue dependency."""
        await asyncio.sleep(20)
        while True:
            try:
                await self._options_service().run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Options scheduler cycle failed")
            interval = self._options_service().config().get("manage_interval_seconds", 10)
            await asyncio.sleep(max(5, int(interval)))


async def _answer_quietly(query):
    try:
        await query.answer()
    except BadRequest as exc:
        # "Query is too old": the spinner is gone, but the press still counts.
        logger.info("Options callback answer skipped: %s", exc)


def _candidate_expiry_ms(candidate):
    expiry = int(_safe_number(candidate.get("expiry_date_ms")))
    if expiry > 0:
        return expiry
    # Older summaries lack the expiry: parse it from e.g. "SOL-260828-88-C".
    parts = str(candidate.get("symbol") or "").split("-")
    if len(parts) >= 2 and len(parts[1]) == 6 and parts[1].isdigit():
        try:
            day = datetime.strptime(parts[1], "%y%m%d").replace(
                hour=8, tzinfo=timezone.utc
            )
        except ValueError:
            return 0
        return int(day.timestamp() * 1000)
    return 0


def _candidate_expired(candidate, now_ms=None):
    expiry = _candidate_expiry_ms(candidate)
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    return bool(expiry) and expiry <= now_ms


def _candidate_found_text(candidate):
    found = int(_safe_number(candidate.get("found_at_ms")))
    if found <= 0:
        return "발견 시각 미기록"
    return datetime.fromtimestamp(found / 1000, _KST).strftime("%m-%d %H:%M KST 발견")


def _underlyings_text(cfg):
    if cfg.get("btc_only"):
        return f"{BTC_ONLY_UNDERLYING} 전용 (/btcoptions)"
    return ", ".join(cfg.get("underlyings") or []) or "-"


def _safe_number(value):
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
