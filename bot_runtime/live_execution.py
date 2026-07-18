"""Exchange sizing, TP ladder normalization, and live plan validation."""

from __future__ import annotations

async def _fetch_futures_account_equity(self, cfg):
    if self.is_upbit_mode():
        raise InvalidOrderPlan("advanced live futures equity is not available in Upbit mode")

    balance = await asyncio.to_thread(self.exchange.fetch_balance)
    candidates = []

    try:
        usdt_total = balance.get("USDT", {}).get("total")
        if usdt_total is not None:
            candidates.append(float(usdt_total))
    except Exception:
        pass

    try:
        usdt_free = balance.get("USDT", {}).get("free")
        if usdt_free is not None:
            candidates.append(float(usdt_free))
    except Exception:
        pass

    try:
        info = balance.get("info", {})
        total_wallet_balance = info.get("totalWalletBalance")
        if total_wallet_balance is not None:
            candidates.append(float(total_wallet_balance))
    except Exception:
        pass

    candidates = [x for x in candidates if x > 0]
    if not candidates:
        raise InvalidOrderPlan("could not fetch real futures account equity")
    return min(candidates)

def _get_min_notional_for_symbol(self, symbol, cfg):
    market = None
    try:
        market = self.exchange.market(symbol)
    except Exception:
        pass

    if market:
        limits = market.get("limits", {}) or {}
        cost = limits.get("cost", {}) or {}
        min_cost = cost.get("min")
        if min_cost:
            return float(min_cost)
    return float(cfg.get("min_notional_usdt", 5.0) or 5.0)


def _get_min_amount_for_symbol(self, symbol):
    try:
        market = self.exchange.market(symbol)
    except Exception:
        market = None
    if not isinstance(market, dict):
        return 0.0
    try:
        min_amount = float(((market.get("limits") or {}).get("amount") or {}).get("min") or 0.0)
    except (TypeError, ValueError):
        min_amount = 0.0
    return max(0.0, min_amount)


def _get_amount_step_for_symbol(self, symbol):
    try:
        market = self.exchange.market(symbol)
    except Exception:
        market = None
    if not isinstance(market, dict):
        return 0.0
    info = market.get("info", {}) if isinstance(market.get("info"), dict) else {}
    filters = info.get("filters", []) if isinstance(info.get("filters"), list) else []
    for item in filters:
        if not isinstance(item, dict):
            continue
        for key in ("stepSize", "qtyStep", "minQty"):
            try:
                value = float(item.get(key) or 0.0)
                if value > 0:
                    return value
            except (TypeError, ValueError):
                continue
    precision = (market.get("precision") or {}).get("amount") if isinstance(market.get("precision"), dict) else None
    try:
        if isinstance(precision, int):
            return 10 ** (-precision)
        value = float(precision)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    return self._get_min_amount_for_symbol(symbol)


def _tp_ladder_rr_for_price(side, entry_price, sl_distance, tp_price):
    try:
        entry = float(entry_price)
        distance = float(sl_distance)
        price = float(tp_price)
    except (TypeError, ValueError):
        return 0.0
    if distance <= 0:
        return 0.0
    if str(side).lower() == "long":
        return (price - entry) / distance
    return (entry - price) / distance


