"""Telegram controls for the isolated Binance European Options sleeve."""

from __future__ import annotations

import asyncio
import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler

from options_trading import OptionsTradingService
from options_trading.config import OPTIONS_CAPITAL_LIMIT_USDT


logger = logging.getLogger(__name__)


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
                    f"💰 {OPTIONS_CAPITAL_LIMIT_USDT:.0f}달러 예산",
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
            (
                "고정 한도: 수수료 포함 동시 위험 최대 "
                f"{status.get('capital_limit_usdt', OPTIONS_CAPITAL_LIMIT_USDT):.2f} USDT"
            ),
            f"전략 잔여예산: {status.get('cash_bankroll_usdt', 0):.4f} USDT",
            f"옵션 계좌: 가용 {_safe_number(balance.get('available')):.4f} / 평가 {_safe_number(balance.get('equity')):.4f} USDT",
            f"거래소 포지션/주문: {status.get('exchange_positions', 0)} / {status.get('exchange_orders', 0)}",
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
        else:
            lines.extend(["", "보유 중: 없음"])
        if candidate:
            lines.extend(
                [
                    "",
                    "최근 후보:",
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

    def _register_options_trading_handlers(self, owner_only):
        async def options_cmd(update, context):
            await update.message.reply_text(
                await self._format_options_status(refresh=True),
                reply_markup=self._build_options_keyboard(),
            )

        async def options_callback(update, context):
            query = update.callback_query
            if not query:
                return
            await query.answer()
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
                    f"매수 프리미엄·예상 수수료 합계는 {OPTIONS_CAPITAL_LIMIT_USDT:.0f} USDT를 넘지 않으며 "
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
                    f"{OPTIONS_CAPITAL_LIMIT_USDT:.0f} USDT·최소수량·수수료를 먼저 통과한 계약만 "
                    "순기대수익·Delta·DTE·IV/RV·IV표면·skew·Spread·최근 체결흐름·유동성·Greeks로 비교합니다.\n"
                    "메이커 우선 체결 뒤 순기대수익이 남을 때만 IOC로 전환합니다. 고정 +80% 익절 대신 단계형 추적청산으로 큰 수익을 열어 두며 -55% 프리미엄 손절과 만기·시간 제한은 유지합니다.\n"
                    "옵션 매수만 허용하므로 신규 진입용 SELL/네이키드 매도는 만들지 않습니다.",
                )
                return
            if action == "budget":
                status = await self._options_service().status_snapshot(refresh=True)
                await self._edit_options_message(
                    query,
                    f"💰 옵션 전용 {OPTIONS_CAPITAL_LIMIT_USDT:.0f} USDT 원장\n"
                    f"남은 전략예산: {status.get('cash_bankroll_usdt', 0):.4f} USDT\n"
                    "한 번의 진입은 남은 전략예산·실제 옵션 가용잔고·고정 한도 중 가장 작은 금액 안에서 계산하며, "
                    "프리미엄과 예상 진입 수수료를 합쳐 제한합니다.\n"
                    f"수익은 원장으로 돌아오지만 동시 위험 한도는 계속 {OPTIONS_CAPITAL_LIMIT_USDT:.0f} USDT입니다.\n"
                    "선물 잔고·선물 리스크·일일손실 한도와는 완전히 별개입니다.",
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
            interval = self._options_service().config().get("manage_interval_seconds", 30)
            await asyncio.sleep(max(15, int(interval)))


def _safe_number(value):
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
