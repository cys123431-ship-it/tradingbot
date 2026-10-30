//! Port of `trading_safety/order_gateway.py` (submit_entry and submit_reduce_only_close scope).

use std::collections::HashMap;
use std::str::FromStr;
use std::sync::Arc;

use serde_json::{Map, Value};

use crate::exchange::{ExchangeError, FuturesExchange};
use crate::store::order_state::{
    build_client_order_id, BuildClientOrderIdArgs, OrderIntent, OrderRecord, OrderState,
    SQLiteTradingStateStore, StoreError,
};

fn entry_progress_rank(state: &str) -> i64 {
    match state {
        s if s == OrderState::Planned.as_str() => 0,
        s if s == OrderState::Submitting.as_str() => 1,
        s if s == OrderState::SubmittedUnknown.as_str() => 1,
        s if s == OrderState::Acknowledged.as_str() => 2,
        s if s == OrderState::PartiallyFilled.as_str() => 3,
        s if s == OrderState::FilledUnverifiedLiquidation.as_str() => 4,
        s if s == OrderState::FilledLiquidationConflict.as_str() => 5,
        s if s == OrderState::FilledUnprotected.as_str() => 5,
        s if s == OrderState::Protected.as_str() => 6,
        s if s == OrderState::Closing.as_str() => 7,
        s if s == OrderState::PartiallyClosed.as_str() => 8,
        _ => -1,
    }
}

fn daily_loss_exemption_allowed(strategy: &str, requested: bool) -> bool {
    if !requested {
        return false;
    }
    let normalized = strategy.trim().to_lowercase();
    normalized == "adaptive_breakout_trend_v1" || normalized == "adaptive_trend_pyramid"
}

fn as_dict(value: Option<&Value>) -> Map<String, Value> {
    match value {
        Some(Value::Object(map)) => map.clone(),
        _ => Map::new(),
    }
}

#[derive(Debug, Clone, Default)]
pub struct OrderSubmissionResult {
    pub client_order_id: String,
    pub state: String,
    pub order: Option<Value>,
    pub position: Option<Value>,
    pub recovered: bool,
    pub error: Option<String>,
}

impl OrderSubmissionResult {
    pub fn accepted(&self) -> bool {
        self.state == OrderState::Acknowledged.as_str()
            || self.state == OrderState::PartiallyFilled.as_str()
            || self.state == OrderState::FilledUnverifiedLiquidation.as_str()
            || self.state == OrderState::FilledUnprotected.as_str()
            || self.state == OrderState::Protected.as_str()
    }
}

fn order_client_id(order: Option<&Value>) -> String {
    let map = match order {
        Some(Value::Object(m)) => m,
        _ => return String::new(),
    };
    let info = as_dict(map.get("info"));
    for key in ["clientOrderId", "client_order_id"] {
        if let Some(v) = map.get(key) {
            if truthy(v) {
                return value_str(v);
            }
        }
    }
    for key in ["clientOrderId", "origClientOrderId"] {
        if let Some(v) = info.get(key) {
            if truthy(v) {
                return value_str(v);
            }
        }
    }
    String::new()
}

fn order_id(order: Option<&Value>) -> Option<String> {
    let map = match order {
        Some(Value::Object(m)) => m,
        _ => return None,
    };
    let info = as_dict(map.get("info"));
    if let Some(v) = map.get("id") {
        if !matches!(v, Value::Null) {
            return Some(value_str(v));
        }
    }
    if let Some(v) = map.get("orderId") {
        if !matches!(v, Value::Null) {
            return Some(value_str(v));
        }
    }
    if let Some(v) = info.get("orderId") {
        if !matches!(v, Value::Null) {
            return Some(value_str(v));
        }
    }
    None
}

fn order_fill(order: Option<&Value>) -> (f64, f64) {
    let map = match order {
        Some(Value::Object(m)) => m,
        _ => return (0.0, 0.0),
    };
    let info = as_dict(map.get("info"));
    let filled_val = map
        .get("filled")
        .filter(|v| truthy(v))
        .or_else(|| info.get("executedQty"))
        .cloned()
        .unwrap_or(Value::from(0.0));
    let average_val = map
        .get("average")
        .filter(|v| truthy(v))
        .or_else(|| info.get("avgPrice"))
        .filter(|v| truthy(v))
        .or_else(|| map.get("price"))
        .cloned()
        .unwrap_or(Value::from(0.0));
    match (to_f64_opt(&filled_val), to_f64_opt(&average_val)) {
        (Some(f), Some(a)) => (f.abs(), a),
        _ => (0.0, 0.0),
    }
}

fn position_qty(position: Option<&Value>) -> f64 {
    let map = match position {
        Some(Value::Object(m)) => m,
        _ => return 0.0,
    };
    let info = as_dict(map.get("info"));
    let value = map
        .get("contracts")
        .filter(|v| truthy(v))
        .or_else(|| info.get("positionAmt"))
        .cloned()
        .unwrap_or(Value::from(0.0));
    match to_f64_opt(&value) {
        Some(f) => f.abs(),
        None => 0.0,
    }
}

fn position_side(position: Option<&Value>) -> String {
    let map = match position {
        Some(Value::Object(m)) => m,
        _ => return String::new(),
    };
    let side = map
        .get("side")
        .map(value_str)
        .unwrap_or_default()
        .to_lowercase();
    if side == "long" || side == "short" {
        return side;
    }
    let info = as_dict(map.get("info"));
    let amt = info
        .get("positionAmt")
        .map(|v| to_f64_opt(v).unwrap_or(0.0))
        .unwrap_or(0.0);
    if amt > 0.0 {
        "long".to_string()
    } else {
        "short".to_string()
    }
}