def _build_tp_ladder_orders(
    self,
    symbol,
    side,
    entry_price,
    sl_price,
    actual_qty,
    cfg=None,
):
    cfg = cfg if isinstance(cfg, dict) else {}
    side = str(side or "").lower()
    entry = _safe_float_or_none(entry_price)
    sl = _safe_float_or_none(sl_price)
    qty = _safe_float_or_none(actual_qty)
    if side not in {"long", "short"}:
        return {"ok": False, "mode": "invalid", "reason": "invalid_side", "tp_targets": []}
    if entry is None or entry <= 0:
        return {"ok": False, "mode": "invalid", "reason": "missing_entry_ref", "tp_targets": []}
    if sl is None or sl <= 0:
        return {"ok": False, "mode": "invalid", "reason": "missing_sl_price", "tp_targets": []}
    if qty is None or qty <= 0:
        return {"ok": False, "mode": "invalid", "reason": "invalid_actual_qty", "tp_targets": []}

    if side == "long":
        sl_distance = entry - sl
    else:
        sl_distance = sl - entry
    if sl_distance <= 0:
        return {
            "ok": False,
            "mode": "invalid",
            "reason": "invalid_sl_distance",
            "entry_ref": entry,
            "sl_price": sl,
            "tp_targets": [],
        }

    tp1_rr = max(0.1, float(cfg.get("live_tp1_rr", cfg.get("partial_take_profit_r_multiple", 1.0)) or 1.0))
    tp2_rr = max(
        float(cfg.get("live_min_tp2_rr", 2.0) or 2.0),
        float(cfg.get("live_tp2_rr", cfg.get("second_take_profit_r_multiple", 2.4)) or 2.4),
    )
    min_tp2_rr = max(0.1, float(cfg.get("live_min_tp2_rr", 2.0) or 2.0))
    tp1_pct = min(1.0, max(0.0, float(cfg.get("live_tp1_pct", 0.25) or 0.25)))
    tp2_pct = min(1.0, max(0.0, float(cfg.get("live_tp2_pct", 0.75) or 0.75)))
    if tp1_pct <= 0 or tp2_pct <= 0 or tp1_pct + tp2_pct <= 0:
        tp1_pct, tp2_pct = 0.25, 0.75
    total_pct = tp1_pct + tp2_pct
    tp1_pct /= total_pct
    tp2_pct /= total_pct
    policy = str(cfg.get("tp_split_failure_policy", "single_tp2_with_warning") or "single_tp2_with_warning").lower()
    if policy not in {"single_tp2_with_warning", "block_entry", "try_adaptive_split"}:
        policy = "single_tp2_with_warning"

    def _price_for_rr(rr):
        raw = entry + sl_distance * rr if side == "long" else entry - sl_distance * rr
        rounded = float(self.safe_price(symbol, raw))
        actual_rr = _tp_ladder_rr_for_price(side, entry, sl_distance, rounded)
        if actual_rr + 1e-9 < min_tp2_rr and rr >= min_tp2_rr:
            raw = entry + sl_distance * (min_tp2_rr * 1.001) if side == "long" else entry - sl_distance * (min_tp2_rr * 1.001)
            rounded = float(self.safe_price(symbol, raw))
            actual_rr = _tp_ladder_rr_for_price(side, entry, sl_distance, rounded)
        return rounded, actual_rr

    tp1_price, tp1_actual_rr = _price_for_rr(tp1_rr)
    tp2_price, tp2_actual_rr = _price_for_rr(tp2_rr)
    if tp2_actual_rr + 1e-9 < min_tp2_rr:
        return {
            "ok": False,
            "mode": "invalid",
            "reason": "tp2_rr_below_min_after_rounding",
            "entry_ref": entry,
            "sl_price": sl,
            "sl_distance": sl_distance,
            "tp2_price": tp2_price,
            "tp2_rr": tp2_actual_rr,
            "min_tp2_rr": min_tp2_rr,
            "tp_targets": [],
        }
    if side == "long" and not (tp1_price > entry and tp2_price > tp1_price):
        return {"ok": False, "mode": "invalid", "reason": "invalid_long_tp_ordering", "tp_targets": []}
    if side == "short" and not (tp1_price < entry and tp2_price < tp1_price):
        return {"ok": False, "mode": "invalid", "reason": "invalid_short_tp_ordering", "tp_targets": []}

    min_amount = self._get_min_amount_for_symbol(symbol)
    step_size = self._get_amount_step_for_symbol(symbol)

    def _split_targets(first_pct, second_pct, mode):
        residual = _calculate_residual_tp_quantities(
            symbol,
            qty,
            [qty * first_pct, qty * second_pct],
            self.safe_amount,
        )
        tp1_qty = float(residual[0]) if len(residual) > 0 else 0.0
        tp2_qty = float(residual[1]) if len(residual) > 1 else 0.0
        min_ok = (
            (min_amount <= 0 or tp1_qty + 1e-12 >= min_amount)
            and (min_amount <= 0 or tp2_qty + 1e-12 >= min_amount)
        )
        if tp1_qty <= 0 or tp2_qty <= 0 or not min_ok or tp1_qty + tp2_qty > qty * 1.000001:
            return None
        return {
            "ok": True,
            "mode": mode,
            "tp_orders": [
                {"name": "TP1", "price": tp1_price, "qty": tp1_qty, "rr": tp1_actual_rr, "pct": first_pct},
                {"name": "TP2", "price": tp2_price, "qty": tp2_qty, "rr": tp2_actual_rr, "pct": second_pct},
            ],
            "tp_targets": [
                {"label": "TP1", "kind": "tp1", "distance": abs(tp1_price - entry), "qty_ratio": first_pct, "target_r": tp1_rr, "effective_r": tp1_actual_rr},
                {"label": "TP2", "kind": "tp2", "distance": abs(tp2_price - entry), "qty_ratio": second_pct, "target_r": tp2_rr, "effective_r": tp2_actual_rr},
            ],
            "warnings": [],
        }

    split = _split_targets(tp1_pct, tp2_pct, "split_tp1_tp2")
    if split is None and policy == "try_adaptive_split":
        split = _split_targets(0.5, 0.5, "adaptive_split_50_50")
    if split is not None:
        split.update({
            "entry_ref": entry,
            "sl_price": sl,
            "sl_distance": sl_distance,
            "actual_qty": qty,
            "step_size": step_size,
            "min_amount": min_amount,
            "policy": policy,
        })
        return split

    required_base = min_amount or step_size or 0.0
    min_leg_pct = max(1e-9, min(tp1_pct, tp2_pct))
    required_min_qty_for_split = required_base / min_leg_pct if required_base > 0 else None
    reason = "qty_too_small_for_tp1_tp2_split"
    base = {
        "ok": False,
        "mode": "split_not_possible",
        "reason": reason,
        "entry_ref": entry,
        "sl_price": sl,
        "sl_distance": sl_distance,
        "actual_qty": qty,
        "step_size": step_size,
        "min_amount": min_amount,
        "required_min_qty_for_split": required_min_qty_for_split,
        "policy": policy,
        "tp_targets": [],
        "warnings": [reason],
    }
    if policy == "block_entry":
        return base

    full_qty = float(self.safe_amount(symbol, qty))
    if full_qty <= 0 or (min_amount > 0 and full_qty + 1e-12 < min_amount):
        return dict(base, reason="actual_qty_below_exchange_min_amount")
    return dict(
        base,
        ok=True,
        mode="single_tp2_with_warning",
        tp_orders=[
            {"name": "TP2", "price": tp2_price, "qty": full_qty, "rr": tp2_actual_rr, "pct": 1.0},
        ],
        tp_targets=[
            {"label": "TP2", "kind": "tp2", "distance": abs(tp2_price - entry), "qty_ratio": 1.0, "target_r": tp2_rr, "effective_r": tp2_actual_rr},
        ],
    )


