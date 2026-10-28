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
    "🚫 기능(A~E)을 최소 1개 선택해야 합니다.\n"
    "지금은 A~E가 모두 OFF라 이 전략은 신호를 만들지 않고 진입하지 않습니다."
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

        feature_rows = [
            [InlineKeyboardButton(mark(bool(cfg.get(FEATURE_FIELDS[name])), f"{name} · {FEATURE_LABELS[name]}"),
                                  callback_data=f"mc:feat:{name}")]
            for name in FEATURES
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
            [InlineKeyboardButton(f"🧩 기능: {mode or '없음(동작 안 함)'} — 최소 1개 선택", callback_data="mc:status")],
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
            "🧩 /macross — SMA3 / SMA120 + 기능 선택",
            f"전략: {'ON' if enabled else 'OFF'} · 모드: {cfg.get('trading_mode')} · 봉: {TIMEFRAME_LABELS[cfg['timeframe']]}",
            f"기능: {_feature_summary(cfg)}",
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
        mode = feature_mode(cfg)
        lines = [
            "📘 /macross 규칙",
            f"· 신호: SMA{cfg['fast_period']} / SMA{cfg['slow_period']} 크로스 (마감봉)",
            f"· 봉: {TIMEFRAME_LABELS[cfg['timeframe']]} · 레버리지 {cfg['leverage']}x",
            f"· 선택된 기능: {mode or '없음 (동작 안 함)'}",
            "",
            "A · 추세필터: SMA120 방향 일치 + ADX·효율 임계 통과",
            f"     ADX ≥ {float(cfg['feature_a_adx_min']):g}, 효율 ≥ {float(cfg['feature_a_efficiency_min']):g}",
            "B · ATR 초기손절: 진입 즉시 진입가 ± ATR×배수 손절",
            f"     배수 {float(cfg['feature_b_atr_multiplier']):g}",
            "C · 변동성 타겟 사이징: ATR/가격 비율로 수량 조절",
            f"     목표 {float(cfg['feature_c_target_atr_pct']):g}, 배율 {float(cfg['feature_c_min_scale']):g}~{float(cfg['feature_c_max_scale']):g}",
            "D · ATR 샹들리에: 고점/저점 기준 트레일링(수익 고정 대신)",
            f"     배수 {float(cfg['feature_d_atr_multiplier']):g}",
            "E · Donchian 돌파: N봉 최고/최저 돌파를 추가 트리거로",
            f"     {cfg['feature_e_period']}봉",
            "",
            "· 보유 중 반대 신호 시 시장가 청산 후 반대 진입",
            "· A~E는 독립 스위치이며 조합해서 시험할 수 있습니다.",
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
            name = action.split(":", 1)[1].upper()
            if name not in FEATURES:
                return self._ma_cross_status_text(), keyboard()
            changed, _ = await self._toggle_ma_cross_feature(name)
            cfg = self._ma_cross_config()
            if not changed:
                text = f"🚫 마지막 남은 기능({name})은 끌 수 없습니다. 최소 1개는 선택해야 합니다."
            else:
                state = "ON" if cfg[FEATURE_FIELDS[name]] else "OFF"
                text = f"🧩 기능 {name} {state} — 현재: {feature_mode(cfg) or '없음'}"
            return text + "\n\n" + self._ma_cross_status_text(), keyboard()
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
