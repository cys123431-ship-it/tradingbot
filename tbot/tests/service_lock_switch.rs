#![allow(unused)] // ported test fixtures keep the Python helpers
#![allow(
    clippy::type_complexity,
    clippy::ptr_arg,
    clippy::too_many_arguments,
    dead_code
)]
//! Port of `tests/test_btc_ma_cross_profit_lock_switch.py`.
//!
//! Excluded (Telegram controller / config writer / keyboard):
//!   test_manual_trend_choice_releases_the_forced_mode,
//!   test_telegram_switch_button_toggles_and_shows_the_count,
//!   test_status_line_shows_the_switch_progress.

mod common;

use std::collections::HashMap;

use common::*;
use serde_json::{json, Map, Value};
use tbot::exchange::FuturesExchange;
use tbot::strategy::service::{BoxFut, ServiceDeps, SmaCrossService, Variant};

// ---------------------------------------------------------------- Writer

/// Stand-in for the Telegram controller's async config writer.
struct Writer {
    cfg: std::sync::Arc<std::sync::Mutex<Value>>,
    writes: std::sync::Arc<std::sync::Mutex<Vec<String>>>,
}

impl Writer {
    fn new(cfg: std::sync::Arc<std::sync::Mutex<Value>>) -> Self {
        Self {
            cfg,
            writes: std::sync::Arc::new(std::sync::Mutex::new(Vec::new())),
        }
    }
}

// ---------------------------------------------------------------- ScopeFake

/// `FakeBinance(mark, wallet)` whose `fetch_ohlcv("1h")` returns the supplied rows.
struct ScopeFake {
    inner: FakeBinance,
    rows: std::sync::Mutex<Vec<Value>>,
}

impl ScopeFake {
    fn new(rows: Vec<Value>, mark: f64, wallet: f64) -> Self {
        let opts = FakeOptions {
            h1: Vec::new(),
            m15: Vec::new(),
            m1: Vec::new(),
            mark,
            wallet,
            info: exchange_info("0.001", "0.001", "50", "0.10", "TRADING"),
        };
        Self {
            inner: FakeBinance::new(opts),
            rows: std::sync::Mutex::new(rows),
        }
    }

    fn with_state<R>(&self, f: impl FnOnce(&mut FakeState) -> R) -> R {
        let mut s = self.inner.state.lock().unwrap();
        f(&mut s)
    }
}

impl FuturesExchange for ScopeFake {
    async fn load_markets(&self) -> Result<Map<String, Value>, tbot::exchange::ExchangeError> {
        self.inner.load_markets().await
    }

    fn markets(&self) -> Option<Map<String, Value>> {
        self.inner.markets()
    }

    async fn exchange_info_raw(&self) -> Result<Value, tbot::exchange::ExchangeError> {
        self.inner.exchange_info_raw().await
    }

    async fn fetch_ohlcv(
        &self,
        symbol: &str,
        timeframe: &str,
        since: Option<i64>,
        limit: Option<u32>,
    ) -> Result<Vec<tbot::core::candles::Candle>, tbot::exchange::ExchangeError> {
        if timeframe == "1m" {
            return self
                .inner
                .fetch_ohlcv(symbol, timeframe, since, limit)
                .await;
        }
        let rows = self.rows.lock().unwrap().clone();
        Ok(rows
            .iter()
            .map(|r| {
                let a = r.as_array().cloned().unwrap_or_default();
                let mut out = [0.0_f64; 6];
                for (i, v) in a.iter().take(6).enumerate() {
                    out[i] = v.as_f64().unwrap_or(0.0);
                }
                out
            })
            .collect())
    }

    async fn premium_index_raw(
        &self,
        market_id: &str,
    ) -> Result<Value, tbot::exchange::ExchangeError> {
        self.inner.premium_index_raw(market_id).await
    }

    async fn commission_rate_raw(
        &self,
        market_id: &str,
    ) -> Result<Value, tbot::exchange::ExchangeError> {
        self.inner.commission_rate_raw(market_id).await
    }

    async fn fetch_balance(&self) -> Result<Value, tbot::exchange::ExchangeError> {
        self.inner.fetch_balance().await
    }

    async fn position_side_dual_raw(&self) -> Result<Value, tbot::exchange::ExchangeError> {
        self.inner.position_side_dual_raw().await
    }

    async fn set_margin_mode(
        &self,
        mode: &str,
        symbol: &str,
    ) -> Result<Value, tbot::exchange::ExchangeError> {
        self.inner.set_margin_mode(mode, symbol).await
    }

