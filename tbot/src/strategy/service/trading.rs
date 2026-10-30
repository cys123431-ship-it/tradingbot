//! Entry, exit, protection and reconciliation flow moved from
//! BtcEmaPullbackService methods that BtcMaCrossService does not override.
//!
//! Child-overridden methods (`new_trade`, `on_trade_closed`,
//! `protection_spec`, `attach_protection`, `manage_live`, `manage_paper`,
//! `maybe_enter`, `pre_trade_validation`, `verify_protection`,
//! `entry_text`, `exit_text`) still live in `stubs.rs` / `stubs2.rs`.

use std::collections::{HashMap, HashSet};

use crate::store::ledger::py_float_repr;
use rust_decimal::Decimal;
use serde_json::{json, Map, Value};

use crate::core::rules::{d, round_price_to_tick};
use crate::exchange::{ExchangeError, FuturesExchange};
use crate::safety::algo_gateway::AlgoLookupStatus;
use crate::safety::order_gateway::ReduceOnlyCloseRequest;
use crate::store::ledger::trading_day_bounds;
use crate::store::order_state::{
    build_client_order_id, BuildClientOrderIdArgs, OrderIntent, OrderState,
};
use crate::strategy::config_base::{
    effective_trading_mode, TRADING_MODE_DRY_RUN, TRADING_MODE_LIVE,
};
use crate::strategy::service::{
    iso_ms, position_entry, position_qty, position_side, py_f, rules_from_value, symbol_pair,
    ServiceError, SmaCrossService, CCXT_SYMBOL, ENTRY_FILLED, ENTRY_SUBMITTED, ERROR_RECOVERY,
    EXIT_FILLED, IDLE, MARKET_ID, OPEN_ALGO_STATUSES, POSITION_OPEN, POSITION_RECONCILIATION,
    PROTECTION_FAILED, PROTECTION_ORDERS_SUBMITTED, TRADE_RECORDED,
};

// ---------------------------------------------------------------------------
// Local helpers
// ---------------------------------------------------------------------------

/// `datetime.fromtimestamp(self._clock(), timezone.utc)`.
fn now_datetime(secs: f64) -> chrono::DateTime<chrono::Utc> {
    use chrono::TimeZone;
    let ms = (secs * 1000.0) as i64;
    chrono::Utc
        .timestamp_millis_opt(ms)
        .single()
        .unwrap_or_else(chrono::Utc::now)
}

/// Python `dict.get(key)` returning Option<&Value>, treating Null as absent.
fn get<'a>(m: &'a Map<String, Value>, key: &str) -> Option<&'a Value> {
    match m.get(key) {
        None | Some(Value::Null) => None,
        Some(v) => Some(v),
    }
}

fn get_str(m: &Map<String, Value>, key: &str) -> String {
    get(m, key)
        .and_then(|v| v.as_str().map(|s| s.to_string()))
        .unwrap_or_default()
}

/// `_f(value)` from a map, default 0.0.
fn get_num(m: &Map<String, Value>, key: &str) -> f64 {
    py_f(m.get(key), 0.0)
}

/// `Decimal(str(value))` of a trade field; 0 when missing/unparseable.
fn get_dec(m: &Map<String, Value>, key: &str) -> Decimal {
    match get(m, key) {
        Some(Value::String(s)) => Decimal::from_str_exact(s.trim()).unwrap_or(Decimal::ZERO),
        Some(other) => {
            let s = other.to_string();
            Decimal::from_str_exact(s.trim()).unwrap_or(Decimal::ZERO)
        }
        None => Decimal::ZERO,
    }
}

fn pharoah_number(f: f64) -> Value {
    serde_json::Number::from_f64(f)
        .map(Value::Number)
        .unwrap_or(Value::Null)
}

