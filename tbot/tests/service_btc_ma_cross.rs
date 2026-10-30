#![allow(
    clippy::type_complexity,
    clippy::ptr_arg,
    clippy::too_many_arguments,
    dead_code
)]
//! Port of `tests/test_btc_ma_cross.py` (service + signal tests).
//!
//! Excluded (Telegram controller / wiring / daily report / menus):
//!   test_telegram_menu_timeframe_emergency_stop_and_exclusivity,
//!   test_pullback_on_turns_ma_cross_off, test_wiring_keyboard_config_stop_and_report,
//!   test_daily_report_has_ma_cross_section, test_sma_buttons_and_header,
//!   test_preset_button_sets_the_fast_period_too,
//!   test_invalid_periods_are_rejected_without_changing_config,
//!   test_sma_help_lists_the_text_command, test_large_slow_period_lifts_candle_history.
//!
//! Signal-level `evaluate_cross` / `profit_lock_target` / `emergency_stop_price` /
//! `sma` / `market_is_flat` live in the Python `btc_ma_cross.signals` module which
//! has no Rust port; the cross/lock/emergency behaviour is exercised through the
//! service in the tests below (entry stop price, profit-lock ratchet, reverse).

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use serde_json::{json, Map, Value};

use tbot::core::candles::Candle;
use tbot::exchange::fake::{exchange_info, FakeBinance, FakeOptions, FakeState, H1, M1, T0};
use tbot::exchange::{ExchangeError, FuturesExchange};
use tbot::store::clock::SystemClock;
use tbot::store::order_state::OrderRecord;
use tbot::strategy::service::{self, BoxFut, ServiceDeps, SmaCrossService, Variant};

// ---------------------------------------------------------------- bars

/// n-1 flat closes then one final close: a cross happens on the last bar.
fn bars(last_close: f64, n: usize, base: f64, start_ms: i64, span: i64) -> Vec<Value> {
    let mut rows = Vec::with_capacity(n);
    for i in 0..n.saturating_sub(1) {
        rows.push(json!([
            start_ms + (i as i64) * span,
            base,
            base * 1.001,
            base * 0.999,
            base,
            10.0
        ]));
    }
    let o = base;
    rows.push(json!([
        start_ms + ((n - 1) as i64) * span,
        o,
        o.max(last_close) * 1.001,
        o.min(last_close) * 0.999,
        last_close,
        10.0
    ]));
    rows
}

fn bars_default(last_close: f64) -> Vec<Value> {
    bars(last_close, 205, 85_000.0, T0, H1)
}

fn now_after(rows: &[Value], span: i64, seconds: i64) -> i64 {
    let last = rows
        .last()
        .and_then(|r| {
            r.as_array()
                .and_then(|a| a.first())
                .and_then(|v| v.as_i64())
        })
        .unwrap_or(T0);
    last + span + seconds * 1000
}

fn now_after_default(rows: &[Value]) -> i64 {
    now_after(rows, H1, 5)
}

// ---------------------------------------------------------------- Fake

/// `Fake(FakeBinance)`: wraps `FakeBinance` but serves `bars["1h"]` from the
/// harness and adds `fire(client_prefix, price)`.
struct Fake {
    inner: FakeBinance,
    bars: Mutex<HashMap<String, Vec<Value>>>,
    /// Python `_lagging_close`: after a reduce-only close, report the old position N more times.
    lag_reads: Mutex<i64>,
    lag_left: Mutex<i64>,
    lag_snapshot: Mutex<Option<Value>>,
    /// Python `with_fxs`: rows appended to every fetch_positions result.
    extra_positions: Mutex<Vec<Value>>,
    /// Python `failing_cancel`: fapiPrivateDeleteAlgoOrder raises.
    fail_algo_delete: Mutex<bool>,
}

impl Fake {
    fn new(rows: Vec<Value>, opts: FakeOptions) -> Self {
        let mut m = HashMap::new();
        m.insert("1h".to_string(), rows);
        Self {
            inner: FakeBinance::new(opts),
            bars: Mutex::new(m),
            lag_reads: Mutex::new(0),
            lag_left: Mutex::new(0),
            lag_snapshot: Mutex::new(None),
            extra_positions: Mutex::new(Vec::new()),
            fail_algo_delete: Mutex::new(false),
        }
    }

    fn with_state<R>(&self, f: impl FnOnce(&mut FakeState) -> R) -> R {
        let mut s = self.inner.state.lock().unwrap();
        f(&mut s)
    }

    fn algos_snapshot(&self) -> HashMap<String, Value> {
        self.with_state(|s| s.algos.clone())
    }

    /// Binance fires one specific trigger (e.g. only the profit lock).
    fn fire(&self, client_prefix: &str, price: f64) {
        self.with_state(|s| {
            let (side, qty) = match s.position.as_ref() {
                Some(p) => {
                    let pos_side = p.get("side").and_then(|v| v.as_str()).unwrap_or("long");
                    let side = if pos_side == "long" { "SELL" } else { "BUY" };
                    let qty = p.get("contracts").and_then(|v| v.as_f64()).unwrap_or(0.0);
                    (side.to_string(), qty)
                }
                None => ("SELL".to_string(), 0.0),
            };
            let keys: Vec<String> = s.algos.keys().cloned().collect();
            for key in keys {
                if let Some(order) = s.algos.get_mut(&key) {
                    let status = order
                        .get("algoStatus")
                        .and_then(|v| v.as_str())
                        .unwrap_or("");
                    let cid = order
                        .get("clientAlgoId")
                        .and_then(|v| v.as_str())
                        .unwrap_or("");
                    if status == "NEW" && cid.starts_with(client_prefix) {
                        if let Some(obj) = order.as_object_mut() {
                            obj.insert(
                                "algoStatus".to_string(),
                                Value::String("FINISHED".to_string()),
                            );
                        }
                    }
                }
            }
            s.position = None;
            s.user_trades.push(json!({
                "side": side.to_uppercase(),
                "qty": qty.to_string(),
                "price": price.to_string(),
                "commission": (price * qty * 0.0004).to_string(),
                "commissionAsset": "USDT",
                "time": 3
            }));
        });
    }
}

