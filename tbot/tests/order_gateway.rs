use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use serde_json::{Map, Value};
use tbot::exchange::{ExchangeError, FuturesExchange};
use tbot::safety::order_gateway::{
    EntryRequest, IdempotentOrderGateway, OrderSubmissionResult, ReduceOnlyCloseRequest,
};
use tbot::store::clock::SystemClock;
use tbot::store::order_state::{OrderRecord, OrderState, SQLiteTradingStateStore};

#[derive(Clone, Copy, PartialEq, Eq)]
enum Behavior {
    Filled,
    Partial,
    TimeoutNotReceived,
    TimeoutAfterFill,
}

struct MockExchange {
    behavior: Behavior,
    orders: Mutex<HashMap<String, Value>>,
    state: Mutex<MockState>,
    failed_lookup: Mutex<bool>,
    auth_error: Mutex<bool>,
}

struct MockState {
    create_count: i64,
    cancel_count: i64,
    position: Option<Value>,
}

impl MockExchange {
    fn new(behavior: Behavior) -> Self {
        Self {
            behavior,
            orders: Mutex::new(HashMap::new()),
            state: Mutex::new(MockState {
                create_count: 0,
                cancel_count: 0,
                position: None,
            }),
            failed_lookup: Mutex::new(false),
            auth_error: Mutex::new(false),
        }
    }

    fn set_position(&self, position: Option<Value>) {
        self.state.lock().unwrap().position = position;
    }

    fn create_count(&self) -> i64 {
        self.state.lock().unwrap().create_count
    }

    fn cancel_count(&self) -> i64 {
        self.state.lock().unwrap().cancel_count
    }

    fn orders_snapshot(&self) -> HashMap<String, Value> {
        self.orders.lock().unwrap().clone()
    }

    fn set_failed_lookup(&self, failed: bool) {
        *self.failed_lookup.lock().unwrap() = failed;
    }

    fn set_auth_error(&self, auth: bool) {
        *self.auth_error.lock().unwrap() = auth;
    }
}

impl FuturesExchange for MockExchange {
    async fn fetch_positions(
        &self,
        _symbols: Option<Vec<String>>,
    ) -> Result<Vec<Value>, ExchangeError> {
        let state = self.state.lock().unwrap();
        match &state.position {
            Some(p) => Ok(vec![p.clone()]),
            None => Ok(Vec::new()),
        }
    }

    async fn get_order_raw(
        &self,
        _market_id: &str,
        orig_client_order_id: &str,
    ) -> Result<Value, ExchangeError> {
        if *self.failed_lookup.lock().unwrap() {
            return Err(ExchangeError::new("TimeoutError", "lookup unavailable"));
        }
        let orders = self.orders.lock().unwrap();
        Ok(orders
            .get(orig_client_order_id)
            .cloned()
            .unwrap_or(Value::Null))
    }

    async fn create_order(
        &self,
        symbol: &str,
        _order_type: &str,
        side: &str,
        qty: f64,
        _price: Option<f64>,
        params: Map<String, Value>,
    ) -> Result<Value, ExchangeError> {
        if *self.auth_error.lock().unwrap() {
            self.state.lock().unwrap().create_count += 1;
            return Err(ExchangeError::new("AuthenticationError", "invalid api key"));
        }

        let client_id = params
            .get("newClientOrderId")
            .map(|v| match v {
                Value::String(s) => s.clone(),
                other => other.to_string(),
            })
            .unwrap_or_default();

        {
            let mut state = self.state.lock().unwrap();
            state.create_count += 1;
        }

        if self.behavior == Behavior::TimeoutNotReceived {
            return Err(ExchangeError::new(
                "TimeoutError",
                "request timed out before exchange receipt",
            ));
        }

        let count = self.state.lock().unwrap().create_count;
        let status = if self.behavior == Behavior::Partial {
            "PARTIALLY_FILLED"
        } else {
            "FILLED"
        };
        let filled = if self.behavior == Behavior::Partial {
            qty * 0.3
        } else {
            qty
        };
        let mut order = Map::new();
        order.insert("id".to_string(), Value::String(count.to_string()));
        order.insert(
            "clientOrderId".to_string(),
            Value::String(client_id.clone()),
        );
        order.insert("symbol".to_string(), Value::String(symbol.to_string()));
        order.insert("status".to_string(), Value::String(status.to_string()));
        order.insert("amount".to_string(), Value::from(qty));
        order.insert("filled".to_string(), Value::from(filled));
        order.insert("average".to_string(), Value::from(100.0));
        let order_val = Value::Object(order);
        self.orders
            .lock()
            .unwrap()
            .insert(client_id, order_val.clone());

        if self.behavior == Behavior::TimeoutAfterFill {
            let pos_side = if side == "buy" { "long" } else { "short" };
            let mut pos = Map::new();
            pos.insert("symbol".to_string(), Value::String(symbol.to_string()));
            pos.insert("side".to_string(), Value::String(pos_side.to_string()));
            pos.insert("contracts".to_string(), Value::from(filled));
            pos.insert("entryPrice".to_string(), Value::from(100.0));
            self.state.lock().unwrap().position = Some(Value::Object(pos));
            return Err(ExchangeError::new(
                "TimeoutError",
                "HTTP response lost after fill",
            ));
        }

        Ok(order_val)
    }

