//! Position management, exits, booking, reconciliation and ownership recovery.

use serde_json::{json, Map, Value};

use crate::core::options_strategy as st;
use crate::exchange::binance_options::{OptionsApi, OptionsApiError, OrderRef};
use crate::strategy::options_config::py_truthy;
use crate::strategy::options_service::{
    adaptive_trailing_exit, client_order_id, format_price, log_line, order_average_price,
    order_executed_quantity, order_is_terminal, position_quantity, profit_reference_price,
    py_first, py_rows, py_str, py_str_or_empty, realized_pnl, OptionsService, BOT_CLIENT_PREFIX,
};

impl<C: OptionsApi> OptionsService<C> {
    // === Public dispatcher used by options_service.rs ===
    pub(crate) async fn manage_active_position_inner(
        &self,
        force_exit: bool,
        // The adaptive layer fetches its own data (Python signature has none).
        _prefetched: Option<Value>,
    ) -> Value {
        if force_exit {
            return self.manage_base(true, None).await;
        }
        let position = self.st_get("active_position");
        if !py_truthy(&position) {
            return self.manage_base(false, None).await;
        }
        let symbol = py_str_or_empty(position.get("symbol"));
        if symbol.is_empty() {
            return self.manage_base(false, None).await;
        }
        // adaptive manage flow
        let exchange_positions = match self.client().positions(Some(&symbol)).await {
            Ok(v) => py_rows(&v),
            Err(e) => {
                return self.record_reason(
                    &format!("옵션 포지션 관리 API 오류: {}", e),
                    true,
                    None,
                );
            }
        };
        let exchange_qty = Self::find_exchange_qty(&exchange_positions, &symbol);
        let tracked_qty = st::pyf(position.get("quantity"), 0.0).max(0.0);
        if exchange_qty <= 1e-12 || exchange_qty + 1e-12 < tracked_qty {
            let prefetched = json!({"exchange_positions": exchange_positions});
            return self.manage_base(false, Some(prefetched)).await;
        }
        let mark_res = self.client().mark_price(Some(&symbol)).await;
        let depth_res = self.client().depth(&symbol, 20).await;
        let mark_payload = match mark_res {
            Ok(v) => v,
            Err(e) => {
                return self.record_reason(
                    &format!("옵션 포지션 관리 API 오류: {}", e),
                    true,
                    None,
                );
            }
        };
        let depth = match depth_res {
            Ok(v) => v,
            Err(e) => {
                return self.record_reason(
                    &format!("옵션 포지션 관리 API 오류: {}", e),
                    true,
                    None,
                );
            }
        };
        let mark = py_first(&mark_payload);
        let mark_price = st::pyf(
            mark.get("markPrice"),
            st::pyf(position.get("entry_price"), 0.0),
        );
        let bids = depth.get("bids").cloned().unwrap_or(json!([]));
        let bid = first_bid(&bids, mark_price);
        let entry_price = st::pyf(position.get("entry_price"), 0.0).max(1e-12);
        let pnl_pct = mark_price / entry_price - 1.0;
        let cfg = self.config();
        let profit_price = profit_reference_price(&cfg, mark_price, &bids);
        let profit_pct = profit_price / entry_price - 1.0;
        let now_ms = self.now_ms();
        let expiry_ms = st::pyf(position.get("expiry_date_ms"), 0.0) as i64;
        let expiry_hours = if expiry_ms != 0 {
            (expiry_ms - now_ms) as f64 / 3_600_000.0
        } else {
            9999.0
        };
        let peak = st::pyf(position.get("peak_mark"), entry_price).max(profit_price);
        if adaptive_trailing_exit(entry_price, peak, profit_price) {
            let managed_qty = exchange_qty.min(tracked_qty);
            if managed_qty > 1e-12 && bid > 0.0 {
                return self
                    .exit_position(&position, managed_qty, bid, "OPTION_ADAPTIVE_CONVEX_TRAIL")
                    .await;
            }
        }
        let stop_loss = st::pyf(cfg.get("stop_loss_pct"), 0.45);
        let take_profit = st::pyf(cfg.get("take_profit_pct"), 0.80);
        let trail_activation = st::pyf(cfg.get("trail_activation_pct"), 0.35);
        let trail_drawdown = st::pyf(cfg.get("trail_drawdown_pct"), 0.25);
        let max_hold_hours = st::pyf(cfg.get("max_hold_hours"), 72.0);
        let expiry_exit_hours = st::pyf(cfg.get("expiry_exit_hours"), 8.0);
        let entry_time_ms = st::pyf(position.get("entry_time_ms"), now_ms as f64) as i64;
        let legacy_triggered = pnl_pct <= -stop_loss
            || profit_pct >= take_profit
            || (peak >= entry_price * (1.0 + trail_activation)
                && profit_price <= peak * (1.0 - trail_drawdown))
            || (now_ms - entry_time_ms) as f64 >= max_hold_hours * 3_600_000.0
            || expiry_hours <= expiry_exit_hours;
        if legacy_triggered {
            let prefetched = json!({
                "exchange_positions": exchange_positions,
                "mark_payload": mark_payload,
                "depth": depth,
            });
            return self.manage_base(false, Some(prefetched)).await;
        }
        self.refresh_exit_signal(&position, &cfg, now_ms).await;
        let position = match self.st_get("active_position") {
            v if py_truthy(&v) => v,
            _ => position,
        };
        let side = py_str_or_empty(position.get("side")).to_uppercase();
        let underlying_score = st::pyf(position.get("exit_underlying_score"), 0.0);
        let aligned_strength = if side == "CALL" {
            underlying_score
        } else {
            -underlying_score
        };
        let signal_threshold = st::pyf(cfg.get("min_abs_signal"), 0.46);
        let opposed = aligned_strength <= -(signal_threshold * 0.70);
        let weak = aligned_strength < signal_threshold * 0.50;
        let current_iv = st::pyf(mark.get("markIV"), 0.0);
        let entry_iv = st::pyf(position.get("entry_mark_iv"), 0.0);
        let delta = st::pyf(mark.get("delta"), 0.0).abs();
        let theta = st::pyf(mark.get("theta"), 0.0);
        let theta_burden = if theta != 0.0 {
            theta.abs() / mark_price.max(1e-9)
        } else {
            0.0
        };
        let dte_days = expiry_hours / 24.0;
        let elapsed_hours =
            (now_ms as f64 - st::pyf(position.get("entry_time_ms"), now_ms as f64)) / 3_600_000.0;
        {
            let mut updated = position.clone();
            if let Value::Object(map) = &mut updated {
                map.insert("last_mark_iv".to_string(), json!(current_iv));
                map.insert("last_delta".to_string(), json!(delta));
                map.insert("last_theta".to_string(), json!(theta));
                map.insert("last_theta_burden".to_string(), json!(theta_burden));
                map.insert("last_dte_days".to_string(), json!(dte_days));
            }
            self.st_set("active_position", updated);
            let _ = self.save_state();
        }
        let iv_collapse = st::pyf(cfg.get("iv_collapse_exit_pct"), 0.30);
        let near_expiry_days = st::pyf(cfg.get("near_expiry_risk_days"), 2.0);
        let theta_burden_exit = st::pyf(cfg.get("theta_burden_exit_pct"), 0.12);
        let delta_collapse = st::pyf(cfg.get("delta_collapse_exit"), 0.15);
        let adaptive_time_stop = st::pyf(cfg.get("adaptive_time_stop_hours"), 36.0);
        let reason = if opposed && dte_days <= 7.0 {
            Some("OPTION_UNDERLYING_TREND_BREAK")
        } else if entry_iv > 0.0
            && current_iv > 0.0
            && current_iv <= entry_iv * (1.0 - iv_collapse)
            && weak
        {
            Some("OPTION_IV_COLLAPSE_WITH_WEAK_TREND")
        } else if dte_days <= near_expiry_days
            && (theta_burden >= theta_burden_exit || delta < delta_collapse || weak)
        {
            Some("OPTION_NEAR_EXPIRY_DECAY_RISK")
        } else if elapsed_hours >= adaptive_time_stop && pnl_pct <= 0.10 && weak {
            Some("OPTION_ADAPTIVE_TIME_STOP")
        } else {
            None
        };
        if let Some(r) = reason {
            let managed_qty = exchange_qty.min(tracked_qty);
            if managed_qty > 1e-12 && bid > 0.0 {
                return self.exit_position(&position, managed_qty, bid, r).await;
            }
        }
        let prefetched = json!({
            "exchange_positions": exchange_positions,
            "mark_payload": mark_payload,
            "depth": depth,
        });
        self.manage_base(false, Some(prefetched)).await
    }