impl FuturesExchange for Fake {
    async fn load_markets(&self) -> Result<Map<String, Value>, ExchangeError> {
        self.inner.load_markets().await
    }

    fn markets(&self) -> Option<Map<String, Value>> {
        self.inner.markets()
    }

    async fn exchange_info_raw(&self) -> Result<Value, ExchangeError> {
        self.inner.exchange_info_raw().await
    }

    async fn fetch_ohlcv(
        &self,
        symbol: &str,
        timeframe: &str,
        since: Option<i64>,
        limit: Option<u32>,
    ) -> Result<Vec<Candle>, ExchangeError> {
        if self.with_state(|s| s.raise_klines) {
            return Err(ExchangeError::new("ConnectionError", "disconnected"));
        }
        if timeframe == "1m" {
            return self
                .inner
                .fetch_ohlcv(symbol, timeframe, since, limit)
                .await;
        }
        let rows = {
            let map = self.bars.lock().unwrap();
            map.get(timeframe).cloned()
        };
        let rows = match rows {
            Some(r) => r,
            None => {
                return Err(ExchangeError::new(
                    "Exception",
                    format!("unknown timeframe {timeframe}"),
                ))
            }
        };
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

    async fn premium_index_raw(&self, market_id: &str) -> Result<Value, ExchangeError> {
        self.inner.premium_index_raw(market_id).await
    }

    async fn commission_rate_raw(&self, market_id: &str) -> Result<Value, ExchangeError> {
        self.inner.commission_rate_raw(market_id).await
    }

    async fn fetch_balance(&self) -> Result<Value, ExchangeError> {
        self.inner.fetch_balance().await
    }

    async fn position_side_dual_raw(&self) -> Result<Value, ExchangeError> {
        self.inner.position_side_dual_raw().await
    }

    async fn set_margin_mode(&self, mode: &str, symbol: &str) -> Result<Value, ExchangeError> {
        self.inner.set_margin_mode(mode, symbol).await
    }

    async fn set_leverage(&self, leverage: u32, symbol: &str) -> Result<Value, ExchangeError> {
        self.inner.set_leverage(leverage, symbol).await
    }

    async fn position_risk_v2_raw(&self, market_id: &str) -> Result<Value, ExchangeError> {
        self.inner.position_risk_v2_raw(market_id).await
    }

    async fn fetch_positions(
        &self,
        symbols: Option<Vec<String>>,
    ) -> Result<Vec<Value>, ExchangeError> {
        {
            let mut left = self.lag_left.lock().unwrap();
            if *left > 0 {
                *left -= 1;
                let mut snap = self
                    .lag_snapshot
                    .lock()
                    .unwrap()
                    .clone()
                    .unwrap_or(Value::Null);
                if let Some(obj) = snap.as_object_mut() {
                    obj.insert("symbol".into(), json!("BTC/USDT:USDT"));
                }
                return Ok(vec![snap]);
            }
        }
        let mut rows = self.inner.fetch_positions(symbols).await?;
        rows.extend(self.extra_positions.lock().unwrap().iter().cloned());
        Ok(rows)
    }

    async fn get_order_raw(
        &self,
        market_id: &str,
        orig_client_order_id: &str,
    ) -> Result<Value, ExchangeError> {
        self.inner
            .get_order_raw(market_id, orig_client_order_id)
            .await
    }

    async fn fetch_open_orders(&self, symbol: Option<&str>) -> Result<Vec<Value>, ExchangeError> {
        self.inner.fetch_open_orders(symbol).await
    }

    async fn cancel_order(&self, order_id: &str, symbol: &str) -> Result<Value, ExchangeError> {
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
    ) -> Result<Value, ExchangeError> {
        let reduce_only = params
            .get("reduceOnly")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let stale_reads = *self.lag_reads.lock().unwrap();
        if reduce_only && stale_reads > 0 {
            let position = self.with_state(|s| s.position.clone());
            if let Some(pos) = position {
                *self.lag_snapshot.lock().unwrap() = Some(pos);
                *self.lag_left.lock().unwrap() = stale_reads;
            }
        }
        self.inner
            .create_order(symbol, order_type, side, amount, price, params)
            .await
    }

    async fn algo_post_raw(&self, params: Map<String, Value>) -> Result<Value, ExchangeError> {
        self.inner.algo_post_raw(params).await
    }

    async fn algo_get_raw(&self, client_algo_id: &str) -> Result<Value, ExchangeError> {
        self.inner.algo_get_raw(client_algo_id).await
    }

    async fn algo_open_raw(&self) -> Result<Value, ExchangeError> {
        self.inner.algo_open_raw().await
    }

    async fn algo_delete_raw(&self, client_algo_id: &str) -> Result<Value, ExchangeError> {
        if *self.fail_algo_delete.lock().unwrap() {
            return Err(ExchangeError::new(
                "Exception",
                r#"binance {"code":-1001,"msg":"Internal error"}"#,
            ));
        }
        self.inner.algo_delete_raw(client_algo_id).await
    }

    async fn user_trades_raw(
        &self,
        market_id: &str,
        start_time: Option<i64>,
        limit: u32,
    ) -> Result<Value, ExchangeError> {
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
    ) -> Result<Value, ExchangeError> {
        self.inner
            .income_raw(market_id, income_type, start_time, limit)
            .await
    }

    async fn fetch_tickers(&self) -> Result<Map<String, Value>, ExchangeError> {
        self.inner.fetch_tickers().await
    }
}

// ---------------------------------------------------------------- Harness

fn temp_dir(tag: &str) -> PathBuf {
    static N: AtomicU64 = AtomicU64::new(0);
    let n = N.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!(
        "tbot_btc_ma_cross_{tag}_{n}_{}",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

struct Harness {
    rows: Vec<Value>,
    now_ms: i64,
    fake: Arc<Fake>,
    cfg: Arc<Mutex<Value>>,
    network: String,
    notes: Arc<Mutex<Vec<String>>>,
    tmp_path: PathBuf,
    /// Shared with the service clock: Python tests mutate `h.now_ms`.
    clock_ms: Arc<Mutex<i64>>,
    service: SmaCrossService<Fake>,
}

impl Harness {
    fn new(
        tmp_path: &Path,
        live: bool,
        network: &str,
        last_close: f64,
        cfg_extra: HashMap<String, Value>,
    ) -> Self {
        let rows = bars_default(last_close);
        let now_ms = now_after_default(&rows);
        let info = if network == "testnet" {
            exchange_info("0.0001", "0.0001", "50", "0.10", "TRADING")
        } else {
            // Python: FakeBinance(info=None) -> exchange_info() defaults
            exchange_info("0.001", "0.001", "50", "0.10", "TRADING")
        };
        let opts = FakeOptions {
            h1: Vec::new(),
            m15: Vec::new(),
            m1: Vec::new(),
            mark: 85_000.0,
            wallet: 5_000.0,
            info,
        };
        let fake = Arc::new(Fake::new(rows.clone(), opts));

        let mut cfg = Map::new();
        cfg.insert("enabled".to_string(), Value::Bool(true));
        cfg.insert(
            "trading_mode".to_string(),
            Value::String(if live {
                "LIVE".to_string()
            } else {
                "DRY_RUN".to_string()
            }),
        );
        for (k, v) in cfg_extra {
            cfg.insert(k, v);
        }
        let cfg = Arc::new(Mutex::new(Value::Object(cfg)));
        let notes = Arc::new(Mutex::new(Vec::new()));

        let mut h = Harness {
            rows,
            now_ms,
            fake,
            cfg,
            network: network.to_string(),
            notes,
            tmp_path: tmp_path.to_path_buf(),
            clock_ms: Arc::new(Mutex::new(now_ms)),
            service: SmaCrossService::new(
                Variant::BtcMa,
                ServiceDeps {
                    config_getter: Arc::new(|| Value::Null),
                    credentials_getter: Arc::new(|_n: &str| Value::Null),
                    exchange_factory: Arc::new(|_n: &str, _c: &Value| {
                        Err(ExchangeError::new("Exception", "unset"))
                    }),
                    runtime_dir: PathBuf::new(),
                    notifier: None,
                    main_network_getter: None,
                    clock: Arc::new(|| 0.0),
                    sleep: Arc::new(|_s: f64| Box::pin(async {})),
                    environ: HashMap::new(),
                    config_writer: None,
                    store_clock: Arc::new(SystemClock),
                },
            ),
        };
        h.service = h.build();
        h
    }

    fn build(&self) -> SmaCrossService<Fake> {
        let cfg_arc = self.cfg.clone();
        let config_getter: Arc<dyn Fn() -> Value + Send + Sync> =
            Arc::new(move || cfg_arc.lock().unwrap().clone());

        let credentials_getter: Arc<dyn Fn(&str) -> Value + Send + Sync> =
            Arc::new(|_n: &str| json!({"api_key": "k", "secret_key": "s"}));

        let fake = self.fake.clone();
        let exchange_factory: Arc<
            dyn Fn(&str, &Value) -> Result<Arc<Fake>, ExchangeError> + Send + Sync,
        > = Arc::new(move |_n: &str, _c: &Value| Ok(fake.clone()));

        let notes = self.notes.clone();
        let notifier: Option<Arc<dyn Fn(String) -> BoxFut<()> + Send + Sync>> =
            Some(Arc::new(move |text: String| {
                let notes = notes.clone();
                Box::pin(async move {
                    notes.lock().unwrap().push(text);
                })
            }));

        let network = self.network.clone();
        let main_network_getter: Option<Arc<dyn Fn() -> Option<String> + Send + Sync>> =
            Some(Arc::new(move || Some(network.clone())));

        *self.clock_ms.lock().unwrap() = self.now_ms;
        let clock_now = self.clock_ms.clone();
        let clock: Arc<dyn Fn() -> f64 + Send + Sync> =
            Arc::new(move || *clock_now.lock().unwrap() as f64 / 1000.0);

        let sleep: Arc<dyn Fn(f64) -> BoxFut<()> + Send + Sync> =
            Arc::new(|_s: f64| Box::pin(async {}));

        let deps = ServiceDeps {
            config_getter,
            credentials_getter,
            exchange_factory,
            runtime_dir: self.tmp_path.clone(),
            notifier,
            main_network_getter,
            clock,
            sleep,
            environ: HashMap::new(),
            config_writer: None,
            store_clock: Arc::new(SystemClock),
        };
        SmaCrossService::new(Variant::BtcMa, deps)
    }

    async fn run(&self) -> Value {
        self.service.run_cycle().await.unwrap()
    }

    fn state(&self, mode: &str) -> Map<String, Value> {
        self.service.load_state(&self.network, mode).unwrap()
    }

    fn state_live(&self) -> Map<String, Value> {
        self.state("LIVE")
    }

    /// Append a new closed bar that produces a cross the other way.
    fn next_bar(&mut self, last_close: f64) {
        let mut rows = Vec::with_capacity(self.rows.len() + 1);
        // rows[:-1] + [rows[-1]]
        for r in self.rows.iter().take(self.rows.len().saturating_sub(1)) {
            rows.push(r.clone());
        }
        if let Some(last) = self.rows.last() {
            rows.push(last.clone());
        }
        let prev_close = rows
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
            prev_close,
            prev_close.max(last_close) * 1.001,
            prev_close.min(last_close) * 0.999,
            last_close,
            10.0
        ]));
        self.rows = rows.clone();
        self.fake
            .bars
            .lock()
            .unwrap()
            .insert("1h".to_string(), rows.clone());
        self.now_ms = now_after_default(&rows);
        self.set_time_from_now();
    }

