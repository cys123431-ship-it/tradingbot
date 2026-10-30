use std::collections::HashMap;
use std::sync::Mutex;

use serde_json::{json, Map, Value};

use super::errors::ExchangeError;
use super::facade::FuturesExchange;
use crate::core::candles::Candle;

pub const H1: i64 = 3_600_000;
pub const M15: i64 = 900_000;
pub const M1: i64 = 60_000;
const T0_RAW: i64 = 1_790_000_000_000;
pub const T0: i64 = T0_RAW - (T0_RAW % H1);
pub const UNKNOWN: &str = r#"binance {"code":-2013,"msg":"Order does not exist."}"#;

fn to_candle(row: &Value) -> Candle {
    let arr = row.as_array().cloned().unwrap_or_default();
    let mut out = [0.0_f64; 6];
    for (i, v) in arr.iter().take(6).enumerate() {
        out[i] = v.as_f64().unwrap_or(0.0);
    }
    out
}

pub fn trend_1h(n: usize, start: f64, step: f64) -> Vec<Value> {
    let mut rows = Vec::with_capacity(n);
    let mut price = start;
    for i in 0..n {
        let o = price;
        let c = price * (1.0 + step);
        rows.push(json!([
            T0 + (i as i64) * H1,
            o,
            o.max(c) * 1.0005,
            o.min(c) * 0.9995,
            c,
            100.0
        ]));
        price = c;
    }
    rows
}

pub fn trend_1h_default() -> Vec<Value> {
    trend_1h(220, 80_000.0, 0.001)
}

pub fn flat_1h(n: usize, start: f64) -> Vec<Value> {
    let mut rows = Vec::with_capacity(n);
    for i in 0..n {
        let close = start * (if i % 2 == 0 { 0.9996 } else { 1.0004 });
        rows.push(json!([
            T0 + (i as i64) * H1,
            start,
            start * 1.0008,
            start * 0.9992,
            close,
            100.0
        ]));
    }
    rows
}

pub fn flat_1h_default() -> Vec<Value> {
    flat_1h(220, 80_000.0)
}

pub fn long_setup_15m(n: usize, start: f64, step: f64, confirm_scale: f64) -> Vec<Value> {
    let mut rows: Vec<Value> = Vec::with_capacity(n);
    let mut price = start;
    let end_ms = T0 + 220 * H1;
    let first = end_ms - (n as i64) * M15;
    for i in 0..(n.saturating_sub(4)) {
        let o = price;
        let c = price * (1.0 + step);
        rows.push(json!([
            first + (i as i64) * M15,
            o,
            c * 1.0010,
            o * 0.9990,
            c,
            50.0
        ]));
        price = c;
    }
    for k in 0..3 {
        let o = price;
        let c = price * (1.0 - 0.0011);
        rows.push(json!([
            first + ((n - 4 + k) as i64) * M15,
            o,
            o * 1.0002,
            c * 0.9996,
            c,
            50.0
        ]));
        price = c;
    }
    let prev_high = rows
        .last()
        .and_then(|r| r.as_array().and_then(|a| a.get(2)).and_then(|v| v.as_f64()))
        .unwrap_or(0.0);
    let o = price;
    let c = (prev_high * 1.0004).max(o * (1.0 + 0.0016 * confirm_scale));
    rows.push(json!([
        first + ((n - 1) as i64) * M15,
        o,
        c * 1.0002,
        o * 0.9997,
        c,
        80.0
    ]));
    rows
}

pub fn long_setup_15m_default() -> Vec<Value> {
    long_setup_15m(220, 80_000.0, 0.0003, 1.0)
}

pub fn mirror(rows: &[Value], center: f64) -> Vec<Value> {
    rows.iter()
        .map(|r| {
            let a = r.as_array().cloned().unwrap_or_default();
            let get = |i: usize| a.get(i).and_then(|v| v.as_f64()).unwrap_or(0.0);
            json!([
                get(0),
                center - get(1),
                center - get(3),
                center - get(2),
                center - get(4),
                get(5)
            ])
        })
        .collect()
}