    pub(crate) fn find_exchange_qty(positions: &[Value], symbol: &str) -> f64 {
        for row in positions {
            if py_str_or_empty(row.get("symbol")) == symbol {
                return position_quantity(row).max(0.0);
            }
        }
        0.0
    }

    pub(crate) async fn refresh_exit_signal(&self, position: &Value, cfg: &Value, now_ms: i64) {
        let refresh_ms = (st::pyf(cfg.get("exit_signal_refresh_seconds"), 900.0) as i64) * 1000;
        let last_ms = st::pyf(position.get("last_exit_signal_refresh_ms"), 0.0) as i64;
        if now_ms - last_ms < refresh_ms {
            return;
        }
        let underlying = py_str_or_empty(position.get("underlying"));
        if underlying.is_empty() {
            return;
        }
        match self.underlying_signal(&underlying, cfg).await {
            Ok(signal) => {
                let mut updated = position.clone();
                if let Value::Object(map) = &mut updated {
                    map.insert("last_exit_signal_refresh_ms".to_string(), json!(now_ms));
                    map.insert(
                        "exit_underlying_score".to_string(),
                        json!(st::pyf(signal.get("score"), 0.0)),
                    );
                    map.insert(
                        "exit_underlying_direction".to_string(),
                        signal.get("direction").cloned().unwrap_or(Value::Null),
                    );
                    map.insert(
                        "exit_underlying_accepted".to_string(),
                        json!(py_truthy(signal.get("accepted").unwrap_or(&Value::Null))),
                    );
                    map.insert(
                        "exit_underlying_strategy".to_string(),
                        signal.get("strategy").cloned().unwrap_or(Value::Null),
                    );
                }
                self.st_set("active_position", updated);
                let _ = self.save_state();
            }
            Err(e) => {
                log_line(&format!(
                    "Adaptive option exit signal refresh failed for {}: {}",
                    underlying, e
                ));
            }
        }
    }

    pub(crate) async fn resolve_pending_exit(&self, position: &Value) -> Option<Value> {
        let pending = self.st_get("pending_exit");
        if !py_truthy(&pending) {
            return None;
        }
        let symbol = py_str_or_empty(pending.get("symbol"));
        let client_id = py_str_or_empty(pending.get("client_order_id"));
        if symbol.is_empty() || !client_id.starts_with(BOT_CLIENT_PREFIX) {
            return Some(self.record_reason("OPTIONS_PENDING_EXIT_INVALID", true, None));
        }
        let order_res = self
            .client()
            .query_order(&symbol, &OrderRef::Client(client_id))
            .await;
        let order = match order_res {
            Ok(v) => v,
            Err(e) => {
                return Some(self.record_reason(
                    &format!("옵션 미확정 청산 주문 확인 실패: {}", e),
                    true,
                    None,
                ));
            }
        };
        let status = py_str_or_empty(order.get("status")).to_uppercase();
        let filled = order_executed_quantity(&order);
        if filled > 0.0 {
            let limit_price = st::pyf(pending.get("limit_price"), 0.0);
            let reason_raw = pending.get("reason").cloned().unwrap_or(Value::Null);
            let reason = if py_truthy(&reason_raw) {
                py_str_or_empty(Some(&reason_raw))
            } else {
                "OPTION_RECOVERED_EXIT".to_string()
            };
            return Some(
                self.book_exit_order(position, &order, limit_price, &reason)
                    .await,
            );
        }
        if matches!(
            status.as_str(),
            "CANCELED" | "REJECTED" | "EXPIRED" | "FILLED"
        ) {
            self.st_set("pending_exit", Value::Null);
            let _ = self.save_state();
            return None;
        }
        Some(json!({
            "action": "waiting",
            "reason": "옵션 청산 주문 상태 확인 중 — 중복 청산을 차단했습니다.",
        }))
    }

