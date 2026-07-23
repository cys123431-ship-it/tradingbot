"""Idempotent live entry and protective-order placement and repair."""

from __future__ import annotations

async def execute_live_order_plan(self, plan, cfg):
    # Final new-entry gate. This protects automatic strategies and the user
    # custom-entry path even if a stale watchlist or cached selector result
    # somehow contains a blocked TradFi commodity. Existing-position exit and
    # protection flows do not call this entry function and remain unaffected.
    ctrl = getattr(self, "ctrl", None)
    if ctrl is not None and hasattr(ctrl, "_assert_symbol_tradeable_in_current_exchange_mode"):
        validated_symbol = await ctrl._assert_symbol_tradeable_in_current_exchange_mode(plan.symbol)
        if validated_symbol:
            plan.symbol = validated_symbol

    plan_engine = str(getattr(plan, "engine", "") or "").strip().lower()
    user_custom_plan = plan_engine in {"user_custom", "custom_entry"}
    if not user_custom_plan:
        automatic_owner = self
        if not hasattr(automatic_owner, "_assert_automatic_entry_scan_scope"):
            automatic_owner = (
                (getattr(ctrl, "engines", {}) or {}).get("signal")
                if ctrl is not None
                else None
            )
        if automatic_owner is not None and hasattr(
            automatic_owner, "_assert_automatic_entry_scan_scope"
        ):
            plan.symbol = await automatic_owner._assert_automatic_entry_scan_scope(
                plan.symbol
            )
        if automatic_owner is not None and hasattr(
            automatic_owner, "get_effective_automatic_daily_trade_limit"
        ):
            daily_limit = int(
                automatic_owner.get_effective_automatic_daily_trade_limit()
            )
            daily_entries = int(
                automatic_owner.get_automatic_daily_entry_count()
                if hasattr(automatic_owner, "get_automatic_daily_entry_count")
                else 0
            )
            if daily_limit > 0 and daily_entries >= daily_limit:
                raise TradingSafetyError(
                    "REJECTED_DAILY_TRADE_LIMIT: automatic daily trade count "
                    f"{daily_entries} >= {daily_limit}"
                )
    cfg = enforce_activation_stage(cfg if isinstance(cfg, dict) else {})
    if _normalize_live_real_stage(cfg.get("live_activation_stage")) == "LIVE_REAL_SMALL_CAP":
        if bool(cfg.get("testnet", False)):
            raise TradingSafetyError("LIVE_REAL_SMALL_CAP must not use testnet")
        if not bool(cfg.get("real_order_enabled", False)) or not bool(cfg.get("live_trading", False)):
            raise TradingSafetyError("LIVE_REAL_SMALL_CAP real order flags are not enabled")
        plan_limits = getattr(plan, "live_real_effective_limits", None)
        plan_equity = (
            plan_limits.get("account_equity_usdt")
            if isinstance(plan_limits, dict)
            else cfg.get("account_reference_equity_usdt")
        )
        apply_live_small_cap_limits_to_config(cfg, plan_equity)
        assert_symbol_allowed_for_live_real(plan.symbol, cfg)
        assert_live_real_loss_limits_not_exceeded(cfg)
        configure_binance_futures_environment(self.exchange, cfg)
    pause_state = load_critical_pause_state()
    pause_decision = evaluate_critical_pause_block(state=pause_state, requested_symbol=plan.symbol)
    if pause_decision.blocked:
        await _handle_critical_pause_entry_block(
            self,
            symbol=plan.symbol,
            raw_symbol=plan.symbol,
            side=plan.side,
            decision=pause_decision,
            qty=getattr(plan, "qty", None),
            phase="EXECUTE_PLAN_PREFLIGHT",
        )
        return {
            "status": "ENTRY_BLOCKED",
            "error": pause_decision.reason_code,
            "client_order_id": None,
            "order_sent": False,
        }

    assert_trading_allowed(plan.symbol, cfg, include_critical_pause=False)
    plan = self._normalize_live_order_plan_for_exchange(plan, cfg)
    cfg["last_price"] = plan.entry_price or cfg.get("last_price") or 0.0
    stop_atr_distance = max(
        0.1,
        float(cfg.get("initial_stop_atr_distance", 2.0) or 2.0),
    )
    cfg["last_context_atr"] = (
        abs(float(cfg["last_price"]) - plan.initial_sl_price)
        / stop_atr_distance
    )
    self._validate_live_order_plan_for_exchange(plan, cfg)

    if _normalize_live_real_stage(cfg.get("live_activation_stage")) == "LIVE_REAL_SMALL_CAP":
        lev = int(max(1.0, min(float(cfg.get("leverage", 5) or 5), float(cfg.get("max_leverage", 5) or 5), 5.0)))
    else:
        lev = int(max(1.0, float(cfg.get("leverage", 5))))
    await self.ensure_market_settings(plan.symbol, leverage=lev)
    liquidation_preflight = await self._preflight_liquidation_safety(
        plan.symbol,
        plan.side,
        plan.entry_price,
        plan.initial_sl_price,
        lev,
        plan.qty,
        cfg,
    )
    if not liquidation_preflight.get('valid'):
        reason = liquidation_preflight.get('reason') or liquidation_preflight.get('status')
        await self.ctrl.notify(
            f"[진입 거부] 전략 스탑이 청산가보다 늦게 발동합니다.\n"
            f"심볼: {plan.symbol}\n방향: {plan.side}\n"
            f"스탑: {plan.initial_sl_price}\n예상 청산가: {liquidation_preflight.get('liquidation_price')}\n"
            f"이유: {reason}"
        )
        return {'status': 'ENTRY_REJECTED_LIQUIDATION_CONFLICT', 'error': reason}
    selected_leverage = int(liquidation_preflight.get('selected_leverage') or lev)
    if selected_leverage < lev:
        lev = selected_leverage
        cfg['leverage'] = lev
        await self.ensure_market_settings(plan.symbol, leverage=lev)
        liquidation_preflight = await self._preflight_liquidation_safety(
            plan.symbol,
            plan.side,
            plan.entry_price,
            plan.initial_sl_price,
            lev,
            plan.qty,
            cfg,
        )
        if not liquidation_preflight.get('valid'):
            return {
                'status': 'ENTRY_REJECTED_LIQUIDATION_CONFLICT',
                'error': liquidation_preflight.get('reason'),
            }
    strategy_name = str(getattr(plan, "engine", None) or cfg.get("active_strategy") or "UTBREAKOUT")
    outcome = await _submit_idempotent_crypto_entry(
        self,
        plan.symbol,
        plan.side,
        plan.qty,
        strategy_name,
        {**dict(cfg or {}), **getattr(plan, "__dict__", {})},
    )
    if outcome.critical_pause_block is not None:
        await _handle_critical_pause_entry_block(
            self,
            symbol=plan.symbol,
            raw_symbol=plan.symbol,
            side=plan.side,
            decision=outcome.critical_pause_block,
            qty=plan.qty,
            phase="EXECUTE_PLAN_LATE_GATE",
        )
        return {
            "status": "ENTRY_BLOCKED",
            "error": outcome.critical_pause_block.reason_code,
            "client_order_id": outcome.client_order_id,
            "order_sent": False,
        }

    if outcome.entry_block_reason is not None:
        await _handle_noncritical_entry_block(
            self,
            symbol=plan.symbol,
            raw_symbol=plan.symbol,
            side=plan.side,
            reason=outcome.entry_block_reason,
            qty=plan.qty,
            phase="EXECUTE_PLAN_LATE_GATE",
        )
        return {
            "status": "ENTRY_BLOCKED",
            "error": outcome.entry_block_reason,
            "client_order_id": outcome.client_order_id,
            "order_sent": False,
        }

    if outcome.duplicate_protected:
        return {
            "status": "DUPLICATE",
            "error": "duplicate signal already protected",
            "client_order_id": outcome.client_order_id,
            "order_sent": False,
            "submission": outcome.submission,
        }

    if outcome.submission_error is not None:
        submission = outcome.submission
        logger.error(
            "[LiveOrderPlan] entry submission not accepted: %s",
            outcome.submission_error,
        )
        await self.ctrl.notify(
            "UTBreakout entry not sent: "
            f"{plan.symbol} {plan.side} / "
            "ENTRY_FAILED / "
            f"{outcome.submission_error}"
        )
        return {
            "status": "ENTRY_FAILED",
            "error": outcome.submission_error,
            "client_order_id": outcome.client_order_id,
            "order_sent": False,
            "submission": submission,
        }

    submission = outcome.submission
    if submission is None:
        raise RuntimeError("ENTRY_SUBMISSION_RESULT_MISSING")
    entry_order = getattr(submission, "order", None) or {}
    client_order_id = outcome.client_order_id

    self.position_cache = None
    self.position_cache_time = 0

    confirmation = await self._confirm_entry_position(
        plan.symbol,
        str(plan.side).lower(),
        entry_order,
        plan.entry_price,
        plan.qty,
    )
    pos = submission.position or confirmation.get("position")
    if not pos:
        _mark_crypto_entry_state(
            self,
            client_order_id,
            OrderState.SUBMITTED_UNKNOWN,
            last_error="entry accepted but fill/position remained unverified",
        )
        self.crypto_entry_lock_reason = f"SUBMITTED_UNKNOWN:{client_order_id}"
        await self.ctrl.notify(
            f"CRITICAL: {plan.symbol} entry state is unknown. New entries are locked pending reconciliation."
        )
        return {
            "status": "SUBMITTED_UNKNOWN",
            "entry_order": entry_order,
            "client_order_id": client_order_id,
        }

    actual_entry = float(pos.get("entryPrice") or plan.entry_price or 0.0)
    actual_qty = abs(float(self._position_signed_contracts(pos) or pos.get("contracts", 0) or plan.qty))

    if actual_entry <= 0 or actual_qty <= 0:
        _mark_crypto_entry_state(
            self,
            client_order_id,
            OrderState.SUBMITTED_UNKNOWN,
            last_error="invalid confirmed fill values",
        )
        self.crypto_entry_lock_reason = f"SUBMITTED_UNKNOWN:{client_order_id}"
        await self.ctrl.notify(f"CRITICAL: invalid confirmed fill for {plan.symbol}; entries locked")
        return {
            "status": "SUBMITTED_UNKNOWN",
            "entry_order": entry_order,
            "client_order_id": client_order_id,
        }

    plan = self._rebuild_plan_after_fill(plan, actual_entry, actual_qty, cfg)
    _mark_crypto_entry_state(
        self,
        client_order_id,
        OrderState.FILLED_UNVERIFIED_LIQUIDATION,
        exchange_order_id=(entry_order.get("id") if isinstance(entry_order, dict) else None),
        filled_qty=actual_qty,
        average_fill_price=actual_entry,
    )

    actual_liquidation_check = await self._verify_actual_liquidation_safety(
        plan.symbol,
        plan.side,
        plan.initial_sl_price,
        pos,
        cfg,
        client_order_id,
    )
    if not actual_liquidation_check.get('valid'):
        return {
            'status': actual_liquidation_check.get('status'),
            'entry_order': entry_order,
            'client_order_id': client_order_id,
            'close_status': actual_liquidation_check.get('close_status'),
        }
    pos = actual_liquidation_check.get('position') or pos

    logger.info(
        "[LiveOrderPlan] entry fill verified: %s side=%s entry=%.12f qty=%.12f",
        plan.symbol,
        plan.side,
        actual_entry,
        actual_qty,
    )
    try:
        sl_order = await self._place_initial_sl_from_plan(plan, pos, cfg)
    except Exception as sl_error:
        if 'SL liquidation safety failed' in str(sl_error):
            return {
                'status': 'FILLED_LIQUIDATION_CONFLICT',
                'entry_order': entry_order,
                'client_order_id': client_order_id,
                'error': str(sl_error),
            }
        # SL 체결 오류 시 영구 락 발동
        return await self._handle_sl_failure_with_persistent_pause(
            symbol=plan.symbol,
            position=pos,
            plan=plan,
            error=sl_error,
            cfg=cfg,
        )

    tp_orders = []
    for tp in plan.tp_orders:
        try:
            order = await self._place_reduce_only_tp_from_plan(plan, tp, cfg)
            tp_orders.append(order)
        except Exception as tp_error:
            logger.error("[LiveOrderPlan] TP placement failed: %s %s", tp.tp_name, tp_error)
            await self.ctrl.notify(f"⚠️ {plan.symbol} {tp.tp_name} 생성 실패. SL은 유지됩니다: {tp_error}")

    state = self._register_live_ladder_position_state(plan, pos, sl_order, tp_orders, cfg)
    audit_status = None
    if hasattr(self, "_audit_protection_orders"):
        audit_status = await self._audit_and_repair_live_ladder_protection(
            plan.symbol,
            pos,
            state,
            cfg,
            reason="initial ladder placement audit",
        )

    sl_order_id = (
        self._protection_order_id(sl_order)
        if hasattr(self, "_protection_order_id")
        else (sl_order or {}).get("id")
    )
    tp_order_ids = [
        self._protection_order_id(order)
        if hasattr(self, "_protection_order_id")
        else order.get("id")
        for order in tp_orders
        if isinstance(order, dict)
    ]
    sl_verified = bool(
        sl_order_id
        and isinstance(audit_status, dict)
        and audit_status.get("fetch_ok")
        and audit_status.get("sl_present")
        and not audit_status.get("sl_qty_mismatch")
    )
    if sl_verified:
        _mark_crypto_entry_state(
            self,
            client_order_id,
            OrderState.PROTECTED,
            stop_order_id=str(sl_order_id),
            take_profit_order_ids=[str(value) for value in tp_order_ids if value],
        )
        if str(getattr(self, 'crypto_entry_lock_reason', '') or '').startswith('FILLED_'):
            self._set_crypto_entry_lock(None)
    else:
        self.crypto_entry_lock_reason = f"FILLED_UNPROTECTED:{client_order_id}"
        self.trading_state_store.set_runtime_state("entry_lock_reason", self.crypto_entry_lock_reason)
        await self.ctrl.notify(
            f"CRITICAL: {plan.symbol} position is not yet verified as stop-loss protected. "
            "New entries are locked while protection recovery continues."
        )

    await self.ctrl.notify(
        f"✅ LiveOrderPlan 진입 완료: {plan.symbol} {plan.side} qty={plan.qty} "
        f"SL={plan.initial_sl_price} TP={len(tp_orders)}/{len(plan.tp_orders)} engine={plan.engine}"
    )

    return {
        "status": "LIVE_ORDER_PLAN_EXECUTED",
        "entry_order": entry_order,
        "sl_order": sl_order,
        "tp_orders": tp_orders,
        "plan": plan,
        "protection_audit": audit_status,
        "client_order_id": client_order_id,
    }