pub fn mirror_default(rows: &[Value]) -> Vec<Value> {
    mirror(rows, 160_000.0)
}

pub fn now_after(rows15: &[Value], seconds: i64) -> i64 {
    let last = rows15
        .last()
        .and_then(|r| {
            r.as_array()
                .and_then(|a| a.first())
                .and_then(|v| v.as_i64())
        })
        .unwrap_or(T0);
    last + M15 + seconds * 1000
}

pub fn now_after_default(rows15: &[Value]) -> i64 {
    now_after(rows15, 5)
}

pub fn exchange_info(
    step: &str,
    min_qty: &str,
    min_notional: &str,
    tick: &str,
    status: &str,
) -> Value {
    json!({
        "symbols": [{
            "symbol": "BTCUSDT",
            "status": status,
            "contractType": "PERPETUAL",
            "pricePrecision": 2,
            "quantityPrecision": 3,
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": tick, "minPrice": "556.80", "maxPrice": "4529764"},
                {"filterType": "LOT_SIZE", "minQty": min_qty, "stepSize": step, "maxQty": "1000"},
                {"filterType": "MARKET_LOT_SIZE", "minQty": min_qty, "stepSize": step, "maxQty": "120"},
                {"filterType": "MIN_NOTIONAL", "notional": min_notional}
            ]
        }]
    })
}

pub fn exchange_info_default() -> Value {
    exchange_info("0.001", "0.001", "50", "0.10", "TRADING")
}

pub struct FakeOptions {
    pub h1: Vec<Value>,
    pub m15: Vec<Value>,
    pub m1: Vec<Value>,
    pub mark: f64,
    pub wallet: f64,
    pub info: Value,
}

impl Default for FakeOptions {
    fn default() -> Self {
        Self {
            h1: Vec::new(),
            m15: Vec::new(),
            m1: Vec::new(),
            mark: 85_000.0,
            wallet: 5_000.0,
            info: exchange_info_default(),
        }
    }
}

pub struct FakeState {
    pub h1: Vec<Value>,
    pub m15: Vec<Value>,
    pub m1: Vec<Value>,
    pub mark: f64,
    pub wallet: f64,
    pub info: Value,
    pub position: Option<Value>,
    pub other_positions: Vec<Value>,
    pub orders: Vec<Value>,
    pub algos: HashMap<String, Value>,
    pub algo_attempts: Vec<Value>,
    pub fail_sl_creates: u32,
    pub fail_tp_creates: u32,
    pub breach_sl: bool,
    pub fill_ratio: f64,
    pub raise_positions: bool,
    pub raise_klines: bool,
    pub dual: bool,
    pub leverage: u32,
    pub margin_type: String,
    pub user_trades: Vec<Value>,
    pub income: Vec<Value>,
}

pub struct FakeBinance {
    pub state: Mutex<FakeState>,
}

impl FakeBinance {
    pub fn new(opts: FakeOptions) -> Self {
        Self {
            state: Mutex::new(FakeState {
                h1: opts.h1,
                m15: opts.m15,
                m1: opts.m1,
                mark: opts.mark,
                wallet: opts.wallet,
                info: opts.info,
                position: None,
                other_positions: Vec::new(),
                orders: Vec::new(),
                algos: HashMap::new(),
                algo_attempts: Vec::new(),
                fail_sl_creates: 0,
                fail_tp_creates: 0,
                breach_sl: false,
                fill_ratio: 1.0,
                raise_positions: false,
                raise_klines: false,
                dual: false,
                leverage: 3,
                margin_type: "isolated".to_string(),
                user_trades: Vec::new(),
                income: Vec::new(),
            }),
        }
    }

    fn fill(state: &mut FakeState, side: &str, qty: f64, price: f64, time_ms: i64) {
        state.user_trades.push(json!({
            "side": side.to_uppercase(),
            "qty": qty.to_string(),
            "price": price.to_string(),
            "commission": (price * qty * 0.0004).to_string(),
            "commissionAsset": "USDT",
            "time": time_ms
        }));
    }