/// Python `str(Decimal)` / `str(float)` for numeric JSON output.
fn str_of_value(v: &Value) -> String {
    match v {
        Value::Null => String::new(),
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

/// Deterministic client order id (mirrors `build_client_order_id`).
///
/// Python signature: `(strategy, symbol, side, timestamp, stage, revision=None)`.
/// The Rust args use `signal_timestamp`, `stage`, `leg`, `max_length`.
fn build_cid(
    strategy: &str,
    symbol: &str,
    side: &str,
    bar_ms: i64,
    stage: &str,
    revision: Option<std::num::NonZeroI64>,
) -> String {
    // Python: build_client_order_id(..., kind, revision=revision or None)
    let revision = revision.map(|r| r.get().to_string());
    let timestamp = Value::from(bar_ms);
    build_client_order_id(BuildClientOrderIdArgs {
        strategy,
        symbol,
        side,
        signal_timestamp: Some(&timestamp),
        stage,
        leg: None,
        revision: revision.as_deref(),
        max_length: 36,
    })
}

// ---------------------------------------------------------------------------
// Entry / finalize / accounting (parent that the child does not override)
// ---------------------------------------------------------------------------

impl<E: FuturesExchange + 'static> SmaCrossService<E> {
    // `_order_log`
    pub(crate) fn order_log(
        &self,
        trade: &Map<String, Value>,
        decision: &Value,
        context: &Map<String, Value>,
        cfg: &Value,
        requested_entry: &str,
    ) -> Map<String, Value> {
        let mut m = Map::new();
        m.insert(
            "signal_id".into(),
            get(trade, "signal_id").cloned().unwrap_or(Value::Null),
        );
        m.insert(
            "side".into(),
            get(trade, "side").cloned().unwrap_or(Value::Null),
        );
        m.insert(
            "quantity".into(),
            get(trade, "quantity").cloned().unwrap_or(Value::Null),
        );
        m.insert(
            "requested_entry".into(),
            Value::String(requested_entry.to_string()),
        );
        m.insert(
            "actual_avg_entry".into(),
            get(trade, "entry_price").cloned().unwrap_or(Value::Null),
        );
        m.insert(
            "stop_price".into(),
            get(trade, "stop_price").cloned().unwrap_or(Value::Null),
        );
        m.insert(
            "take_profit_price".into(),
            get(trade, "take_profit_price")
                .cloned()
                .unwrap_or(Value::Null),
        );
        m.insert(
            "leverage".into(),
            cfg.get("leverage").cloned().unwrap_or(Value::Null),
        );
        m.insert(
            "margin_mode".into(),
            cfg.get("margin_type").cloned().unwrap_or(Value::Null),
        );
        let notional = get_dec(trade, "entry_price") * get_dec(trade, "quantity");
        // Python: notional / D(cfg["leverage"]); cfg["leverage"] present in normalized config.
        let leverage = d(cfg.get("leverage"), None).unwrap_or(Decimal::ZERO);
        let margin = if leverage != Decimal::ZERO {
            notional / leverage
        } else {
            Decimal::ZERO
        };
        m.insert("notional".into(), Value::String(notional.to_string()));
        m.insert("estimated_margin".into(), Value::String(margin.to_string()));
        let dec = |k: &str| -> Value { decision.get(k).cloned().unwrap_or(Value::Null) };
        m.insert("estimated_fee".into(), dec("estimated_entry_fee"));
        m.insert("estimated_risk".into(), dec("estimated_total_risk"));
        m.insert(
            "wallet_balance".into(),
            context
                .get("wallet_balance")
                .cloned()
                .unwrap_or(Value::Null),
        );
        m.insert("sizing_mode".into(), dec("sizing_mode"));
        m
    }

    // `enter_paper`
    pub async fn enter_paper(
        &self,
        network: &str,
        cfg: &Value,
        state: &mut Map<String, Value>,
        signal: &Value,
        decision: &Value,
        context: &Map<String, Value>,
    ) -> Result<Value, ServiceError> {
        let mode = TRADING_MODE_DRY_RUN;
        let mark_dec = d(context.get("mark_price"), None)
            .map_err(|e| ServiceError::new("ValueError", e.to_string()))?;
        let slip = d(cfg.get("slippage_buffer_pct"), None)
            .map_err(|e| ServiceError::new("ValueError", e.to_string()))?;
        let side = signal
            .get("side")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let raw_fill = if side == "LONG" {
            mark_dec * (Decimal::ONE + slip)
        } else {
            mark_dec * (Decimal::ONE - slip)
        };
        let rules = decisions_rules(decision)?;
        let tick = rules.tick_size.to_string();
        let fill_dec = round_price_to_tick(&raw_fill.to_string(), &tick, "down")
            .map_err(|e| ServiceError::new("ValueError", e.to_string()))?;
        let fill = fill_dec.to_string();
        let now = self.now_ms();
        let signal_bar = Self::signal_bar(signal);
        let side0 = side
            .chars()
            .next()
            .map(|c| c.to_string())
            .unwrap_or_default();
        let trade_id = format!("paper-{signal_bar}-{side0}");
        let quantity = d(decision.get("quantity"), None)
            .map_err(|e| ServiceError::new("ValueError", e.to_string()))?;
        let mut trade = self
            .new_trade(
                network,
                mode,
                signal,
                decision,
                context,
                &trade_id,
                fill.as_str(),
                quantity,
                now,
                &rules,
                cfg,
            )
            .await?;
        trade.insert("paper_last_checked_ms".into(), Value::from(now));
        trade.insert("funding_settled".into(), Value::Array(vec![]));
        trade.insert("funding".into(), Value::String("0".into()));
        {
            let ledger = self.ledger(network)?;
            let l = ledger.lock().unwrap();
            l.upsert_trade(&trade)
                .map_err(|e| ServiceError::new("StoreError", e.to_string()))?;
            let mut fields = Map::new();
            fields.insert("mode".into(), Value::String(mode.to_string()));
            fields.insert("network".into(), Value::String(network.to_string()));
            fields.insert("dry_run".into(), Value::Bool(true));
            let log = self.order_log(&trade, decision, context, cfg, &mark_dec.to_string());
            for (k, v) in log {
                fields.insert(k, v);
            }
            l.event("ORDER", fields);
        }
        self.set_phase(network, mode, state, POSITION_OPEN, {
            let mut e = Map::new();
            e.insert("trade".into(), Value::Object(trade.clone()));
            e
        })?;
        let text = self.entry_text(&trade, mode)?;
        self.notify(text, None).await;
        let mut out = Map::new();
        out.insert("action".into(), Value::String("entered".into()));
        out.insert("trade".into(), Value::Object(trade));
        Ok(Value::Object(out))
    }

    // `_settle_paper_funding`
    pub(crate) async fn settle_paper_funding(
        &self,
        network: &str,
        trade: &mut Map<String, Value>,
        until_ms: i64,
    ) {
        let period: i64 = 8 * 3_600_000;
        let entry_ms = get(trade, "entry_ms").map(py_int).unwrap_or(0);
        let settled: Vec<i64> = trade
            .get("funding_settled")
            .and_then(|v| v.as_array())
            .map(|arr| arr.iter().map(py_int).collect())
            .unwrap_or_default();
        let settled_set: HashSet<i64> = settled.iter().copied().collect();
        let first = (entry_ms / period + 1) * period;
        let mut due: Vec<i64> = Vec::new();
        let mut t = first;
        while t <= until_ms {
            if !settled_set.contains(&t) {
                due.push(t);
            }
            t += period;
        }
        if due.is_empty() {
            return;
        }
        let (_, market) = symbol_pair(Some(&Value::Object(trade.clone())));
        let rate = match self.premium_index(network, &market).await {
            Ok(m) => py_f(m.get("funding_rate"), 0.0),
            // Python: ignored on purpose (premium index failure).
            Err(_) => return,
        };
        let notional = get_num(trade, "entry_price") * get_num(trade, "quantity");
        let side = get_str(trade, "side");
        let sign = if side == "LONG" { -1.0 } else { 1.0 };
        let funding = get_num(trade, "funding") + sign * notional * rate * due.len() as f64;
        trade.insert("funding".into(), Value::String(funding.to_string()));
        let mut all = settled;
        all.extend(due);
        trade.insert(
            "funding_settled".into(),
            Value::Array(all.into_iter().map(Value::from).collect()),
        );
        trade.insert("funding_estimated".into(), Value::Bool(true));
    }

    // `finalize_paper`
    pub async fn finalize_paper(
        &self,
        network: &str,
        cfg: &Value,
        state: &mut Map<String, Value>,
        exit_price: &str,
        exit_reason: &str,
        exit_ms: i64,
    ) -> Result<Value, ServiceError> {
        let mode = TRADING_MODE_DRY_RUN;
        let trade = state
            .get("trade")
            .and_then(|v| v.as_object().cloned())
            .ok_or_else(|| ServiceError::new("ValueError", "finalize_paper: no trade"))?;
        // _f(trade.get("fee_rate"), cfg["estimated_taker_fee_rate"])
        let fee_rate = match get(&trade, "fee_rate") {
            Some(Value::String(s)) => s
                .trim()
                .parse::<f64>()
                .unwrap_or_else(|_| py_f(cfg.get("estimated_taker_fee_rate"), 0.0)),
            Some(other) => py_f(Some(other), py_f(cfg.get("estimated_taker_fee_rate"), 0.0)),
            None => py_f(cfg.get("estimated_taker_fee_rate"), 0.0),
        };
        let qty = get_num(&trade, "quantity");
        let entry = get_num(&trade, "entry_price");
        let exit_f = exit_price.trim().parse::<f64>().unwrap_or(0.0);
        let commission = (entry + exit_f) * qty * fee_rate;
        let funding = get_num(&trade, "funding");
        let result = self.close_trade(
            &trade,
            exit_price,
            exit_reason,
            exit_ms,
            commission,
            funding,
            None,
        );
        self.set_phase(network, mode, state, EXIT_FILLED, Map::new())?;
        {
            let ledger = self.ledger(network)?;
            let l = ledger.lock().unwrap();
            l.upsert_trade(&result)
                .map_err(|e| ServiceError::new("StoreError", e.to_string()))?;
            let mut fields = Map::new();
            fields.insert("mode".into(), Value::String(mode.to_string()));
            fields.insert("network".into(), Value::String(network.to_string()));
            fields.insert("dry_run".into(), Value::Bool(true));
            let log = self.exit_log(network, mode, cfg, &result)?;
            for (k, v) in log {
                fields.insert(k, v);
            }
            l.event("EXIT", fields);
        }
        self.on_trade_closed(network, mode, state, &result);
        self.set_phase(network, mode, state, TRADE_RECORDED, {
            let mut e = Map::new();
            e.insert("trade".into(), Value::Null);
            e
        })?;
        self.set_phase(network, mode, state, IDLE, Map::new())?;
        let text = self.exit_text(&result, mode)?;
        self.notify(text, None).await;
        let mut out = Map::new();
        out.insert("action".into(), Value::String("exited".into()));
        out.insert("trade".into(), Value::Object(result));
        Ok(Value::Object(out))
    }

    // `_close_trade`
    pub(crate) fn close_trade(
        &self,
        trade: &Map<String, Value>,
        exit_price: &str,
        exit_reason: &str,
        exit_ms: i64,
        commission: f64,
        funding: f64,
        extra: Option<Map<String, Value>>,
    ) -> Map<String, Value> {
        let long = get_str(trade, "side") == "LONG";
        let qty = get_num(trade, "quantity");
        let entry = get_num(trade, "entry_price");
        let exit_f = exit_price.trim().parse::<f64>().unwrap_or(0.0);
        let gross = (exit_f - entry) * qty * if long { 1.0 } else { -1.0 };
        let net = gross - commission + funding;
        let risk = get_num(trade, "initial_risk");
        let mut result = trade.clone();
        result.insert("status".into(), Value::String("CLOSED".into()));
        result.insert("exit_time".into(), Value::String(iso_ms(exit_ms)));
        result.insert("exit_price".into(), Value::String(exit_price.to_string()));
        result.insert("exit_reason".into(), Value::String(exit_reason.to_string()));
        result.insert("gross_pnl".into(), Value::String(py_float_repr(gross)));
        result.insert(
            "commission".into(),
            Value::String(py_float_repr(commission)),
        );
        result.insert("funding".into(), Value::String(py_float_repr(funding)));
        result.insert("net_pnl".into(), Value::String(py_float_repr(net)));
        // Python: str(net / (entry * qty) * 100 if entry and qty else 0)
        let pnl_pct = if entry != 0.0 && qty != 0.0 {
            py_float_repr(net / (entry * qty) * 100.0)
        } else {
            "0".to_string()
        };
        result.insert("pnl_pct".into(), Value::String(pnl_pct));
        result.insert(
            "r_multiple".into(),
            Value::String(if risk > 0.0 {
                py_float_repr(net / risk)
            } else {
                String::new()
            }),
        );
        result.insert(
            "result".into(),
            Value::String(if net > 0.0 {
                "WIN".into()
            } else if net < 0.0 {
                "LOSS".into()
            } else {
                "BREAKEVEN".into()
            }),
        );
        result.insert("extra".into(), Value::Object(extra.unwrap_or_default()));
        result
    }

    // `_exit_log`
    pub(crate) fn exit_log(
        &self,
        network: &str,
        mode: &str,
        cfg: &Value,
        trade: &Map<String, Value>,
    ) -> Result<Map<String, Value>, ServiceError> {
        let tz = cfg
            .get("day_timezone")
            .and_then(|v| v.as_str())
            .unwrap_or("UTC");
        let (start, end, _key) = trading_day_bounds(now_datetime((self.deps.clock)()), tz);
        let today: Vec<Map<String, Value>> = {
            let ledger = self.ledger(network)?;
            let l = ledger.lock().unwrap();
            l.trades(Some(mode), Some(network), Some(start), Some(end), None)
                .map_err(|e| ServiceError::new("StoreError", e.to_string()))?
        };
        let mut streak = 0i64;
        let closed: Vec<&Map<String, Value>> = today
            .iter()
            .filter(|t| get_str(t, "status") == "CLOSED")
            .collect();
        for row in closed.iter().rev() {
            if get_num(row, "net_pnl") < 0.0 {
                streak += 1;
            } else {
                break;
            }
        }
        let mut m = Map::new();
        for (src, dst) in [
            ("trade_id", "trade_id"),
            ("exit_reason", "exit_reason"),
            ("entry_price", "entry_price"),
            ("exit_price", "exit_price"),
            ("gross_pnl", "gross_pnl"),
            ("commission", "commission"),
            ("funding", "funding_fee"),
            ("net_pnl", "net_pnl"),
            ("pnl_pct", "pnl_pct"),
            ("r_multiple", "R_multiple"),
        ] {
            m.insert(dst.into(), get(trade, src).cloned().unwrap_or(Value::Null));
        }
        m.insert("consecutive_losses".into(), Value::from(streak));
        m.insert("daily_trade_count".into(), Value::from(today.len() as i64));
        Ok(m)
    }

    // `enter_live`
    pub async fn enter_live(
        &self,
        network: &str,
        cfg: &Value,
        state: &mut Map<String, Value>,
        signal: &Value,
        decision: &Value,
        context: &Map<String, Value>,
    ) -> Result<Value, ServiceError> {
        let mode = TRADING_MODE_LIVE;
        let ledger = self.ledger(network)?;
        let (order_gateway, _algo) = self.gateway(network).await?;
        let (ccxt_symbol, _market) = symbol_pair(Some(signal));
        let side = get_str(signal_as_map(signal), "side");
        let client_order_id = build_cid(
            self.client_id(),
            &ccxt_symbol,
            &side.to_lowercase(),
            Self::signal_bar(signal),
            "entry",
            None,
        );
        let mut pending = Map::new();
        pending.insert(
            "client_order_id".into(),
            Value::String(client_order_id.clone()),
        );
        pending.insert("signal".into(), signal.clone());
        pending.insert(
            "decision_qty".into(),
            Value::String(str_of_value(
                decision.get("quantity").unwrap_or(&Value::Null),
            )),
        );
        self.set_phase(network, mode, state, ENTRY_SUBMITTED, {
            let mut e = Map::new();
            e.insert("pending_entry".into(), Value::Object(pending));
            e
        })?;
        let qty = py_f(decision.get("quantity"), 0.0);
        let submit = order_gateway
            .submit_entry(crate::safety::order_gateway::EntryRequest {
                strategy: self.client_id().to_string(),
                symbol: ccxt_symbol.clone(),
                side: side.to_lowercase(),
                signal_timestamp: Value::from(Self::signal_bar(signal)),
                qty,
                params: None,
                daily_loss_exempt: false,
            })
            .await?;
        {
            let l = ledger.lock().unwrap();
            let mut fields = Map::new();
            fields.insert("mode".into(), Value::String(mode.into()));
            fields.insert("network".into(), Value::String(network.into()));
            fields.insert(
                "signal_id".into(),
                get(signal_as_map(signal), "signal_id")
                    .cloned()
                    .unwrap_or(Value::Null),
            );
            fields.insert(
                "client_order_id".into(),
                Value::String(submit.client_order_id.clone()),
            );
            fields.insert("state".into(), Value::String(submit.state.clone()));
            fields.insert(
                "error".into(),
                submit
                    .error
                    .clone()
                    .map(Value::String)
                    .unwrap_or(Value::Null),
            );
            l.event("ENTRY_RESULT", fields);
        }
        let blocked_states = [
            "BLOCKED".to_string(),
            OrderState::Failed.as_str().to_string(),
            OrderState::Canceled.as_str().to_string(),
        ];
        if blocked_states.iter().any(|s| s == &submit.state) {
            let msg = submit.error.clone().unwrap_or_default();
            self.set_phase(network, mode, state, IDLE, {
                let mut e = Map::new();
                e.insert("pending_entry".into(), Value::Null);
                e.insert(
                    "last_error".into(),
                    Value::String(format!("ENTRY_REJECTED: {msg}")),
                );
                e
            })?;
            self.notify(format!("⚠️ {} 진입 실패: {msg}", self.label()), None)
                .await;
            let mut out = Map::new();
            out.insert("action".into(), Value::String("rejected".into()));
            out.insert("reason".into(), Value::String(msg));
            return Ok(Value::Object(out));
        }
        let position: Option<Value> = match submit.position.clone() {
            Some(p) => Some(p),
            None => match self.fetch_position(network, &ccxt_symbol).await {
                Ok(p) => p,
                // Python: position=None, POSITION_FETCH_FAILED event.
                Err(exc) => {
                    let l = ledger.lock().unwrap();
                    let mut fields = Map::new();
                    fields.insert("network".into(), Value::String(network.into()));
                    fields.insert("error".into(), Value::String(exc.to_string()));
                    l.event("POSITION_FETCH_FAILED", fields);
                    None
                }
            },
        };
        let pos_side = position_side(position.as_ref());
        if position.is_none() || pos_side != side {
            let mut pending = Map::new();
            pending.insert(
                "client_order_id".into(),
                Value::String(submit.client_order_id.clone()),
            );
            pending.insert("signal".into(), signal.clone());
            pending.insert(
                "decision_qty".into(),
                Value::String(str_of_value(
                    decision.get("quantity").unwrap_or(&Value::Null),
                )),
            );
            let last_err = format!(
                "ENTRY_UNCONFIRMED: {} {}",
                submit.state,
                submit.error.clone().unwrap_or_default()
            );
            self.set_phase(network, mode, state, POSITION_RECONCILIATION, {
                let mut e = Map::new();
                e.insert("reconciled".into(), Value::Bool(false));
                e.insert("pending_entry".into(), Value::Object(pending));
                e.insert("last_error".into(), Value::String(last_err));
                e
            })?;
            let mut out = Map::new();
            out.insert("action".into(), Value::String("reconcile".into()));
            out.insert("reason".into(), Value::String(submit.state.clone()));
            return Ok(Value::Object(out));
        }
        let position = position.unwrap();
        let qty_real = position_qty(Some(&position));
        let mut avg = position_entry(Some(&position));
        if avg == 0.0 {
            avg = py_f(submit.order.as_ref().and_then(|o| o.get("average")), 0.0);
        }
        let rules = decisions_rules(decision)?;
        let dec_qty = d(decision.get("quantity"), None)
            .map_err(|e| ServiceError::new("ValueError", e.to_string()))?;
        let mut trade = self
            .new_trade(
                network,
                mode,
                signal,
                decision,
                context,
                &submit.client_order_id,
                &avg.to_string(),
                dec_qty,
                self.now_ms(),
                &rules,
                cfg,
            )
            .await?;
        if Decimal::from_f64_retain(qty_real).unwrap_or(Decimal::ZERO) < dec_qty {
            trade.insert(
                "rule_violation".into(),
                Value::String(format!("PARTIAL_FILL: {qty_real} of {dec_qty}")),
            );
        }
        trade.insert(
            "entry_client_order_id".into(),
            Value::String(submit.client_order_id.clone()),
        );
        {
            let l = ledger.lock().unwrap();
            l.upsert_trade(&trade)
                .map_err(|e| ServiceError::new("StoreError", e.to_string()))?;
        }
        self.set_phase(network, mode, state, ENTRY_FILLED, {
            let mut e = Map::new();
            e.insert("trade".into(), Value::Object(trade.clone()));
            e.insert("pending_entry".into(), Value::Null);
            e
        })?;
        {
            let l = ledger.lock().unwrap();
            let mut fields = Map::new();
            fields.insert("mode".into(), Value::String(mode.into()));
            fields.insert("network".into(), Value::String(network.into()));
            let mark_str = context
                .get("mark_price")
                .map(str_of_value)
                .unwrap_or_default();
            let log = self.order_log(&trade, decision, context, cfg, &mark_str);
            for (k, v) in log {
                fields.insert(k, v);
            }
            l.event("ORDER", fields);
        }
        let text = self.entry_text(&trade, mode)?;
        self.notify(text, None).await;
        self.attach_protection(network, cfg, state).await
    }

    // `_algo_client_id`
    pub(crate) fn algo_client_id(&self, trade: &Map<String, Value>, kind: &str) -> String {
        let rev = trade
            .get(format!("{kind}_revision").as_str())
            .map(py_int)
            .unwrap_or(0);
        let (_, market) = symbol_pair(Some(&Value::Object(trade.clone())));
        build_cid(
            self.client_id(),
            &market,
            &get_str(trade, "side"),
            get(trade, "signal_bar_ms").map(py_int).unwrap_or(0),
            kind,
            std::num::NonZeroI64::new(rev),
        )
    }

    // `place_protection`
    pub async fn place_protection(
        &self,
        network: &str,
        cfg: &Value,
        trade: &mut Map<String, Value>,
        kind: &str,
    ) -> Result<String, ServiceError> {
        let (_, algo) = self.gateway(network).await?;
        let mut client_id = self.algo_client_id(trade, kind);
        let spec = self.protection_spec(cfg, trade, kind)?;
        let close_side = if get_str(trade, "side") == "LONG" {
            "SELL"
        } else {
            "BUY"
        };
        let delays: Vec<f64> = cfg
            .get("protection_retry_delays_seconds")
            .and_then(|v| v.as_array())
            .map(|arr| arr.iter().map(|v| py_f(Some(v), 0.0)).collect())
            .unwrap_or_default();
        let attempts = py_int(cfg.get("protection_retry_attempts").unwrap_or(&Value::Null));
        let mut last_error = String::new();
        for attempt in 0..attempts.max(0) {
            let lookup = algo.fetch_by_client_id(&client_id).await;
            match lookup.status {
                AlgoLookupStatus::Found => {
                    let status = lookup
                        .order
                        .as_ref()
                        .and_then(|o| o.get("status"))
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_uppercase();
                    if OPEN_ALGO_STATUSES.iter().any(|s| *s == status) {
                        trade.insert(format!("{kind}_client_algo_id"), Value::String(client_id));
                        return Ok("OK".into());
                    }
                    let rev = trade
                        .get(format!("{kind}_revision").as_str())
                        .map(py_int)
                        .unwrap_or(0);
                    trade.insert(format!("{kind}_revision"), Value::from(rev + 1));
                    client_id = self.algo_client_id(trade, kind);
                    continue;
                }
                AlgoLookupStatus::Unknown => {
                    last_error = lookup
                        .error
                        .clone()
                        .unwrap_or_else(|| "lookup unknown".into());
                }
                AlgoLookupStatus::NotFound => {
                    let trigger = spec.get("trigger").cloned().unwrap_or(Value::Null);
                    let quantity = spec.get("quantity").cloned();
                    let close_position = spec
                        .get("close_position")
                        .and_then(|v| v.as_bool())
                        .unwrap_or(true);
                    let working_type = spec
                        .get("working_type")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string();
                    let order_type = spec
                        .get("order_type")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string();
                    let (ccxt_symbol, _) = symbol_pair(Some(&Value::Object(trade.clone())));
                    let res = algo
                        .create_conditional_order(
                            &ccxt_symbol,
                            &order_type,
                            close_side,
                            quantity,
                            trigger,
                            &client_id,
                            true,
                            &working_type,
                            false, // price_protect: Python default
                            None,
                            close_position,
                            None,
                        )
                        .await;
                    match res {
                        Err(exc) => {
                            last_error = format!("{}: {}", exc.class, exc.message);
                            let lower = last_error.to_lowercase();
                            if last_error.contains("-2021") || lower.contains("immediately trigger")
                            {
                                return Ok("BREACHED".into());
                            }
                            let l = self.ledger(network)?;
                            let ledger = l.lock().unwrap();
                            let mut fields = Map::new();
                            fields.insert("network".into(), Value::String(network.into()));
                            fields.insert("kind".into(), Value::String(kind.into()));
                            fields.insert("attempt".into(), Value::from(attempt + 1));
                            fields.insert("error".into(), Value::String(last_error.clone()));
                            ledger.event("PROTECTION_RETRY", fields);
                        }
                        Ok(_) => {
                            let confirm = algo.fetch_by_client_id(&client_id).await;
                            if matches!(confirm.status, AlgoLookupStatus::Found) {
                                trade.insert(
                                    format!("{kind}_client_algo_id"),
                                    Value::String(client_id),
                                );
                                return Ok("OK".into());
                            }
                            last_error = confirm
                                .error
                                .unwrap_or_else(|| "created but not confirmed".into());
                        }
                    }
                }
            }
            if (attempt as usize) < delays.len() {
                (self.deps.sleep)(delays[attempt as usize]).await;
            }
        }
        Ok(format!("FAILED:{last_error}"))
    }

    // `failsafe_close`
    pub async fn failsafe_close(
        &self,
        network: &str,
        cfg: &Value,
        state: &mut Map<String, Value>,
        reason: &str,
    ) -> Result<Value, ServiceError> {
        let mode = TRADING_MODE_LIVE;
        let mut trade = state
            .get("trade")
            .and_then(|v| v.as_object().cloned())
            .ok_or_else(|| ServiceError::new("ValueError", "failsafe_close: no trade"))?;
        self.set_phase(network, mode, state, PROTECTION_FAILED, {
            let mut e = Map::new();
            e.insert("last_error".into(), Value::String(reason.into()));
            e
        })?;
        {
            let ledger = self.ledger(network)?;
            let l = ledger.lock().unwrap();
            let mut fields = Map::new();
            fields.insert("network".into(), Value::String(network.into()));
            fields.insert(
                "trade_id".into(),
                get(&trade, "trade_id").cloned().unwrap_or(Value::Null),
            );
            fields.insert("reason".into(), Value::String(reason.into()));
            l.event("FAILSAFE", fields);
        }
        let (ccxt_symbol, market) = symbol_pair(Some(&Value::Object(trade.clone())));
        let (position, orders) = {
            let p = self.fetch_position(network, &ccxt_symbol).await;
            let o = self.open_algo_orders(network, Some(&market)).await;
            match (p, o) {
                (Ok(p), Ok(o)) => (p, o),
                (Err(exc), _) | (_, Err(exc)) => {
                    self.set_phase(network, mode, state, ERROR_RECOVERY, {
                        let mut e = Map::new();
                        e.insert(
                            "last_error".into(),
                            Value::String(format!("{reason}; state unknown: {exc}")),
                        );
                        e
                    })?;
                    self.notify(
                        format!(
                            "🚨 {}: 보호주문 실패 + 거래소 상태 확인 불가 ({}). 다음 주기에 재시도합니다.",
                            self.label(),
                            exc
                        ),
                        None,
                    )
                    .await;
                    let mut out = Map::new();
                    out.insert("action".into(), Value::String("error".into()));
                    out.insert("reason".into(), Value::String(reason.into()));
                    return Ok(Value::Object(out));
                }
            }
        };
        if position.is_none() {
            return self
                .finalize_live(network, cfg, state, Some("FAILSAFE"))
                .await;
        }
        let close_side = if get_str(&trade, "side") == "LONG" {
            "sell"
        } else {
            "buy"
        };
        let has_stop = orders.iter().any(|o| {
            let is_stop = matches!(
                o.get("orderType").and_then(|v| v.as_str()).unwrap_or(""),
                "STOP_MARKET" | "STOP"
            );
            let same_side = o
                .get("side")
                .and_then(|v| v.as_str())
                .map(|s| s.to_lowercase())
                == Some(close_side.to_string());
            is_stop && same_side
        });
        if has_stop {
            self.set_phase(network, mode, state, POSITION_OPEN, {
                let mut e = Map::new();
                e.insert("last_error".into(), Value::String("".into()));
                e
            })?;
            let ledger = self.ledger(network)?;
            let l = ledger.lock().unwrap();
            let mut fields = Map::new();
            fields.insert("network".into(), Value::String(network.into()));
            fields.insert(
                "reason".into(),
                Value::String("stop order found on exchange".into()),
            );
            l.event("FAILSAFE_ABORTED", fields);
            let mut out = Map::new();
            out.insert("action".into(), Value::String("managed".into()));
            out.insert("reason".into(), Value::String("STOP_FOUND".into()));
            return Ok(Value::Object(out));
        }
        let (order_gateway, _) = self.gateway(network).await?;
        let position_ref = position.as_ref().unwrap();
        let qty = position_qty(Some(position_ref));
        let side_lower = get_str(&trade, "side").to_lowercase();
        let sig = get(&trade, "signal_bar_ms").cloned().unwrap_or(Value::Null);
        let result = order_gateway
            .submit_reduce_only_close(ReduceOnlyCloseRequest {
                strategy: self.client_id().to_string(),
                symbol: ccxt_symbol.clone(),
                position_side: side_lower,
                position_signature: sig,
                qty,
                reason: "emergency_failsafe".into(),
                params: None,
                order_intent: None,
                order_purpose: None,
                order_type: "market".into(),
                price: None,
                leg: None,
            })
            .await?;
        trade.insert("failsafe".into(), Value::Bool(true));
        state.insert("trade".into(), Value::Object(trade.clone()));
        {
            let ledger = self.ledger(network)?;
            let l = ledger.lock().unwrap();
            let mut fields = Map::new();
            fields.insert("network".into(), Value::String(network.into()));
            fields.insert("state".into(), Value::String(result.state.clone()));
            fields.insert(
                "error".into(),
                result
                    .error
                    .clone()
                    .map(Value::String)
                    .unwrap_or(Value::Null),
            );
            l.event("FAILSAFE_CLOSE", fields);
        }
        self.notify(
            format!(
                "🚨 {}: 손절 주문을 걸지 못해 포지션을 시장가로 정리했습니다 ({}) → {}",
                self.label(),
                reason,
                result.state
            ),
            None,
        )
        .await;
        if result.state == OrderState::Closed.as_str() {
            return self
                .finalize_live(network, cfg, state, Some("FAILSAFE"))
                .await;
        }
        self.set_phase(network, mode, state, ERROR_RECOVERY, {
            let mut e = Map::new();
            e.insert("trade".into(), Value::Object(trade));
            e.insert(
                "last_error".into(),
                Value::String(format!("FAILSAFE_CLOSE_{}", result.state)),
            );
            e
        })?;
        let mut out = Map::new();
        out.insert("action".into(), Value::String("error".into()));
        out.insert("reason".into(), Value::String(result.state));
        Ok(Value::Object(out))
    }

    // `finalize_live`
    pub async fn finalize_live(
        &self,
        network: &str,
        cfg: &Value,
        state: &mut Map<String, Value>,
        forced_reason: Option<&str>,
    ) -> Result<Value, ServiceError> {
        let mode = TRADING_MODE_LIVE;
        let trade = state
            .get("trade")
            .and_then(|v| v.as_object().cloned())
            .ok_or_else(|| ServiceError::new("ValueError", "finalize_live: no trade"))?;
        self.set_phase(network, mode, state, EXIT_FILLED, Map::new())?;
        let (_, algo) = self.gateway(network).await?;
        let mut reason = forced_reason.map(|s| s.to_string());
        for kind in self.variant.protection_kinds() {
            let key = format!("{kind}_client_algo_id");
            let Some(client_id) = get(&trade, &key)
                .and_then(|v| v.as_str())
                .map(|s| s.to_string())
            else {
                continue;
            };
            let lookup = algo.fetch_by_client_id(&client_id).await;
            let status = lookup
                .order
                .as_ref()
                .and_then(|o| o.get("status"))
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_uppercase();
            if matches!(lookup.status, AlgoLookupStatus::Found)
                && OPEN_ALGO_STATUSES.iter().any(|s| *s == status)
            {
                if let Err(exc) = self.cancel_algo(network, &client_id).await {
                    let ledger = self.ledger(network)?;
                    let l = ledger.lock().unwrap();
                    let mut fields = Map::new();
                    fields.insert("network".into(), Value::String(network.into()));
                    fields.insert("client_algo_id".into(), Value::String(client_id));
                    fields.insert("error".into(), Value::String(exc.to_string()));
                    l.event("CANCEL_FAILED", fields);
                }
            } else if matches!(lookup.status, AlgoLookupStatus::Found)
                && reason.is_none()
                && matches!(status.as_str(), "TRIGGERED" | "FINISHED" | "FILLED")
            {
                reason = Some(
                    self.variant
                        .exit_reason_for_kind(kind)
                        .map(|s| s.to_string())
                        .unwrap_or_else(|| kind.to_uppercase()),
                );
            }
        }
        let fills = self.exit_fills(network, &trade).await?;
        let failsafe = get(&trade, "failsafe")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let exit_reason = reason.unwrap_or_else(|| {
            if failsafe {
                "FAILSAFE".into()
            } else {
                "MANUAL".into()
            }
        });
        let mut extra = Map::new();
        extra.insert(
            "pnl_source".into(),
            fills.get("source").cloned().unwrap_or(Value::Null),
        );
        let exit_price = str_of_value(fills.get("exit_price").unwrap_or(&Value::Null));
        let exit_ms = fills.get("exit_ms").and_then(|v| v.as_i64()).unwrap_or(0);
        let commission = fills
            .get("commission")
            .and_then(|v| v.as_f64())
            .unwrap_or(0.0);
        let funding = fills.get("funding").and_then(|v| v.as_f64()).unwrap_or(0.0);
        let result = self.close_trade(
            &trade,
            &exit_price,
            &exit_reason,
            exit_ms,
            commission,
            funding,
            Some(extra),
        );
        {
            let ledger = self.ledger(network)?;
            let l = ledger.lock().unwrap();
            l.upsert_trade(&result)
                .map_err(|e| ServiceError::new("StoreError", e.to_string()))?;
            let mut fields = Map::new();
            fields.insert("mode".into(), Value::String(mode.into()));
            fields.insert("network".into(), Value::String(network.into()));
            let log = self.exit_log(network, mode, cfg, &result)?;
            for (k, v) in log {
                fields.insert(k, v);
            }
            l.event("EXIT", fields);
        }
        let (ccxt_symbol, _market) = symbol_pair(Some(&Value::Object(trade.clone())));
        let entry_id = get(&trade, "entry_client_order_id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        let store = self.store(network)?;
        {
            let mut st = store.lock().unwrap();
            if let Some(id) = entry_id.as_ref() {
                if st.get(id).ok().flatten().is_some() {
                    st.transition(id, OrderState::Closed.as_str(), Map::new())
                        .map_err(|e| ServiceError::new("StoreError", e.to_string()))?;
                    st.release_entry_lease(id, OrderState::Closed.as_str())
                        .map_err(|e| ServiceError::new("StoreError", e.to_string()))?;
                }
            }
            if let Ok(records) = st.active_for_symbol(&ccxt_symbol) {
                let intents = [
                    OrderIntent::Close.as_str(),
                    OrderIntent::EmergencyClose.as_str(),
                    OrderIntent::ManualClose.as_str(),
                ];
                for record in records {
                    if intents.contains(&record.order_intent.as_str()) {
                        st.transition(
                            &record.client_order_id,
                            OrderState::Closed.as_str(),
                            Map::new(),
                        )
                        .map_err(|e| ServiceError::new("StoreError", e.to_string()))?;
                        st.release_entry_lease(
                            &record.client_order_id,
                            OrderState::Closed.as_str(),
                        )
                        .map_err(|e| ServiceError::new("StoreError", e.to_string()))?;
                    }
                }
            }
        }
        self.on_trade_closed(network, mode, state, &result);
        self.set_phase(network, mode, state, TRADE_RECORDED, {
            let mut e = Map::new();
            e.insert("trade".into(), Value::Null);
            e.insert("last_error".into(), Value::String("".into()));
            e
        })?;
        self.set_phase(network, mode, state, IDLE, Map::new())?;
        let text = self.exit_text(&result, mode)?;
        self.notify(text, None).await;
        let mut out = Map::new();
        out.insert("action".into(), Value::String("exited".into()));
        out.insert("trade".into(), Value::Object(result));
        Ok(Value::Object(out))
    }

    // `_exit_fills`
    pub(crate) async fn exit_fills(
        &self,
        network: &str,
        trade: &Map<String, Value>,
    ) -> Result<Map<String, Value>, ServiceError> {
        let exchange = self.exchange(network).await?;
        let (_, market) = symbol_pair(Some(&Value::Object(trade.clone())));
        let start = get(trade, "entry_ms").map(py_int).unwrap_or(0) - 5_000;
        let now = self.now_ms();
        let long = get_str(trade, "side") == "LONG";
        let close_side = if long { "SELL" } else { "BUY" };
        let exit_price;
        let exit_ms;
        let commission;
        let source: &str;
        let user_trades = exchange.user_trades_raw(&market, Some(start), 1000).await;
        let computed = user_trades.and_then(|payload| {
            let fills: Vec<Value> = payload.as_array().cloned().unwrap_or_default();
            let commission: f64 = fills
                .iter()
                .filter(|f| {
                    f.get("commissionAsset")
                        .and_then(|v| v.as_str())
                        .unwrap_or("USDT")
                        == "USDT"
                })
                .map(|f| py_f(f.get("commission"), 0.0))
                .sum();
            let closing: Vec<&Value> = fills
                .iter()
                .filter(|f| {
                    f.get("side")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_uppercase()
                        == close_side
                })
                .collect();
            let qty: f64 = closing.iter().map(|f| py_f(f.get("qty"), 0.0)).sum();
            if qty <= 0.0 {
                return Err(ExchangeError::new("ValueError", "no closing fills"));
            }
            let weighted: f64 = closing
                .iter()
                .map(|f| py_f(f.get("price"), 0.0) * py_f(f.get("qty"), 0.0))
                .sum();
            let ep = weighted / qty;
            let em = closing
                .iter()
                .map(|f| py_f(f.get("time"), 0.0) as i64)
                .max()
                .unwrap_or(0);
            Ok::<(f64, i64, f64), ExchangeError>((ep, em, commission))
        });
        match computed {
            Ok((ep, em, cm)) => {
                exit_price = ep.to_string();
                exit_ms = em;
                commission = cm;
                source = "exchange";
            }
            Err(exc) => {
                eprintln!("BTC pullback exit fills unavailable: {}", exc);
                let fallback = match self.premium_index(network, &market).await {
                    Ok(m) => py_f(m.get("mark_price"), 0.0),
                    // Python: ignored on purpose (premium index failure).
                    Err(_) => get_num(trade, "entry_price"),
                };
                let fee_rate = {
                    let v = get(trade, "fee_rate");
                    if v.is_some() {
                        py_f(v, 0.0005)
                    } else {
                        0.0005
                    }
                };
                commission = (get_num(trade, "entry_price") + fallback)
                    * get_num(trade, "quantity")
                    * fee_rate;
                exit_price = fallback.to_string();
                exit_ms = now;
                source = "estimated";
            }
        }
        let mut funding = 0.0;
        match exchange
            .income_raw(&market, Some("FUNDING_FEE"), Some(start), 1000)
            .await
        {
            Ok(payload) => {
                let rows = payload.as_array().cloned().unwrap_or_default();
                funding = rows.iter().map(|r| py_f(r.get("income"), 0.0)).sum();
            }
            Err(exc) => {
                eprintln!("BTC pullback funding income unavailable: {}", exc);
            }
        }
        let mut m = Map::new();
        m.insert("exit_price".into(), Value::String(exit_price));
        m.insert("exit_ms".into(), Value::from(exit_ms));
        m.insert("commission".into(), pharoah_number(commission));
        m.insert("funding".into(), pharoah_number(funding));
        m.insert("source".into(), Value::String(source.into()));
        Ok(m)
    }

    // `reconcile_live`
    pub async fn reconcile_live(
        &self,
        network: &str,
        cfg: &Value,
        state: &mut Map<String, Value>,
    ) -> Result<(), ServiceError> {
        let mode = TRADING_MODE_LIVE;
        self.set_phase(network, mode, state, POSITION_RECONCILIATION, Map::new())?;
        let pending = state
            .get("pending_entry")
            .and_then(|v| v.as_object().cloned());
        if let Some(pending) = pending.as_ref() {
            let (order_gateway, _) = self.gateway(network).await?;
            let cid = pending
                .get("client_order_id")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let store = self.store(network)?;
            let exists = {
                let st = store.lock().unwrap();
                st.get(cid).ok().flatten()
            };
            if let Some(record) = exists {
                if let Err(exc) = order_gateway.recover(&record, false).await {
                    eprintln!("BTC pullback recover failed: {}", exc);
                }
            }
        }
        let trade = state.get("trade").and_then(|v| v.as_object().cloned());
        let all_orders = match self.open_algo_orders(network, None).await {
            Ok(o) => o,
            Err(exc) => {
                {
                    let ledger = self.ledger(network)?;
                    let l = ledger.lock().unwrap();
                    let mut fields = Map::new();
                    fields.insert("network".into(), Value::String(network.into()));
                    fields.insert("error".into(), Value::String(exc.to_string()));
                    l.event("RECONCILE_FAILED", fields);
                }
                state.insert(
                    "last_error".into(),
                    Value::String(format!("RECONCILE_FAILED: {exc}")),
                );
                self.save_state(network, mode, state)?;
                return Ok(());
            }
        };
        let (ccxt_symbol, market) = if let Some(t) = trade.as_ref() {
            symbol_pair(Some(&Value::Object(t.clone())))
        } else if let Some(p) = pending.as_ref() {
            symbol_pair(p.get("signal"))
        } else {
            let own: Vec<&Value> = all_orders
                .iter()
                .filter(|o| {
                    o.get("clientAlgoId")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .starts_with(self.client_id())
                })
                .collect();
            if let Some(first) = own.first() {
                let mid = first
                    .get("symbol")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_uppercase();
                let mut m = Map::new();
                m.insert("market_id".into(), Value::String(mid));
                symbol_pair(Some(&Value::Object(m)))
            } else {
                (CCXT_SYMBOL.into(), MARKET_ID.into())
            }
        };
        let position = match self.fetch_position(network, &ccxt_symbol).await {
            Ok(p) => p,
            Err(exc) => {
                {
                    let ledger = self.ledger(network)?;
                    let l = ledger.lock().unwrap();
                    let mut fields = Map::new();
                    fields.insert("network".into(), Value::String(network.into()));
                    fields.insert("error".into(), Value::String(exc.to_string()));
                    l.event("RECONCILE_FAILED", fields);
                }
                state.insert(
                    "last_error".into(),
                    Value::String(format!("RECONCILE_FAILED: {exc}")),
                );
                self.save_state(network, mode, state)?;
                return Ok(());
            }
        };
        let orders: Vec<Value> = all_orders
            .iter()
            .filter(|o| {
                o.get("symbol")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_uppercase()
                    == market
            })
            .cloned()
            .collect();
        let exchange = self.exchange(network).await?;
        let risk: Map<String, Value> = match exchange.position_risk_v2_raw(&market).await {
            Ok(payload) => {
                let row = payload
                    .as_array()
                    .and_then(|arr| {
                        arr.iter()
                            .find(|r| {
                                r.get("symbol").and_then(|v| v.as_str()) == Some(market.as_str())
                            })
                            .cloned()
                    })
                    .unwrap_or_else(|| json!({}));
                let mut m = Map::new();
                m.insert(
                    "leverage".into(),
                    row.get("leverage").cloned().unwrap_or(Value::Null),
                );
                m.insert(
                    "margin_type".into(),
                    row.get("marginType").cloned().unwrap_or(Value::Null),
                );
                m
            }
            Err(exc) => {
                let mut m = Map::new();
                m.insert("error".into(), Value::String(exc.to_string()));
                m
            }
        };
        {
            let ledger = self.ledger(network)?;
            let l = ledger.lock().unwrap();
            let mut fields = Map::new();
            fields.insert("network".into(), Value::String(network.into()));
            fields.insert("symbol".into(), Value::String(market.clone()));
            fields.insert(
                "position_side".into(),
                Value::String(position_side(position.as_ref())),
            );
            fields.insert(
                "position_qty".into(),
                pharoah_number(position_qty(position.as_ref())),
            );
            fields.insert(
                "position_entry".into(),
                pharoah_number(position_entry(position.as_ref())),
            );
            fields.insert(
                "open_algo_orders".into(),
                Value::Array(
                    orders
                        .iter()
                        .map(|o| o.get("clientAlgoId").cloned().unwrap_or(Value::Null))
                        .collect(),
                ),
            );
            fields.insert("had_trade".into(), Value::Bool(trade.is_some()));
            for (k, v) in risk.clone() {
                fields.insert(k, v);
            }
            l.event("RECONCILE", fields);
        }
        if let Some(position) = position.as_ref() {
            let side = position_side(Some(position));
            let qty = position_qty(Some(position));
            let entry = position_entry(Some(position));
            let mut trade = trade;
            if let (None, Some(pending_map)) = (trade.as_ref(), pending.as_ref()) {
                let rules = self.trading_rules(network, cfg, &market).await?;
                let signal = pending_map.get("signal").cloned().unwrap_or(Value::Null);
                let mut decision = Map::new();
                let (fee_rate, _) = self.taker_fee_rate(network, cfg, &market).await?;
                decision.insert("fee_rate".into(), Value::String(fee_rate));
                let tid = pending_map
                    .get("client_order_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let mut new_trade = self
                    .new_trade(
                        network,
                        mode,
                        &signal,
                        &Value::Object(decision),
                        &Map::new(),
                        &tid,
                        &entry.to_string(),
                        Decimal::from_f64_retain(qty).unwrap_or(Decimal::ZERO),
                        self.now_ms(),
                        &rules,
                        cfg,
                    )
                    .await?;
                new_trade.insert("entry_client_order_id".into(), Value::String(tid));
                trade = Some(new_trade);
            } else if trade.is_none()
                && !orders.iter().any(|o| {
                    o.get("clientAlgoId")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .starts_with(self.client_id())
                })
            {
                {
                    let ledger = self.ledger(network)?;
                    let l = ledger.lock().unwrap();
                    let mut fields = Map::new();
                    fields.insert("network".into(), Value::String(network.into()));
                    fields.insert("symbol".into(), Value::String(market.clone()));
                    fields.insert("side".into(), Value::String(side));
                    fields.insert("qty".into(), pharoah_number(qty));
                    fields.insert("entry".into(), pharoah_number(entry));
                    l.event("RECONCILE_FOREIGN_POSITION", fields);
                }
                state.insert("pending_entry".into(), Value::Null);
                self.set_phase(network, mode, state, IDLE, {
                    let mut e = Map::new();
                    e.insert("reconciled".into(), Value::Bool(true));
                    e
                })?;
                return Ok(());
            } else if trade.is_none() {
                let rules = self.trading_rules(network, cfg, &market).await?;
                let bar = self.now_ms();
                let mut signal = Map::new();
                signal.insert("side".into(), Value::String(side.clone()));
                signal.insert(
                    "signal_id".into(),
                    Value::String(format!("RECOVERED:{bar}")),
                );
                signal.insert("15m_bar_open_ms".into(), Value::from(bar));
                signal.insert("market_id".into(), Value::String(market.clone()));
                signal.insert("ccxt_symbol".into(), Value::String(ccxt_symbol.clone()));
                let mut decision = Map::new();
                let (fee_rate, _) = self.taker_fee_rate(network, cfg, &market).await?;
                decision.insert("fee_rate".into(), Value::String(fee_rate));
                let mut new_trade = self
                    .new_trade(
                        network,
                        mode,
                        &Value::Object(signal),
                        &Value::Object(decision),
                        &Map::new(),
                        &format!("recovered-{bar}"),
                        &entry.to_string(),
                        Decimal::from_f64_retain(qty).unwrap_or(Decimal::ZERO),
                        bar,
                        &rules,
                        cfg,
                    )
                    .await?;
                new_trade.insert(
                    "rule_violation".into(),
                    Value::String("RECOVERED_FROM_OWN_PROTECTION_ORDERS".into()),
                );
                self.notify(
                    format!(
                        "⚠️ {}: 이 전략의 손절/익절 주문이 걸린 {} {} {} 포지션을 기록 없이 발견해 관리 대상으로 복구합니다.",
                        self.label(),
                        market,
                        side,
                        qty
                    ),
                    None,
                )
                .await;
                trade = Some(new_trade);
            } else if let Some(t) = trade.as_mut() {
                let tside = get_str(t, "side");
                let tqty = get_num(t, "quantity");
                let tentry = get_num(t, "entry_price");
                if tside != side || (qty - tqty).abs() > 1e-9 || (entry - tentry).abs() > 1e-6 {
                    let ledger = self.ledger(network)?;
                    let l = ledger.lock().unwrap();
                    let mut bot = Map::new();
                    bot.insert("side".into(), Value::String(tside));
                    bot.insert("qty".into(), Value::String(tqty.to_string()));
                    bot.insert("entry".into(), Value::String(tentry.to_string()));
                    let mut exch = Map::new();
                    exch.insert("side".into(), Value::String(side.clone()));
                    exch.insert("qty".into(), pharoah_number(qty));
                    exch.insert("entry".into(), pharoah_number(entry));
                    let mut fields = Map::new();
                    fields.insert("network".into(), Value::String(network.into()));
                    fields.insert("bot".into(), Value::Object(bot));
                    fields.insert("exchange".into(), Value::Object(exch));
                    l.event("RECONCILE_DIFF", fields);
                }
                t.insert("quantity".into(), Value::String(qty.to_string()));
            }
            let mut trade = trade.expect("trade");
            {
                let ledger = self.ledger(network)?;
                let l = ledger.lock().unwrap();
                l.upsert_trade(&trade)
                    .map_err(|e| ServiceError::new("StoreError", e.to_string()))?;
            }
            state.insert("trade".into(), Value::Object(trade.clone()));
            state.insert("pending_entry".into(), Value::Null);
            let ids: HashSet<String> = orders
                .iter()
                .filter_map(|o| {
                    o.get("clientAlgoId")
                        .and_then(|v| v.as_str())
                        .map(|s| s.to_string())
                })
                .collect();
            let sl_id = get(&trade, "sl_client_algo_id")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string());
            if let Some(sl) = sl_id {
                if ids.contains(&sl) {
                    let tp_id = get(&trade, "tp_client_algo_id")
                        .and_then(|v| v.as_str())
                        .map(|s| s.to_string());
                    let tp_present = tp_id.map(|id| ids.contains(&id)).unwrap_or(false);
                    trade.insert("tp_missing".into(), Value::Bool(!tp_present));
                    self.set_phase(network, mode, state, POSITION_OPEN, {
                        let mut e = Map::new();
                        e.insert("trade".into(), Value::Object(trade.clone()));
                        e.insert("reconciled".into(), Value::Bool(true));
                        e
                    })?;
                    return Ok(());
                }
            }
            state.insert("reconciled".into(), Value::Bool(true));
            self.attach_protection(network, cfg, state).await?;
            return Ok(());
        }
        state.insert("pending_entry".into(), Value::Null);
        for order in all_orders.iter() {
            let client_id = order
                .get("clientAlgoId")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            if client_id.starts_with(self.client_id()) {
                self.cancel_algo(network, &client_id).await?;
                let ledger = self.ledger(network)?;
                let l = ledger.lock().unwrap();
                let mut fields = Map::new();
                fields.insert("network".into(), Value::String(network.into()));
                fields.insert("client_algo_id".into(), Value::String(client_id));
                l.event("CANCEL_STRAY_PROTECTION", fields);
            }
        }
        state.insert("reconciled".into(), Value::Bool(true));
        if trade.is_some() {
            self.finalize_live(network, cfg, state, None).await?;
            return Ok(());
        }
        self.set_phase(network, mode, state, IDLE, {
            let mut e = Map::new();
            e.insert("reconciled".into(), Value::Bool(true));
            e
        })?;
        Ok(())
    }

    // `close_position`
    pub async fn close_position(&self, reason: &str) -> Result<Value, ServiceError> {
        let cfg = self.config();
        let env: HashMap<String, String> = self.deps.environ.clone();
        let mode = effective_trading_mode(Some(&cfg), &env);
        let Some(network) = self.current_network(Some(&cfg)) else {
            let mut out = Map::new();
            out.insert("action".into(), Value::String("none".into()));
            out.insert(
                "reason".into(),
                Value::String("EXCHANGE_MODE_UNSUPPORTED".into()),
            );
            return Ok(Value::Object(out));
        };
        let _guard = self.cycle_lock().lock().await;
        let mut state = self.load_state(&network, &mode)?;
        let trade_opt = state.get("trade").and_then(|v| v.as_object().cloned());
        let Some(trade) = trade_opt else {
            let mut out = Map::new();
            out.insert("action".into(), Value::String("none".into()));
            out.insert("reason".into(), Value::String("NO_POSITION".into()));
            return Ok(Value::Object(out));
        };
        let (ccxt_symbol, market) = symbol_pair(Some(&Value::Object(trade.clone())));
        if mode == TRADING_MODE_DRY_RUN {
            let price_dec = d(
                self.premium_index(&network, &market)
                    .await?
                    .get("mark_price"),
                None,
            )
            .map_err(|e| ServiceError::new("ValueError", e.to_string()))?;
            let price = price_dec.to_string();
            return self
                .finalize_paper(&network, &cfg, &mut state, &price, "MANUAL", self.now_ms())
                .await;
        }
        let position = self.fetch_position(&network, &ccxt_symbol).await?;
        let Some(position) = position else {
            return self
                .finalize_live(&network, &cfg, &mut state, Some("MANUAL"))
                .await;
        };
        let (order_gateway, _) = self.gateway(&network).await?;
        let qty = position_qty(Some(&position));
        let sig = get(&trade, "signal_bar_ms").cloned().unwrap_or(Value::Null);
        let result = order_gateway
            .submit_reduce_only_close(ReduceOnlyCloseRequest {
                strategy: self.client_id().to_string(),
                symbol: ccxt_symbol.clone(),
                position_side: get_str(&trade, "side").to_lowercase(),
                position_signature: sig,
                qty,
                reason: reason.to_string(),
                params: None,
                order_intent: None,
                order_purpose: None,
                order_type: "market".into(),
                price: None,
                leg: None,
            })
            .await?;
        if result.state == OrderState::Closed.as_str() {
            return self
                .finalize_live(&network, &cfg, &mut state, Some("MANUAL"))
                .await;
        }
        let text = format!("{} {}", result.state, result.error.unwrap_or_default());
        let mut out = Map::new();
        out.insert("action".into(), Value::String("closing".into()));
        out.insert("reason".into(), Value::String(text.trim().to_string()));
        Ok(Value::Object(out))
    }
}

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

pub(crate) fn py_int(v: &Value) -> i64 {
    match v {
        Value::Number(n) => n
            .as_i64()
            .or_else(|| n.as_f64().map(|f| f as i64))
            .unwrap_or(0),
        Value::Bool(b) => *b as i64,
        Value::String(s) => s.trim().parse::<f64>().map(|f| f as i64).unwrap_or(0),
        _ => 0,
    }
}

fn signal_as_map(signal: &Value) -> &Map<String, Value> {
    static EMPTY: std::sync::OnceLock<Map<String, Value>> = std::sync::OnceLock::new();
    signal
        .as_object()
        .unwrap_or_else(|| EMPTY.get_or_init(Map::new))
}

/// `decision["rules"]` carries a serialized `TradingRules`.
fn decisions_rules(decision: &Value) -> Result<crate::core::rules::TradingRules, ServiceError> {
    let v = decision
        .get("rules")
        .ok_or_else(|| ServiceError::new("ValueError", "missing decision.rules"))?;
    rules_from_value(v)
}