fn classify_order(order: Option<&Value>, intent: OrderIntent) -> String {
    let map = match order {
        Some(Value::Object(m)) => m,
        _ => return OrderState::Acknowledged.as_str().to_string(),
    };
    let info = as_dict(map.get("info"));
    let status = map
        .get("status")
        .filter(|v| truthy(v))
        .or_else(|| info.get("status"))
        .map(value_str)
        .unwrap_or_default()
        .to_uppercase();
    let (filled, _) = order_fill(order);
    let amount = map
        .get("amount")
        .filter(|v| truthy(v))
        .or_else(|| info.get("origQty"))
        .cloned()
        .unwrap_or(Value::from(0.0));
    let requested = to_f64_opt(&amount).map(|f| f.abs()).unwrap_or(0.0);

    if ["CANCELED", "CANCELLED", "EXPIRED", "REJECTED"].contains(&status.as_str()) {
        return if status != "REJECTED" {
            OrderState::Canceled.as_str().to_string()
        } else {
            OrderState::Failed.as_str().to_string()
        };
    }
    let fully_filled = status == "CLOSED"
        || status == "FILLED"
        || (filled > 0.0 && requested > 0.0 && filled + 1e-12 >= requested);
    if fully_filled {
        if matches!(
            intent,
            OrderIntent::Close
                | OrderIntent::EmergencyClose
                | OrderIntent::ManualClose
                | OrderIntent::GridExit
        ) {
            return OrderState::Closing.as_str().to_string();
        }
        if matches!(
            intent,
            OrderIntent::ProtectionSl | OrderIntent::ProtectionTp
        ) {
            return OrderState::Protected.as_str().to_string();
        }
        return OrderState::FilledUnverifiedLiquidation.as_str().to_string();
    }
    if status == "PARTIALLY_FILLED" || status == "PARTIALLYFILLED" || filled > 0.0 {
        return OrderState::PartiallyFilled.as_str().to_string();
    }
    OrderState::Acknowledged.as_str().to_string()
}

fn is_definitive_rejection(err: &ExchangeError) -> bool {
    let name = err.class.to_lowercase();
    let text = err.message.to_lowercase();
    const TOKENS: &[&str] = &[
        "invalidorder",
        "insufficientfunds",
        "authentication",
        "permissiondenied",
    ];
    TOKENS.iter().any(|t| name.contains(t)) || text.contains("rejected")
}

fn is_authentication_error(err: &ExchangeError) -> bool {
    let name = err.class.to_lowercase();
    name.contains("authentication") || name.contains("permissiondenied")
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i != 0
            } else if let Some(u) = n.as_u64() {
                u != 0
            } else if let Some(f) = n.as_f64() {
                f != 0.0
            } else {
                false
            }
        }
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

fn value_str(value: &Value) -> String {
    match value {
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
        Value::Array(_) | Value::Object(_) => value.to_string(),
    }
}

fn to_f64_opt(value: &Value) -> Option<f64> {
    match value {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse::<f64>().ok(),
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        _ => None,
    }
}

fn value_from_f64(v: f64) -> Value {
    serde_json::Number::from_f64(v)
        .map(Value::Number)
        .unwrap_or(Value::Null)
}

