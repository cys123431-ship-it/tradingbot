"""Live ladder exit management and performance persistence."""

from __future__ import annotations

async def _manage_live_ladder_exit_policy(self, symbol, pos, df, cfg):
    state = self._get_utbreakout_trailing_state(symbol)
    if not isinstance(state, dict):
        return None
    if not pos:
        cleanup_status = await self._reconcile_closed_position_protection(
            symbol,
            reason="take profit closed position",
            alert=True,
            attempts=3,
        )
        if isinstance(cleanup_status, dict) and cleanup_status.get("position_active"):
            return {"status": "POSITION_ACTIVE", "audit": cleanup_status}
        if isinstance(cleanup_status, dict) and cleanup_status.get("cleanup_confirmed"):
            await self._record_closed_trade_accounting(
                symbol,
                "take profit/stop loss closed position",
                state=state,
            )
            self._clear_utbreakout_trailing_state(
                symbol,
                finalize=True,
                reason="take profit closed position",
            )
            self._clear_aggressive_growth_position(symbol)
            return {"status": "FLAT_CLEANED", "audit": cleanup_status}
        return {"status": "FLAT_CLEANUP_PENDING", "audit": cleanup_status}
    if not bool(state.get("advanced_live_ladder_state", False)):
        return None
    if df is None or len(df) < 6:
        return None

    try:
        closed_df = df.iloc[:-1].copy().reset_index(drop=True)
        metric_closed_df, post_entry_gate = self._utbreakout_post_entry_closed_bars(
            closed_df,
            state,
            cfg,
        )
        waiting_for_post_entry_bar = bool(
            post_entry_gate
            and (
                metric_closed_df is None
                or metric_closed_df.empty
            )
        )
        if metric_closed_df is None or metric_closed_df.empty:
            metric_closed_df = closed_df
        closed_bar = metric_closed_df.iloc[-1]
        current_bar = {
            "index": int(state.get("bars_seen", 0)),
            "open": float(closed_bar["open"]),
            "high": float(closed_bar["high"]),
            "low": float(closed_bar["low"]),
            "close": float(closed_bar["close"]),
            "volume": float(closed_bar.get("volume", 0.0)),
        }
        raw_bar_ts = closed_bar.get("timestamp", len(metric_closed_df) - 1)
        try:
            closed_bar_ts = int(raw_bar_ts)
        except (TypeError, ValueError, OverflowError):
            closed_bar_ts = str(raw_bar_ts)
        previous_bar_ts = state.get("last_bar_ts")
        if post_entry_gate:
            new_closed_bar = (
                not waiting_for_post_entry_bar
                and closed_bar_ts != previous_bar_ts
            )
            if not waiting_for_post_entry_bar:
                state["bars_seen"] = max(
                    int(state.get("bars_seen", 0) or 0),
                    int(metric_closed_df["timestamp"].nunique())
                    if "timestamp" in metric_closed_df.columns
                    else len(metric_closed_df),
                )
                state["last_bar_ts"] = closed_bar_ts
                current_bar["index"] = int(state["bars_seen"])
                self._set_utbreakout_trailing_state(symbol, state)
        elif previous_bar_ts is None:
            state["last_bar_ts"] = closed_bar_ts
            new_closed_bar = False
            self._set_utbreakout_trailing_state(symbol, state)
        else:
            new_closed_bar = closed_bar_ts != previous_bar_ts
        if new_closed_bar:
            state["bars_seen"] = int(state.get("bars_seen", 0) or 0) + 1
            state["last_bar_ts"] = closed_bar_ts
            current_bar["index"] = int(state["bars_seen"])
            self._set_utbreakout_trailing_state(symbol, state)
    except Exception:
        return None

    state = await self._refresh_ladder_fill_state(symbol, pos, state, cfg)
    audit_status = None
    if hasattr(self, "_audit_protection_orders"):
        audit_status = await self._audit_and_repair_live_ladder_protection(
            symbol,
            pos,
            state,
            cfg,
            reason="live ladder monitor",
        )
    fallback_status = await self._maybe_tp2_fallback_close(symbol, pos, state, cfg, audit_status=audit_status)
    if fallback_status.get("status") == "TP2_FALLBACK_CLOSED":
        self._clear_utbreakout_trailing_state(symbol, finalize=True, reason="TP2 fallback close")
        return {"status": "EXITED", "reason": "TP2_FALLBACK_CLOSE", "fallback": fallback_status}

    if waiting_for_post_entry_bar:
        state["last_exit_policy_reason"] = (
            "waiting for first full post-entry closed bar"
        )
        return self._set_utbreakout_trailing_state(symbol, state)

    if not new_closed_bar:
        return state

    side = str(pos.get("side") or state.get("side") or "").lower()
    entry_price = _safe_float_or_none(pos.get("entryPrice")) or _safe_float_or_none(state.get("entry_price"))
    risk_distance = _safe_float_or_none(state.get("risk_distance"))
    current_r = None
    if entry_price and risk_distance and risk_distance > 0 and side in {"long", "short"}:
        favorable_move = (
            current_bar["close"] - entry_price
            if side == "long"
            else entry_price - current_bar["close"]
        )
        current_r = favorable_move / risk_distance
    position_proxy = {
        "side": side.upper(),
        "timeframe": cfg.get("timeframe", "15m"),
        "entry_bar_index": int(state.get("entry_bar_index", 0)),
        "tp1_filled": bool(state.get("tp1_filled", False)),
        "partial_filled": bool(state.get("tp1_filled", False)),
        "current_r": current_r,
    }

    time_stop = evaluate_time_stop(position_proxy, current_bar, cfg)
    if time_stop.should_exit:
        close_result = await self._close_position_reduce_only_market(symbol, pos, reason=time_stop.reason, cfg=cfg)
        if not (
            bool((close_result or {}).get("_flat_confirmed"))
            and bool((close_result or {}).get("_cleanup_confirmed"))
        ):
            return {"status": "EXIT_PENDING", "reason": time_stop.reason, "order": close_result}
        self._clear_utbreakout_trailing_state(symbol, finalize=True, reason=time_stop.reason)
        return {"status": "EXITED", "reason": time_stop.reason}

    rows = []
    try:
        for i, row in enumerate(closed_df.to_dict("records")):
            rows.append({
                "index": i,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0.0)),
            })
        indicator_values = await self._build_live_indicator_values(symbol, pos.get("side", ""), rows, len(rows) - 1, cfg)
        context = build_market_context(
            rows=rows,
            idx=len(rows) - 1,
            symbol=symbol,
            timeframe=cfg.get("timeframe", "15m"),
            values=indicator_values,
            config=cfg,
        )
    except Exception as context_error:
        logger.warning("Signal invalid context build failed for %s: %s", symbol, context_error)
        return None

    invalid_exit = evaluate_signal_invalid_exit(position_proxy, context, cfg)
    if invalid_exit.should_exit:
        close_result = await self._close_position_reduce_only_market(symbol, pos, reason=invalid_exit.reason, cfg=cfg)
        if not (
            bool((close_result or {}).get("_flat_confirmed"))
            and bool((close_result or {}).get("_cleanup_confirmed"))
        ):
            return {"status": "EXIT_PENDING", "reason": invalid_exit.reason, "order": close_result}
        self._clear_utbreakout_trailing_state(symbol, finalize=True, reason=invalid_exit.reason)
        return {"status": "EXITED", "reason": invalid_exit.reason}
    return None