    /// Simulates Binance firing the SL or TP trigger.
    pub fn trigger(&self, kind: &str, price: f64) {
        let mut state = self.state.lock().unwrap();
        let (side, qty) = match state.position.as_ref() {
            Some(p) => {
                let pos_side = p.get("side").and_then(|v| v.as_str()).unwrap_or("long");
                let side = if pos_side == "long" { "SELL" } else { "BUY" };
                let qty = p.get("contracts").and_then(|v| v.as_f64()).unwrap_or(0.0);
                (side.to_string(), qty)
            }
            None => ("SELL".to_string(), 0.0),
        };
        let keys: Vec<String> = state.algos.keys().cloned().collect();
        for key in keys {
            if let Some(order) = state.algos.get_mut(&key) {
                let status = order
                    .get("algoStatus")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let ot = order
                    .get("orderType")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                if status == "NEW" && ot == kind {
                    if let Some(obj) = order.as_object_mut() {
                        obj.insert(
                            "algoStatus".to_string(),
                            Value::String("FINISHED".to_string()),
                        );
                    }
                }
            }
        }
        state.position = None;
        FakeBinance::fill(&mut state, &side, qty, price, 3);
    }
}

fn algo_key_from_params(params: &Map<String, Value>) -> Option<String> {
    params
        .get("clientAlgoId")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
}

fn parse_params_map(params: &Map<String, Value>) -> Map<String, Value> {
    params.clone()
}

impl FuturesExchange for FakeBinance {
    async fn load_markets(&self) -> Result<Map<String, Value>, ExchangeError> {
        Ok(Map::new())
    }

    fn markets(&self) -> Option<Map<String, Value>> {
        Some(Map::new())
    }

    async fn exchange_info_raw(&self) -> Result<Value, ExchangeError> {
        let state = self.state.lock().unwrap();
        Ok(state.info.clone())
    }

    async fn fetch_ohlcv(
        &self,
        _symbol: &str,
        timeframe: &str,
        _since: Option<i64>,
        _limit: Option<u32>,
    ) -> Result<Vec<Candle>, ExchangeError> {
        let state = self.state.lock().unwrap();
        if state.raise_klines {
            return Err(ExchangeError::new(
                "ConnectionError",
                "websocket/REST disconnected",
            ));
        }
        let rows = match timeframe {
            "1h" => &state.h1,
            "15m" => &state.m15,
            "1m" => &state.m1,
            _ => {
                return Err(ExchangeError::new(
                    "Exception",
                    format!("unknown timeframe {timeframe}"),
                ))
            }
        };
        Ok(rows.iter().map(to_candle).collect())
    }

    async fn premium_index_raw(&self, _market_id: &str) -> Result<Value, ExchangeError> {
        let state = self.state.lock().unwrap();
        Ok(json!({
            "symbol": "BTCUSDT",
            "markPrice": state.mark.to_string(),
            "lastFundingRate": "0.0001",
            "nextFundingTime": "0"
        }))
    }

    async fn commission_rate_raw(&self, _market_id: &str) -> Result<Value, ExchangeError> {
        Ok(json!({
            "symbol": "BTCUSDT",
            "takerCommissionRate": "0.0004",
            "makerCommissionRate": "0.0002"
        }))
    }

    async fn fetch_balance(&self) -> Result<Value, ExchangeError> {
        let state = self.state.lock().unwrap();
        let w = state.wallet.to_string();
        Ok(json!({
            "info": {
                "totalWalletBalance": w,
                "availableBalance": w,
                "totalMarginBalance": w
            }
        }))
    }

    async fn position_side_dual_raw(&self) -> Result<Value, ExchangeError> {
        let state = self.state.lock().unwrap();
        Ok(json!({"dualSidePosition": state.dual}))
    }

    async fn set_margin_mode(&self, mode: &str, _symbol: &str) -> Result<Value, ExchangeError> {
        let mut state = self.state.lock().unwrap();
        state.margin_type = mode.to_string();
        Ok(Value::Null)
    }

