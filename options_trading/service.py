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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ownership_recovery_done = False

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

    @staticmethod
    def _pending_entry(selected, quantity, price, client_order_id, execution_mode):
        signal = selected.get("signal") or {}
        return {
            "symbol": selected.get("symbol"),
            "underlying": selected.get("underlying") or signal.get("underlying"),
            "side": selected.get("side"),
            "quantity": base_runtime._f(quantity),
            "submitted_price": base_runtime._f(price),
            "unit": base_runtime._f(selected.get("unit"), 1.0),
            "tick_size": base_runtime._f(selected.get("tick_size")),
            "expiry_date_ms": int(base_runtime._f(selected.get("expiryDate"))),
            "client_order_id": client_order_id,
            "execution_mode": execution_mode,
            "created_at_ms": int(base_runtime.time.time() * 1000),
            "signal_key": str(signal.get("signal_key") or ""),
            "signal_score": base_runtime._f(signal.get("score")),
            "signal_strategy": signal.get("strategy") or "ADAPTIVE_TREND",
            "spot_price": base_runtime._f(signal.get("spot_price")),
            "option_score": base_runtime._f(selected.get("score")),
            "entry_mark_iv": base_runtime._f(selected.get("mark_iv")),
            "entry_delta": base_runtime._f(selected.get("delta")),
            "entry_gamma": base_runtime._f(selected.get("gamma")),
            "entry_theta": base_runtime._f(selected.get("theta")),
            "entry_vega": base_runtime._f(selected.get("vega")),
            "target_delta": base_runtime._f(selected.get("target_delta")),
            "target_dte_days": base_runtime._f(selected.get("target_dte_days")),
            "entry_iv_to_realized": base_runtime._f(selected.get("iv_to_realized")),
            "entry_skew_ratio": base_runtime._f(selected.get("skew_ratio")),
            "entry_surface_iv_premium_pct": base_runtime._f(
                selected.get("surface_iv_premium_pct")
            ),
            "entry_net_expected_edge_pct": base_runtime._f(
                selected.get("net_expected_edge_pct")
            ),
            "entry_ioc_net_expected_edge_pct": base_runtime._f(
                selected.get("ioc_net_expected_edge_pct")
            ),
            "entry_flow_score": base_runtime._f(selected.get("flow_score")),
        }

    def _set_pending_entry(
        self, selected, quantity, price, client_order_id, execution_mode
    ):
        self.state["pending_entry"] = self._pending_entry(
            selected, quantity, price, client_order_id, execution_mode
        )
        self._save_state()

    def _clear_pending_entry(self, client_order_id=None):
        pending = self.state.get("pending_entry") or {}
        if client_order_id and pending.get("client_order_id") != client_order_id:
            return
        self.state["pending_entry"] = None
        self._save_state()

    @staticmethod
    def _order_is_terminal(order):
        return str((order or {}).get("status") or "").upper() in {
            "FILLED",
            "CANCELED",
            "REJECTED",
            "EXPIRED",
        }

    async def _submit_ioc_entry(self, selected, quantity, limit_price, tick):
        symbol = selected.get("symbol")
        client_order_id = self._client_order_id("b")
        price_text = self._format_price(limit_price, tick)
        self._set_pending_entry(
            selected, quantity, limit_price, client_order_id, "IOC"
        )
        try:
            response = await self._call(
                self._client().new_order,
                symbol,
                "BUY",
                "LIMIT",
                quantity,
                price=price_text,
                time_in_force="IOC",
                reduce_only=False,
                client_order_id=client_order_id,
            )
        except base_runtime.BinanceOptionsApiError as exc:
            if not exc.uncertain:
                self._clear_pending_entry(client_order_id)
                raise
            try:
                response = await self._call(
                    self._client().query_order,
                    symbol,
                    client_order_id=client_order_id,
                )
            except Exception as query_exc:
                raise base_runtime.BinanceOptionsApiError(
                    "OPTIONS_IOC_ORDER_STATUS_UNKNOWN", uncertain=True
                ) from query_exc
        order = await self._resolve_submitted_order(
            symbol,
            client_order_id,
            response,
            limit_price,
        )
        return order, client_order_id, limit_price, "IOC"

    async def _submit_entry_order(self, selected, quantity, ioc_price, tick, cfg):
        """Try post-only first, then cross only when the net edge survives."""

        symbol = selected.get("symbol")
        bid = max(0.0, base_runtime._f(selected.get("bid")))
        ask = max(0.0, base_runtime._f(selected.get("ask")))
        maker_enabled = bool(cfg.get("maker_first_enabled", True)) and bid > 0
        if not maker_enabled:
            return await self._submit_ioc_entry(
                selected,
                quantity,
                ioc_price,
                tick,
            )

        maker_price = bid + tick if tick > 0 and bid + tick < ask else bid
        maker_price = max(tick or 1e-12, maker_price)
        maker_client_id = self._client_order_id("m")
        self._set_pending_entry(
            selected, quantity, maker_price, maker_client_id, "MAKER"
        )
        try:
            response = await self._call(
                self._client().new_order,
                symbol,
                "BUY",
                "LIMIT",
                quantity,
                price=self._format_price(maker_price, tick),
                time_in_force="GTC",
                post_only=True,
                reduce_only=False,
                client_order_id=maker_client_id,
            )
        except base_runtime.BinanceOptionsApiError as exc:
            if exc.uncertain:
                try:
                    response = await self._call(
                        self._client().query_order,
                        symbol,
                        client_order_id=maker_client_id,
                    )
                except Exception as query_exc:
                    raise base_runtime.BinanceOptionsApiError(
                        "OPTIONS_MAKER_ORDER_STATUS_UNKNOWN",
                        uncertain=True,
                    ) from query_exc
            elif bool(selected.get("ioc_eligible")):
                self._clear_pending_entry(maker_client_id)
                return await self._submit_ioc_entry(
                    selected,
                    quantity,
                    ioc_price,
                    tick,
                )
            else:
                self._clear_pending_entry(maker_client_id)
                raise

        order = response if isinstance(response, dict) else {}
        if self._order_is_terminal(order):
            if base_runtime._order_executed_quantity(order) > 0:
                return order, maker_client_id, maker_price, "MAKER"
        else:
            await base_runtime.asyncio.sleep(
                base_runtime._f(cfg.get("maker_wait_seconds"), 2.0)
            )
            try:
                queried = await self._call(
                    self._client().query_order,
                    symbol,
                    client_order_id=maker_client_id,
                )
                if isinstance(queried, dict):
                    order = queried
            except Exception as exc:
                base_runtime.logger.warning(
                    "Options maker order query failed before cancel %s: %s",
                    maker_client_id,
                    exc,
                )

        if not self._order_is_terminal(order):
            try:
                canceled = await self._call(
                    self._client().cancel_order,
                    symbol,
                    client_order_id=maker_client_id,
                )
                if isinstance(canceled, dict):
                    order = canceled
            except Exception as exc:
                raise base_runtime.BinanceOptionsApiError(
                    "OPTIONS_MAKER_CANCEL_FAILED",
                    uncertain=True,
                ) from exc
            try:
                queried = await self._call(
                    self._client().query_order,
                    symbol,
                    client_order_id=maker_client_id,
                )
                if (
                    isinstance(queried, dict)
                    and base_runtime._order_executed_quantity(queried)
                    >= base_runtime._order_executed_quantity(order)
                ):
                    order = queried
            except Exception as exc:
                base_runtime.logger.warning(
                    "Options maker reconciliation query failed after confirmed cancel %s: %s",
                    maker_client_id,
                    exc,
                )

        if base_runtime._order_executed_quantity(order) > 0:
            return order, maker_client_id, maker_price, "MAKER"
        if not bool(selected.get("ioc_eligible")):
            self._clear_pending_entry(maker_client_id)
            return {}, maker_client_id, maker_price, "MAKER_ONLY"
        self._clear_pending_entry(maker_client_id)
        return await self._submit_ioc_entry(
            selected,
            quantity,
            ioc_price,
            tick,
        )

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
            entry_fraction=min(
                base_runtime._f(cfg.get("entry_fraction"), 1.00),
                base_runtime._f(selected.get("entry_fraction"), 1.00),
            ),
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
        try:
            order, client_order_id, submitted_price, execution_mode = (
                await self._submit_entry_order(
                    selected,
                    plan["quantity"],
                    limit_price,
                    tick,
                    cfg,
                )
            )
        except base_runtime.BinanceOptionsApiError as exc:
            if exc.uncertain:
                return self._record_reason(
                    "옵션 주문 상태 또는 메이커 취소 상태가 불확실해 신규 주문을 중단했습니다. 거래소 주문을 확인하세요.",
                    error=True,
                )
            return self._record_reason(f"옵션 진입 주문 거절: {exc}", error=True)

        filled_qty = base_runtime._order_executed_quantity(order)
        if filled_qty <= 0:
            self._clear_pending_entry(client_order_id)
            return self._record_reason(
                "옵션 메이커 주문이 미체결됐고 IOC 기준 순기대수익도 부족해 추격 진입하지 않았습니다."
            )

        fill_price = base_runtime._order_average_price(order, submitted_price)
        signal_key = str((selected.get("signal") or {}).get("signal_key") or "")
        if signal_key:
            consumed = list(self.state.get("consumed_signal_keys") or [])
            if signal_key not in consumed:
                consumed.append(signal_key)
            self.state["consumed_signal_keys"] = consumed[-200:]
            self._save_state()
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
            "entry_execution_mode": execution_mode,
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
                "entry_surface_iv_premium_pct": base_runtime._f(selected.get("surface_iv_premium_pct")),
                "entry_net_expected_edge_pct": base_runtime._f(selected.get("net_expected_edge_pct")),
                "entry_ioc_net_expected_edge_pct": base_runtime._f(selected.get("ioc_net_expected_edge_pct")),
                "entry_flow_score": base_runtime._f(selected.get("flow_score")),
            }
        )

        self.state["active_position"] = position
        self.state["pending_entry"] = None
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

    @staticmethod
    def _order_client_id(order):
        return str(
            (order or {}).get("clientOrderId")
            or (order or {}).get("client_order_id")
            or ""
        )

    @staticmethod
    def _contract_metadata(exchange_info, symbol):
        rows = (
            (exchange_info or {}).get("optionSymbols", [])
            if isinstance(exchange_info, dict)
            else base_runtime._rows(exchange_info)
        )
        for row in rows:
            if str((row or {}).get("symbol") or "") != symbol:
                continue
            filters = {
                str(item.get("filterType") or ""): item
                for item in (row.get("filters") or [])
                if isinstance(item, dict)
            }
            lot = filters.get("LOT_SIZE", {})
            price_filter = filters.get("PRICE_FILTER", {})
            return {
                "symbol": symbol,
                "underlying": row.get("underlying"),
                "side": row.get("side")
                or ("CALL" if str(symbol).endswith("-C") else "PUT"),
                "unit": base_runtime._f(row.get("unit"), 1.0),
                "tick_size": base_runtime._f(
                    row.get("tickSize") or price_filter.get("tickSize")
                ),
                "min_qty": base_runtime._f(
                    row.get("minQty") or lot.get("minQty")
                ),
                "expiry_date_ms": int(base_runtime._f(row.get("expiryDate"))),
            }
        return None

    async def _activate_recovered_entry(self, pending, order, exchange_quantity):
        filled_qty = base_runtime._order_executed_quantity(order)
        managed_qty = min(max(0.0, exchange_quantity), max(0.0, filled_qty))
        if managed_qty <= 1e-12:
            self._clear_pending_entry(pending.get("client_order_id"))
            return None
        fill_price = base_runtime._order_average_price(
            order, pending.get("submitted_price")
        )
        unit = max(1e-12, base_runtime._f(pending.get("unit"), 1.0))
        premium = fill_price * managed_qty * unit
        fee = estimate_option_fee(
            fill_price,
            pending.get("spot_price"),
            managed_qty,
            unit,
        )
        total_cost = premium + fee
        position = {
            "symbol": pending.get("symbol"),
            "underlying": pending.get("underlying"),
            "side": pending.get("side"),
            "quantity": managed_qty,
            "original_quantity": managed_qty,
            "entry_price": fill_price,
            "entry_premium_usdt": premium,
            "entry_fee_usdt": fee,
            "entry_total_usdt": total_cost,
            "entry_time_ms": int(
                order.get("updateTime")
                or order.get("time")
                or pending.get("created_at_ms")
                or base_runtime.time.time() * 1000
            ),
            "expiry_date_ms": int(pending.get("expiry_date_ms") or 0),
            "unit": unit,
            "tick_size": base_runtime._f(pending.get("tick_size")),
            "peak_mark": fill_price,
            "client_order_id": pending.get("client_order_id"),
            "order_id": order.get("orderId"),
            "entry_execution_mode": pending.get("execution_mode") or "RECOVERED",
            "signal_score": base_runtime._f(pending.get("signal_score")),
            "option_score": base_runtime._f(pending.get("option_score")),
            "signal_strategy": pending.get("signal_strategy") or "RECOVERED_BOT_ORDER",
            "entry_mark_iv": base_runtime._f(pending.get("entry_mark_iv")),
            "entry_delta": base_runtime._f(pending.get("entry_delta")),
            "entry_gamma": base_runtime._f(pending.get("entry_gamma")),
            "entry_theta": base_runtime._f(pending.get("entry_theta")),
            "entry_vega": base_runtime._f(pending.get("entry_vega")),
            "target_delta": base_runtime._f(pending.get("target_delta")),
            "target_dte_days": base_runtime._f(pending.get("target_dte_days")),
            "entry_iv_to_realized": base_runtime._f(
                pending.get("entry_iv_to_realized")
            ),
            "entry_skew_ratio": base_runtime._f(pending.get("entry_skew_ratio")),
            "entry_surface_iv_premium_pct": base_runtime._f(
                pending.get("entry_surface_iv_premium_pct")
            ),
            "entry_net_expected_edge_pct": base_runtime._f(
                pending.get("entry_net_expected_edge_pct")
            ),
            "entry_ioc_net_expected_edge_pct": base_runtime._f(
                pending.get("entry_ioc_net_expected_edge_pct")
            ),
            "entry_flow_score": base_runtime._f(pending.get("entry_flow_score")),
            "status": "OPEN",
            "recovered_after_restart": True,
        }
        self.state["cash_bankroll_usdt"] = max(
            0.0,
            base_runtime._f(self.state.get("cash_bankroll_usdt")) - total_cost,
        )
        self.state["active_position"] = position
        self.state["pending_entry"] = None
        signal_key = str(pending.get("signal_key") or "")
        if signal_key:
            consumed = list(self.state.get("consumed_signal_keys") or [])
            if signal_key not in consumed:
                consumed.append(signal_key)
            self.state["consumed_signal_keys"] = consumed[-200:]
        self.state["last_reason"] = f"{pending.get('symbol')} 봇 옵션 포지션 재연결"
        self.state["last_error"] = ""
        self._save_state()
        await self._notify(
            "♻️ 재시작 후 봇 옵션 포지션을 주문 ID로 확인해 자동 관리에 다시 연결했습니다.\n"
            f"종목: {pending.get('symbol')} | 관리 수량 {managed_qty:g}"
        )
        return {"action": "recovered", "position": position}

    async def _resolve_pending_entry(self, pending, snapshot):
        symbol = str(pending.get("symbol") or "")
        client_order_id = str(pending.get("client_order_id") or "")
        if not symbol or not client_order_id.startswith(base_runtime.BOT_CLIENT_PREFIX):
            return self._record_reason("OPTIONS_PENDING_ENTRY_INVALID", error=True)
        try:
            order = await self._call(
                self._client().query_order,
                symbol,
                client_order_id=client_order_id,
            )
            if not self._order_is_terminal(order):
                await self._call(
                    self._client().cancel_order,
                    symbol,
                    client_order_id=client_order_id,
                )
                order = await self._call(
                    self._client().query_order,
                    symbol,
                    client_order_id=client_order_id,
                )
        except Exception as exc:
            return self._record_reason(
                f"옵션 재시작 주문 확인 실패: {exc}", error=True
            )
        exchange_quantity = 0.0
        for row in snapshot.get("positions") or []:
            if str(row.get("symbol") or "") == symbol:
                exchange_quantity = abs(base_runtime._position_quantity(row))
                break
        if base_runtime._order_executed_quantity(order) <= 1e-12:
            if self._order_is_terminal(order):
                self._clear_pending_entry(client_order_id)
                return None
            return self._record_reason(
                "봇 옵션 진입 주문이 아직 미확정 상태라 신규 주문을 차단했습니다."
            )
        return await self._activate_recovered_entry(
            pending, order, exchange_quantity
        )

    async def _recover_legacy_bot_position(self, snapshot):
        exchange_info = None
        for row in snapshot.get("positions") or []:
            symbol = str(row.get("symbol") or "")
            exchange_quantity = abs(base_runtime._position_quantity(row))
            if not symbol or exchange_quantity <= 1e-12:
                continue
            try:
                trades = base_runtime._rows(
                    await self._call(self._client().user_trades, symbol, limit=100)
                )
                order_cache = {}
                bot_buys = []
                bot_sell_qty = 0.0
                for trade in trades:
                    order_id = trade.get("orderId")
                    if order_id not in order_cache:
                        order_cache[order_id] = await self._call(
                            self._client().query_order, symbol, order_id=order_id
                        )
                    order = order_cache[order_id] or {}
                    if not self._order_client_id(order).startswith(
                        base_runtime.BOT_CLIENT_PREFIX
                    ):
                        continue
                    qty = abs(
                        base_runtime._f(trade.get("quantity") or trade.get("qty"))
                    )
                    if str(trade.get("side") or "").upper() == "BUY":
                        bot_buys.append((trade, order, qty))
                    elif str(trade.get("side") or "").upper() == "SELL":
                        bot_sell_qty += qty
                owned_qty = max(0.0, sum(item[2] for item in bot_buys) - bot_sell_qty)
                if owned_qty <= 1e-12:
                    continue
                if exchange_info is None:
                    exchange_info = await self._get_exchange_info()
                metadata = self._contract_metadata(exchange_info, symbol)
                if not metadata or not metadata.get("expiry_date_ms"):
                    continue
                managed_qty = min(exchange_quantity, owned_qty)
                weighted = sum(
                    base_runtime._f(trade.get("price")) * qty
                    for trade, _, qty in bot_buys
                )
                entry_price = weighted / max(sum(item[2] for item in bot_buys), 1e-12)
                first_trade, first_order, _ = bot_buys[0]
                pending = {
                    **metadata,
                    "quantity": managed_qty,
                    "submitted_price": entry_price,
                    "client_order_id": self._order_client_id(first_order),
                    "execution_mode": "LEGACY_RECOVERY",
                    "created_at_ms": int(
                        first_trade.get("time")
                        or first_order.get("updateTime")
                        or base_runtime.time.time() * 1000
                    ),
                    "spot_price": 0.0,
                    "signal_strategy": "RECOVERED_BOT_ORDER",
                }
                synthetic_order = {
                    "status": "FILLED",
                    "executedQty": managed_qty,
                    "avgPrice": entry_price,
                    "orderId": first_order.get("orderId"),
                    "updateTime": pending["created_at_ms"],
                }
                return await self._activate_recovered_entry(
                    pending, synthetic_order, exchange_quantity
                )
            except Exception as exc:
                base_runtime.logger.info(
                    "Options ownership check skipped for %s: %s", symbol, exc
                )
        return None

    async def _recover_runtime_ownership(self):
        if self._ownership_recovery_done:
            return None
        if not self.config().get("enabled") and not self.state.get("pending_entry"):
            self._ownership_recovery_done = True
            return None
        try:
            snapshot = await self._private_snapshot()
        except Exception as exc:
            return self._record_reason(
                f"옵션 재시작 소유권 확인 실패: {exc}", error=True
            )
        pending = self.state.get("pending_entry") or None
        if pending:
            result = await self._resolve_pending_entry(pending, snapshot)
            if result is not None:
                if self.state.get("active_position"):
                    self._ownership_recovery_done = True
                return result
        recovered = await self._recover_legacy_bot_position(snapshot)
        self._ownership_recovery_done = True
        return recovered


__all__ = ("OptionsTradingService",)
