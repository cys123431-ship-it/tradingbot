"""Telegram controls (/btcmacross) and scheduler for the BTC SMA3/SMA200 cross strategy."""

from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler

from btc_ema_pullback.config import NETWORK_MAINNET, NETWORK_TESTNET, TRADING_MODE_DRY_RUN, TRADING_MODE_ENV, TRADING_MODE_LIVE
from btc_ma_cross import BtcMaCrossService
from btc_ma_cross.config import CONFIG_KEY, EMERGENCY_STOP_CHOICES, TIMEFRAMES

from .controller_btc_pullback import NETWORK_LABELS, _answer_quietly, _kst, _n
from .strategy_registry import BINANCE_MAINNET, BINANCE_TESTNET

logger = logging.getLogger(__name__)
TIMEFRAME_LABELS = {"15m": "15분", "30m": "30분", "1h": "1시간", "2h": "2시간", "4h": "4시간"}


class ControllerBtcMaCrossMixin:
    def _btc_ma_cross_service(self):
        service = getattr(self, "btc_ma_cross_service", None)
        if service is not None:
            return service

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
            elif "⚠️" in text or "🔒" in text:
                event_type = "PROTECTION_MISSING" if "⚠️" in text else "SL_PROTECTION"
            await self.notify_plain(text, event_type=event_type)

        service = BtcMaCrossService(
            config_getter=lambda: self.cfg.get(CONFIG_KEY, {}) or {},
            credentials_getter=lambda network: self._get_exchange_credentials(
                BINANCE_TESTNET if network == NETWORK_TESTNET else BINANCE_MAINNET
            ) or {},
            exchange_factory=exchange_factory,
            runtime_dir=self.runtime_dir,
            notifier=notifier,
            main_network_getter=main_network_getter,
        )
        self.btc_ma_cross_service = service
        return service

    # ------------------------------------------------------------ rendering
    @staticmethod
    def _build_btc_ma_cross_keyboard(cfg, *, confirm=None):
        def mark(selected, label):
            return f"✅ {label}" if selected else label

        rows = [
            [
                InlineKeyboardButton(mark(cfg.get("enabled"), "▶️ 전략 ON"), callback_data="bm:on"),
                InlineKeyboardButton(mark(not cfg.get("enabled"), "⏹ 신규진입 OFF"), callback_data="bm:off"),
            ],
            [
                InlineKeyboardButton(mark(cfg.get("trading_mode") == TRADING_MODE_DRY_RUN, "🧪 DRY_RUN"), callback_data="bm:mode:dry"),
                InlineKeyboardButton(mark(cfg.get("trading_mode") == TRADING_MODE_LIVE, "💥 LIVE"), callback_data="bm:mode:live"),
            ],
            [
                InlineKeyboardButton(mark(cfg.get("timeframe") == tf, TIMEFRAME_LABELS[tf]), callback_data=f"bm:tf:{tf}")
                for tf in TIMEFRAMES
            ],
            [
                InlineKeyboardButton(
                    mark(int(cfg.get("emergency_stop_roi_percent") or 0) == choice,
                         "비상손절 OFF" if choice == 0 else f"-{choice}%"),
                    callback_data=f"bm:es:{choice}",
                )
                for choice in EMERGENCY_STOP_CHOICES
            ],
            [
                InlineKeyboardButton("📊 상태", callback_data="bm:status"),
                InlineKeyboardButton("📈 성과 통계", callback_data="bm:stats"),
            ],
            [
                InlineKeyboardButton("🧾 최근 판단", callback_data="bm:signals"),
                InlineKeyboardButton("🔄 거래소 재대조", callback_data="bm:reconcile"),
            ],
            [
                InlineKeyboardButton("📘 전략 규칙", callback_data="bm:rules"),
                InlineKeyboardButton("🔻 포지션 청산", callback_data="bm:close"),
            ],
        ]
        if confirm:
            rows.insert(0, [
                InlineKeyboardButton(confirm[0], callback_data=confirm[1]),
                InlineKeyboardButton("취소", callback_data="bm:status"),
            ])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _btc_ma_cross_lock_table(cfg):
        start, step, gap = _n(cfg["lock_start_roi_percent"]), _n(cfg["lock_step_percent"]), _n(cfg["lock_gap_percent"])
        steps = [start + step * i for i in range(4)]
        return " / ".join(f"+{s:g}%→+{s - gap:g}%" for s in steps) + " …"

    async def _format_btc_ma_cross_status(self):
        service = self._btc_ma_cross_service()
        status = await service.status()
        if status.get("unsupported"):
            return (
                "📏 BTC 3/200 SMA 크로스 전략\n"
                "지금 /setup 거래소가 업비트라 사용할 수 없습니다. /setup에서 바이낸스(테스트넷 데모 또는 메인넷)를 선택하세요."
            )
        cfg, state, mode, network = status["config"], status["state"], status["mode"], status["network"]
        trade = state.get("trade") or {}
        stop = int(cfg["emergency_stop_roi_percent"])
        lines = [
            "📏 BTC 3/200 SMA 크로스 전략 (반대 크로스 시 즉시 반대 진입)",
            f"전략: {'ON' if cfg['enabled'] else 'OFF'} · 모드: {mode}"
            + (" (환경변수로 DRY_RUN 강제)" if cfg["trading_mode"] == TRADING_MODE_LIVE and mode == TRADING_MODE_DRY_RUN else ""),
            f"거래소: {NETWORK_LABELS.get(network, network)} (/setup 선택을 따름) · API 키: {'있음' if status['has_credentials'] else '없음'}",
            f"메인 선물 자동매매: {'일시정지' if getattr(self, 'is_paused', False) else '동작 중'} (선물 전략은 하나만 켜짐)",
            f"봉: {TIMEFRAME_LABELS[cfg['timeframe']]} (마감봉 기준) · SMA {cfg['fast_period']}/{cfg['slow_period']}",
            f"레버리지 {cfg['leverage']}x ISOLATED 고정 · 증거금: 가용 잔고의 {_n(cfg['margin_fraction']) * 100:.0f}%",
            f"비상손절: {'OFF' if stop == 0 else f'증거금 -{stop}% (Mark 기준)'}",
            f"수익 확보 손절(증거금 기준, Last 기준 트리거): {self._btc_ma_cross_lock_table(cfg)}",
        ]
        if mode == TRADING_MODE_DRY_RUN and not status["has_credentials"]:
            lines.append(f"가상 잔고: {cfg['dry_run_balance_usdt']} USDT + 가상 손익 (API 키 없음)")
        if trade:
            lines += [
                "",
                f"보유: {trade.get('side')} {trade.get('quantity')} BTC @ {trade.get('entry_price')} "
                f"(증거금 {_n(trade.get('margin_used')):.2f} USDT)",
                f"비상손절 {trade.get('stop_price') or 'OFF'} · 수익 확보 "
                + (f"+{_n(trade.get('lock_roi')):g}% @ {trade.get('lock_price')}" if trade.get("lock_price") else "아직 없음 (+5% 도달 전)"),
            ]
        else:
            lines += ["", "보유: 없음"]
        last = status.get("last_skip") or {}
        if last:
            detail = last.get("skip_reason") or (f"{last.get('side')} 크로스" if last.get("side") else "-")
            lines.append(f"최근 판단 ({_kst(last.get('ts'))}): {detail}")
        if state.get("last_error"):
            lines.append(f"최근 오류: {state['last_error']}")
        lines.append("OFF는 신규 진입만 멈추며, 보유 포지션의 비상손절·수익 확보·반대 크로스 청산은 계속됩니다.")
        return "\n".join(lines)

    async def _format_btc_ma_cross_stats(self):
        status = await self._btc_ma_cross_service().status()
        if status.get("unsupported"):
            return "/setup 거래소가 업비트라 사용할 수 없습니다."
        s = status["stats"]
        pf = s["profit_factor"]
        return "\n".join([
            f"📈 BTC 3/200 SMA 성과 ({status['mode']} · {status['network']})",
            f"거래 {s['total_trades']}회 · 승 {s['wins']} / 패 {s['losses']} · 승률 {s['win_rate'] * 100:.1f}%",
            f"총이익 {s['gross_profit']:+.4f} · 총손실 {s['gross_loss']:+.4f} · 순손익 {s['net_pnl']:+.4f} USDT",
            f"평균 이익 {s['average_win']:+.4f} · 평균 손실 {s['average_loss']:+.4f}",
            f"손익비(PF) {'∞' if pf == float('inf') else f'{pf:.2f}'} · 기대값 {s['expectancy']:+.4f} USDT/회 · "
            f"평균 증거금 수익률 {s['average_r'] * 100:+.2f}%",
            f"최대 연승 {s['max_consecutive_wins']} · 최대 연패 {s['max_consecutive_losses']} · 최대 낙폭 {s['max_drawdown']:.4f} USDT",
            "과거 성과가 미래 수익을 보장하지 않습니다.",
        ])

    def _format_btc_ma_cross_signals(self):
        service = self._btc_ma_cross_service()
        network = service.current_network()
        if not network:
            return "/setup 거래소가 업비트라 사용할 수 없습니다."
        events = service.ledger(network).recent_events(
            8, kinds={"SIGNAL", "SKIP", "ORDER", "EXIT", "FAILSAFE", "PROFIT_LOCK"})
        if not events:
            return "🧾 아직 기록된 판단이 없습니다. (선택한 봉이 마감될 때마다 기록됩니다)"
        lines = ["🧾 최근 판단 (최신순)"]
        for event in events:
            kind = event.get("event")
            if kind == "SIGNAL":
                detail = (f"{event['side']} 크로스" if event.get("side") else event.get("skip_reason")) or "-"
                detail += f" · SMA3 {_n(event.get('sma_fast')):.1f} / SMA200 {_n(event.get('sma_slow')):.1f}"
            elif kind == "ORDER":
                detail = f"{event.get('side')} {event.get('quantity')} @ {event.get('actual_avg_entry')}"
            elif kind == "PROFIT_LOCK":
                detail = f"+{_n(event.get('reached_roi')):g}% 도달 → +{_n(event.get('locked_roi')):g}% 확보 @ {event.get('trigger')}"
            else:
                detail = event.get("skip_reason") or event.get("exit_reason") or event.get("reason") or "-"
            lines.append(f"{_kst(event.get('ts'))} {kind}: {detail}")
        return "\n".join(lines)

    def _btc_ma_cross_rules_text(self, cfg):
        stop = int(cfg["emergency_stop_roi_percent"])
        return (
            "📘 BTC 3/200 SMA 크로스 규칙\n"
            f"• 봉: {TIMEFRAME_LABELS[cfg['timeframe']]} 마감봉 기준 (텔레그램에서 15분~4시간 선택)\n"
            "• 진입: SMA3이 SMA200 상향 돌파 → 롱 / 하향 돌파 → 숏\n"
            "• 보유 중 반대 크로스: 시장가(reduce-only) 청산 후 즉시 반대 방향 진입\n"
            f"• 수익 확보(증거금 기준): {self._btc_ma_cross_lock_table(cfg)} (항상 도달 칸 {cfg['lock_gap_percent']}% 아래, 한 번 올라간 손절은 내려가지 않음)\n"
            "• 수익 확보 손절로 청산되면 다음 크로스까지 대기\n"
            f"• 비상손절: {'OFF' if stop == 0 else f'증거금 -{stop}%'} (메뉴에서 선택, 거래소에 Mark 기준으로 걸림)\n"
            f"• 레버리지 {cfg['leverage']}배 고정, 가용 증거금의 {_n(cfg['margin_fraction']) * 100:.0f}% 사용, BTCUSDT 1포지션\n"
            "• 거래소는 /setup 선택을 따르고, 켜면 다른 선물 자동전략은 멈춤\n"
            "• 수익을 보장하지 않는 전략입니다."
        )

    async def _edit_btc_ma_cross(self, query, text, keyboard):
        try:
            await query.edit_message_text(text, reply_markup=keyboard)
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    async def _set_btc_ma_cross_value(self, key, value):
        await self.cfg.update_value([CONFIG_KEY, key], value)

    async def _handle_btc_ma_cross_action(self, action):
        service = self._btc_ma_cross_service()

        def keyboard(confirm=None):
            return self._build_btc_ma_cross_keyboard(service.config(), confirm=confirm)

        cfg = service.config()
        network = service.current_network(cfg)
        if not network:
            return await self._format_btc_ma_cross_status(), keyboard()
        holding = bool(service.load_state(network, service.mode(cfg)).get("trade"))

        if action == "on":
            return (
                "⚠️ BTC 3/200 SMA 크로스 전략을 켤까요?\n"
                f"모드 {service.mode(cfg)} · {NETWORK_LABELS[network]} (/setup) · {TIMEFRAME_LABELS[cfg['timeframe']]}\n"
                "켜면 메인 선물 자동매매와 BTC 눌림목 전략의 신규 진입이 멈춥니다.\n"
                + ("실제 주문이 나갑니다." if service.mode(cfg) == TRADING_MODE_LIVE else "DRY_RUN: 실제 주문 없이 가상 체결만 기록합니다."),
                keyboard(("✅ 전략 시작", "bm:confirm_on")),
            )
        if action == "confirm_on":
            await self._set_btc_ma_cross_value("enabled", True)
            was_running = not getattr(self, "is_paused", False)
            self.is_paused = True
            turn_off = getattr(self, "_turn_off_standalone_futures_strategies", None)
            if callable(turn_off):
                await turn_off(except_key=CONFIG_KEY)
            if service.mode() == TRADING_MODE_LIVE and network == NETWORK_MAINNET:
                options_off = getattr(self, "_turn_off_options_for_futures_strategy", None)
                if callable(options_off):
                    await options_off(include_btc_pullback=False)
            note = "\n메인 선물 자동매매 신규 진입을 일시정지했습니다 (보유 포지션 관리는 계속)." if was_running else ""
            return "✅ BTC 3/200 SMA 전략 ON" + note + "\n\n" + await self._format_btc_ma_cross_status(), keyboard()
        if action == "off":
            await self._set_btc_ma_cross_value("enabled", False)
            return "⏹ 신규 진입 OFF (보유 포지션 관리는 계속)\n\n" + await self._format_btc_ma_cross_status(), keyboard()
        if action.startswith(("mode:", "tf:")) and holding:
            return "⚠️ 포지션 보유 중에는 모드/봉을 바꿀 수 없습니다. 먼저 정리하세요.", keyboard()
        if action == "mode:dry":
            await self._set_btc_ma_cross_value("trading_mode", TRADING_MODE_DRY_RUN)
            service.reset_reconciliation()
            return "🧪 DRY_RUN으로 전환 (실제 주문 없음)\n\n" + await self._format_btc_ma_cross_status(), keyboard()
        if action == "mode:live":
            return (
                "⚠️ LIVE로 전환할까요? 실제 주문이 나갑니다.\n"
                f"거래소: {NETWORK_LABELS[network]} (/setup)\n"
                f"환경변수 {TRADING_MODE_ENV}=DRY_RUN 이 설정돼 있으면 계속 DRY_RUN으로 동작합니다.",
                keyboard(("✅ LIVE 전환", "bm:confirm_live")),
            )
        if action == "confirm_live":
            await self._set_btc_ma_cross_value("trading_mode", TRADING_MODE_LIVE)
            service.reset_reconciliation()
            return "💥 LIVE 전환 완료 (다음 주기에 거래소 상태를 먼저 대조합니다)\n\n" + await self._format_btc_ma_cross_status(), keyboard()
        if action.startswith("tf:"):
            timeframe = action.split(":", 1)[1]
            if timeframe in TIMEFRAMES:
                await self._set_btc_ma_cross_value("timeframe", timeframe)
            return f"🕒 봉: {TIMEFRAME_LABELS.get(timeframe, timeframe)}\n\n" + await self._format_btc_ma_cross_status(), keyboard()
        if action.startswith("es:"):
            try:
                choice = int(action.split(":", 1)[1])
            except ValueError:
                choice = -1
            if choice in EMERGENCY_STOP_CHOICES:
                await self._set_btc_ma_cross_value("emergency_stop_roi_percent", choice)
            note = "\n(보유 중인 포지션은 기존 비상손절 유지, 다음 진입부터 적용)" if holding else ""
            label = "OFF" if choice == 0 else f"증거금 -{choice}%"
            return f"🛑 비상손절: {label}{note}\n\n" + await self._format_btc_ma_cross_status(), keyboard()
        if action == "stats":
            return await self._format_btc_ma_cross_stats(), keyboard()
        if action == "signals":
            return self._format_btc_ma_cross_signals(), keyboard()
        if action == "rules":
            return self._btc_ma_cross_rules_text(cfg), keyboard()
        if action == "reconcile":
            service.reset_reconciliation()
            result = await service.run_cycle()
            return f"🔄 재대조: {result.get('action')} {result.get('reason') or ''}\n\n" + await self._format_btc_ma_cross_status(), keyboard()
        if action == "close":
            if not holding:
                return "보유 중인 이 전략의 포지션이 없습니다.\n\n" + await self._format_btc_ma_cross_status(), keyboard()
            return "⚠️ 이 전략의 BTCUSDT 포지션을 시장가(reduce-only)로 청산할까요?", keyboard(("✅ 청산 확인", "bm:confirm_close"))
        if action == "confirm_close":
            result = await service.close_position()
            return f"🔻 청산 요청: {result.get('action')} {result.get('reason') or ''}\n\n" + await self._format_btc_ma_cross_status(), keyboard()
        return await self._format_btc_ma_cross_status(), keyboard()

    def _register_btc_ma_cross_handlers(self, owner_only):
        async def btc_ma_cross_cmd(update, context):
            await update.message.reply_text(
                await self._format_btc_ma_cross_status(),
                reply_markup=self._build_btc_ma_cross_keyboard(self._btc_ma_cross_service().config()),
            )

        async def btc_ma_cross_callback(update, context):
            query = update.callback_query
            if not query:
                return
            await _answer_quietly(query)
            action = str(query.data or "").split(":", 1)[-1]
            text, keyboard = await self._handle_btc_ma_cross_action(action)
            await self._edit_btc_ma_cross(query, text, keyboard)

        self.tg_app.add_handler(CommandHandler("btcmacross", owner_only(btc_ma_cross_cmd)))
        self.tg_app.add_handler(CallbackQueryHandler(owner_only(btc_ma_cross_callback), pattern=r"^bm:"))

    async def _stop_btc_ma_cross_for_emergency(self):
        lines = []
        try:
            was_on = bool((self.cfg.get(CONFIG_KEY, {}) or {}).get("enabled"))
            await self._set_btc_ma_cross_value("enabled", False)
            result = await self._btc_ma_cross_service().close_position(reason="emergency_stop")
            if result.get("action") == "none":
                lines.append("BTC 3/200 SMA: OFF" + ("" if was_on else " (이미 꺼져 있었음)"))
            else:
                lines.append(f"BTC 3/200 SMA: OFF · 포지션 청산 {result.get('action')} {result.get('reason') or ''}".strip())
        except Exception as exc:
            logger.exception("BTC MA cross emergency stop failed")
            lines.append(f"⚠️ BTC 3/200 SMA 정지 처리 오류: {exc}")
        return lines

    async def _btc_ma_cross_loop(self):
        await asyncio.sleep(30)
        while True:
            interval = 10
            try:
                service = self._btc_ma_cross_service()
                interval = int(service.config().get("loop_interval_seconds", 10))
                if service.needs_cycle():
                    await service.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("BTC MA cross scheduler cycle failed")
            await asyncio.sleep(max(5, interval))


__all__ = ("ControllerBtcMaCrossMixin",)
