"""BTC SMA3/SMA200 cross: stop-and-reverse with a margin-ROI profit lock.

Inherits order execution, reconciliation, fail-safe, accounting and DRY_RUN
plumbing from the BTC pullback service; only the signal, sizing and the
exit logic differ:

* entry: SMA3 crosses SMA200 on a closed candle (timeframe from Telegram)
* opposite cross while holding: close (reduce-only) and enter the other way
* profit lock: margin ROI +5% -> stop at +4%, then one step per +5%
* optional emergency stop (margin-ROI loss, closePosition on mark price)
* after a profit-lock / emergency exit: wait for the next cross
"""

from __future__ import annotations

from btc_ema_pullback.config import TRADING_MODE_DRY_RUN, TRADING_MODE_LIVE
from btc_ema_pullback.rules import D, ceil_to_step, floor_to_step, round_price_to_tick
from btc_ema_pullback.signals import TIMEFRAME_MS
from btc_ema_pullback.service import (
    CCXT_SYMBOL,
    ERROR_RECOVERY,
    IDLE,
    POSITION_OPEN,
    PRE_TRADE_VALIDATION,
    PROTECTION_FAILED,
    PROTECTION_ORDERS_SUBMITTED,
    SIGNAL_DETECTED,
    BtcEmaPullbackService,
    _f,
    _iso,
    _position_qty,
)
from trading_safety.order_state import OrderState

from .config import CLIENT_ID_STRATEGY, STRATEGY_VERSION, normalize_btc_ma_cross_config
from .signals import emergency_stop_price, evaluate_cross, profit_lock_target

# Skip reasons that can clear up within minutes; a reverse entry blocked by
# one of them is retried instead of being dropped until the next cross.
TRANSIENT_ENTRY_BLOCKS = (
    "OPEN_ORDERS_UNKNOWN",
    "OPEN_ORDERS_CANCEL_FAILED",
    "POSITION_STATE_UNCERTAIN",
    "ACCOUNT_POSITION_EXISTS",
    "BALANCE_UNAVAILABLE",
    "TRADING_RULES_UNAVAILABLE",
    "ACCOUNT_SETUP_UNVERIFIED",
    "POSITION_MODE_UNKNOWN",
)