    async fn cancel_order(&self, order_id: &str, symbol: &str) -> Result<Value, ExchangeError> {
        self.state.lock().unwrap().cancel_count += 1;
        let mut out = Map::new();
        out.insert("id".to_string(), Value::String(order_id.to_string()));
        out.insert("symbol".to_string(), Value::String(symbol.to_string()));
        out.insert("status".to_string(), Value::String("canceled".to_string()));
        Ok(Value::Object(out))
    }
}

fn make_gateway(
    exchange: Arc<MockExchange>,
    dir: &std::path::Path,
) -> Arc<IdempotentOrderGateway<MockExchange>> {
    let path = dir.join("state.sqlite3");
    let store = SQLiteTradingStateStore::open(&path, Arc::new(SystemClock)).unwrap();
    let store_arc = Arc::new(Mutex::new(store));
    Arc::new(IdempotentOrderGateway::new(
        exchange,
        store_arc,
        vec![0.0],
        "cancel_remainder".to_string(),
    ))
}

#[tokio::test]
async fn test_same_signal_submits_only_once() {
    let dir = tempfile::tempdir().unwrap();
    let exchange = Arc::new(MockExchange::new(Behavior::Filled));
    let gateway = make_gateway(exchange.clone(), dir.path());

    let make_req = || EntryRequest {
        strategy: "UTB".to_string(),
        symbol: "BTC/USDT:USDT".to_string(),
        side: "LONG".to_string(),
        signal_timestamp: Value::from(1000),
        qty: 0.1,
        params: None,
        daily_loss_exempt: false,
    };

    let first = gateway.submit_entry(make_req()).await.unwrap();
    let second = gateway.submit_entry(make_req()).await.unwrap();

    assert!(first.accepted());
    assert!(second.recovered);
    assert_eq!(exchange.create_count(), 1);
}

#[tokio::test]
async fn test_timeout_after_exchange_fill_recovers_existing_order() {
    let dir = tempfile::tempdir().unwrap();
    let exchange = Arc::new(MockExchange::new(Behavior::TimeoutAfterFill));
    let gateway = make_gateway(exchange.clone(), dir.path());
    let result = gateway
        .submit_entry(EntryRequest {
            strategy: "UTB".to_string(),
            symbol: "BTC/USDT:USDT".to_string(),
            side: "LONG".to_string(),
            signal_timestamp: Value::from(1000),
            qty: 0.1,
            params: None,
            daily_loss_exempt: false,
        })
        .await
        .unwrap();
    assert!(result.recovered);
    assert_eq!(
        result.state,
        OrderState::FilledUnverifiedLiquidation.as_str()
    );
    assert_eq!(exchange.create_count(), 1);
}

#[tokio::test]
async fn test_timeout_without_exchange_receipt_remains_unknown_and_blocks() {
    let dir = tempfile::tempdir().unwrap();
    let exchange = Arc::new(MockExchange::new(Behavior::TimeoutNotReceived));
    let gateway = make_gateway(exchange.clone(), dir.path());
    let result = gateway
        .submit_entry(EntryRequest {
            strategy: "UTB".to_string(),
            symbol: "BTC/USDT:USDT".to_string(),
            side: "LONG".to_string(),
            signal_timestamp: Value::from(1000),
            qty: 0.1,
            params: None,
            daily_loss_exempt: false,
        })
        .await
        .unwrap();
    let second = gateway
        .submit_entry(EntryRequest {
            strategy: "UTB".to_string(),
            symbol: "ETH/USDT:USDT".to_string(),
            side: "LONG".to_string(),
            signal_timestamp: Value::from(1000),
            qty: 0.1,
            params: None,
            daily_loss_exempt: false,
        })
        .await
        .unwrap();
    assert_eq!(result.state, OrderState::SubmittedUnknown.as_str());
    assert_eq!(second.state, "BLOCKED");
    assert_eq!(exchange.create_count(), 1);
}