    fn set_now(&mut self, now_ms: i64) {
        self.now_ms = now_ms;
        self.set_time_from_now();
    }

    /// Push `self.now_ms` into the service clock (after `h.now_ms = ...`).
    fn set_time_from_now(&self) {
        *self.clock_ms.lock().unwrap() = self.now_ms;
    }

    fn advance_ms(&mut self, delta: i64) {
        self.now_ms += delta;
        self.set_time_from_now();
    }
}

fn approx(a: f64, b: f64) -> bool {
    (a - b).abs() < 1e-6
}

// ---------------------------------------------------------------- service tests

#[tokio::test]
async fn entry_uses_half_the_margin_at_5x_with_emergency_stop() {
    let root = temp_dir("entry");
    let h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    let r = h.run().await;
    assert_eq!(r["action"], json!("entered"));
    let state = h.state_live();
    let trade = state["trade"].as_object().cloned().unwrap();
    assert_eq!(trade["side"], json!("LONG"));
    let qty: f64 = trade["quantity"].as_str().unwrap().parse().unwrap();
    assert!((qty - 0.147).abs() < 1e-9);
    assert_eq!(h.fake.with_state(|s| s.leverage), 5);
    assert_eq!(h.fake.with_state(|s| s.margin_type.clone()), "isolated");
    let attempts = h.fake.with_state(|s| s.algo_attempts.clone());
    let stops: Vec<&Value> = attempts
        .iter()
        .filter(|a| a.get("type").and_then(|v| v.as_str()) == Some("STOP_MARKET"))
        .collect();
    assert_eq!(stops.len(), 1);
    assert_eq!(stops[0]["closePosition"], json!("true"));
    assert_eq!(stops[0]["triggerPrice"], json!("79900.0"));
    assert_eq!(stops[0]["workingType"], json!("MARK_PRICE"));
    assert!(attempts
        .iter()
        .all(|a| a.get("type").and_then(|v| v.as_str()) != Some("TAKE_PROFIT_MARKET")));
}