async def _close_position_reduce_only_market(self, symbol, pos, reason, cfg):
    side = str(pos.get("side", "")).lower()
    if side not in {"long", "short"}:
        raise ValueError(f"invalid position side: {side}")
    qty = self.safe_amount(symbol, abs(float(self._position_signed_contracts(pos) or pos.get("contracts", 0) or 0)))
    if float(qty) <= 0:
        raise ValueError("close qty <= 0")

    params = self._close_order_params_for_position(pos)
    params["reduceOnly"] = True
    _ensure_trading_safety_runtime(self)
    owner = getattr(self, 'ctrl', None) or self
    state = self._get_utbreakout_trailing_state(symbol) or {}
    close_submission = await owner.crypto_execution_service.submit_reduce_only_close(
        strategy=str(state.get("engine") or "UTBREAKOUT_EXIT"),
        symbol=symbol,
        position_side=side,
        position_signature=(
            pos.get("timestamp")
            or pos.get("entryPrice")
            or state.get("created_at")
            or "open-position"
        ),
        qty=float(qty),
        reason=reason,
        params=params,
    )
    order = close_submission.order or {}
    if close_submission.state == OrderState.SUBMITTED_UNKNOWN.value:
        self.crypto_entry_lock_reason = f"SUBMITTED_UNKNOWN:{close_submission.client_order_id}"
        self.trading_state_store.set_runtime_state("entry_lock_reason", self.crypto_entry_lock_reason)
        await self.ctrl.notify(
            f"CRITICAL: {symbol} close order status is unknown. New entries remain locked."
        )
        return {
            "status": OrderState.SUBMITTED_UNKNOWN.value,
            "client_order_id": close_submission.client_order_id,
            "error": close_submission.error,
            "_flat_confirmed": False,
            "_cleanup_confirmed": False,
        }
    await self.ctrl.notify(f"⚠️ {symbol} {reason} 사유로 reduceOnly 시장가 청산 실행")
    await asyncio.sleep(0.5)
    self.position_cache = None
    self.position_cache_time = 0
    position_fetch_ok, remaining_pos = await self._fetch_server_position_checked(symbol)
    result = dict(order) if isinstance(order, dict) else {'order': order}
    result['_position_fetch_ok'] = bool(position_fetch_ok)
    result['_flat_confirmed'] = bool(position_fetch_ok and not remaining_pos)
    if not position_fetch_ok:
        logger.error(f"Reduce-only close accepted but position verification failed for {symbol}")
        return result
    if remaining_pos:
        result['_remaining_position'] = remaining_pos
        logger.warning(
            f"Reduce-only close accepted but position remains for {symbol}: "
            f"{remaining_pos.get('side')} {remaining_pos.get('contracts')}"
        )
        return result

    cleanup_status = await self._reconcile_closed_position_protection(
        symbol,
        reason=f"{reason}: flat confirmed",
        alert=True,
        attempts=3,
    )
    result['_cleanup_status'] = cleanup_status
    result['_cleanup_confirmed'] = bool(
        isinstance(cleanup_status, dict) and cleanup_status.get('cleanup_confirmed')
    )
    order_exit_price = (
        _safe_float_or_none(result.get('average'))
        or _safe_float_or_none(result.get('price'))
    )
    state = self._get_utbreakout_trailing_state(symbol)
    result['_accounting'] = await self._record_closed_trade_accounting(
        symbol,
        reason,
        exit_price=order_exit_price,
        state=state,
    )
    _mark_crypto_symbol_closed(self, symbol, reason)
    return result

