//! Ported from the Python options-service test-suite.
//!
//! Only tests that exercise the service itself are kept; tests that merely
//! grep Python sources, the Telegram controller tests and the pure
//! strategy/config tests live elsewhere.
#![allow(
    dead_code,
    unused_variables,
    unused_imports,
    clippy::manual_async_fn,
    clippy::derivable_impls,
    clippy::manual_range_contains
)]

use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, Mutex};

use chrono::Utc;
use serde_json::{json, Map, Value};

use crate::app::ipc::BoxFut;
use crate::core::candles::Candle;
use crate::exchange::binance_options::{
    ApiResult, OptionOrder, OptionsApi, OptionsApiError, OrderRef,
};
use crate::store::clock::{Clock, FixedClock};
use crate::strategy::options_config::{normalize_options_config, OPTIONS_CAPITAL_LIMIT_USDT};
use crate::strategy::options_service::{
    adaptive_trailing_exit, ConfigGetter, Notifier, OptionsService, Sleeper,
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// |a - b| <= 1e-9 * max(1, |b|)
fn approx(a: f64, b: f64) -> bool {
    (a - b).abs() <= 1e-9 * 1.0_f64.max(b.abs())
}

#[track_caller]
fn assert_approx(a: f64, b: f64) {
    assert!(approx(a, b), "expected {} ~= {}", a, b);
}

/// Port of the Python `_trend_rows` helper.
fn trend_rows(count: usize, slope: f64, interval_ms: i64) -> Vec<Candle> {
    let start = 100.0_f64;
    let mut rows = Vec::with_capacity(count);
    for i in 0..count {
        let fi = i as f64;
        let close = start + slope * fi + (((i % 5) as f64) - 2.0) * 0.04;
        rows.push([
            1_700_000_000_000.0 + (i as f64) * (interval_ms as f64),
            close - 0.1,
            close + 0.4,
            close - 0.4,
            close,
            1000.0,
        ]);
    }
    rows
}

fn now_ms_from(clock: &FixedClock) -> i64 {
    (clock.now_epoch_secs() * 1000.0).round() as i64
}

fn f64_of(v: &Value) -> f64 {
    match v {
        Value::Number(n) => n.as_f64().unwrap_or(0.0),
        Value::String(s) => s.parse::<f64>().unwrap_or(0.0),
        _ => 0.0,
    }
}

fn opt_str(pairs: &[(&str, &str)], key: &str) -> Option<String> {
    pairs
        .iter()
        .find(|(k, _)| *k == key)
        .map(|(_, v)| (*v).to_string())
}

// ---------------------------------------------------------------------------
// Fake options API client
// ---------------------------------------------------------------------------

#[derive(Default)]
struct FakeState {
    /// Positions reported by the fake `positions` endpoint.
    manual_positions: Value,
    /// Open orders reported by the fake `open_orders` endpoint.
    open_orders: Value,
    /// User trades reported by the fake `user_trades` endpoint.
    trades: Value,
    /// Configured query results keyed by client id / order id text.
    query_orders: HashMap<String, Value>,
    /// Exchange info rows (`optionSymbols` array); `None` builds a default row.
    exchange_info_rows: Option<Value>,
    /// Mark price rows (`None` builds the default ETH row).
    mark_rows: Option<Value>,
    /// Ticker rows (`None` builds the default ETH row).
    ticker_rows: Option<Value>,
    /// Depth snapshot (`None` builds the default ETH book).
    depth: Option<Value>,
    /// Margin account snapshot (`None` builds the default 21 USDT account).
    margin_account: Option<Value>,

    can_trade: bool,
    balance_available: String,
    balance_equity: String,

    /// New-order behaviour: `Fill` returns FILLED, `LeaveNew` returns NEW with
    /// zero fill, `Error` returns an API error.
    new_order_behaviour: NewOrderBehaviour,
    /// Orders that were cancelled by the fake (drives the `UnfilledMaker` query).
    cancelled: bool,

    // Call recorders.
    orders: Vec<OptionOrder>,
    mark_calls: Vec<Option<String>>,
    ticker_calls: Vec<Option<String>>,
    cancels: Vec<(String, String)>,
    queries: Vec<String>,
    any_calls: usize,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum NewOrderBehaviour {
    Fill,
    LeaveNew,
    Error,
}

impl Default for NewOrderBehaviour {
    fn default() -> Self {
        Self::Fill
    }
}

struct FakeClient {
    authenticated: bool,
    state: Mutex<FakeState>,
    clock: Arc<FixedClock>,
}

impl FakeClient {
    fn new(authenticated: bool, clock: Arc<FixedClock>) -> Self {
        Self {
            authenticated,
            clock,
            state: Mutex::new(FakeState {
                manual_positions: json!([]),
                open_orders: json!([]),
                trades: json!([]),
                query_orders: HashMap::new(),
                exchange_info_rows: None,
                mark_rows: None,
                ticker_rows: None,
                depth: None,
                margin_account: None,
                can_trade: true,
                balance_available: "21".to_string(),
                balance_equity: "21".to_string(),
                new_order_behaviour: NewOrderBehaviour::default(),
                cancelled: false,
                orders: Vec::new(),
                mark_calls: Vec::new(),
                ticker_calls: Vec::new(),
                cancels: Vec::new(),
                queries: Vec::new(),
                any_calls: 0,
            }),
        }
    }

    fn now_ms(&self) -> i64 {
        now_ms_from(&self.clock)
    }

    fn set_positions(&self, value: Value) {
        if let Ok(mut st) = self.state.lock() {
            st.manual_positions = value;
        }
    }

    fn set_open_orders(&self, value: Value) {
        if let Ok(mut st) = self.state.lock() {
            st.open_orders = value;
        }
    }

    fn set_trades(&self, value: Value) {
        if let Ok(mut st) = self.state.lock() {
            st.trades = value;
        }
    }

    fn set_query_order(&self, key: &str, value: Value) {
        if let Ok(mut st) = self.state.lock() {
            st.query_orders.insert(key.to_string(), value);
        }
    }

    fn set_can_trade(&self, value: bool) {
        if let Ok(mut st) = self.state.lock() {
            st.can_trade = value;
        }
    }

    fn set_balance(&self, available: &str, equity: &str) {
        if let Ok(mut st) = self.state.lock() {
            st.balance_available = available.to_string();
            st.balance_equity = equity.to_string();
        }
    }

    fn set_new_order_behaviour(&self, behaviour: NewOrderBehaviour) {
        if let Ok(mut st) = self.state.lock() {
            st.new_order_behaviour = behaviour;
        }
    }

    fn snapshot_orders(&self) -> Vec<OptionOrder> {
        match self.state.lock() {
            Ok(st) => st.orders.clone(),
            Err(_) => Vec::new(),
        }
    }

    fn total_calls(&self) -> usize {
        match self.state.lock() {
            Ok(st) => st.any_calls,
            Err(_) => 0,
        }
    }

    fn mark_calls(&self) -> Vec<Option<String>> {
        match self.state.lock() {
            Ok(st) => st.mark_calls.clone(),
            Err(_) => Vec::new(),
        }
    }

    fn ticker_calls(&self) -> Vec<Option<String>> {
        match self.state.lock() {
            Ok(st) => st.ticker_calls.clone(),
            Err(_) => Vec::new(),
        }
    }

    fn sells(&self) -> Vec<OptionOrder> {
        self.snapshot_orders()
            .into_iter()
            .filter(|o| o.side == "SELL")
            .collect()
    }

    fn bought_calls(&self, method: &str) -> usize {
        match method {
            "mark" => self.mark_calls().iter().filter(|s| s.is_some()).count(),
            "ticker" => self.ticker_calls().iter().filter(|s| s.is_some()).count(),
            _ => 0,
        }
    }
}

fn default_margin_account(can_trade: bool, available: &str, equity: &str) -> Value {
    json!({
        "canTrade": can_trade,
        "asset": [{"asset": "USDT", "available": available, "equity": equity}],
    })
}

impl FakeClient {
    /// Exchange info as seen by the fake, with `expiryDate` derived from the
    /// clock (mirrors the Python helper that used `time.time()`).
    fn exchange_info_value(&self) -> Value {
        let now = self.now_ms();
        let expiry = now + 5 * 86_400_000;
        let default_row = json!({
            "symbol": "ETH-TEST-185-C",
            "expiryDate": expiry,
            "status": "TRADING",
            "strikePrice": "185",
            "underlying": "ETHUSDT",
            "side": "CALL",
            "unit": 1,
            "minQty": "0.01",
            "contractType": "CRYPTO_OPTIONS",
            "underlyingType": "CRYPTO",
            "filters": [
                {"filterType": "LOT_SIZE", "minQty": "0.01", "stepSize": "0.01"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
            ],
        });
        let rows = match self.state.lock() {
            Ok(st) => st
                .exchange_info_rows
                .clone()
                .unwrap_or_else(|| json!([default_row.clone()])),
            Err(_) => json!([default_row]),
        };
        json!({"optionSymbols": rows})
    }
}

impl OptionsApi for FakeClient {
    fn authenticated(&self) -> bool {
        self.authenticated
    }

    async fn sync_time(&self) -> ApiResult {
        Ok(json!({"serverTime": self.now_ms()}))
    }

    async fn ping(&self) -> ApiResult {
        Ok(json!({}))
    }

    async fn exchange_info(&self) -> ApiResult {
        Ok(self.exchange_info_value())
    }

    async fn mark_price(&self, symbol: Option<&str>) -> ApiResult {
        let mark_price = {
            let mut st = match self.state.lock() {
                Ok(st) => st,
                Err(_) => return Err(OptionsApiError::new("LOCK_POISONED")),
            };
            st.any_calls += 1;
            st.mark_calls.push(symbol.map(|s| s.to_string()));
            let manual = matches!(&st.manual_positions, Value::Array(rows) if !rows.is_empty());
            if manual {
                "9.9".to_string()
            } else {
                "0.49".to_string()
            }
        };
        let base = json!({
            "symbol": "ETH-TEST-185-C",
            "markPrice": mark_price,
            "markIV": "0.02",
            "delta": "0.45",
        });
        match symbol {
            None => Ok(json!([base])),
            Some(sym) => {
                let mut row = base.clone();
                if let Some(obj) = row.as_object_mut() {
                    obj.insert("symbol".to_string(), Value::String(sym.to_string()));
                }
                Ok(json!([row]))
            }
        }
    }

    async fn ticker(&self, symbol: Option<&str>) -> ApiResult {
        {
            let mut st = match self.state.lock() {
                Ok(st) => st,
                Err(_) => return Err(OptionsApiError::new("LOCK_POISONED")),
            };
            st.any_calls += 1;
            st.ticker_calls.push(symbol.map(|s| s.to_string()));
        }
        let base = json!({
            "symbol": "ETH-TEST-185-C",
            "amount": "1000",
            "bidPrice": "0.48",
            "askPrice": "0.50",
        });
        match symbol {
            None => Ok(json!([base])),
            Some(sym) => {
                let mut row = base.clone();
                if let Some(obj) = row.as_object_mut() {
                    obj.insert("symbol".to_string(), Value::String(sym.to_string()));
                }
                Ok(json!([row]))
            }
        }
    }

    async fn depth(&self, symbol: &str, _limit: u32) -> ApiResult {
        let configured = match self.state.lock() {
            Ok(st) => st.depth.clone(),
            Err(_) => None,
        };
        match configured {
            Some(d) => Ok(d),
            None => Ok(json!({
                "bids": [["0.48", "10"]],
                "asks": [["0.50", "10"]],
            })),
        }
    }

    async fn recent_trades(&self, _symbol: &str, _limit: u32) -> ApiResult {
        Ok(json!([]))
    }

    async fn margin_account(&self) -> ApiResult {
        let snapshot = {
            let st = match self.state.lock() {
                Ok(st) => st,
                Err(_) => return Err(OptionsApiError::new("LOCK_POISONED")),
            };
            (
                st.can_trade,
                st.balance_available.clone(),
                st.balance_equity.clone(),
                st.margin_account.clone(),
            )
        };
        let (can_trade, available, equity, configured) = snapshot;
        match configured {
            Some(v) => Ok(v),
            None => Ok(default_margin_account(can_trade, &available, &equity)),
        }
    }

    async fn positions(&self, _symbol: Option<&str>) -> ApiResult {
        match self.state.lock() {
            Ok(st) => Ok(st.manual_positions.clone()),
            Err(_) => Ok(json!([])),
        }
    }

    async fn open_orders(&self, _symbol: Option<&str>) -> ApiResult {
        match self.state.lock() {
            Ok(st) => Ok(st.open_orders.clone()),
            Err(_) => Ok(json!([])),
        }
    }

    async fn query_order(&self, symbol: &str, order: &OrderRef) -> ApiResult {
        let key = match order {
            OrderRef::Client(id) => id.clone(),
            OrderRef::Id(v) => match v {
                Value::String(s) => s.clone(),
                Value::Number(n) => n.to_string(),
                other => other.to_string(),
            },
        };
        let found = {
            let mut st = match self.state.lock() {
                Ok(st) => st,
                Err(_) => return Err(OptionsApiError::new("LOCK_POISONED")),
            };
            st.any_calls += 1;
            st.queries.push(key.clone());
            st.query_orders.get(&key).cloned()
        };
        match found {
            Some(v) => Ok(v),
            None => Err(OptionsApiError::new("UNEXPECTED_QUERY")),
        }
    }

    async fn new_order(&self, order: &OptionOrder) -> ApiResult {
        let behaviour = {
            let mut st = match self.state.lock() {
                Ok(st) => st,
                Err(_) => return Err(OptionsApiError::new("LOCK_POISONED")),
            };
            st.any_calls += 1;
            st.orders.push(order.clone());
            st.new_order_behaviour
        };
        match behaviour {
            NewOrderBehaviour::Fill => Ok(json!({
                "symbol": order.symbol,
                "status": "FILLED",
                "executedQty": order.quantity,
                "avgPrice": order.price,
                "orderId": 123,
                "clientOrderId": order.client_order_id,
            })),
            NewOrderBehaviour::LeaveNew => Ok(json!({
                "symbol": order.symbol,
                "status": "NEW",
                "executedQty": "0",
                "orderId": 123,
                "clientOrderId": order.client_order_id,
            })),
            NewOrderBehaviour::Error => Err(OptionsApiError::new("ORDER_REJECTED")),
        }
    }

    async fn cancel_order(&self, symbol: &str, order: &OrderRef) -> ApiResult {
        let key = match order {
            OrderRef::Client(id) => id.clone(),
            OrderRef::Id(v) => match v {
                Value::String(s) => s.clone(),
                Value::Number(n) => n.to_string(),
                other => other.to_string(),
            },
        };
        {
            let mut st = match self.state.lock() {
                Ok(st) => st,
                Err(_) => return Err(OptionsApiError::new("LOCK_POISONED")),
            };
            st.any_calls += 1;
            st.cancelled = true;
            st.cancels.push((symbol.to_string(), key));
        }
        Ok(json!({"symbol": symbol, "status": "CANCELED"}))
    }

    async fn user_trades(&self, _symbol: &str, _start_time: Option<i64>, _limit: u32) -> ApiResult {
        match self.state.lock() {
            Ok(st) => Ok(st.trades.clone()),
            Err(_) => Ok(json!([])),
        }
    }

    async fn exercise_records(
        &self,
        _symbol: Option<&str>,
        _start_time: Option<i64>,
        _limit: u32,
    ) -> ApiResult {
        Ok(json!([]))
    }

    async fn underlying_klines(
        &self,
        _symbol: &str,
        interval: &str,
        _limit: u32,
    ) -> Result<Vec<Candle>, OptionsApiError> {
        Ok(fake_market(interval))
    }
}

/// Port of the Python `_FakeMarket.fetch_ohlcv`.
fn fake_market(timeframe: &str) -> Vec<Candle> {
    if timeframe == "4h" {
        trend_rows(160, 1.2, 14_400_000)
    } else {
        trend_rows(240, 0.35, 3_600_000)
    }
}

// ---------------------------------------------------------------------------
// Service builder helpers
// ---------------------------------------------------------------------------

struct Harness {
    svc: OptionsService<FakeClient>,
    client: Arc<FakeClient>,
    clock: Arc<FixedClock>,
    notifications: Arc<Mutex<Vec<String>>>,
    sleeps: Arc<Mutex<Vec<f64>>>,
}

fn service_from_config(dir: &Path, cfg: Value) -> Harness {
    std::fs::create_dir_all(dir).unwrap();
    let state_path = dir.join("options_state.json");
    let clock = Arc::new(FixedClock::new(Utc::now(), Utc::now().timestamp() as f64));
    let client = Arc::new(FakeClient::new(true, clock.clone()));
    let notifications: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
    let sleeps: Arc<Mutex<Vec<f64>>> = Arc::new(Mutex::new(Vec::new()));

    let cfg_getter: ConfigGetter = {
        let value = cfg.clone();
        Arc::new(move || value.clone())
    };
    let notifier: Notifier = {
        let out = notifications.clone();
        Arc::new(move |text: String| -> BoxFut<()> {
            if let Ok(mut guard) = out.lock() {
                guard.push(text);
            }
            Box::pin(async {})
        })
    };
    let sleeper: Sleeper = {
        let out = sleeps.clone();
        Arc::new(move |secs: f64| -> BoxFut<()> {
            if let Ok(mut guard) = out.lock() {
                guard.push(secs);
            }
            Box::pin(async {})
        })
    };

    let svc = OptionsService::new(
        cfg_getter,
        client.clone(),
        state_path,
        Some(notifier),
        clock.clone(),
        sleeper,
    );
    Harness {
        svc,
        client,
        clock,
        notifications,
        sleeps,
    }
}

fn default_cfg(enabled: bool) -> Value {
    json!({
        "enabled": enabled,
        "underlyings": ["ETHUSDT"],
        "min_abs_signal": 0.45,
        "min_quote_volume_usdt": 1,
    })
}

fn service(dir: &Path, enabled: bool) -> Harness {
    service_from_config(dir, default_cfg(enabled))
}

fn tempdir() -> std::path::PathBuf {
    let base = std::env::temp_dir();
    let uniq = format!(
        "opt_svc_test_{}_{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    );
    let path = base.join(uniq);
    std::fs::create_dir_all(&path).unwrap();
    path
}

fn st_get(svc: &OptionsService<FakeClient>, key: &str) -> Value {
    svc.st_get(key)
}

fn set_state(svc: &OptionsService<FakeClient>, key: &str, value: Value) {
    svc.st_set(key, value);
    svc.save_state().unwrap();
}

fn active_position<C: OptionsApi>(svc: &OptionsService<C>) -> Value {
    svc.st_get("active_position")
}

fn position_symbol(svc: &OptionsService<FakeClient>) -> Option<String> {
    match active_position(svc) {
        Value::Object(map) => map.get("symbol").and_then(|v| v.as_str()).map(String::from),
        _ => None,
    }
}

// ---------------------------------------------------------------------------
// test_options_trading.py
// ---------------------------------------------------------------------------

#[tokio::test]
async fn options_runtime_defaults_to_no_order_when_disabled() {
    let dir = tempdir();
    let h = service(&dir, false);
    let result = h.svc.run_cycle(true, false).await;
    assert_eq!(result["action"], "waiting");
    assert_eq!(h.client.total_calls(), 0);
}

#[tokio::test]
async fn options_runtime_enters_buy_only_with_live_balance_below_hundred_cap() {
    let dir = tempdir();
    let h = service(&dir, true);
    let result = h.svc.run_cycle(true, false).await;
    assert_eq!(result["action"], "entered");

    let orders = h.client.snapshot_orders();
    assert_eq!(orders.len(), 1);
    let order = &orders[0];
    assert_eq!(order.side, "BUY");
    assert!(!order.reduce_only);
    assert_eq!(order.time_in_force.as_deref(), Some("GTC"));
    assert!(order.post_only);

    let position = active_position(&h.svc);
    let entry_total = f64_of(&position["entry_total_usdt"]);
    assert!(entry_total <= 21.0);
    assert!(entry_total <= OPTIONS_CAPITAL_LIMIT_USDT);

    let bankroll = f64_of(&st_get(&h.svc, "cash_bankroll_usdt"));
    assert!(bankroll >= 0.0 && bankroll < OPTIONS_CAPITAL_LIMIT_USDT);

    let mark_calls = h.client.mark_calls();
    assert!(mark_calls.iter().any(|s| s.is_none()));
    let ticker_calls = h.client.ticker_calls();
    assert!(ticker_calls.iter().any(|s| s.is_none()));
    assert_eq!(h.client.bought_calls("mark"), 0);
    assert_eq!(h.client.bought_calls("ticker"), 0);
}

#[tokio::test]
async fn manual_force_scan_has_cooldown_to_prevent_api_bursts() {
    let dir = tempdir();
    let h = service(&dir, true);
    let first = h.svc.run_cycle(true, false).await;
    assert_eq!(first["action"], "entered");
    set_state(&h.svc, "active_position", Value::Null);

    let second = h.svc.run_cycle(true, false).await;
    assert_eq!(second["action"], "waiting");
    let reason = second["reason"].as_str().unwrap_or("").to_string();
    assert!(
        reason.contains("MANUAL_SCAN_COOLDOWN"),
        "reason: {}",
        reason
    );
}

#[tokio::test]
async fn pending_bot_entry_is_recovered_by_client_order_id() {
    let dir = tempdir();
    let h = service(&dir, true);
    let symbol = "ETH-TEST-185-C";
    h.client
        .set_positions(json!([{"symbol": symbol, "quantity": "1"}]));

    let now = now_ms_from(&h.clock);
    let selected = json!({
        "symbol": symbol,
        "underlying": "ETHUSDT",
        "side": "CALL",
        "unit": 1,
        "tick_size": 0.1,
        "expiryDate": now + 5 * 86_400_000,
        "signal": {"strategy": "ADAPTIVE_TREND", "spot_price": 100},
    });
    let pending = h
        .svc
        .pending_entry_value(&selected, "1", 10.0, "tboptb-recovery", "IOC");
    set_state(&h.svc, "pending_entry", pending);
    h.client.set_query_order(
        "tboptb-recovery",
        json!({
            "symbol": symbol,
            "status": "FILLED",
            "executedQty": "1",
            "avgPrice": "10",
            "orderId": 555,
            "clientOrderId": "tboptb-recovery",
        }),
    );

    let result = h.svc.recover_runtime_ownership().await;
    let result = result.expect("expected recovery value");
    assert_eq!(result["action"], "recovered");
    assert!(st_get(&h.svc, "pending_entry").is_null());
    assert_eq!(position_symbol(&h.svc).as_deref(), Some(symbol));
    assert_eq!(active_position(&h.svc)["recovered_after_restart"], true);
}

#[tokio::test]
async fn pending_exit_is_booked_without_submitting_duplicate_sell() {
    let dir = tempdir();
    let h = service(&dir, false);
    let symbol = "ETH-TEST-185-C";
    let now = now_ms_from(&h.clock);
    set_state(&h.svc, "cash_bankroll_usdt", json!(0.0));
    let position = json!({
        "symbol": symbol,
        "side": "CALL",
        "quantity": 1.0,
        "original_quantity": 1.0,
        "entry_price": 10.0,
        "entry_total_usdt": 10.0,
        "entry_time_ms": now,
        "expiry_date_ms": now + 5 * 86_400_000,
        "unit": 1.0,
        "tick_size": 0.1,
        "peak_mark": 10.0,
    });
    set_state(&h.svc, "active_position", position);
    set_state(
        &h.svc,
        "pending_exit",
        json!({
            "symbol": symbol,
            "quantity": 1.0,
            "limit_price": 12.0,
            "client_order_id": "tbopts-recovery",
            "reason": "OPTION_PREMIUM_TARGET",
        }),
    );
    h.client.set_query_order(
        "tbopts-recovery",
        json!({
            "symbol": symbol,
            "status": "FILLED",
            "executedQty": "1",
            "avgPrice": "12",
            "orderId": 777,
            "clientOrderId": "tbopts-recovery",
        }),
    );

    let result = h.svc.run_cycle(false, false).await;
    assert_eq!(result["action"], "exited");
    assert!(st_get(&h.svc, "pending_exit").is_null());
    assert!(active_position(&h.svc).is_null());
    assert!(h.client.snapshot_orders().is_empty());
}

#[tokio::test]
async fn options_runtime_blocks_entry_when_any_manual_option_position_exists() {
    let dir = tempdir();
    let h = service(&dir, true);
    h.client
        .set_positions(json!([{"symbol": "BTC-MANUAL-C", "quantity": "0.01"}]));
    let result = h.svc.run_cycle(true, false).await;
    assert_eq!(result["action"], "waiting");
    assert!(h.client.snapshot_orders().is_empty());
    let reason = result["reason"].as_str().unwrap_or("").to_string();
    assert!(reason.contains("기존 옵션 포지션"), "reason: {}", reason);
}

#[tokio::test]
async fn options_runtime_reconciles_manual_partial_close_once() {
    let dir = tempdir();
    let h = service(&dir, false);
    let symbol = "ETH-TEST-185-C";
    let now = now_ms_from(&h.clock);
    set_state(&h.svc, "cash_bankroll_usdt", json!(10.0));
    let position = json!({
        "symbol": symbol,
        "side": "CALL",
        "quantity": 1.0,
        "original_quantity": 1.0,
        "entry_price": 10.0,
        "entry_total_usdt": 10.0,
        "entry_time_ms": now,
        "expiry_date_ms": now + 86_400_000,
        "unit": 1.0,
        "tick_size": 0.1,
        "peak_mark": 10.0,
    });
    set_state(&h.svc, "active_position", position);
    h.client
        .set_positions(json!([{"symbol": symbol, "quantity": "0.5"}]));
    h.client.set_trades(json!([{
        "id": 77,
        "orderId": 900,
        "symbol": symbol,
        "side": "SELL",
        "quantity": "0.5",
        "price": "12",
        "fee": "0.1",
    }]));

    let result = h.svc.run_cycle(true, false).await;
    assert_eq!(result["action"], "managed");

    let active = active_position(&h.svc);
    assert_approx(f64_of(&active["quantity"]), 0.5);
    assert_approx(f64_of(&st_get(&h.svc, "cash_bankroll_usdt")), 15.9);
    assert_approx(f64_of(&active["partial_exit_proceeds_usdt"]), 6.0);
    assert_eq!(active["processed_exit_trade_ids"], json!(["77"]));
}

#[tokio::test]
async fn underlying_signal_uses_only_completed_candles() {
    let dir = tempdir();
    let h = service(&dir, true);
    let rows = trend_rows(240, 0.35, 3_600_000);
    let cfg = normalize_options_config(&json!({}));

    let result = h
        .svc
        .underlying_signal("ETHUSDT", &cfg)
        .await
        .expect("underlying signal");

    let last_completed_ts = rows[rows.len() - 2][0];
    assert_approx(f64_of(&result["signal_bar_ts"]), last_completed_ts);
    let signal_key = result["signal_key"].as_str().unwrap_or("").to_string();
    let ts_text = format!("{}", last_completed_ts.round() as i64);
    assert!(
        signal_key.ends_with(&ts_text),
        "signal_key {} should end with {}",
        signal_key,
        ts_text
    );
}

// ---------------------------------------------------------------------------
// test_options_adaptive.py
// ---------------------------------------------------------------------------

#[tokio::test]
async fn can_trade_and_open_orders_remain_fail_closed() {
    let dir = tempdir();
    let h = service(&dir, true);
    h.client.set_can_trade(false);
    let result = h.svc.run_cycle(true, false).await;
    assert_eq!(result["action"], "blocked");
    assert!(h.client.snapshot_orders().is_empty());
    let outcomes = st_get(&h.svc, "recent_scan_outcomes");
    assert_eq!(
        outcomes.as_array().and_then(|a| a.last()).cloned(),
        Some(json!("CAN_TRADE"))
    );

    let dir2 = dir.join("orders");
    let h2 = service(&dir2, true);
    h2.client
        .set_open_orders(json!([{"symbol": "ETH-MANUAL-C", "status": "NEW"}]));
    let result2 = h2.svc.run_cycle(true, false).await;
    assert_eq!(result2["action"], "waiting");
    assert!(h2.client.snapshot_orders().is_empty());
    let outcomes2 = st_get(&h2.svc, "recent_scan_outcomes");
    assert_eq!(
        outcomes2.as_array().and_then(|a| a.last()).cloned(),
        Some(json!("OPEN_ORDER"))
    );
}

#[tokio::test]
async fn budget_filter_skips_unaffordable_contract_before_selection() {
    let dir = tempdir();
    let h = adaptive_service(&dir);
    let result = h.svc.run_cycle(true, false).await;
    assert_eq!(result["action"], "entered", "{result}");
    let orders = h.client.orders.lock().unwrap().clone();
    assert_eq!(orders.len(), 1);
    let order = &orders[0];
    assert_eq!(order.symbol, "ETH-AFF-C");
    assert_eq!(order.side, "BUY");
    assert!(!order.reduce_only);

    let entry_total = f64_of(&active_position(&h.svc)["entry_total_usdt"]);
    assert!(entry_total <= 24.0_f64.min(OPTIONS_CAPITAL_LIMIT_USDT));

    let diagnostics = st_get_any(&h.svc, "last_scan_diagnostics");
    let rejections = &diagnostics["contract_rejections"];
    let edge_rejections = rejections
        .get("OPTION_NET_EXPECTED_EDGE_TOO_LOW")
        .map(|v| v.as_u64().unwrap_or(0))
        .unwrap_or(0);
    assert!(
        edge_rejections >= 1,
        "expected OPTION_NET_EXPECTED_EDGE_TOO_LOW >= 1, got {}",
        edge_rejections
    );
    let outcomes = st_get_any(&h.svc, "recent_scan_outcomes");
    assert_eq!(
        outcomes.as_array().and_then(|a| a.last()).cloned(),
        Some(json!("ORDERABLE_CANDIDATE"))
    );
}

#[tokio::test]
async fn rejection_stats_are_bounded_and_exposed() {
    let dir = tempdir();
    let h = service(&dir, false);
    h.svc.record_scan_outcome("DIRECTION_SIGNAL", None);
    h.svc.record_scan_outcome("BUDGET", None);
    h.svc.record_scan_outcome("BUDGET", None);
    let status = h.svc.status_snapshot(false).await;
    assert_eq!(status["scan_outcomes_window"], 3);
    assert_eq!(status["scan_rejection_stats"]["DIRECTION_SIGNAL"], 1);
    assert_eq!(status["scan_rejection_stats"]["BUDGET"], 2);
}

// ---------------------------------------------------------------------------
// test_options_convexity_v2.py
// ---------------------------------------------------------------------------

#[tokio::test]
async fn unfilled_maker_is_canceled_and_does_not_consume_signal() {
    let dir = tempdir();
    let cfg = json!({
        "enabled": true,
        "maker_first_enabled": true,
        "maker_wait_seconds": 0.5,
    });
    let h = service_from_config(&dir, cfg);
    h.client
        .set_new_order_behaviour(NewOrderBehaviour::LeaveNew);

    let selected = json!({
        "symbol": "BTC-X-C",
        "underlying": "BTCUSDT",
        "side": "CALL",
        "expiryDate": 2_000_000_000_000_i64,
        "unit": 1,
        "min_qty": 0.01,
        "step_size": 0.01,
        "tick_size": 0.1,
        "bid": 0.48,
        "ask": 0.50,
        "entry_fraction": 0.70,
        "ioc_eligible": false,
        "signal": {
            "signal_key": "BTC:ADAPTIVE_TREND:CALL:1",
            "spot_price": 100.0,
            "score": 0.8,
        },
    });
    let snapshot = json!({"balance": {"available": 100.0}});
    let result = h.svc.enter(selected, snapshot).await;

    assert_eq!(result["action"], "waiting");
    let cancels = match h.client.state.lock() {
        Ok(st) => st.cancels.clone(),
        Err(_) => Vec::new(),
    };
    assert!(!cancels.is_empty(), "expected at least one cancel");

    let consumed = st_get(&h.svc, "consumed_signal_keys");
    match consumed {
        Value::Array(rows) => assert!(rows.is_empty(), "consumed_signal_keys should be empty"),
        Value::Null => {}
        other => panic!("unexpected consumed_signal_keys: {}", other),
    }
    assert!(active_position(&h.svc).is_null());
}

#[test]
fn convex_trailing_tiers() {
    let cases: [(f64, f64, bool); 4] = [
        (1.49, 1.0, false),
        (1.50, 1.09, true),
        (2.0, 1.34, true),
        (3.0, 2.09, true),
    ];
    for (peak, mark, expected) in cases.iter() {
        let got = adaptive_trailing_exit(1.0, *peak, *mark);
        assert_eq!(got, *expected, "peak={} mark={}", peak, mark);
    }
}

// ---------------------------------------------------------------------------
// test_options_btc_only.py (service-side tests only)
// ---------------------------------------------------------------------------

const BTC_SYMBOL: &str = "BTC-TEST-60000-C";

/// Configurable book fake for the btc_only tests.
struct BookClient {
    authenticated: bool,
    mark: f64,
    bid: f64,
    clock: Arc<FixedClock>,
    state: Mutex<FakeState>,
}

impl BookClient {
    fn new(mark: f64, bid: f64, clock: Arc<FixedClock>) -> Self {
        Self {
            authenticated: true,
            mark,
            bid,
            clock,
            state: Mutex::new(FakeState {
                manual_positions: json!([{"symbol": BTC_SYMBOL, "quantity": "1"}]),
                open_orders: json!([]),
                trades: json!([]),
                query_orders: HashMap::new(),
                exchange_info_rows: None,
                mark_rows: None,
                ticker_rows: None,
                depth: None,
                margin_account: None,
                can_trade: true,
                balance_available: "21".to_string(),
                balance_equity: "21".to_string(),
                new_order_behaviour: NewOrderBehaviour::Fill,
                cancelled: false,
                orders: Vec::new(),
                mark_calls: Vec::new(),
                ticker_calls: Vec::new(),
                cancels: Vec::new(),
                queries: Vec::new(),
                any_calls: 0,
            }),
        }
    }

    fn orders_snapshot(&self) -> Vec<OptionOrder> {
        match self.state.lock() {
            Ok(st) => st.orders.clone(),
            Err(_) => Vec::new(),
        }
    }
}

impl OptionsApi for BookClient {
    fn authenticated(&self) -> bool {
        self.authenticated
    }
    async fn sync_time(&self) -> ApiResult {
        Ok(json!({"serverTime": now_ms_from(&self.clock)}))
    }
    async fn ping(&self) -> ApiResult {
        Ok(json!({}))
    }
    async fn exchange_info(&self) -> ApiResult {
        let now = now_ms_from(&self.clock);
        Ok(json!({
            "optionSymbols": [{
                "symbol": BTC_SYMBOL,
                "expiryDate": now + 30 * 86_400_000_i64,
                "status": "TRADING",
                "strikePrice": "60000",
                "underlying": "BTCUSDT",
                "side": "CALL",
                "unit": 1,
                "minQty": "0.01",
                "contractType": "CRYPTO_OPTIONS",
                "underlyingType": "CRYPTO",
                "filters": [
                    {"filterType": "LOT_SIZE", "minQty": "0.01", "stepSize": "0.01"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                ],
            }]
        }))
    }
    async fn mark_price(&self, symbol: Option<&str>) -> ApiResult {
        if let Ok(mut st) = self.state.lock() {
            st.mark_calls.push(symbol.map(|s| s.to_string()));
            st.any_calls += 1;
        }
        Ok(json!([{
            "symbol": symbol.unwrap_or(BTC_SYMBOL),
            "markPrice": format!("{}", self.mark),
            "markIV": "0.5",
            "delta": "0.5",
        }]))
    }
    async fn ticker(&self, symbol: Option<&str>) -> ApiResult {
        if let Ok(mut st) = self.state.lock() {
            st.ticker_calls.push(symbol.map(|s| s.to_string()));
            st.any_calls += 1;
        }
        Ok(json!([{
            "symbol": symbol.unwrap_or(BTC_SYMBOL),
            "amount": "1000",
            "bidPrice": format!("{}", self.bid),
            "askPrice": format!("{}", self.mark + 1.0),
        }]))
    }
    async fn depth(&self, _symbol: &str, _limit: u32) -> ApiResult {
        Ok(json!({
            "bids": [[format!("{}", self.bid), "5"]],
            "asks": [[format!("{}", self.mark + 1.0), "5"]],
        }))
    }
    async fn recent_trades(&self, _symbol: &str, _limit: u32) -> ApiResult {
        Ok(json!([]))
    }
    async fn margin_account(&self) -> ApiResult {
        Ok(default_margin_account(true, "21", "21"))
    }
    async fn positions(&self, _symbol: Option<&str>) -> ApiResult {
        Ok(json!([{"symbol": BTC_SYMBOL, "quantity": "1"}]))
    }
    async fn open_orders(&self, _symbol: Option<&str>) -> ApiResult {
        Ok(json!([]))
    }
    async fn query_order(&self, _symbol: &str, _order: &OrderRef) -> ApiResult {
        Err(OptionsApiError::new("UNEXPECTED_QUERY"))
    }
    async fn new_order(&self, order: &OptionOrder) -> ApiResult {
        if let Ok(mut st) = self.state.lock() {
            st.orders.push(order.clone());
            st.any_calls += 1;
        }
        Ok(json!({
            "symbol": order.symbol,
            "status": "FILLED",
            "executedQty": order.quantity,
            "avgPrice": order.price,
            "orderId": 1,
            "clientOrderId": order.client_order_id,
        }))
    }
    async fn cancel_order(&self, symbol: &str, _order: &OrderRef) -> ApiResult {
        if let Ok(mut st) = self.state.lock() {
            st.cancels.push((symbol.to_string(), String::new()));
            st.any_calls += 1;
        }
        Ok(json!({"symbol": symbol, "status": "CANCELED"}))
    }
    async fn user_trades(&self, _symbol: &str, _start_time: Option<i64>, _limit: u32) -> ApiResult {
        Ok(json!([]))
    }
    async fn exercise_records(
        &self,
        _symbol: Option<&str>,
        _start_time: Option<i64>,
        _limit: u32,
    ) -> ApiResult {
        Ok(json!([]))
    }
    async fn underlying_klines(
        &self,
        _symbol: &str,
        interval: &str,
        _limit: u32,
    ) -> Result<Vec<Candle>, OptionsApiError> {
        Ok(fake_market(interval))
    }
}

struct BookHarness {
    svc: OptionsService<BookClient>,
    client: Arc<BookClient>,
    clock: Arc<FixedClock>,
}

fn book_service(dir: &Path, cfg: Value, mark: f64, bid: f64) -> BookHarness {
    std::fs::create_dir_all(dir).unwrap();
    let state_path = dir.join("options_state.json");
    let clock = Arc::new(FixedClock::new(Utc::now(), Utc::now().timestamp() as f64));
    let client = Arc::new(BookClient::new(mark, bid, clock.clone()));
    let cfg_getter: ConfigGetter = {
        let value = cfg.clone();
        Arc::new(move || value.clone())
    };
    let sleeper: Sleeper = Arc::new(|_secs: f64| -> BoxFut<()> { Box::pin(async {}) });
    let svc = OptionsService::new(
        cfg_getter,
        client.clone(),
        state_path,
        None,
        clock.clone(),
        sleeper,
    );

    let now = now_ms_from(&clock);
    svc.st_set("cash_bankroll_usdt", json!(0.0));
    svc.st_set(
        "active_position",
        json!({
            "symbol": BTC_SYMBOL,
            "side": "CALL",
            "quantity": 1.0,
            "original_quantity": 1.0,
            "entry_price": 10.0,
            "entry_total_usdt": 10.0,
            "entry_time_ms": now,
            "expiry_date_ms": now + 30 * 86_400_000_i64,
            "unit": 1.0,
            "tick_size": 0.1,
            "peak_mark": 10.0,
        }),
    );
    svc.save_state().unwrap();

    BookHarness { svc, client, clock }
}

fn btc_sells(client: &BookClient) -> Vec<OptionOrder> {
    client
        .orders_snapshot()
        .into_iter()
        .filter(|o| o.side == "SELL")
        .collect()
}

fn base_cfg_btc() -> Value {
    json!({"btc_only": true, "take_profit_pct": 1.0, "trail_activation_pct": 3.0})
}

#[tokio::test]
async fn mark_target_without_bid_does_not_fire_in_btc_mode() {
    let dir = tempdir();
    let h = book_service(&dir, base_cfg_btc(), 21.0, 15.0);
    let result = h.svc.manage_base(false, None).await;
    assert_eq!(result["action"], "managed");
    assert!(btc_sells(&h.client).is_empty());
    let position = active_position(&h.svc);
    assert_approx(f64_of(&position["last_bid_pnl_pct"]), 0.5);
    assert_approx(f64_of(&position["peak_mark"]), 15.0);
}

#[tokio::test]
async fn bid_target_fires_and_sells_at_the_bid() {
    let dir = tempdir();
    let h = book_service(&dir, base_cfg_btc(), 21.0, 20.5);
    let _ = h.svc.manage_base(false, None).await;
    let sells = btc_sells(&h.client);
    assert_eq!(sells.len(), 1);
    let price: f64 = sells[0]
        .price
        .as_deref()
        .and_then(|p| p.parse::<f64>().ok())
        .unwrap_or(0.0);
    assert_approx(price, 20.4);
    assert!(sells[0].reduce_only);
}

#[tokio::test]
async fn legacy_mode_still_uses_mark_for_target() {
    let dir = tempdir();
    let cfg = json!({"take_profit_pct": 1.0, "trail_activation_pct": 3.0});
    let h = book_service(&dir, cfg, 21.0, 15.0);
    let _ = h.svc.manage_base(false, None).await;
    assert_eq!(btc_sells(&h.client).len(), 1);
}

#[tokio::test]
async fn stop_loss_still_uses_mark_in_btc_mode() {
    let dir = tempdir();
    let cfg = json!({"btc_only": true, "stop_loss_pct": 0.55});
    let h = book_service(&dir, cfg, 9.0, 4.0);
    let result = h.svc.manage_base(false, None).await;
    assert_eq!(result["action"], "managed");
    assert!(btc_sells(&h.client).is_empty());
}

#[tokio::test]
async fn adaptive_runtime_uses_the_bid_too() {
    // The Python test monkeypatches `_refresh_exit_signal` to return `None`;
    // we get the same effect by setting a fresh refresh timestamp.
    let dir = tempdir();
    let cfg = json!({"btc_only": true, "take_profit_pct": 1.0});

    let h = book_service(&dir, cfg.clone(), 21.0, 15.0);
    {
        let mut position = active_position(&h.svc);
        if let Some(obj) = position.as_object_mut() {
            obj.insert(
                "last_exit_signal_refresh_ms".to_string(),
                json!(now_ms_from(&h.clock)),
            );
        }
        h.svc.st_set("active_position", position);
    }
    let result = h.svc.manage_active_position(false, None).await;
    assert_eq!(result["action"], "managed");
    assert!(btc_sells(&h.client).is_empty());

    let dir_b = dir.join("b");
    let h2 = book_service(&dir_b, cfg, 21.0, 20.5);
    {
        let mut position = active_position(&h2.svc);
        if let Some(obj) = position.as_object_mut() {
            obj.insert(
                "last_exit_signal_refresh_ms".to_string(),
                json!(now_ms_from(&h2.clock)),
            );
        }
        h2.svc.st_set("active_position", position);
    }
    let _ = h2.svc.manage_active_position(false, None).await;
    assert_eq!(btc_sells(&h2.client).len(), 1);
}

#[tokio::test]
async fn switching_to_btc_only_clears_old_sol_candidate_and_scan_stats() {
    // Uses a plain ETH/SOL universe snapshot; we drive it through the same
    // service helpers.
    let dir = tempdir();
    let cfg_inner = json!({"underlyings": ["SOLUSDT"]});
    let h = service_from_config(&dir, cfg_inner.clone());
    set_state(&h.svc, "active_position", Value::Null);
    let same = h.svc.sync_scan_universe();
    set_state(
        &h.svc,
        "last_candidate",
        json!({"symbol": "SOL-260828-88-C", "underlying": "SOLUSDT"}),
    );
    set_state(
        &h.svc,
        "recent_scan_outcomes",
        json!(vec!["ORDERABLE_CANDIDATE"; 3]),
    );
    h.svc.save_state().unwrap();

    assert!(same, "first sync records the SOL universe");
    assert!(!h.svc.sync_scan_universe(), "same universe keeps history");
    assert_eq!(
        st_get(&h.svc, "recent_scan_outcomes"),
        json!(vec!["ORDERABLE_CANDIDATE"; 3])
    );

    // Now switch the config to btc_only. The service cfg getter is fixed, so
    // rebuild a fresh service over the same state path.
    let mut btc_cfg = cfg_inner.clone();
    if let Some(obj) = btc_cfg.as_object_mut() {
        obj.insert("btc_only".to_string(), json!(true));
    }
    // Persist the current state and reopen with the btc_only config.
    h.svc.save_state().unwrap();
    let reopened = service_from_config(&dir, btc_cfg);
    let status = reopened.svc.status_snapshot(false).await;
    assert!(status["last_candidate"].is_null());
    assert_eq!(status["scan_outcomes_window"], 0);
    assert_eq!(st_get(&reopened.svc, "scan_universe"), json!(["BTCUSDT"]));
}

// ---------------------------------------------------------------------------
// test_options_wallet_budget.py (service-side tests only)
// ---------------------------------------------------------------------------

#[tokio::test]
async fn depleted_legacy_ledger_no_longer_blocks_entries() {
    let dir = tempdir();
    let h = service(&dir, true);
    set_state(&h.svc, "cash_bankroll_usdt", json!(0.5));
    let result = h.svc.run_cycle(true, false).await;
    assert_eq!(result["action"], "entered");
    let orders = h.client.snapshot_orders();
    assert_eq!(orders[0].side, "BUY");
    let entry_total = f64_of(&active_position(&h.svc)["entry_total_usdt"]);
    assert!(entry_total <= 21.0);
}

#[tokio::test]
async fn status_reports_wallet_budget() {
    let dir = tempdir();
    let h = service(&dir, false);
    let status = h.svc.status_snapshot(false).await;
    assert_eq!(status["capital_limit_usdt"], 0.0);
    let budget_text = status["budget_text"].as_str().unwrap_or("").to_string();
    assert!(
        budget_text.contains("고정 한도 없음"),
        "budget_text: {}",
        budget_text
    );
    assert_eq!(status["realized_pnl_usdt"], 0.0);
}

// ---------------------------------------------------------------------------
// test_options_adaptive._Client (three contracts: unaffordable EXP, affordable
// AFF, PUT peer). Its mark/ticker require a symbol, so the batch preload fails
// exactly like the Python TypeError path.
// ---------------------------------------------------------------------------

struct AdaptiveClient {
    clock: Arc<FixedClock>,
    orders: Mutex<Vec<OptionOrder>>,
}

impl OptionsApi for AdaptiveClient {
    fn authenticated(&self) -> bool {
        true
    }
    async fn sync_time(&self) -> ApiResult {
        Ok(json!({"serverTime": now_ms_from(&self.clock)}))
    }
    async fn ping(&self) -> ApiResult {
        Ok(json!({}))
    }
    async fn exchange_info(&self) -> ApiResult {
        let now = now_ms_from(&self.clock);
        let common = json!({
            "expiryDate": now + 5 * 86_400_000_i64,
            "status": "TRADING",
            "strikePrice": "185",
            "underlying": "ETHUSDT",
            "unit": 1,
            "minQty": "0.01",
            "contractType": "CRYPTO_OPTIONS",
            "underlyingType": "CRYPTO",
            "filters": [
                {"filterType": "LOT_SIZE", "minQty": "0.01", "stepSize": "0.01"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
            ],
        });
        let row = |symbol: &str, side: &str| {
            let mut r = common.clone();
            r["symbol"] = json!(symbol);
            r["side"] = json!(side);
            r
        };
        Ok(json!({"optionSymbols": [
            row("ETH-EXP-C", "CALL"),
            row("ETH-AFF-C", "CALL"),
            row("ETH-PEER-P", "PUT"),
        ]}))
    }
    async fn mark_price(&self, symbol: Option<&str>) -> ApiResult {
        let Some(symbol) = symbol else {
            return Err(OptionsApiError::new("symbol required"));
        };
        Ok(json!([{
            "symbol": symbol,
            "markPrice": if symbol.contains("EXP") { "19990" } else { "0.49" },
            "markIV": "0.04",
            "delta": if symbol.ends_with("-P") { "-0.45" } else { "0.45" },
            "gamma": "0.01", "theta": "-0.05", "vega": "0.2",
        }]))
    }
    async fn ticker(&self, symbol: Option<&str>) -> ApiResult {
        let Some(symbol) = symbol else {
            return Err(OptionsApiError::new("symbol required"));
        };
        if symbol.contains("EXP") {
            return Ok(json!([{"amount": "1000", "bidPrice": "19980", "askPrice": "20000"}]));
        }
        Ok(json!([{"amount": "1000", "bidPrice": "0.48", "askPrice": "0.50"}]))
    }
    async fn depth(&self, symbol: &str, _limit: u32) -> ApiResult {
        if symbol.contains("EXP") {
            return Ok(json!({"bids": [["19980", "10"]], "asks": [["20000", "10"]]}));
        }
        Ok(json!({"bids": [["0.48", "10"]], "asks": [["0.50", "10"]]}))
    }
    async fn recent_trades(&self, _symbol: &str, _limit: u32) -> ApiResult {
        Err(OptionsApiError::new("no recent_trades on this fake"))
    }
    async fn margin_account(&self) -> ApiResult {
        Ok(
            json!({"canTrade": true, "asset": [{"asset": "USDT", "available": "24", "equity": "24"}]}),
        )
    }
    async fn positions(&self, _symbol: Option<&str>) -> ApiResult {
        Ok(json!([]))
    }
    async fn open_orders(&self, _symbol: Option<&str>) -> ApiResult {
        Ok(json!([]))
    }
    async fn query_order(&self, _symbol: &str, _order: &OrderRef) -> ApiResult {
        Err(OptionsApiError::new("filled fake order should not query"))
    }
    async fn new_order(&self, order: &OptionOrder) -> ApiResult {
        let mut orders = self.orders.lock().unwrap();
        orders.push(order.clone());
        Ok(json!({
            "symbol": order.symbol,
            "status": "FILLED",
            "executedQty": order.quantity,
            "avgPrice": order.price,
            "orderId": orders.len(),
            "clientOrderId": order.client_order_id,
        }))
    }
    async fn cancel_order(&self, symbol: &str, _order: &OrderRef) -> ApiResult {
        Ok(json!({"symbol": symbol, "status": "CANCELED"}))
    }
    async fn user_trades(&self, _symbol: &str, _start: Option<i64>, _limit: u32) -> ApiResult {
        Ok(json!([]))
    }
    async fn exercise_records(
        &self,
        _symbol: Option<&str>,
        _start: Option<i64>,
        _limit: u32,
    ) -> ApiResult {
        Ok(json!([]))
    }
    async fn underlying_klines(
        &self,
        _symbol: &str,
        interval: &str,
        _limit: u32,
    ) -> Result<Vec<Candle>, OptionsApiError> {
        Ok(fake_market(interval))
    }
}

struct AdaptiveHarness {
    svc: OptionsService<AdaptiveClient>,
    client: Arc<AdaptiveClient>,
}

fn adaptive_service(dir: &Path) -> AdaptiveHarness {
    std::fs::create_dir_all(dir).unwrap();
    let clock = Arc::new(FixedClock::new(Utc::now(), Utc::now().timestamp() as f64));
    let client = Arc::new(AdaptiveClient {
        clock: clock.clone(),
        orders: Mutex::new(Vec::new()),
    });
    let cfg = json!({"enabled": true, "underlyings": ["ETHUSDT"], "min_quote_volume_usdt": 1});
    let cfg_getter: ConfigGetter = Arc::new(move || cfg.clone());
    let sleeper: Sleeper = Arc::new(|_secs: f64| -> BoxFut<()> { Box::pin(async {}) });
    let svc = OptionsService::new(
        cfg_getter,
        client.clone(),
        dir.join("options_state.json"),
        None,
        clock,
        sleeper,
    );
    AdaptiveHarness { svc, client }
}

fn st_get_any<C: OptionsApi>(svc: &OptionsService<C>, key: &str) -> Value {
    svc.st_get(key)
}

// ---------------------------------------------------------------------------
// User rule (2026-11-18): option entries only while every futures strategy is OFF.
// ---------------------------------------------------------------------------

#[tokio::test]
async fn futures_strategy_on_blocks_option_entries() {
    let dir = tempdir();
    let h = service(&dir, true);
    let futures_on = Arc::new(Mutex::new(true));
    let flag = futures_on.clone();
    h.svc.set_entry_guard(Arc::new(move || {
        if *flag.lock().unwrap() {
            Some("선물 전략(/tradfi)이 켜져 있어 옵션 신규 진입을 하지 않습니다.".to_string())
        } else {
            None
        }
    }));
    let result = h.svc.run_cycle(true, false).await;
    assert_eq!(result["action"], "waiting");
    assert!(
        result["reason"].as_str().unwrap_or("").contains("/tradfi"),
        "{result}"
    );
    assert!(h.client.snapshot_orders().is_empty());

    *futures_on.lock().unwrap() = false;
    let result = h.svc.run_cycle(true, false).await;
    assert_eq!(result["action"], "entered", "{result}");
}
