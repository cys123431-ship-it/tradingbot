"""Runtime signal logic for the independent EMA200 + UT Bot + RSI 2H strategy."""

from __future__ import annotations

import math

import pandas as pd

from .ema200_utbot_rsi import (
    EMA200_UTBOT_RSI_CONFIG_KEY,
    EMA200_UTBOT_RSI_DISPLAY_NAME,
    evaluate_ema200_utbot_rsi_entry,
    evaluate_ema200_utbot_rsi_loss_gate,
    normalize_ema200_utbot_rsi_config,
)


class SignalEMA200UTBotRSIMixin:
    def _get_ema200_utbot_rsi_config(self, strategy_params=None):
        params = (
            strategy_params
            if isinstance(strategy_params, dict)
            else self.get_runtime_strategy_params()
        )
        raw = params.get(EMA200_UTBOT_RSI_CONFIG_KEY, {}) if isinstance(params, dict) else {}
        return normalize_ema200_utbot_rsi_config(raw)

    @staticmethod
    def _calculate_wilder_rsi_for_ema200_strategy(close_series, length):
        close = pd.Series(close_series, dtype=float).reset_index(drop=True)
        length = max(2, int(length))
        delta = close.diff()
        gains = delta.clip(lower=0.0)
        losses = (-delta.clip(upper=0.0))
        avg_gain = pd.Series(float("nan"), index=close.index, dtype=float)
        avg_loss = pd.Series(float("nan"), index=close.index, dtype=float)
        if len(close) <= length:
            return avg_gain

        # Wilder's RMA starts with a simple average of the first `length`
        # changes, then applies (previous * (length - 1) + current) / length.
        # pandas ewm(adjust=False) seeds from the first observation instead and
        # produces materially different early RSI values.
        avg_gain.iloc[length] = float(gains.iloc[1:length + 1].mean())
        avg_loss.iloc[length] = float(losses.iloc[1:length + 1].mean())
        for index in range(length + 1, len(close)):
            avg_gain.iloc[index] = (
                avg_gain.iloc[index - 1] * (length - 1) + gains.iloc[index]
            ) / length
            avg_loss.iloc[index] = (
                avg_loss.iloc[index - 1] * (length - 1) + losses.iloc[index]
            ) / length
        rs = avg_gain / avg_loss.replace(0.0, float("nan"))
        rsi = 100.0 - (100.0 / (1.0 + rs))
        only_gain = (avg_loss == 0.0) & (avg_gain > 0.0)
        flat = (avg_loss == 0.0) & (avg_gain == 0.0)
        rsi = rsi.mask(only_gain, 100.0).mask(flat, 50.0)
        return rsi

    def _calculate_ema200_utbot_rsi_signal(self, df, strategy_params):
        cfg = self._get_ema200_utbot_rsi_config(strategy_params)
        if not cfg.get("enabled", True):
            return None, f"{EMA200_UTBOT_RSI_DISPLAY_NAME}: 신규 진입 OFF", {
                "enabled": False,
                "timeframe": "2h",
            }

        closed = df.iloc[:-1].copy().reset_index(drop=True)
        ema_period = int(cfg["ema_period"])
        rsi_length = int(cfg["rsi_length"])
        min_bars = max(ema_period + 2, rsi_length + 3)
        if len(closed) < min_bars:
            return None, (
                f"{EMA200_UTBOT_RSI_DISPLAY_NAME}: 데이터 부족 "
                f"({len(closed)}/{min_bars} 완료봉)"
            ), {
                "enabled": True,
                "timeframe": "2h",
                "required_bars": min_bars,
                "available_bars": len(closed),
            }

        close_series = closed["close"].astype(float)
        ema_series = close_series.ewm(span=ema_period, adjust=False).mean()
        rsi_series = self._calculate_wilder_rsi_for_ema200_strategy(
            close_series,
            rsi_length,
        )
        valid_rsi = pd.DataFrame({
            "timestamp": closed["timestamp"],
            "rsi": rsi_series,
        }).dropna().reset_index(drop=True)
        if len(valid_rsi) < 2:
            return None, f"{EMA200_UTBOT_RSI_DISPLAY_NAME}: RSI 계산 대기", {
                "enabled": True,
                "timeframe": "2h",
            }

        ut_sig, ut_reason, ut_detail = self._calculate_utbot_signal(
            df,
            strategy_params,
        )
        curr_rsi_row = valid_rsi.iloc[-1]
        prev_rsi_row = valid_rsi.iloc[-2]
        curr_closed = closed.iloc[-1]
        curr_close = float(curr_closed["close"])
        curr_ema = float(ema_series.iloc[-1])
        rsi_ts = int(curr_rsi_row["timestamp"])
        signal, reason, entry_detail = evaluate_ema200_utbot_rsi_entry(
            close_price=curr_close,
            ema200=curr_ema,
            ut_state=ut_detail.get("bias_side"),
            ut_last_signal_side=ut_detail.get("signal_side"),
            ut_last_signal_ts=ut_detail.get("signal_ts"),
            prev_rsi=float(prev_rsi_row["rsi"]),
            curr_rsi=float(curr_rsi_row["rsi"]),
            rsi_signal_ts=rsi_ts,
            threshold=float(cfg["rsi_threshold"]),
        )

        detail = {
            **entry_detail,
            "enabled": True,
            "timeframe": "2h",
            "ema_period": ema_period,
            "rsi_length": rsi_length,
            "rsi_threshold": float(cfg["rsi_threshold"]),
            "ut_signal": ut_sig,
            "ut_reason": ut_reason,
            "ut_state": ut_detail.get("bias_side"),
            "ut_detail": dict(ut_detail or {}),
            "signal_ts": rsi_ts if signal else None,
            "closed_candle_ts": int(curr_closed["timestamp"]),
            "closed_candle_open": float(curr_closed["open"]),
            "closed_candle_high": float(curr_closed["high"]),
            "closed_candle_low": float(curr_closed["low"]),
            "closed_candle_close": curr_close,
        }
        if signal:
            return signal, f"{EMA200_UTBOT_RSI_DISPLAY_NAME}: {reason}", detail
        return None, f"{EMA200_UTBOT_RSI_DISPLAY_NAME} 대기: {reason}", detail

    async def _ema200_utbot_rsi_new_entry_gate(self, symbol, strategy_params=None):
        cfg = self._get_ema200_utbot_rsi_config(strategy_params)
        try:
            total, free, _ = await self.get_balance_info()
        except Exception as exc:
            return {
                "allowed": False,
                "reason": f"계좌 잔고 확인 실패: {type(exc).__name__}: {exc}",
            }
        equity = float(total or 0.0)
        if equity <= 0:
            equity = float(free or 0.0)
        if equity <= 0:
            return {"allowed": False, "reason": "계좌 equity 확인 불가"}

        try:
            _, daily_pnl = self.db.get_daily_stats()
            _, weekly_pnl = self.db.get_weekly_stats()
        except Exception as exc:
            return {
                "allowed": False,
                "reason": f"손익 기록 확인 실패: {type(exc).__name__}: {exc}",
            }

        gate = evaluate_ema200_utbot_rsi_loss_gate(
            account_equity=equity,
            daily_realized_pnl=daily_pnl,
            weekly_realized_pnl=weekly_pnl,
            config=cfg,
        )
        gate["account_equity"] = equity
        gate["free_balance"] = float(free or 0.0)
        gate["symbol"] = symbol
        return gate

    async def _handle_ema200_utbot_rsi_primary_strategy(
        self,
        symbol,
        k,
        pos,
        strategy_name,
        detail,
        sig,
    ):
        cfg = self._get_ema200_utbot_rsi_config()
        if pos:
            self.last_entry_reason[symbol] = (
                f"포지션 보유 중 ({str(pos.get('side') or '').upper()}), "
                "정상 청산은 2시간봉 UT Bot 반대 신호를 기다립니다."
            )
            return

        if not cfg.get("enabled", True):
            self.last_entry_reason[symbol] = (
                f"{strategy_name}: 신규 진입 OFF. 청산/안전 보호는 유지됩니다."
            )
            return

        if sig not in {"long", "short"}:
            return

        gate = await self._ema200_utbot_rsi_new_entry_gate(symbol)
        if not gate.get("allowed"):
            self.last_entry_reason[symbol] = (
                f"{strategy_name} 신규 진입 차단: {gate.get('reason')}"
            )
            try:
                await self.ctrl.notify(
                    "🛑 EMA200 + UT Bot + RSI (2H) 신규 진입 차단\n"
                    f"심볼: {symbol}\n"
                    f"이유: {gate.get('reason')}\n"
                    "현재 보유 포지션을 강제청산하는 규칙이 아니라, "
                    "추가 손실을 막기 위해 새 진입만 중지하는 리스크 규칙입니다."
                )
            except Exception:
                pass
            return

        side_label = "LONG" if sig == "long" else "SHORT"
        self.last_entry_reason[symbol] = (
            f"{strategy_name} UT 선행 + 현재 RSI 방향 조건 충족 -> {side_label} 진입"
        )
        await self.entry(symbol, sig, float(k["c"]))


__all__ = ("SignalEMA200UTBotRSIMixin",)