#[tokio::test]
async fn emergency_stop_off_places_no_stop() {
    let root = temp_dir("no_stop");
    let mut extra = HashMap::new();
    extra.insert("emergency_stop_roi_percent".to_string(), json!(0));
    let h = Harness::new(&root, true, "testnet", 86_000.0, extra);
    h.run().await;
    assert_eq!(h.state_live()["trade"]["stop_price"], json!(""));
    assert!(h.fake.with_state(|s| s.algo_attempts.clone()).is_empty());
}

#[tokio::test]
async fn profit_lock_ratchets_and_replaces_the_previous_lock() {
    let root = temp_dir("lock_ratchet");
    let h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    h.run().await;
    h.fake.with_state(|s| s.mark = 85_000.0 * 1.011);
    let _ = h.now_ms + 20_000;
    h.run().await;
    let trade = h.state_live()["trade"].as_object().cloned().unwrap();
    assert_eq!(trade["lock_roi"], json!("4.0"));
    assert_eq!(trade["lock_price"], json!("85680.0"));
    let attempts = h.fake.with_state(|s| s.algo_attempts.clone());
    let lock: Vec<&Value> = attempts
        .iter()
        .filter(|a| a.get("reduceOnly").and_then(|v| v.as_str()) == Some("true"))
        .collect();
    assert!(!lock.is_empty());
    assert_eq!(lock[0]["quantity"], trade["quantity"]);
    assert_eq!(lock[0]["workingType"], json!("CONTRACT_PRICE"));
    let first_lock = trade["lock_client_algo_id"].as_str().unwrap().to_string();

    h.fake.with_state(|s| s.mark = 85_000.0 * 1.021);
    h.run().await;
    let trade = h.state_live()["trade"].as_object().cloned().unwrap();
    assert_eq!(trade["lock_roi"], json!("9.0"));
    assert_eq!(trade["lock_price"], json!("86530.0"));
    let algos = h.fake.algos_snapshot();
    assert_eq!(algos[&first_lock]["algoStatus"], json!("CANCELED"));

    h.fake.with_state(|s| s.mark = 85_000.0 * 1.012);
    h.run().await;
    assert_eq!(h.state_live()["trade"]["lock_roi"], json!("9.0"));
}

#[tokio::test]
async fn profit_lock_exit_then_wait_for_the_next_cross() {
    let root = temp_dir("lock_exit");
    let h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    h.run().await;
    h.fake.with_state(|s| s.mark = 85_000.0 * 1.021);
    h.run().await;
    let lock_id = h.state_live()["trade"]["lock_client_algo_id"]
        .as_str()
        .unwrap()
        .to_string();
    h.fake.fire(&lock_id, 86_530.0);
    let result = h.run().await;
    assert_eq!(result["trade"]["exit_reason"], json!("PROFIT_LOCK"));
    let net_pnl: f64 = result["trade"]["net_pnl"]
        .as_str()
        .unwrap()
        .parse()
        .unwrap();
    assert!(net_pnl > 0.0);
    let orders = h.fake.with_state(|s| s.orders.len());
    assert_eq!(h.run().await["reason"], json!("WAITING_NEXT_CLOSE"));
    assert_eq!(h.fake.with_state(|s| s.orders.len()), orders);
    let algos = h.fake.algos_snapshot();
    assert!(algos
        .values()
        .all(|o| o.get("algoStatus").and_then(|v| v.as_str()) != Some("NEW")));
}