#[tokio::test]
async fn test_partial_fill_uses_actual_filled_quantity() {
    let dir = tempfile::tempdir().unwrap();
    let exchange = Arc::new(MockExchange::new(Behavior::Partial));
    let gateway = make_gateway(exchange.clone(), dir.path());
    let result = gateway
        .submit_entry(EntryRequest {
            strategy: "UTB".to_string(),
            symbol: "BTC/USDT:USDT".to_string(),
            side: "LONG".to_string(),
            signal_timestamp: Value::from(1000),
            qty: 1.0,
            params: None,
            daily_loss_exempt: false,
        })
        .await
        .unwrap();
    let record = gateway
        .store()
        .lock()
        .unwrap()
        .get(&result.client_order_id)
        .unwrap()
        .unwrap();
    assert_eq!(result.state, OrderState::PartiallyFilled.as_str());
    assert!((record.filled_qty - 0.3).abs() < 1e-9);
    let blocker = gateway
        .store()
        .lock()
        .unwrap()
        .entry_block_reason(None, false, false)
        .unwrap();
    assert!(blocker.is_some());
    assert_eq!(exchange.cancel_count(), 1);
}

#[tokio::test]
async fn test_reduce_only_close_is_idempotent_and_cannot_reverse() {
    let dir = tempfile::tempdir().unwrap();
    let exchange = Arc::new(MockExchange::new(Behavior::Filled));
    let mut pos = Map::new();
    pos.insert(
        "symbol".to_string(),
        Value::String("BTC/USDT:USDT".to_string()),
    );
    pos.insert("side".to_string(), Value::String("long".to_string()));
    pos.insert("contracts".to_string(), Value::from(0.1));
    pos.insert("entryPrice".to_string(), Value::from(100.0));
    exchange.set_position(Some(Value::Object(pos)));
    let gateway = make_gateway(exchange.clone(), dir.path());

    let make_req = || ReduceOnlyCloseRequest {
        strategy: "UTB".to_string(),
        symbol: "BTC/USDT:USDT".to_string(),
        position_side: "long".to_string(),
        position_signature: Value::String("entry-1".to_string()),
        qty: 0.1,
        reason: "manual".to_string(),
        params: None,
        order_intent: None,
        order_purpose: None,
        order_type: "market".to_string(),
        price: None,
        leg: None,
    };

    let first: OrderSubmissionResult = gateway.submit_reduce_only_close(make_req()).await.unwrap();
    let second = gateway.submit_reduce_only_close(make_req()).await.unwrap();
    let orders = exchange.orders_snapshot();
    let order = orders.get(&first.client_order_id).unwrap();
    let oid = order.get("clientOrderId").unwrap().as_str().unwrap();
    assert_eq!(oid, first.client_order_id);
    assert_eq!(exchange.create_count(), 1);
    assert!(second.recovered);
}

#[tokio::test]
async fn test_pre_submit_order_lookup_failure_blocks_without_order() {
    let dir = tempfile::tempdir().unwrap();
    let exchange = Arc::new(MockExchange::new(Behavior::Filled));
    exchange.set_failed_lookup(true);
    let gateway = make_gateway(exchange.clone(), dir.path());
    let result = gateway
        .submit_entry(EntryRequest {
            strategy: "UTB".to_string(),
            symbol: "BTC/USDT:USDT".to_string(),
            side: "LONG".to_string(),
            signal_timestamp: Value::from(1000),
            qty: 0.1,
            params: None,
            daily_loss_exempt: false,
        })
        .await
        .unwrap();
    assert_eq!(result.state, OrderState::SubmittedUnknown.as_str());
    assert_eq!(exchange.create_count(), 0);
    let state = gateway
        .store()
        .lock()
        .unwrap()
        .get_runtime_state("entry_lock_reason", None)
        .unwrap();
    assert_eq!(
        state,
        Some(Value::String("RECONCILIATION_REQUIRED".to_string()))
    );
}

