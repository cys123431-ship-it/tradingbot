//! S4: protection orders, profit lock, position management / exits, reverse
//! entries and notification texts (btc_ma_cross/service.py tail).

use std::str::FromStr;

use rust_decimal::Decimal;
use serde_json::{json, Map, Value};

use crate::core::candles::timeframe_ms;
use crate::core::rules::{round_price_to_tick, TradingRules};
use crate::core::sma_cross::{profit_lock_target, CrossResult};
use crate::exchange::{ExchangeError, FuturesExchange};
use crate::safety::order_gateway::ReduceOnlyCloseRequest;
use crate::store::order_state::{OrderIntent, OrderState};
use crate::strategy::config_base::TRADING_MODE_DRY_RUN;
use crate::strategy::service::base::py_truthy;
use crate::strategy::service::{
    py_f, symbol_pair, ServiceError, SmaCrossService, CCXT_SYMBOL, ERROR_RECOVERY, IDLE, MARKET_ID,
    POSITION_OPEN, PROFIT_LOCK_EXIT_REASON, PROTECTION_FAILED, PROTECTION_ORDERS_SUBMITTED,
};

// Re-exported helper used by the service (from trading.rs).
use crate::strategy::config_base::TRADING_MODE_LIVE;
use crate::strategy::service::sma::{STOP_MA_EXIT, TRANSIENT_ENTRY_BLOCKS};
use crate::strategy::service::trading::py_int;

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

fn get<'a>(m: &'a Map<String, Value>, key: &str) -> Option<&'a Value> {
    match m.get(key) {
        Some(Value::Null) | None => None,
        Some(v) => Some(v),
    }
}

fn get_str(m: &Map<String, Value>, key: &str) -> String {
    get(m, key)
        .map(|v| match v {
            Value::String(s) => s.clone(),
            Value::Null => String::new(),
            other => other.to_string(),
        })
        .unwrap_or_default()
}

fn get_num(m: &Map<String, Value>, key: &str) -> f64 {
    py_f(m.get(key), 0.0)
}

fn get_int(m: &Map<String, Value>, key: &str) -> i64 {
    get(m, key)
        .map(|v| match v {
            Value::Number(n) => n
                .as_i64()
                .or_else(|| n.as_f64().map(|f| f as i64))
                .unwrap_or(0),
            Value::String(s) => s.trim().parse::<f64>().map(|f| f as i64).unwrap_or(0),
            Value::Bool(b) => *b as i64,
            _ => 0,
        })
        .unwrap_or(0)
}

fn py_num(f: f64) -> Value {
    serde_json::Number::from_f64(f)
        .map(Value::Number)
        .unwrap_or(Value::Null)
}

/// Python `f"{f:g}"` for the notification strings.
fn fmt_g(f: f64) -> String {
    if !f.is_finite() {
        return format!("{}", f);
    }
    // `:g` drops trailing zeros and prints at most 6 significant digits.
    let s = format!("{:.6}", f);
    let s = s.trim_end_matches('0').trim_end_matches('.').to_string();
    if s.is_empty() || s == "-0" {
        "0".to_string()
    } else {
        s
    }
}

fn fmt_fixed(f: f64, digits: usize) -> String {
    format!("{:.*}", digits, f)
}

fn fmt_signed(f: f64, digits: usize) -> String {
    let body = format!("{:.*}", digits, f);
    if f >= 0.0 && !body.starts_with('-') {
        format!("+{body}")
    } else {
        body
    }
}

fn str_of(v: &Value) -> String {
    match v {
        Value::Null => "None".to_string(),
        Value::String(s) => s.clone(),
        Value::Bool(b) => {
            if *b {
                "True".to_string()
            } else {
                "False".to_string()
            }
        }
        Value::Number(n) => n.to_string(),
        other => other.to_string(),
    }
}

fn ser_steps(rules: &TradingRules) -> Value {
    serde_json::to_value(rules).unwrap_or(Value::Null)
}

/// Splits a list of `processed_signals` while keeping unknown values intact.
fn remove_signal(state: &mut Map<String, Value>, signal_id: &str) {
    let current = state
        .get("processed_signals")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    let filtered: Vec<Value> = current
        .into_iter()
        .filter(|v| v.as_str() != Some(signal_id))
        .collect();
    state.insert("processed_signals".to_string(), Value::Array(filtered));
}

fn processed_contains(state: &Map<String, Value>, signal_id: &str) -> bool {
    state
        .get("processed_signals")
        .and_then(|v| v.as_array())
        .map(|a| a.iter().any(|v| v.as_str() == Some(signal_id)))
        .unwrap_or(false)
}

fn push_processed(state: &mut Map<String, Value>, signal_id: &str) {
    let entry = state
        .entry("processed_signals".to_string())
        .or_insert_with(|| Value::Array(Vec::new()));
    if let Value::Array(arr) = entry {
        arr.push(Value::String(signal_id.to_string()));
    }
}

fn starts_with_any(text: &str, prefixes: &[&str]) -> bool {
    prefixes.iter().any(|p| text.starts_with(p))
}