def _rebuild_plan_after_fill(self, plan, actual_entry, actual_qty, cfg):
    sl_price = calculate_initial_stop_price(
        side=plan.side,
        entry_price=actual_entry,
        atr=cfg.get("last_context_atr", abs(actual_entry - plan.initial_sl_price) / 2.0),
        config=cfg,
        engine=plan.engine,
    )
    tp_config = dict(cfg)
    tp_config["symbol"] = plan.symbol
    tp_config["initial_stop_price"] = sl_price

    tp_orders = build_ladder_tp_orders(
        side=plan.side,
        entry_price=actual_entry,
        qty=actual_qty,
        ladder=plan.ladder,
        config=tp_config,
    )
    plan.entry_price = actual_entry
    plan.qty = actual_qty
    plan.initial_sl_price = sl_price
    plan.tp_orders = tp_orders
    return self._normalize_live_order_plan_for_exchange(plan, cfg)

async def _place_initial_sl_from_plan(self, plan, pos, cfg):
    exit_side = "sell" if str(plan.side).upper() == "LONG" else "buy"
    qty = self.safe_amount(plan.symbol, abs(float(self._position_signed_contracts(pos) or pos.get("contracts", 0) or plan.qty)))
    stop_price = self.safe_price(plan.symbol, plan.initial_sl_price)
    if float(qty) <= 0:
        raise InvalidOrderPlan("SL qty <= 0")
    liquidation_check = await self._verify_actual_liquidation_safety(
        plan.symbol,
        plan.side,
        stop_price,
        pos,
        cfg,
        None,
    )
    if not liquidation_check.get('valid'):
        raise InvalidOrderPlan(
            f"SL liquidation safety failed: {liquidation_check.get('status')}"
        )
    pos = liquidation_check.get('position') or pos

    logger.info(
        "[LiveOrderPlan] placing SL: %s side=%s qty=%s stop=%s reduceOnly=True",
        plan.symbol,
        exit_side,
        qty,
        stop_price,
    )
    return await self._create_protection_order_with_retries(
        plan.symbol,
        "stop_market",
        exit_side,
        qty,
        None,
        {
            "stopPrice": stop_price,
            "reduceOnly": True,
            "newClientOrderId": self._build_protection_client_order_id(
                plan.symbol,
                plan.side.lower(),
                "sl",
                pos,
                trigger_price=stop_price,
                quantity=qty,
                leg="sl",
                position_identity=(
                    pos.get('entry_client_order_id')
                    or pos.get('clientOrderId')
                    or pos.get('timestamp')
                    or self._protection_position_signature(pos)
                ),
            ),
        },
        "SL",
        max_attempts=int(cfg.get("sl_place_max_retries", 3) or 3),
        retry_delay_sec=float(cfg.get("sl_retry_delay_sec", 0.7) or 0.7),
    )