fn python_repr(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::Bool(b) => {
            if *b {
                "True".to_string()
            } else {
                "False".to_string()
            }
        }
        Value::Number(n) => n.to_string(),
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

/// Map a store error into an `ExchangeError` with class `StoreError`.
fn store_error(e: StoreError) -> ExchangeError {
    ExchangeError::new("StoreError", e.to_string())
}

fn store_lock_poisoned() -> ExchangeError {
    ExchangeError::new("StoreError", "order state store lock poisoned")
}

pub struct EntryRequest {
    pub strategy: String,
    pub symbol: String,
    pub side: String,
    pub signal_timestamp: Value,
    pub qty: f64,
    pub params: Option<Map<String, Value>>,
    pub daily_loss_exempt: bool,
}

impl Default for EntryRequest {
    fn default() -> Self {
        Self {
            strategy: String::new(),
            symbol: String::new(),
            side: String::new(),
            signal_timestamp: Value::Null,
            qty: 0.0,
            params: None,
            daily_loss_exempt: false,
        }
    }
}

pub struct ReduceOnlyCloseRequest {
    pub strategy: String,
    pub symbol: String,
    pub position_side: String,
    pub position_signature: Value,
    pub qty: f64,
    pub reason: String,
    pub params: Option<Map<String, Value>>,
    pub order_intent: Option<OrderIntent>,
    pub order_purpose: Option<String>,
    pub order_type: String,
    pub price: Option<f64>,
    pub leg: Option<String>,
}

impl Default for ReduceOnlyCloseRequest {
    fn default() -> Self {
        Self {
            strategy: String::new(),
            symbol: String::new(),
            position_side: String::new(),
            position_signature: Value::Null,
            qty: 0.0,
            reason: String::new(),
            params: None,
            order_intent: None,
            order_purpose: None,
            order_type: "market".to_string(),
            price: None,
            leg: None,
        }
    }
}

pub struct IdempotentOrderGateway<E: FuturesExchange> {
    exchange: Arc<E>,
    store: Arc<std::sync::Mutex<SQLiteTradingStateStore>>,
    locks: std::sync::Mutex<HashMap<String, Arc<tokio::sync::Mutex<()>>>>,
    recovery_delays: Vec<f64>,
    partial_fill_policy: String,
    global_entry_lock: Arc<tokio::sync::Mutex<()>>,
    lease_owner_id: String,
}

impl<E: FuturesExchange> IdempotentOrderGateway<E> {
    pub fn new(
        exchange: Arc<E>,
        store: Arc<std::sync::Mutex<SQLiteTradingStateStore>>,
        recovery_delays: Vec<f64>,
        partial_fill_policy: String,
    ) -> Self {
        let pid = std::process::id();
        let id = &exchange as *const _ as usize;
        let lease_owner_id = format!("{pid}:{id}");
        let policy = if partial_fill_policy.is_empty() {
            "cancel_remainder".to_string()
        } else {
            partial_fill_policy.to_lowercase()
        };
        Self {
            exchange,
            store,
            locks: std::sync::Mutex::new(HashMap::new()),
            recovery_delays,
            partial_fill_policy: policy,
            global_entry_lock: Arc::new(tokio::sync::Mutex::new(())),
            lease_owner_id,
        }
    }

    pub fn exchange(&self) -> &Arc<E> {
        &self.exchange
    }

    pub fn store(&self) -> &Arc<std::sync::Mutex<SQLiteTradingStateStore>> {
        &self.store
    }

    fn lock_for(&self, symbol: &str) -> Arc<tokio::sync::Mutex<()>> {
        let key = symbol.to_uppercase();
        if let Ok(map) = self.locks.lock() {
            if let Some(lock) = map.get(&key) {
                return lock.clone();
            }
        }
        let new_lock = Arc::new(tokio::sync::Mutex::new(()));
        if let Ok(mut map) = self.locks.lock() {
            let entry = map.entry(key).or_insert_with(|| new_lock.clone());
            return entry.clone();
        }
        new_lock
    }

    fn store_get(&self, id: &str) -> Result<Option<OrderRecord>, ExchangeError> {
        let guard = self.store.lock().map_err(|_| store_lock_poisoned())?;
        guard.get(id).map_err(store_error)
    }

    fn store_upsert(&self, record: OrderRecord) -> Result<OrderRecord, ExchangeError> {
        let mut guard = self.store.lock().map_err(|_| store_lock_poisoned())?;
        guard.upsert(record).map_err(store_error)
    }

    fn store_transition(
        &self,
        id: &str,
        new_state: &str,
        changes: Map<String, Value>,
    ) -> Result<OrderRecord, ExchangeError> {
        let mut guard = self.store.lock().map_err(|_| store_lock_poisoned())?;
        guard
            .transition(id, new_state, changes)
            .map_err(store_error)
    }

    fn store_entry_block_reason(
        &self,
        symbol: Option<&str>,
        for_position_add: bool,
        ignore_daily_loss_lock: bool,
    ) -> Result<Option<String>, ExchangeError> {
        let guard = self.store.lock().map_err(|_| store_lock_poisoned())?;
        guard
            .entry_block_reason(symbol, for_position_add, ignore_daily_loss_lock)
            .map_err(store_error)
    }

    fn store_set_runtime_state(&self, key: &str, value: Value) -> Result<(), ExchangeError> {
        let guard = self.store.lock().map_err(|_| store_lock_poisoned())?;
        guard.set_runtime_state(key, &value).map_err(store_error)
    }

    fn store_exec<R>(
        &self,
        f: impl FnOnce(&mut SQLiteTradingStateStore) -> Result<R, StoreError>,
    ) -> Result<R, ExchangeError> {
        let mut guard = self.store.lock().map_err(|_| store_lock_poisoned())?;
        f(&mut guard).map_err(store_error)
    }

    async fn fetch_position(&self, symbol: &str) -> Result<Option<Value>, ExchangeError> {
        let positions = self
            .exchange
            .fetch_positions(Some(vec![symbol.to_string()]))
            .await?;
        for position in positions {
            let contracts = match &position {
                Value::Object(map) => map
                    .get("contracts")
                    .filter(|v| truthy(v))
                    .cloned()
                    .or_else(|| {
                        map.get("info")
                            .and_then(|info| match info {
                                Value::Object(i) => i.get("positionAmt").cloned(),
                                _ => None,
                            })
                            .filter(truthy)
                    })
                    .unwrap_or(Value::from(0.0)),
                _ => Value::from(0.0),
            };
            if let Some(v) = to_f64_opt(&contracts) {
                if v.abs() > 0.0 {
                    return Ok(Some(position));
                }
            }
        }
        Ok(None)
    }

    pub async fn fetch_order_by_client_id(
        &self,
        symbol: &str,
        client_order_id: &str,
    ) -> Result<Option<Value>, ExchangeError> {
        let market_id = symbol.replace('/', "");
        let market_id = market_id.split(':').next().unwrap_or("").to_string();

        let mut lookup_errors: Vec<ExchangeError> = Vec::new();

        match self
            .exchange
            .get_order_raw(&market_id, client_order_id)
            .await
        {
            Ok(value) => {
                if matches!(value, Value::Null) {
                    // fall through to open orders
                } else {
                    return Ok(Some(value));
                }
            }
            Err(err) => {
                if err.class == "NotSupported" {
                    // skip
                } else {
                    let text = err.message.to_lowercase();
                    if ["unknown order", "order does not exist", "-2013"]
                        .iter()
                        .any(|t| text.contains(t))
                    {
                        return Ok(None);
                    }
                    lookup_errors.push(err);
                }
            }
        }

        match self.exchange.fetch_open_orders(Some(symbol)).await {
            Ok(orders) => {
                for order in orders {
                    if order_client_id(Some(&order)) == client_order_id {
                        return Ok(Some(order));
                    }
                }
                if lookup_errors.is_empty() {
                    return Ok(None);
                }
            }
            Err(err) => {
                if err.class != "NotSupported" {
                    lookup_errors.push(err);
                }
            }
        }

        if let Some(last) = lookup_errors.last() {
            return Err(ExchangeError {
                class: "RuntimeError".to_string(),
                message: format!(
                    "client order lookup unavailable: {}: {}",
                    last.class, last.message
                ),
                code: None,
                outcome_unknown: last.outcome_unknown,
            });
        }
        Ok(None)
    }

    pub fn transition_from_exchange_order(
        &self,
        client_order_id: &str,
        order: &Value,
    ) -> Result<Option<OrderRecord>, ExchangeError> {
        let record = self.store_get(client_order_id)?;
        let intent = record
            .as_ref()
            .map(|r| OrderIntent::from_str(&r.order_intent).unwrap_or(OrderIntent::Entry))
            .unwrap_or(OrderIntent::Entry);
        let mut state = classify_order(Some(order), intent);
        let (mut filled, mut average) = order_fill(Some(order));

        if let Some(rec) = record.as_ref() {
            if matches!(
                intent,
                OrderIntent::Entry | OrderIntent::PositionAdd | OrderIntent::GridEntry
            ) {
                let current_rank = entry_progress_rank(&rec.order_state);
                let incoming_rank = entry_progress_rank(&state);
                let exchange_terminal_after_fill = (state == OrderState::Canceled.as_str()
                    || state == OrderState::Failed.as_str())
                    && rec.filled_qty > 0.0;
                if (incoming_rank >= 0 && current_rank > incoming_rank)
                    || exchange_terminal_after_fill
                {
                    state = rec.order_state.clone();
                }
                filled = rec.filled_qty.max(filled);
                if average <= 0.0 {
                    average = rec.average_fill_price;
                }
            }
        }

        let mut changes = Map::new();
        changes.insert(
            "exchange_order_id".to_string(),
            match order_id(Some(order)) {
                Some(id) => Value::String(id),
                None => Value::Null,
            },
        );
        changes.insert("filled_qty".to_string(), value_from_f64(filled));
        changes.insert("average_fill_price".to_string(), value_from_f64(average));
        changes.insert("last_error".to_string(), Value::Null);
        let updated = self.store_transition(client_order_id, &state, changes)?;
        Ok(Some(updated))
    }

    async fn cancel_partial_remainder(
        &self,
        symbol: &str,
        order: &Value,
    ) -> Result<(), ExchangeError> {
        if self.partial_fill_policy != "cancel_remainder" {
            return Ok(());
        }
        let order_id = match order_id(Some(order)) {
            Some(id) => id,
            None => return Ok(()),
        };
        self.exchange.cancel_order(&order_id, symbol).await?;
        Ok(())
    }

    async fn create_order(
        &self,
        symbol: &str,
        order_type: &str,
        side: &str,
        qty: f64,
        price: Option<f64>,
        params: Map<String, Value>,
    ) -> Result<Value, ExchangeError> {
        self.exchange
            .create_order(symbol, order_type, side, qty, price, params)
            .await
    }

    async fn recover_entry_or_add(
        &self,
        record: &OrderRecord,
        wait: bool,
    ) -> Result<Option<OrderSubmissionResult>, ExchangeError> {
        let attempts: Vec<f64> = if wait {
            self.recovery_delays.clone()
        } else {
            vec![0.0]
        };
        let tolerance = 1e-12_f64.max(record.requested_qty * 1e-9);
        for delay in attempts {
            if delay > 0.0 {
                sleep_secs(delay).await;
            }
            let lookup = async {
                let order = self
                    .fetch_order_by_client_id(&record.symbol, &record.client_order_id)
                    .await?;
                let position = self.fetch_position(&record.symbol).await?;
                Ok::<_, ExchangeError>((order, position))
            }
            .await;

            let (order, position) = match lookup {
                Ok(v) => v,
                Err(exc) => {
                    let current = self.store_get(&record.client_order_id)?;
                    let retry = current.as_ref().map(|c| c.retry_count).unwrap_or(0) + 1;
                    let mut changes = Map::new();
                    changes.insert(
                        "last_error".to_string(),
                        Value::String(format!("entry recovery lookup failed: {}", exc.message)),
                    );
                    changes.insert("retry_count".to_string(), Value::from(retry));
                    self.store_transition(
                        &record.client_order_id,
                        OrderState::SubmittedUnknown.as_str(),
                        changes,
                    )?;
                    continue;
                }
            };

            let mut updated = match order.as_ref() {
                Some(o) => self
                    .transition_from_exchange_order(&record.client_order_id, o)?
                    .unwrap_or_else(|| record.clone()),
                None => record.clone(),
            };

            let current_qty = position_qty(position.as_ref());
            let filled_delta = (current_qty - record.position_qty_before).max(0.0);
            if filled_delta > tolerance {
                let pos_map = match position.as_ref() {
                    Some(Value::Object(m)) => Some(m),
                    _ => None,
                };
                let info = as_dict(pos_map.and_then(|m| m.get("info")));
                let entry_price = pos_map
                    .and_then(|m| m.get("entryPrice"))
                    .filter(|v| truthy(v))
                    .or_else(|| info.get("entryPrice").filter(|v| truthy(v)))
                    .and_then(to_f64_opt)
                    .unwrap_or(0.0);
                let avg = if entry_price > 0.0 {
                    entry_price
                } else {
                    updated.average_fill_price
                };
                let mut changes = Map::new();
                changes.insert(
                    "filled_qty".to_string(),
                    value_from_f64(updated.filled_qty.max(filled_delta)),
                );
                changes.insert("average_fill_price".to_string(), value_from_f64(avg));
                changes.insert("last_error".to_string(), Value::Null);
                updated = self.store_transition(
                    &record.client_order_id,
                    OrderState::FilledUnverifiedLiquidation.as_str(),
                    changes,
                )?;
            } else if order.is_some() && updated.order_state == OrderState::PartiallyFilled.as_str()
            {
                let order_ref = order.as_ref().unwrap();
                self.cancel_partial_remainder(&record.symbol, order_ref)
                    .await?;
            } else if order.is_none() {
                let mut changes = Map::new();
                changes.insert(
                    "last_error".to_string(),
                    Value::String(
                        "order not found and position quantity did not increase".to_string(),
                    ),
                );
                updated = self.store_transition(
                    &record.client_order_id,
                    OrderState::SubmittedUnknown.as_str(),
                    changes,
                )?;
            }

            return Ok(Some(OrderSubmissionResult {
                client_order_id: record.client_order_id.clone(),
                state: updated.order_state.clone(),
                order,
                position,
                recovered: true,
                error: updated.last_error.clone(),
            }));
        }
        Ok(None)
    }

    async fn recover_close(
        &self,
        record: &OrderRecord,
        wait: bool,
    ) -> Result<Option<OrderSubmissionResult>, ExchangeError> {
        let attempts: Vec<f64> = if wait {
            self.recovery_delays.clone()
        } else {
            vec![0.0]
        };
        let tolerance = 1e-12_f64.max(record.position_qty_before * 1e-9);
        let expected_side = record.side.to_lowercase();
        for delay in attempts {
            if delay > 0.0 {
                sleep_secs(delay).await;
            }
            let lookup = async {
                let order = self
                    .fetch_order_by_client_id(&record.symbol, &record.client_order_id)
                    .await?;
                let position = self.fetch_position(&record.symbol).await?;
                Ok::<_, ExchangeError>((order, position))
            }
            .await;

            let (order, position) = match lookup {
                Ok(v) => v,
                Err(exc) => {
                    let current = self.store_get(&record.client_order_id)?;
                    let retry = current.as_ref().map(|c| c.retry_count).unwrap_or(0) + 1;
                    let mut changes = Map::new();
                    changes.insert(
                        "last_error".to_string(),
                        Value::String(format!("close recovery lookup failed: {}", exc.message)),
                    );
                    changes.insert("retry_count".to_string(), Value::from(retry));
                    self.store_transition(
                        &record.client_order_id,
                        OrderState::SubmittedUnknown.as_str(),
                        changes,
                    )?;
                    continue;
                }
            };

            let pos_side = if position.is_some() {
                position_side(position.as_ref())
            } else {
                String::new()
            };
            if position.is_some() && !pos_side.is_empty() && pos_side != expected_side {
                let reason = format!(
                    "RECONCILIATION_REQUIRED:close_reversed_position:{}",
                    record.symbol
                );
                self.store_set_runtime_state(
                    "entry_lock_reason",
                    Value::String(format!("CRITICAL_PAUSE:{}", reason)),
                )?;
                let mut changes = Map::new();
                changes.insert("last_error".to_string(), Value::String(reason.clone()));
                let updated = self.store_transition(
                    &record.client_order_id,
                    OrderState::SubmittedUnknown.as_str(),
                    changes,
                )?;
                return Ok(Some(OrderSubmissionResult {
                    client_order_id: record.client_order_id.clone(),
                    state: updated.order_state.clone(),
                    order,
                    position,
                    recovered: true,
                    error: Some(reason),
                }));
            }

            let remaining_qty = position_qty(position.as_ref());
            if position.is_none() || remaining_qty <= tolerance {
                let mut changes = Map::new();
                changes.insert(
                    "filled_qty".to_string(),
                    value_from_f64(record.filled_qty.max(record.position_qty_before)),
                );
                changes.insert("last_error".to_string(), Value::Null);
                let updated = self.store_transition(
                    &record.client_order_id,
                    OrderState::Closed.as_str(),
                    changes,
                )?;
                self.store_exec(|store| {
                    store.release_entry_lease_for_symbol(&record.symbol, true)
                })?;
                return Ok(Some(OrderSubmissionResult {
                    client_order_id: record.client_order_id.clone(),
                    state: updated.order_state.clone(),
                    order,
                    position,
                    recovered: true,
                    error: None,
                }));
            }

            let closed_qty = (record.position_qty_before - remaining_qty).max(0.0);
            let updated = if closed_qty > tolerance {
                let mut changes = Map::new();
                changes.insert("filled_qty".to_string(), value_from_f64(closed_qty));
                changes.insert("last_error".to_string(), Value::Null);
                self.store_transition(
                    &record.client_order_id,
                    OrderState::PartiallyClosed.as_str(),
                    changes,
                )?
            } else if let Some(o) = order.as_ref() {
                let trans = self
                    .transition_from_exchange_order(&record.client_order_id, o)?
                    .unwrap_or_else(|| record.clone());
                if trans.order_state != OrderState::Acknowledged.as_str()
                    && trans.order_state != OrderState::Closing.as_str()
                {
                    self.store_transition(
                        &record.client_order_id,
                        OrderState::Closing.as_str(),
                        Map::new(),
                    )?
                } else {
                    trans
                }
            } else {
                let mut changes = Map::new();
                changes.insert(
                    "last_error".to_string(),
                    Value::String(
                        "close order not found and position quantity unchanged".to_string(),
                    ),
                );
                self.store_transition(
                    &record.client_order_id,
                    OrderState::SubmittedUnknown.as_str(),
                    changes,
                )?
            };

            return Ok(Some(OrderSubmissionResult {
                client_order_id: record.client_order_id.clone(),
                state: updated.order_state.clone(),
                order,
                position,
                recovered: true,
                error: updated.last_error.clone(),
            }));
        }
        Ok(None)
    }

    pub async fn recover(
        &self,
        record: &OrderRecord,
        wait: bool,
    ) -> Result<Option<OrderSubmissionResult>, ExchangeError> {
        let intent = OrderIntent::from_str(&record.order_intent).unwrap_or(OrderIntent::Entry);
        if matches!(
            intent,
            OrderIntent::Close
                | OrderIntent::EmergencyClose
                | OrderIntent::ManualClose
                | OrderIntent::GridExit
        ) {
            return self.recover_close(record, wait).await;
        }
        self.recover_entry_or_add(record, wait).await
    }

    async fn _recover(
        &self,
        record: &OrderRecord,
        wait: bool,
    ) -> Result<Option<OrderSubmissionResult>, ExchangeError> {
        self.recover(record, wait).await
    }

    pub async fn submit_entry(
        &self,
        req: EntryRequest,
    ) -> Result<OrderSubmissionResult, ExchangeError> {
        let EntryRequest {
            strategy,
            symbol,
            side,
            signal_timestamp,
            qty,
            params,
            daily_loss_exempt,
        } = req;
        let daily_loss_exempt = daily_loss_exemption_allowed(&strategy, daily_loss_exempt);

        let ts_value = if matches!(signal_timestamp, Value::Null) {
            None
        } else {
            Some(&signal_timestamp)
        };
        let client_order_id = build_client_order_id(BuildClientOrderIdArgs {
            strategy: &strategy,
            symbol: &symbol,
            side: &side,
            signal_timestamp: ts_value,
            stage: "entry",
            leg: None,
            revision: None,
            max_length: 36,
        });

        let _global = self.global_entry_lock.clone().lock_owned().await;
        let symbol_lock = self.lock_for(&symbol);
        let _symbol_guard = symbol_lock.lock_owned().await;

        if let Some(existing) = self.store_get(&client_order_id)? {
            let st = existing.order_state.as_str();
            if st == OrderState::Protected.as_str()
                || st == OrderState::Closed.as_str()
                || st == OrderState::Canceled.as_str()
                || st == OrderState::Failed.as_str()
            {
                if st == OrderState::Closed.as_str()
                    || st == OrderState::Canceled.as_str()
                    || st == OrderState::Failed.as_str()
                {
                    self.store_exec(|store| {
                        store.release_entry_lease(&client_order_id, &existing.order_state)
                    })?;
                }
                return Ok(OrderSubmissionResult {
                    client_order_id,
                    state: existing.order_state,
                    order: None,
                    position: None,
                    recovered: true,
                    error: Some("signal already handled".to_string()),
                });
            }
            if let Some(recovered) = self._recover(&existing, false).await? {
                return Ok(recovered);
            }
            return Ok(OrderSubmissionResult {
                client_order_id,
                state: existing.order_state,
                order: None,
                position: None,
                recovered: true,
                error: existing.last_error,
            });
        }

        let blocker = self.store_entry_block_reason(Some(&symbol), false, daily_loss_exempt)?;
        if let Some(blocker) = blocker {
            return Ok(OrderSubmissionResult {
                client_order_id,
                state: "BLOCKED".to_string(),
                order: None,
                position: None,
                recovered: false,
                error: Some(blocker),
            });
        }

        let position = match self.fetch_position(&symbol).await {
            Ok(p) => p,
            Err(exc) => {
                self.store_set_runtime_state(
                    "entry_lock_reason",
                    Value::String(format!(
                        "RECONCILIATION_REQUIRED:position_fetch:{}",
                        exc.class
                    )),
                )?;
                return Ok(OrderSubmissionResult {
                    client_order_id,
                    state: "BLOCKED".to_string(),
                    order: None,
                    position: None,
                    recovered: false,
                    error: Some(format!("position preflight unavailable: {}", exc.message)),
                });
            }
        };
        if position.is_some() {
            return Ok(OrderSubmissionResult {
                client_order_id,
                state: "BLOCKED".to_string(),
                order: None,
                position,
                recovered: false,
                error: Some("position already exists".to_string()),
            });
        }

        let acquired = self.store_exec(|store| {
            store.try_acquire_entry_lease(
                &self.lease_owner_id,
                120.0,
                &symbol,
                &client_order_id,
                false,
                false,
            )
        })?;
        if !acquired {
            return Ok(OrderSubmissionResult {
                client_order_id,
                state: "BLOCKED".to_string(),
                order: None,
                position: None,
                recovered: false,
                error: Some("GLOBAL_ENTRY_LEASE_HELD".to_string()),
            });
        }

        let mut metadata = Map::new();
        metadata.insert(
            "daily_loss_exempt".to_string(),
            Value::Bool(daily_loss_exempt),
        );
        let record = OrderRecord {
            client_order_id: client_order_id.clone(),
            symbol: symbol.clone(),
            side: side.to_uppercase(),
            strategy: strategy.clone(),
            signal_timestamp: python_repr(&signal_timestamp),
            requested_qty: qty,
            order_intent: OrderIntent::Entry.as_str().to_string(),
            order_purpose: "entry".to_string(),
            position_qty_before: 0.0,
            metadata,
            ..Default::default()
        };
        let record = self.store_upsert(record)?;

        let exchange_existing = match self
            .fetch_order_by_client_id(&symbol, &client_order_id)
            .await
        {
            Ok(v) => v,
            Err(exc) => {
                let mut changes = Map::new();
                changes.insert(
                    "last_error".to_string(),
                    Value::String(format!(
                        "pre-submit duplicate lookup unavailable: {}",
                        exc.message
                    )),
                );
                self.store_transition(
                    &client_order_id,
                    OrderState::SubmittedUnknown.as_str(),
                    changes,
                )?;
                self.store_set_runtime_state(
                    "entry_lock_reason",
                    Value::String("RECONCILIATION_REQUIRED".to_string()),
                )?;
                return Ok(OrderSubmissionResult {
                    client_order_id,
                    state: OrderState::SubmittedUnknown.as_str().to_string(),
                    order: None,
                    position: None,
                    recovered: false,
                    error: Some(exc.message),
                });
            }
        };
        if let Some(existing_order) = exchange_existing {
            let updated = self
                .transition_from_exchange_order(&client_order_id, &existing_order)?
                .unwrap_or_else(|| record.clone());
            return Ok(OrderSubmissionResult {
                client_order_id,
                state: updated.order_state,
                order: Some(existing_order),
                position: None,
                recovered: true,
                error: None,
            });
        }

        self.store_transition(
            &client_order_id,
            OrderState::Submitting.as_str(),
            Map::new(),
        )?;

        let mut order_params = params.unwrap_or_default();
        order_params.insert(
            "newClientOrderId".to_string(),
            Value::String(client_order_id.clone()),
        );
        let side_lower = side.to_lowercase();
        let exchange_side = if side_lower == "long" || side_lower == "buy" {
            "buy"
        } else {
            "sell"
        };

        let create_result = self
            .create_order(&symbol, "market", exchange_side, qty, None, order_params)
            .await;

        let order = match create_result {
            Ok(o) => o,
            Err(exc) => {
                if is_definitive_rejection(&exc) {
                    let mut changes = Map::new();
                    changes.insert("last_error".to_string(), Value::String(exc.message.clone()));
                    self.store_transition(&client_order_id, OrderState::Failed.as_str(), changes)?;
                    let st = OrderState::Failed.as_str().to_string();
                    self.store_exec(|store| store.release_entry_lease(&client_order_id, &st))?;
                    if is_authentication_error(&exc) {
                        self.store_set_runtime_state(
                            "entry_lock_reason",
                            Value::String(format!("AUTHENTICATION_ERROR:{}", exc.class)),
                        )?;
                    }
                    return Ok(OrderSubmissionResult {
                        client_order_id,
                        state: OrderState::Failed.as_str().to_string(),
                        order: None,
                        position: None,
                        recovered: false,
                        error: Some(exc.message),
                    });
                }
                let mut changes = Map::new();
                changes.insert(
                    "last_error".to_string(),
                    Value::String(format!("{}: {}", exc.class, exc.message)),
                );
                changes.insert("retry_count".to_string(), Value::from(1));
                self.store_transition(
                    &client_order_id,
                    OrderState::SubmittedUnknown.as_str(),
                    changes,
                )?;
                let unknown_record = match self.store_get(&client_order_id)? {
                    Some(r) => r,
                    None => {
                        return Err(ExchangeError::new(
                            "RuntimeError",
                            "submitted order state disappeared before recovery",
                        ));
                    }
                };
                if let Some(recovered) = self._recover(&unknown_record, true).await? {
                    return Ok(recovered);
                }
                return Ok(OrderSubmissionResult {
                    client_order_id,
                    state: OrderState::SubmittedUnknown.as_str().to_string(),
                    order: None,
                    position: None,
                    recovered: false,
                    error: Some(exc.message),
                });
            }
        };

        let mut updated = self
            .transition_from_exchange_order(&client_order_id, &order)?
            .unwrap_or_else(|| record.clone());
        if updated.order_state == OrderState::Canceled.as_str()
            || updated.order_state == OrderState::Failed.as_str()
        {
            let st = updated.order_state.clone();
            self.store_exec(|store| store.release_entry_lease(&client_order_id, &st))?;
        }
        if updated.order_state == OrderState::PartiallyFilled.as_str() {
            match self.cancel_partial_remainder(&symbol, &order).await {
                Ok(()) => {}
                Err(exc) => {
                    let mut changes = Map::new();
                    changes.insert(
                        "last_error".to_string(),
                        Value::String(format!("partial remainder cancel failed: {}", exc.message)),
                    );
                    self.store_transition(
                        &client_order_id,
                        OrderState::SubmittedUnknown.as_str(),
                        changes,
                    )?;
                    return Ok(OrderSubmissionResult {
                        client_order_id,
                        state: OrderState::SubmittedUnknown.as_str().to_string(),
                        order: Some(order),
                        position: None,
                        recovered: false,
                        error: Some(exc.message),
                    });
                }
            }
        }

        let position = self.fetch_position(&symbol).await?;
        if let Some(pos) = position.as_ref() {
            let pos_side = pos
                .get("side")
                .map(value_str)
                .unwrap_or_default()
                .to_lowercase();
            let expected_side = if side_lower == "long" || side_lower == "buy" {
                "long"
            } else {
                "short"
            };
            if !pos_side.is_empty() && pos_side != expected_side {
                let message = format!(
                    "order/position side contradiction: expected={} actual={}",
                    expected_side, pos_side
                );
                let mut changes = Map::new();
                changes.insert("last_error".to_string(), Value::String(message.clone()));
                self.store_transition(
                    &client_order_id,
                    OrderState::SubmittedUnknown.as_str(),
                    changes,
                )?;
                return Ok(OrderSubmissionResult {
                    client_order_id,
                    state: OrderState::SubmittedUnknown.as_str().to_string(),
                    order: Some(order),
                    position,
                    recovered: false,
                    error: Some(message),
                });
            }

            let pos_map = match pos {
                Value::Object(m) => m,
                _ => unreachable!(),
            };
            let info = as_dict(pos_map.get("info"));
            let contracts = pos_map
                .get("contracts")
                .filter(|v| truthy(v))
                .or_else(|| info.get("positionAmt").filter(|v| truthy(v)))
                .and_then(to_f64_opt)
                .unwrap_or(updated.filled_qty);
            let entry = pos_map
                .get("entryPrice")
                .filter(|v| truthy(v))
                .or_else(|| info.get("entryPrice").filter(|v| truthy(v)))
                .and_then(to_f64_opt)
                .unwrap_or(updated.average_fill_price);

            let mut changes = Map::new();
            changes.insert("filled_qty".to_string(), value_from_f64(contracts.abs()));
            changes.insert("average_fill_price".to_string(), value_from_f64(entry));
            updated = self.store_transition(
                &client_order_id,
                OrderState::FilledUnverifiedLiquidation.as_str(),
                changes,
            )?;
        }

        Ok(OrderSubmissionResult {
            client_order_id,
            state: updated.order_state,
            order: Some(order),
            position,
            recovered: false,
            error: None,
        })
    }

    pub async fn submit_reduce_only_close(
        &self,
        req: ReduceOnlyCloseRequest,
    ) -> Result<OrderSubmissionResult, ExchangeError> {
        let ReduceOnlyCloseRequest {
            strategy,
            symbol,
            position_side,
            position_signature,
            qty,
            reason,
            params,
            order_intent,
            order_purpose,
            order_type,
            price,
            leg,
        } = req;

        let mut purpose = order_purpose
            .or_else(|| {
                if reason.is_empty() {
                    None
                } else {
                    Some(reason.clone())
                }
            })
            .unwrap_or_else(|| "close".to_string());
        purpose = purpose.trim().to_lowercase().replace(' ', "_");
        if !purpose.starts_with("close_") {
            purpose = format!("close_{}", purpose);
        }
        let intent = match order_intent {
            Some(i) => i,
            None => {
                if purpose.contains("emergency")
                    || purpose.contains("protection")
                    || purpose.contains("liquidation")
                {
                    OrderIntent::EmergencyClose
                } else if purpose.contains("manual") {
                    OrderIntent::ManualClose
                } else if purpose.contains("grid") {
                    OrderIntent::GridExit
                } else {
                    OrderIntent::Close
                }
            }
        };

        let ts_value = if matches!(position_signature, Value::Null) {
            None
        } else {
            Some(&position_signature)
        };
        let client_order_id = build_client_order_id(BuildClientOrderIdArgs {
            strategy: &strategy,
            symbol: &symbol,
            side: &position_side,
            signal_timestamp: ts_value,
            stage: &purpose,
            leg: leg.as_deref(),
            revision: None,
            max_length: 36,
        });

        let symbol_lock = self.lock_for(&symbol);
        let _symbol_guard = symbol_lock.lock_owned().await;

        if let Some(existing) = self.store_get(&client_order_id)? {
            if let Some(recovered) = self.recover_close(&existing, false).await? {
                return Ok(recovered);
            }
            return Ok(OrderSubmissionResult {
                client_order_id,
                state: existing.order_state,
                order: None,
                position: None,
                recovered: true,
                error: existing.last_error,
            });
        }

        let before = match self.fetch_position(&symbol).await {
            Ok(p) => p,
            Err(exc) => {
                self.store_set_runtime_state(
                    "entry_lock_reason",
                    Value::String(format!(
                        "RECONCILIATION_REQUIRED:close_preflight:{}",
                        exc.class
                    )),
                )?;
                return Ok(OrderSubmissionResult {
                    client_order_id,
                    state: "BLOCKED".to_string(),
                    order: None,
                    position: None,
                    recovered: false,
                    error: Some(exc.message),
                });
            }
        };

        let before_qty = position_qty(before.as_ref());
        if before_qty <= 0.0 {
            return Ok(OrderSubmissionResult {
                client_order_id,
                state: OrderState::Closed.as_str().to_string(),
                order: None,
                position: before,
                recovered: true,
                error: Some("position already flat".to_string()),
            });
        }

        let expected_side = position_side.to_lowercase();
        let actual_side = position_side_of(before.as_ref());
        if !actual_side.is_empty() && actual_side != expected_side {
            return Ok(OrderSubmissionResult {
                client_order_id,
                state: "BLOCKED".to_string(),
                order: None,
                position: before,
                recovered: false,
                error: Some("position side mismatch before close".to_string()),
            });
        }

        let mut metadata = Map::new();
        metadata.insert("reason".to_string(), Value::String(reason.clone()));
        metadata.insert("reduce_only".to_string(), Value::Bool(true));

        let record = OrderRecord {
            client_order_id: client_order_id.clone(),
            symbol: symbol.clone(),
            side: position_side.to_uppercase(),
            strategy: strategy.clone(),
            signal_timestamp: python_repr(&position_signature),
            requested_qty: qty,
            order_intent: intent.as_str().to_string(),
            order_purpose: purpose.clone(),
            position_qty_before: before_qty,
            position_signature: Some(python_repr(&position_signature)),
            order_state: OrderState::Planned.as_str().to_string(),
            metadata,
            ..Default::default()
        };
        self.store_upsert(record)?;

        let mut close_params = params.unwrap_or_default();
        close_params.insert("reduceOnly".to_string(), Value::Bool(true));
        close_params.insert(
            "newClientOrderId".to_string(),
            Value::String(client_order_id.clone()),
        );
        let close_side = if position_side.to_lowercase() == "long" {
            "sell"
        } else {
            "buy"
        };

        self.store_transition(
            &client_order_id,
            OrderState::Submitting.as_str(),
            Map::new(),
        )?;

        let create_result = self
            .create_order(&symbol, &order_type, close_side, qty, price, close_params)
            .await;

        let order = match create_result {
            Ok(o) => o,
            Err(exc) => {
                if is_definitive_rejection(&exc) {
                    let mut changes = Map::new();
                    changes.insert("last_error".to_string(), Value::String(exc.message.clone()));
                    self.store_transition(&client_order_id, OrderState::Failed.as_str(), changes)?;
                    return Ok(OrderSubmissionResult {
                        client_order_id,
                        state: OrderState::Failed.as_str().to_string(),
                        order: None,
                        position: None,
                        recovered: false,
                        error: Some(exc.message),
                    });
                }
                let mut changes = Map::new();
                changes.insert(
                    "last_error".to_string(),
                    Value::String(format!("{}: {}", exc.class, exc.message)),
                );
                self.store_transition(
                    &client_order_id,
                    OrderState::SubmittedUnknown.as_str(),
                    changes,
                )?;
                let unknown_record = match self.store_get(&client_order_id)? {
                    Some(r) => r,
                    None => {
                        return Err(ExchangeError::new(
                            "RuntimeError",
                            "close order state disappeared before recovery",
                        ));
                    }
                };
                if let Some(recovered) = self.recover_close(&unknown_record, true).await? {
                    return Ok(recovered);
                }
                return Ok(OrderSubmissionResult {
                    client_order_id,
                    state: OrderState::SubmittedUnknown.as_str().to_string(),
                    order: None,
                    position: None,
                    recovered: false,
                    error: Some(exc.message),
                });
            }
        };

        self.transition_from_exchange_order(&client_order_id, &order)?;
        let current = match self.store_get(&client_order_id)? {
            Some(r) => r,
            None => {
                return Err(ExchangeError::new(
                    "RuntimeError",
                    "close order state disappeared after submission",
                ));
            }
        };
        match self.recover_close(&current, false).await? {
            Some(recovered) => Ok(recovered),
            None => Ok(OrderSubmissionResult {
                client_order_id,
                state: OrderState::SubmittedUnknown.as_str().to_string(),
                order: Some(order),
                position: None,
                recovered: false,
                error: Some("close confirmation unavailable".to_string()),
            }),
        }
    }
}

fn position_side_of(position: Option<&Value>) -> String {
    let map = match position {
        Some(Value::Object(m)) => m,
        _ => return String::new(),
    };
    let side = map
        .get("side")
        .map(value_str)
        .unwrap_or_default()
        .to_lowercase();
    if side == "long" || side == "short" {
        return side;
    }
    let info = as_dict(map.get("info"));
    let amt = info
        .get("positionAmt")
        .map(|v| to_f64_opt(v).unwrap_or(0.0))
        .unwrap_or(0.0);
    if amt > 0.0 {
        "long".to_string()
    } else {
        "short".to_string()
    }
}

async fn sleep_secs(secs: f64) {
    if secs <= 0.0 {
        tokio::task::yield_now().await;
        return;
    }
    let duration = std::time::Duration::from_secs_f64(secs);
    tokio::time::sleep(duration).await;
}

/// Expose helpers for tests that need them.
pub fn order_client_id_public(order: Option<&Value>) -> String {
    order_client_id(order)
}
