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

from .controller_btc_pullback import NETWORK_LABELS, _answer_quietly, _kst, _n

logger = logging.getLogger(__name__)
TIMEFRAME_LABELS = {"15m": "15분", "30m": "30분", "1h": "1시간", "2h": "2시간", "4h": "4시간"}
NO_FEATURE_TEXT = (
    "🚫 이 전략은 C(변동성 타겟 사이징)가 고정 선택이라 이 상태가 나오면 안 됩니다.\n"
    "설정이 손상되었을 수 있으니 /macross 를 다시 열어 주세요."
)


def _feature_summary(cfg):
    mode = feature_mode(cfg)
    if not mode:
        return "선택 없음 (동작 안 함)"
    return " + ".join(f"{name}({FEATURE_LABELS[name]})" for name in mode)


class ControllerMaCrossMixin:
    # ----------------------------------------------------------- config access
    def _ma_cross_config(self):
        return normalize_ma_cross_config(self.cfg.get(CONFIG_KEY, {}) or {})

    async def _set_ma_cross_value(self, key, value):
        await self.cfg.update_value([CONFIG_KEY, key], value)

    async def _toggle_ma_cross_feature(self, name):
        """Toggle one feature; refuses to leave every feature OFF."""
        cfg = self._ma_cross_config()
        field = FEATURE_FIELDS[name]
        current = bool(cfg.get(field))
        if current and feature_mode(cfg) == name:
            return False, "LAST_FEATURE"
        await self._set_ma_cross_value(field, 0 if current else 1)
        return True, ""

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
            "🧩 /macross — SMA3 / SMA120 + 변동성 타겟 사이징 (C 전용)",
            f"전략: {'ON' if enabled else 'OFF'} · 모드: {cfg.get('trading_mode')} · 봉: {TIMEFRAME_LABELS[cfg['timeframe']]}",
            f"기능: {_feature_summary(cfg)}",
            "청산: 반대 크로스 시 종료 · 수익잠금 +5%→+4% 계단 · 비상손절 OFF",
            f"레버리지: {cfg['leverage']}x ISOLATED · 증거금: 가용 잔고 {_n(cfg['margin_fraction']) * 100:.0f}%",
            f"종목: {'BTC만' if cfg['scan_scope'] == 'btc' else '알트 상위' + str(cfg['alt_universe_size'])}",
        ]
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
            if scope in ("btc", "alts"):
                await self._set_ma_cross_value("scan_scope", scope)
            return f"🔎 종목: {self._ma_cross_config()['scan_scope']}\n\n" + self._ma_cross_status_text(), keyboard()
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

        self.tg_app.add_handler(CommandHandler("macross", owner_only(ma_cross_cmd)))
        self.tg_app.add_handler(CallbackQueryHandler(owner_only(ma_cross_callback), pattern=r"^mc:"))


__all__ = ("ControllerMaCrossMixin",)
