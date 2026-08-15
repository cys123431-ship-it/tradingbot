"""Public adaptive options service with the configured capital-sleeve migration."""

from __future__ import annotations

import os

from . import adaptive_runtime, runtime as base_runtime
from .config import OPTIONS_CAPITAL_LIMIT_USDT
from .risk import build_long_option_entry_plan, estimate_option_fee


LEGACY_OPTIONS_CAPITAL_LIMIT_USDT = 20.0

# Keep adaptive scan diagnostics aligned with the current sleeve cap without
# duplicating a second hard-coded amount in the UI/state contract.
adaptive_runtime.SCAN_OUTCOME_LABELS["BUDGET"] = (
    f"{OPTIONS_CAPITAL_LIMIT_USDT:.0f} USDT 예산/최소수량 문제"
)


class OptionsTradingService(adaptive_runtime.OptionsTradingService):
    """Production options service with cap migration and balance-aware sizing."""

    @staticmethod
    def _rewrite_cap_text(text):
        return (
            str(text)
            .replace("20.0000 USDT", f"{OPTIONS_CAPITAL_LIMIT_USDT:.4f} USDT")
            .replace("20 USDT", f"{OPTIONS_CAPITAL_LIMIT_USDT:.0f} USDT")
        )

    def _load_state(self):
        existed_before_load = os.path.exists(self.state_path)
        state = super()._load_state()
        if self._state_unreadable:
            return state

        current_limit = float(OPTIONS_CAPITAL_LIMIT_USDT)
        recorded_limit = state.get("capital_limit_usdt")
        if recorded_limit is None:
            previous_limit = (
                LEGACY_OPTIONS_CAPITAL_LIMIT_USDT
                if existed_before_load
                else current_limit
            )
        else:
            previous_limit = max(0.0, base_runtime._f(recorded_limit, current_limit))

        bankroll = max(
            0.0,
            base_runtime._f(state.get("cash_bankroll_usdt"), previous_limit),
        )
        if current_limit > previous_limit:
            # Add only the newly authorized sleeve capital, preserving all
            # prior realized PnL already embedded in the ledger.
            bankroll += current_limit - previous_limit
        elif current_limit < previous_limit:
            bankroll = max(0.0, bankroll - (previous_limit - current_limit))

        state["cash_bankroll_usdt"] = bankroll
        state["capital_limit_usdt"] = current_limit
        self._save_state(state)
        return state

    def _record_reason(self, reason, *, error=False, candidate=None):
        return super()._record_reason(
            self._rewrite_cap_text(reason),
            error=error,
            candidate=candidate,
        )

    async def _notify(self, text):
        return await super()._notify(self._rewrite_cap_text(text))

    async def _enter(self, selected, snapshot):
        """Enter using the smaller of strategy ledger, live balance and hard cap.

        Adaptive preselection already applies this rule. Repeating it here keeps
        the final order plan consistent if the options account holds less than
        the configured sleeve (for example, 21 USDT with a 100 USDT cap).
        """

        cfg = self.config()
        ask = base_runtime._f(selected.get("ask"))
        tick = base_runtime._f(selected.get("tick_size"))
        limit_price = ask + (tick if tick > 0 else 0.0)
        available = max(
            0.0,
            base_runtime._f((snapshot.get("balance") or {}).get("available")),
        )
        planning_bankroll = min(
            max(0.0, base_runtime._f(self.state.get("cash_bankroll_usdt"))),
            available,
            OPTIONS_CAPITAL_LIMIT_USDT,
        )
        plan = build_long_option_entry_plan(
            ask_price=limit_price,
            index_price=(selected.get("signal") or {}).get("spot_price"),
            unit=selected.get("unit", 1),
            min_qty=selected.get("min_qty") or selected.get("minQty"),
            step_size=selected.get("step_size") or selected.get("minQty"),
            cash_bankroll_usdt=planning_bankroll,
            entry_fraction=cfg.get("entry_fraction", 1.00),
            capital_limit_usdt=OPTIONS_CAPITAL_LIMIT_USDT,
        )
        if not plan.get("accepted"):
            return self._record_reason(
                f"{OPTIONS_CAPITAL_LIMIT_USDT:.0f} USDT 한도에서 주문 수량을 만들 수 없습니다: {plan.get('reason')}",
                candidate=self.state.get("last_candidate"),
            )
        if plan["total_entry_cost_usdt"] > available + 1e-9:
            return self._record_reason("옵션 계좌 사용 가능 잔고가 계획 금액보다 적습니다.")

        symbol = selected.get("symbol")
        signal_key = str((selected.get("signal") or {}).get("signal_key") or "")
        if signal_key:
            consumed = list(self.state.get("consumed_signal_keys") or [])
            if signal_key not in consumed:
                consumed.append(signal_key)
            self.state["consumed_signal_keys"] = consumed[-200:]
            self._save_state()

        client_order_id = self._client_order_id("b")
        price_text = self._format_price(limit_price, tick)
        try:
            response = await self._call(
                self._client().new_order,
                symbol,
                "BUY",
                "LIMIT",
                plan["quantity"],
                price=price_text,
                time_in_force="IOC",
                reduce_only=False,
                client_order_id=client_order_id,
            )
        except base_runtime.BinanceOptionsApiError as exc:
            if exc.uncertain:
                try:
                    response = await self._call(
                        self._client().query_order,
                        symbol,
                        client_order_id=client_order_id,
                    )
                except Exception:
                    return self._record_reason(
                        "옵션 주문 응답이 불확실해 신규 주문을 중단했습니다. 거래소 주문을 확인하세요.",
                        error=True,
                    )
            else:
                return self._record_reason(f"옵션 진입 주문 거절: {exc}", error=True)

        order = await self._resolve_submitted_order(
            symbol,
            client_order_id,
            response,
            limit_price,
        )
        filled_qty = base_runtime._order_executed_quantity(order)
        if filled_qty <= 0:
            return self._record_reason("옵션 IOC 주문이 체결되지 않아 포지션을 만들지 않았습니다.")

        fill_price = base_runtime._order_average_price(order, limit_price)
        unit = base_runtime._f(selected.get("unit"), 1.0)
        premium = fill_price * filled_qty * unit
        entry_fee = estimate_option_fee(
            fill_price,
            (selected.get("signal") or {}).get("spot_price"),
            filled_qty,
            unit,
        )
        total_cost = premium + entry_fee
        if total_cost > OPTIONS_CAPITAL_LIMIT_USDT + 1e-8:
            self.state["last_error"] = "OPTIONS_FILLED_COST_EXCEEDED_HARD_CAP"
            self._save_state()
            await self._notify(
                f"🚨 옵션 체결 비용이 {OPTIONS_CAPITAL_LIMIT_USDT:.0f} USDT 한도를 넘었습니다. "
                "추가 진입을 차단하고 현재 포지션만 관리합니다."
            )

        self.state["cash_bankroll_usdt"] = max(
            0.0,
            base_runtime._f(self.state.get("cash_bankroll_usdt")) - total_cost,
        )
        position = {
            "symbol": symbol,
            "underlying": selected.get("underlying"),
            "side": selected.get("side"),
            "quantity": filled_qty,
            "original_quantity": filled_qty,
            "entry_price": fill_price,
            "entry_premium_usdt": premium,
            "entry_fee_usdt": entry_fee,
            "entry_total_usdt": total_cost,
            "entry_time_ms": int(base_runtime.time.time() * 1000),
            "expiry_date_ms": int(base_runtime._f(selected.get("expiryDate"))),
            "unit": unit,
            "tick_size": tick,
            "peak_mark": fill_price,
            "client_order_id": client_order_id,
            "order_id": order.get("orderId"),
            "signal_score": base_runtime._f((selected.get("signal") or {}).get("score")),
            "option_score": base_runtime._f(selected.get("score")),
            "status": "OPEN",
        }

        signal = selected.get("signal") or {}
        position.update(
            {
                "signal_strategy": signal.get("strategy") or "ADAPTIVE_TREND",
                "entry_mark_iv": base_runtime._f(selected.get("mark_iv")),
                "entry_delta": base_runtime._f(selected.get("delta")),
                "entry_gamma": base_runtime._f(selected.get("gamma")),
                "entry_theta": base_runtime._f(selected.get("theta")),
                "entry_vega": base_runtime._f(selected.get("vega")),
                "target_delta": base_runtime._f(selected.get("target_delta")),
                "target_dte_days": base_runtime._f(selected.get("target_dte_days")),
                "entry_iv_to_realized": base_runtime._f(selected.get("iv_to_realized")),
                "entry_skew_ratio": base_runtime._f(selected.get("skew_ratio")),
            }
        )

        self.state["active_position"] = position
        self.state["last_reason"] = f"{symbol} 옵션 매수 체결"
        self.state["last_error"] = ""
        self._save_state()
        await self._notify(
            "\n".join(
                [
                    "🟣 옵션 자동매매 진입",
                    f"종목: {symbol}",
                    f"방향: {selected.get('side')} | 수량 {filled_qty:g}",
                    f"프리미엄: {premium:.4f} USDT | 예상 수수료 {entry_fee:.4f}",
                    (
                        f"전략 잔여예산: {self.state['cash_bankroll_usdt']:.4f} / "
                        f"{OPTIONS_CAPITAL_LIMIT_USDT:.4f} USDT"
                    ),
                    "네이키드 매도 없이 매수 프리미엄만 위험에 노출됩니다.",
                ]
            )
        )
        return {"action": "entered", "position": position}


__all__ = ("OptionsTradingService",)