    /// Base manage (`_manage_active_position` with prefetched).
    pub(crate) async fn manage_base(&self, force_exit: bool, prefetched: Option<Value>) -> Value {
        let position = self.st_get("active_position");
        let symbol = py_str_or_empty(position.get("symbol"));
        if symbol.is_empty() {
            return self.record_reason("OPTIONS_ACTIVE_POSITION_INVALID", true, None);
        }
        if let Some(r) = self.resolve_pending_exit(&position).await {
            return r;
        }
        let prefetched = prefetched.unwrap_or(json!({}));
        // exchange positions
        let exchange_positions: Vec<Value> = match prefetched.get("exchange_positions") {
            Some(v) if !v.is_null() => py_rows(v),
            _ => match self.client().positions(Some(&symbol)).await {
                Ok(v) => py_rows(&v),
                Err(e) => {
                    return self.record_reason(
                        &format!("옵션 포지션 관리 API 오류: {}", e),
                        true,
                        None,
                    );
                }
            },
        };
        let exchange_qty = Self::find_exchange_qty(&exchange_positions, &symbol);
        let tracked_qty = st::pyf(position.get("quantity"), 0.0).max(0.0);
        let mut position = position;
        if exchange_qty + 1e-12 < tracked_qty {
            position = self
                .reconcile_external_reduction(&position, exchange_qty)
                .await;
            if py_truthy(&self.st_get("active_position")) {
                let updated = self.st_get("active_position");
                if py_truthy(&updated) {
                    position = updated;
                }
            }
        }
        if exchange_qty <= 1e-12 {
            let exit_price = st::pyf(position.get("last_external_exit_price"), 0.0);
            return self
                .finalize_trade(
                    &position,
                    exit_price,
                    tracked_qty,
                    0.0,
                    0.0,
                    "EXTERNAL_CLOSE_OR_EXPIRY",
                )
                .await;
        }
        let mark_payload_opt = prefetched.get("mark_payload").cloned();
        let depth_opt = prefetched.get("depth").cloned();
        let (mark_payload, depth) = match (
            mark_payload_opt.filter(|v| !v.is_null()),
            depth_opt.filter(|v| !v.is_null()),
        ) {
            (Some(m), Some(d)) => (m, d),
            _ => {
                let m = self.client().mark_price(Some(&symbol)).await;
                let d = self.client().depth(&symbol, 20).await;
                match (m, d) {
                    (Ok(m), Ok(d)) => (m, d),
                    (Err(e), _) => {
                        return self.record_reason(
                            &format!("옵션 포지션 관리 API 오류: {}", e),
                            true,
                            None,
                        );
                    }
                    (_, Err(e)) => {
                        return self.record_reason(
                            &format!("옵션 포지션 관리 API 오류: {}", e),
                            true,
                            None,
                        );
                    }
                }
            }
        };
        let mark = py_first(&mark_payload);
        let mark_price = st::pyf(
            mark.get("markPrice"),
            st::pyf(position.get("entry_price"), 0.0),
        );
        let bids = depth.get("bids").cloned().unwrap_or(json!([]));
        let bid = first_bid(&bids, mark_price);
        let entry_price = st::pyf(position.get("entry_price"), 0.0).max(1e-12);
        let cfg = self.config();
        let pnl_pct = mark_price / entry_price - 1.0;
        let profit_price = profit_reference_price(&cfg, mark_price, &bids);
        let profit_pct = profit_price / entry_price - 1.0;
        let peak = st::pyf(position.get("peak_mark"), entry_price).max(profit_price);
        {
            let mut updated = position.clone();
            if let Value::Object(map) = &mut updated {
                map.insert("peak_mark".to_string(), json!(peak));
                map.insert("last_mark".to_string(), json!(mark_price));
                map.insert("last_bid".to_string(), json!(bid));
                map.insert("last_pnl_pct".to_string(), json!(pnl_pct));
                map.insert("last_bid_pnl_pct".to_string(), json!(profit_pct));
            }
            self.st_set("active_position", updated);
            let _ = self.save_state();
        }
        let now_ms = self.now_ms();
        let stop_loss = st::pyf(cfg.get("stop_loss_pct"), 0.45);
        let take_profit = st::pyf(cfg.get("take_profit_pct"), 0.80);
        let trail_activation = st::pyf(cfg.get("trail_activation_pct"), 0.35);
        let trail_drawdown = st::pyf(cfg.get("trail_drawdown_pct"), 0.25);
        let max_hold_hours = st::pyf(cfg.get("max_hold_hours"), 72.0);
        let expiry_exit_hours = st::pyf(cfg.get("expiry_exit_hours"), 8.0);
        let entry_time_ms = st::pyf(position.get("entry_time_ms"), now_ms as f64) as i64;
        let expiry_date_ms = st::pyf(position.get("expiry_date_ms"), 0.0) as i64;
        let mut reason = String::new();
        if force_exit {
            reason = "TELEGRAM_FORCE_CLOSE".to_string();
        } else if pnl_pct <= -stop_loss {
            reason = "OPTION_PREMIUM_STOP".to_string();
        } else if profit_pct >= take_profit {
            reason = "OPTION_PREMIUM_TARGET".to_string();
        } else if peak >= entry_price * (1.0 + trail_activation)
            && profit_price <= peak * (1.0 - trail_drawdown)
        {
            reason = "OPTION_PREMIUM_TRAIL".to_string();
        } else if (now_ms - entry_time_ms) as f64 >= max_hold_hours * 3_600_000.0 {
            reason = "OPTION_TIME_STOP".to_string();
        } else if (expiry_date_ms - now_ms) as f64 <= expiry_exit_hours * 3_600_000.0 {
            reason = "OPTION_EXPIRY_GUARD".to_string();
        }
        if reason.is_empty() {
            let text = format!(
                "{} 보유 관리 중 — 프리미엄 {:+.1}%",
                symbol,
                pnl_pct * 100.0
            );
            self.st_set("last_reason", json!(text.clone()));
            let _ = self.save_state();
            return json!({"action": "managed", "reason": text});
        }
        let managed_qty = exchange_qty.min(st::pyf(position.get("quantity"), 0.0));
        if managed_qty <= 1e-12 {
            return self.record_reason(
                "거래소 수량과 봇 관리 수량이 일치하지 않아 청산을 차단했습니다.",
                true,
                None,
            );
        }
        self.exit_position(&position, managed_qty, bid, &reason)
            .await
    }

