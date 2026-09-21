"""Telegram controls for the EMA200 + UT Bot + RSI 2H strategy."""

from __future__ import annotations

import asyncio
from math import isfinite

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
)

from .ema200_utbot_rsi import (
    EMA200_CONSECUTIVE_LOSS_RESET_STATE_KEY,
    EMA200_DAILY_LOSS_RESET_STATE_KEY,
    EMA200_BINANCE_TOP10_BASES,
    EMA200_UTBOT_RSI_CONFIG_KEY,
    EMA200_UTBOT_RSI_DISPLAY_NAME,
    EMA200_UTBOT_RSI_STRATEGY,
    apply_ema200_daily_loss_reset,
    build_ema200_utbot_rsi_risk_plan,
    ema200_small_account_margin_percent,
    get_ema200_consecutive_losses,
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
        candidate_enabled = bool(
            cfg.get("best_candidate_selection_enabled", True)
        )
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
                InlineKeyboardButton(
                    "🏆 최적후보: ON" if candidate_enabled else "🏆 최적후보: OFF",
                    callback_data="e2h:candidate_toggle",
                ),
                InlineKeyboardButton(
                    "❓ 후보선택 설명",
                    callback_data="e2h:help:candidate",
                ),
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
                InlineKeyboardButton("손절거리 3%", callback_data="e2h:emergency:3"),
                InlineKeyboardButton("손절거리 5%", callback_data="e2h:emergency:5"),
                InlineKeyboardButton("손절거리 8%", callback_data="e2h:emergency:8"),
            ],
            [
                InlineKeyboardButton("✍️ 손절거리 직접입력", callback_data="e2h:custom:emergency"),
                InlineKeyboardButton("❓ 손절거리 설명", callback_data="e2h:help:emergency"),
            ],
            [
                InlineKeyboardButton("레버리지 2x", callback_data="e2h:leverage:2"),
                InlineKeyboardButton("3x", callback_data="e2h:leverage:3"),
                InlineKeyboardButton("5x", callback_data="e2h:leverage:5"),
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

    async def _ema200_utbot_rsi_sizing_preview(self, cfg, loss_streak):
        """Describe the next theoretical entry using the current futures balance."""
        engine = (getattr(self, "engines", {}) or {}).get("signal")
        balance_reader = getattr(engine, "get_balance_info", None) if engine else None
        if not callable(balance_reader):
            return (
                "현재 계좌 예상 진입: 잔고 조회 불가\n"
                "계산 예시(5,000 USDT·위험 0.50%·5x): 손절거리 5%이면 "
                "명목 500 USDT / 증거금 100 USDT(계좌의 2%), "
                "손절거리 25%이면 명목 100 USDT / 증거금 20 USDT(계좌의 0.4%)"
            )

        try:
            total_equity, free_balance, _ = await balance_reader()
            equity = float(total_equity or free_balance or 0.0)
            free = float(free_balance or 0.0)
            plan = build_ema200_utbot_rsi_risk_plan(
                account_equity=equity,
                free_balance=free,
                entry_price=1.0,
                config=cfg,
                consecutive_losses=loss_streak,
            )
        except Exception:
            return (
                "현재 계좌 예상 진입: 잔고 조회 실패\n"
                "계산 예시(5,000 USDT·위험 0.50%·5x): 손절거리 5%이면 "
                "명목 500 USDT / 증거금 100 USDT(계좌의 2%), "
                "손절거리 25%이면 명목 100 USDT / 증거금 20 USDT(계좌의 0.4%)"
            )

        notional = float(plan["planned_notional"])
        margin = float(plan["planned_margin"])
        notional_pct = notional / equity * 100.0 if equity > 0 else 0.0
        margin_pct = margin / equity * 100.0 if equity > 0 else 0.0
        cap_note = " (가용잔고 상한 적용)" if plan.get("margin_cap_applied") else ""

        if plan.get("small_account_mode"):
            protection = (
                "비상 손절 없음 / UT 반대 신호로만 청산"
                if not plan.get("emergency_stop_required")
                else (
                    f"진입가 대비 {float(cfg['emergency_exit_percent']):.2f}% 비상 손절 / "
                    f"도달 시 예상손실 {float(plan['planned_emergency_loss_usdt']):.2f} USDT"
                )
            )
            return (
                f"현재 계좌: {equity:.2f} USDT → 1,000 이하 소액계좌 규칙\n"
                f"다음 진입 비율: 증거금 계좌의 {float(plan['margin_percent']):.0f}% / 5x\n"
                f"예상 명목 포지션: {notional:.2f} USDT (계좌의 {notional_pct:.2f}%)\n"
                f"예상 사용 증거금: {margin:.2f} USDT (계좌의 {margin_pct:.2f}%){cap_note}\n"
                f"보호 방식: {protection}"
            )

        return (
            f"현재 계좌: {equity:.2f} USDT → 위험예산 방식\n"
            f"1회 허용손실: {float(plan['risk_budget_usdt']):.2f} USDT "
            f"(계좌의 {float(cfg['risk_per_trade_percent']):.2f}%)\n"
            f"비상 손절 가격거리: 진입가 대비 {float(cfg['emergency_exit_percent']):.2f}%\n"
            f"예상 명목 포지션: {notional:.2f} USDT (계좌의 {notional_pct:.2f}%)\n"
            f"예상 사용 증거금: {margin:.2f} USDT (계좌의 {margin_pct:.2f}%, "
            f"{int(plan['leverage'])}x){cap_note}\n"
            "※ 손절거리를 넓힐수록 같은 손실예산을 지키기 위해 진입금액은 작아집니다."
        )

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
        effective_daily_pnl = 0.0
        daily_reset_active = False
        loss_streak = 0
        loss_streak_reset_active = False
        engine = (getattr(self, "engines", {}) or {}).get("signal")
        store = (
            getattr(engine, "trading_state_store", None)
            if engine
            else None
        ) or getattr(self, "trading_state_store", None)
        try:
            daily_count, daily_pnl = self.db.get_daily_stats()
            weekly_count, weekly_pnl = self.db.get_weekly_stats()
            streak_reset_payload = (
                store.get_runtime_state(
                    EMA200_CONSECUTIVE_LOSS_RESET_STATE_KEY
                )
                if store is not None
                else None
            )
            loss_streak, loss_streak_reset_active = (
                get_ema200_consecutive_losses(
                    self.db,
                    streak_reset_payload,
                )
            )
        except Exception:
            pass
        effective_daily_pnl = float(daily_pnl)
        try:
            payload = (
                store.get_runtime_state(EMA200_DAILY_LOSS_RESET_STATE_KEY)
                if store is not None
                else None
            )
            effective_daily_pnl, _, daily_reset_active = (
                apply_ema200_daily_loss_reset(daily_pnl, payload)
            )
        except Exception:
            daily_reset_active = False
        daily_reset_line = (
            "오늘 손실한도 기준손익(초기화 후): "
            f"{float(effective_daily_pnl):+.4f} USDT\n"
            if daily_reset_active
            else ""
        )
        next_margin_percent = ema200_small_account_margin_percent(loss_streak)
        stage_exit = (
            "UT 반대 신호만(Stop 없음)"
            if loss_streak == 0
            else (
                "UT 반대 신호 + 비상 Stop "
                f"(진입가 대비 {cfg['emergency_exit_percent']:.2f}%)"
            )
        )
        sizing_preview = await self._ema200_utbot_rsi_sizing_preview(cfg, loss_streak)

        latest_symbol = None
        latest_detail = {}
        latest_reason = None
        status_map = getattr(engine, "last_ema200_utbot_rsi_status", {}) if engine else {}
        recent_symbols = []
        if isinstance(status_map, dict) and status_map:
            recent_items = sorted(
                status_map.items(),
                key=lambda item: (
                    int((item[1] or {}).get("evaluated_at_ns") or 0),
                    int((item[1] or {}).get("closed_candle_ts") or 0),
                ),
                reverse=True,
            )
            latest_symbol, latest_detail = recent_items[0]
            recent_symbols = [str(symbol) for symbol, _ in recent_items[:10]]
            latest_reason = (getattr(engine, "last_entry_reason", {}) or {}).get(latest_symbol)

        condition_text = "최근 조건: 아직 2시간 완료봉 평가 기록 없음"
        if latest_detail:
            close_value = latest_detail.get("closed_candle_close")
            ema_value = latest_detail.get("ema200")
            prev_rsi = latest_detail.get("prev_rsi")
            curr_rsi = latest_detail.get("curr_rsi")
            ut_state = str(latest_detail.get("ut_state") or "NONE").upper()
            ut_last = str(latest_detail.get("ut_last_signal_side") or "NONE").upper()
            condition_text = (
                f"최근 조건 ({latest_symbol})\n"
                f"• 종가 / EMA200: {float(close_value or 0):.4f} / {float(ema_value or 0):.4f}\n"
                f"• UT 현재상태: {ut_state} / 최근 UT 신호: {ut_last}\n"
                f"• RSI: {float(prev_rsi or 0):.2f} → {float(curr_rsi or 0):.2f}\n"
                f"• 판단: {latest_reason or '대기'}\n"
                f"• 최근 평가 종목({len(status_map)}개 기록): "
                f"{', '.join(recent_symbols)}"
            )

        candidate_enabled = bool(
            cfg.get("best_candidate_selection_enabled", True)
        )
        candidate_status = (
            "ON — 10개 전부 평가 후 최고 점수 후보부터 주문"
            if candidate_enabled
            else "OFF — 회전 스캔 순서상 첫 유효 신호부터 주문"
        )
        selection_text = "최근 후보선택: 아직 평가 기록 없음"
        selection_state = (
            getattr(engine, "last_ema200_candidate_selection", {})
            if engine
            else {}
        )
        if isinstance(selection_state, dict) and selection_state:
            selection_lines = [
                "최근 후보선택: "
                + (selection_state.get("reason") or "평가 완료")
            ]
            selected = selection_state.get("selected") or {}
            if selected:
                selection_lines.append(
                    "• 선택: "
                    f"{selected.get('symbol')} "
                    f"{str(selected.get('side') or '').upper()} "
                    f"/ 점수 {float(selected.get('score') or 0):.2f}"
                )
            for candidate in (selection_state.get("candidates") or [])[:3]:
                selection_lines.append(
                    f"• {int(candidate.get('rank') or 0)}위 "
                    f"{candidate.get('symbol')} "
                    f"{str(candidate.get('side') or '').upper()} "
                    f"{float(candidate.get('score') or 0):.2f}점 "
                    f"(UT {float(candidate.get('ut_age_bars') or 0):.1f}봉 전, "
                    f"RSIΔ {float(candidate.get('rsi_momentum') or 0):.2f})"
                )
            selection_text = "\n".join(selection_lines)

        return (
            f"🎛 {EMA200_UTBOT_RSI_DISPLAY_NAME}\n\n"
            f"전략 선택: {'✅ ACTIVE' if active else '⬜ 미선택'}\n"
            f"신규 진입: {'ON' if cfg['enabled'] else 'OFF'}\n"
            f"최적 후보 선택: {candidate_status}\n"
            "시간봉: 2시간 완료봉 고정\n"
            f"스캔 종목: {', '.join(EMA200_BINANCE_TOP10_BASES)} (고정 10개)\n"
            "추세: 종가 > EMA200=롱 허용 / 종가 < EMA200=숏 허용\n"
            f"RSI: {cfg['rsi_length']}기간, LONG=50 위 상승 / SHORT=50 아래 하락\n\n"
            f"UT Bot 전용값: Key {cfg['utbot_key_value']:.2f} / "
            f"ATR {cfg['utbot_atr_period']} / "
            f"HA {'ON' if cfg['utbot_use_heikin_ashi'] else 'OFF'} "
            "(공용 /utbot 설정과 분리)\n\n"
            "소액계좌 기준: equity 1,000 USDT 이하\n"
            f"소액계좌 다음 단계(해당 시): 증거금 {next_margin_percent:.0f}% / 5x / {stage_exit}\n"
            f"EMA200 연속손실: {loss_streak}회"
            f"{' (초기화 후)' if loss_streak_reset_active else ''}\n"
            "축소단계: 50% → 35% → 25% → 15% → 10%(하한)\n"
            f"1,000 USDT 초과 시 위험예산: 계좌의 {cfg['risk_per_trade_percent']:.2f}%\n"
            f"초과계좌 레버리지: {cfg['leverage']}x\n"
            f"비상 손절 가격거리: 진입가 대비 {cfg['emergency_exit_percent']:.2f}%\n"
            f"일일 손실한도: {cfg['daily_loss_limit_percent']:.2f}%\n"
            f"최근 7일 손실한도: {cfg['weekly_loss_limit_percent']:.2f}%\n\n"
            f"📐 다음 진입 예상\n{sizing_preview}\n\n"
            f"오늘 실현손익: {float(daily_pnl):+.4f} USDT / {daily_count}건\n"
            f"{daily_reset_line}"
            f"최근 7일 실현손익: {float(weekly_pnl):+.4f} USDT / {weekly_count}건\n\n"
            f"{condition_text}\n\n"
            f"{selection_text}\n\n"
            "중요: 소액계좌의 연속손실 0회 단계는 비상 Stop 없이 "
            "UT Bot 반대 신호로만 청산됩니다. 첫 손실 뒤 다음 진입부터 "
            "포지션이 축소되고 비상 Stop이 적용됩니다."
        )

    @staticmethod
    def _ema200_utbot_rsi_help_text(kind):
        if kind == "candidate":
            return (
                "📘 최적 후보 선택이란?\n\n"
                "ON이면 고정 10개 종목을 동일한 완료 2시간봉 기준으로 모두 평가한 뒤, "
                "EMA200·UT Bot·RSI 진입 조건을 이미 통과한 후보들만 서로 비교합니다.\n\n"
                "점수는 최근 UT 신호, RSI 진행 강도, 진입 방향의 EMA200 기울기, "
                "최근 24시간 거래대금에 가점을 주고 EMA200에서 3ATR보다 지나치게 "
                "멀어진 후보에는 추격진입 감점을 줍니다. 점수는 후보의 순서만 정하며 "
                "원래 진입 조건을 새로 만들거나 우회하지 않습니다.\n\n"
                "예시) BTC와 DOGE가 같은 2시간봉에서 모두 LONG 조건을 충족했을 때, "
                "BTC가 먼저 스캔됐다는 이유로 즉시 진입하지 않습니다. 10개 평가가 "
                "끝난 후 점수가 높은 종목부터 한 종목만 주문합니다. 최고 후보의 주문이 "
                "최소수량·안전장치 등으로 열리지 않으면 다음 순위 후보를 확인합니다.\n\n"
                "OFF이면 기존 방식대로 매 주기 회전되는 스캔 순서에서 처음 발견한 "
                "유효 신호부터 주문합니다. 변경은 새 진입에만 영향을 주며, 기존 포지션의 "
                "UT 반대 신호 청산·보호주문·리스크 관리는 바뀌지 않습니다."
            )
        if kind == "risk":
            return (
                "📘 1회 위험예산이란?\n\n"
                "이 설정은 계좌 equity가 1,000 USDT를 초과할 때 사용합니다. "
                "1,000 USDT 이하에서는 고정 소액계좌 단계(증거금 "
                "50→35→25→15→10%)가 우선합니다.\n\n"
                "한 번의 거래가 비상 손절 가격까지 불리하게 움직였을 때 "
                "계좌에서 최대 얼마 정도를 잃도록 포지션 크기를 잡을지 정하는 값입니다.\n\n"
                "예시) 계좌 5,000 USDT, 위험 0.5%, 손절거리 5%, 레버리지 5x\n"
                "• 허용 손실예산 = 5,000 × 0.5% = 약 25 USDT\n"
                "• 가격이 5% 불리하게 움직일 때 약 25 USDT 손실이 되도록 "
                "포지션 명목금액을 약 500 USDT로 계산합니다.\n"
                "• 필요한 증거금은 약 100 USDT, 즉 계좌의 약 2%입니다.\n"
                "• 따라서 단순히 '레버리지 5배니까 계좌의 5배를 산다'가 아닙니다. "
                "먼저 손실예산을 정하고 수량을 역산합니다.\n\n"
                "권장 시작값은 0.50%입니다. 1%를 넘기면 연속 손실 시 "
                "계좌 감소가 빨라지므로 경고는 표시하지만, 허용 범위 안에서는 직접 설정할 수 있습니다.\n\n"
                "허용 범위는 0.10~5.00%입니다. 값이 클수록 한 번의 비상 손절 손실과 "
                "포지션 크기가 커집니다. 이 값은 UT 반대 신호라는 정상 청산 시점을 바꾸지 않습니다.\n\n"
                "아래 '위험 직접입력'을 누른 뒤 숫자만 보내세요. 예: 0.5"
            )
        if kind == "emergency":
            return (
                "🛟 비상 손절 가격거리란?\n\n"
                "이 퍼센트는 포지션에 넣는 비율이 아니라, 진입가격에서 거래소 Stop까지의 "
                "가격 변동 거리입니다. LONG 100 진입·5%라면 약 95, SHORT 100 진입·5%라면 "
                "약 105가 최후 안전선입니다.\n\n"
                "정상 청산 규칙은 그대로입니다.\n"
                "• LONG: 2시간봉 UT Bot Sell 신호에서 정상 청산\n"
                "• SHORT: 2시간봉 UT Bot Buy 신호에서 정상 청산\n\n"
                "계좌 equity 1,000 USDT 이하이고 EMA200 연속손실이 0회인 첫 단계에는 "
                "사용자 선택에 따라 거래소 Stop을 설치하지 않고 UT 반대 신호만 기다립니다. "
                "첫 손실 뒤 다음 진입부터 이 비상탈출 Stop이 적용됩니다. "
                "1,000 USDT 초과 계좌에는 기존처럼 첫 진입부터 적용됩니다.\n\n"
                "진입금액에도 미치는 영향(1,000 USDT 초과 계좌)\n"
                "예시: 계좌 5,000 USDT, 1회 위험 0.50%, 레버리지 5x\n"
                "• 손절거리 5% → 허용손실 25 USDT / 명목 포지션 약 500 USDT / "
                "증거금 약 100 USDT(계좌의 2%)\n"
                "• 손절거리 25% → 허용손실 25 USDT / 명목 포지션 약 100 USDT / "
                "증거금 약 20 USDT(계좌의 0.4%)\n"
                "즉 손절거리를 넓힐수록 같은 손실예산을 유지하기 위해 진입금액은 작아집니다. "
                "5x에서 25%처럼 너무 먼 Stop은 청산가보다 늦어질 수 있어 진입 자체가 차단됩니다.\n\n"
                "1,000 USDT 이하에서는 손절거리가 아니라 연속손실 단계가 다음 증거금 비율"
                "(50→35→25→15→10%)을 결정합니다. 첫 단계는 Stop 없이 UT 반대 신호로만 "
                "청산하고, 첫 손실 뒤부터 설정한 비상 손절 가격거리가 적용됩니다.\n\n"
                "이 값은 일반 손절 타이밍을 최적화하려는 것이 아니라 극단적인 역방향 움직임의 "
                "최대 손실을 제한하는 최후 안전선입니다. 메뉴에서 거리만 조정할 수 있습니다.\n\n"
                "설정 변경은 새로 진입하는 포지션부터 적용합니다. 이미 보유 중인 포지션의 "
                "보호 주문을 텔레그램 설정 변경만으로 몰래 교체하지 않습니다.\n\n"
                "허용 범위는 0.5~30%입니다. 너무 가까우면 평범한 가격 흔들림에도 Stop이 "
                "체결될 수 있고, 너무 멀면 진입금액이 지나치게 작아지거나 청산가 안전검사에서 "
                "진입이 거부될 수 있습니다. 정상 UT 청산과는 별개입니다.\n\n"
                "아래 '손절거리 직접입력'을 누른 뒤 가격 변동률 숫자만 보내세요. 예: 5"
            )
        if kind == "leverage":
            return (
                "⚙️ 레버리지란?\n\n"
                "계좌 equity 1,000 USDT 이하에서는 이번 소액계좌 규칙에 따라 5x로 고정됩니다. "
                "이 메뉴의 레버리지 설정은 1,000 USDT 초과 계좌에 적용됩니다.\n\n"
                "이 전략에서는 레버리지를 먼저 정해서 손실을 키우는 방식이 아닙니다. "
                "1회 위험예산과 비상탈출 거리를 먼저 계산하고, 그 포지션을 유지하는 데 "
                "필요한 증거금 크기에 레버리지가 영향을 줍니다.\n\n"
                "예시) 명목 포지션 100 USDT라면 5x에서 필요한 초기 증거금은 대략 20 USDT입니다.\n"
                "레버리지가 높을수록 청산가가 가까워질 수 있으므로 기존 청산가 안전검사가 "
                "비상탈출보다 청산 위험이 앞서는 설정을 차단하거나 더 안전한 값으로 제한합니다.\n\n"
                "초기 기본값은 5x이며 허용 범위는 1~10x입니다. 높은 값은 청산 여유를 줄일 수 있지만 "
                "UT 반대 신호라는 정상 청산 규칙은 바꾸지 않습니다. 직접입력 예: 5"
            )
        if kind == "daily":
            return (
                "🗓 일일 손실한도란?\n\n"
                "오늘의 확정(실현) 손실이 계좌 기준 한도에 도달하면 그날 새 포지션 진입만 막습니다.\n"
                "이미 보유 중인 포지션은 이 한도 때문에 억지로 청산하지 않습니다. "
                "보유 포지션은 원래 UT 반대 신호 또는 비상탈출로 종료됩니다.\n\n"
                "예시) 계좌 1,000 USDT, 일일 한도 2%라면 오늘 실현손익이 약 -20 USDT 이하가 된 뒤 "
                "추가 신규진입을 멈춥니다.\n\n"
                "허용 범위는 0.5~20%입니다. 너무 높으면 하루 누적 손실을 크게 허용하고, 너무 낮으면 "
                "새 기회를 일찍 차단합니다. 정상 UT 청산과 비상 Stop에는 영향을 주지 않습니다.\n\n"
                "직접입력 예: 2"
            )
        if kind == "weekly":
            return (
                "📅 최근 7일 손실한도란?\n\n"
                "최근 7일의 확정(실현) 손실이 계좌 기준 한도에 도달하면 신규진입을 막습니다. "
                "일일 한도와 마찬가지로 이미 보유한 포지션을 강제청산하지 않습니다.\n\n"
                "예시) 계좌 1,000 USDT, 7일 한도 5%라면 최근 7일 실현손익이 약 -50 USDT 이하일 때 "
                "새 거래를 중지합니다.\n\n"
                "허용 범위는 1~40%입니다. 너무 높으면 연속 손실 누적을 크게 허용하고, 너무 낮으면 "
                "회복 거래 기회를 일찍 차단합니다. 정상 UT 청산과 비상 Stop에는 영향을 주지 않습니다.\n\n"
                "7일 한도는 일일 한도보다 작게 설정할 수 없습니다. 직접입력 예: 5"
            )
        return (
            "📘 전략 동작 순서\n\n"
            "LONG: 2시간 완료봉 종가가 EMA200 위 → UT Bot Buy 발생 → 그 LONG 상태가 유지되는 동안 "
            "현재 RSI가 50보다 높고 직전 완료봉보다 상승 → 진입. 이후 UT Bot Sell이 발생하면 정상 청산합니다.\n\n"
            "SHORT: 정확히 반대입니다. EMA200 아래 → UT Bot Sell → SHORT 상태 유지 중 RSI가 50보다 낮고 하락 "
            "→ 진입. 이후 UT Bot Buy에서 정상 청산합니다.\n\n"
            "UT 신호는 현재 RSI 평가봉보다 먼저 확정되어야 하며, 같은 봉 신호는 인정하지 않습니다. "
            "완료된 2시간봉만 사용해 진행 중 봉의 흔들림으로 인한 가짜 돌파를 피합니다.\n\n"
            "이 전략의 UT Bot은 Key 1.0 / ATR 10 / 일반 캔들(HA OFF)로 고정되며, "
            "다른 /utbot 전략의 설정을 변경해도 영향을 받지 않습니다.\n\n"
            f"스캔 대상은 {', '.join(EMA200_BINANCE_TOP10_BASES)} 고정 10개이며, "
            "그 밖의 종목은 다른 경로에서 신호가 들어와도 진입 단계에서 차단합니다.\n\n"
            "소액계좌(1,000 USDT 이하)는 첫 단계에서 equity의 50%를 증거금으로 5x 진입하고 "
            "UT 반대 신호로만 청산합니다. 손실 후 다음 진입은 증거금 비율을 "
            "35%→25%→15%→10%로 줄이고 비상 Stop을 적용합니다. 수익 또는 본전 청산 시 "
            "연속손실 단계가 초기화됩니다. 일·주 손실한도는 별도의 신규진입 차단 장치입니다."
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

            if action == "candidate_toggle":
                cfg = self._ema200_utbot_rsi_config()
                current = bool(
                    cfg.get("best_candidate_selection_enabled", True)
                )
                await self._update_ema200_utbot_rsi_value(
                    "best_candidate_selection_enabled",
                    not current,
                )
                await query.edit_message_text(
                    (
                        "🏆 최적 후보 선택 ON — 고정 10개를 모두 평가한 뒤 "
                        "점수순으로 한 종목만 진입합니다."
                        if current is False
                        else (
                            "↩️ 최적 후보 선택 OFF — 회전 스캔 순서에서 "
                            "첫 유효 신호부터 진입합니다."
                        )
                    )
                    + "\n이미 열린 포지션의 청산·보호 방식은 변경되지 않습니다.",
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
            field_labels = {
                "risk": "1회 위험예산",
                "emergency": "비상 손절 가격거리(진입가 대비)",
                "daily": "일일 손실한도",
                "weekly": "최근 7일 손실한도",
                "leverage": "레버리지",
            }
            if action in field_map and len(parts) > 2:
                raw = parts[2]
                value = int(raw) if action == "leverage" else float(raw)
                current_cfg = self._ema200_utbot_rsi_config()
                if action == "weekly" and value < float(current_cfg["daily_loss_limit_percent"]):
                    await query.edit_message_text(
                        f"⚠️ 최근 7일 손실한도는 일일 손실한도 "
                        f"({current_cfg['daily_loss_limit_percent']:.2f}%)보다 작게 설정할 수 없습니다.",
                        reply_markup=self._build_ema200_utbot_rsi_keyboard(),
                    )
                    return
                await self._update_ema200_utbot_rsi_value(field_map[action], value)
                adjustment = ""
                if action == "daily" and value > float(current_cfg["weekly_loss_limit_percent"]):
                    await self._update_ema200_utbot_rsi_value(
                        "weekly_loss_limit_percent",
                        float(value),
                    )
                    adjustment = (
                        f"\n최근 7일 한도도 일일 한도보다 작아지지 않도록 "
                        f"{float(value):.2f}%로 함께 조정했습니다."
                    )
                await query.edit_message_text(
                    f"✅ 설정 변경: {field_labels[action]} = "
                    f"{value}{'x' if action == 'leverage' else '%'}\n\n"
                    "설정은 새로 진입하는 포지션부터 적용됩니다.\n"
                    "📊 상태 버튼에서 현재 계좌 기준 예상 포지션·증거금·진입 비율을 확인할 수 있습니다."
                    + adjustment,
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
            if not isfinite(number):
                await update.message.reply_text(
                    "NaN 또는 무한대는 사용할 수 없습니다. 유한한 숫자를 입력하세요.\n"
                    "설정은 변경하지 않았습니다. /ema200에서 다시 시도하세요."
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
            field_labels = {
                "risk": "1회 위험예산",
                "emergency": "비상 손절 가격거리(진입가 대비)",
                "daily": "일일 손실한도",
                "weekly": "최근 7일 손실한도",
                "leverage": "레버리지",
            }
            await update.message.reply_text(
                f"✅ 직접설정 완료: {field_labels[kind]} = {value}{suffix}\n"
                "새 포지션부터 적용됩니다.\n"
                "📊 상태 버튼에서 현재 계좌 기준 예상 포지션·증거금·진입 비율을 확인할 수 있습니다."
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