async def _place_reduce_only_tp_from_plan(self, plan, tp, cfg):
    qty = self.safe_amount(plan.symbol, tp.qty)
    price = self.safe_price(plan.symbol, tp.price)

    if float(qty) <= 0:
        raise InvalidOrderPlan(f"{tp.tp_name} qty <= 0 after rounding")

    if not tp.reduce_only:
        raise InvalidOrderPlan(f"{tp.tp_name} must be reduceOnly")

    if tp.close_position:
        raise InvalidOrderPlan(f"{tp.tp_name} partial TP must not use closePosition")

    tp_label = _normalize_tp_plan_label(getattr(tp, "tp_label", None) or getattr(tp, "tp_name", ""))
    tp_order_type = str(
        cfg.get(f"{tp_label.lower()}_order_type")
        or cfg.get("tp_order_type", "LIMIT")
    ).upper()

    client_id = self._build_protection_client_order_id(
        plan.symbol,
        str(plan.side).lower(),
        tp.tp_name.lower(),
        {"side": str(plan.side).lower(), "entryPrice": plan.entry_price, "contracts": plan.qty},
        trigger_price=price,
        quantity=qty,
        leg=tp_label,
        position_identity=(
            getattr(plan, 'entry_client_order_id', None)
            or getattr(plan, 'signal_timestamp', None)
            or "|".join(
                (
                    str(plan.symbol),
                    str(plan.side).lower(),
                    str(plan.entry_price),
                    str(plan.qty),
                )
            )
        ),
    )

    if tp_order_type in {"LIMIT", "TAKE_PROFIT_LIMIT"}:
        logger.info(
            "[LiveOrderPlan] placing %s LIMIT: %s side=%s qty=%s price=%s reduceOnly=True",
            tp.tp_name,
            plan.symbol,
            tp.side,
            qty,
            price,
        )
        return await self._create_protection_order_with_retries(
            plan.symbol,
            "limit",
            tp.side,
            qty,
            price,
            {
                "reduceOnly": True,
                "newClientOrderId": client_id,
            },
            tp.tp_name,
            max_attempts=int(cfg.get("tp_place_max_retries", 2) or 2),
        )

    if tp_order_type in {"TAKE_PROFIT_MARKET", "TP_MARKET"}:
        logger.info(
            "[LiveOrderPlan] placing %s TAKE_PROFIT_MARKET: %s side=%s qty=%s stop=%s reduceOnly=True",
            tp.tp_name,
            plan.symbol,
            tp.side,
            qty,
            price,
        )
        return await self._create_protection_order_with_retries(
            plan.symbol,
            "take_profit_market",
            tp.side,
            qty,
            None,
            {
                "stopPrice": price,
                "reduceOnly": True,
                "closePosition": False,
                "newClientOrderId": client_id,
            },
            tp.tp_name,
            max_attempts=int(cfg.get("tp_place_max_retries", 2) or 2),
        )

    raise InvalidOrderPlan(f"Unsupported tp_order_type: {tp_order_type}")