def _format_tp_ladder_lines(self, ladder):
    ladder = ladder if isinstance(ladder, dict) else {}
    lines = ["TP Ladder:"]
    mode = ladder.get("mode") or "unknown"
    lines.append(f"mode={mode}")
    if ladder.get("reason"):
        lines.append(f"reason={ladder.get('reason')}")
    if ladder.get("entry_ref") is not None:
        lines.append(f"entry_ref={self._fmt_signal_trade_value(ladder.get('entry_ref'))}")
    if ladder.get("sl_price") is not None:
        lines.append(f"SL={self._fmt_signal_trade_value(ladder.get('sl_price'))}")
    if ladder.get("sl_distance") is not None:
        lines.append(f"SL_distance={self._fmt_signal_trade_value(ladder.get('sl_distance'))}")
    orders = ladder.get("tp_orders") if isinstance(ladder.get("tp_orders"), list) else []
    if not orders and mode == "split_not_possible":
        lines.append("TP1=not_created")
        lines.append("TP2=not_created")
    else:
        labels = {str(order.get("name") or "").upper() for order in orders if isinstance(order, dict)}
        if "TP1" not in labels and mode == "single_tp2_with_warning":
            lines.append("TP1=not_created")
        for order in orders:
            if not isinstance(order, dict):
                continue
            lines.append(
                f"{order.get('name') or 'TP'}={self._fmt_signal_trade_value(order.get('price'))} / "
                f"RR={float(order.get('rr') or 0.0):.2f} / "
                f"qty={self._fmt_signal_trade_value(order.get('qty'))}"
            )
    if mode == "single_tp2_with_warning":
        lines.append("exit=full_at_TP2")
    return lines