def _register_live_ladder_position_state(self, plan, pos, sl_order, tp_orders, cfg):
    side = str(plan.side).lower()
    created_by_label = {}
    for index, order in enumerate(tp_orders or [], 1):
        label = None
        if index <= len(plan.tp_orders or []):
            label = _normalize_tp_plan_label(
                getattr(plan.tp_orders[index - 1], "tp_label", None)
                or getattr(plan.tp_orders[index - 1], "tp_name", None),
                f"TP{index}",
            )
        if not label and hasattr(self, "_protection_tp_label"):
            label = self._protection_tp_label(order, [])
        if label:
            created_by_label[label] = order
    planned_tp_orders = []
    for index, tp in enumerate(plan.tp_orders or [], 1):
        label = _normalize_tp_plan_label(getattr(tp, "tp_label", None) or getattr(tp, "tp_name", None), f"TP{index}")
        created = created_by_label.get(label)
        order_id = self._protection_order_id(created) if created and hasattr(self, "_protection_order_id") else None
        client_order_id = self._protection_client_order_id(created) if created and hasattr(self, "_protection_client_order_id") else None
        tp.order_id = order_id
        tp.client_order_id = client_order_id
        planned_tp_orders.append({
            "tp_index": getattr(tp, "tp_index", None) or index,
            "tp_label": label,
            "tp_name": tp.tp_name,
            "side": tp.side,
            "price": float(tp.price),
            "qty": float(tp.qty),
            "pct": float(tp.pct),
            "reduce_only": bool(tp.reduce_only),
            "close_position": bool(tp.close_position),
            "order_id": order_id,
            "client_order_id": client_order_id,
            "filled": False,
        })
    created_at = datetime.now(timezone.utc).isoformat()
    entry_timestamp_ms = self._resolve_utbreakout_entry_timestamp_ms(
        plan.symbol,
        plan={
            "entry_timestamp_ms": getattr(plan, "entry_timestamp_ms", None),
            "entry_timestamp": getattr(plan, "entry_timestamp", None),
            "entry_time": getattr(plan, "entry_time", None),
            "created_at": created_at,
        },
    )
    state = {
        "advanced_live_ladder_state": True,
        "side": side,
        "entry_price": float(plan.entry_price),
        "initial_qty": float(plan.qty),
        "risk_distance": abs(float(plan.entry_price) - float(plan.initial_sl_price)),
        "initial_stop_price": float(plan.initial_sl_price),
        "last_stop_price": float(plan.initial_sl_price),
        "tp_orders": list(planned_tp_orders),
        "planned_tp_orders": list(planned_tp_orders),
        "expected_tp_count": len(planned_tp_orders),
        "actual_tp_count": len(tp_orders or []),
        "tp1_order_id": (created_by_label.get("TP1") or {}).get("id"),
        "tp2_order_id": (created_by_label.get("TP2") or {}).get("id"),
        "tp1_filled": False,
        "tp2_filled": False,
        "tp3_filled": False,
        "sl_moved_to_be": False,
        "sl_moved_to_tp1_area": False,
        "entry_bar_index": 0,
        "bars_seen": 0,
        "last_bar_ts": None,
        "entry_timestamp_ms": entry_timestamp_ms,
        "entry_timeframe": cfg.get("timeframe", "15m"),
        "engine": plan.engine,
        "regime": plan.regime,
        "confidence": plan.confidence,
        "expected_r": plan.expected_r,
        "runner_exit_enabled": False,
        "runner_chandelier_enabled": False,
        "tp2_fallback_reached_loops": 0,
        "created_at": created_at,
        "sl_order_id": sl_order.get("id") if isinstance(sl_order, dict) else None,
    }
    return self._set_utbreakout_trailing_state(plan.symbol, state)