async def _handle_sl_failure_with_persistent_pause(self, symbol, position, plan, error, cfg):
    logger.error("[LiveOrderPlan] SL placement failed completely: %s", error)
    await self.ctrl.notify(f"❌ SL 주문 생성 최종 실패! 즉시 비상 청산을 가동합니다: {error}")
    close_status = await self._emergency_close_position_without_stop_loss(
        symbol=symbol,
        reason=f"SL placement failure: {error}",
        max_attempts=5,
    )
    return {"status": "SL_PLACEMENT_FAILED", "close_status": close_status, "error": str(error)}


def _live_tp_plan_by_label(self, symbol, state):
    if hasattr(self, "_planned_tp_orders_from_state"):
        planned = self._planned_tp_orders_from_state(symbol, state)
    else:
        planned = list((state or {}).get("planned_tp_orders") or (state or {}).get("tp_orders") or [])
    return {
        _normalize_tp_plan_label(item.get("tp_label") or item.get("tp_name") or item.get("label")): dict(item)
        for item in planned or []
        if isinstance(item, dict)
    }


def _order_id_from_any(self, order):
    if not isinstance(order, dict):
        return None
    if hasattr(self, "_protection_order_id"):
        return self._protection_order_id(order)
    info = order.get("info", {}) if isinstance(order.get("info"), dict) else {}
    return str(order.get("id") or order.get("orderId") or info.get("orderId") or "") or None