    pub(crate) async fn exit_position(
        &self,
        position: &Value,
        quantity: f64,
        best_bid: f64,
        reason: &str,
    ) -> Value {
        let symbol = py_str_or_empty(position.get("symbol"));
        let tick = st::pyf(position.get("tick_size"), 0.0);
        let limit_price = tick.max(best_bid - (if tick > 0.0 { tick } else { 0.0 }));
        let cid = client_order_id("s", self.now_ms());
        let price_text = format_price(limit_price, tick);
        let quantity_text = st::dec(quantity).normalize().to_string();
        let pending = json!({
            "symbol": symbol,
            "quantity": quantity,
            "limit_price": limit_price,
            "client_order_id": cid,
            "reason": reason,
            "created_at_ms": self.now_ms(),
        });
        self.st_set("pending_exit", pending);
        let _ = self.save_state();
        let order_req = crate::exchange::binance_options::OptionOrder {
            symbol: symbol.clone(),
            side: "SELL".to_string(),
            order_type: "LIMIT".to_string(),
            quantity: quantity_text,
            price: Some(price_text),
            time_in_force: Some("IOC".to_string()),
            post_only: false,
            reduce_only: true,
            client_order_id: Some(cid.clone()),
        };
        let response = match self.client().new_order(&order_req).await {
            Ok(v) => v,
            Err(e) => {
                if e.uncertain {
                    match self
                        .client()
                        .query_order(&symbol, &OrderRef::Client(cid.clone()))
                        .await
                    {
                        Ok(v) => v,
                        Err(_) => {
                            return self.record_reason(
                                "옵션 청산 주문 상태가 불확실합니다. 중복 청산을 막고 다음 주기에 재확인합니다.",
                                true,
                                None,
                            );
                        }
                    }
                } else {
                    self.st_set("pending_exit", Value::Null);
                    let _ = self.save_state();
                    return self.record_reason(&format!("옵션 청산 주문 거절: {}", e), true, None);
                }
            }
        };
        let order = match self
            .resolve_submitted_order(&symbol, &cid, &response, limit_price)
            .await
        {
            Ok(v) => v,
            Err(e) => {
                return self.record_reason(&format!("옵션 청산 오류: {}", e), true, None);
            }
        };
        self.book_exit_order(position, &order, limit_price, reason)
            .await
    }

    pub(crate) async fn book_exit_order(
        &self,
        position: &Value,
        order: &Value,
        limit_price: f64,
        reason: &str,
    ) -> Value {
        let filled = order_executed_quantity(order);
        if filled <= 0.0 {
            let status = py_str_or_empty(order.get("status")).to_uppercase();
            if matches!(
                status.as_str(),
                "CANCELED" | "REJECTED" | "EXPIRED" | "FILLED"
            ) {
                self.st_set("pending_exit", Value::Null);
                let _ = self.save_state();
            }
            return self.record_reason(
                "옵션 청산 IOC가 미체결되어 다음 관리 주기에 재시도합니다.",
                false,
                None,
            );
        }
        let exit_price = order_average_price(order, limit_price);
        let unit = st::pyf(position.get("unit"), 1.0);
        let proceeds = exit_price * filled * unit;
        let exit_fee = st::estimate_option_fee(exit_price, 0.0, filled, unit);
        let quantity = st::pyf(position.get("quantity"), 0.0);
        let remaining = (quantity - filled).max(0.0);
        let mut position = position.clone();
        if let Some(order_id) = order.get("orderId") {
            if !order_id.is_null() {
                let mut accounted: Vec<String> = match position.get("accounted_exit_order_ids") {
                    Some(Value::Array(items)) => items.iter().map(|v| py_str(Some(v))).collect(),
                    _ => Vec::new(),
                };
                let oid_text = py_str(Some(order_id));
                if !accounted.contains(&oid_text) {
                    accounted.push(oid_text);
                }
                let len = accounted.len();
                let start = len.saturating_sub(100);
                let trimmed: Vec<Value> = accounted[start..].iter().map(|s| json!(s)).collect();
                if let Value::Object(map) = &mut position {
                    map.insert("accounted_exit_order_ids".to_string(), json!(trimmed));
                }
            }
        }
        let new_bankroll =
            (st::pyf(Some(&self.st_get("cash_bankroll_usdt")), 0.0) + proceeds - exit_fee).max(0.0);
        self.st_set("cash_bankroll_usdt", json!(new_bankroll));
        self.st_set("pending_exit", Value::Null);
        if remaining > 1e-12 {
            let prev_partial = st::pyf(position.get("partial_exit_proceeds_usdt"), 0.0) + proceeds;
            let prev_fees = st::pyf(position.get("partial_exit_fees_usdt"), 0.0) + exit_fee;
            let reconcile = self.now_ms();
            if let Value::Object(map) = &mut position {
                map.insert("quantity".to_string(), json!(remaining));
                map.insert(
                    "partial_exit_proceeds_usdt".to_string(),
                    json!(prev_partial),
                );
                map.insert("partial_exit_fees_usdt".to_string(), json!(prev_fees));
                map.insert("reconcile_after_ms".to_string(), json!(reconcile));
            }
            self.st_set("active_position", position);
            self.st_set(
                "last_reason",
                json!(format!(
                    "옵션 부분 청산 {}, 잔여 {}",
                    crate::app::telegram_bot::format_g(filled),
                    crate::app::telegram_bot::format_g(remaining)
                )),
            );
            let _ = self.save_state();
            return json!({"action": "partial_exit", "remaining": remaining});
        }
        self.finalize_trade(&position, exit_price, filled, proceeds, exit_fee, reason)
            .await
    }

    pub(crate) async fn finalize_trade(
        &self,
        position: &Value,
        exit_price: f64,
        exit_quantity: f64,
        proceeds_in: f64,
        exit_fee_in: f64,
        reason: &str,
    ) -> Value {
        let partial_proceeds = st::pyf(position.get("partial_exit_proceeds_usdt"), 0.0);
        let partial_fees = st::pyf(position.get("partial_exit_fees_usdt"), 0.0);
        let proceeds = proceeds_in + partial_proceeds;
        let exit_fee = exit_fee_in + partial_fees;
        let entry_cost = st::pyf(position.get("entry_total_usdt"), 0.0);
        let pnl = proceeds - exit_fee - entry_cost;
        let symbol = position.get("symbol").cloned().unwrap_or(Value::Null);
        let quantity = st::pyf(position.get("original_quantity"), exit_quantity);
        let mut trade = Map::new();
        trade.insert("symbol".to_string(), symbol.clone());
        trade.insert(
            "side".to_string(),
            position.get("side").cloned().unwrap_or(Value::Null),
        );
        trade.insert("quantity".to_string(), json!(quantity));
        trade.insert(
            "entry_price".to_string(),
            json!(st::pyf(position.get("entry_price"), 0.0)),
        );
        trade.insert("exit_price".to_string(), json!(exit_price));
        trade.insert("entry_total_usdt".to_string(), json!(entry_cost));
        trade.insert("exit_proceeds_usdt".to_string(), json!(proceeds));
        trade.insert("exit_fee_usdt".to_string(), json!(exit_fee));
        trade.insert("pnl_usdt".to_string(), json!(pnl));
        trade.insert("reason".to_string(), json!(reason));
        trade.insert("closed_at".to_string(), json!(self.now_iso()));
        let trade_value = Value::Object(trade);
        let mut trades: Vec<Value> = match self.st_get("trades") {
            Value::Array(items) => items,
            _ => Vec::new(),
        };
        trades.push(trade_value.clone());
        let len = trades.len();
        let start = len.saturating_sub(200);
        let trimmed: Vec<Value> = trades[start..].to_vec();
        self.st_set("trades", json!(trimmed));
        self.st_set("active_position", Value::Null);
        self.st_set(
            "last_reason",
            json!(format!(
                "{} 청산 완료 ({})",
                py_str_or_empty(position.get("symbol")),
                reason
            )),
        );
        self.st_set("last_error", json!(""));
        let _ = self.save_state();
        let new_state = self.state();
        let realized = realized_pnl(&new_state);
        let text = [
            "🟣 옵션 자동매매 청산".to_string(),
            format!("종목: {}", py_str_or_empty(position.get("symbol"))),
            format!("사유: {}", reason),
            format!("실현손익(추정 수수료 포함): {:+.4} USDT", pnl),
            format!("봇 옵션 누적 손익: {:+.4} USDT", realized),
        ]
        .join("\n");
        self.notify_managed(&text).await;
        json!({"action": "exited", "trade": trade_value})
    }