impl<E: FuturesExchange + 'static> SmaCrossService<E> {
    // ---------------------------------------------------------- protection
    pub(crate) fn protection_spec(
        &self,
        cfg: &Value,
        trade: &Map<String, Value>,
        kind: &str,
    ) -> Result<Map<String, Value>, ServiceError> {
        let mut out = Map::new();
        if kind == "sl" {
            out.insert(
                "order_type".to_string(),
                Value::String("STOP_MARKET".to_string()),
            );
            out.insert(
                "trigger".to_string(),
                get(trade, "stop_price").cloned().unwrap_or(Value::Null),
            );
            out.insert("close_position".to_string(), Value::Bool(true));
            out.insert(
                "working_type".to_string(),
                cfg.get("working_type")
                    .cloned()
                    .unwrap_or(Value::String("MARK_PRICE".to_string())),
            );
            return Ok(out);
        }
        // Profit lock: reduce-only STOP_MARKET for the position quantity so it
        // can coexist with the closePosition emergency stop.
        out.insert(
            "order_type".to_string(),
            Value::String("STOP_MARKET".to_string()),
        );
        out.insert(
            "trigger".to_string(),
            get(trade, "lock_price").cloned().unwrap_or(Value::Null),
        );
        out.insert("close_position".to_string(), Value::Bool(false));
        out.insert(
            "quantity".to_string(),
            get(trade, "quantity").cloned().unwrap_or(Value::Null),
        );
        out.insert(
            "working_type".to_string(),
            cfg.get("lock_working_type")
                .cloned()
                .unwrap_or(Value::String("MARK_PRICE".to_string())),
        );
        Ok(out)
    }

    pub async fn attach_protection(
        &self,
        network: &str,
        cfg: &Value,
        state: &mut Map<String, Value>,
    ) -> Result<Value, ServiceError> {
        let mode = TRADING_MODE_LIVE;
        let mut trade = state
            .get("trade")
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default();
        self.set_phase(
            network,
            mode,
            state,
            PROTECTION_ORDERS_SUBMITTED,
            Map::new(),
        )?;
        if get(&trade, "stop_price")
            .map(|v| py_truthy(Some(v)))
            .unwrap_or(false)
        {
            let result = self
                .place_protection(network, cfg, &mut trade, "sl")
                .await?;
            let mut fields = Map::new();
            fields.insert("network".to_string(), Value::String(network.to_string()));
            fields.insert("kind".to_string(), Value::String("sl".to_string()));
            fields.insert("result".to_string(), Value::String(result.clone()));
            fields.insert(
                "symbol".to_string(),
                get(&trade, "market_id").cloned().unwrap_or(Value::Null),
            );
            fields.insert(
                "trigger".to_string(),
                get(&trade, "stop_price").cloned().unwrap_or(Value::Null),
            );
            fields.insert(
                "working_type".to_string(),
                cfg.get("working_type").cloned().unwrap_or(Value::Null),
            );
            self.ledger(network)?
                .lock()
                .unwrap()
                .event("PROTECTION", fields);
            if result != "OK" {
                state.insert("trade".to_string(), Value::Object(trade));
                return self
                    .failsafe_close(network, cfg, state, &format!("SL_{result}"))
                    .await;
            }
        }
        trade.insert(
            "last_protection_check_ms".to_string(),
            Value::Number(self.now_ms().into()),
        );
        let entry_id = get_str(&trade, "entry_client_order_id");
        if !entry_id.is_empty() {
            let store = self.store(network)?;
            let exists = {
                let guard = store.lock().unwrap();
                guard
                    .get(&entry_id)
                    .map_err(|e| ServiceError::new("StoreError", e.to_string()))?
                    .is_some()
            };
            if exists {
                let mut changes = Map::new();
                changes.insert(
                    "stop_order_id".to_string(),
                    get(&trade, "sl_client_algo_id")
                        .cloned()
                        .unwrap_or(Value::Null),
                );
                let mut guard = store.lock().unwrap();
                guard
                    .transition(&entry_id, OrderState::Protected.as_str(), changes)
                    .map_err(|e| ServiceError::new("StoreError", e.to_string()))?;
            }
        }
        self.set_phase(
            network,
            mode,
            state,
            POSITION_OPEN,
            json!({ "trade": trade })
                .as_object()
                .cloned()
                .unwrap_or_default(),
        )?;
        Ok(json!({"action": "entered", "trade": trade}))
    }

    pub async fn verify_protection(
        &self,
        network: &str,
        cfg: &Value,
        state: &mut Map<String, Value>,
    ) -> Result<(), ServiceError> {
        let mut trade = state
            .get("trade")
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default();
        let market = symbol_pair(state.get("trade")).1;
        let orders = match self.open_algo_orders(network, Some(&market)).await {
            Ok(v) => v,
            Err(exc) => {
                let mut fields = Map::new();
                fields.insert("network".to_string(), Value::String(network.to_string()));
                fields.insert("error".to_string(), Value::String(exc.to_string()));
                self.ledger(network)?
                    .lock()
                    .unwrap()
                    .event("PROTECTION_CHECK_FAILED", fields);
                return Ok(());
            }
        };
        let ids: std::collections::HashSet<String> = orders
            .iter()
            .map(|o| o.get("clientAlgoId").map(str_of).unwrap_or_default())
            .collect();
        let stop_price_set = get(&trade, "stop_price")
            .map(|v| py_truthy(Some(v)))
            .unwrap_or(false);
        if stop_price_set {
            let sl_id = get_str(&trade, "sl_client_algo_id");
            if !ids.contains(&sl_id) {
                let rev = get_int(&trade, "sl_revision") + 1;
                trade.insert("sl_revision".to_string(), Value::Number(rev.into()));
                let result = self
                    .place_protection(network, cfg, &mut trade, "sl")
                    .await?;
                let mut fields = Map::new();
                fields.insert("network".to_string(), Value::String(network.to_string()));
                fields.insert("kind".to_string(), Value::String("sl".to_string()));
                fields.insert("result".to_string(), Value::String(result.clone()));
                self.ledger(network)?
                    .lock()
                    .unwrap()
                    .event("PROTECTION_REPAIR", fields);
                if result != "OK" {
                    state.insert("trade".to_string(), Value::Object(trade));
                    self.failsafe_close(network, cfg, state, &format!("SL_MISSING_{result}"))
                        .await?;
                    return Ok(());
                }
            }
        }
        let lock_price_set = get(&trade, "lock_price")
            .map(|v| py_truthy(Some(v)))
            .unwrap_or(false);
        if lock_price_set {
            let lock_id = get_str(&trade, "lock_client_algo_id");
            if !ids.contains(&lock_id) {
                let rev = get_int(&trade, "lock_revision") + 1;
                trade.insert("lock_revision".to_string(), Value::Number(rev.into()));
                let result = self
                    .place_protection(network, cfg, &mut trade, "lock")
                    .await?;
                let mut fields = Map::new();
                fields.insert("network".to_string(), Value::String(network.to_string()));
                fields.insert("kind".to_string(), Value::String("lock".to_string()));
                fields.insert("result".to_string(), Value::String(result.clone()));
                self.ledger(network)?
                    .lock()
                    .unwrap()
                    .event("PROTECTION_REPAIR", fields);
                if result == "BREACHED" {
                    state.insert("trade".to_string(), Value::Object(trade));
                    self.close_live(network, cfg, state, "profit_lock_breached", "PROFIT_LOCK")
                        .await?;
                    return Ok(());
                }
            }
        }
        trade.insert(
            "last_protection_check_ms".to_string(),
            Value::Number(self.now_ms().into()),
        );
        state.insert("trade".to_string(), Value::Object(trade));
        self.save_state(network, TRADING_MODE_LIVE, state)?;
        Ok(())
    }

    // ---------------------------------------------------------- profit lock
    fn lock_update(
        &self,
        cfg: &Value,
        trade: &mut Map<String, Value>,
        price: f64,
    ) -> Result<Option<Value>, ServiceError> {
        let side = get_str(trade, "side");
        let long = side == "LONG";
        let entry_price = get_num(trade, "entry_price");
        let mut best = py_f(get(trade, "best_price"), entry_price);
        best = if long {
            best.max(price)
        } else {
            best.min(price)
        };
        trade.insert(
            "best_price".to_string(),
            Value::String(crate::store::ledger::py_float_repr(best)),
        );
        let leverage = get_num(trade, "leverage");
        let start_roi = py_f(cfg.get("lock_start_roi_percent"), 0.0);
        let step = py_f(cfg.get("lock_step_percent"), 0.0);
        let gap = py_f(cfg.get("lock_gap_percent"), 0.0);
        let Some((reached, locked, stop)) =
            profit_lock_target(&side, entry_price, best, leverage, start_roi, step, gap)
        else {
            return Ok(None);
        };
        // Python: trade.get("lock_roi") not in ("", None)
        let had = get(trade, "lock_roi")
            .map(|v| !v.is_null() && v.as_str() != Some(""))
            .unwrap_or(false);
        if had && locked <= get_num(trade, "lock_roi") + 1e-9 {
            return Ok(None);
        }
        let tick = get_str(trade, "tick_size");
        let tick = if tick.is_empty() {
            "0.1".to_string()
        } else {
            tick
        };
        let direction = if long { "down" } else { "up" };
        // Python: round_price_to_tick(stop, ...) -> D(str(stop)); errors propagate.
        let rounded =
            round_price_to_tick(&crate::store::ledger::py_float_repr(stop), &tick, direction)
                .map_err(|e| ServiceError::new("TradingRulesError", e.to_string()))?;
        Ok(Some(json!({
            "reached": py_num(reached),
            "locked": py_num(locked),
            "price": rounded.to_string(),
        })))
    }

    async fn raise_lock_live(
        &self,
        network: &str,
        cfg: &Value,
        state: &mut Map<String, Value>,
        target: &Value,
    ) -> Result<Option<Value>, ServiceError> {
        let mut trade = state
            .get("trade")
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default();
        let old_id = get_str(&trade, "lock_client_algo_id");
        let previous = (
            get(&trade, "lock_roi").cloned().unwrap_or(Value::Null),
            get(&trade, "lock_price").cloned().unwrap_or(Value::Null),
        );
        let locked = target.get("locked").cloned().unwrap_or(Value::Null);
        let price = target.get("price").cloned().unwrap_or(Value::Null);
        trade.insert("lock_roi".to_string(), Value::String(str_of(&locked)));
        trade.insert("lock_price".to_string(), price.clone());
        if !old_id.is_empty() {
            let rev = get_int(&trade, "lock_revision") + 1;
            trade.insert("lock_revision".to_string(), Value::Number(rev.into()));
        }
        let result = self
            .place_protection(network, cfg, &mut trade, "lock")
            .await?;
        let mut fields = Map::new();
        fields.insert("network".to_string(), Value::String(network.to_string()));
        fields.insert(
            "symbol".to_string(),
            get(&trade, "market_id").cloned().unwrap_or(Value::Null),
        );
        fields.insert(
            "reached_roi".to_string(),
            target.get("reached").cloned().unwrap_or(Value::Null),
        );
        fields.insert(
            "locked_roi".to_string(),
            target.get("locked").cloned().unwrap_or(Value::Null),
        );
        fields.insert("trigger".to_string(), price.clone());
        fields.insert("result".to_string(), Value::String(result.clone()));
        self.ledger(network)?
            .lock()
            .unwrap()
            .event("PROFIT_LOCK", fields);
        if result == "BREACHED" {
            state.insert("trade".to_string(), Value::Object(trade));
            let closed = self
                .close_live(network, cfg, state, "profit_lock_breached", "PROFIT_LOCK")
                .await?;
            return Ok(Some(closed));
        }
        if result != "OK" {
            trade.insert("lock_roi".to_string(), previous.0);
            trade.insert("lock_price".to_string(), previous.1);
            state.insert("trade".to_string(), Value::Object(trade));
            self.save_state(network, TRADING_MODE_LIVE, state)?;
            return Ok(None);
        }
        let new_id = get_str(&trade, "lock_client_algo_id");
        if !old_id.is_empty() && old_id != new_id {
            match self.cancel_algo(network, &old_id).await {
                Ok(_) => {}
                Err(exc) => {
                    let mut fields = Map::new();
                    fields.insert("network".to_string(), Value::String(network.to_string()));
                    fields.insert("client_algo_id".to_string(), Value::String(old_id.clone()));
                    fields.insert("error".to_string(), Value::String(exc.to_string()));
                    self.ledger(network)?
                        .lock()
                        .unwrap()
                        .event("CANCEL_FAILED", fields);
                }
            }
        }
        state.insert("trade".to_string(), Value::Object(trade));
        self.save_state(network, TRADING_MODE_LIVE, state)?;
        let reached = py_f(target.get("reached"), 0.0);
        let locked_f = py_f(target.get("locked"), 0.0);
        let price_text = str_of(&price);
        let market = get_str(
            state
                .get("trade")
                .and_then(|v| v.as_object())
                .unwrap_or(&Map::new()),
            "market_id",
        );
        let trade_id = get_str(
            state
                .get("trade")
                .and_then(|v| v.as_object())
                .unwrap_or(&Map::new()),
            "trade_id",
        );
        let text = format!(
            "🔒 {} {}: 증거금 수익 +{}% 도달 → +{}% 확보 손절 {}",
            self.label(),
            market,
            fmt_g(reached),
            fmt_g(locked_f),
            price_text,
        );
        let key = format!("lock:{trade_id}:{}", fmt_g(locked_f));
        self.notify(text, Some(&key)).await;
        Ok(None)
    }

    // --------------------------------------------------------------- LIVE
    async fn close_live(
        &self,
        network: &str,
        cfg: &Value,
        state: &mut Map<String, Value>,
        reason: &str,
        exit_reason: &str,
    ) -> Result<Value, ServiceError> {
        let trade = state
            .get("trade")
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default();
        let (ccxt_symbol, _market) = symbol_pair(Some(&Value::Object(trade.clone())));
        let position = self.fetch_position(network, &ccxt_symbol).await?;
        let quantity = crate::strategy::service::position_qty(position.as_ref());
        if position.is_none() || quantity <= 0.0 {
            return self
                .finalize_live(network, cfg, state, Some(exit_reason))
                .await;
        }
        let (order_gateway, _) = self.gateway(network).await?;
        let side = get_str(&trade, "side").to_lowercase();
        let signature = get(&trade, "signal_bar_ms").cloned().unwrap_or(Value::Null);
        let result = order_gateway
            .submit_reduce_only_close(ReduceOnlyCloseRequest {
                strategy: self.client_id().to_string(),
                symbol: ccxt_symbol.clone(),
                position_side: side,
                position_signature: signature,
                qty: quantity,
                reason: reason.to_string(),
                params: None,
                order_intent: Some(OrderIntent::Close),
                order_purpose: Some(reason.to_string()),
                order_type: "market".to_string(),
                price: None,
                leg: None,
            })
            .await?;
        let mut fields = Map::new();
        fields.insert("network".to_string(), Value::String(network.to_string()));
        fields.insert(
            "symbol".to_string(),
            get(&trade, "market_id").cloned().unwrap_or(Value::Null),
        );
        fields.insert("reason".to_string(), Value::String(reason.to_string()));
        fields.insert("state".to_string(), Value::String(result.state.clone()));
        fields.insert(
            "error".to_string(),
            result
                .error
                .clone()
                .map(Value::String)
                .unwrap_or(Value::Null),
        );
        self.ledger(network)?
            .lock()
            .unwrap()
            .event("CLOSE_ORDER", fields);
        if result.state == OrderState::Closed.as_str() {
            return self
                .finalize_live(network, cfg, state, Some(exit_reason))
                .await;
        }
        let delays: Vec<f64> = cfg
            .get("protection_retry_delays_seconds")
            .and_then(|v| v.as_array())
            .map(|a| a.iter().map(|v| py_f(Some(v), 0.0)).collect())
            .unwrap_or_default();
        for delay in delays {
            (self.deps.sleep)(delay).await;
            let still_open = match self.fetch_position(network, &ccxt_symbol).await {
                Ok(v) => v,
                Err(_) => continue,
            };
            let still_open = still_open
                .as_ref()
                .map(|v| crate::strategy::service::position_qty(Some(v)) > 0.0)
                .unwrap_or(false);
            if !still_open {
                return self
                    .finalize_live(network, cfg, state, Some(exit_reason))
                    .await;
            }
        }
        let error_text = result.error.clone().unwrap_or_default();
        let last_error = format!("CLOSE_{}: {}", result.state, error_text)
            .trim()
            .to_string();
        state.insert("last_error".to_string(), Value::String(last_error.clone()));
        self.save_state(network, TRADING_MODE_LIVE, state)?;
        Ok(json!({"action": "closing", "reason": last_error}))
    }

    pub async fn manage_live(
        &self,
        network: &str,
        cfg: &Value,
        state: &mut Map<String, Value>,
    ) -> Result<Value, ServiceError> {
        let mode = TRADING_MODE_LIVE;
        let trade = state
            .get("trade")
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default();
        let (ccxt_symbol, market) = symbol_pair(Some(&Value::Object(trade.clone())));
        let position = match self.fetch_position(network, &ccxt_symbol).await {
            Ok(v) => v,
            Err(exc) => {
                let mut fields = Map::new();
                fields.insert("network".to_string(), Value::String(network.to_string()));
                fields.insert("error".to_string(), Value::String(exc.to_string()));
                self.ledger(network)?
                    .lock()
                    .unwrap()
                    .event("POSITION_FETCH_FAILED", fields);
                return Ok(json!({"action": "managed", "reason": "POSITION_UNKNOWN"}));
            }
        };
        let pending = state
            .get("trade")
            .and_then(|v| v.get("pending_reverse"))
            .cloned()
            .unwrap_or(Value::Null);
        let stop_pending = get(&trade, "stop_ma_exit_pending")
            .map(|v| py_truthy(Some(v)))
            .unwrap_or(false);
        let flat = position.is_none();
        if flat {
            if stop_pending {
                return self
                    .finalize_live(network, cfg, state, Some(STOP_MA_EXIT))
                    .await;
            }
            if py_truthy(Some(&pending)) {
                let closed = self
                    .finalize_live(network, cfg, state, Some("CROSS_REVERSE"))
                    .await?;
                return self.enter_reverse(network, cfg, &pending, closed).await;
            }
            return self.finalize_live(network, cfg, state, None).await;
        }
        let phase = state
            .get("phase")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
            .unwrap_or_default();
        let failsafe = get(&trade, "failsafe")
            .map(|v| py_truthy(Some(v)))
            .unwrap_or(false);
        if phase == PROTECTION_FAILED || phase == ERROR_RECOVERY || failsafe {
            let last_error = state
                .get("last_error")
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty())
                .map(|s| s.to_string())
                .unwrap_or_else(|| "RETRY_FAILSAFE".to_string());
            return self.failsafe_close(network, cfg, state, &last_error).await;
        }
        if py_truthy(Some(&pending)) {
            return self.reverse_live(network, cfg, state, &pending).await;
        }
        if stop_pending {
            // A previous stop-SMA close did not complete: keep closing.
            return self
                .close_live(network, cfg, state, "stop_ma", STOP_MA_EXIT)
                .await;
        }
        let (signal, _) = self
            .evaluate_new_bar(network, cfg, mode, state, Some(&trade))
            .await?;
        let mut trade = state
            .get("trade")
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default();
        let opposite = self.opposite_signal(cfg, &trade, signal.as_ref());
        if let Some(sig) = opposite {
            return self.reverse_live(network, cfg, state, &sig).await;
        }
        if self.stop_ma_hit(&trade, signal.as_ref()) {
            // The SMA200 reversal above wins when both happen on one bar.
            trade.insert("stop_ma_exit_pending".to_string(), Value::Bool(true));
            state.insert("trade".to_string(), Value::Object(trade));
            self.save_state(network, mode, state)?;
            return self
                .close_live(network, cfg, state, "stop_ma", STOP_MA_EXIT)
                .await;
        }
        let mark = match self.premium_index(network, &market).await {
            Ok(map) => py_f(map.get("mark_price"), 0.0),
            Err(exc) => {
                let mut fields = Map::new();
                fields.insert("network".to_string(), Value::String(network.to_string()));
                fields.insert("error".to_string(), Value::String(exc.to_string()));
                self.ledger(network)?
                    .lock()
                    .unwrap()
                    .event("PRICE_FETCH_FAILED", fields);
                return Ok(json!({"action": "managed", "reason": "PRICE_UNKNOWN"}));
            }
        };
        let target = self.lock_update(cfg, &mut trade, mark)?;
        state.insert("trade".to_string(), Value::Object(trade));
        self.save_state(network, mode, state)?;
        if let Some(t) = &target {
            let closed = self.raise_lock_live(network, cfg, state, t).await?;
            if let Some(v) = closed {
                return Ok(v);
            }
        }
        let last_check = state
            .get("trade")
            .and_then(|v| v.as_object())
            .map(|m| get_int(m, "last_protection_check_ms"))
            .unwrap_or(0);
        let interval = cfg
            .get("protection_check_interval_seconds")
            .map(|v| py_f(Some(v), 0.0))
            .unwrap_or(0.0);
        let due = (self.now_ms() - last_check) as f64 >= interval * 1000.0;
        if due {
            self.verify_protection(network, cfg, state).await?;
        }
        Ok(json!({"action": "managed"}))
    }

    async fn reverse_live(
        &self,
        network: &str,
        cfg: &Value,
        state: &mut Map<String, Value>,
        signal: &Value,
    ) -> Result<Value, ServiceError> {
        let mut trade = state
            .get("trade")
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default();
        trade.insert("pending_reverse".to_string(), signal.clone());
        state.insert("trade".to_string(), Value::Object(trade));
        self.save_state(network, TRADING_MODE_LIVE, state)?;
        let closed = self
            .close_live(network, cfg, state, "cross_reverse", "CROSS_REVERSE")
            .await?;
        let action = closed.get("action").and_then(|v| v.as_str()).unwrap_or("");
        if action != "exited" {
            return Ok(closed);
        }
        self.enter_reverse(network, cfg, signal, closed).await
    }

    async fn enter_reverse(
        &self,
        network: &str,
        cfg: &Value,
        signal: &Value,
        closed: Value,
    ) -> Result<Value, ServiceError> {
        let mut state = self.load_state(network, TRADING_MODE_LIVE)?;
        if !cfg
            .get("enabled")
            .map(|v| py_truthy(Some(v)))
            .unwrap_or(false)
        {
            return Ok(json!({
                "action": "exited",
                "reason": "CROSS_REVERSE (신규진입 OFF라 반대 진입 안 함)",
            }));
        }
        let entered = self
            .enter_on_signal(network, cfg, TRADING_MODE_LIVE, &mut state, signal)
            .await?;
        if self.hold_for_retry(network, TRADING_MODE_LIVE, signal, &entered)? {
            let reason = entered
                .get("reason")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            return Ok(json!({
                "action": "exited",
                "reason": format!("CROSS_REVERSE · 반대 진입 보류 후 재시도: {reason}"),
            }));
        }
        Ok(json!({"action": "reversed", "close": closed, "entry": entered}))
    }

    fn hold_for_retry(
        &self,
        network: &str,
        mode: &str,
        signal: &Value,
        entered: &Value,
    ) -> Result<bool, ServiceError> {
        let reason = entered
            .get("reason")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let action = entered.get("action").and_then(|v| v.as_str()).unwrap_or("");
        if action != "skip" || !starts_with_any(&reason, TRANSIENT_ENTRY_BLOCKS) {
            return Ok(false);
        }
        let signal_id = signal
            .get("signal_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let mut state = self.load_state(network, mode)?;
        remove_signal(&mut state, &signal_id);
        state.insert("pending_reverse_entry".to_string(), signal.clone());
        self.save_state(network, mode, &mut state)?;
        let mut fields = Map::new();
        fields.insert("mode".to_string(), Value::String(mode.to_string()));
        fields.insert("network".to_string(), Value::String(network.to_string()));
        fields.insert("signal_id".to_string(), Value::String(signal_id));
        fields.insert("reason".to_string(), Value::String(reason));
        self.ledger(network)?
            .lock()
            .unwrap()
            .event("REVERSE_ENTRY_HELD", fields);
        Ok(true)
    }

    pub(crate) async fn retry_reverse_entry(
        &self,
        network: &str,
        cfg: &Value,
        mode: &str,
        state: &mut Map<String, Value>,
        signal: &Value,
    ) -> Result<Value, ServiceError> {
        let timeframe = signal
            .get("timeframe")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
            .unwrap_or_else(|| {
                cfg.get("timeframe")
                    .and_then(|v| v.as_str())
                    .unwrap_or("1h")
                    .to_string()
            });
        let span = timeframe_ms(&timeframe).unwrap_or(3_600_000);
        let bar_close_ms = Self::signal_bar(signal) + span;
        let window = cfg
            .get("reverse_retry_window_seconds")
            .map(|v| py_f(Some(v), 0.0))
            .unwrap_or(0.0);
        let signal_id = signal
            .get("signal_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        if (self.now_ms() - bar_close_ms) as f64 > window * 1000.0 {
            state.insert("pending_reverse_entry".to_string(), Value::Null);
            if !processed_contains(state, &signal_id) {
                push_processed(state, &signal_id);
            }
            self.save_state(network, mode, state)?;
            let mut fields = Map::new();
            fields.insert("mode".to_string(), Value::String(mode.to_string()));
            fields.insert("network".to_string(), Value::String(network.to_string()));
            fields.insert("signal_id".to_string(), Value::String(signal_id.clone()));
            self.ledger(network)?
                .lock()
                .unwrap()
                .event("REVERSE_ENTRY_EXPIRED", fields);
            let minutes = (window as i64) / 60;
            let text = format!(
                "⚠️ {}: 반대 진입을 {}분 동안 재시도했지만 막혀 있어 포기했습니다. 다음 크로스까지 대기합니다.",
                self.label(),
                minutes,
            );
            self.notify(text, Some(&format!("expired:{signal_id}")))
                .await;
            return Ok(json!({"action": "skip", "reason": "REVERSE_ENTRY_EXPIRED"}));
        }
        state.insert("pending_reverse_entry".to_string(), Value::Null);
        self.save_state(network, mode, state)?;
        let entered = self
            .enter_on_signal(network, cfg, mode, state, signal)
            .await?;
        if self.hold_for_retry(network, mode, signal, &entered)? {
            let reason = entered
                .get("reason")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            return Ok(json!({
                "action": "waiting",
                "reason": format!("REVERSE_ENTRY_RETRY: {reason}"),
            }));
        }
        Ok(entered)
    }

    // --------------------------------------------------------------- DRY_RUN
    pub async fn manage_paper(
        &self,
        network: &str,
        cfg: &Value,
        state: &mut Map<String, Value>,
    ) -> Result<Value, ServiceError> {
        let mode = TRADING_MODE_DRY_RUN;
        let mut trade = state
            .get("trade")
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default();
        let (ccxt_symbol, market) = symbol_pair(Some(&Value::Object(trade.clone())));
        let exchange = self.exchange(network).await?;
        let since = {
            let v = get_int(&trade, "paper_last_checked_ms");
            if v != 0 {
                v
            } else {
                get_int(&trade, "entry_ms")
            }
        };
        let rows = match exchange
            .fetch_ohlcv(&ccxt_symbol, "1m", Some(since - 60_000), Some(1000))
            .await
        {
            Ok(v) => v,
            Err(exc) => {
                let mut fields = Map::new();
                fields.insert("mode".to_string(), Value::String(mode.to_string()));
                fields.insert("network".to_string(), Value::String(network.to_string()));
                fields.insert(
                    "skip_reason".to_string(),
                    Value::String(format!("PAPER_PRICE_UNAVAILABLE: {exc}")),
                );
                self.ledger(network)?.lock().unwrap().event("SKIP", fields);
                return Ok(json!({"action": "managed", "reason": "PRICE_UNAVAILABLE"}));
            }
        };
        let long = get_str(&trade, "side") == "LONG";
        let slip = cfg
            .get("slippage_buffer_pct")
            .map(|v| py_f(Some(v), 0.0))
            .unwrap_or(0.0);
        let entry_ms = get_int(&trade, "entry_ms");
        let mut sorted: Vec<[f64; 6]> = rows;
        sorted.sort_by(|a, b| a[0].partial_cmp(&b[0]).unwrap_or(std::cmp::Ordering::Equal));
        for row in sorted {
            let open_ms = row[0] as i64;
            if open_ms + 60_000 <= entry_ms {
                continue;
            }
            let high = row[2];
            let low = row[3];
            let close_ms = open_ms + 60_000;
            // Stops first with the levels known before this candle (conservative).
            let candidates: Vec<(&str, String)> = vec![
                ("EMERGENCY_STOP", get_str(&trade, "stop_price")),
                ("PROFIT_LOCK", get_str(&trade, "lock_price")),
            ];
            for (kind, level_text) in candidates {
                if level_text.is_empty() {
                    continue;
                }
                let level = level_text.trim().parse::<f64>().unwrap_or(0.0);
                if (long && low <= level) || (!long && high >= level) {
                    let price = if long {
                        level * (1.0 - slip)
                    } else {
                        level * (1.0 + slip)
                    };
                    self.settle_paper_funding(network, &mut trade, close_ms)
                        .await;
                    return self
                        .finalize_paper(
                            network,
                            cfg,
                            state,
                            &crate::store::ledger::py_float_repr(price),
                            kind,
                            close_ms,
                        )
                        .await;
                }
            }
            let target = self.lock_update(cfg, &mut trade, if long { high } else { low })?;
            if let Some(t) = target {
                let locked = t.get("locked").cloned().unwrap_or(Value::Null);
                let price = t.get("price").cloned().unwrap_or(Value::Null);
                trade.insert("lock_roi".to_string(), Value::String(str_of(&locked)));
                trade.insert("lock_price".to_string(), price.clone());
                let mut fields = Map::new();
                fields.insert("mode".to_string(), Value::String(mode.to_string()));
                fields.insert("network".to_string(), Value::String(network.to_string()));
                fields.insert("dry_run".to_string(), Value::Bool(true));
                fields.insert("symbol".to_string(), Value::String(market.clone()));
                fields.insert(
                    "reached_roi".to_string(),
                    t.get("reached").cloned().unwrap_or(Value::Null),
                );
                fields.insert("locked_roi".to_string(), locked);
                fields.insert("trigger".to_string(), price);
                self.ledger(network)?
                    .lock()
                    .unwrap()
                    .event("PROFIT_LOCK", fields);
            }
        }
        let now = self.now_ms();
        trade.insert(
            "paper_last_checked_ms".to_string(),
            Value::Number(now.into()),
        );
        self.settle_paper_funding(network, &mut trade, now).await;
        state.insert("trade".to_string(), Value::Object(trade));
        self.save_state(network, mode, state)?;
        let trade_ref = state
            .get("trade")
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default();
        let (signal, _) = self
            .evaluate_new_bar(network, cfg, mode, state, Some(&trade_ref))
            .await?;
        let raw_signal = signal;
        let signal = self.opposite_signal(cfg, &trade_ref, raw_signal.as_ref());
        if signal.is_none() && self.stop_ma_hit(&trade_ref, raw_signal.as_ref()) {
            let price = match self.premium_index(network, &market).await {
                Ok(map) => py_f(map.get("mark_price"), 0.0),
                Err(_) => raw_signal
                    .as_ref()
                    .map(|s| py_f(s.get("close"), 0.0))
                    .unwrap_or(0.0),
            };
            let price = if long {
                price * (1.0 - slip)
            } else {
                price * (1.0 + slip)
            };
            self.finalize_paper(
                network,
                cfg,
                state,
                &crate::store::ledger::py_float_repr(price),
                STOP_MA_EXIT,
                now,
            )
            .await?;
            return Ok(json!({"action": "exited", "reason": STOP_MA_EXIT}));
        }
        if let Some(sig) = signal {
            let price = match self.premium_index(network, &market).await {
                Ok(map) => py_f(map.get("mark_price"), 0.0),
                Err(_) => py_f(sig.get("close"), 0.0),
            };
            let price = if long {
                price * (1.0 - slip)
            } else {
                price * (1.0 + slip)
            };
            self.finalize_paper(
                network,
                cfg,
                state,
                &crate::store::ledger::py_float_repr(price),
                "CROSS_REVERSE",
                now,
            )
            .await?;
            let mut state = self.load_state(network, mode)?;
            if !cfg
                .get("enabled")
                .map(|v| py_truthy(Some(v)))
                .unwrap_or(false)
            {
                return Ok(json!({"action": "exited", "reason": "CROSS_REVERSE"}));
            }
            let entered = self
                .enter_on_signal(network, cfg, mode, &mut state, &sig)
                .await?;
            return Ok(json!({"action": "reversed", "entry": entered}));
        }
        Ok(json!({"action": "managed"}))
    }

    // --------------------------------------------------------------- texts
    pub(crate) fn entry_text(
        &self,
        trade: &Map<String, Value>,
        mode: &str,
    ) -> Result<String, ServiceError> {
        let tag = if mode == TRADING_MODE_DRY_RUN {
            "🧪 DRY_RUN"
        } else {
            "💥 LIVE"
        };
        let market = {
            let m = get_str(trade, "market_id");
            if m.is_empty() {
                MARKET_ID.to_string()
            } else {
                m
            }
        };
        let base = if market.ends_with("USDT") {
            market[..market.len() - 4].to_string()
        } else {
            market.clone()
        };
        let stop = if get(trade, "stop_price")
            .map(|v| py_truthy(Some(v)))
            .unwrap_or(false)
        {
            format!("비상손절 {}", get_str(trade, "stop_price"))
        } else {
            "비상손절 OFF".to_string()
        };
        let entry_type = get_str(trade, "entry_type");
        let kind = match entry_type.as_str() {
            "TREND" => "방향 유지",
            "ROTATE" => "청산 후 다른 코인 전환",
            "REALIGN" => "SMA 반대편 전환",
            _ => "돌파",
        };
        let timeframe = get_str(trade, "timeframe");
        let fast = get(trade, "fast_period")
            .cloned()
            .unwrap_or_else(|| json!(3));
        let slow = get(trade, "slow_period")
            .cloned()
            .unwrap_or_else(|| json!(200));
        let network = get_str(trade, "network");
        let side = get_str(trade, "side");
        let quantity = get_str(trade, "quantity");
        let entry_price = get_str(trade, "entry_price");
        let margin = get_num(trade, "margin_used");
        let leverage = get(trade, "leverage").cloned().unwrap_or(Value::Null);
        Ok(format!(
            "📈 {} 진입 ({tag} · {network} · {timeframe} · SMA{}/{} · {kind})\n{market} {side} {quantity} {base} @ {entry_price} · 증거금 {} USDT x{}\n{stop} · 증거금 +5% 도달 시 수익 확보 손절 시작",
            self.label(),
            str_of(&fast),
            str_of(&slow),
            fmt_fixed(margin, 2),
            str_of(&leverage),
        ))
    }

    pub(crate) fn exit_text(
        &self,
        trade: &Map<String, Value>,
        mode: &str,
    ) -> Result<String, ServiceError> {
        let tag = if mode == TRADING_MODE_DRY_RUN {
            "🧪 DRY_RUN"
        } else {
            "💥 LIVE"
        };
        let net_pnl = get_num(trade, "net_pnl");
        let margin = get_num(trade, "margin_used").max(1e-9);
        let roi = net_pnl / margin * 100.0;
        let market = {
            let m = get_str(trade, "market_id");
            if m.is_empty() {
                MARKET_ID.to_string()
            } else {
                m
            }
        };
        let network = get_str(trade, "network");
        let exit_reason = get_str(trade, "exit_reason");
        let side = get_str(trade, "side");
        let entry_price = get_str(trade, "entry_price");
        let exit_price = get_str(trade, "exit_price");
        let commission = get_num(trade, "commission");
        let funding = get_num(trade, "funding");
        Ok(format!(
            "🏁 {} 청산 ({tag} · {network}) — {exit_reason}\n{market} {side} {entry_price} → {}\n순손익 {} USDT (증거금 대비 {}%, 수수료 {:.4}, 펀딩 {})",
            self.label(),
            fmt_g(exit_price.parse::<f64>().unwrap_or(0.0)),
            fmt_signed(net_pnl, 4),
            fmt_signed(roi, 2),
            commission,
            fmt_signed(funding, 4),
        ))
    }
}

// ---------------------------------------------------------------------------
// Local helpers
// ---------------------------------------------------------------------------

/// Formats a float the same way Python `str(float)` does (shortest repr).
fn format_g_dec(f: f64) -> String {
    crate::store::ledger::py_float_repr(f)
}