async def _cancel_labeled_tp_orders(self, symbol, label, planned_tp_orders=None, reason="TP repair"):
    if not hasattr(self, "_collect_protection_orders") or not hasattr(self, "_cancel_protection_orders"):
        return 0
    selected = []
    for order in await self._collect_protection_orders(symbol) or []:
        if self._classify_protection_order(order) != "tp":
            continue
        order_label = self._protection_tp_label(order, planned_tp_orders)
        if order_label == label:
            selected.append(order)
    if not selected:
        return 0
    return await self._cancel_protection_orders(symbol, reason=reason, orders=selected)


async def _repair_missing_tp_order(
    self,
    symbol,
    pos,
    state,
    cfg,
    label="TP2",
    audit_status=None,
    reason="TP audit",
):
    label = _normalize_tp_plan_label(label, "TP2")
    label_key = label.lower()
    status = {"status": "SKIPPED", "symbol": symbol, "label": label}
    state = (
        state
        if isinstance(state, dict)
        else self._get_utbreakout_trailing_state(symbol)
    )
    if not isinstance(state, dict) or not pos:
        status["status"] = "NO_STATE_OR_POSITION"
        return status
    if bool(state.get(f"{label_key}_filled", False)):
        status["status"] = f"{label}_ALREADY_FILLED"
        return status

    current_qty = abs(float(self._position_signed_contracts(pos) or pos.get("contracts", 0) or 0))
    if current_qty <= 0:
        status["status"] = "NO_REMAINING_POSITION"
        return status

    planned_by_label = _live_tp_plan_by_label(self, symbol, state)
    tp_plan = planned_by_label.get(label)
    if not tp_plan:
        logger.error("[%s Repair] no %s plan found for %s", label, label, symbol)
        status["status"] = f"NO_{label}_PLAN"
        return status

    close_side = "sell" if str(pos.get("side") or state.get("side")).lower() == "long" else "buy"
    target_price = _safe_float_value(tp_plan.get("price"), 0.0)
    if target_price <= 0:
        logger.error("[%s Repair] invalid %s price for %s: %s", label, label, symbol, tp_plan.get("price"))
        status["status"] = f"INVALID_{label}_PRICE"
        return status

    audit_status = audit_status or {}
    planned_qty = _safe_float_value(tp_plan.get("qty"), 0.0)
    preserve_runner = bool(
        state.get("preserve_runner_qty")
        or _safe_float_value(state.get("runner_pct"), 0.0) > 0
    )
    use_current_residual = (
        planned_qty <= 0
        or (
            label == "TP2"
            and bool(state.get("tp1_filled", False))
            and not preserve_runner
        )
    )
    repair_qty_source = (
        "current_position"
        if use_current_residual
        else "planned_residual"
    )
    raw_qty = (
        current_qty
        if use_current_residual
        else min(planned_qty, current_qty)
    )
    repair_qty = _safe_float_value(self.safe_amount(symbol, raw_qty), 0.0)
    if repair_qty <= 0:
        logger.error("[%s Repair] %s qty <= 0 after precision for %s: raw=%s", label, label, symbol, raw_qty)
        status.update({"status": f"INVALID_{label}_QTY", "raw_qty": raw_qty})
        return status

    min_amount = self._get_min_amount_for_symbol(symbol)
    if min_amount > 0 and repair_qty + 1e-12 < min_amount:
        logger.error(
            "[%s Repair] minimum amount failed for %s: qty=%.12f min=%.12f",
            label,
            symbol,
            repair_qty,
            min_amount,
        )
        status.update({
            "status": f"{label}_REPAIR_MIN_AMOUNT_FAILED",
            "qty": repair_qty,
            "min_amount": min_amount,
        })
        return status

    try:
        min_notional = float(self._get_min_notional_for_symbol(symbol, cfg)) if hasattr(self, "_get_min_notional_for_symbol") else float((cfg or {}).get("min_notional_usdt", 5.0) or 5.0)
    except Exception:
        min_notional = float((cfg or {}).get("min_notional_usdt", 5.0) or 5.0)
    notional = repair_qty * target_price
    if min_notional > 0 and notional < min_notional:
        # Partial fills can leave a valid reduce-only protection order below
        # the ordinary entry minimum. Let the exchange decide, matching the
        # initial protection path, instead of locally abandoning the repair.
        logger.warning(
            "[%s Repair] reduce-only notional below configured entry minimum "
            "for %s; submitting to exchange: qty=%.12f price=%.12f "
            "notional=%.8f min=%.8f",
            label,
            symbol,
            repair_qty,
            target_price,
            notional,
            min_notional,
        )
        status["min_notional_warning"] = {
            "notional": float(notional),
            "configured_min": float(min_notional),
        }

    tp_order = TakeProfitOrderPlan(
        label,
        close_side,
        target_price,
        repair_qty,
        True,
        False,
        float(tp_plan.get("r_multiple", 0.0) or 0.0),
        float(tp_plan.get("pct", 0.0) or 0.0),
        int(tp_plan.get("tp_index") or (2 if label == "TP2" else 1)),
        label,
    )
    repair_plan = LiveOrderPlan(
        symbol=symbol,
        side="LONG" if str(pos.get("side") or state.get("side")).lower() == "long" else "SHORT",
        entry_type="MARKET",
        entry_price=float(state.get("entry_price") or pos.get("entryPrice") or 0.0),
        qty=current_qty,
        risk_pct=0.0,
        initial_sl_price=float(state.get("last_stop_price") or state.get("initial_stop_price") or 0.0),
        tp_orders=[tp_order],
        ladder=None,
        reduce_only=False,
        engine=str(state.get("engine") or ""),
        regime=str(state.get("regime") or ""),
        confidence=float(state.get("confidence") or 0.0),
        expected_r=float(state.get("expected_r") or 0.0),
        reasons=[reason],
    )

    try:
        await _cancel_labeled_tp_orders(
            self,
            symbol,
            label,
            planned_by_label.values(),
            reason=f"{reason}: recreate {label}",
        )
        order = await self._place_reduce_only_tp_from_plan(repair_plan, tp_order, cfg or {})
    except Exception as exc:
        logger.error("[%s Repair] %s recreate failed for %s: %s", label, label, symbol, exc)
        await self.ctrl.notify(f"🚨 {symbol} {label} 재생성 실패: {exc}")
        status.update({"status": f"{label}_REPAIR_FAILED", "error": str(exc)})
        return status

    order_id = _order_id_from_any(self, order)
    client_id = self._protection_client_order_id(order) if hasattr(self, "_protection_client_order_id") else None
    for item in state.get("planned_tp_orders") or state.get("tp_orders") or []:
        if isinstance(item, dict) and _normalize_tp_plan_label(item.get("tp_label") or item.get("tp_name")) == label:
            item["qty"] = float(repair_qty)
            item["price"] = float(target_price)
            item["side"] = close_side
            item["order_id"] = order_id
            item["client_order_id"] = client_id
            item["filled"] = False
    state[f"{label_key}_order_id"] = order_id
    state[f"last_{label_key}_repair_ts"] = datetime.now(timezone.utc).isoformat()
    self._set_utbreakout_trailing_state(symbol, state)
    self._persist_active_entry_protection_refs(
        symbol,
        take_profit_order_ids=[order_id] if order_id else [],
        take_profit_order_labels=(
            {str(order_id): label}
            if order_id
            else {}
        ),
    )
    logger.info(
        "[%s Repair] recreated %s for %s: side=%s qty=%.12f price=%.12f source=%s order_id=%s",
        label,
        label,
        symbol,
        close_side,
        repair_qty,
        target_price,
        repair_qty_source,
        order_id,
    )
    await self.ctrl.notify(f"✅ {symbol} {label} 재생성 완료: {close_side} {repair_qty:.8f} @ {target_price:.8f}")
    status.update({"status": f"{label}_REPAIRED", "order": order, "qty": repair_qty, "price": target_price, "qty_source": repair_qty_source})
    return status