#[tokio::test]
async fn opposite_cross_closes_and_reverses_immediately() {
    let root = temp_dir("opposite");
    let mut h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    h.run().await;
    h.next_bar(83_000.0);
    let result = h.run().await;
    assert_eq!(result["action"], json!("reversed"), "{}", result);
    let ledger = h.service.ledger("testnet").unwrap();
    let closed = ledger
        .lock()
        .unwrap()
        .trades(Some("LIVE"), None, None, None, Some("CLOSED"))
        .unwrap();
    let last = closed.last().unwrap();
    assert_eq!(last["side"], json!("LONG"));
    assert_eq!(last["exit_reason"], json!("CROSS_REVERSE"));
    let trade = h.state_live();
    assert_eq!(trade["trade"]["side"], json!("SHORT"));
    assert_eq!(
        h.fake
            .with_state(|s| s.position.as_ref().map(|p| p["side"].clone())),
        Some(json!("short"))
    );
    let orders = h.fake.with_state(|s| s.orders.clone());
    assert!(orders
        .iter()
        .any(|o| o["clientOrderId"].as_str().unwrap_or("").contains("close")));
}

#[tokio::test]
async fn emergency_stop_failure_triggers_failsafe() {
    let root = temp_dir("failsafe");
    let h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    h.fake.with_state(|s| s.fail_sl_creates = 99);
    let result = h.run().await;
    assert_eq!(result["action"], json!("exited"));
    assert_eq!(result["trade"]["exit_reason"], json!("FAILSAFE"));
    assert!(h.fake.with_state(|s| s.position.is_none()));
}

#[tokio::test]
async fn below_exchange_minimum_is_skipped() {
    let root = temp_dir("below_min");
    let h = Harness::new(&root, true, "mainnet", 86_000.0, HashMap::new());
    h.fake.with_state(|s| s.wallet = 10.0);
    let result = h.run().await;
    let reason = result["reason"].as_str().unwrap_or("");
    assert!(reason.starts_with("BELOW_EXCHANGE_MINIMUM"), "{reason}");
    assert!(h.fake.with_state(|s| s.orders.is_empty()));
}

#[tokio::test]
async fn any_account_position_blocks_entry() {
    let root = temp_dir("account_pos");
    let h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    h.fake.with_state(|s| {
        s.other_positions = vec![json!({
            "symbol": "ETH/USDT:USDT",
            "side": "long",
            "contracts": 1.0,
            "entryPrice": 3000
        })];
    });
    let result = h.run().await;
    let reason = result["reason"].as_str().unwrap_or("");
    assert!(reason.starts_with("ACCOUNT_POSITION_EXISTS"), "{reason}");
}

#[tokio::test]
async fn dry_run_simulates_lock_exit_without_orders() {
    let root = temp_dir("dry_lock");
    let h = Harness::new(&root, false, "testnet", 86_000.0, HashMap::new());
    assert_eq!(h.run().await["action"], json!("entered"));
    let trade = h.state("DRY_RUN")["trade"].as_object().cloned().unwrap();
    let entry: f64 = trade["entry_price"].as_str().unwrap().parse().unwrap();
    let entry_ms = trade["entry_ms"].as_i64().unwrap();
    let t1 = entry_ms + M1;
    h.fake.with_state(|s| {
        s.m1 = vec![
            json!([t1, entry, entry * 1.021, entry, entry * 1.02, 1.0]),
            json!([
                t1 + M1,
                entry * 1.02,
                entry * 1.02,
                entry * 1.015,
                entry * 1.016,
                1.0
            ]),
        ];
    });
    let result = h.run().await;
    assert_eq!(result["action"], json!("exited"));
    assert_eq!(result["trade"]["exit_reason"], json!("PROFIT_LOCK"));
    assert!(h.fake.with_state(|s| s.orders.is_empty()));
    assert!(h.fake.with_state(|s| s.algo_attempts.is_empty()));
    let net_pnl: f64 = result["trade"]["net_pnl"]
        .as_str()
        .unwrap()
        .parse()
        .unwrap();
    assert!(net_pnl > 0.0);
}

#[tokio::test]
async fn dry_run_reverses_on_opposite_cross() {
    let root = temp_dir("dry_reverse");
    let mut h = Harness::new(&root, false, "testnet", 86_000.0, HashMap::new());
    h.run().await;
    h.next_bar(83_000.0);
    assert_eq!(h.run().await["action"], json!("reversed"));
    assert_eq!(h.state("DRY_RUN")["trade"]["side"], json!("SHORT"));
    assert!(h.fake.with_state(|s| s.orders.is_empty()));
}

#[tokio::test]
async fn owned_keys_and_client_prefix() {
    let root = temp_dir("owned_keys");
    let h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    assert!(h.service.owned_position_keys().unwrap().is_empty());
    h.run().await;
    let keys = h.service.owned_position_keys().unwrap();
    assert_eq!(keys, vec!["BTCUSDT".to_string()]);
    let attempts = h.fake.with_state(|s| s.algo_attempts.clone());
    assert!(attempts.iter().all(|a| a["clientAlgoId"]
        .as_str()
        .unwrap_or("")
        .starts_with("btcma-")));
    let orders = h.fake.with_state(|s| s.orders.clone());
    assert!(orders.iter().all(|o| o["clientOrderId"]
        .as_str()
        .unwrap_or("")
        .starts_with("btcma-")));
}

