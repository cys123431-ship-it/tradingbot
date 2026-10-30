"""Telegram controls (/btcmacross) and scheduler for the BTC SMA3/SMA200 cross strategy."""

from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler

from btc_ema_pullback.config import NETWORK_MAINNET, NETWORK_TESTNET, TRADING_MODE_DRY_RUN, TRADING_MODE_ENV, TRADING_MODE_LIVE
from btc_ma_cross import BtcMaCrossService
from btc_ma_cross.config import (
    CONFIG_KEY,
    DEFAULT_LEVERAGE,
    EMERGENCY_STOP_CHOICES,
    LEVERAGE_CHOICES,
    REENTRY_WAIT_CHOICES,
    SLOW_PERIOD_CHOICES,
    TIMEFRAMES,
    TRADFI_SCOPE,
)
from btc_ma_cross.service import (
    TRADFI_MAINNET_ONLY_REASON,
    TRADFI_SESSION_CLOSED_REASON,
)
from trading_safety.sma_periods import (
    PERIOD_COMMANDS,
    PERIOD_PRESETS,
    PeriodError,
    is_default_periods,
    parse_period_command,
    periods_label,
    required_candle_history,
)
from .controller_btc_pullback import NETWORK_LABELS, _answer_quietly, _kst, _n
from .strategy_registry import BINANCE_MAINNET, BINANCE_TESTNET

logger = logging.getLogger(__name__)
TIMEFRAME_LABELS = {"3m": "3분", "5m": "5분", "15m": "15분", "30m": "30분", "1h": "1시간", "2h": "2시간", "4h": "4시간"}


def _btc_sma_button(cfg, mark, fast, slow):
    """One SMA preset button; highlighted when the live pair matches."""
    current = (int(cfg.get("fast_period") or 0), int(cfg.get("slow_period") or 0))
    return InlineKeyboardButton(
        mark(current == (fast, slow), f"SMA{fast}/SMA{slow}"),
        callback_data=f"bm:sma:{fast}:{slow}",
    )


def _rotation_text(cfg):
    if not cfg.get("rotate_after_exit"):
        return "OFF"
    if cfg.get("entry_mode") != "trend":
        return "ON (방향 유지 중 진입 모드에서만 동작)"
    if cfg.get("scan_scope") == "single":
        return "ON (코인 지정 중에는 동작 안 함 — 지정 코인만 거래)"
    return "ON — 익절·손절 직후 방금 코인을 뺀 거래대금 1위 코인에 바로 진입"


def _scope_text(cfg):
    if cfg.get("scan_scope") == TRADFI_SCOPE:
        return f"🏦 TradFi만 — 바이낸스 메인넷 미국주식 + 금 (선물 상장 종목, 24h 거래대금 상위 {cfg.get('alt_universe_size', 50)}, 미국 정규장 시간에만 신규 진입)"
    if cfg.get("scan_scope") == "alts":
        return f"알트 전용 24h 거래대금 상위 {cfg.get('alt_universe_size', 50)}개 (BTC 제외, 동시 신호 시 거래대금 1위)"
    if cfg.get("scan_scope") == "single":
        return f"지정 코인 {cfg.get('single_symbol') or 'BTCUSDT'}만"
    return "BTC만"


TRADFI_KO = {
    "weekend": "주말 휴장",
    "holiday": "미국 휴장일",
    "outside_regular_session": "정규장 시간 아님",
    "regular_session_open": "정규장 진행 중",
}


def _us_session_text(session):
    """One-line US regular-session status for the Telegram status screen."""
    if not session:
        return "확인 불가"
    reason = str(session.get("reason") or "")
    label = TRADFI_KO.get(reason, reason or "-")
    if session.get("open"):
        return f"✅ {label} ({session.get('regular_close', '')[:19]})"
    return f"⏸ {label} (다음 정규장 {session.get('regular_open', '')[:19]})"


PICK_LIST_SIZE = 12
DEFAULT_PROFIT_LOCK_SWITCH_COUNT = 3


def _profit_lock_switch_text(cfg, state=None):
    """익절 N회 → 다음 진입부터 돌파 시 진입 강제 상태 문구."""
    count = int(cfg.get("profit_lock_switch_count") or 0)
    if count == 0:
        return "OFF (익절 횟수와 상관없이 선택한 진입 모드를 유지)"
    state = state or {}
    done = int(state.get("profit_lock_exit_count") or 0)
    text = f"수익 확보 익절 {count}회마다 다음 진입부터 '돌파 시 진입' 강제 (현재 {done}/{count})"
    if state.get("entry_mode_forced_cross") or state.get("pending_entry_mode_switch"):
        text += " · 돌파 강제 적용됨"
    return text