async def _repair_missing_tp2_order(self, symbol, pos, state, cfg, audit_status=None, reason="TP2 audit"):
    return await _repair_missing_tp_order(
        self,
        symbol,
        pos,
        state,
        cfg,
        label="TP2",
        audit_status=audit_status,
        reason=reason,
    )


async def _audit_and_repair_live_ladder_protection(self, symbol, pos, state, cfg, reason="ladder audit"):
    collapse = self._collapse_state_tp_plan_for_exchange(symbol, pos, state)
    planned = _live_tp_plan_by_label(self, symbol, state)
    audit = await self._audit_protection_orders(
        symbol,
        pos=pos,
        expected_tp=True,
        expected_sl=True,
        planned_tp_orders=list(planned.values()),
        alert=True,
    )
    if collapse:
        audit["tp_min_amount_fallback"] = collapse
    if audit.get("emergency_close_status"):
        return audit
    if audit.get("missing_sl") or audit.get("sl_qty_mismatch"):
        stop_price = float((state or {}).get("last_stop_price") or (state or {}).get("initial_stop_price") or 0.0)
        if stop_price > 0:
            logger.warning(
                "[Protection Audit] SL repair required for %s; replacing SL at %.12f",
                symbol,
                stop_price,
            )
            repaired_sl = await self._replace_stop_loss_order(
                symbol,
                pos,
                stop_price,
                reason=f"{reason}: SL repair",
            )
            audit["sl_repair_order"] = repaired_sl
            if repaired_sl:
                state["active"] = True
                state["last_stop_price"] = float(stop_price)
                state["sl_order_id"] = repaired_sl.get("id") if isinstance(repaired_sl, dict) else None
                self._set_utbreakout_trailing_state(symbol, state)
    if audit.get("missing_tp1") and "TP1" in planned:
        logger.warning("[Protection Audit] TP1 repair required for %s: %s", symbol, audit.get("status"))
        repair = await _repair_missing_tp_order(
            self,
            symbol,
            pos,
            state,
            cfg,
            label="TP1",
            audit_status=audit,
            reason=reason,
        )
        audit["tp1_repair"] = repair
    if audit.get("missing_tp2") or audit.get("tp2_qty_mismatch"):
        logger.warning("[Protection Audit] TP2 repair required for %s: %s", symbol, audit.get("status"))
        repair = await _repair_missing_tp2_order(self, symbol, pos, state, cfg, audit_status=audit, reason=reason)
        audit["tp2_repair"] = repair
    return audit


