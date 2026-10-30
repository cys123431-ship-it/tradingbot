"""Telegram controls (/macross) and scheduler for the SMA3/SMA120 A~E strategy."""

from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler

from btc_ema_pullback.config import TRADING_MODE_DRY_RUN, TRADING_MODE_ENV, TRADING_MODE_LIVE
from btc_ma_cross_plus import (
    CONFIG_KEY,
    FEATURES,
    FEATURE_FIELDS,
    FEATURE_LABELS,
    feature_mode,
    normalize_ma_cross_config,
)
from btc_ma_cross_plus.config import LEVERAGE_CHOICES
from trading_safety.sma_periods import (
    MA_CROSS_PERIOD_COMMANDS,
    PERIOD_PRESETS,
    PeriodError,
    is_default_periods,
    parse_period_command,
    periods_label,
    required_candle_history,
)
from btc_ma_cross_plus.service import MaCrossService
from btc_ma_cross_plus.tradfi import (
    TRADFI_MAINNET_ONLY_REASON,
    TRADFI_SCOPE,
    TRADFI_SESSION_CLOSED_REASON,
)

from .controller_btc_pullback import NETWORK_LABELS, _answer_quietly, _kst, _n

logger = logging.getLogger(__name__)
TIMEFRAME_LABELS = {"15m": "15분", "30m": "30분", "1h": "1시간", "2h": "2시간", "4h": "4시간"}
NO_FEATURE_TEXT = (
    "🚫 이 전략은 C(변동성 타겟 사이징)가 고정 선택이라 이 상태가 나오면 안 됩니다.\n"
    "설정이 손상되었을 수 있으니 /macross 를 다시 열어 주세요."
)
TRADFI_KO = {
    "weekend": "주말 휴장",
    "holiday": "미국 휴장일",
    "outside_regular_session": "정규장 시간 아님",
    "regular_session_open": "정규장 진행 중",
}


def _scope_text(cfg):
    if cfg.get("scan_scope") == TRADFI_SCOPE:
        return (f"🏦 TradFi만 — 바이낸스 메인넷 미국주식 + 금 "
                f"(24h 거래대금 상위 {cfg.get('alt_universe_size', 50)}, 미국 정규장에만 신규 진입)")
    if cfg.get("scan_scope") == "alts":
        return f"🌐 알트 전용 24h 거래대금 상위 {cfg.get('alt_universe_size', 50)}개 (BTC 제외)"
    if cfg.get("scan_scope") == "single":
        return f"🎯 지정 코인 {cfg.get('single_symbol') or 'BTCUSDT'}만"
    return "🪙 BTC만"


def _us_session_text(session):
    """One-line US regular-session status for the Telegram status screen."""
    if not session:
        return "확인 불가"
    reason = str(session.get("reason") or "")
    label = TRADFI_KO.get(reason, reason or "-")
    if session.get("open"):
        return f"✅ {label} ({str(session.get('regular_close', ''))[:19]})"
    return f"⏸ {label} (다음 정규장 {str(session.get('regular_open', ''))[:19]})"


def _feature_summary(cfg):
    mode = feature_mode(cfg)
    if not mode:
        return "선택 없음 (동작 안 함)"
    return " + ".join(f"{name}({FEATURE_LABELS[name]})" for name in mode)


def _sma_button(cfg, mark, fast, slow, prefix):
    """One SMA preset button; highlighted when the live pair matches."""
    current = (int(cfg.get("fast_period") or 0), int(cfg.get("slow_period") or 0))
    return InlineKeyboardButton(
        mark(current == (fast, slow), f"SMA{fast}/SMA{slow}"),
        callback_data=f"{prefix}:sma:{fast}:{slow}",
    )