// ------------------------------------------- reversal cancels old protection

fn open_algos(fake: &Fake) -> Vec<Value> {
    fake.algos_snapshot()
        .into_values()
        .filter(|o| o.get("algoStatus").and_then(|v| v.as_str()) == Some("NEW"))
        .collect()
}

#[tokio::test]
async fn reversal_cancels_the_old_emergency_stop_and_places_a_new_one() {
    let root = temp_dir("rev_stop");
    let mut h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    h.run().await;
    let old_stop = h.state_live()["trade"]["sl_client_algo_id"]
        .as_str()
        .unwrap()
        .to_string();
    assert_eq!(h.fake.algos_snapshot()[&old_stop]["side"], json!("SELL"));
    h.next_bar(83_000.0);
    assert_eq!(h.run().await["action"], json!("reversed"));
    assert_eq!(
        h.fake.algos_snapshot()[&old_stop]["algoStatus"],
        json!("CANCELED")
    );
    let open = open_algos(&h.fake);
    assert_eq!(open.len(), 1);
    let new_stop = &open[0];
    let trade = h.state_live();
    let sl = trade["trade"]["sl_client_algo_id"].as_str().unwrap();
    assert_eq!(new_stop["clientAlgoId"].as_str().unwrap(), sl);
    assert_ne!(sl, old_stop);
    assert_eq!(new_stop["side"], json!("BUY"));
    assert_eq!(new_stop["orderType"], json!("STOP_MARKET"));
    let trigger: f64 = new_stop["triggerPrice"]
        .as_str()
        .map(|s| s.parse().unwrap())
        .unwrap_or_else(|| new_stop["triggerPrice"].as_f64().unwrap_or(0.0));
    let entry: f64 = trade["trade"]["entry_price"]
        .as_str()
        .unwrap()
        .parse()
        .unwrap();
    assert!(trigger > entry);
}

#[tokio::test]
async fn reversal_also_cancels_an_active_profit_lock() {
    let root = temp_dir("rev_lock");
    let mut h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    h.run().await;
    h.fake.with_state(|s| s.mark = 85_000.0 * 1.021);
    h.run().await;
    let trade = h.state_live()["trade"].as_object().cloned().unwrap();
    let sl = trade["sl_client_algo_id"].as_str().unwrap().to_string();
    let lk = trade["lock_client_algo_id"].as_str().unwrap().to_string();
    assert_eq!(open_algos(&h.fake).len(), 2);
    h.next_bar(83_000.0);
    assert_eq!(h.run().await["action"], json!("reversed"));
    let algos = h.fake.algos_snapshot();
    for id in [&sl, &lk] {
        assert_eq!(algos[id]["algoStatus"], json!("CANCELED"));
    }
    let remaining = open_algos(&h.fake);
    assert_eq!(remaining.len(), 1);
    assert_eq!(remaining[0]["side"], json!("BUY"));
    assert_eq!(h.state_live()["trade"]["lock_price"], json!(""));
}

#[tokio::test]
async fn reversal_never_enters_while_an_old_order_could_not_be_cancelled() {
    let root = temp_dir("rev_cancel_fail");
    let mut h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    h.run().await;
    let old_stop = h.state_live()["trade"]["sl_client_algo_id"]
        .as_str()
        .unwrap()
        .to_string();

    *h.fake.fail_algo_delete.lock().unwrap() = true;

    h.next_bar(83_000.0);
    let result = h.run().await;
    let reason = result["reason"].as_str().unwrap_or("");
    assert!(reason.contains("반대 진입 보류"), "{reason}");
    assert!(h.fake.with_state(|s| s.position.is_none()));
    assert_eq!(
        h.fake.algos_snapshot()[&old_stop]["algoStatus"],
        json!("NEW")
    );
    let entries: Vec<Value> = h
        .fake
        .with_state(|s| s.orders.clone())
        .into_iter()
        .filter(|o| o["clientOrderId"].as_str().unwrap_or("").contains("entry"))
        .collect();
    assert_eq!(entries.len(), 1);
    assert_eq!(
        h.state_live()["pending_reverse_entry"]["side"],
        json!("SHORT")
    );
    h.advance_ms(20_000);
    let reason2 = h.run().await["reason"].as_str().unwrap_or("").to_string();
    assert!(
        reason2.starts_with("REVERSE_ENTRY_RETRY: OPEN_ORDERS_CANCEL_FAILED"),
        "{reason2}"
    );
    // The exchange recovers: the old stop is cleaned up and the SHORT is taken.
    *h.fake.fail_algo_delete.lock().unwrap() = false;
    h.advance_ms(20_000);
    assert_eq!(h.run().await["action"], json!("entered"));
    assert_eq!(
        h.fake.algos_snapshot()[&old_stop]["algoStatus"],
        json!("CANCELED")
    );
    assert_eq!(
        h.fake
            .with_state(|s| s.position.as_ref().map(|p| p["side"].clone())),
        Some(json!("short"))
    );
    assert_eq!(h.state_live()["trade"]["side"], json!("SHORT"));
    assert!(h.state_live()["pending_reverse_entry"].is_null());
    assert_eq!(open_algos(&h.fake).len(), 1);
}