async def _fetch_tp2_price_snapshot(self, symbol):
    snapshot = {"last": None, "bid": None, "ask": None}
    try:
        ticker = await asyncio.to_thread(self.exchange.fetch_ticker, symbol)
        if isinstance(ticker, dict):
            snapshot["last"] = _safe_float_or_none(ticker.get("last") or ticker.get("mark") or ticker.get("close"))
            snapshot["bid"] = _safe_float_or_none(ticker.get("bid"))
            snapshot["ask"] = _safe_float_or_none(ticker.get("ask"))
    except Exception as exc:
        logger.warning("[TP2 Fallback] fetch_ticker failed for %s: %s", symbol, exc)
    if snapshot["bid"] is None or snapshot["ask"] is None:
        try:
            book = await asyncio.to_thread(self.exchange.fetch_order_book, symbol, 5)
            if isinstance(book, dict):
                bids = book.get("bids") or []
                asks = book.get("asks") or []
                if bids:
                    snapshot["bid"] = _safe_float_or_none(bids[0][0])
                if asks:
                    snapshot["ask"] = _safe_float_or_none(asks[0][0])
        except Exception as exc:
            logger.debug("[TP2 Fallback] fetch_order_book skipped for %s: %s", symbol, exc)
    return snapshot


def _tp2_target_reached(side, target_price, snapshot):
    target = _safe_float_value(target_price, 0.0)
    if target <= 0 or not isinstance(snapshot, dict):
        return False
    values = [_safe_float_or_none(snapshot.get(key)) for key in ("last", "bid", "ask")]
    values = [float(value) for value in values if value is not None and float(value) > 0]
    if not values:
        return False
    side = str(side or "").lower()
    if side == "long":
        return max(values) >= target
    if side == "short":
        return min(values) <= target
    return False