class BtcMaCrossService(BtcEmaPullbackService):
    CLIENT_ID = CLIENT_ID_STRATEGY
    RUNTIME_SUBDIR = "btc_ma_cross"
    LABEL = "BTC 3/200 SMA"
    PROTECTION_KINDS = ("sl", "lock")
    EXIT_REASON_BY_KIND = {"sl": "EMERGENCY_STOP", "lock": "PROFIT_LOCK"}
    normalize_config = staticmethod(normalize_btc_ma_cross_config)

    # ------------------------------------------------------------- signal
    async def cross_signal(self, network, cfg):
        exchange = await self.exchange(network)
        rows = await self._call(exchange.fetch_ohlcv, CCXT_SYMBOL, cfg["timeframe"], None, int(cfg["candle_history"]))
        if not isinstance(rows, list):
            raise ValueError("invalid kline response")
        return evaluate_cross(rows, cfg, self.now_ms())

    async def _evaluate_new_bar(self, network, cfg, mode, state):
        """Return the cross signal of a newly closed candle (None if already seen)."""
        try:
            signal = await self.cross_signal(network, cfg)
        except Exception as exc:
            reason = f"MARKET_DATA_UNAVAILABLE: {type(exc).__name__}: {exc}"
            state["last_error"] = reason
            self.save_state(network, mode, state)
            self.ledger(network).event("SKIP", mode=mode, network=network, skip_reason=reason)
            return None, reason
        bar = int(signal.get("bar_open_ms") or 0)
        if bar and bar == int(state.get("last_evaluated_bar") or 0):
            return None, "WAITING_NEXT_CLOSE"
        if bar:
            state["last_evaluated_bar"] = bar
        state["last_error"] = ""
        self.ledger(network).event("SIGNAL", mode=mode, network=network, **signal)
        self.save_state(network, mode, state)
        return signal, signal.get("skip_reason") or ""

    # -------------------------------------------------------------- entry
    async def maybe_enter(self, network, cfg, mode, state):
        pending = state.get("pending_reverse_entry")
        if pending:
            return await self._retry_reverse_entry(network, cfg, mode, state, pending)
        signal, reason = await self._evaluate_new_bar(network, cfg, mode, state)
        if signal is None:
            return {"action": "waiting" if reason == "WAITING_NEXT_CLOSE" else "skip", "reason": reason}
        if not signal.get("side"):
            return {"action": "skip", "reason": reason or "NO_CROSS"}
        return await self._enter_on_signal(network, cfg, mode, state, signal)

    async def _enter_on_signal(self, network, cfg, mode, state, signal):
        signal_id = signal["signal_id"]
        if signal_id in state["processed_signals"]:
            return {"action": "skip", "reason": "DUPLICATE_SIGNAL"}
        self._set_phase(network, mode, state, SIGNAL_DETECTED, last_signal_id=signal_id)
        self._set_phase(network, mode, state, PRE_TRADE_VALIDATION)
        decision, reason, context = await self.pre_trade_validation(network, cfg, mode, signal)
        state["processed_signals"].append(signal_id)
        if reason:
            self.ledger(network).event("SKIP", mode=mode, network=network, signal_id=signal_id,
                                       side=signal["side"], skip_reason=reason, **context)
            self._set_phase(network, mode, state, IDLE)
            await self._notify(f"⚠️ {self.LABEL}: {signal['side']} 크로스 진입 건너뜀 — {reason}", key=f"skip:{signal_id}")
            return {"action": "skip", "reason": reason}
        if mode == TRADING_MODE_LIVE:
            return await self.enter_live(network, cfg, state, signal, decision, context)
        return await self.enter_paper(network, cfg, state, signal, decision, context)

    async def pre_trade_validation(self, network, cfg, mode, signal):
        """Sizing = margin_fraction of the available balance x fixed leverage."""
        context = {}
        try:
            wallet, available, equity, balance_source = await self.balances(network, cfg, mode)
        except Exception as exc:
            return None, f"BALANCE_UNAVAILABLE: {exc}", context
        context.update(wallet_balance=wallet, available_balance=available, equity=equity, balance_source=balance_source)
        if mode == TRADING_MODE_LIVE:
            try:
                positions = await self.fetch_account_positions(network)
            except Exception as exc:
                return None, f"POSITION_STATE_UNCERTAIN: {exc}", context
            if positions:
                held = ", ".join(f"{p.get('symbol')} {p.get('side')} {_position_qty(p)}" for p in positions[:3])
                return None, f"ACCOUNT_POSITION_EXISTS: {held}", context
            setup_error = await self.ensure_account_setup(network, cfg)
            if setup_error:
                return None, setup_error, context
            stray = await self.cancel_stray_protection(network)
            if stray:
                return None, stray, context
        try:
            rules = await self.trading_rules(network, cfg)
            market = await self.premium_index(network)
        except Exception as exc:
            return None, f"TRADING_RULES_UNAVAILABLE: {type(exc).__name__}: {exc}", context
        fee_rate, fee_source = await self.taker_fee_rate(network, cfg)
        price = D(str(market["mark_price"]))
        leverage = D(cfg["leverage"])
        margin = D(str(available)) * D(cfg["margin_fraction"])
        quantity = floor_to_step(margin * leverage / price, rules.effective_step)
        required = max(rules.effective_min_qty, ceil_to_step(rules.min_notional / price, rules.effective_step))
        context.update(mark_price=market["mark_price"], funding_rate=market["funding_rate"], taker_fee_rate=fee_rate,
                       fee_source=fee_source, margin=margin, leverage=leverage, quantity=quantity,
                       exchange_minimum_quantity=required, step_size=rules.effective_step, min_notional=rules.min_notional)
        if quantity < required:
            if not cfg["allow_auto_raise_to_exchange_minimum"]:
                return None, (
                    f"BELOW_EXCHANGE_MINIMUM: {quantity} < {required} BTC "
                    f"(증거금 {margin:.2f} USDT x {leverage}배, 최소 주문 {rules.min_notional} USDT)"
                ), context
            quantity = required
        if quantity > rules.effective_max_qty:
            return None, f"ABOVE_MAX_QTY: {quantity} > {rules.effective_max_qty}", context
        notional = quantity * price
        needed = notional / leverage
        if needed > D(str(available)) * D(cfg["max_margin_usage_pct"]):
            return None, f"INSUFFICIENT_MARGIN: needs {needed:.4f} > available {available:.4f} USDT", context
        decision = {
            "accepted": True,
            "quantity": quantity,
            "notional": notional,
            "margin": needed,
            "estimated_entry_fee": notional * fee_rate,
            "estimated_total_risk": None,
            "sizing_mode": f"margin_{cfg['margin_fraction']}x{cfg['leverage']}",
            "rules": rules,
            "fee_rate": fee_rate,
        }
        return decision, "", context

    def _new_trade(self, network, mode, signal, decision, context, *, trade_id, entry_price, quantity, entry_ms, rules, cfg):
        entry = D(entry_price)
        qty = D(quantity)
        leverage = int(cfg["leverage"])
        stop = emergency_stop_price(signal["side"], entry, leverage, cfg["emergency_stop_roi_percent"])
        stop_text = ""
        if stop is not None:
            # Rounded towards the entry: the loss never exceeds the chosen ROI.
            stop_text = str(round_price_to_tick(stop, rules.tick_size, "up" if signal["side"] == "LONG" else "down"))
        margin = entry * qty / D(leverage)
        return {
            "trade_id": trade_id,
            "signal_id": signal["signal_id"],
            "signal_bar_ms": self._signal_bar(signal),
            "mode": mode,
            "network": network,
            "status": "OPEN",
            "side": signal["side"],
            "quantity": str(qty),
            "entry_price": str(entry),
            "stop_price": stop_text,
            "take_profit_price": "",
            "entry_time": _iso(entry_ms),
            "entry_ms": entry_ms,
            # R multiple of this strategy = net PnL / margin (margin ROI).
            "initial_risk": str(margin),
            "margin_used": str(margin),
            "leverage": leverage,
            "fee_rate": str(decision.get("fee_rate") or cfg["estimated_taker_fee_rate"]),
            "strategy_version": STRATEGY_VERSION,
            "rule_violation": "",
            "emergency_stop_roi": cfg["emergency_stop_roi_percent"],
            "best_price": str(entry),
            "lock_roi": "",
            "lock_price": "",
            "sl_revision": 0,
            "lock_revision": 0,
            "timeframe": cfg["timeframe"],
            "tick_size": str(rules.tick_size),
        }

    def _protection_spec(self, cfg, trade, kind):
        if kind == "sl":
            return {"order_type": "STOP_MARKET", "trigger": trade["stop_price"], "close_position": True,
                    "working_type": cfg["working_type"]}
        # Profit lock: reduce-only STOP_MARKET for the position quantity so it can
        # coexist with the closePosition emergency stop.
        return {"order_type": "STOP_MARKET", "trigger": trade["lock_price"], "close_position": False,
                "quantity": trade["quantity"], "working_type": cfg["lock_working_type"]}

    async def attach_protection(self, network, cfg, state):
        mode = TRADING_MODE_LIVE
        trade = state["trade"]
        self._set_phase(network, mode, state, PROTECTION_ORDERS_SUBMITTED)
        if trade.get("stop_price"):
            result = await self.place_protection(network, cfg, trade, "sl")
            self.ledger(network).event("PROTECTION", network=network, kind="sl", result=result,
                                       trigger=trade["stop_price"], working_type=cfg["working_type"])
            if result != "OK":
                state["trade"] = trade
                return await self.failsafe_close(network, cfg, state, f"SL_{result}")
        trade["last_protection_check_ms"] = self.now_ms()
        entry_id = trade.get("entry_client_order_id")
        if entry_id and self.store(network).get(entry_id):
            self.store(network).transition(entry_id, OrderState.PROTECTED, stop_order_id=trade.get("sl_client_algo_id"))
        self._set_phase(network, mode, state, POSITION_OPEN, trade=trade)
        return {"action": "entered", "trade": trade}

    # ---------------------------------------------------------- profit lock
    def _lock_update(self, cfg, trade, price):
        """Track the best price; return the new lock target if it moved up."""
        long = trade["side"] == "LONG"
        best = _f(trade.get("best_price"), trade["entry_price"])
        best = max(best, price) if long else min(best, price)
        trade["best_price"] = str(best)
        target = profit_lock_target(trade["side"], trade["entry_price"], best, trade["leverage"],
                                    cfg["lock_start_roi_percent"], cfg["lock_step_percent"], cfg["lock_gap_percent"])
        if target is None:
            return None
        reached, locked, stop = target
        if trade.get("lock_roi") not in ("", None) and locked <= _f(trade["lock_roi"]) + 1e-9:
            return None
        tick = trade.get("tick_size") or "0.1"
        rounded = round_price_to_tick(stop, tick, "down" if long else "up")
        return {"reached": reached, "locked": locked, "price": str(rounded)}

    async def _raise_lock_live(self, network, cfg, state, target):
        trade = state["trade"]
        old_id = trade.get("lock_client_algo_id")
        previous = (trade.get("lock_roi"), trade.get("lock_price"))
        trade["lock_roi"], trade["lock_price"] = str(target["locked"]), target["price"]
        if old_id:
            trade["lock_revision"] = int(trade.get("lock_revision") or 0) + 1
        result = await self.place_protection(network, cfg, trade, "lock")
        self.ledger(network).event("PROFIT_LOCK", network=network, reached_roi=target["reached"],
                                   locked_roi=target["locked"], trigger=target["price"], result=result)
        if result == "BREACHED":
            # Price already below the new lock: take the profit now.
            state["trade"] = trade
            return await self._close_live(network, cfg, state, "profit_lock_breached", "PROFIT_LOCK")
        if result != "OK":
            trade["lock_roi"], trade["lock_price"] = previous
            state["trade"] = trade
            self.save_state(network, TRADING_MODE_LIVE, state)
            return None
        if old_id and old_id != trade.get("lock_client_algo_id"):
            try:
                await self.cancel_algo(network, old_id)
            except Exception as exc:
                self.ledger(network).event("CANCEL_FAILED", network=network, client_algo_id=old_id, error=str(exc))
        state["trade"] = trade
        self.save_state(network, TRADING_MODE_LIVE, state)
        await self._notify(
            f"🔒 {self.LABEL}: 증거금 수익 +{target['reached']:g}% 도달 → +{target['locked']:g}% 확보 손절 {target['price']}",
            key=f"lock:{trade['trade_id']}:{target['locked']}",
        )
        return None

    # ------------------------------------------------------------- LIVE
    async def _close_live(self, network, cfg, state, reason, exit_reason):
        trade = state["trade"]
        position = await self.fetch_position(network)
        if not position:
            return await self.finalize_live(network, cfg, state, exit_reason)
        order_gateway, _ = await self.gateway(network)
        result = await order_gateway.submit_reduce_only_close(
            strategy=self.CLIENT_ID, symbol=CCXT_SYMBOL, position_side=trade["side"].lower(),
            position_signature=trade["signal_bar_ms"], qty=_position_qty(position), reason=reason,
        )
        self.ledger(network).event("CLOSE_ORDER", network=network, reason=reason, state=result.state, error=result.error)
        if result.state == OrderState.CLOSED.value:
            return await self.finalize_live(network, cfg, state, exit_reason)
        state["last_error"] = f"CLOSE_{result.state}: {result.error or ''}".strip()
        self.save_state(network, TRADING_MODE_LIVE, state)
        return {"action": "closing", "reason": state["last_error"]}

    async def manage_live(self, network, cfg, state):
        mode = TRADING_MODE_LIVE
        trade = state["trade"]
        try:
            position = await self.fetch_position(network)
        except Exception as exc:
            self.ledger(network).event("POSITION_FETCH_FAILED", network=network, error=str(exc))
            return {"action": "managed", "reason": "POSITION_UNKNOWN"}
        if not position:
            return await self.finalize_live(network, cfg, state, None)
        if state.get("phase") in {PROTECTION_FAILED, ERROR_RECOVERY} or trade.get("failsafe"):
            return await self.failsafe_close(network, cfg, state, state.get("last_error") or "RETRY_FAILSAFE")
        pending = trade.get("pending_reverse")
        if pending:
            return await self._reverse_live(network, cfg, state, pending)
        signal, _ = await self._evaluate_new_bar(network, cfg, mode, state)
        trade = state["trade"]
        if signal and signal.get("side") and signal["side"] != trade["side"]:
            return await self._reverse_live(network, cfg, state, signal)
        try:
            mark = (await self.premium_index(network))["mark_price"]
        except Exception as exc:
            self.ledger(network).event("PRICE_FETCH_FAILED", network=network, error=str(exc))
            return {"action": "managed", "reason": "PRICE_UNKNOWN"}
        target = self._lock_update(cfg, trade, mark)
        state["trade"] = trade
        self.save_state(network, mode, state)
        if target:
            closed = await self._raise_lock_live(network, cfg, state, target)
            if closed:
                return closed
        due = self.now_ms() - int(state["trade"].get("last_protection_check_ms") or 0) >= int(cfg["protection_check_interval_seconds"]) * 1000
        if due:
            await self.verify_protection(network, cfg, state)
        return {"action": "managed"}

    async def _reverse_live(self, network, cfg, state, signal):
        """Opposite cross: close the position, then enter the other way."""
        trade = state["trade"]
        trade["pending_reverse"] = signal
        state["trade"] = trade
        self.save_state(network, TRADING_MODE_LIVE, state)
        closed = await self._close_live(network, cfg, state, "cross_reverse", "CROSS_REVERSE")
        if closed.get("action") != "exited":
            return closed
        state = self.load_state(network, TRADING_MODE_LIVE)
        if not cfg["enabled"]:
            return {"action": "exited", "reason": "CROSS_REVERSE (신규진입 OFF라 반대 진입 안 함)"}
        entered = await self._enter_on_signal(network, cfg, TRADING_MODE_LIVE, state, signal)
        if self._hold_for_retry(network, TRADING_MODE_LIVE, signal, entered):
            return {"action": "exited", "reason": f"CROSS_REVERSE · 반대 진입 보류 후 재시도: {entered.get('reason')}"}
        return {"action": "reversed", "close": closed, "entry": entered}

    def _hold_for_retry(self, network, mode, signal, entered):
        """Keep a reverse entry that a temporary problem blocked; True if held."""
        reason = str(entered.get("reason") or "")
        if entered.get("action") != "skip" or not reason.startswith(TRANSIENT_ENTRY_BLOCKS):
            return False
        state = self.load_state(network, mode)
        state["processed_signals"] = [s for s in state["processed_signals"] if s != signal["signal_id"]]
        state["pending_reverse_entry"] = signal
        self.save_state(network, mode, state)
        self.ledger(network).event("REVERSE_ENTRY_HELD", mode=mode, network=network,
                                   signal_id=signal["signal_id"], reason=reason)
        return True

    async def _retry_reverse_entry(self, network, cfg, mode, state, signal):
        bar_close_ms = self._signal_bar(signal) + TIMEFRAME_MS[signal.get("timeframe") or cfg["timeframe"]]
        if self.now_ms() - bar_close_ms > int(cfg["reverse_retry_window_seconds"]) * 1000:
            state["pending_reverse_entry"] = None
            if signal["signal_id"] not in state["processed_signals"]:
                state["processed_signals"].append(signal["signal_id"])
            self.save_state(network, mode, state)
            self.ledger(network).event("REVERSE_ENTRY_EXPIRED", mode=mode, network=network, signal_id=signal["signal_id"])
            await self._notify(f"⚠️ {self.LABEL}: 반대 진입을 {cfg['reverse_retry_window_seconds'] // 60}분 동안 재시도했지만 "
                               "막혀 있어 포기했습니다. 다음 크로스까지 대기합니다.", key=f"expired:{signal['signal_id']}")
            return {"action": "skip", "reason": "REVERSE_ENTRY_EXPIRED"}
        state["pending_reverse_entry"] = None
        self.save_state(network, mode, state)
        entered = await self._enter_on_signal(network, cfg, mode, state, signal)
        if self._hold_for_retry(network, mode, signal, entered):
            return {"action": "waiting", "reason": f"REVERSE_ENTRY_RETRY: {entered.get('reason')}"}
        return entered

    async def verify_protection(self, network, cfg, state):
        trade = state["trade"]
        try:
            orders = await self.open_algo_orders(network)
        except Exception as exc:
            self.ledger(network).event("PROTECTION_CHECK_FAILED", network=network, error=str(exc))
            return
        ids = {str(o.get("clientAlgoId") or "") for o in orders}
        if trade.get("stop_price") and trade.get("sl_client_algo_id") not in ids:
            trade["sl_revision"] = int(trade.get("sl_revision") or 0) + 1
            result = await self.place_protection(network, cfg, trade, "sl")
            self.ledger(network).event("PROTECTION_REPAIR", network=network, kind="sl", result=result)
            if result != "OK":
                state["trade"] = trade
                await self.failsafe_close(network, cfg, state, f"SL_MISSING_{result}")
                return
        if trade.get("lock_price") and trade.get("lock_client_algo_id") not in ids:
            trade["lock_revision"] = int(trade.get("lock_revision") or 0) + 1
            result = await self.place_protection(network, cfg, trade, "lock")
            self.ledger(network).event("PROTECTION_REPAIR", network=network, kind="lock", result=result)
            if result == "BREACHED":
                state["trade"] = trade
                await self._close_live(network, cfg, state, "profit_lock_breached", "PROFIT_LOCK")
                return
        trade["last_protection_check_ms"] = self.now_ms()
        state["trade"] = trade
        self.save_state(network, TRADING_MODE_LIVE, state)

    # ------------------------------------------------------------ DRY_RUN
    async def manage_paper(self, network, cfg, state):
        mode = TRADING_MODE_DRY_RUN
        trade = state["trade"]
        exchange = await self.exchange(network)
        since = int(trade.get("paper_last_checked_ms") or trade["entry_ms"])
        try:
            rows = await self._call(exchange.fetch_ohlcv, CCXT_SYMBOL, "1m", since - 60_000, 1000)
        except Exception as exc:
            self.ledger(network).event("SKIP", mode=mode, network=network, skip_reason=f"PAPER_PRICE_UNAVAILABLE: {exc}")
            return {"action": "managed", "reason": "PRICE_UNAVAILABLE"}
        long = trade["side"] == "LONG"
        slip = _f(cfg["slippage_buffer_pct"])
        for row in sorted(rows or [], key=lambda r: r[0]):
            if int(row[0]) + 60_000 <= int(trade["entry_ms"]):
                continue
            high, low = _f(row[2]), _f(row[3])
            close_ms = int(row[0]) + 60_000
            # Stops first with the levels known before this candle (conservative).
            for kind, level in (("EMERGENCY_STOP", trade.get("stop_price")), ("PROFIT_LOCK", trade.get("lock_price"))):
                if not level:
                    continue
                level = _f(level)
                if (long and low <= level) or (not long and high >= level):
                    price = level * (1 - slip) if long else level * (1 + slip)
                    await self._settle_paper_funding(network, trade, close_ms)
                    return await self.finalize_paper(network, cfg, state, price, kind, close_ms)
            target = self._lock_update(cfg, trade, high if long else low)
            if target:
                trade["lock_roi"], trade["lock_price"] = str(target["locked"]), target["price"]
                self.ledger(network).event("PROFIT_LOCK", mode=mode, network=network, dry_run=True,
                                           reached_roi=target["reached"], locked_roi=target["locked"], trigger=target["price"])
        now = self.now_ms()
        trade["paper_last_checked_ms"] = now
        await self._settle_paper_funding(network, trade, now)
        state["trade"] = trade
        self.save_state(network, mode, state)
        signal, _ = await self._evaluate_new_bar(network, cfg, mode, state)
        if signal and signal.get("side") and signal["side"] != trade["side"]:
            try:
                price = (await self.premium_index(network))["mark_price"]
            except Exception:
                price = _f(signal.get("close"))
            price = price * (1 - slip) if long else price * (1 + slip)
            await self.finalize_paper(network, cfg, state, price, "CROSS_REVERSE", now)
            state = self.load_state(network, mode)
            if not cfg["enabled"]:
                return {"action": "exited", "reason": "CROSS_REVERSE"}
            entered = await self._enter_on_signal(network, cfg, mode, state, signal)
            return {"action": "reversed", "entry": entered}
        return {"action": "managed"}

    # ------------------------------------------------------------ texts
    def _entry_text(self, trade, mode):
        tag = "🧪 DRY_RUN" if mode == TRADING_MODE_DRY_RUN else "💥 LIVE"
        stop = f"비상손절 {trade['stop_price']}" if trade.get("stop_price") else "비상손절 OFF"
        return (
            f"📈 {self.LABEL} 진입 ({tag} · {trade['network']} · {trade.get('timeframe')})\n"
            f"{trade['side']} {trade['quantity']} BTC @ {trade['entry_price']} · 증거금 {_f(trade['margin_used']):.2f} USDT x{trade['leverage']}\n"
            f"{stop} · 증거금 +5% 도달 시 수익 확보 손절 시작"
        )

    def _exit_text(self, trade, mode):
        tag = "🧪 DRY_RUN" if mode == TRADING_MODE_DRY_RUN else "💥 LIVE"
        roi = _f(trade.get("net_pnl")) / max(_f(trade.get("margin_used")), 1e-9) * 100
        return (
            f"🏁 {self.LABEL} 청산 ({tag} · {trade['network']}) — {trade['exit_reason']}\n"
            f"{trade['side']} {trade['entry_price']} → {_f(trade['exit_price']):.1f}\n"
            f"순손익 {_f(trade['net_pnl']):+.4f} USDT (증거금 대비 {roi:+.2f}%, 수수료 {_f(trade['commission']):.4f}, "
            f"펀딩 {_f(trade['funding']):+.4f})"
        )


__all__ = ("BtcMaCrossService",)