    pub(crate) async fn reconcile_external_reduction(
        &self,
        position: &Value,
        exchange_quantity: f64,
    ) -> Value {
        let symbol = py_str_or_empty(position.get("symbol"));
        let tracked_quantity = st::pyf(position.get("quantity"), 0.0).max(0.0);
        let missing_quantity = (tracked_quantity - exchange_quantity.max(0.0)).max(0.0);
        if missing_quantity <= 1e-12 {
            return position.clone();
        }
        let start_time = st::pyf(position.get("entry_time_ms"), 0.0) as i64;
        let mut proceeds = 0.0f64;
        let mut exit_fee = 0.0f64;
        let mut sold_qty = 0.0f64;
        let mut weighted = 0.0f64;
        let mut processed: Vec<String> = match position.get("processed_exit_trade_ids") {
            Some(Value::Array(items)) => items.iter().map(|v| py_str(Some(v))).collect(),
            _ => Vec::new(),
        };
        let processed_set: std::collections::HashSet<String> = processed.iter().cloned().collect();
        let accounted_orders: std::collections::HashSet<String> =
            match position.get("accounted_exit_order_ids") {
                Some(Value::Array(items)) => items.iter().map(|v| py_str(Some(v))).collect(),
                _ => std::collections::HashSet::new(),
            };
        let mut newly_processed: Vec<String> = Vec::new();
        match self
            .client()
            .user_trades(&symbol, Some(start_time), 100)
            .await
        {
            Ok(trades_payload) => {
                for row in py_rows(&trades_payload) {
                    if py_str_or_empty(row.get("side")).to_uppercase() != "SELL" {
                        continue;
                    }
                    let trade_key = build_trade_key(&row);
                    if processed_set.contains(&trade_key) {
                        continue;
                    }
                    newly_processed.push(trade_key.clone());
                    if accounted_orders.contains(&py_str(row.get("orderId"))) {
                        continue;
                    }
                    let qty_source = row
                        .get("quantity")
                        .filter(|v| py_truthy(v))
                        .cloned()
                        .or_else(|| row.get("qty").cloned());
                    let qty = st::pyf(qty_source.as_ref(), 0.0).max(0.0);
                    let accepted_qty = qty.min((missing_quantity - sold_qty).max(0.0));
                    if accepted_qty <= 1e-12 {
                        continue;
                    }
                    let price = st::pyf(row.get("price"), 0.0);
                    let ratio = if qty > 0.0 { accepted_qty / qty } else { 0.0 };
                    sold_qty += accepted_qty;
                    weighted += accepted_qty * price;
                    let default_quote = qty * price;
                    let quote_value = st::pyf(row.get("quoteQty"), default_quote);
                    proceeds += quote_value * ratio * st::pyf(position.get("unit"), 1.0);
                    let fee_source = row
                        .get("fee")
                        .cloned()
                        .or_else(|| row.get("commission").cloned());
                    exit_fee += st::pyf(fee_source.as_ref(), 0.0).abs() * ratio;
                }
            }
            Err(e) => {
                log_line(&format!(
                    "Options external-close trade reconciliation failed: {}",
                    e
                ));
            }
        }
        if sold_qty <= 1e-12 && exchange_quantity <= 1e-12 {
            match self
                .client()
                .exercise_records(Some(&symbol), Some(start_time), 100)
                .await
            {
                Ok(records_payload) => {
                    for row in py_rows(&records_payload) {
                        if py_str_or_empty(row.get("symbol")) != symbol {
                            continue;
                        }
                        let record_key = build_exercise_key(&row);
                        if processed_set.contains(&record_key) {
                            continue;
                        }
                        newly_processed.push(record_key);
                        let amount_src = row
                            .get("amount")
                            .filter(|v| py_truthy(v))
                            .cloned()
                            .or_else(|| row.get("exerciseAmount").filter(|v| py_truthy(v)).cloned())
                            .or_else(|| row.get("settleAmount").filter(|v| py_truthy(v)).cloned())
                            .or_else(|| row.get("profit").filter(|v| py_truthy(v)).cloned());
                        proceeds += st::pyf(amount_src.as_ref(), 0.0).max(0.0);
                        let fee_src = row
                            .get("fee")
                            .filter(|v| py_truthy(v))
                            .cloned()
                            .or_else(|| row.get("exerciseFee").cloned());
                        exit_fee += st::pyf(fee_src.as_ref(), 0.0).abs();
                    }
                }
                Err(e) => {
                    log_line(&format!("Options exercise reconciliation failed: {}", e));
                }
            }
        }
        let booked_qty = if sold_qty > 1e-12 {
            sold_qty
        } else {
            missing_quantity
        };
        let exit_price = if sold_qty > 1e-12 {
            weighted / sold_qty
        } else if proceeds > 0.0 {
            proceeds / (missing_quantity * st::pyf(position.get("unit"), 1.0)).max(1e-12)
        } else {
            0.0
        };
        let new_bankroll =
            (st::pyf(Some(&self.st_get("cash_bankroll_usdt")), 0.0) + proceeds - exit_fee).max(0.0);
        self.st_set("cash_bankroll_usdt", json!(new_bankroll));
        let mut updated = position.clone();
        let prev_partial = st::pyf(position.get("partial_exit_proceeds_usdt"), 0.0) + proceeds;
        let prev_fees = st::pyf(position.get("partial_exit_fees_usdt"), 0.0) + exit_fee;
        let reconcile = self.now_ms();
        // Python `list(processed.union(newly_processed))[-200:]` in existing order then new.
        for np in &newly_processed {
            if !processed.contains(np) {
                processed.push(np.clone());
            }
        }
        let len = processed.len();
        let start = len.saturating_sub(200);
        let trimmed: Vec<Value> = processed[start..].iter().map(|s| json!(s)).collect();
        if let Value::Object(map) = &mut updated {
            map.insert("quantity".to_string(), json!(exchange_quantity.max(0.0)));
            map.insert(
                "partial_exit_proceeds_usdt".to_string(),
                json!(prev_partial),
            );
            map.insert("partial_exit_fees_usdt".to_string(), json!(prev_fees));
            map.insert("processed_exit_trade_ids".to_string(), json!(trimmed));
            map.insert("last_external_exit_price".to_string(), json!(exit_price));
            map.insert("last_external_exit_quantity".to_string(), json!(booked_qty));
            map.insert("reconcile_after_ms".to_string(), json!(reconcile));
        }
        self.st_set("active_position", updated.clone());
        let _ = self.save_state();
        updated
    }

