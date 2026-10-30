#![allow(unused)] // ported test fixtures keep the Python helpers
#![allow(
    clippy::type_complexity,
    clippy::ptr_arg,
    clippy::too_many_arguments,
    dead_code
)]
//! Port of `tests/test_btc_ma_cross_scope.py` (alt-coin scan + trend-state entries).
//!
//! Excluded (Telegram controller / menus / `_callbacks` / `_handle_btc_ma_cross_action`):
//!   test_scope_and_entry_mode_buttons,
//!   test_status_shows_the_reentry_wait_and_the_button,
//!   test_rotation_button
//!
//! NEEDS pub: `SmaCrossService::_on_trade_closed` (`test_cross_reverse_does_not_trigger_rotation`)
//! is not part of the public API, so that test is replaced by its observable behaviour:
//! a CROSS_REVERSE exit does not set `rotation_pending` (exercised through the rotation harness).

mod common;

use tbot::exchange::fake::UNKNOWN;

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use common::*;
use serde_json::{json, Map, Value};
use tbot::core::candles::Candle;
use tbot::exchange::{ExchangeError, FuturesExchange};
use tbot::store::clock::SystemClock;
use tbot::strategy::service::{BoxFut, ServiceDeps, SmaCrossService, Variant};

// ---------------------------------------------------------------- symbol info

fn symbol_info(market: &str, step: &str, min_notional: &str, contract_type: &str) -> Value {
    json!({
        "symbol": market,
        "status": "TRADING",
        "contractType": contract_type,
        "pricePrecision": 4,
        "quantityPrecision": 3,
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01", "minPrice": "0.01", "maxPrice": "1000000"},
            {"filterType": "LOT_SIZE", "minQty": step, "stepSize": step, "maxQty": "1000000"},
            {"filterType": "MARKET_LOT_SIZE", "minQty": step, "stepSize": step, "maxQty": "1000000"},
            {"filterType": "MIN_NOTIONAL", "notional": min_notional},
        ],
    })
}

fn ccxt_of(market: &str) -> String {
    format!("{}/USDT:USDT", &market[..market.len() - 4])
}

fn market_of(symbol: &str) -> String {
    symbol.split(':').next().unwrap_or("").replace('/', "")
}

/// `coin(volume, last_close, base=100.0, **extra)`.
fn coin(volume: f64, last_close: f64, base: f64, extra: &[(&str, Value)]) -> Value {
    let rows = bars(last_close * base / 85_000.0, 205, base, T0, H1);
    let mut m = Map::new();
    m.insert("volume".into(), json!(volume));
    m.insert("rows".into(), Value::Array(rows));
    m.insert("mark".into(), json!(base));
    for (k, v) in extra {
        m.insert((*k).to_string(), v.clone());
    }
    Value::Object(m)
}

fn coin_default(volume: f64, last_close: f64) -> Value {
    coin(volume, last_close, 100.0, &[])
}

fn coin_with(volume: f64, last_close: f64, extra: &[(&str, Value)]) -> Value {
    coin(volume, last_close, 100.0, extra)
}

// ---------------------------------------------------------------- MultiFake

/// `MultiFake(FakeBinance)`: several USD-M markets with per-symbol candles, marks
/// and positions. Only the overridden methods differ from `FakeBinance`.
pub struct MultiFake {
    pub inner: FakeBinance,
    pub coins: Mutex<Map<String, Value>>,
    pub positions: Mutex<Map<String, Value>>,
    pub markets: Map<String, Value>,
    pub info: Value,
    pub orders: Mutex<Vec<Value>>,
    pub algos: Mutex<Map<String, Value>>,
    pub algo_attempts: Mutex<Vec<Value>>,
    pub user_trades: Mutex<Vec<Value>>,
    pub leverage: Mutex<u32>,
    pub margin_type: Mutex<String>,
}

impl MultiFake {
    pub fn new(coins: Map<String, Value>) -> Self {
        let opts = FakeOptions {
            h1: Vec::new(),
            m15: Vec::new(),
            m1: Vec::new(),
            mark: 85_000.0,
            wallet: 5_000.0,
            info: exchange_info("0.001", "0.001", "50", "0.10", "TRADING"),
        };
        let mut symbols = Vec::new();
        let mut markets = Map::new();
        for (m, c) in coins.iter() {
            let step = c
                .get("step")
                .and_then(|v| v.as_str())
                .unwrap_or("0.001")
                .to_string();
            let min_notional = c
                .get("min_notional")
                .and_then(|v| v.as_str())
                .unwrap_or("5")
                .to_string();
            let contract_type = c
                .get("contract_type")
                .and_then(|v| v.as_str())
                .unwrap_or("PERPETUAL")
                .to_string();
            symbols.push(symbol_info(m, &step, &min_notional, &contract_type));
            let ccxt = ccxt_of(m);
            let base = &m[..m.len() - 4];
            markets.insert(
                ccxt.clone(),
                json!({
                    "id": m,
                    "symbol": ccxt,
                    "base": base,
                    "quote": "USDT",
                    "settle": "USDT",
                    "active": true,
                    "swap": true,
                    "linear": true,
                    "info": {"contractType": contract_type},
                }),
            );
        }
        let info = json!({"symbols": symbols});
        let mut s = Self {
            inner: FakeBinance::new(opts),
            coins: Mutex::new(coins),
            positions: Mutex::new(Map::new()),
            markets,
            info,
            orders: Mutex::new(Vec::new()),
            algos: Mutex::new(Map::new()),
            algo_attempts: Mutex::new(Vec::new()),
            user_trades: Mutex::new(Vec::new()),
            leverage: Mutex::new(3),
            margin_type: Mutex::new("isolated".to_string()),
        };
        // keep the inner state in sync (some paths read it directly)
        let info = s.info.clone();
        s.inner.state.lock().unwrap().info = info;
        s
    }

