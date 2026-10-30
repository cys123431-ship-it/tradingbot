//! Shared test doubles for the SMA service tests: the Python `Fake(FakeBinance)`
//! subclass and `Harness` from tests/test_btc_ma_cross.py.
#![allow(unused, dead_code, clippy::type_complexity, clippy::too_many_arguments)]

pub mod scope;

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use serde_json::{json, Map, Value};

use tbot::core::candles::Candle;
pub use tbot::exchange::fake::{exchange_info, FakeBinance, FakeOptions, FakeState, H1, M1, T0};
use tbot::exchange::{ExchangeError, FuturesExchange};
use tbot::store::clock::SystemClock;
pub use tbot::store::order_state::OrderRecord;
pub use tbot::strategy::service::{BoxFut, ServiceDeps, SmaCrossService, Variant};

// ---------------------------------------------------------------- bars

/// n-1 flat closes then one final close: a cross happens on the last bar.
pub fn bars(last_close: f64, n: usize, base: f64, start_ms: i64, span: i64) -> Vec<Value> {
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

pub fn bars_default(last_close: f64) -> Vec<Value> {
    bars(last_close, 205, 85_000.0, T0, H1)
}

pub fn now_after(rows: &[Value], span: i64, seconds: i64) -> i64 {
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

pub fn now_after_default(rows: &[Value]) -> i64 {
    now_after(rows, H1, 5)
}

// ---------------------------------------------------------------- Fake

/// `Fake(FakeBinance)`: wraps `FakeBinance` but serves `bars["1h"]` from the
/// harness and adds `fire(client_prefix, price)`.
pub struct Fake {
    pub inner: FakeBinance,
    pub bars: Mutex<HashMap<String, Vec<Value>>>,
    /// Python `_lagging_close`: after a reduce-only close, report the old position N more times.
    pub lag_reads: Mutex<i64>,
    pub lag_left: Mutex<i64>,
    pub lag_snapshot: Mutex<Option<Value>>,
    /// Python `with_fxs`: rows appended to every fetch_positions result.
    pub extra_positions: Mutex<Vec<Value>>,
    /// Python `failing_cancel`: fapiPrivateDeleteAlgoOrder raises.
    pub fail_algo_delete: Mutex<bool>,
}

impl Fake {
    pub fn new(rows: Vec<Value>, opts: FakeOptions) -> Self {
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

    pub fn with_state<R>(&self, f: impl FnOnce(&mut FakeState) -> R) -> R {
        let mut s = self.inner.state.lock().unwrap();
        f(&mut s)
    }

    pub fn algos_snapshot(&self) -> HashMap<String, Value> {
        self.with_state(|s| s.algos.clone())
    }

    /// Binance fires one specific trigger (e.g. only the profit lock).
    pub fn fire(&self, client_prefix: &str, price: f64) {
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

pub fn temp_dir(tag: &str) -> PathBuf {
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

pub struct Harness {
    pub rows: Vec<Value>,
    pub now_ms: i64,
    pub fake: Arc<Fake>,
    pub cfg: Arc<Mutex<Value>>,
    pub network: String,
    pub notes: Arc<Mutex<Vec<String>>>,
    pub tmp_path: PathBuf,
    /// Shared with the service clock: Python tests mutate `h.now_ms`.
    pub clock_ms: Arc<Mutex<i64>>,
    pub service: SmaCrossService<Fake>,
}

impl Harness {
    pub fn new(
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

    pub fn build(&self) -> SmaCrossService<Fake> {
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

    pub async fn run(&self) -> Value {
        self.service.run_cycle().await.unwrap()
    }

    pub fn state(&self, mode: &str) -> Map<String, Value> {
        self.service.load_state(&self.network, mode).unwrap()
    }

    pub fn state_live(&self) -> Map<String, Value> {
        self.state("LIVE")
    }

    /// Append a new closed bar that produces a cross the other way.
    pub fn next_bar(&mut self, last_close: f64) {
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

    pub fn set_now(&mut self, now_ms: i64) {
        self.now_ms = now_ms;
        self.set_time_from_now();
    }

    /// Push `self.now_ms` into the service clock (after `h.now_ms = ...`).
    pub fn set_time_from_now(&self) {
        *self.clock_ms.lock().unwrap() = self.now_ms;
    }

    pub fn advance_ms(&mut self, delta: i64) {
        self.now_ms += delta;
        self.set_time_from_now();
    }
}

pub fn approx(a: f64, b: f64) -> bool {
    (a - b).abs() < 1e-6
}