    pub(crate) async fn resolve_pending_entry_impl(
        &self,
        pending: &Value,
        snapshot: &Value,
    ) -> Option<Value> {
        let symbol = py_str_or_empty(pending.get("symbol"));
        let client_id = py_str_or_empty(pending.get("client_order_id"));
        if symbol.is_empty() || !client_id.starts_with(BOT_CLIENT_PREFIX) {
            return Some(self.record_reason("OPTIONS_PENDING_ENTRY_INVALID", true, None));
        }
        let order_result = self
            .client()
            .query_order(&symbol, &OrderRef::Client(client_id.clone()))
            .await;
        let mut order = match order_result {
            Ok(v) => v,
            Err(e) => {
                return Some(self.record_reason(
                    &format!("옵션 재시작 주문 확인 실패: {}", e),
                    true,
                    None,
                ));
            }
        };
        if !order_is_terminal(&order) {
            if let Err(e) = self
                .client()
                .cancel_order(&symbol, &OrderRef::Client(client_id.clone()))
                .await
            {
                return Some(self.record_reason(
                    &format!("옵션 재시작 주문 확인 실패: {}", e),
                    true,
                    None,
                ));
            }
            match self
                .client()
                .query_order(&symbol, &OrderRef::Client(client_id.clone()))
                .await
            {
                Ok(v) => order = v,
                Err(e) => {
                    return Some(self.record_reason(
                        &format!("옵션 재시작 주문 확인 실패: {}", e),
                        true,
                        None,
                    ));
                }
            }
        }
        let mut exchange_quantity = 0.0f64;
        if let Some(Value::Array(items)) = snapshot.get("positions") {
            for row in items {
                if py_str_or_empty(row.get("symbol")) == symbol {
                    exchange_quantity = position_quantity(row).abs();
                    break;
                }
            }
        }
        if order_executed_quantity(&order) <= 1e-12 {
            if order_is_terminal(&order) {
                self.clear_pending_entry_managed(Some(&client_id));
                return None;
            }
            return Some(self.record_reason(
                "봇 옵션 진입 주문이 아직 미확정 상태라 신규 주문을 차단했습니다.",
                false,
                None,
            ));
        }
        Some(
            self.activate_recovered_entry(pending, &order, exchange_quantity)
                .await,
        )
    }

    pub(crate) async fn activate_recovered_entry(
        &self,
        pending: &Value,
        order: &Value,
        exchange_quantity: f64,
    ) -> Value {
        let filled_qty = order_executed_quantity(order);
        let managed_qty = exchange_quantity.max(0.0).min(filled_qty.max(0.0));
        if managed_qty <= 1e-12 {
            let cid = py_str_or_empty(pending.get("client_order_id"));
            self.clear_pending_entry_managed(if cid.is_empty() { None } else { Some(&cid) });
            return Value::Null;
        }
        let fill_price = order_average_price(order, st::pyf(pending.get("submitted_price"), 0.0));
        let unit = st::pyf(pending.get("unit"), 1.0).max(1e-12);
        let premium = fill_price * managed_qty * unit;
        let fee = st::estimate_option_fee(
            fill_price,
            st::pyf(pending.get("spot_price"), 0.0),
            managed_qty,
            unit,
        );
        let total_cost = premium + fee;
        let update_time = order
            .get("updateTime")
            .filter(|v| py_truthy(v))
            .cloned()
            .or_else(|| order.get("time").filter(|v| py_truthy(v)).cloned())
            .or_else(|| {
                pending
                    .get("created_at_ms")
                    .filter(|v| py_truthy(v))
                    .cloned()
            });
        let entry_time_ms = match update_time {
            Some(v) => st::pyf(Some(&v), 0.0) as i64,
            None => self.now_ms(),
        };
        let expiry_ms = st::pyf(pending.get("expiry_date_ms"), 0.0) as i64;
        let mut position = Map::new();
        position.insert(
            "symbol".to_string(),
            pending.get("symbol").cloned().unwrap_or(Value::Null),
        );
        position.insert(
            "underlying".to_string(),
            pending.get("underlying").cloned().unwrap_or(Value::Null),
        );
        position.insert(
            "side".to_string(),
            pending.get("side").cloned().unwrap_or(Value::Null),
        );
        position.insert("quantity".to_string(), json!(managed_qty));
        position.insert("original_quantity".to_string(), json!(managed_qty));
        position.insert("entry_price".to_string(), json!(fill_price));
        position.insert("entry_premium_usdt".to_string(), json!(premium));
        position.insert("entry_fee_usdt".to_string(), json!(fee));
        position.insert("entry_total_usdt".to_string(), json!(total_cost));
        position.insert("entry_time_ms".to_string(), json!(entry_time_ms));
        position.insert("expiry_date_ms".to_string(), json!(expiry_ms));
        position.insert("unit".to_string(), json!(unit));
        position.insert(
            "tick_size".to_string(),
            json!(st::pyf(pending.get("tick_size"), 0.0)),
        );
        position.insert("peak_mark".to_string(), json!(fill_price));
        position.insert(
            "client_order_id".to_string(),
            pending
                .get("client_order_id")
                .cloned()
                .unwrap_or(Value::Null),
        );
        position.insert(
            "order_id".to_string(),
            order.get("orderId").cloned().unwrap_or(Value::Null),
        );
        let exec_mode = pending
            .get("execution_mode")
            .filter(|v| py_truthy(v))
            .cloned()
            .unwrap_or(json!("RECOVERED"));
        position.insert("entry_execution_mode".to_string(), exec_mode);
        position.insert(
            "signal_score".to_string(),
            json!(st::pyf(pending.get("signal_score"), 0.0)),
        );
        position.insert(
            "option_score".to_string(),
            json!(st::pyf(pending.get("option_score"), 0.0)),
        );
        let strategy = pending
            .get("signal_strategy")
            .filter(|v| py_truthy(v))
            .cloned()
            .unwrap_or(json!("RECOVERED_BOT_ORDER"));
        position.insert("signal_strategy".to_string(), strategy);
        for (key, src) in [
            ("entry_mark_iv", "entry_mark_iv"),
            ("entry_delta", "entry_delta"),
            ("entry_gamma", "entry_gamma"),
            ("entry_theta", "entry_theta"),
            ("entry_vega", "entry_vega"),
            ("target_delta", "target_delta"),
            ("target_dte_days", "target_dte_days"),
            ("entry_iv_to_realized", "entry_iv_to_realized"),
            ("entry_skew_ratio", "entry_skew_ratio"),
            (
                "entry_surface_iv_premium_pct",
                "entry_surface_iv_premium_pct",
            ),
            ("entry_net_expected_edge_pct", "entry_net_expected_edge_pct"),
            (
                "entry_ioc_net_expected_edge_pct",
                "entry_ioc_net_expected_edge_pct",
            ),
            ("entry_flow_score", "entry_flow_score"),
        ] {
            position.insert(key.to_string(), json!(st::pyf(pending.get(src), 0.0)));
        }
        position.insert("status".to_string(), json!("OPEN"));
        position.insert("recovered_after_restart".to_string(), json!(true));
        let new_bankroll =
            (st::pyf(Some(&self.st_get("cash_bankroll_usdt")), 0.0) - total_cost).max(0.0);
        self.st_set("cash_bankroll_usdt", json!(new_bankroll));
        let position_value = Value::Object(position);
        self.st_set("active_position", position_value.clone());
        self.st_set("pending_entry", Value::Null);
        let signal_key = py_str_or_empty(pending.get("signal_key"));
        if !signal_key.is_empty() {
            let mut consumed: Vec<String> = match self.st_get("consumed_signal_keys") {
                Value::Array(items) => items.iter().map(|v| py_str(Some(v))).collect(),
                _ => Vec::new(),
            };
            if !consumed.contains(&signal_key) {
                consumed.push(signal_key);
            }
            let len = consumed.len();
            let start = len.saturating_sub(200);
            let trimmed: Vec<Value> = consumed[start..].iter().map(|s| json!(s)).collect();
            self.st_set("consumed_signal_keys", json!(trimmed));
        }
        self.st_set(
            "last_reason",
            json!(format!(
                "{} 봇 옵션 포지션 재연결",
                py_str_or_empty(pending.get("symbol"))
            )),
        );
        self.st_set("last_error", json!(""));
        let _ = self.save_state();
        let text = format!(
            "♻️ 재시작 후 봇 옵션 포지션을 주문 ID로 확인해 자동 관리에 다시 연결했습니다.\n종목: {} | 관리 수량 {}",
            py_str_or_empty(pending.get("symbol")),
            crate::app::telegram_bot::format_g(managed_qty)
        );
        self.notify_managed(&text).await;
        json!({"action": "recovered", "position": position_value})
    }

