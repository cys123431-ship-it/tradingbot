//! Position add (averaging in) through the idempotent order gateway.

use std::sync::{Arc, Mutex};

use serde_json::{Map, Value};
use tbot::exchange::fake::{FakeBinance, FakeOptions};
use tbot::exchange::FuturesExchange;
use tbot::safety::order_gateway::{EntryRequest, IdempotentOrderGateway, PositionAddRequest};
use tbot::store::clock::SystemClock;
use tbot::store::order_state::{OrderState, SQLiteTradingStateStore};

fn make_gateway(
    exchange: Arc<FakeBinance>,
    dir: &std::path::Path,
) -> (
    Arc<IdempotentOrderGateway<FakeBinance>>,
    Arc<Mutex<SQLiteTradingStateStore>>,
) {
    let path = dir.join("state.sqlite3");
    let store = SQLiteTradingStateStore::open(&path, Arc::new(SystemClock)).unwrap();
    let store_arc = Arc::new(Mutex::new(store));
    let gateway = Arc::new(IdempotentOrderGateway::new(
        exchange,
        store_arc.clone(),
        vec![0.0],
        "cancel_remainder".to_string(),
    ));
    (gateway, store_arc)
}

fn entry_req() -> EntryRequest {
    EntryRequest {
        strategy: "SMA".to_string(),
        symbol: "BTC/USDT:USDT".to_string(),
        side: "long".to_string(),
        signal_timestamp: Value::from(1000),
        qty: 1.0,
        params: None,
        daily_loss_exempt: false,
    }
}

fn add_req() -> PositionAddRequest {
    PositionAddRequest {
        strategy: "SMA".to_string(),
        symbol: "BTC/USDT:USDT".to_string(),
        side: "long".to_string(),
        signal_timestamp: Value::from(2000),
        leg: 1,
        qty: 0.5,
    }
}

#[tokio::test]
async fn add_is_blocked_without_a_position() {
    let dir = tempfile::tempdir().unwrap();
    let exchange = Arc::new(FakeBinance::new(FakeOptions::default()));
    let (gateway, _store) = make_gateway(exchange.clone(), dir.path());

    let result = gateway.submit_position_add(add_req()).await.unwrap();
    assert_eq!(result.state, "BLOCKED");
    assert_eq!(result.error.as_deref(), Some("no position to add to"));
}

#[tokio::test]
async fn add_is_blocked_on_the_other_side() {
    let dir = tempfile::tempdir().unwrap();
    let exchange = Arc::new(FakeBinance::new(FakeOptions::default()));
    let (gateway, _store) = make_gateway(exchange.clone(), dir.path());

    let entry = gateway.submit_entry(entry_req()).await.unwrap();
    assert!(entry.accepted());

    let mut req = add_req();
    req.side = "short".to_string();
    let result = gateway.submit_position_add(req).await.unwrap();
    assert_eq!(result.state, "BLOCKED");
}

#[tokio::test]
async fn add_after_entry_increases_the_position_and_is_idempotent() {
    let dir = tempfile::tempdir().unwrap();
    let exchange = Arc::new(FakeBinance::new(FakeOptions::default()));
    let (gateway, store) = make_gateway(exchange.clone(), dir.path());

    let entry = gateway.submit_entry(entry_req()).await.unwrap();
    assert!(entry.accepted());

    // Entry record is protected: adds on the same symbol are allowed.
    {
        let mut store = store.lock().unwrap();
        store
            .transition(
                entry.client_order_id.as_str(),
                OrderState::Protected.as_str(),
                Map::new(),
            )
            .unwrap();
    }

    let first = gateway.submit_position_add(add_req()).await.unwrap();
    assert!(first.accepted(), "unexpected: {:?}", first.state);

    let positions = exchange.fetch_positions(None).await.unwrap();
    let contracts = positions
        .first()
        .and_then(|p| p.get("contracts"))
        .and_then(|v| v.as_f64())
        .unwrap();
    assert_eq!(contracts, 1.5);

    let record = {
        let store = store.lock().unwrap();
        store.get(first.client_order_id.as_str()).unwrap().unwrap()
    };
    assert_eq!(record.order_intent, "POSITION_ADD");
    assert_eq!(record.position_qty_before, 1.0);

    let second = gateway.submit_position_add(add_req()).await.unwrap();
    assert!(second.recovered);
    let positions = exchange.fetch_positions(None).await.unwrap();
    let contracts = positions
        .first()
        .and_then(|p| p.get("contracts"))
        .and_then(|v| v.as_f64())
        .unwrap();
    assert_eq!(contracts, 1.5);
}