class ControllerMaCrossMixin:
    # ----------------------------------------------------------- config access
    def _ma_cross_config(self):
        return normalize_ma_cross_config(self.cfg.get(CONFIG_KEY, {}) or {})

    async def _set_ma_cross_value(self, key, value):
        await self.cfg.update_value([CONFIG_KEY, key], value)

    def _ma_cross_network(self):
        """Network the /setup exchange maps to (None for Upbit / unwired)."""
        from btc_ema_pullback.config import NETWORK_MAINNET, NETWORK_TESTNET
        from .strategy_registry import BINANCE_MAINNET, BINANCE_TESTNET

        getter = getattr(self, "get_exchange_mode", None)
        mode = getter() if callable(getter) else None
        if mode == BINANCE_MAINNET:
            return NETWORK_MAINNET
        if mode == BINANCE_TESTNET:
            return NETWORK_TESTNET
        return None

    def _ma_cross_tradfi_refusal(self):
        """Message when TradFi cannot be used on the current exchange, else ''."""
        network = self._ma_cross_network()
        if network and MaCrossService.tradfi_network_allowed(network):
            return ""
        network_label = NETWORK_LABELS.get(network, network or "업비트")
        return (
            "🚫 TradFi 전용은 바이낸스 메인넷에서만 사용할 수 있습니다.\n"
            f"지금 거래소: {network_label} (/setup 선택을 따름)\n"
            "테스트넷(데모)에는 미국주식 TradFi 계약이 거의 없어 모드를 켜지 않습니다.\n"
            "/setup에서 바이낸스 메인넷으로 바꾼 뒤 다시 선택하세요."
        )

    async def _toggle_ma_cross_feature(self, name):
        """Toggle one feature; refuses to leave every feature OFF."""
        cfg = self._ma_cross_config()
        field = FEATURE_FIELDS[name]
        current = bool(cfg.get(field))
        if current and feature_mode(cfg) == name:
            return False, "LAST_FEATURE"
        await self._set_ma_cross_value(field, 0 if current else 1)
        return True, ""

    # ------------------------------------------------------------ service
    def _ma_cross_service(self):
        """Live service reused from the 3/200 SMA plumbing, TradFi-capable."""
        service = getattr(self, "ma_cross_service", None)
        if service is not None:
            return service

        from btc_ema_pullback.config import NETWORK_MAINNET, NETWORK_TESTNET
        from .strategy_registry import BINANCE_MAINNET, BINANCE_TESTNET

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
            await self.notify_plain(text, event_type=event_type)

        service = MaCrossService(
            config_getter=lambda: self.cfg.get(CONFIG_KEY, {}) or {},
            credentials_getter=lambda network: self._get_exchange_credentials(
                BINANCE_TESTNET if network == NETWORK_TESTNET else BINANCE_MAINNET
            ) or {},
            exchange_factory=exchange_factory,
            runtime_dir=self.runtime_dir,
            notifier=notifier,
            main_network_getter=main_network_getter,
        )
        self.ma_cross_service = service
        return service

    # --------------------------------------------------------------- keyboard
    @staticmethod
    def _build_ma_cross_keyboard(cfg, *, confirm=None):
        def mark(selected, label):
            return f"✅ {label}" if selected else label

        # UI is C-only: the other feature blocks stay in the code for reference but
        # are never selected from Telegram.
        feature_rows = [
            [InlineKeyboardButton(mark(True, f"C · {FEATURE_LABELS['C']} (고정)"), callback_data="mc:status")]
        ]
        mode = feature_mode(cfg)
        rows = [
            [
                InlineKeyboardButton(mark(cfg.get("enabled"), "▶️ 전략 ON"), callback_data="mc:on"),
                InlineKeyboardButton(mark(not cfg.get("enabled"), "⏹ 신규진입 OFF"), callback_data="mc:off"),
            ],
            [
                InlineKeyboardButton(mark(cfg.get("trading_mode") == TRADING_MODE_DRY_RUN, "🧪 DRY_RUN"),
                                     callback_data="mc:mode:dry"),
                InlineKeyboardButton(mark(cfg.get("trading_mode") == TRADING_MODE_LIVE, "💥 LIVE"),
                                     callback_data="mc:mode:live"),
            ],
            [
                _sma_button(cfg, mark, fast, slow, "mc")
                for label, fast, slow in PERIOD_PRESETS[:3]
            ],
            [
                _sma_button(cfg, mark, fast, slow, "mc")
                for label, fast, slow in PERIOD_PRESETS[3:]
            ],
            [
                InlineKeyboardButton("✏️ 직접 입력: /map 1 200", callback_data="mc:sma_help"),
            ],
            [InlineKeyboardButton(f"🧩 기능: {mode or '없음'} · 청산: 반대 크로스만", callback_data="mc:status")],
            *feature_rows,
            [
                InlineKeyboardButton(mark(cfg.get("timeframe") == tf, TIMEFRAME_LABELS[tf]),
                                     callback_data=f"mc:tf:{tf}")
                for tf in ("1h", "2h", "4h")
            ],
            [
                InlineKeyboardButton(mark(int(cfg.get("leverage") or 5) == lev, f"{lev}x"),
                                     callback_data=f"mc:lev:{lev}")
                for lev in LEVERAGE_CHOICES
            ],
            [
                InlineKeyboardButton(mark(cfg.get("scan_scope") == "btc", "🪙 BTC만"), callback_data="mc:scope:btc"),
                InlineKeyboardButton(mark(cfg.get("scan_scope") == "alts", "🌐 알트 상위50"),
                                     callback_data="mc:scope:alts"),
            ],
            [
                InlineKeyboardButton(mark(cfg.get("scan_scope") == TRADFI_SCOPE, "🏦 TradFi만 (메인넷)"),
                                     callback_data="mc:scope:tradfi"),
            ],
            [
                InlineKeyboardButton("📊 상태", callback_data="mc:status"),
                InlineKeyboardButton("📘 전략 규칙", callback_data="mc:rules"),
            ],
            [
                InlineKeyboardButton("🔄 거래소 재대조", callback_data="mc:reconcile"),
                InlineKeyboardButton("📈 최근 판단", callback_data="mc:signals"),
            ],
        ]
        if confirm:
            rows.insert(0, [
                InlineKeyboardButton(confirm[0], callback_data=confirm[1]),
                InlineKeyboardButton("취소", callback_data="mc:status"),
            ])
        return InlineKeyboardMarkup(rows)

    # ------------------------------------------------------------------ status
    def _ma_cross_status_text(self):
        cfg = self._ma_cross_config()
        mode = feature_mode(cfg)
        enabled = bool(cfg.get("enabled"))
        lines = [
            f"🧩 /macross — SMA{cfg['fast_period']} / SMA{cfg['slow_period']} + 변동성 타겟 사이징 (C 전용)",
            f"전략: {'ON' if enabled else 'OFF'} · 모드: {cfg.get('trading_mode')} · 봉: {TIMEFRAME_LABELS[cfg['timeframe']]}",
            f"기능: {_feature_summary(cfg)}",
            "청산: 반대 크로스 시 종료 · 수익잠금 +5%→+4% 계단 · 비상손절 OFF",
            f"레버리지: {cfg['leverage']}x ISOLATED · 증거금: 가용 잔고 {_n(cfg['margin_fraction']) * 100:.0f}%",
            f"종목: {_scope_text(cfg)}",
        ]
        if cfg.get("scan_scope") == TRADFI_SCOPE:
            network = self._ma_cross_network()
            service = self._ma_cross_service()
            scope_state = {"allowed": False, "detail": "업비트에서는 사용할 수 없습니다."}
            if network:
                try:
                    scope_state = service.tradifi_scope_state(network)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("macross TradFi status lookup failed: %s", exc)
                    scope_state = {"allowed": False, "detail": f"상태 확인 실패 ({type(exc).__name__})"}
            lines.append(
                f"🏦 TradFi 신규 진입: {'가능' if scope_state.get('allowed') else '차단 — ' + str(scope_state.get('detail'))}"
            )
            if network:
                try:
                    lines.append(f"미국 정규장: {_us_session_text(service.us_session_status())}")
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("macross US session lookup failed: %s", exc)
        if not mode:
            lines += ["", NO_FEATURE_TEXT]
        elif enabled:
            lines.append("이 전략이 켜져 있으면 다른 선물 전략은 자동 OFF 됩니다.")
        return "\n".join(lines)

    @staticmethod
    def _ma_cross_rules_text(cfg):
        lines = [
            "📘 /macross 규칙 (C 전용)",
            f"· 신호: SMA{cfg['fast_period']} / SMA{cfg['slow_period']} 크로스 (완료된 {TIMEFRAME_LABELS[cfg['timeframe']]}봉)",
            "· 상향 돌파 → LONG · 하향 돌파 → SHORT",
            "",
            "C · 변동성 타겟 사이징",
            f"  수량 = 잔고 × {_n(cfg['margin_fraction']) * 100:.0f}% × 변동성배율 × {cfg['leverage']}x ÷ 가격",
            f"  변동성배율 = 목표 {float(cfg['feature_c_target_atr_pct']) * 100:g}% ÷ (ATR/가격), "
            f"범위 {float(cfg['feature_c_min_scale']):g}~{float(cfg['feature_c_max_scale']):g}",
            "  → 조용한 장에서 크게, 요동치는 장에서 작게",
            "",
            "청산",
            "· 반대 크로스 발생 시 포지션 종료 (시장가)",
            f"· 수익잠금: 최고 증거금 ROI +{float(cfg['lock_start_roi_percent']):g}% 도달 → "
            f"+{float(cfg['lock_start_roi_percent']) - float(cfg['lock_gap_percent']):g}%에 Stop, "
            f"이후 +{float(cfg['lock_step_percent']):g}%마다 한 칸 상향",
            "· 비상손절: OFF (반대 크로스 전까지 손실을 그대로 보유)",
            "",
            "참고: 이 전략은 다른 선물 전략과 동시에 켜지지 않습니다.",
        ]
        return "\n".join(lines)

    # ----------------------------------------------------------------- actions
    async def _set_ma_cross_periods(self, fast, slow):
        """Store a validated SMA pair; returns (ok, message)."""
        try:
            fast_p, slow_p = parse_period_command([fast, slow])
        except PeriodError as exc:
            return False, f"⚠️ 기간 변경 실패: {exc}\n예) /map 1 200"
        cfg_before = self._ma_cross_config()
        await self._set_ma_cross_value("fast_period", fast_p)
        await self._set_ma_cross_value("slow_period", slow_p)
        needed = required_candle_history(slow_p)
        if int(cfg_before.get("candle_history") or 0) < needed:
            await self._set_ma_cross_value("candle_history", needed)
        cfg = self._ma_cross_config()
        network = self._ma_cross_network()
        holding = False
        if network:
            service = self._ma_cross_service()
            try:
                holding = bool(service.load_state(network, service.mode(cfg)).get("trade"))
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("macross holding lookup failed: %s", exc)
        note = "\n(보유 중인 포지션은 진입 당시 기간 기준으로 관리, 다음 진입부터 새 기간 적용)" if holding else ""
        if not is_default_periods(fast_p, slow_p, default_fast=3, default_slow=120):
            note += "\n(참고: 이 전략의 백테스트 최적 조합은 3/120입니다 — 실제 성과 검증 없이 바꾸지 마세요)"
        return True, (
            f"✅ SMA 기간 변경: {periods_label(fast_p, slow_p)} "
            f"(패스트 {fast_p} · 슬로우 {slow_p})\n"
            f"• 봉 {cfg['timeframe']} 기준 · 신호 계산에 {cfg['candle_history']}봉 사용{note}"
        )

    async def _handle_ma_cross_action(self, action):
        cfg = self._ma_cross_config()
        keyboard = lambda confirm=None: self._build_ma_cross_keyboard(self._ma_cross_config(), confirm=confirm)  # noqa: E731

        if action == "on":
            if not feature_mode(cfg):
                return NO_FEATURE_TEXT + "\n\nA~E 중 하나 이상을 먼저 선택하세요.", keyboard()
            return (
                "⚠️ /macross 전략을 켤까요?\n"
                f"모드: {cfg['trading_mode']} · {TIMEFRAME_LABELS[cfg['timeframe']]} · 기능 {feature_mode(cfg)}\n"
                "켜면 다른 선물 전략(3/200 SMA, BTC 눌림목)의 신규 진입이 OFF 됩니다.",
                keyboard(("✅ 전략 시작", "mc:confirm_on")),
            )
        if action == "confirm_on":
            if not feature_mode(cfg):
                return NO_FEATURE_TEXT, keyboard()
            await self._set_ma_cross_value("enabled", True)
            turn_off = getattr(self, "_turn_off_standalone_futures_strategies", None)
            if callable(turn_off):
                await turn_off(except_key=CONFIG_KEY)
            self.is_paused = True
            return "✅ /macross ON — 다른 선물 전략은 OFF 했습니다.\n\n" + self._ma_cross_status_text(), keyboard()
        if action == "off":
            await self._set_ma_cross_value("enabled", False)
            return "⏹ /macross 신규 진입 OFF (보유 포지션은 계속 관리)\n\n" + self._ma_cross_status_text(), keyboard()
        if action.startswith("feat:"):
            return (
                "🧩 이 전략은 C(변동성 타겟 사이징) 전용입니다.\n"
                "기능 선택은 고정되어 있고, A·B·D·E는 사용하지 않습니다.\n\n"
                + self._ma_cross_status_text()
            ), keyboard()
        if action.startswith("tf:"):
            tf = action.split(":", 1)[1]
            if tf in TIMEFRAME_LABELS:
                await self._set_ma_cross_value("timeframe", tf)
            return f"🕒 봉: {TIMEFRAME_LABELS[self._ma_cross_config()['timeframe']]}\n\n" + self._ma_cross_status_text(), keyboard()
        if action.startswith("lev:"):
            try:
                lev = int(action.split(":", 1)[1])
            except ValueError:
                lev = -1
            if lev in LEVERAGE_CHOICES:
                await self._set_ma_cross_value("leverage", lev)
            return f"⚙️ 레버리지: {self._ma_cross_config()['leverage']}x\n\n" + self._ma_cross_status_text(), keyboard()
        if action.startswith("scope:"):
            scope = action.split(":", 1)[1]
            if scope == TRADFI_SCOPE:
                refusal = self._ma_cross_tradfi_refusal()
                if refusal:
                    return refusal, keyboard()
                await self._set_ma_cross_value("scan_scope", TRADFI_SCOPE)
                try:
                    session = self._ma_cross_service().us_session_status()
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("macross US session lookup failed: %s", exc)
                    session = {}
                return (
                    f"🏦 TradFi만 선택: {_scope_text(self._ma_cross_config())}\n"
                    f"• 미국 정규장에서만 신규 진입 · 지금: {_us_session_text(session)}\n"
                    "• 장 마감·주말·휴장에는 새 진입을 하지 않고, 보유 포지션의 반대 크로스 청산과 "
                    "수익잠금은 계속 동작합니다.\n\n"
                    + self._ma_cross_status_text()
                ), keyboard()
            if scope in ("btc", "alts"):
                await self._set_ma_cross_value("scan_scope", scope)
            return f"🔎 종목: {_scope_text(self._ma_cross_config())}\n\n" + self._ma_cross_status_text(), keyboard()
        if action == "mode:live":
            return (
                "⚠️ LIVE로 전환할까요? 실제 주문이 나갑니다.\n"
                f"환경변수 {TRADING_MODE_ENV}=DRY_RUN 이면 계속 DRY_RUN으로 동작합니다.",
                keyboard(("✅ LIVE 전환", "mc:confirm_live")),
            )
        if action == "mode:dry":
            await self._set_ma_cross_value("trading_mode", TRADING_MODE_DRY_RUN)
            return "🧪 DRY_RUN\n\n" + self._ma_cross_status_text(), keyboard()
        if action == "confirm_live":
            await self._set_ma_cross_value("trading_mode", TRADING_MODE_LIVE)
            return "💥 LIVE 전환 완료\n\n" + self._ma_cross_status_text(), keyboard()
        if action == "rules":
            return self._ma_cross_rules_text(cfg), keyboard()
        if action == "sma_help":
            presets = " · ".join(label for label, _, _ in PERIOD_PRESETS)
            return (
                f"✏️ SMA 기간 직접 입력\n지금: {periods_label(cfg['fast_period'], cfg['slow_period'])}\n\n"
                "명령: /map <패스트> <슬로우>  (또는 /maperiod)\n"
                "예) /map 1 200 → 패스트 1, 슬로우 200\n"
                "예) /map 5 60 → 패스트 5, 슬로우 60\n\n"
                f"버튼 프리셋: {presets}\n"
                "• 패스트는 슬로우보다 작아야 합니다 (1 이상, 슬로우 500 이하).\n"
                "• 보유 중에 바꾸면 다음 진입부터 적용되고, "
                "보유 포지션은 진입 당시 기간으로 계속 관리됩니다.\n"
                "• 이 전략의 백테스트 최적 조합은 3/120입니다.",
                keyboard(),
            )
        if action.startswith("sma:"):
            parts = action.split(":")
            if len(parts) != 3:
                return "⚠️ 알 수 없는 SMA 설정입니다.", keyboard()
            _, message = await self._set_ma_cross_periods(parts[1], parts[2])
            return message + "\n\n" + self._ma_cross_status_text(), keyboard()
        return self._ma_cross_status_text(), keyboard()

    # -------------------------------------------------------------- handlers
    def _register_ma_cross_handlers(self, owner_only):
        async def ma_cross_cmd(update, context):
            await update.message.reply_text(
                self._ma_cross_status_text(),
                reply_markup=self._build_ma_cross_keyboard(self._ma_cross_config()),
            )

        async def ma_cross_callback(update, context):
            query = update.callback_query
            if not query:
                return
            await _answer_quietly(query)
            action = str(query.data or "").split(":", 1)[-1]
            text, keyboard = await self._handle_ma_cross_action(action)
            try:
                await query.edit_message_text(text, reply_markup=keyboard)
            except BadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    raise

        async def ma_cross_period_cmd(update, context):
            args = list(getattr(context, "args", None) or [])
            if not args:
                _, help_text = await self._handle_ma_cross_action("sma_help")
                await update.message.reply_text(
                    help_text, reply_markup=self._build_ma_cross_keyboard(self._ma_cross_config()))
                return
            try:
                fast_p, slow_p = parse_period_command(args)
            except PeriodError as exc:
                cfg = self._ma_cross_config()
                await update.message.reply_text(
                    f"⚠️ 기간 변경 실패: {exc}\n예) /map 1 200\n"
                    f"지금: {periods_label(cfg['fast_period'], cfg['slow_period'])}"
                )
                return
            _, message = await self._set_ma_cross_periods(fast_p, slow_p)
            await update.message.reply_text(
                message + "\n\n" + self._ma_cross_status_text(),
                reply_markup=self._build_ma_cross_keyboard(self._ma_cross_config()),
            )

        self.tg_app.add_handler(CommandHandler("macross", owner_only(ma_cross_cmd)))
        for name in MA_CROSS_PERIOD_COMMANDS:
            self.tg_app.add_handler(CommandHandler(name, owner_only(ma_cross_period_cmd)))
        self.tg_app.add_handler(CallbackQueryHandler(owner_only(ma_cross_callback), pattern=r"^mc:"))


__all__ = ("ControllerMaCrossMixin",)