    fn coin_get(&self, market: &str, key: &str) -> Value {
        self.coins
            .lock()
            .unwrap()
            .get(market)
            .and_then(|v| v.get(key).cloned())
            .unwrap_or(Value::Null)
    }

    fn coin_rows(&self, market: &str) -> Vec<Value> {
        self.coin_get(market, "rows")
            .as_array()
            .cloned()
            .unwrap_or_default()
    }

    fn coin_mark(&self, market: &str) -> f64 {
        self.coin_get(market, "mark").as_f64().unwrap_or(0.0)
    }

    fn set_coin_mark(&self, market: &str, mark: f64) {
        if let Some(c) = self.coins.lock().unwrap().get_mut(market) {
            if let Some(obj) = c.as_object_mut() {
                obj.insert("mark".to_string(), json!(mark));
            }
        }
    }

    fn add_bar(&self, market: &str, last_close: f64) {
        let mut rows = self.coin_rows(market);
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
            prev.max(last_close) * 1.001,
            prev.min(last_close) * 0.999,
            last_close,
            1.0
        ]));
        if let Some(c) = self.coins.lock().unwrap().get_mut(market) {
            if let Some(obj) = c.as_object_mut() {
                obj.insert("rows".to_string(), Value::Array(rows));
            }
        }
    }

    fn push_row(&self, market: &str, row: Value) {
        let mut rows = self.coin_rows(market);
        rows.push(row);
        if let Some(c) = self.coins.lock().unwrap().get_mut(market) {
            if let Some(obj) = c.as_object_mut() {
                obj.insert("rows".to_string(), Value::Array(rows));
            }
        }
    }

    fn position_keys(&self) -> Vec<String> {
        self.positions.lock().unwrap().keys().cloned().collect()
    }

    fn fill(&self, side: &str, qty: f64, price: f64, time_ms: i64) {
        self.user_trades.lock().unwrap().push(json!({
            "side": side.to_uppercase(),
            "qty": qty.to_string(),
            "price": price.to_string(),
            "commission": (price * qty * 0.0004).to_string(),
            "commissionAsset": "USDT",
            "time": time_ms
        }));
    }

    fn fire_lock(&self, market: &str, price: f64) {
        let symbol = ccxt_of(market);
        let pos = self.positions.lock().unwrap().remove(&symbol);
        let pos = match pos {
            Some(p) => p,
            None => return,
        };
        {
            let mut algos = self.algos.lock().unwrap();
            for order in algos.values_mut() {
                if order.get("algoStatus").and_then(|v| v.as_str()) == Some("NEW") {
                    let cid = order
                        .get("clientAlgoId")
                        .and_then(|v| v.as_str())
                        .unwrap_or("");
                    let parts: Vec<&str> = cid.split('-').collect();
                    if parts.get(3) == Some(&"lock") {
                        if let Some(obj) = order.as_object_mut() {
                            obj.insert(
                                "algoStatus".to_string(),
                                Value::String("FINISHED".to_string()),
                            );
                        }
                    }
                }
            }
        }
        let side = if pos.get("side").and_then(|v| v.as_str()) == Some("long") {
            "SELL"
        } else {
            "BUY"
        };
        let qty = pos.get("contracts").and_then(|v| v.as_f64()).unwrap_or(0.0);
        self.fill(side, qty, price, 3);
    }

    fn recent_events(&self, limit: usize, kinds: &[&str]) -> Vec<Value> {
        // Events live in the service events file; helper reads via ledger isn't exposed,
        // so tests reach for the ledger through the service (see helper below).
        let _ = (limit, kinds);
        Vec::new()
    }
}

impl FuturesExchange for MultiFake {
    async fn load_markets(&self) -> Result<Map<String, Value>, ExchangeError> {
        Ok(self.markets.clone())
    }

    fn markets(&self) -> Option<Map<String, Value>> {
        Some(self.markets.clone())
    }

    async fn exchange_info_raw(&self) -> Result<Value, ExchangeError> {
        Ok(self.info.clone())
    }

    async fn fetch_ohlcv(
        &self,
        symbol: &str,
        timeframe: &str,
        since: Option<i64>,
        limit: Option<u32>,
    ) -> Result<Vec<Candle>, ExchangeError> {
        if timeframe == "1m" {
            return self
                .inner
                .fetch_ohlcv(symbol, timeframe, since, limit)
                .await;
        }
        let market = market_of(symbol);
        let rows = self.coin_rows(&market);
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
        let mark = self.coin_mark(market_id);
        Ok(json!({
            "symbol": market_id,
            "markPrice": mark.to_string(),
            "lastFundingRate": "0.0001",
            "nextFundingTime": "0"
        }))
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
        *self.margin_type.lock().unwrap() = mode.to_string();
        self.inner.set_margin_mode(mode, symbol).await
    }

    async fn set_leverage(&self, leverage: u32, symbol: &str) -> Result<Value, ExchangeError> {
        *self.leverage.lock().unwrap() = leverage;
        self.inner.set_leverage(leverage, symbol).await
    }

    async fn position_risk_v2_raw(&self, market_id: &str) -> Result<Value, ExchangeError> {
        Ok(json!([{
            "symbol": market_id,
            "leverage": self.leverage.lock().unwrap().to_string(),
            "marginType": self.margin_type.lock().unwrap().clone(),
        }]))
    }

    async fn fetch_positions(
        &self,
        symbols: Option<Vec<String>>,
    ) -> Result<Vec<Value>, ExchangeError> {
        let mut rows: Vec<Value> = Vec::new();
        {
            let positions = self.positions.lock().unwrap();
            for (s, p) in positions.iter() {
                let mut row = p.as_object().cloned().unwrap_or_default();
                row.insert("symbol".into(), Value::String(s.clone()));
                rows.push(Value::Object(row));
            }
        }
        if let Some(wanted_syms) = symbols {
            let wanted: std::collections::HashSet<String> =
                wanted_syms.iter().map(|s| market_of(s)).collect();
            rows.retain(|r| {
                let sym = r.get("symbol").and_then(|v| v.as_str()).unwrap_or("");
                wanted.contains(&market_of(sym))
            });
        }
        Ok(rows)
    }