    pub(crate) async fn recover_legacy_bot_position_impl(&self, snapshot: &Value) -> Option<Value> {
        let mut exchange_info: Option<Value> = None;
        let positions = match snapshot.get("positions") {
            Some(Value::Array(items)) => items.clone(),
            _ => Vec::new(),
        };
        for row in positions {
            let symbol = py_str_or_empty(row.get("symbol"));
            let exchange_quantity = position_quantity(&row).abs();
            if symbol.is_empty() || exchange_quantity <= 1e-12 {
                continue;
            }
            let result: Option<Value> = async {
                let trades_payload = self.client().user_trades(&symbol, None, 100).await.ok()?;
                let trades = py_rows(&trades_payload);
                let mut order_cache: std::collections::HashMap<String, Value> =
                    std::collections::HashMap::new();
                let mut bot_buys: Vec<(Value, Value, f64)> = Vec::new();
                let mut bot_sell_qty = 0.0f64;
                for trade in trades {
                    let order_id_val = trade.get("orderId").cloned().unwrap_or(Value::Null);
                    let order_id_key = py_str(Some(&order_id_val));
                    let order = if let Some(existing) = order_cache.get(&order_id_key) {
                        existing.clone()
                    } else {
                        let fetched = match self
                            .client()
                            .query_order(&symbol, &OrderRef::Id(order_id_val.clone()))
                            .await
                        {
                            Ok(v) => v,
                            Err(_) => Value::Null,
                        };
                        order_cache.insert(order_id_key.clone(), fetched.clone());
                        fetched
                    };
                    let order_obj = if order.is_object() { order } else { json!({}) };
                    if !order_client_id(&order_obj).starts_with(BOT_CLIENT_PREFIX) {
                        continue;
                    }
                    let qty_source = trade
                        .get("quantity")
                        .filter(|v| py_truthy(v))
                        .cloned()
                        .or_else(|| trade.get("qty").cloned());
                    let qty = st::pyf(qty_source.as_ref(), 0.0).abs();
                    let side = py_str_or_empty(trade.get("side")).to_uppercase();
                    if side == "BUY" {
                        bot_buys.push((trade.clone(), order_obj, qty));
                    } else if side == "SELL" {
                        bot_sell_qty += qty;
                    }
                }
                let total_buy: f64 = bot_buys.iter().map(|(_, _, q)| q).sum();
                let owned_qty = (total_buy - bot_sell_qty).max(0.0);
                if owned_qty <= 1e-12 {
                    return None;
                }
                if exchange_info.is_none() {
                    exchange_info = Some(self.exchange_info_cached().await.ok()?);
                }
                let info = exchange_info.as_ref()?;
                let metadata = contract_metadata(info, &symbol);
                let metadata = match metadata {
                    Some(m) => m,
                    None => return None,
                };
                let expiry_check = st::pyf(metadata.get("expiry_date_ms"), 0.0);
                if expiry_check == 0.0 {
                    return None;
                }
                let managed_qty = exchange_quantity.min(owned_qty);
                let mut weighted = 0.0f64;
                for (trade, _, qty) in &bot_buys {
                    weighted += st::pyf(trade.get("price"), 0.0) * qty;
                }
                let entry_price = weighted / total_buy.max(1e-12);
                let (first_trade, first_order, _) = bot_buys.first()?.clone();
                let now_ms_val = self.now_ms();
                let created_at_ms = {
                    let t1 = first_trade.get("time").filter(|v| py_truthy(v)).cloned();
                    let t2 = first_order
                        .get("updateTime")
                        .filter(|v| py_truthy(v))
                        .cloned();
                    match t1.or(t2) {
                        Some(v) => st::pyf(Some(&v), 0.0) as i64,
                        None => now_ms_val,
                    }
                };
                let mut pending = match metadata.as_object() {
                    Some(m) => m.clone(),
                    None => Map::new(),
                };
                pending.insert("quantity".to_string(), json!(managed_qty));
                pending.insert("submitted_price".to_string(), json!(entry_price));
                pending.insert(
                    "client_order_id".to_string(),
                    json!(order_client_id(&first_order)),
                );
                pending.insert("execution_mode".to_string(), json!("LEGACY_RECOVERY"));
                pending.insert("created_at_ms".to_string(), json!(created_at_ms));
                pending.insert("spot_price".to_string(), json!(0.0));
                pending.insert("signal_strategy".to_string(), json!("RECOVERED_BOT_ORDER"));
                let pending_value = Value::Object(pending);
                let synthetic = json!({
                    "status": "FILLED",
                    "executedQty": managed_qty,
                    "avgPrice": entry_price,
                    "orderId": first_order.get("orderId").cloned().unwrap_or(Value::Null),
                    "updateTime": created_at_ms,
                });
                let activated = self
                    .activate_recovered_entry(&pending_value, &synthetic, exchange_quantity)
                    .await;
                if activated.is_null() {
                    None
                } else {
                    Some(activated)
                }
            }
            .await;
            if let Some(r) = result {
                return Some(r);
            }
        }
        None
    }