    async fn set_leverage(
        &self,
        leverage: u32,
        symbol: &str,
    ) -> Result<Value, tbot::exchange::ExchangeError> {
        self.inner.set_leverage(leverage, symbol).await
    }

    async fn position_risk_v2_raw(
        &self,
        market_id: &str,
    ) -> Result<Value, tbot::exchange::ExchangeError> {
        self.inner.position_risk_v2_raw(market_id).await
    }

    async fn fetch_positions(
        &self,
        symbols: Option<Vec<String>>,
    ) -> Result<Vec<Value>, tbot::exchange::ExchangeError> {
        self.inner.fetch_positions(symbols).await
    }

    async fn get_order_raw(
        &self,
        market_id: &str,
        orig_client_order_id: &str,
    ) -> Result<Value, tbot::exchange::ExchangeError> {
        self.inner
            .get_order_raw(market_id, orig_client_order_id)
            .await
    }

    async fn fetch_open_orders(
        &self,
        symbol: Option<&str>,
    ) -> Result<Vec<Value>, tbot::exchange::ExchangeError> {
        self.inner.fetch_open_orders(symbol).await
    }

    async fn cancel_order(
        &self,
        order_id: &str,
        symbol: &str,
    ) -> Result<Value, tbot::exchange::ExchangeError> {
        self.inner.cancel_order(order_id, symbol).await
    }

    async fn create_order(
        &self,
        symbol: &str,
        order_type: &str,
        side: &str,
        amount: f64,
        price: Option<f64>,
        params: Map<String, Value>,
    ) -> Result<Value, tbot::exchange::ExchangeError> {
        self.inner
            .create_order(symbol, order_type, side, amount, price, params)
            .await
    }

    async fn algo_post_raw(
        &self,
        params: Map<String, Value>,
    ) -> Result<Value, tbot::exchange::ExchangeError> {
        self.inner.algo_post_raw(params).await
    }

    async fn algo_get_raw(
        &self,
        client_algo_id: &str,
    ) -> Result<Value, tbot::exchange::ExchangeError> {
        self.inner.algo_get_raw(client_algo_id).await
    }

    async fn algo_open_raw(&self) -> Result<Value, tbot::exchange::ExchangeError> {
        self.inner.algo_open_raw().await
    }

    async fn algo_delete_raw(
        &self,
        client_algo_id: &str,
    ) -> Result<Value, tbot::exchange::ExchangeError> {
        self.inner.algo_delete_raw(client_algo_id).await
    }

    async fn user_trades_raw(
        &self,
        market_id: &str,
        start_time: Option<i64>,
        limit: u32,
    ) -> Result<Value, tbot::exchange::ExchangeError> {
        self.inner
            .user_trades_raw(market_id, start_time, limit)
            .await
    }

    async fn income_raw(
        &self,
        market_id: &str,
        income_type: Option<&str>,
        start_time: Option<i64>,
        limit: u32,
    ) -> Result<Value, tbot::exchange::ExchangeError> {
        self.inner
            .income_raw(market_id, income_type, start_time, limit)
            .await
    }

    async fn fetch_tickers(&self) -> Result<Map<String, Value>, tbot::exchange::ExchangeError> {
        self.inner.fetch_tickers().await
    }
}

// ---------------------------------------------------------------- helpers

/// `_close`: simulate a trade-closed callback with the given exit reason.
/// NEEDS pub: `_on_trade_closed`
async fn close_trade(service: &SmaCrossService<Fake>, network: &str, mode: &str, reason: &str) {
    let mut state = {
        let s = service.load_state(network, mode).unwrap();
        s
    };
    service.on_trade_closed(
        network,
        mode,
        &mut state,
        &json!({
            "market_id": "BTCUSDT",
            "side": "LONG",
            "exit_reason": reason,
            "trade_id": "t",
        })
        .as_object()
        .cloned()
        .unwrap(),
    );
    service.save_state(network, mode, &mut state).unwrap();
}

/// Python `_close(service, state, reason)`: in-memory state, no save.
fn close_in(service: &SmaCrossService<Fake>, state: &mut Map<String, Value>, reason: &str) {
    let trade =
        json!({"market_id": "BTCUSDT", "side": "LONG", "exit_reason": reason, "trade_id": "t"});
    service.on_trade_closed("testnet", "LIVE", state, trade.as_object().unwrap());
}

fn trend_harness(root: &std::path::Path, extra: &[(&str, Value)]) -> Harness {
    let mut cfg = HashMap::new();
    cfg.insert("entry_mode".to_string(), json!("trend"));
    for (k, v) in extra {
        cfg.insert((*k).to_string(), v.clone());
    }
    Harness::new(root, true, "testnet", 86_000.0, cfg)
}