    async fn set_leverage(&self, leverage: u32, _symbol: &str) -> Result<Value, ExchangeError> {
        let mut state = self.state.lock().unwrap();
        state.leverage = leverage;
        Ok(Value::Null)
    }

    async fn position_risk_v2_raw(&self, _market_id: &str) -> Result<Value, ExchangeError> {
        let state = self.state.lock().unwrap();
        Ok(json!([{
            "symbol": "BTCUSDT",
            "leverage": state.leverage.to_string(),
            "marginType": state.margin_type
        }]))
    }

    async fn fetch_positions(
        &self,
        symbols: Option<Vec<String>>,
    ) -> Result<Vec<Value>, ExchangeError> {
        let state = self.state.lock().unwrap();
        if state.raise_positions {
            return Err(ExchangeError::new("TimeoutError", "positions timeout"));
        }
        let mut rows: Vec<Value> = Vec::new();
        if let Some(pos) = state.position.as_ref() {
            let mut obj = pos.as_object().cloned().unwrap_or_default();
            obj.insert(
                "symbol".to_string(),
                Value::String("BTC/USDT:USDT".to_string()),
            );
            rows.push(Value::Object(obj));
        }
        if symbols.is_none() {
            for p in &state.other_positions {
                rows.push(p.clone());
            }
        }
        Ok(rows)
    }

    async fn get_order_raw(
        &self,
        _market_id: &str,
        orig_client_order_id: &str,
    ) -> Result<Value, ExchangeError> {
        let state = self.state.lock().unwrap();
        for order in &state.orders {
            if order.get("clientOrderId").and_then(|v| v.as_str()) == Some(orig_client_order_id) {
                return Ok(order.clone());
            }
        }
        Err(ExchangeError::new("Exception", UNKNOWN))
    }

    async fn fetch_open_orders(&self, _symbol: Option<&str>) -> Result<Vec<Value>, ExchangeError> {
        Ok(Vec::new())
    }

    async fn cancel_order(&self, order_id: &str, _symbol: &str) -> Result<Value, ExchangeError> {
        Ok(json!({"id": order_id, "status": "canceled"}))
    }

    async fn create_order(
        &self,
        _symbol: &str,
        _order_type: &str,
        side: &str,
        amount: f64,
        _price: Option<f64>,
        params: Map<String, Value>,
    ) -> Result<Value, ExchangeError> {
        let mut state = self.state.lock().unwrap();
        let cid = params
            .get("newClientOrderId")
            .cloned()
            .unwrap_or(Value::Null);
        let reduce_only = params
            .get("reduceOnly")
            .and_then(|v| match v {
                Value::Bool(b) => Some(*b),
                Value::String(s) => Some(s == "true"),
                _ => None,
            })
            .unwrap_or(false);
        let order_id = (state.orders.len() + 1).to_string();
        let order = if reduce_only {
            let closed = state
                .position
                .as_ref()
                .and_then(|p| p.get("contracts"))
                .and_then(|v| v.as_f64())
                .unwrap_or(0.0);
            state.position = None;
            let order = json!({
                "id": order_id,
                "clientOrderId": cid,
                "status": "closed",
                "filled": closed,
                "amount": amount,
                "average": state.mark
            });
            let side_upper = side.to_uppercase();
            let mark = state.mark;
            FakeBinance::fill(&mut state, &side_upper, closed, mark, 2);
            order
        } else {
            let filled = (amount * state.fill_ratio * 1_000_000.0).round() / 1_000_000.0;
            let pos_side = if side == "buy" { "long" } else { "short" };
            state.position = Some(json!({
                "side": pos_side,
                "contracts": filled,
                "entryPrice": state.mark
            }));
            let status = if state.fill_ratio >= 1.0 {
                "closed"
            } else {
                "open"
            };
            let order = json!({
                "id": order_id,
                "clientOrderId": cid,
                "status": status,
                "filled": filled,
                "amount": amount,
                "average": state.mark
            });
            let side_upper = side.to_uppercase();
            let mark = state.mark;
            FakeBinance::fill(&mut state, &side_upper, filled, mark, 1);
            order
        };
        state.orders.push(order.clone());
        Ok(order)
    }