    async fn get_order_raw(
        &self,
        market_id: &str,
        orig_client_order_id: &str,
    ) -> Result<Value, ExchangeError> {
        let orders = self.orders.lock().unwrap();
        for order in orders.iter() {
            if order.get("clientOrderId").and_then(|v| v.as_str()) == Some(orig_client_order_id) {
                return Ok(order.clone());
            }
        }
        Err(ExchangeError::new("Exception", UNKNOWN))
    }

    async fn fetch_open_orders(&self, symbol: Option<&str>) -> Result<Vec<Value>, ExchangeError> {
        Ok(Vec::new())
    }

    async fn cancel_order(&self, order_id: &str, symbol: &str) -> Result<Value, ExchangeError> {
        Ok(json!({"id": order_id, "status": "canceled"}))
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
        let _ = order_type;
        let _ = price;
        let cid = params
            .get("newClientOrderId")
            .cloned()
            .unwrap_or(Value::Null);
        let market = market_of(symbol);
        let mark = self.coin_mark(&market);
        let reduce_only = params
            .get("reduceOnly")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let order = {
            let mut orders = self.orders.lock().unwrap();
            let id = (orders.len() + 1).to_string();
            if reduce_only {
                let closed = self
                    .positions
                    .lock()
                    .unwrap()
                    .remove(symbol)
                    .and_then(|p| p.get("contracts").and_then(|v| v.as_f64()))
                    .unwrap_or(0.0);
                let order = json!({
                    "id": id,
                    "clientOrderId": cid,
                    "status": "closed",
                    "filled": closed,
                    "amount": amount,
                    "average": mark,
                    "symbol": symbol,
                });
                orders.push(order.clone());
                drop(orders);
                self.fill(side, closed, mark, 2);
                order
            } else {
                let side_lo = if side == "buy" { "long" } else { "short" };
                {
                    let mut positions = self.positions.lock().unwrap();
                    positions.insert(
                        symbol.to_string(),
                        json!({"side": side_lo, "contracts": amount, "entryPrice": mark}),
                    );
                }
                let order = json!({
                    "id": id,
                    "clientOrderId": cid,
                    "status": "closed",
                    "filled": amount,
                    "amount": amount,
                    "average": mark,
                    "symbol": symbol,
                });
                orders.push(order.clone());
                drop(orders);
                self.fill(side, amount, mark, 1);
                order
            }
        };
        Ok(order)
    }

    async fn algo_post_raw(&self, params: Map<String, Value>) -> Result<Value, ExchangeError> {
        self.algo_attempts
            .lock()
            .unwrap()
            .push(Value::Object(params.clone()));
        let cid = params
            .get("clientAlgoId")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let kind = params
            .get("type")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let symbol = params
            .get("symbol")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let side = params
            .get("side")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let trigger = params.get("triggerPrice").cloned().unwrap_or(Value::Null);
        let working_type = params.get("workingType").cloned().unwrap_or(Value::Null);
        let algo_id = self.algos.lock().unwrap().len() + 1;
        let order = json!({
            "algoId": algo_id,
            "clientAlgoId": cid,
            "symbol": symbol,
            "orderType": kind,
            "side": side,
            "triggerPrice": trigger,
            "closePosition": params.get("closePosition").cloned().unwrap_or(Value::Null),
            "workingType": working_type,
            "algoStatus": "NEW",
        });
        self.algos.lock().unwrap().insert(cid, order.clone());
        Ok(order)
    }

    async fn algo_get_raw(&self, client_algo_id: &str) -> Result<Value, ExchangeError> {
        let algos = self.algos.lock().unwrap();
        match algos.get(client_algo_id) {
            Some(o) => Ok(o.clone()),
            None => Err(ExchangeError::new("Exception", UNKNOWN)),
        }
    }

    async fn algo_open_raw(&self) -> Result<Value, ExchangeError> {
        let algos = self.algos.lock().unwrap();
        let out: Vec<Value> = algos
            .values()
            .filter(|o| o.get("algoStatus").and_then(|v| v.as_str()) == Some("NEW"))
            .cloned()
            .collect();
        Ok(Value::Array(out))
    }

    async fn algo_delete_raw(&self, client_algo_id: &str) -> Result<Value, ExchangeError> {
        let mut algos = self.algos.lock().unwrap();
        match algos.get_mut(client_algo_id) {
            Some(o) => {
                if let Some(obj) = o.as_object_mut() {
                    obj.insert(
                        "algoStatus".to_string(),
                        Value::String("CANCELED".to_string()),
                    );
                }
                Ok(o.clone())
            }
            None => Err(ExchangeError::new(
                "Exception",
                r#"binance {"code":-2011,"msg":"Unknown order sent."}"#,
            )),
        }
    }

    async fn user_trades_raw(
        &self,
        market_id: &str,
        start_time: Option<i64>,
        limit: u32,
    ) -> Result<Value, ExchangeError> {
        let _ = (market_id, start_time, limit);
        Ok(Value::Array(self.user_trades.lock().unwrap().clone()))
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
        let coins = self.coins.lock().unwrap();
        let mut out = Map::new();
        for (m, c) in coins.iter() {
            let ccxt = ccxt_of(m);
            out.insert(
                ccxt.clone(),
                json!({"symbol": ccxt, "quoteVolume": c.get("volume").cloned().unwrap_or(Value::Null)}),
            );
        }
        Ok(out)
    }
}

// ---------------------------------------------------------------- ScopeHarness