async def _maybe_tp2_fallback_close(self, symbol, pos, state, cfg, audit_status=None):
    if not bool((cfg or {}).get("enable_tp2_fallback_close", False)):
        return {"status": "DISABLED"}
    if not isinstance(state, dict) or not pos or bool(state.get("tp2_filled", False)):
        return {"status": "SKIPPED"}
    planned = _live_tp_plan_by_label(self, symbol, state)
    tp2_plan = planned.get("TP2")
    if not tp2_plan:
        return {"status": "NO_TP2_PLAN"}
    side = str(pos.get("side") or state.get("side") or "").lower()
    snapshot = await _fetch_tp2_price_snapshot(self, symbol)
    if not _tp2_target_reached(side, tp2_plan.get("price"), snapshot):
        state["tp2_fallback_reached_loops"] = 0
        self._set_utbreakout_trailing_state(symbol, state)
        return {"status": "NOT_REACHED", "snapshot": snapshot}

    loops = int(state.get("tp2_fallback_reached_loops", 0) or 0) + 1
    state["tp2_fallback_reached_loops"] = loops
    self._set_utbreakout_trailing_state(symbol, state)
    confirm_loops = max(1, int((cfg or {}).get("tp2_fallback_confirm_loops", 2) or 2))
    logger.warning(
        "[TP2 Fallback] target reached for %s loop %s/%s, price=%s snapshot=%s audit=%s",
        symbol,
        loops,
        confirm_loops,
        tp2_plan.get("price"),
        snapshot,
        (audit_status or {}).get("status"),
    )
    if loops < confirm_loops:
        return {"status": "CONFIRMING", "loops": loops, "snapshot": snapshot}
    if not bool((cfg or {}).get("tp2_fallback_use_market", True)):
        return {"status": "REACHED_MARKET_DISABLED", "loops": loops, "snapshot": snapshot}

    self.position_cache = None
    self.position_cache_time = 0
    position_fetch_ok, fresh_pos = await self._fetch_server_position_checked(symbol)
    if not position_fetch_ok:
        return {"status": "POSITION_FETCH_FAILED"}
    if not fresh_pos:
        state["tp2_fallback_reached_loops"] = 0
        self._set_utbreakout_trailing_state(symbol, state)
        return {"status": "ALREADY_FLAT"}
    order = await self._close_position_reduce_only_market(symbol, fresh_pos, reason="TP2 fallback close", cfg=cfg or {})
    state["tp2_fallback_reached_loops"] = 0
    if not (
        bool((order or {}).get("_flat_confirmed"))
        and bool((order or {}).get("_cleanup_confirmed"))
    ):
        self._set_utbreakout_trailing_state(symbol, state)
        return {
            "status": "TP2_FALLBACK_CLOSE_PENDING",
            "order": order,
            "snapshot": snapshot,
        }
    state["tp2_fallback_closed_at"] = datetime.now(timezone.utc).isoformat()
    self._set_utbreakout_trailing_state(symbol, state)
    logger.warning("[TP2 Fallback] reduceOnly market close executed for %s: %s", symbol, order)
    await self.ctrl.notify(f"⚠️ {symbol} TP2 도달 후 미체결 fallback 시장가 청산 실행")
    return {"status": "TP2_FALLBACK_CLOSED", "order": order, "snapshot": snapshot}

__all__ = (
    'execute_live_order_plan',
    '_rebuild_plan_after_fill',
    '_place_initial_sl_from_plan',
    '_place_reduce_only_tp_from_plan',
    '_handle_sl_failure_with_persistent_pause',
    '_live_tp_plan_by_label',
    '_order_id_from_any',
    '_cancel_labeled_tp_orders',
    '_repair_missing_tp_order',
    '_repair_missing_tp2_order',
    '_audit_and_repair_live_ladder_protection',
    '_fetch_tp2_price_snapshot',
    '_tp2_target_reached',
    '_maybe_tp2_fallback_close',
)