def _profit_lock_switch_button(cfg, state=None):
    count = int(cfg.get("profit_lock_switch_count") or 0)
    state = state or {}
    if count == 0:
        return "🔁 익절N회→돌파 강제: OFF"
    done = int(state.get("profit_lock_exit_count") or 0)
    forced = " ✅" if (state.get("entry_mode_forced_cross") or state.get("pending_entry_mode_switch")) else ""
    return f"🔁 익절{count}회→돌파 강제: ON ({done}/{count}){forced}"


def _coin_error_text(reason):
    code, _, symbol = str(reason).partition(":")
    return {
        "SYMBOL_EMPTY": "코인 이름을 입력하세요. 예: /macoin SOL",
        "SYMBOL_NOT_FOUND": f"{symbol} USDT 무기한 선물을 이 거래소에서 찾지 못했습니다.",
        "NOT_TRADING": f"{symbol}은(는) 지금 거래가 중지된 상태입니다.",
        "STABLECOIN": f"{symbol}은(는) 스테이블코인이라 지정할 수 없습니다.",
        "TRADFI": f"{symbol}은(는) 주식·원자재(TradFi) 계약이라 지정할 수 없습니다.",
    }.get(code, f"코인을 확인하지 못했습니다: {reason}")


def _reentry_text(cfg):
    wait = int(cfg.get("reentry_wait_bars", 3))
    return "반대 크로스까지 대기" if wait == 0 else f"{wait}봉 대기 후 같은 방향 재진입 (반대 크로스 시 즉시)"