fn install_writer(h: &mut Harness) -> std::sync::Arc<std::sync::Mutex<Vec<String>>> {
    let writes = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let (w, cfg) = (writes.clone(), h.cfg.clone());
    h.service.deps.config_writer = Some(std::sync::Arc::new(move |mode: String| {
        let (w, cfg) = (w.clone(), cfg.clone());
        Box::pin(async move {
            w.lock().unwrap().push(mode.clone());
            cfg.lock().unwrap()["entry_mode"] = json!(mode);
            true
        }) as BoxFut<bool>
    }));
    writes
}

// ---------------------------------------------------------------- config

#[tokio::test]
async fn config_default_and_choices() {
    let normalize = |v: Value| Variant::BtcMa.normalize(&v);
    assert_eq!(normalize(json!({}))["profit_lock_switch_count"], json!(3));
    assert_eq!(
        normalize(json!({"profit_lock_switch_count": 0}))["profit_lock_switch_count"],
        json!(0)
    );
    assert_eq!(
        normalize(json!({"profit_lock_switch_count": "5"}))["profit_lock_switch_count"],
        json!(5)
    );
    assert_eq!(
        normalize(json!({"profit_lock_switch_count": 99}))["profit_lock_switch_count"],
        json!(3)
    );
    assert_eq!(
        normalize(json!({"profit_lock_switch_count": null}))["profit_lock_switch_count"],
        json!(3)
    );
    // PROFIT_LOCK_SWITCH_CHOICES == (0, 1, 2, 3, 4, 5)
    for choice in [0, 1, 2, 3, 4, 5] {
        assert_eq!(
            normalize(json!({"profit_lock_switch_count": choice}))["profit_lock_switch_count"],
            json!(choice)
        );
    }
}

// ------------------------------------------------------------- counts / switch

#[tokio::test]
async fn counts_only_profit_lock_exits() {
    let root = temp_dir("counts");
    let h = trend_harness(&root, &[]);
    let mut state = h.state_live();
    close_in(&h.service, &mut state, "PROFIT_LOCK");
    assert_eq!(state["profit_lock_exit_count"], json!(1));
    for reason in ["CROSS_REVERSE", "EMERGENCY_STOP", "MANUAL", "FAILSAFE"] {
        close_in(&h.service, &mut state, reason);
    }
    assert_eq!(state["profit_lock_exit_count"], json!(1));
    assert!(state
        .get("pending_entry_mode_switch")
        .map_or(true, Value::is_null));
    assert!(!state
        .get("entry_mode_forced_cross")
        .and_then(Value::as_bool)
        .unwrap_or(false));
}

#[tokio::test]
async fn third_profit_lock_queues_the_forced_cross_switch() {
    let root = temp_dir("third");
    let mut extra = HashMap::new();
    extra.insert("entry_mode".to_string(), json!("trend"));
    let h = Harness::new(&root, true, "testnet", 86_000.0, extra);
    {
        let mut cfg = h.cfg.lock().unwrap();
        if let Some(obj) = cfg.as_object_mut() {
            obj.insert("entry_mode".to_string(), json!("trend"));
        }
    }
    let _ = h.run().await;

    let mut state = h.state_live();
    for _ in 0..2 {
        h.service
            .on_trade_closed(
                "testnet",
                "LIVE",
                &mut state,
                &json!({"market_id": "BTCUSDT", "side": "LONG", "exit_reason": "PROFIT_LOCK", "trade_id": "t"}).as_object().cloned().unwrap(),
            );
    }
    assert!(state.get("pending_entry_mode_switch").is_none());
    h.service
        .on_trade_closed(
            "testnet",
            "LIVE",
            &mut state,
            &json!({"market_id": "BTCUSDT", "side": "LONG", "exit_reason": "PROFIT_LOCK", "trade_id": "t"}).as_object().cloned().unwrap(),
        );
    let switch = state["pending_entry_mode_switch"]
        .as_object()
        .cloned()
        .unwrap();
    assert_eq!(switch["entry_mode"], json!("cross"));
    assert_eq!(switch["from_mode"], json!("trend"));
    assert_eq!(switch["exit_count"], json!(3));
    assert_eq!(state["entry_mode_forced_cross"], json!(true));
    assert_eq!(state["profit_lock_exit_count"], json!(0));
}