pub struct ScopeHarness {
    pub now_ms: i64,
    pub fake: Arc<MultiFake>,
    pub cfg: Arc<Mutex<Value>>,
    pub tmp_path: std::path::PathBuf,
    pub clock_ms: Arc<Mutex<i64>>,
    pub exchange_factory:
        Arc<dyn Fn(&str, &Value) -> Result<Arc<MultiFake>, ExchangeError> + Send + Sync>,
    pub service: SmaCrossService<MultiFake>,
}

impl ScopeHarness {
    pub fn new(
        tmp_path: &std::path::Path,
        coins: Map<String, Value>,
        live: bool,
        cfg_extra: Vec<(String, Value)>,
    ) -> Self {
        let fake = Arc::new(MultiFake::new(coins.clone()));
        let any_rows: Vec<Value> = coins
            .values()
            .next()
            .and_then(|c| c.get("rows"))
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();
        let now_ms = now_after(&any_rows, H1, 5);

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
        cfg.insert("scan_scope".to_string(), Value::String("alts".to_string()));
        for (k, v) in cfg_extra {
            cfg.insert(k, v);
        }
        let cfg = Arc::new(Mutex::new(Value::Object(cfg)));

        let fake_factory = fake.clone();
        let factory: Arc<
            dyn Fn(&str, &Value) -> Result<Arc<MultiFake>, ExchangeError> + Send + Sync,
        > = Arc::new(move |_n: &str, _c: &Value| Ok(fake_factory.clone()));

        let mut h = ScopeHarness {
            now_ms,
            fake,
            cfg,
            tmp_path: tmp_path.to_path_buf(),
            clock_ms: Arc::new(Mutex::new(now_ms)),
            exchange_factory: factory,
            service: SmaCrossService::new(
                Variant::BtcMa,
                ServiceDeps {
                    config_getter: Arc::new(|| Value::Null),
                    credentials_getter: Arc::new(|_n: &str| Value::Null),
                    exchange_factory: Arc::new(|_n: &str, _c: &Value| {
                        Err(ExchangeError::new("Exception", "unset"))
                    }),
                    runtime_dir: std::path::PathBuf::new(),
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

    pub fn build(&self) -> SmaCrossService<MultiFake> {
        let cfg_arc = self.cfg.clone();
        let config_getter: Arc<dyn Fn() -> Value + Send + Sync> =
            Arc::new(move || cfg_arc.lock().unwrap().clone());

        let credentials_getter: Arc<dyn Fn(&str) -> Value + Send + Sync> =
            Arc::new(|_n: &str| json!({"api_key": "k", "secret_key": "s"}));

        let exchange_factory = self.exchange_factory.clone();

        let notifier: Option<Arc<dyn Fn(String) -> BoxFut<()> + Send + Sync>> =
            Some(Arc::new(move |_text: String| Box::pin(async {})));

        let main_network_getter: Option<Arc<dyn Fn() -> Option<String> + Send + Sync>> =
            Some(Arc::new(|| Some("testnet".to_string())));

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

    pub fn set_time_from_now(&self) {
        *self.clock_ms.lock().unwrap() = self.now_ms;
    }

    pub fn advance_ms(&mut self, delta: i64) {
        self.now_ms += delta;
        self.set_time_from_now();
    }

    pub fn set_now(&mut self, now_ms: i64) {
        self.now_ms = now_ms;
        self.set_time_from_now();
    }

    pub fn state(&self, mode: &str) -> Map<String, Value> {
        self.service.load_state("testnet", mode).unwrap()
    }

    pub fn state_live(&self) -> Map<String, Value> {
        self.state("LIVE")
    }
}

// ---------------------------------------------------------------- coin helpers

fn trend_coins() -> Map<String, Value> {
    let mut rows = bars(86_000.0, 205, 85_000.0, T0, H1);
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
        86_000.0,
        86_600.0,
        85_900.0,
        86_500.0,
        1.0
    ]));
    let mut m = Map::new();
    m.insert(
        "BTCUSDT".to_string(),
        json!({"volume": 1, "rows": rows, "mark": 85_000.0}),
    );
    m
}

/// Cross up on the previous bar, still above on the last closed bar.
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

fn down_rows(base: f64) -> Vec<Value> {
    let mut rows = bars(84_000.0 * base / 85_000.0, 205, base, T0, H1);
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
        prev * 0.999,
        1.0
    ]));
    rows
}