def _entry_mode_text(cfg):
    if cfg.get("entry_mode") == "trend":
        return ("방향 유지 중 진입 (SMA3이 SMA200 위=롱 / 아래=숏, 수익확보·손절 청산 후 그 코인은 "
                f"{_reentry_text(cfg)})")
    return "돌파 시 진입 (SMA3이 SMA200을 뚫는 봉)"


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
            config_writer=self,
        )
        self.btc_ma_cross_service = service
        return service

    async def set_entry_mode(self, entry_mode):
        """Config writer used by the profit-lock switch (called from the strategy loop)."""
        if entry_mode not in ("cross", "trend"):
            return False
        await self._set_btc_ma_cross_value("entry_mode", entry_mode)
        return str((self.cfg.get(CONFIG_KEY, {}) or {}).get("entry_mode")) == entry_mode

    # ------------------------------------------------------------ rendering
    @staticmethod
    def _build_btc_ma_cross_keyboard(cfg, *, confirm=None, state=None):
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
                InlineKeyboardButton(mark(cfg.get("scan_scope") not in ("alts", "single", TRADFI_SCOPE), "🪙 BTC만"), callback_data="bm:scope:btc"),
                InlineKeyboardButton(mark(cfg.get("scan_scope") == "alts", f"🌐 알트 상위{cfg.get('alt_universe_size', 50)}"),
                                     callback_data="bm:scope:alts"),
                InlineKeyboardButton(
                    mark(True, f"🎯 {cfg.get('single_symbol') or 'BTCUSDT'}") if cfg.get("scan_scope") == "single"
                    else "🎯 코인 지정",
                    callback_data="bm:pick",
                ),
            ],
            [
                InlineKeyboardButton(mark(cfg.get("scan_scope") == TRADFI_SCOPE, "🏦 TradFi만 (메인넷)"),
                                     callback_data="bm:scope:tradfi"),
            ],
            [
                InlineKeyboardButton(mark(cfg.get("entry_mode") != "trend", "⚡ 돌파 시 진입"), callback_data="bm:entry:cross"),
                InlineKeyboardButton(mark(cfg.get("entry_mode") == "trend", "📈 방향 유지 중 진입"), callback_data="bm:entry:trend"),
            ],
            [
                InlineKeyboardButton(
                    _profit_lock_switch_button(cfg, state),
                    callback_data="bm:pls:" + ("off" if int(cfg.get("profit_lock_switch_count") or 0) else "on"),
                ),
            ],
            [
                InlineKeyboardButton(mark(cfg.get("rotate_after_exit"), "🔁 청산 후 다른 코인 바로 진입"),
                                     callback_data="bm:rot:" + ("off" if cfg.get("rotate_after_exit") else "on")),
            ],
            [
                InlineKeyboardButton(
                    mark(int(cfg.get("reentry_wait_bars", 3)) == choice,
                         "재진입 크로스까지" if choice == 0 else f"재진입 {choice}봉 후"),
                    callback_data=f"bm:rw:{choice}",
                )
                for choice in REENTRY_WAIT_CHOICES
            ],
            [
                _btc_sma_button(cfg, mark, fast, slow)
                for label, fast, slow in PERIOD_PRESETS[:2]
            ],
            [
                _btc_sma_button(cfg, mark, fast, slow)
                for label, fast, slow in PERIOD_PRESETS[2:]
            ],
            [
                InlineKeyboardButton("✏️ 직접 입력: /btcma 1 200", callback_data="bm:sma_help"),
            ],
            [
                InlineKeyboardButton(mark(cfg.get("timeframe") == tf, TIMEFRAME_LABELS[tf]), callback_data=f"bm:tf:{tf}")
                for tf in TIMEFRAMES[:4]
            ],
            [
                InlineKeyboardButton(mark(cfg.get("timeframe") == tf, TIMEFRAME_LABELS[tf]), callback_data=f"bm:tf:{tf}")
                for tf in TIMEFRAMES[4:]
            ],
            [
                InlineKeyboardButton(mark(int(cfg.get("leverage") or DEFAULT_LEVERAGE) == lev, f"{lev}x"),
                                     callback_data=f"bm:lev:{lev}")
                for lev in LEVERAGE_CHOICES
            ],
            [
                InlineKeyboardButton(mark(int(cfg.get("emergency_stop_roi_percent") or 0) == choice,
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

    async def _btc_ma_cross_pick_keyboard(self, network):
        """BTC plus the top alts by 24h volume as one-tap coin buttons."""
        service = self._btc_ma_cross_service()
        cfg = service.config()
        coins = ["BTCUSDT"]
        try:
            for _, market_id, _ in await service._volume_universe(network, cfg):
                if market_id not in coins:
                    coins.append(market_id)
                if len(coins) >= PICK_LIST_SIZE:
                    break
        except Exception as exc:
            logger.warning("3/200 SMA coin list unavailable: %s", exc)
        current = cfg.get("single_symbol") if cfg.get("scan_scope") == "single" else None
        buttons = [
            InlineKeyboardButton(("✅ " if coin == current else "") + coin[:-4], callback_data=f"bm:coin:{coin}")
            for coin in coins
        ]
        rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
        rows.append([InlineKeyboardButton("↩️ 돌아가기", callback_data="bm:status")])
        return InlineKeyboardMarkup(rows)

    async def _set_btc_ma_cross_periods(self, fast, slow):
        """Store a validated SMA pair; returns (ok, message).

        A held position keeps the lines it was opened with, so the pair only
        affects the next entry (the service stores ``slow_period`` on the trade).
        """
        try:
            fast_p, slow_p = parse_period_command([fast, slow])
        except PeriodError as exc:
            return False, f"⚠️ 기간 변경 실패: {exc}\n예) /btcma 1 200"
        service = self._btc_ma_cross_service()
        cfg_before = service.config()
        await self._set_btc_ma_cross_value("fast_period", fast_p)
        await self._set_btc_ma_cross_value("slow_period", slow_p)
        needed = required_candle_history(slow_p)
        if int(cfg_before.get("candle_history") or 0) < needed:
            await self._set_btc_ma_cross_value("candle_history", needed)
        cfg = service.config()
        network = service.current_network(cfg)
        holding = bool((service.load_state(network, service.mode(cfg)).get("trade") or {})) if network else False
        note = "\n(보유 중인 포지션은 진입 당시 기간 기준으로 관리, 다음 진입부터 새 기간 적용)" if holding else ""
        return True, (
            f"✅ SMA 기간 변경: {periods_label(fast_p, slow_p)} "
            f"(패스트 {fast_p} · 슬로우 {slow_p})\n"
            f"• 봉 {cfg['timeframe']} 기준 · 신호 계산에 {cfg['candle_history']}봉 사용{note}"
        )

    async def _set_btc_ma_cross_coin(self, network, text):
        service = self._btc_ma_cross_service()
        try:
            _, market_id = await service.resolve_coin(network, text)
        except ValueError as exc:
            return False, "⚠️ " + _coin_error_text(exc)
        except Exception as exc:
            return False, f"⚠️ 거래소 종목 정보를 불러오지 못했습니다: {type(exc).__name__}"
        await self._set_btc_ma_cross_value("single_symbol", market_id)
        await self._set_btc_ma_cross_value("scan_scope", "single")
        holding = service.load_state(network, service.mode()).get("trade") or {}
        note = ''
        if holding and (holding.get("market_id") or "BTCUSDT") != market_id:
            note = f"\n(보유 중인 {holding.get('market_id') or 'BTCUSDT'} 포지션은 그대로 관리, 청산 후부터 {market_id}만 진입)"
        return True, f"🎯 코인 지정: {market_id}만 거래합니다.{note}"

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
                "📏 3/200 SMA 크로스 전략\n"
                "지금 /setup 거래소가 업비트라 사용할 수 없습니다. /setup에서 바이낸스(테스트넷 데모 또는 메인넷)를 선택하세요."
            )
        cfg, state, mode, network = status["config"], status["state"], status["mode"], status["network"]
        trade = state.get("trade") or {}
        stop = int(cfg["emergency_stop_roi_percent"])
        lines = [
            f"📏 {cfg['fast_period']}/{cfg['slow_period']} SMA 크로스 전략 (반대 크로스 시 즉시 반대 진입)",
            f"전략: {'ON' if cfg['enabled'] else 'OFF'} · 모드: {mode}"
            + (" (환경변수로 DRY_RUN 강제)" if cfg["trading_mode"] == TRADING_MODE_LIVE and mode == TRADING_MODE_DRY_RUN else ""),
            f"거래소: {NETWORK_LABELS.get(network, network)} (/setup 선택을 따름) · API 키: {'있음' if status['has_credentials'] else '없음'}",
            f"메인 선물 자동매매: {'일시정지' if getattr(self, 'is_paused', False) else '동작 중'} (선물 전략은 하나만 켜짐)",
            f"종목: {_scope_text(cfg)}",
            f"진입: {_entry_mode_text(cfg)}",
            f"익절 자동 전환: {_profit_lock_switch_text(cfg, state)}",
            f"청산 후 다른 코인 전환: {_rotation_text(cfg)}",
            f"봉: {TIMEFRAME_LABELS[cfg['timeframe']]} (마감봉 기준) · SMA {cfg['fast_period']}/{cfg['slow_period']}",
            f"레버리지 {cfg['leverage']}x ISOLATED (2~5x 선택, 다음 진입부터) · 증거금: 가용 잔고의 {_n(cfg['margin_fraction']) * 100:.0f}%",
            f"비상손절: {'OFF' if stop == 0 else f'증거금 -{stop}% (Mark 기준)'}",
            f"수익 확보 손절(증거금 기준, Last 기준 트리거): {self._btc_ma_cross_lock_table(cfg)}",
        ]
        if cfg.get("scan_scope") == TRADFI_SCOPE:
            _tradfi_state = service.tradifi_scope_state(network)
            lines.append(
                f"🏦 TradFi 신규 진입: {'가능' if _tradfi_state.get('allowed') else '차단 — ' + str(_tradfi_state.get('detail'))}"
            )
            lines.append(f"미국 정규장: {_us_session_text(service.us_session_status())}")
        if mode == TRADING_MODE_DRY_RUN and not status["has_credentials"]:
            lines.append(f"가상 잔고: {cfg['dry_run_balance_usdt']} USDT + 가상 손익 (API 키 없음)")
        if trade:
            lines += [
                "",
                f"보유: {trade.get('market_id') or 'BTCUSDT'} {trade.get('side')} {trade.get('quantity')} @ {trade.get('entry_price')} "
                f"(증거금 {_n(trade.get('margin_used')):.2f} USDT)",
                f"비상손절 {trade.get('stop_price') or 'OFF'} · 수익 확보 "
                + (f"+{_n(trade.get('lock_roi')):g}% @ {trade.get('lock_price')}" if trade.get("lock_price") else "아직 없음 (+5% 도달 전)"),
            ]
        else:
            lines += ["", "보유: 없음"]
            for wait_line in service.reentry_wait_lines(cfg, state):
                lines.append(f"⏳ {wait_line}")
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
            f"📈 3/200 SMA 성과 ({status['mode']} · {status['network']})",
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
                detail = (f"{event.get('market_id') or ''} {event['side']} 신호".strip() if event.get("side")
                          else event.get("skip_reason")) or "-"
                if event.get("rotation"):
                    detail += f" · 청산 후 다른 코인 전환 ({event.get('exclude')} 제외 {event.get('evaluated')}종목 평가)"
                elif event.get("scope") == "alts":
                    detail += f" · {event.get('evaluated')}종목 평가, 크로스 {len(event.get('crosses') or [])}건"
                else:
                    detail += f" · SMA3 {_n(event.get('sma_fast')):.8g} / SMA200 {_n(event.get('sma_slow')):.8g}"
            elif kind == "ORDER":
                detail = f"{event.get('symbol') or ''} {event.get('side')} {event.get('quantity')} @ {event.get('actual_avg_entry')}"
            elif kind == "PROFIT_LOCK":
                detail = f"+{_n(event.get('reached_roi')):g}% 도달 → +{_n(event.get('locked_roi')):g}% 확보 @ {event.get('trigger')}"
            else:
                detail = event.get("skip_reason") or event.get("exit_reason") or event.get("reason") or "-"
            lines.append(f"{_kst(event.get('ts'))} {kind}: {detail}")
        return "\n".join(lines)

    def _btc_ma_cross_rules_text(self, cfg):
        stop = int(cfg["emergency_stop_roi_percent"])
        return (
            f"📘 {cfg['fast_period']}/{cfg['slow_period']} SMA 크로스 규칙 (기간은 /btcma 또는 버튼으로 변경)\n"
            f"• 종목: {_scope_text(cfg)} (TradFi·스테이블코인 제외, 🎯 코인 지정 또는 /macoin SOL 로 1개 지정)\n"
            f"• 진입: {_entry_mode_text(cfg)}\n"
            f"• 익절 자동 전환: {_profit_lock_switch_text(cfg)}\n"
            f"• 청산 후 다른 코인 전환: {_rotation_text(cfg)}\n"
            f"• 봉: {TIMEFRAME_LABELS[cfg['timeframe']]} 마감봉 기준, 진입·청산(반대 크로스) 모두 같은 봉 (3분~4시간 선택)\n"
            "• 보유 중 반대 크로스(또는 봉 마감 때 SMA3이 반대편): 시장가(reduce-only) 청산 후 즉시 반대 방향 진입\n"
            f"• 수익 확보(증거금 기준): {self._btc_ma_cross_lock_table(cfg)} (항상 도달 칸 {cfg['lock_gap_percent']}% 아래, 한 번 올라간 손절은 내려가지 않음)\n"
            "• 수익 확보 손절로 청산되면 다음 크로스까지 대기\n"
            f"• 비상손절: {'OFF' if stop == 0 else f'증거금 -{stop}%'} (메뉴에서 선택, 거래소에 Mark 기준으로 걸림)\n"
            f"• 레버리지 {cfg['leverage']}배 ISOLATED 선택(2~5배, 다음 진입부터), 가용 증거금의 {_n(cfg['margin_fraction']) * 100:.0f}% 사용, 계좌 전체 1포지션\n"
            "• 🏦 TradFi만: 바이낸스 메인넷 전용 · 미국주식 TradFi + 금만 · 미국 정규장 시간에만 신규 진입\n"
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

    def _tradfi_scope_refusal(self, service, network):
        """Message when TradFi cannot be used on the current exchange, else ''."""
        if service.tradfi_network_allowed(network):
            return ""
        network_label = NETWORK_LABELS.get(network, network)
        return (
            "🚫 TradFi 전용은 바이낸스 메인넷에서만 사용할 수 있습니다.\n"
            f"지금 거래소: {network_label} (/setup 선택을 따름)\n"
            "테스트넷(데모)에는 미국주식 TradFi 계약이 거의 없어 모드를 켜지 않습니다.\n"
            "/setup에서 바이낸스 메인넷으로 바꾼 뒤 다시 선택하세요."
        )

    def _btc_ma_cross_state(self):
        """Current strategy state for the keyboard (empty when Upbit is selected)."""
        service = self._btc_ma_cross_service()
        cfg = service.config()
        network = service.current_network(cfg)
        return service.load_state(network, service.mode(cfg)) if network else {}

    async def _reset_btc_ma_cross_counters(self, network):
        """Clear the 익절 count and the forced-cross flag (manual mode choice)."""
        service = self._btc_ma_cross_service()
        state = service.load_state(network, service.mode(service.config()))
        state["profit_lock_exit_count"] = 0
        state["entry_mode_forced_cross"] = False
        state["pending_entry_mode_switch"] = None
        service.save_state(network, service.mode(service.config()), state)
        return state

    async def _handle_btc_ma_cross_action(self, action):
        service = self._btc_ma_cross_service()

        cfg = service.config()
        network = service.current_network(cfg)
        if not network:
            return await self._format_btc_ma_cross_status(), keyboard()
        state = service.load_state(network, service.mode(cfg))
        holding = bool(state.get("trade"))

        def keyboard(confirm=None):
            return self._build_btc_ma_cross_keyboard(service.config(), confirm=confirm, state=state)

        if action == "on":
            return (
                "⚠️ 3/200 SMA 크로스 전략을 켤까요?\n"
                f"모드 {service.mode(cfg)} · {NETWORK_LABELS[network]} (/setup) · {TIMEFRAME_LABELS[cfg['timeframe']]}\n"
                f"종목 {_scope_text(cfg)} · {'방향 유지 중 진입' if cfg['entry_mode'] == 'trend' else '돌파 시 진입'}\n"
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
            return "✅ 3/200 SMA 전략 ON" + note + "\n\n" + await self._format_btc_ma_cross_status(), keyboard()
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
        if action == "pick" or action == "scope:single":
            return (
                "🎯 거래할 코인 1개를 고르세요 (거래대금 상위 순).\n"
                "목록에 없는 코인은 /macoin SOL 처럼 입력하면 됩니다.\n"
                f"지금: {_scope_text(cfg)}",
                await self._btc_ma_cross_pick_keyboard(network),
            )
        if action.startswith("coin:"):
            _, message = await self._set_btc_ma_cross_coin(network, action.split(":", 1)[1])
            return message + "\n\n" + await self._format_btc_ma_cross_status(), keyboard()
        if action.startswith("scope:"):
            scope = action.split(":", 1)[1]
            if scope == TRADFI_SCOPE:
                refusal = self._tradfi_scope_refusal(service, network)
                if refusal:
                    return refusal, keyboard()
                await self._set_btc_ma_cross_value("scan_scope", TRADFI_SCOPE)
                note = "\n(보유 중인 포지션은 그대로 관리, 다음 진입부터 적용)" if holding else ""
                session = service.us_session_status()
                return (
                    f"🏦 TradFi만 선택: {_scope_text(service.config())}\n"
                    f"• 미국 정규장에서만 신규 진입합니다. 지금: {_us_session_text(session)}\n"
                    f"• 장 마감·주말·휴장에는 새 진입을 하지 않고, 보유 포지션의 비상손절·수익 확보·"
                    f"반대 크로스 청산은 계속 동작합니다.{note}\n\n"
                    + await self._format_btc_ma_cross_status()
                ), keyboard()
            if scope in ("btc", "alts"):
                await self._set_btc_ma_cross_value("scan_scope", scope)
            note = "\n(보유 중인 포지션은 그대로 관리, 다음 진입부터 적용)" if holding else ""
            return f"🔎 종목: {_scope_text(service.config())}{note}\n\n" + await self._format_btc_ma_cross_status(), keyboard()
        if action.startswith("lev:"):
            try:
                choice = int(action.split(":", 1)[1])
            except ValueError:
                choice = -1
            if choice in LEVERAGE_CHOICES:
                await self._set_btc_ma_cross_value("leverage", choice)
            note = "\n(보유 중인 포지션은 기존 레버리지 유지, 다음 진입부터 적용)"
            return (
                f"⚙️ 레버리지: {service.config()['leverage']}x ISOLATED{note}\n"
                f"증거금 비율은 가용 잔고의 {_n(service.config()['margin_fraction']) * 100:.0f}% 그대로이고, "
                f"수익 확보·비상손절은 증거금 기준 %로 계산되므로 레버리지를 바꾸면 같은 %의 가격거리가 좁아집니다.\n\n"
                + await self._format_btc_ma_cross_status()
            ), keyboard()
        if action.startswith("entry:"):
            entry_mode = action.split(":", 1)[1]
            if entry_mode in ("cross", "trend"):
                await self._set_btc_ma_cross_value("entry_mode", entry_mode)
                if entry_mode == "trend":
                    # A manual choice wins: stop forcing and restart the count.
                    await self._reset_btc_ma_cross_counters(network)
                    state = service.load_state(network, service.mode(service.config()))
            return f"🎯 진입: {_entry_mode_text(service.config())}\n\n" + await self._format_btc_ma_cross_status(), keyboard()
        if action == "pls:on":
            await self._set_btc_ma_cross_value("profit_lock_switch_count", DEFAULT_PROFIT_LOCK_SWITCH_COUNT)
            return (
                f"🔁 익절 자동 전환 ON: 수익 확보 익절 {DEFAULT_PROFIT_LOCK_SWITCH_COUNT}회마다 "
                "다음 진입부터 '돌파 시 진입'으로 강제합니다.\n"
                "(강제 전환 뒤 카운트는 0부터 다시 셉니다. '방향 유지 중 진입'을 직접 누르면 강제가 해제됩니다.)\n\n"
                + await self._format_btc_ma_cross_status()
            ), keyboard()
        if action == "pls:off":
            await self._set_btc_ma_cross_value("profit_lock_switch_count", 0)
            return (
                "🔁 익절 자동 전환 OFF: 익절 횟수와 상관없이 선택한 진입 모드를 유지합니다.\n\n"
                + await self._format_btc_ma_cross_status()
            ), keyboard()
        if action.startswith("rot:"):
            await self._set_btc_ma_cross_value("rotate_after_exit", action.split(":", 1)[1] == "on")
            return f"🔁 청산 후 다른 코인 전환: {_rotation_text(service.config())}\n\n" + await self._format_btc_ma_cross_status(), keyboard()
        if action.startswith("rw:"):
            try:
                choice = int(action.split(":", 1)[1])
            except ValueError:
                choice = -1
            if choice in REENTRY_WAIT_CHOICES:
                await self._set_btc_ma_cross_value("reentry_wait_bars", choice)
            return f"⏳ 익절·손절 후 재진입: {_reentry_text(service.config())}\n\n" + await self._format_btc_ma_cross_status(), keyboard()
        if action.startswith("slow:"):
            try:
                slow = int(action.split(":", 1)[1])
            except ValueError:
                slow = -1
            if slow in SLOW_PERIOD_CHOICES:
                await self._set_btc_ma_cross_value("slow_period", slow)
            note = (
                f"\n보유 중 포지션도 다음 봉 마감부터 SMA3/SMA{slow} 기준으로 관리합니다. "
                "그때 SMA3이 반대편에 있으면 반대 방향으로 전환합니다." if holding else ""
            )
            return f"📐 기준선: SMA3 / SMA{service.config()['slow_period']}{note}\n\n" + await self._format_btc_ma_cross_status(), keyboard()
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
        if action == "sma_help":
            current = periods_label(cfg.get("fast_period", 3), cfg.get("slow_period", 200))
            presets = " · ".join(label for label, _, _ in PERIOD_PRESETS)
            return (
                f"✏️ SMA 기간 직접 입력\n지금: {current}\n\n"
                "명령: /btcma <패스트> <슬로우>\n"
                "예) /btcma 1 200 → 패스트 1, 슬로우 200\n"
                "예) /btcma 5 60 → 패스트 5, 슬로우 60\n"
                "예) /btcma 9/21 형식도 가능\n\n"
                f"버튼 프리셋: {presets}\n"
                "• 패스트는 슬로우보다 작아야 합니다 (1 이상, 슬로우 500 이하).\n"
                "• 보유 중에 바꾸면 다음 진입부터 적용되고, "
                "보유 포지션은 진입 당시 기간으로 계속 관리됩니다.",
                keyboard(),
            )
        if action.startswith("sma:"):
            parts = action.split(":")
            if len(parts) != 3:
                return "⚠️ 알 수 없는 SMA 설정입니다.", keyboard()
            _, message = await self._set_btc_ma_cross_periods(parts[1], parts[2])
            return message + "\n\n" + await self._format_btc_ma_cross_status(), keyboard()
        if action.startswith("slow:"):
            # Backwards compatibility with older keyboards: SMA3 / SMA<slow>.
            _, message = await self._set_btc_ma_cross_periods(3, action.split(":", 1)[1])
            return message + "\n\n" + await self._format_btc_ma_cross_status(), keyboard()
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
            held = service.load_state(network, service.mode(cfg)).get("trade") or {}
            return (f"⚠️ 이 전략의 {held.get('market_id') or 'BTCUSDT'} 포지션을 시장가(reduce-only)로 청산할까요?",
                    keyboard(("✅ 청산 확인", "bm:confirm_close")))
        if action == "confirm_close":
            result = await service.close_position()
            return f"🔻 청산 요청: {result.get('action')} {result.get('reason') or ''}\n\n" + await self._format_btc_ma_cross_status(), keyboard()
        return await self._format_btc_ma_cross_status(), keyboard()

    def _register_btc_ma_cross_handlers(self, owner_only):
        async def btc_ma_cross_cmd(update, context):
            await update.message.reply_text(
                await self._format_btc_ma_cross_status(),
                reply_markup=self._build_btc_ma_cross_keyboard(
                    self._btc_ma_cross_service().config(),
                    state=self._btc_ma_cross_state(),
                ),
            )

        async def btc_ma_cross_callback(update, context):
            query = update.callback_query
            if not query:
                return
            await _answer_quietly(query)
            action = str(query.data or "").split(":", 1)[-1]
            text, keyboard = await self._handle_btc_ma_cross_action(action)
            await self._edit_btc_ma_cross(query, text, keyboard)

        async def btc_ma_coin_cmd(update, context):
            service = self._btc_ma_cross_service()
            network = service.current_network()
            if not network:
                await update.message.reply_text(await self._format_btc_ma_cross_status())
                return
            args = list(getattr(context, "args", None) or [])
            if not args:
                await update.message.reply_text(
                    "🎯 거래할 코인 1개를 고르세요. 직접 입력: /macoin SOL",
                    reply_markup=await self._btc_ma_cross_pick_keyboard(network),
                )
                return
            _, message = await self._set_btc_ma_cross_coin(network, args[0])
            await update.message.reply_text(
                message + "\n\n" + await self._format_btc_ma_cross_status(),
                reply_markup=self._build_btc_ma_cross_keyboard(service.config(), state=self._btc_ma_cross_state()),
            )

        async def btc_ma_period_cmd(update, context):
            service = self._btc_ma_cross_service()
            args = list(getattr(context, "args", None) or [])
            if not args:
                _, help_text = await self._handle_btc_ma_cross_action("sma_help")
                await update.message.reply_text(
                    help_text,
                    reply_markup=self._build_btc_ma_cross_keyboard(
                        service.config(), state=self._btc_ma_cross_state()),
                )
                return
            try:
                fast_p, slow_p = parse_period_command(args)
            except PeriodError as exc:
                current_cfg = service.config()
                await update.message.reply_text(
                    f"⚠️ 기간 변경 실패: {exc}\n예) /btcma 1 200\n"
                    f"지금: {periods_label(current_cfg.get('fast_period', 3), current_cfg.get('slow_period', 200))}"
                )
                return
            ok, message = await self._set_btc_ma_cross_periods(fast_p, slow_p)
            await update.message.reply_text(
                message + "\n\n" + await self._format_btc_ma_cross_status(),
                reply_markup=self._build_btc_ma_cross_keyboard(
                    service.config(), state=self._btc_ma_cross_state()),
            )

        self.tg_app.add_handler(CommandHandler("btcmacross", owner_only(btc_ma_cross_cmd)))
        self.tg_app.add_handler(CommandHandler("macoin", owner_only(btc_ma_coin_cmd)))
        for name in PERIOD_COMMANDS:
            self.tg_app.add_handler(CommandHandler(name, owner_only(btc_ma_period_cmd)))
        self.tg_app.add_handler(CallbackQueryHandler(owner_only(btc_ma_cross_callback), pattern=r"^bm:"))

    async def _stop_btc_ma_cross_for_emergency(self):
        lines = []
        try:
            was_on = bool((self.cfg.get(CONFIG_KEY, {}) or {}).get("enabled"))
            await self._set_btc_ma_cross_value("enabled", False)
            result = await self._btc_ma_cross_service().close_position(reason="emergency_stop")
            if result.get("action") == "none":
                lines.append("3/200 SMA: OFF" + ("" if was_on else " (이미 꺼져 있었음)"))
            else:
                lines.append(f"3/200 SMA: OFF · 포지션 청산 {result.get('action')} {result.get('reason') or ''}".strip())
        except Exception as exc:
            logger.exception("BTC MA cross emergency stop failed")
            lines.append(f"⚠️ 3/200 SMA 정지 처리 오류: {exc}")
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