#[tokio::test]
async fn reverse_retry_gives_up_after_the_window() {
    let root = temp_dir("rev_expire");
    let mut h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    h.run().await;
    *h.fake.fail_algo_delete.lock().unwrap() = true;
    h.next_bar(83_000.0);
    h.run().await;
    h.advance_ms(11 * 60_000); // past the 10-minute retry window
    assert_eq!(h.run().await["reason"], json!("REVERSE_ENTRY_EXPIRED"));
    assert!(h.state_live()["pending_reverse_entry"].is_null());
    let notes = h.notes.lock().unwrap().clone();
    assert!(notes.iter().any(|n| n.contains("포기")));
    h.advance_ms(20_000);
    assert_eq!(h.run().await["reason"], json!("WAITING_NEXT_CLOSE"));
}

// --------------------------------------- stuck order records

/// `_stuck_fxs_record`: insert a stuck SUBMITTED_UNKNOWN FXS entry record.
fn stuck_fxs_record(service: &SmaCrossService<Fake>, network: &str) -> (OrderRecord, String) {
    let cid = "btcma-fxsusdt-l-entry-b5cff459a7a".to_string();
    let store = service.store(network).unwrap();
    let mut store = store.lock().unwrap();
    let record = store
        .upsert(OrderRecord {
            client_order_id: cid.clone(),
            symbol: "FXS/USDT:USDT".into(),
            side: "LONG".into(),
            strategy: "btcma".into(),
            signal_timestamp: "1".into(),
            requested_qty: 10.0,
            order_state: "SUBMITTED_UNKNOWN".into(),
            ..Default::default()
        })
        .unwrap();
    store
        .try_acquire_entry_lease("old-process", 120.0, "FXS/USDT:USDT", &cid, false, false)
        .unwrap();
    (record, cid)
}

fn order_state_of(service: &SmaCrossService<Fake>, cid: &str) -> String {
    let store = service.store("testnet").unwrap();
    let store = store.lock().unwrap();
    store
        .get(cid)
        .unwrap()
        .map(|r| r.order_state)
        .unwrap_or_default()
}

#[tokio::test]
async fn stuck_unknown_entry_on_another_coin_no_longer_blocks_entries() {
    let root = temp_dir("stuck_ok");
    let h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    let (record, cid) = stuck_fxs_record(&h.service, "testnet");
    assert_eq!(record.order_state, "SUBMITTED_UNKNOWN");
    {
        let store = h.service.store("testnet").unwrap();
        let store = store.lock().unwrap();
        store
            .set_runtime_state(
                "entry_lock_reason",
                &json!("RECONCILIATION_REQUIRED:position_fetch:TimeoutError"),
            )
            .unwrap();
        assert!(store
            .entry_block_reason(Some("BTC/USDT:USDT"), false, false)
            .unwrap()
            .unwrap_or_default()
            .starts_with("RUNTIME_ENTRY_LOCK"));
    }
    let result = h.run().await;
    assert_eq!(result["action"], json!("entered"), "{}", result);
    assert_eq!(order_state_of(&h.service, &cid), "CANCELED");
    {
        let store = h.service.store("testnet").unwrap();
        let store = store.lock().unwrap();
        assert!(store
            .get_runtime_state("entry_lock_reason", None)
            .unwrap()
            .is_none());
    }
    let notes = h.notes.lock().unwrap().clone();
    assert!(notes.iter().any(|n| n.contains("🧹")));
}

#[tokio::test]
async fn stuck_record_is_kept_while_its_position_really_exists() {
    let root = temp_dir("stuck_keep");
    let h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    let (_record, cid) = stuck_fxs_record(&h.service, "testnet");
    h.fake.extra_positions.lock().unwrap().push(json!({
        "symbol": "FXS/USDT:USDT",
        "side": "long",
        "contracts": 10.0,
        "entryPrice": 1.0
    }));
    let resolved = h
        .service
        .resolve_stale_entry_records("testnet")
        .await
        .unwrap();
    assert!(resolved.is_empty());
    assert_eq!(order_state_of(&h.service, &cid), "SUBMITTED_UNKNOWN");
}

#[tokio::test]
async fn stuck_record_is_kept_when_the_exchange_cannot_be_checked() {
    let root = temp_dir("stuck_timeout");
    let h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    let (_record, cid) = stuck_fxs_record(&h.service, "testnet");
    h.fake.with_state(|s| s.raise_positions = true);
    assert!(h
        .service
        .resolve_stale_entry_records("testnet")
        .await
        .unwrap()
        .is_empty());
    assert_eq!(order_state_of(&h.service, &cid), "SUBMITTED_UNKNOWN");
}

// ----------------------------- reverse when the close confirmation lags

/// Simulate Binance still reporting the old position for a few reads.
fn lagging_close(h: &Harness, stale_reads: i64) {
    *h.fake.lag_reads.lock().unwrap() = stale_reads;
    *h.fake.lag_left.lock().unwrap() = 0;
    *h.fake.lag_snapshot.lock().unwrap() = None;
}

#[tokio::test]
async fn reverse_survives_a_short_position_lag_in_the_same_cycle() {
    let root = temp_dir("lag_short");
    let mut h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    h.run().await;
    lagging_close(&h, 3);
    h.next_bar(83_000.0);
    assert_eq!(h.run().await["action"], json!("reversed"));
    let ledger = h.service.ledger("testnet").unwrap();
    let closed = ledger
        .lock()
        .unwrap()
        .trades(Some("LIVE"), None, None, None, Some("CLOSED"))
        .unwrap();
    assert_eq!(
        closed.last().unwrap()["exit_reason"],
        json!("CROSS_REVERSE")
    );
    assert_eq!(h.state_live()["trade"]["side"], json!("SHORT"));
}