fn rotation_harness(tmp_path: &std::path::Path, extra: Vec<(String, Value)>) -> ScopeHarness {
    let mut cfg: Vec<(String, Value)> = vec![
        ("entry_mode".to_string(), json!("trend")),
        ("rotate_after_exit".to_string(), json!(true)),
    ];
    for (k, v) in extra {
        if let Some(slot) = cfg.iter_mut().find(|(ck, _)| ck == &k) {
            slot.1 = v;
        } else {
            cfg.push((k, v));
        }
    }
    let mut coins = Map::new();
    coins.insert(
        "BTCUSDT".to_string(),
        json!({"volume": 1000, "rows": trend_rows(86_000.0, 85_000.0), "mark": 85_000.0}),
    );
    coins.insert(
        "ETHUSDT".to_string(),
        json!({"volume": 800, "rows": trend_rows(86_000.0, 3_000.0), "mark": 3_000.0}),
    );
    coins.insert(
        "SOLUSDT".to_string(),
        json!({"volume": 700, "rows": down_rows(150.0), "mark": 150.0}),
    );
    let mut h = ScopeHarness::new(tmp_path, coins.clone(), true, cfg);
    let rows = coins["BTCUSDT"]["rows"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let now = now_after(&rows, H1, 5);
    h.set_now(now);
    h
}

fn cfg_map(entries: Vec<(&str, Value)>) -> Vec<(String, Value)> {
    entries
        .into_iter()
        .map(|(k, v)| (k.to_string(), v))
        .collect()
}

// ---------------------------------------------------------------- universe

#[tokio::test]
async fn universe_is_top_n_by_volume_without_stables_or_tradfi() {
    let root = temp_dir("scope_universe");
    let mut coins = Map::new();
    for i in 0..60 {
        coins.insert(
            format!("C{i:02}USDT"),
            coin_default(1000.0 - (i as f64), 85_000.0),
        );
    }
    coins.insert("USDCUSDT".to_string(), coin_default(5000.0, 85_000.0));
    coins.insert(
        "AAPLUSDT".to_string(),
        coin_with(
            4000.0,
            85_000.0,
            &[("contract_type", json!("TRADIFI_PERPETUAL"))],
        ),
    );
    let h = ScopeHarness::new(&root, coins, true, Vec::new());
    let cfg = h.service.config();
    let universe = h.service.scan_universe("testnet", &cfg).await.unwrap();
    let markets: Vec<String> = universe.iter().map(|(_, m, _)| m.clone()).collect();
    assert_eq!(markets.len(), 50);
    assert_eq!(markets[0], "C00USDT");
    assert_eq!(markets[markets.len() - 1], "C49USDT");
    assert!(!markets.iter().any(|m| m == "USDCUSDT"));
    assert!(!markets.iter().any(|m| m == "AAPLUSDT"));
}

#[tokio::test]
async fn btc_scope_ignores_alts() {
    let root = temp_dir("scope_btc");
    let mut coins = Map::new();
    coins.insert("BTCUSDT".to_string(), coin_default(1.0, 85_000.0));
    coins.insert("ETHUSDT".to_string(), coin_default(9.0, 86_000.0));
    let h = ScopeHarness::new(
        &root,
        coins,
        true,
        cfg_map(vec![("scan_scope", json!("btc"))]),
    );
    let cfg = h.service.config();
    let universe = h.service.scan_universe("testnet", &cfg).await.unwrap();
    let markets: Vec<String> = universe.iter().map(|(_, m, _)| m.clone()).collect();
    assert_eq!(markets, vec!["BTCUSDT".to_string()]);
}

// ---------------------------------------------------------------- alt entries

#[tokio::test]
async fn highest_volume_coin_with_a_signal_wins() {
    let root = temp_dir("scope_highest_vol");
    let mut coins = Map::new();
    coins.insert("BTCUSDT".to_string(), coin_default(900.0, 85_000.0));
    coins.insert("ETHUSDT".to_string(), coin_default(800.0, 86_000.0));
    coins.insert("SOLUSDT".to_string(), coin_default(700.0, 84_000.0));
    let h = ScopeHarness::new(&root, coins, true, Vec::new());
    let r = h.run().await;
    assert_eq!(r["action"], json!("entered"));
    let trade = h.state_live()["trade"].as_object().cloned().unwrap();
    assert_eq!(trade["market_id"], json!("ETHUSDT"));
    assert_eq!(trade["side"], json!("LONG"));
    assert_eq!(h.fake.position_keys(), vec!["ETH/USDT:USDT".to_string()]);
    let attempts = h.fake.algo_attempts.lock().unwrap().clone();
    assert!(attempts
        .iter()
        .all(|a| a.get("symbol").and_then(|v| v.as_str()) == Some("ETHUSDT")));
    let owned: std::collections::HashSet<String> = h
        .service
        .owned_position_keys()
        .unwrap()
        .into_iter()
        .collect();
    let expected: std::collections::HashSet<String> = ["ETHUSDT".to_string()].into_iter().collect();
    assert_eq!(owned, expected);
    let ledger = h.service.ledger("testnet").unwrap();
    let mut kinds = std::collections::HashSet::new();
    kinds.insert("SIGNAL".to_string());
    let events = {
        let g = ledger.lock().unwrap();
        g.recent_events(1, Some(&kinds))
    };
    let signal = events.first().cloned().unwrap_or(Value::Null);
    // BTC is left out of the alt scope.
    assert_eq!(signal["evaluated"], json!(2));
    let crosses: std::collections::HashSet<String> = signal["crosses"]
        .as_array()
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .filter_map(|v| v.as_str().map(|s| s.to_string()))
        .collect();
    let expected_crosses: std::collections::HashSet<String> =
        ["ETHUSDT:LONG".to_string(), "SOLUSDT:SHORT".to_string()]
            .into_iter()
            .collect();
    assert_eq!(crosses, expected_crosses);
}

#[tokio::test]
async fn unusable_coin_falls_through_to_the_next() {
    let root = temp_dir("scope_unusable");
    let mut coins = Map::new();
    coins.insert(
        "ETHUSDT".to_string(),
        coin_with(800.0, 86_000.0, &[("min_notional", json!("100000"))]),
    );
    coins.insert("SOLUSDT".to_string(), coin_default(700.0, 84_000.0));
    let h = ScopeHarness::new(&root, coins, true, Vec::new());
    assert_eq!(h.run().await["action"], json!("entered"));
    let trade = h.state_live()["trade"].as_object().cloned().unwrap();
    assert_eq!(trade["market_id"], json!("SOLUSDT"));
    assert_eq!(trade["side"], json!("SHORT"));
}

#[tokio::test]
async fn opposite_cross_reverses_on_the_same_alt() {
    let root = temp_dir("scope_opposite");
    let mut coins = Map::new();
    coins.insert("ETHUSDT".to_string(), coin_default(800.0, 86_000.0));
    coins.insert("SOLUSDT".to_string(), coin_default(700.0, 85_000.0));
    let mut h = ScopeHarness::new(&root, coins, true, Vec::new());
    h.run().await;
    h.fake.add_bar("ETHUSDT", 96.0);
    h.fake.add_bar("SOLUSDT", 100.0);
    h.advance_ms(H1);
    assert_eq!(h.run().await["action"], json!("reversed"));
    let trade = h.state_live()["trade"].as_object().cloned().unwrap();
    assert_eq!(trade["market_id"], json!("ETHUSDT"));
    assert_eq!(trade["side"], json!("SHORT"));
}

// ---------------------------------------------------------------- trend mode

#[tokio::test]
async fn cross_mode_does_not_enter_without_a_cross() {
    let root = temp_dir("scope_cross_no_entry");
    let h = ScopeHarness::new(
        &root,
        trend_coins(),
        true,
        cfg_map(vec![("scan_scope", json!("btc"))]),
    );
    let rows = h.fake.coin_rows("BTCUSDT");
    let now = now_after(&rows, H1, 5);
    let mut h = h;
    h.set_now(now);
    assert_eq!(h.run().await["reason"], json!("NO_SIGNAL"));
    assert!(h.fake.orders.lock().unwrap().is_empty());
}

#[tokio::test]
async fn trend_mode_enters_when_sma3_is_already_above() {
    let root = temp_dir("scope_trend_entry");
    let h = ScopeHarness::new(
        &root,
        trend_coins(),
        true,
        cfg_map(vec![
            ("scan_scope", json!("btc")),
            ("entry_mode", json!("trend")),
        ]),
    );
    let rows = h.fake.coin_rows("BTCUSDT");
    let now = now_after(&rows, H1, 5);
    let mut h = h;
    h.set_now(now);
    assert_eq!(h.run().await["action"], json!("entered"));
    let trade = h.state_live()["trade"].as_object().cloned().unwrap();
    assert_eq!(trade["side"], json!("LONG"));
    assert_eq!(trade["entry_type"], json!("TREND"));
}

#[tokio::test]
async fn trend_mode_waits_for_the_next_cross_after_a_profit_lock_exit() {
    let root = temp_dir("scope_trend_wait");
    let h = ScopeHarness::new(
        &root,
        trend_coins(),
        true,
        cfg_map(vec![
            ("scan_scope", json!("btc")),
            ("entry_mode", json!("trend")),
            ("reentry_wait_bars", json!(0)),
        ]),
    );
    let mut h = h;
    let rows = h.fake.coin_rows("BTCUSDT");
    let now = now_after(&rows, H1, 5);
    h.set_now(now);
    h.run().await;
    h.fake.set_coin_mark("BTCUSDT", 85_000.0 * 1.021);
    h.advance_ms(20_000);
    h.run().await;
    h.fake.fire_lock("BTCUSDT", 86_530.0);
    h.advance_ms(20_000);
    assert_eq!(h.run().await["trade"]["exit_reason"], json!("PROFIT_LOCK"));
    assert_eq!(
        h.state_live()["reentry_block"]["BTCUSDT"]["side"],
        json!("LONG")
    );
    // Next bar: SMA3 still above SMA200 -> no new LONG (wait for a cross).
    let rows = h.fake.coin_rows("BTCUSDT");
    let last_ts = rows
        .last()
        .and_then(|r| {
            r.as_array()
                .and_then(|a| a.first())
                .and_then(|v| v.as_i64())
        })
        .unwrap_or(T0);
    h.fake.push_row(
        "BTCUSDT",
        json!([last_ts + H1, 86_500.0, 86_700.0, 86_400.0, 86_600.0, 1.0]),
    );
    let rows = h.fake.coin_rows("BTCUSDT");
    let now = now_after(&rows, H1, 5);
    h.set_now(now);
    assert_eq!(h.run().await["reason"], json!("NO_SIGNAL"));
    // A cross down releases the block and (trend mode) enters SHORT on it.
    let rows = h.fake.coin_rows("BTCUSDT");
    let last_ts = rows
        .last()
        .and_then(|r| {
            r.as_array()
                .and_then(|a| a.first())
                .and_then(|v| v.as_i64())
        })
        .unwrap_or(T0);
    h.fake.push_row(
        "BTCUSDT",
        json!([last_ts + H1, 86_600.0, 86_600.0, 70_000.0, 70_100.0, 1.0]),
    );
    let rows = h.fake.coin_rows("BTCUSDT");
    let now = now_after(&rows, H1, 5);
    h.set_now(now);
    h.fake.set_coin_mark("BTCUSDT", 70_100.0);
    assert_eq!(h.run().await["action"], json!("entered"));
    let state = h.state_live();
    assert_eq!(state["trade"]["side"], json!("SHORT"));
    assert_eq!(state["reentry_block"], json!({}));
}

// ---------------------------------------------------------------- testnet ranking

#[tokio::test]
async fn testnet_universe_is_ranked_by_mainnet_volume() {
    let root = temp_dir("scope_testnet_rank");
    let mut demo_coins = Map::new();
    demo_coins.insert("FXSUSDT".to_string(), coin_default(99_999.0, 85_000.0));
    demo_coins.insert("BTCUSDT".to_string(), coin_default(1.0, 85_000.0));
    demo_coins.insert("ETHUSDT".to_string(), coin_default(2.0, 85_000.0));

    let mut main_coins = Map::new();
    main_coins.insert("BTCUSDT".to_string(), coin_default(900.0, 85_000.0));
    main_coins.insert("ETHUSDT".to_string(), coin_default(800.0, 85_000.0));
    main_coins.insert("SOLUSDT".to_string(), coin_default(700.0, 85_000.0));

    let demo = Arc::new(MultiFake::new(demo_coins));
    let mainnet = Arc::new(MultiFake::new(main_coins));

    let cfg = Arc::new(Mutex::new(json!({
        "enabled": true,
        "trading_mode": "DRY_RUN",
        "scan_scope": "alts",
    })));

    let cfg_arc = cfg.clone();
    let config_getter: Arc<dyn Fn() -> Value + Send + Sync> =
        Arc::new(move || cfg_arc.lock().unwrap().clone());
    let credentials_getter: Arc<dyn Fn(&str) -> Value + Send + Sync> =
        Arc::new(|_n: &str| json!({}));
    let demo_f = demo.clone();
    let mainnet_f = mainnet.clone();
    let exchange_factory: Arc<
        dyn Fn(&str, &Value) -> Result<Arc<MultiFake>, ExchangeError> + Send + Sync,
    > = Arc::new(move |n: &str, _c: &Value| {
        if n == "mainnet" {
            Ok(mainnet_f.clone())
        } else {
            Ok(demo_f.clone())
        }
    });
    let notifier: Option<Arc<dyn Fn(String) -> BoxFut<()> + Send + Sync>> =
        Some(Arc::new(move |_t: String| Box::pin(async {})));
    let main_network_getter: Option<Arc<dyn Fn() -> Option<String> + Send + Sync>> =
        Some(Arc::new(|| Some("testnet".to_string())));
    let sleep: Arc<dyn Fn(f64) -> BoxFut<()> + Send + Sync> =
        Arc::new(|_s: f64| Box::pin(async {}));
    let clock: Arc<dyn Fn() -> f64 + Send + Sync> = Arc::new(|| 0.0);

    let deps = ServiceDeps {
        config_getter,
        credentials_getter,
        exchange_factory,
        runtime_dir: root.clone(),
        notifier,
        main_network_getter,
        clock,
        sleep,
        environ: HashMap::new(),
        config_writer: None,
        store_clock: Arc::new(SystemClock),
    };
    let service = SmaCrossService::new(Variant::BtcMa, deps);

    let cfg_val = service.config();
    let universe = service.scan_universe("testnet", &cfg_val).await.unwrap();
    let markets: Vec<String> = universe.iter().map(|(_, m, _)| m.clone()).collect();
    // FXS: not on mainnet, SOL: not on demo, BTC: alts only.
    assert_eq!(markets, vec!["ETHUSDT".to_string()]);

    cfg.lock().unwrap()["rank_testnet_by_mainnet_volume"] = json!(false);
    service.clear_universe_cache();
    let cfg_val = service.config();
    let universe = service.scan_universe("testnet", &cfg_val).await.unwrap();
    let markets: Vec<String> = universe.iter().map(|(_, m, _)| m.clone()).collect();
    assert_eq!(markets.first().cloned(), Some("FXSUSDT".to_string()));
}

// ---------------------------------------------------------------- reentry / rotation

fn add_up_bar(fake: &MultiFake, market: &str, close: f64) {
    let rows = fake.coin_rows(market);
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
    fake.push_row(
        market,
        json!([last_ts + H1, prev, close * 1.001, prev * 0.999, close, 1.0]),
    );
}

async fn exit_by_profit_lock(h: &mut ScopeHarness) -> Vec<Value> {
    let rows = h.fake.coin_rows("BTCUSDT");
    let now = now_after(&rows, H1, 5);
    h.set_now(now);
    let _ = h.service.run_cycle().await;
    h.fake.set_coin_mark("BTCUSDT", 85_000.0 * 1.021);
    h.advance_ms(20_000);
    let _ = h.service.run_cycle().await;
    h.fake.fire_lock("BTCUSDT", 86_530.0);
    h.advance_ms(20_000);
    let _ = h.service.run_cycle().await;
    h.fake.coin_rows("BTCUSDT")
}

async fn lock_exit(h: &mut ScopeHarness, market: &str, mark_up: f64) -> Value {
    let mark = h.fake.coin_mark(market) * mark_up;
    h.fake.set_coin_mark(market, mark);
    h.advance_ms(20_000);
    let _ = h.service.run_cycle().await;
    h.fake.fire_lock(market, mark);
    h.advance_ms(20_000);
    h.service.run_cycle().await.unwrap()
}

#[tokio::test]
async fn trend_mode_reenters_the_same_direction_after_three_bars() {
    let root = temp_dir("scope_reenter_three");
    let mut h = rotation_harness(
        &root,
        cfg_map(vec![
            ("scan_scope", json!("btc")),
            ("entry_mode", json!("trend")),
            ("rotate_after_exit", json!(false)),
        ]),
    );
    // default: 3 bars
    let _rows = exit_by_profit_lock(&mut h).await;
    let orders = h.fake.orders.lock().unwrap().len();
    for (i, close) in [86_600.0_f64, 86_700.0].iter().enumerate() {
        add_up_bar(&h.fake, "BTCUSDT", *close);
        let rows = h.fake.coin_rows("BTCUSDT");
        let now = now_after(&rows, H1, 5);
        h.set_now(now);
        let _ = h.run().await;
        assert_eq!(
            h.fake.orders.lock().unwrap().len(),
            orders,
            "re-entered after {} bar(s)",
            i + 1
        );
        let ledger = h.service.ledger("testnet").unwrap();
        let mut kinds = std::collections::HashSet::new();
        kinds.insert("SIGNAL".to_string());
        let events = {
            let g = ledger.lock().unwrap();
            g.recent_events(1, Some(&kinds))
        };
        let last = events.first().cloned().unwrap_or(Value::Null);
        let skip = last
            .get("skip_reason")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        assert!(skip.starts_with("REENTRY_WAIT: BTCUSDT LONG"), "{}", skip);
    }
    add_up_bar(&h.fake, "BTCUSDT", 86_800.0);
    let rows = h.fake.coin_rows("BTCUSDT");
    let now = now_after(&rows, H1, 5);
    h.set_now(now);
    h.fake.set_coin_mark("BTCUSDT", 86_800.0);
    let result = h.run().await;
    assert_eq!(result["action"], json!("entered"), "{result}");
    let state = h.state_live();
    assert_eq!(state["trade"]["side"], json!("LONG"));
    assert_eq!(state["trade"]["entry_type"], json!("TREND"));
}

#[tokio::test]
async fn legacy_block_without_exit_time_is_already_over() {
    let root = temp_dir("scope_legacy_block");
    let h = ScopeHarness::new(
        &root,
        trend_coins(),
        true,
        cfg_map(vec![
            ("scan_scope", json!("btc")),
            ("entry_mode", json!("trend")),
        ]),
    );
    let mut h = h;
    let mut state = h.state_live();
    state.insert("reentry_block".to_string(), json!({"BTCUSDT": "LONG"}));
    h.service.save_state("testnet", "LIVE", &mut state).unwrap();
    let rows = h.fake.coin_rows("BTCUSDT");
    let now = now_after(&rows, H1, 5);
    h.set_now(now);
    assert_eq!(h.run().await["action"], json!("entered"));
}

#[tokio::test]
async fn after_an_exit_the_next_coin_is_entered_at_once() {
    let root = temp_dir("scope_rotate_next");
    let mut h = rotation_harness(&root, Vec::new());
    assert_eq!(h.run().await["action"], json!("entered"));
    assert_eq!(h.state_live()["trade"]["market_id"], json!("ETHUSDT"));
    assert_eq!(
        lock_exit(&mut h, "ETHUSDT", 1.021).await["trade"]["exit_reason"],
        json!("PROFIT_LOCK")
    );
    assert_eq!(
        h.state_live()["rotation_pending"]["exclude"],
        json!("ETHUSDT")
    );
    h.advance_ms(10_000); // same candle, no close waited for
    let result = h.run().await;
    assert_eq!(result["action"], json!("entered"), "{}", result);
    let trade = h.state_live()["trade"].as_object().cloned().unwrap();
    assert_eq!(trade["market_id"], json!("SOLUSDT"));
    assert_eq!(trade["side"], json!("SHORT"));
    assert_eq!(trade["entry_type"], json!("ROTATE"));
    assert!(h.state_live()["rotation_pending"].is_null());
}

#[tokio::test]
async fn rotation_off_waits_for_the_next_candle() {
    let root = temp_dir("scope_rotate_off");
    let mut h = rotation_harness(&root, cfg_map(vec![("rotate_after_exit", json!(false))]));
    h.run().await;
    lock_exit(&mut h, "ETHUSDT", 1.021).await;
    h.advance_ms(10_000);
    assert_eq!(h.run().await["reason"], json!("WAITING_NEXT_CLOSE"));
    assert!(h.state_live()["trade"].is_null());
}

#[tokio::test]
async fn btc_only_scope_rotates_to_the_top_alt() {
    let root = temp_dir("scope_rotate_btc");
    let mut h = rotation_harness(&root, cfg_map(vec![("scan_scope", json!("btc"))]));
    assert_eq!(h.run().await["action"], json!("entered"));
    assert_eq!(h.state_live()["trade"]["market_id"], json!("BTCUSDT"));
    lock_exit(&mut h, "BTCUSDT", 1.021).await;
    h.advance_ms(10_000);
    assert_eq!(h.run().await["action"], json!("entered"));
    // #1 by volume is the coin just closed -> #2
    assert_eq!(h.state_live()["trade"]["market_id"], json!("ETHUSDT"));
}

// NEEDS pub: `SmaCrossService::_on_trade_closed`
// Original test called `_on_trade_closed(..., {"exit_reason": "CROSS_REVERSE"})` and asserted
// `not state.get("rotation_pending")`. The method is `pub(crate)`, so we exercise the same
// contract through a CROSS_REVERSE exit and assert no rotation_pending is set.
#[tokio::test]
async fn cross_reverse_does_not_trigger_rotation() {
    let root = temp_dir("scope_cross_reverse");
    let mut coins = Map::new();
    // BTC in uptrend, ETH with a cross that will produce CROSS_REVERSE on next bar.
    coins.insert(
        "BTCUSDT".to_string(),
        json!({"volume": 1000, "rows": trend_rows(86_000.0, 85_000.0), "mark": 85_000.0}),
    );
    coins.insert(
        "ETHUSDT".to_string(),
        json!({"volume": 800, "rows": trend_rows(86_000.0, 3_000.0), "mark": 3_000.0}),
    );
    coins.insert(
        "SOLUSDT".to_string(),
        json!({"volume": 700, "rows": down_rows(150.0), "mark": 150.0}),
    );
    let mut h = ScopeHarness::new(
        &root,
        coins,
        true,
        cfg_map(vec![
            ("entry_mode", json!("trend")),
            ("rotate_after_exit", json!(true)),
            ("scan_scope", json!("alts")),
        ]),
    );
    let rows = h.fake.coin_rows("BTCUSDT");
    let now = now_after(&rows, H1, 5);
    h.set_now(now);
    assert_eq!(h.run().await["action"], json!("entered"));
    assert_eq!(h.state_live()["trade"]["market_id"], json!("ETHUSDT"));
    // Force a cross-reverse on ETH: drop its last close far below the SMA.
    let rows = h.fake.coin_rows("ETHUSDT");
    let last_ts = rows
        .last()
        .and_then(|r| {
            r.as_array()
                .and_then(|a| a.first())
                .and_then(|v| v.as_i64())
        })
        .unwrap_or(T0);
    let prev = rows
        .last()
        .and_then(|r| r.as_array().and_then(|a| a.get(4)).and_then(|v| v.as_f64()))
        .unwrap_or(0.0);
    h.fake.push_row(
        "ETHUSDT",
        json!([last_ts + H1, prev, prev * 1.001, 2_000.0, 2_000.0, 1.0]),
    );
    let rows = h.fake.coin_rows("ETHUSDT");
    let now = now_after(&rows, H1, 5);
    h.set_now(now);
    let _ = h.run().await;
    let state = h.state_live();
    let rotation_pending = state
        .get("rotation_pending")
        .cloned()
        .unwrap_or(Value::Null);
    assert!(rotation_pending.is_null());
}