#[tokio::test]
async fn test_authentication_error_persists_global_entry_lock() {
    let dir = tempfile::tempdir().unwrap();
    let exchange = Arc::new(MockExchange::new(Behavior::Filled));
    exchange.set_auth_error(true);
    let gateway = make_gateway(exchange.clone(), dir.path());
    let result = gateway
        .submit_entry(EntryRequest {
            strategy: "UTB".to_string(),
            symbol: "BTC/USDT:USDT".to_string(),
            side: "LONG".to_string(),
            signal_timestamp: Value::from(1000),
            qty: 0.1,
            params: None,
            daily_loss_exempt: false,
        })
        .await
        .unwrap();
    assert_eq!(result.state, OrderState::Failed.as_str());
    let lock = gateway
        .store()
        .lock()
        .unwrap()
        .get_runtime_state("entry_lock_reason", None)
        .unwrap();
    match lock {
        Some(Value::String(s)) => assert!(s.starts_with("AUTHENTICATION_ERROR")),
        _ => panic!("expected lock string"),
    }
}

fn today_utc() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64;
    let days = secs.div_euclid(86_400);
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    format!("{:04}-{:02}-{:02}", y, m, d)
}

#[tokio::test]
async fn test_persisted_daily_loss_lock_blocks_new_and_additional_orders() {
    let dir = tempfile::tempdir().unwrap();
    let exchange = Arc::new(MockExchange::new(Behavior::Filled));
    let gateway = make_gateway(exchange.clone(), dir.path());

    let mut lock = Map::new();
    lock.insert("date".to_string(), Value::String(today_utc()));
    lock.insert(
        "reason".to_string(),
        Value::String("DAILY_LOSS_LIMIT".to_string()),
    );
    gateway
        .store()
        .lock()
        .unwrap()
        .set_runtime_state("daily_loss_entry_lock", &Value::Object(lock))
        .unwrap();

    let entry = gateway
        .submit_entry(EntryRequest {
            strategy: "UTB".to_string(),
            symbol: "BTC/USDT:USDT".to_string(),
            side: "LONG".to_string(),
            signal_timestamp: Value::from(1000),
            qty: 0.1,
            params: None,
            daily_loss_exempt: false,
        })
        .await
        .unwrap();

    let mut pos = Map::new();
    pos.insert(
        "symbol".to_string(),
        Value::String("BTC/USDT:USDT".to_string()),
    );
    pos.insert("side".to_string(), Value::String("long".to_string()));
    pos.insert("contracts".to_string(), Value::from(0.1));
    pos.insert("entryPrice".to_string(), Value::from(100.0));
    exchange.set_position(Some(Value::Object(pos)));

    assert_eq!(entry.state, "BLOCKED");
    assert!(entry
        .error
        .as_deref()
        .unwrap_or("")
        .starts_with("DAILY_LOSS_LOCKED:"));
    assert_eq!(exchange.create_count(), 0);
}

#[tokio::test]
async fn test_small_account_trend_can_bypass_only_daily_loss_lock() {
    let dir = tempfile::tempdir().unwrap();
    let exchange = Arc::new(MockExchange::new(Behavior::Filled));
    let gateway = make_gateway(exchange.clone(), dir.path());

    let mut lock = Map::new();
    lock.insert("date".to_string(), Value::String(today_utc()));
    lock.insert(
        "reason".to_string(),
        Value::String("DAILY_LOSS_LIMIT".to_string()),
    );
    gateway
        .store()
        .lock()
        .unwrap()
        .set_runtime_state("daily_loss_entry_lock", &Value::Object(lock))
        .unwrap();

    let entry = gateway
        .submit_entry(EntryRequest {
            strategy: "adaptive_breakout_trend_v1".to_string(),
            symbol: "BTC/USDT:USDT".to_string(),
            side: "LONG".to_string(),
            signal_timestamp: Value::from(2000),
            qty: 0.1,
            params: None,
            daily_loss_exempt: true,
        })
        .await
        .unwrap();

    assert_ne!(entry.state, "BLOCKED");
    assert_eq!(exchange.create_count(), 1);
    let record = gateway
        .store()
        .lock()
        .unwrap()
        .get(&entry.client_order_id)
        .unwrap()
        .unwrap();
    assert_eq!(
        record.metadata.get("daily_loss_exempt"),
        Some(&Value::Bool(true))
    );
}