async def _refresh_ladder_fill_state(self, symbol, pos, state, cfg):
    current_qty = abs(float(self._position_signed_contracts(pos) or pos.get("contracts", 0) or 0))
    initial_qty = float(state.get("initial_qty") or 0.0)
    if initial_qty <= 0:
        return state

    state = self._update_utbreakout_fill_flags_from_position_qty(
        symbol,
        state,
        current_qty,
    )
    tp1_breakeven_enabled = bool(
        (cfg or {}).get(
            "tp1_breakeven_enabled",
            state.get("tp1_breakeven_enabled", True),
        )
    )
    if (
        state.get("tp1_filled")
        and not state.get("tp2_filled")
        and tp1_breakeven_enabled
        and not state.get("sl_moved_to_be")
    ):
        breakeven_stop = self._calculate_be_plus_fees_stop(pos, state, cfg)
        replacement = await self._replace_stop_loss_order(
            symbol,
            pos,
            breakeven_stop,
            reason="TP1 filled -> BE stop"
        )
        if replacement:
            state["sl_moved_to_be"] = True
            state["breakeven_armed"] = True
            state["active"] = True
            state["last_stop_price"] = float(breakeven_stop)
            state["sl_order_id"] = replacement.get("id") if isinstance(replacement, dict) else None
            state["runner_mode"] = "tp1_breakeven"
            self._set_utbreakout_trailing_state(symbol, state)

    if state.get("tp2_filled") and not state.get("sl_moved_to_tp1_area"):
        tp1_area_stop = self._calculate_tp1_area_stop(pos, state, cfg)
        side = str(state.get("side") or pos.get("side") or "").lower()
        current_price = _safe_float_or_none(
            pos.get("markPrice")
            or pos.get("mark_price")
            or pos.get("last")
            or pos.get("currentPrice")
        )
        chosen_stop = float(tp1_area_stop)
        lock_mode = "tp2_tp1_area"
        if current_price is not None:
            tp1_area_valid = (
                chosen_stop < current_price
                if side == "long"
                else chosen_stop > current_price
            )
            if not tp1_area_valid:
                fallback_stop = float(
                    self._calculate_be_plus_fees_stop(pos, state, cfg)
                )
                fallback_valid = (
                    fallback_stop < current_price
                    if side == "long"
                    else fallback_stop > current_price
                )
                if fallback_valid:
                    chosen_stop = fallback_stop
                    lock_mode = "tp2_breakeven_fallback"
        replacement = await self._replace_stop_loss_order(
            symbol,
            pos,
            chosen_stop,
            reason="TP2 filled -> profit protection stop"
        )
        if replacement:
            state["sl_moved_to_tp1_area"] = lock_mode == "tp2_tp1_area"
            state["tp2_profit_lock_pending"] = (
                lock_mode != "tp2_tp1_area"
            )
            state["sl_moved_to_be"] = True
            state["breakeven_armed"] = True
            state["active"] = True
            state["last_stop_price"] = float(chosen_stop)
            state["sl_order_id"] = replacement.get("id") if isinstance(replacement, dict) else None
            state["runner_mode"] = lock_mode
            self._set_utbreakout_trailing_state(symbol, state)

    if hasattr(self, "_audit_protection_orders"):
        self._set_utbreakout_trailing_state(symbol, state)
        await self._audit_and_repair_live_ladder_protection(
            symbol,
            pos,
            state,
            cfg,
            reason="ladder fill-state residual audit",
        )

    return self._set_utbreakout_trailing_state(symbol, state)

