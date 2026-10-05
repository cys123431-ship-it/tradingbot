"""Telegram controls (/btcpullback) and scheduler for the BTC EMA pullback strategy."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler

from btc_ema_pullback import BtcEmaPullbackService
from btc_ema_pullback.config import (
    NETWORK_MAINNET,
    NETWORK_TESTNET,
    TRADING_MODE_DRY_RUN,
    TRADING_MODE_ENV,
    TRADING_MODE_LIVE,
    sizing_mode_for,
)

from .strategy_registry import BINANCE_MAINNET, BINANCE_TESTNET

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")
CONFIG_KEY = "btc_ema_pullback"
NETWORK_LABELS = {NETWORK_TESTNET: "테스트넷(데모 5,000 USDT)", NETWORK_MAINNET: "메인넷(실계좌)"}


def _n(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _kst(iso_text):
    try:
        return datetime.fromisoformat(str(iso_text)).astimezone(_KST).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return "-"


async def _answer_quietly(query):
    try:
        await query.answer()
    except BadRequest as exc:
        logger.info("btcpullback callback answer skipped: %s", exc)


class ControllerBtcPullbackMixin:
    def _btc_pullback_service(self):
        service = getattr(self, "btc_pullback_service", None)
        if service is not None:
            return service

        def config_getter():
            return self.cfg.get(CONFIG_KEY, {}) or {}

        def credentials_getter(network):
            api_cfg = self.cfg.get("api", {}) or {}
            return api_cfg.get("testnet" if network == NETWORK_TESTNET else "mainnet", {}) or {}

        def exchange_factory(network, creds):
            mode = BINANCE_TESTNET if network == NETWORK_TESTNET else BINANCE_MAINNET
            exchange = self._build_exchange(creds, mode)
            self._configure_exchange_network(exchange, mode)
            return exchange

        def main_network_getter():
            mode = self.get_exchange_mode()
            if mode == BINANCE_MAINNET:
                return NETWORK_MAINNET
            if mode == BINANCE_TESTNET:
                return NETWORK_TESTNET
            return None

        async def notifier(text):
            event_type = None
            if "🚨" in text:
                event_type = "EMERGENCY"
            elif "진입 (" in text:
                event_type = "ENTRY_FILLED"
            elif "청산 (" in text:
                event_type = "EXIT_FILLED"
            elif "⚠️" in text:
                event_type = "PROTECTION_MISSING"
            await self.notify_plain(text, event_type=event_type)

        service = BtcEmaPullbackService(
            config_getter=config_getter,
            credentials_getter=credentials_getter,
            exchange_factory=exchange_factory,
            runtime_dir=self.runtime_dir,
            notifier=notifier,
            main_network_getter=main_network_getter,
        )
        self.btc_pullback_service = service
        return service

    # ------------------------------------------------------------ rendering
    @staticmethod
    def _build_btc_pullback_keyboard(cfg, *, confirm=None):
        def mark(selected, label):
            return f"✅ {label}" if selected else label

        rows = [
            [
                InlineKeyboardButton(mark(cfg.get("enabled"), "▶️ 전략 ON"), callback_data="bp:on"),
                InlineKeyboardButton(mark(not cfg.get("enabled"), "⏹ 신규진입 OFF"), callback_data="bp:off"),
            ],
            [
                InlineKeyboardButton(mark(cfg.get("trading_mode") == TRADING_MODE_DRY_RUN, "🧪 DRY_RUN"), callback_data="bp:mode:dry"),
                InlineKeyboardButton(mark(cfg.get("trading_mode") == TRADING_MODE_LIVE, "💥 LIVE"), callback_data="bp:mode:live"),
            ],
            [
                InlineKeyboardButton(mark(cfg.get("network") == NETWORK_TESTNET, "🧪 테스트넷"), callback_data="bp:net:testnet"),
                InlineKeyboardButton(mark(cfg.get("network") == NETWORK_MAINNET, "💰 메인넷"), callback_data="bp:net:mainnet"),
            ],
            [
                InlineKeyboardButton("📊 상태", callback_data="bp:status"),
                InlineKeyboardButton("📈 성과 통계", callback_data="bp:stats"),
            ],
            [
                InlineKeyboardButton("🧾 최근 신호 판단", callback_data="bp:signals"),
                InlineKeyboardButton("🔄 거래소 재대조", callback_data="bp:reconcile"),
            ],
            [
                InlineKeyboardButton("📘 전략 규칙", callback_data="bp:rules"),
                InlineKeyboardButton("🔻 포지션 청산", callback_data="bp:close"),
            ],
        ]
        if confirm:
            rows.insert(0, [
                InlineKeyboardButton(confirm[0], callback_data=confirm[1]),
                InlineKeyboardButton("취소", callback_data="bp:status"),
            ])
        return InlineKeyboardMarkup(rows)

    async def _format_btc_pullback_status(self):
        service = self._btc_pullback_service()
        status = await service.status()
        cfg, state = status["config"], status["state"]
        mode, network = status["mode"], status["network"]
        trade = state.get("trade") or {}
        today = status["trades_today"]
        closed_today = [t for t in today if t.get("status") == "CLOSED"]
        streak = 0
        for row in reversed(closed_today):
            if _n(row.get("net_pnl")) < 0:
                streak += 1
            else:
                break
        sizing = sizing_mode_for(cfg)
        sizing_text = (
            f"고정 {cfg['base_quantity']} BTC" if sizing == "fixed"
            else f"위험 기반 (지갑의 {_n(cfg['max_risk_per_trade_pct']) * 100:.1f}% 손실 한도로 수량 계산 · 소액 한도 해제)"
        )
        lines = [
            "📐 BTC EMA 눌림목 전략 (1h 추세 + 15m 눌림)",
            f"전략: {'ON' if cfg['enabled'] else 'OFF'} · 모드: {mode}"
            + (" (환경변수로 DRY_RUN 강제)" if cfg["trading_mode"] == TRADING_MODE_LIVE and mode == TRADING_MODE_DRY_RUN else ""),
            f"네트워크: {NETWORK_LABELS.get(network, network)} · API 키: {'있음' if status['has_credentials'] else '없음'}",
            f"단계: {state.get('phase')}",
            f"수량: {sizing_text}",
            f"손절 {_n(cfg['stop_loss_pct']) * 100:.1f}% · 익절 {_n(cfg['take_profit_pct']) * 100:.1f}% · "
            f"{cfg['leverage']}x ISOLATED · 트리거 {cfg['working_type']}",
            f"오늘({status['day']} KST): 거래 {len(today)}/{cfg['max_trades_per_day']} · "
            f"순손익 {sum(_n(t.get('net_pnl')) for t in closed_today):+.4f} USDT · 연속 손실 {streak}/{cfg['max_consecutive_losses']}",
        ]
        if mode == TRADING_MODE_LIVE and status["main_network"] == network and not cfg["allow_shared_account_with_main_bot"]:
            lines.append("⚠️ 메인 봇과 같은 계좌라 LIVE 신규 진입이 차단됩니다.")
        if mode == TRADING_MODE_DRY_RUN and not status["has_credentials"]:
            lines.append(f"가상 잔고: {cfg['dry_run_balance_usdt']} USDT + 가상 손익 (API 키 없음)")
        if trade:
            lines += [
                "",
                f"보유: {trade.get('side')} {trade.get('quantity')} BTC @ {trade.get('entry_price')}",
                f"손절 {trade.get('stop_price')} · 익절 {trade.get('take_profit_price')}"
                + (" · ⚠️ 익절 주문 재시도 중" if trade.get("tp_missing") else ""),
            ]
        else:
            lines += ["", "보유: 없음"]
        last = status.get("last_skip") or {}
        if last:
            lines.append(
                f"최근 판단 ({_kst(last.get('ts'))}): "
                + (f"{last.get('side')} 신호" if last.get("side") and not last.get("skip_reason") else (last.get("skip_reason") or "-"))
            )
        if state.get("last_error"):
            lines.append(f"최근 오류: {state['last_error']}")
        lines.append("OFF는 신규 진입만 멈추며 보유 포지션의 손절·익절 관리는 계속됩니다.")
        return "\n".join(lines)

    async def _format_btc_pullback_stats(self):
        status = await self._btc_pullback_service().status()
        s = status["stats"]
        pf = s["profit_factor"]
        return "\n".join([
            f"📈 BTC 눌림목 성과 ({status['mode']} · {status['network']})",
            f"거래 {s['total_trades']}회 · 승 {s['wins']} / 패 {s['losses']} · 승률 {s['win_rate'] * 100:.1f}%",
            f"총이익 {s['gross_profit']:+.4f} · 총손실 {s['gross_loss']:+.4f} · 순손익 {s['net_pnl']:+.4f} USDT",
            f"평균 이익 {s['average_win']:+.4f} · 평균 손실 {s['average_loss']:+.4f}",
            f"손익비(PF) {'∞' if pf == float('inf') else f'{pf:.2f}'} · 기대값 {s['expectancy']:+.4f} USDT/회 · 평균 {s['average_r']:+.2f}R",
            f"최대 연승 {s['max_consecutive_wins']} · 최대 연패 {s['max_consecutive_losses']} · 최대 낙폭 {s['max_drawdown']:.4f} USDT",
            "승률만이 아니라 손익비·기대값·낙폭을 함께 보세요. 과거 성과가 미래 수익을 보장하지 않습니다.",
        ])

    def _format_btc_pullback_signals(self):
        cfg = self._btc_pullback_service().config()
        events = self._btc_pullback_service().ledger(cfg["network"]).recent_events(8, kinds={"SIGNAL", "SKIP", "ORDER", "EXIT", "FAILSAFE"})
        if not events:
            return "🧾 아직 기록된 신호 판단이 없습니다. (15분봉이 마감될 때마다 기록됩니다)"
        lines = ["🧾 최근 판단 (최신순)"]
        for event in events:
            kind = event.get("event")
            detail = event.get("skip_reason") or event.get("exit_reason") or event.get("reason") or ""
            if kind == "SIGNAL" and event.get("side") and not detail:
                detail = f"{event['side']} 신호 · 15m 종가 {_n(event.get('15m_close')):.1f}"
            elif kind == "ORDER":
                detail = f"{event.get('side')} {event.get('quantity')} @ {event.get('actual_avg_entry')}"
            lines.append(f"{_kst(event.get('ts'))} {kind}: {detail or '-'}")
        return "\n".join(lines)

    @staticmethod
    def _btc_pullback_rules_text(cfg):
        return (
            "📘 BTC EMA 눌림목 규칙\n"
            "• 추세(1시간봉 마감 기준): 종가 > EMA20 > EMA50 이고 EMA20 상승 → 롱 후보 (숏은 반대)\n"
            f"• 횡보 제외: EMA 간격 < {_n(cfg['ema_separation_threshold']) * 100:.2f}%, "
            f"EMA20 기울기 < {_n(cfg['ema_slope_threshold']) * 100:.3f}%/시간, "
            f"최근 {cfg['recent_cross_lookback']}시간 교차 > {cfg['max_recent_crosses']}회\n"
            f"• 진입(15분봉 마감 기준): 최근 {cfg['pullback_lookback']}봉 안에 EMA20 근처 눌림 → EMA50 붕괴 없음 → "
            "확인 캔들 종가가 직전 봉 고점(숏은 저점) 돌파\n"
            f"• 추격 금지: 확인 캔들 범위 > ATR×{cfg['max_entry_candle_atr_multiple']} 또는 몸통 > ATR×{cfg['max_entry_body_atr_multiple']}\n"
            f"• 손절 {_n(cfg['stop_loss_pct']) * 100:.1f}% / 익절 {_n(cfg['take_profit_pct']) * 100:.1f}% (실제 평균 체결가 기준, MARK_PRICE 트리거, 포지션 종료 전용 주문)\n"
            f"• 1회 위험 ≤ 지갑의 {_n(cfg['max_risk_per_trade_pct']) * 100:.1f}% (수수료·슬리피지 포함), 넘으면 SKIP\n"
            f"• 하루 {cfg['max_trades_per_day']}회 · {cfg['max_consecutive_losses']}연속 손실 · 하루 -{_n(cfg['max_daily_loss_pct']) * 100:.1f}% 도달 시 당일 신규 진입 중단 (KST 자정 기준)\n"
            "• 물타기·추가진입·마틴게일·손절 확대 없음, 동시에 BTCUSDT 1포지션만\n"
            "• 메인넷: 0.001 BTC 고정 / 테스트넷: 위험 기반 수량(소액 한도 해제)\n"
            "• 수익을 보장하지 않는 전략입니다."
        )

    async def _edit_btc_pullback(self, query, text, keyboard):
        try:
            await query.edit_message_text(text, reply_markup=keyboard)
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    async def _set_btc_pullback_value(self, key, value):
        await self.cfg.update_value([CONFIG_KEY, key], value)

    async def _handle_btc_pullback_action(self, action):
        """Return (text, keyboard) for one /btcpullback button press."""
        service = self._btc_pullback_service()

        def keyboard(confirm=None):
            return self._build_btc_pullback_keyboard(service.config(), confirm=confirm)

        cfg = service.config()
        state = service.load_state(cfg["network"], service.mode(cfg))
        holding = bool(state.get("trade"))

        if action == "on":
            return (
                "⚠️ BTC 눌림목 전략을 켤까요?\n"
                f"모드 {service.mode(cfg)} · {NETWORK_LABELS[cfg['network']]}\n"
                + ("실제 주문이 나갑니다." if service.mode(cfg) == TRADING_MODE_LIVE else "DRY_RUN: 실제 주문 없이 가상 체결만 기록합니다."),
                keyboard(("✅ 전략 시작", "bp:confirm_on")),
            )
        if action == "confirm_on":
            await self._set_btc_pullback_value("enabled", True)
            if service.mode() == TRADING_MODE_LIVE and service.config()["network"] == NETWORK_MAINNET:
                turn_off = getattr(self, "_turn_off_options_for_futures_strategy", None)
                if callable(turn_off):
                    await turn_off()
            return "✅ BTC 눌림목 전략 ON\n\n" + await self._format_btc_pullback_status(), keyboard()
        if action == "off":
            await self._set_btc_pullback_value("enabled", False)
            return "⏹ 신규 진입 OFF (보유 포지션 보호는 계속)\n\n" + await self._format_btc_pullback_status(), keyboard()
        if action.startswith(("mode:", "net:")) and holding:
            return "⚠️ 포지션 보유 중에는 모드/네트워크를 바꿀 수 없습니다. 먼저 정리하세요.", keyboard()
        if action == "mode:dry":
            await self._set_btc_pullback_value("trading_mode", TRADING_MODE_DRY_RUN)
            service.reset_reconciliation()
            return "🧪 DRY_RUN으로 전환 (실제 주문 없음)\n\n" + await self._format_btc_pullback_status(), keyboard()
        if action == "mode:live":
            return (
                "⚠️ LIVE로 전환할까요? 실제 주문이 나갑니다.\n"
                f"네트워크: {NETWORK_LABELS[cfg['network']]}\n"
                f"환경변수 {TRADING_MODE_ENV}=DRY_RUN 이 설정돼 있으면 계속 DRY_RUN으로 동작합니다.",
                keyboard(("✅ LIVE 전환", "bp:confirm_live")),
            )
        if action == "confirm_live":
            await self._set_btc_pullback_value("trading_mode", TRADING_MODE_LIVE)
            service.reset_reconciliation()
            return "💥 LIVE 전환 완료 (다음 주기에 거래소 상태를 먼저 대조합니다)\n\n" + await self._format_btc_pullback_status(), keyboard()
        if action == "net:testnet":
            await self._set_btc_pullback_value("network", NETWORK_TESTNET)
            service.reset_reconciliation()
            return "🧪 테스트넷(데모)으로 전환\n\n" + await self._format_btc_pullback_status(), keyboard()
        if action == "net:mainnet":
            return (
                "⚠️ 메인넷(실계좌)으로 바꿀까요?\n수량은 0.001 BTC 고정 규칙이 적용됩니다.",
                keyboard(("✅ 메인넷 전환", "bp:confirm_mainnet")),
            )
        if action == "confirm_mainnet":
            await self._set_btc_pullback_value("network", NETWORK_MAINNET)
            service.reset_reconciliation()
            return "💰 메인넷으로 전환\n\n" + await self._format_btc_pullback_status(), keyboard()
        if action == "stats":
            return await self._format_btc_pullback_stats(), keyboard()
        if action == "signals":
            return self._format_btc_pullback_signals(), keyboard()
        if action == "rules":
            return self._btc_pullback_rules_text(cfg), keyboard()
        if action == "reconcile":
            service.reset_reconciliation()
            result = await service.run_cycle()
            return f"🔄 재대조: {result.get('action')} {result.get('reason') or ''}\n\n" + await self._format_btc_pullback_status(), keyboard()
        if action == "close":
            if not holding:
                return "보유 중인 이 전략의 포지션이 없습니다.\n\n" + await self._format_btc_pullback_status(), keyboard()
            return "⚠️ 이 전략의 BTCUSDT 포지션을 시장가(reduce-only)로 청산할까요?", keyboard(("✅ 청산 확인", "bp:confirm_close"))
        if action == "confirm_close":
            result = await service.close_position()
            return f"🔻 청산 요청: {result.get('action')} {result.get('reason') or ''}\n\n" + await self._format_btc_pullback_status(), keyboard()
        return await self._format_btc_pullback_status(), keyboard()

    def _register_btc_pullback_handlers(self, owner_only):
        async def btc_pullback_cmd(update, context):
            service = self._btc_pullback_service()
            await update.message.reply_text(
                await self._format_btc_pullback_status(),
                reply_markup=self._build_btc_pullback_keyboard(service.config()),
            )

        async def btc_pullback_callback(update, context):
            query = update.callback_query
            if not query:
                return
            await _answer_quietly(query)
            action = str(query.data or "").split(":", 1)[-1]
            text, keyboard = await self._handle_btc_pullback_action(action)
            await self._edit_btc_pullback(query, text, keyboard)

        self.tg_app.add_handler(CommandHandler("btcpullback", owner_only(btc_pullback_cmd)))
        self.tg_app.add_handler(CallbackQueryHandler(owner_only(btc_pullback_callback), pattern=r"^bp:"))

    async def _stop_btc_pullback_for_emergency(self):
        """STOP button: strategy OFF and its position closed (LIVE reduce-only / paper)."""
        lines = []
        try:
            cfg = self.cfg.get(CONFIG_KEY, {}) or {}
            was_on = bool(cfg.get("enabled"))
            await self._set_btc_pullback_value("enabled", False)
            result = await self._btc_pullback_service().close_position(reason="emergency_stop")
            if result.get("action") == "none":
                lines.append("BTC 눌림목: OFF" + ("" if was_on else " (이미 꺼져 있었음)"))
            else:
                lines.append(f"BTC 눌림목: OFF · 포지션 청산 {result.get('action')} {result.get('reason') or ''}".strip())
        except Exception as exc:
            logger.exception("BTC pullback emergency stop failed")
            lines.append(f"⚠️ BTC 눌림목 정지 처리 오류: {exc}")
        return lines

    async def _btc_pullback_loop(self):
        await asyncio.sleep(25)
        while True:
            interval = 10
            try:
                service = self._btc_pullback_service()
                interval = int(service.config().get("loop_interval_seconds", 10))
                if service.needs_cycle():
                    await service.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("BTC pullback scheduler cycle failed")
            await asyncio.sleep(max(5, interval))


__all__ = ("ControllerBtcPullbackMixin",)