#[tokio::test]
async fn switch_off_never_forces() {
    let root = temp_dir("switch_off");
    let mut extra = HashMap::new();
    extra.insert("entry_mode".to_string(), json!("trend"));
    extra.insert("profit_lock_switch_count".to_string(), json!(0));
    let h = Harness::new(&root, true, "testnet", 86_000.0, extra);
    {
        let mut cfg = h.cfg.lock().unwrap();
        if let Some(obj) = cfg.as_object_mut() {
            obj.insert("entry_mode".to_string(), json!("trend"));
            obj.insert("profit_lock_switch_count".to_string(), json!(0));
        }
    }
    let _ = h.run().await;

    let mut state = h.state_live();
    for _ in 0..6 {
        h.service
            .on_trade_closed(
                "testnet",
                "LIVE",
                &mut state,
                &json!({"market_id": "BTCUSDT", "side": "LONG", "exit_reason": "PROFIT_LOCK", "trade_id": "t"}).as_object().cloned().unwrap(),
            );
    }
    assert_eq!(state["profit_lock_exit_count"], json!(6));
    assert!(state.get("pending_entry_mode_switch").is_none());
    assert!(!state
        .get("entry_mode_forced_cross")
        .and_then(|v| v.as_bool())
        .unwrap_or(false));
}

// ------------------------------------------------------ actual entry cycle

#[tokio::test]
async fn forced_switch_is_applied_before_the_next_entry() {
    let root = temp_dir("forced_apply");
    let mut h = trend_harness(&root, &[]);
    let writes = install_writer(&mut h);
    let mut state = h.state_live();
    for _ in 0..3 {
        close_in(&h.service, &mut state, "PROFIT_LOCK");
    }
    h.service.save_state("testnet", "LIVE", &mut state).unwrap();
    h.next_bar(83_000.0); // opposite-cross bar: entered this cycle
    let result = h.run().await;
    assert_eq!(*writes.lock().unwrap(), vec!["cross".to_string()]);
    assert_eq!(h.cfg.lock().unwrap()["entry_mode"], json!("cross"));
    assert_eq!(result["action"], json!("entered"), "{result}");
    assert_eq!(h.state_live()["trade"]["side"], json!("SHORT"));
    let saved = h.state_live();
    assert!(saved["pending_entry_mode_switch"].is_null());
    assert_eq!(saved["last_entry_mode_switch"]["applied"], json!(true));
    assert_eq!(saved["entry_mode_forced_cross"], json!(true));
}

fn h_writes(
    w: &std::sync::Arc<std::sync::Mutex<Vec<String>>>,
) -> std::sync::MutexGuard<'_, Vec<String>> {
    w.lock().unwrap()
}

#[tokio::test]
async fn forced_cross_does_not_reenter_without_a_new_cross() {
    // The cross ended two bars ago (SMA3 stays above SMA200); no new cross on the last bar.
    let root = temp_dir("forced_no_reenter");
    let mut h = trend_harness(&root, &[]);
    let rows = trend_rows(86_000.0, 85_000.0);
    h.fake
        .bars
        .lock()
        .unwrap()
        .insert("1h".to_string(), rows.clone());
    h.rows = rows.clone();
    h.set_now(now_after(&rows, H1, 5));
    install_writer(&mut h);
    let mut state = h.state_live();
    for _ in 0..3 {
        close_in(&h.service, &mut state, "PROFIT_LOCK");
    }
    h.service.save_state("testnet", "LIVE", &mut state).unwrap();
    let result = h.run().await;
    assert_eq!(h.cfg.lock().unwrap()["entry_mode"], json!("cross"));
    // No cross: no entry (trend mode would have re-entered the same direction).
    assert_eq!(result["action"], json!("skip"), "{result}");
    assert_eq!(result["reason"], json!("NO_SIGNAL"));
    assert!(h.state_live()["trade"].is_null());
    assert!(h.fake.with_state(|s| s.orders.is_empty()));
}

/// Python `_trend_rows`: cross up on the previous bar, still above on the last closed bar.
fn trend_rows(last_close: f64, base: f64) -> Vec<Value> {
    let mut rows = bars(last_close * base / 85_000.0, 205, base, T0, H1);
    let prev = rows
        .last()
        .and_then(|r| r.as_array().and_then(|a| a.get(4)).and_then(|v| v.as_f64()))
        .unwrap_or(0.0);
    let last_ts = rows
        .last()
        .and_then(|r| {
            r.as_array()
                .and_then(|a| a.first())
                .and_then(|v| v.as_i64())
        })
        .unwrap_or(T0);
    rows.push(json!([
        last_ts + H1,
        prev,
        prev * 1.002,
        prev * 0.998,
        prev * 1.001,
        1.0
    ]));
    rows
}