def _calculate_be_plus_fees_stop(self, pos, state, cfg):
    entry = float(state["entry_price"])
    risk = float(state["risk_distance"])
    side = str(state["side"]).lower()
    offset_r = float(cfg.get("be_plus_fee_offset_r", 0.03) or 0.03)
    if side == "long":
        return entry + risk * offset_r
    return entry - risk * offset_r

def _calculate_tp1_area_stop(self, pos, state, cfg):
    entry = float(state["entry_price"])
    risk = float(state["risk_distance"])
    side = str(state["side"]).lower()
    tp1_r = 1.0

    tp_orders = (
        state.get("planned_tp_orders")
        or state.get("tp_orders")
        or []
    )
    for tp in tp_orders:
        if not isinstance(tp, dict):
            continue
        label = _normalize_tp_plan_label(
            tp.get("tp_label")
            or tp.get("tp_name")
            or tp.get("label"),
            "",
        )
        price = _safe_float_or_none(tp.get("price"))
        if label == "TP1" and price is not None and price > 0:
            return float(price)
    if side == "long":
        return entry + risk * tp1_r
    return entry - risk * tp1_r

ENGINE_PERFORMANCE_STATS = {}

def _update_engine_performance_stats(trade, cfg=None):
    engine = trade.get("engine")
    if not engine or engine == "NONE":
        return False
    if engine not in ENGINE_PERFORMANCE_STATS:
        ENGINE_PERFORMANCE_STATS[engine] = {
            "trade_count": 0,
            "expectancy_r": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_pct": 0.0,
            "max_consecutive_losses": 0,
            "consecutive_losses": 0,
            "fee_burden": 0.0,
            "funding_burden": 0.0,
            "oos_expectancy": None,
            "gross_profit_r": 0.0,
            "gross_loss_r": 0.0,
        }
    stats = ENGINE_PERFORMANCE_STATS[engine]
    stats["trade_count"] += 1
    pnl_r = float(trade.get("realized_r", trade.get("r_multiple", 0.0)) or 0.0)
    stats["expectancy_r"] += (pnl_r - stats["expectancy_r"]) / stats["trade_count"]
    if pnl_r > 0:
        stats["gross_profit_r"] += pnl_r
    elif pnl_r < 0:
        stats["gross_loss_r"] += abs(pnl_r)
    stats["profit_factor"] = (
        stats["gross_profit_r"] / stats["gross_loss_r"]
        if stats["gross_loss_r"] > 0
        else (float('inf') if stats["gross_profit_r"] > 0 else 0.0)
    )

    if pnl_r < 0:
        stats["consecutive_losses"] += 1
        stats["max_consecutive_losses"] = max(stats["max_consecutive_losses"], stats["consecutive_losses"])
    else:
        stats["consecutive_losses"] = 0

    if should_disable_engine(stats, cfg):
        if cfg:
            disabled = cfg.get("disabled_engines", [])
            if engine not in disabled:
                disabled.append(engine)
                cfg["disabled_engines"] = disabled
                logger.warning(f"Engine {engine} disabled due to poor live performance.")
    return True