    // === Delegating accessors ===
    pub(crate) fn notify_managed(&self, text: &str) -> crate::app::ipc::BoxFut<()> {
        let text = text.to_string();
        // build an owned future that captures self by Arc through a clone of Some(notifier)
        let notifier = self.notifier.clone();
        Box::pin(async move {
            if let Some(n) = notifier {
                let fut = n(text);
                fut.await;
            }
        })
    }

    pub(crate) fn clear_pending_entry_managed(&self, client_id: Option<&str>) {
        self.clear_pending_entry(client_id);
    }
}

fn first_bid(bids: &Value, fallback: f64) -> f64 {
    if let Some(arr) = bids.as_array() {
        if let Some(first) = arr.first() {
            if let Some(inner) = first.as_array() {
                if let Some(v) = inner.first() {
                    return st::pyf(Some(v), 0.0);
                }
            }
        }
    }
    fallback
}

fn build_trade_key(row: &Value) -> String {
    let id = row.get("id").cloned().unwrap_or(Value::Null);
    if py_truthy(&id) {
        return py_str(Some(&id));
    }
    let trade_id = row.get("tradeId").cloned().unwrap_or(Value::Null);
    if py_truthy(&trade_id) {
        return py_str(Some(&trade_id));
    }
    let order_id = py_str(row.get("orderId"));
    let time = py_str(row.get("time"));
    let price = py_str(row.get("price"));
    let qty_source = row
        .get("quantity")
        .filter(|v| py_truthy(v))
        .cloned()
        .or_else(|| row.get("qty").cloned());
    let qty = py_str(qty_source.as_ref());
    format!("{}:{}:{}:{}", order_id, time, price, qty)
}

fn build_exercise_key(row: &Value) -> String {
    let id = row.get("id").cloned().unwrap_or(Value::Null);
    if py_truthy(&id) {
        return py_str(Some(&id));
    }
    let exercise_id = row.get("exerciseId").cloned().unwrap_or(Value::Null);
    if py_truthy(&exercise_id) {
        return py_str(Some(&exercise_id));
    }
    let time = py_str(row.get("time"));
    let amount = py_str(row.get("amount"));
    format!("exercise:{}:{}", time, amount)
}

fn order_client_id(order: &Value) -> String {
    let raw = order
        .get("clientOrderId")
        .filter(|v| py_truthy(v))
        .cloned()
        .or_else(|| {
            order
                .get("client_order_id")
                .filter(|v| py_truthy(v))
                .cloned()
        });
    py_str_or_empty(raw.as_ref())
}

fn contract_metadata(exchange_info: &Value, symbol: &str) -> Option<Value> {
    let rows: Vec<Value> = if exchange_info.is_object() {
        match exchange_info.get("optionSymbols") {
            Some(Value::Array(items)) => items.clone(),
            _ => py_rows(exchange_info),
        }
    } else {
        py_rows(exchange_info)
    };
    for row in rows {
        if py_str_or_empty(row.get("symbol")) != symbol {
            continue;
        }
        let mut filters: std::collections::HashMap<String, Value> =
            std::collections::HashMap::new();
        if let Some(Value::Array(items)) = row.get("filters") {
            for item in items {
                if item.is_object() {
                    let key = py_str_or_empty(item.get("filterType"));
                    filters.insert(key, item.clone());
                }
            }
        }
        let lot = filters.get("LOT_SIZE").cloned().unwrap_or(json!({}));
        let price_filter = filters.get("PRICE_FILTER").cloned().unwrap_or(json!({}));
        let side = {
            let raw = row.get("side").cloned().unwrap_or(Value::Null);
            if py_truthy(&raw) {
                raw
            } else if symbol.ends_with("-C") {
                json!("CALL")
            } else {
                json!("PUT")
            }
        };
        let tick_src = row
            .get("tickSize")
            .filter(|v| py_truthy(v))
            .cloned()
            .or_else(|| price_filter.get("tickSize").cloned());
        let min_qty_src = row
            .get("minQty")
            .filter(|v| py_truthy(v))
            .cloned()
            .or_else(|| lot.get("minQty").cloned());
        let mut meta = Map::new();
        meta.insert("symbol".to_string(), json!(symbol));
        meta.insert(
            "underlying".to_string(),
            row.get("underlying").cloned().unwrap_or(Value::Null),
        );
        meta.insert("side".to_string(), side);
        meta.insert("unit".to_string(), json!(st::pyf(row.get("unit"), 1.0)));
        meta.insert(
            "tick_size".to_string(),
            json!(st::pyf(tick_src.as_ref(), 0.0)),
        );
        meta.insert(
            "min_qty".to_string(),
            json!(st::pyf(min_qty_src.as_ref(), 0.0)),
        );
        meta.insert(
            "expiry_date_ms".to_string(),
            json!(st::pyf(row.get("expiryDate"), 0.0) as i64),
        );
        return Some(Value::Object(meta));
    }
    None
}

#[allow(dead_code)]
fn _assert_options_api_bound<C: OptionsApi>(_: &OptionsService<C>) {}

#[allow(dead_code)]
fn _noop(_: OptionsApiError) {}