    async fn algo_post_raw(&self, params: Map<String, Value>) -> Result<Value, ExchangeError> {
        let mut state = self.state.lock().unwrap();
        let params_value = Value::Object(parse_params_map(&params));
        state.algo_attempts.push(params_value);
        let kind = params
            .get("type")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        if kind == "STOP_MARKET" {
            if state.breach_sl {
                return Err(ExchangeError::new(
                    "Exception",
                    r#"binance {"code":-2021,"msg":"Order would immediately trigger."}"#,
                ));
            }
            if state.fail_sl_creates > 0 {
                state.fail_sl_creates -= 1;
                return Err(ExchangeError::new("TimeoutError", "algo order timeout"));
            }
        }
        if kind == "TAKE_PROFIT_MARKET" && state.fail_tp_creates > 0 {
            state.fail_tp_creates -= 1;
            return Err(ExchangeError::new("TimeoutError", "algo order timeout"));
        }
        let client_algo_id = params.get("clientAlgoId").cloned().unwrap_or(Value::Null);
        let order = json!({
            "algoId": state.algos.len() + 1,
            "clientAlgoId": client_algo_id,
            "symbol": "BTCUSDT",
            "orderType": kind,
            "side": params.get("side").cloned().unwrap_or(Value::Null),
            "triggerPrice": params.get("triggerPrice").cloned().unwrap_or(Value::Null),
            "closePosition": params.get("closePosition").cloned().unwrap_or(Value::Null),
            "workingType": params.get("workingType").cloned().unwrap_or(Value::Null),
            "algoStatus": "NEW"
        });
        if let Some(key) = algo_key_from_params(&params) {
            state.algos.insert(key, order.clone());
        }
        Ok(order)
    }

    async fn algo_get_raw(&self, client_algo_id: &str) -> Result<Value, ExchangeError> {
        let state = self.state.lock().unwrap();
        match state.algos.get(client_algo_id) {
            Some(order) => Ok(order.clone()),
            None => Err(ExchangeError::new("Exception", UNKNOWN)),
        }
    }

    async fn algo_open_raw(&self) -> Result<Value, ExchangeError> {
        let state = self.state.lock().unwrap();
        let mut out = Vec::new();
        for order in state.algos.values() {
            if order.get("algoStatus").and_then(|v| v.as_str()) == Some("NEW") {
                out.push(order.clone());
            }
        }
        Ok(Value::Array(out))
    }

    async fn algo_delete_raw(&self, client_algo_id: &str) -> Result<Value, ExchangeError> {
        let mut state = self.state.lock().unwrap();
        match state.algos.get_mut(client_algo_id) {
            Some(order) => {
                if let Some(obj) = order.as_object_mut() {
                    obj.insert(
                        "algoStatus".to_string(),
                        Value::String("CANCELED".to_string()),
                    );
                }
                Ok(order.clone())
            }
            None => Err(ExchangeError::new(
                "Exception",
                r#"binance {"code":-2011,"msg":"Unknown order sent."}"#,
            )),
        }
    }

    async fn user_trades_raw(
        &self,
        _market_id: &str,
        _start_time: Option<i64>,
        _limit: u32,
    ) -> Result<Value, ExchangeError> {
        let state = self.state.lock().unwrap();
        Ok(Value::Array(state.user_trades.clone()))
    }

    async fn income_raw(
        &self,
        _market_id: &str,
        _income_type: Option<&str>,
        _start_time: Option<i64>,
        _limit: u32,
    ) -> Result<Value, ExchangeError> {
        let state = self.state.lock().unwrap();
        Ok(Value::Array(state.income.clone()))
    }

    async fn fetch_tickers(&self) -> Result<Map<String, Value>, ExchangeError> {
        Ok(Map::new())
    }
}