#[tokio::test]
async fn test_other_strategy_cannot_request_daily_loss_exemption() {
    let dir = tempfile::tempdir().unwrap();
    let exchange = Arc::new(MockExchange::new(Behavior::Filled));
    let gateway = make_gateway(exchange.clone(), dir.path());

    let mut lock = Map::new();
    lock.insert("date".to_string(), Value::String(today_utc()));
    lock.insert(
        "reason".to_string(),
        Value::String("DAILY_LOSS_LIMIT".to_string()),
    );
    gateway
        .store()
        .lock()
        .unwrap()
        .set_runtime_state("daily_loss_entry_lock", &Value::Object(lock))
        .unwrap();

    let entry = gateway
        .submit_entry(EntryRequest {
            strategy: "UTB".to_string(),
            symbol: "BTC/USDT:USDT".to_string(),
            side: "LONG".to_string(),
            signal_timestamp: Value::from(3000),
            qty: 0.1,
            params: None,
            daily_loss_exempt: true,
        })
        .await
        .unwrap();

    assert_eq!(entry.state, "BLOCKED");
    assert!(entry
        .error
        .as_deref()
        .unwrap_or("")
        .starts_with("DAILY_LOSS_LOCKED:"));
    assert_eq!(exchange.create_count(), 0);
}

#[test]
fn test_exchange_ack_cannot_regress_concurrent_fill_state() {
    let dir = tempfile::tempdir().unwrap();
    let exchange = Arc::new(MockExchange::new(Behavior::Filled));
    let gateway = make_gateway(exchange.clone(), dir.path());

    let record = gateway
        .store()
        .lock()
        .unwrap()
        .upsert(OrderRecord {
            client_order_id: "cid-race".to_string(),
            symbol: "BTC/USDT:USDT".to_string(),
            side: "LONG".to_string(),
            strategy: "UTB".to_string(),
            signal_timestamp: "1".to_string(),
            requested_qty: 1.0,
            order_state: OrderState::FilledUnverifiedLiquidation.as_str().to_string(),
            filled_qty: 1.0,
            average_fill_price: 100.0,
            ..Default::default()
        })
        .unwrap();

    let mut order = Map::new();
    order.insert("id".to_string(), Value::String("123".to_string()));
    order.insert("status".to_string(), Value::String("NEW".to_string()));
    order.insert("amount".to_string(), Value::from(1.0));
    order.insert("filled".to_string(), Value::from(0.0));
    order.insert("average".to_string(), Value::from(0.0));

    let updated = gateway
        .transition_from_exchange_order(&record.client_order_id, &Value::Object(order))
        .unwrap()
        .unwrap();

    assert_eq!(
        updated.order_state,
        OrderState::FilledUnverifiedLiquidation.as_str()
    );
    assert!((updated.filled_qty - 1.0).abs() < 1e-9);
    assert!((updated.average_fill_price - 100.0).abs() < 1e-9);
}

#[tokio::test]
async fn test_runtime_protection_lock_blocks_actual_exchange_entry_submission() {
    let dir = tempfile::tempdir().unwrap();
    let exchange = Arc::new(MockExchange::new(Behavior::Filled));
    let gateway = make_gateway(exchange.clone(), dir.path());
    gateway
        .store()
        .lock()
        .unwrap()
        .set_runtime_state(
            "entry_lock_reason",
            &Value::String("PENDING_PROTECTION_RECONCILIATION:BTC/USDT:USDT".to_string()),
        )
        .unwrap();

    let result = gateway
        .submit_entry(EntryRequest {
            strategy: "UTB".to_string(),
            symbol: "BTC/USDT:USDT".to_string(),
            side: "LONG".to_string(),
            signal_timestamp: Value::from(9001),
            qty: 0.1,
            params: None,
            daily_loss_exempt: false,
        })
        .await
        .unwrap();

    assert_eq!(result.state, "BLOCKED");
    assert!(result
        .error
        .as_deref()
        .unwrap_or("")
        .contains("PENDING_PROTECTION_RECONCILIATION"));
    assert_eq!(exchange.create_count(), 0);
}

#[tokio::test]
async fn store_failure_blocks_entry() {
    let dir = tempfile::tempdir().unwrap();
    let exchange = Arc::new(MockExchange::new(Behavior::Filled));
    let gateway = make_gateway(exchange.clone(), dir.path());

    // Break the underlying orders table so any store read/write fails.
    let path = dir.path().join("state.sqlite3");
    let conn = rusqlite::Connection::open(&path).unwrap();
    conn.execute_batch("DROP TABLE crypto_orders").unwrap();
    drop(conn);

    let result = gateway
        .submit_entry(EntryRequest {
            strategy: "UTB".to_string(),
            symbol: "BTC/USDT:USDT".to_string(),
            side: "LONG".to_string(),
            signal_timestamp: Value::from(1000),
            qty: 0.1,
            params: None,
            daily_loss_exempt: false,
        })
        .await;

    let err = result.expect_err("expected store failure to propagate");
    assert_eq!(err.class, "StoreError");
    assert_eq!(exchange.create_count(), 0);
}