def _collapse_tp_targets_for_exchange(self, symbol, total_qty, targets):
    normalized_targets = [dict(target) for target in (targets or []) if isinstance(target, dict)]
    if not normalized_targets:
        return normalized_targets, None
    min_amount = self._get_min_amount_for_symbol(symbol)
    executable_total = _safe_float_value(self.safe_amount(symbol, total_qty), 0.0)
    if min_amount <= 0 or executable_total + 1e-12 < min_amount:
        return normalized_targets, None

    undersized = [
        target
        for target in normalized_targets
        if 0 < _safe_float_value(target.get("qty"), 0.0) + 1e-12 < min_amount
    ]
    if not undersized:
        return normalized_targets, None

    exit_target = max(
        normalized_targets,
        key=lambda target: (
            _safe_float_value(target.get("effective_r"), 0.0),
            _safe_float_value(target.get("target_r"), 0.0),
            _safe_float_value(target.get("distance"), 0.0),
            int(target.get("tp_index") or 1),
        ),
    )
    collapsed = dict(exit_target)
    collapsed_label = str(
        collapsed.get("tp_label")
        or collapsed.get("label")
        or collapsed.get("tp_name")
        or "TP2"
    )
    collapsed.update({
        "qty": executable_total,
        "raw_qty": executable_total,
        "qty_ratio": 1.0,
        "pct": 100.0,
        "collapsed_min_amount": True,
        "tp_split_failure_policy": "single_tp2_with_warning",
        "tp_ladder_mode": "single_tp2_with_warning",
        "tp_ladder_reason": "qty_too_small_for_tp1_tp2_split",
        "collapsed_from": [
            str(target.get("tp_label") or target.get("label") or target.get("tp_name") or "TP")
            for target in normalized_targets
        ],
    })
    return [collapsed], {
        "min_amount": min_amount,
        "total_qty": executable_total,
        "collapsed_label": collapsed_label,
        "mode": "single_tp2_with_warning",
        "reason": "qty_too_small_for_tp1_tp2_split",
    }


def _collapse_state_tp_plan_for_exchange(self, symbol, pos, state):
    if not isinstance(state, dict) or not pos or bool(state.get("tp1_filled", False)):
        return None
    current_qty = abs(float(self._position_signed_contracts(pos) or pos.get("contracts", 0) or 0))
    initial_qty = _safe_float_value(state.get("initial_qty"), current_qty)
    if current_qty <= 0 or (initial_qty > 0 and current_qty < initial_qty * 0.98):
        return None
    planned = self._planned_tp_orders_from_state(symbol, state)
    collapsed, details = self._collapse_tp_targets_for_exchange(symbol, current_qty, planned)
    if not details:
        return None
    state["planned_tp_orders"] = [dict(item) for item in collapsed]
    state["tp_orders"] = [dict(item) for item in collapsed]
    state["expected_tp_count"] = 1
    state["tp_min_amount_fallback"] = dict(details)
    state["tp2_disabled_min_amount"] = True
    state["remaining_ratio"] = 0.0
    self._set_utbreakout_trailing_state(symbol, state)
    logger.warning(
        "[Protection] collapsed TP state for %s to one executable order: qty=%.12f min_amount=%.12f",
        symbol,
        float(details["total_qty"]),
        float(details["min_amount"]),
    )
    return details