#[tokio::test]
async fn reverse_is_kept_when_the_close_shows_up_only_next_cycle() {
    let root = temp_dir("lag_long");
    let mut h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    h.run().await;
    lagging_close(&h, 20);
    h.next_bar(83_000.0);
    let first = h.run().await;
    assert_eq!(first["action"], json!("closing"), "{}", first);
    let pending = h.state_live()["trade"]["pending_reverse"].clone();
    assert!(
        !pending.is_null() && pending != json!(false) && pending != json!({}),
        "{pending}"
    );
    for _ in 0..20 {
        let rows = h.fake.fetch_positions(None).await.unwrap_or_default();
        if rows.is_empty() {
            break;
        }
    }
    h.advance_ms(10_000);
    let result = h.run().await;
    assert_eq!(result["action"], json!("reversed"), "{}", result);
    let ledger = h.service.ledger("testnet").unwrap();
    let closed = ledger
        .lock()
        .unwrap()
        .trades(Some("LIVE"), None, None, None, Some("CLOSED"))
        .unwrap();
    assert_eq!(
        closed.last().unwrap()["exit_reason"],
        json!("CROSS_REVERSE")
    );
    assert_eq!(h.state_live()["trade"]["side"], json!("SHORT"));
}

// -------------------------------------------------- SMA200 / SMA120 selection

/// `_between_rows`: SMA3 (104) above SMA120 (~100) but below SMA200 (~108).
fn between_rows() -> Vec<Value> {
    let mut closes: Vec<f64> = Vec::with_capacity(220);
    closes.extend(std::iter::repeat_n(120.0, 100));
    closes.extend(std::iter::repeat_n(100.0, 117));
    closes.extend(std::iter::repeat_n(104.0, 3));
    closes
        .iter()
        .enumerate()
        .map(|(i, c)| json!([T0 + (i as i64) * H1, c, c * 1.001, c * 0.999, c, 10.0]))
        .collect()
}

#[tokio::test]
async fn switching_to_sma120_while_holding_follows_the_new_line() {
    let root = temp_dir("switch_sma");
    let mut extra = HashMap::new();
    extra.insert("entry_mode".to_string(), json!("trend"));
    let mut h = Harness::new(&root, true, "testnet", 86_000.0, extra);
    h.rows = between_rows();
    h.fake
        .bars
        .lock()
        .unwrap()
        .insert("1h".to_string(), h.rows.clone());
    h.now_ms = now_after_default(&h.rows);
    h.set_time_from_now();
    assert_eq!(h.run().await["action"], json!("entered"));
    assert_eq!(h.state_live()["trade"]["side"], json!("SHORT"));

    {
        let mut cfg = h.cfg.lock().unwrap();
        let mut obj = cfg.as_object().cloned().unwrap();
        obj.insert("slow_period".to_string(), json!(120));
        *cfg = Value::Object(obj);
    }
    let last_ts = h
        .rows
        .last()
        .and_then(|r| {
            r.as_array()
                .and_then(|a| a.first())
                .and_then(|v| v.as_i64())
        })
        .unwrap();
    h.rows
        .push(json!([last_ts + H1, 104.0, 104.1, 103.9, 104.0, 10.0]));
    h.fake
        .bars
        .lock()
        .unwrap()
        .insert("1h".to_string(), h.rows.clone());
    h.now_ms = now_after_default(&h.rows);
    h.set_time_from_now();
    let result = h.run().await;
    assert_eq!(result["action"], json!("reversed"), "{}", result);
    let ledger = h.service.ledger("testnet").unwrap();
    let closed = ledger
        .lock()
        .unwrap()
        .trades(Some("LIVE"), None, None, None, Some("CLOSED"))
        .unwrap();
    let last = closed.last().unwrap();
    assert_eq!(last["side"], json!("SHORT"));
    assert_eq!(last["exit_reason"], json!("CROSS_REVERSE"));
    let trade = h.state_live()["trade"].as_object().cloned().unwrap();
    assert_eq!(trade["side"], json!("LONG"));
    assert_eq!(trade["entry_type"], json!("REALIGN"));
    assert_eq!(trade["slow_period"], json!(120));
}

#[tokio::test]
async fn holding_on_the_right_side_is_not_touched() {
    let root = temp_dir("holding");
    let mut extra = HashMap::new();
    extra.insert("entry_mode".to_string(), json!("trend"));
    extra.insert("slow_period".to_string(), json!(120));
    let mut h = Harness::new(&root, true, "testnet", 86_000.0, extra);
    h.rows = between_rows();
    h.fake
        .bars
        .lock()
        .unwrap()
        .insert("1h".to_string(), h.rows.clone());
    h.now_ms = now_after_default(&h.rows);
    h.set_time_from_now();
    h.run().await;
    assert_eq!(h.state_live()["trade"]["side"], json!("LONG"));
    let last_ts = h
        .rows
        .last()
        .and_then(|r| {
            r.as_array()
                .and_then(|a| a.first())
                .and_then(|v| v.as_i64())
        })
        .unwrap();
    h.rows
        .push(json!([last_ts + H1, 104.0, 104.1, 103.9, 104.0, 10.0]));
    h.fake
        .bars
        .lock()
        .unwrap()
        .insert("1h".to_string(), h.rows.clone());
    h.now_ms = now_after_default(&h.rows);
    h.set_time_from_now();
    assert_eq!(h.run().await["action"], json!("managed"));
    assert_eq!(h.state_live()["trade"]["side"], json!("LONG"));
}

// Keep `service`, `approx`, `Map` referenced even if unused by later bodies.
#[allow(dead_code)]
fn _touches() {
    let _: Option<BoxFut<()>> = None;
    let _ = service::CCXT_SYMBOL;
    let _: Option<Map<String, Value>> = None;
    assert!(approx(1.0, 1.0));
}