def record_live_trade_result(trade, cfg=None, store=None):
    if not isinstance(trade, dict):
        raise TypeError("trade must be a dictionary")
    if not trade.get("trade_id"):
        raise ValueError("trade_id is required")
    persistent_store = store
    if persistent_store is None:
        persistent_store = SQLiteTradingStateStore(
            os.getenv("CRYPTO_TRADING_STATE_DB", "runtime/trading_state.sqlite3")
        )
    outcome = persistent_store.upsert_trade_result(trade)
    if not outcome.inserted and not outcome.updated:
        logger.info("Unchanged live trade result ignored: %s", trade.get("trade_id"))
        return False
    ENGINE_PERFORMANCE_STATS.clear()
    ENGINE_PERFORMANCE_STATS.update(
        rebuild_engine_performance_stats(persistent_store.load_trade_results())
    )
    return True


def _restore_engine_performance_stats(store):
    if store is None:
        return 0
    ENGINE_PERFORMANCE_STATS.clear()
    trades = store.load_trade_results()
    ENGINE_PERFORMANCE_STATS.update(rebuild_engine_performance_stats(trades))
    restored = sum(
        1
        for trade in trades
        if not trade.get('provisional') and trade.get('engine') not in {None, '', 'NONE'}
    )
    logger.info("Restored live engine performance: trades=%d engines=%d", restored, len(ENGINE_PERFORMANCE_STATS))
    return restored

__all__ = (
    '_manage_live_ladder_exit_policy',
    '_close_position_reduce_only_market',
    '_register_live_ladder_position_state',
    '_refresh_ladder_fill_state',
    '_calculate_be_plus_fees_stop',
    '_calculate_tp1_area_stop',
    'ENGINE_PERFORMANCE_STATS',
    '_update_engine_performance_stats',
    'record_live_trade_result',
    '_restore_engine_performance_stats',
)