def _normalize_live_order_plan_for_exchange(self, plan, cfg):
    plan.qty = float(self.safe_amount(plan.symbol, plan.qty))
    plan.initial_sl_price = float(self.safe_price(plan.symbol, plan.initial_sl_price))

    normalized_tps = []
    raw_tp_quantities = [float(getattr(tp, "qty", 0.0) or 0.0) for tp in (plan.tp_orders or [])]
    residual_quantities = _calculate_residual_tp_quantities(
        plan.symbol,
        plan.qty,
        raw_tp_quantities,
        self.safe_amount,
    )
    for index, tp in enumerate(plan.tp_orders or [], 1):
        tp.qty = float(residual_quantities[index - 1]) if index <= len(residual_quantities) else 0.0
        tp.price = float(self.safe_price(plan.symbol, tp.price))
        if getattr(tp, "tp_index", None) is None:
            tp.tp_index = index
        if not getattr(tp, "tp_label", None):
            tp.tp_label = _normalize_tp_plan_label(getattr(tp, "tp_name", ""), f"TP{index}")
        if tp.qty > 0:
            normalized_tps.append(tp)

    min_amount = self._get_min_amount_for_symbol(plan.symbol)
    undersized_tps = [
        tp for tp in normalized_tps
        if min_amount > 0 and 0 < float(tp.qty) + 1e-12 < min_amount
    ]
    if undersized_tps and float(plan.qty) + 1e-12 >= min_amount:
        first_tp = normalized_tps[0]
        first_tp.qty = float(plan.qty)
        first_tp.pct = 100.0
        normalized_tps = [first_tp]
        logger.warning(
            "[LiveOrderPlan] collapsed TP ladder for %s: qty=%.12f min_amount=%.12f",
            plan.symbol,
            float(plan.qty),
            float(min_amount),
        )

    plan.tp_orders = normalized_tps
    if plan.tp_orders:
        logger.info(
            "[LiveOrderPlan] normalized TP ladder for %s: entry_qty=%.12f %s",
            plan.symbol,
            float(plan.qty),
            [
                {
                    "label": tp.tp_label or tp.tp_name,
                    "price": float(tp.price),
                    "qty": float(tp.qty),
                }
                for tp in plan.tp_orders
            ],
        )
    return plan

def _validate_live_order_plan_for_exchange(self, plan, cfg):
    validate_live_order_plan(plan, cfg)
    min_notional = self._get_min_notional_for_symbol(plan.symbol, cfg)
    validate_min_notional_against_live_cap(min_notional, cfg)
    entry_ref = plan.entry_price or cfg.get("last_price") or 0.0
    if not entry_ref:
        raise InvalidOrderPlan("missing entry reference price for minNotional check")

    if float(plan.qty) * float(entry_ref) < min_notional:
        raise InvalidOrderPlan("order notional below minNotional")
    validate_live_real_order_caps_after_rounding(plan, cfg)
    return True

__all__ = (
    '_fetch_futures_account_equity',
    '_get_min_notional_for_symbol',
    '_get_min_amount_for_symbol',
    '_get_amount_step_for_symbol',
    '_tp_ladder_rr_for_price',
    '_build_tp_ladder_orders',
    '_format_tp_ladder_lines',
    '_collapse_tp_targets_for_exchange',
    '_collapse_state_tp_plan_for_exchange',
    '_normalize_live_order_plan_for_exchange',
    '_validate_live_order_plan_for_exchange',
)
