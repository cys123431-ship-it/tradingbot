use std::sync::atomic::{AtomicI64, Ordering};
use std::time::Duration;

use hmac::{Hmac, Mac};
use serde_json::{json, Value};
use sha2::Sha256;

use crate::core::candles::Candle;

type HmacSha256 = Hmac<Sha256>;

// ---------------------------------------------------------------------------
// Network / credentials
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Network {
    Mainnet,
    Testnet,
}

impl Network {
    pub fn base_url(self) -> &'static str {
        match self {
            Network::Mainnet => "https://fapi.binance.com",
            Network::Testnet => "https://demo-fapi.binance.com",
        }
    }
}

#[derive(Clone)]
pub struct Credentials {
    pub api_key: String,
    pub secret: String,
}

impl std::fmt::Debug for Credentials {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let head: String = self.api_key.chars().take(4).collect();
        f.debug_struct("Credentials")
            .field("api_key", &format!("{head}..."))
            .field("secret", &"<redacted>")
            .finish()
    }
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

#[derive(Debug, thiserror::Error)]
pub enum BinanceError {
    #[error("binance api error {code}: {msg} (http {status})")]
    Api { status: u16, code: i64, msg: String },
    #[error("rate limited (http {status}), retry after {retry_after_secs:?}s")]
    RateLimited {
        status: u16,
        retry_after_secs: Option<u64>,
    },
    #[error("http {status}: {body}")]
    Http { status: u16, body: String },
    #[error("network: {message}")]
    Network { message: String, sent: bool },
    #[error("decode: {0}")]
    Decode(String),
    #[error("invalid request: {0}")]
    Invalid(String),
    #[error("missing api credentials")]
    MissingCredentials,
}

impl BinanceError {
    /// The request may or may not have reached the matching engine.
    pub fn outcome_unknown(&self) -> bool {
        match self {
            BinanceError::Network { sent, .. } => *sent,
            BinanceError::Http { status, .. } => *status >= 500,
            BinanceError::Api { status, code, .. } => *status >= 500 || *code == -1007,
            _ => false,
        }
    }
}

// ---------------------------------------------------------------------------
// URL encoding (unreserved: ALPHA / DIGIT / '-' '_' '.' '~')
// ---------------------------------------------------------------------------

pub fn urlencode(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    for byte in input.bytes() {
        let b = byte;
        let unreserved = b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~');
        if unreserved {
            out.push(b as char);
        } else {
            out.push('%');
            out.push_str(&format!("{:02X}", b));
        }
    }
    out
}

fn encode_params(params: &[(String, String)]) -> String {
    let mut out = String::new();
    for (i, (k, v)) in params.iter().enumerate() {
        if i > 0 {
            out.push('&');
        }
        out.push_str(&urlencode(k));
        out.push('=');
        out.push_str(&urlencode(v));
    }
    out
}

// ---------------------------------------------------------------------------
// Order parameter structs
// ---------------------------------------------------------------------------

pub struct NewOrder {
    pub symbol: String,
    pub side: String,
    pub order_type: String,
    pub quantity: Option<String>,
    pub price: Option<String>,
    pub time_in_force: Option<String>,
    pub reduce_only: bool,
    pub position_side: Option<String>,
    pub new_client_order_id: String,
    pub new_order_resp_type: Option<String>,
}

pub struct NewAlgoOrder {
    pub symbol: String,
    pub side: String,
    pub order_type: String,
    pub quantity: Option<String>,
    pub trigger_price: String,
    pub client_algo_id: String,
    pub reduce_only: bool,
    pub working_type: String,
    pub price_protect: bool,
    pub position_side: Option<String>,
    pub close_position: bool,
    pub price: Option<String>,
}

const CONDITIONAL_TYPES: [&str; 5] = [
    "STOP",
    "STOP_MARKET",
    "TAKE_PROFIT",
    "TAKE_PROFIT_MARKET",
    "TRAILING_STOP_MARKET",
];

fn bool_text(value: bool) -> String {
    if value {
        "true".to_string()
    } else {
        "false".to_string()
    }
}

// ---------------------------------------------------------------------------
// Method verb
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Method {
    Get,
    Post,
    Delete,
}

impl Method {
    fn is_get(self) -> bool {
        matches!(self, Method::Get)
    }
}

// ---------------------------------------------------------------------------
// Client
// ---------------------------------------------------------------------------

pub struct BinanceClient {
    http: reqwest::Client,
    base_url: String,
    creds: Option<Credentials>,
    recv_window_ms: u64,
    time_offset_ms: AtomicI64,
}

impl BinanceClient {
    pub fn new(network: Network, creds: Option<Credentials>) -> Self {
        Self::with_base_url(network.base_url(), creds)
    }

    pub fn with_base_url(base_url: &str, creds: Option<Credentials>) -> Self {
        let http = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(10))
            .build()
            .expect("failed to build reqwest client");
        BinanceClient {
            http,
            base_url: base_url.trim_end_matches('/').to_string(),
            creds,
            recv_window_ms: 5000,
            time_offset_ms: AtomicI64::new(0),
        }
    }

    pub fn sign(secret: &str, query: &str) -> String {
        let mut mac =
            HmacSha256::new_from_slice(secret.as_bytes()).expect("HMAC accepts any key length");
        mac.update(query.as_bytes());
        hex::encode(mac.finalize().into_bytes())
    }

    fn now_ms(&self) -> i64 {
        let now = chrono_like_now_ms();
        now + self.time_offset_ms.load(Ordering::Relaxed)
    }

    // -- core request path -------------------------------------------------

    async fn send(
        &self,
        method: Method,
        path: &str,
        mut params: Vec<(String, String)>,
        signed: bool,
    ) -> Result<Value, BinanceError> {
        if signed {
            let secret = match &self.creds {
                Some(c) => c.secret.clone(),
                None => return Err(BinanceError::MissingCredentials),
            };
            let api_key = match &self.creds {
                Some(c) => c.api_key.clone(),
                None => return Err(BinanceError::MissingCredentials),
            };
            params.push(("recvWindow".to_string(), self.recv_window_ms.to_string()));
            params.push(("timestamp".to_string(), self.now_ms().to_string()));
            let query = encode_params(&params);
            let signature = Self::sign(&secret, &query);
            let full_query = format!("{query}&signature={signature}");
            return self.execute(method, path, full_query, Some(api_key)).await;
        }
        let query = encode_params(&params);
        self.execute(method, path, query, None).await
    }

    async fn execute(
        &self,
        method: Method,
        path: &str,
        query: String,
        api_key: Option<String>,
    ) -> Result<Value, BinanceError> {
        // Retry policy: GET only.
        let mut retry_done = false;
        loop {
            let result = self
                .execute_once(method, path, &query, api_key.as_deref())
                .await;
            match result {
                Ok(v) => return Ok(v),
                Err(err) => {
                    if !method.is_get() || retry_done {
                        // POST/DELETE: on -1021 resync time but return original error.
                        if !method.is_get() {
                            if let BinanceError::Api { code: -1021, .. } = &err {
                                let _ = self.sync_time().await;
                            }
                        }
                        return Err(err);
                    }
                    match &err {
                        BinanceError::Api { code: -1021, .. } => {
                            retry_done = true;
                            if self.sync_time().await.is_err() {
                                return Err(err);
                            }
                            continue;
                        }
                        BinanceError::Network { .. } => {
                            retry_done = true;
                            tokio::time::sleep(Duration::from_millis(500)).await;
                            continue;
                        }
                        _ => return Err(err),
                    }
                }
            }
        }
    }

    async fn execute_once(
        &self,
        method: Method,
        path: &str,
        query: &str,
        api_key: Option<&str>,
    ) -> Result<Value, BinanceError> {
        let url = if query.is_empty() {
            format!("{}{}", self.base_url, path)
        } else {
            format!("{}{}?{}", self.base_url, path, query)
        };
        let mut builder = match method {
            Method::Get => self.http.get(&url),
            Method::Post => self.http.post(&url),
            Method::Delete => self.http.delete(&url),
        };
        if let Some(key) = api_key {
            builder = builder.header("X-MBX-APIKEY", key);
        }
        let response = match builder.send().await {
            Ok(r) => r,
            Err(e) => {
                let sent = !e.is_connect();
                return Err(BinanceError::Network {
                    message: e.to_string(),
                    sent,
                });
            }
        };
        let status = response.status().as_u16();
        let retry_after_secs = response
            .headers()
            .get("retry-after")
            .and_then(|v| v.to_str().ok())
            .and_then(|s| s.trim().parse::<u64>().ok());
        let body = match response.text().await {
            Ok(b) => b,
            Err(e) => {
                return Err(BinanceError::Network {
                    message: e.to_string(),
                    sent: true,
                });
            }
        };
        if (200..300).contains(&status) {
            return serde_json::from_str::<Value>(&body)
                .map_err(|e| BinanceError::Decode(e.to_string()));
        }
        if status == 429 || status == 418 {
            return Err(BinanceError::RateLimited {
                status,
                retry_after_secs,
            });
        }
        // 4xx / 5xx: prefer {"code":..,"msg":..}
        if let Ok(v) = serde_json::from_str::<Value>(&body) {
            if let Some(obj) = v.as_object() {
                if let (Some(code), Some(msg)) = (obj.get("code"), obj.get("msg")) {
                    if let Some(code) = code.as_i64() {
                        let msg = msg.as_str().unwrap_or("").to_string();
                        return Err(BinanceError::Api { status, code, msg });
                    }
                }
            }
        }
        let truncated: String = body.chars().take(300).collect();
        Err(BinanceError::Http {
            status,
            body: truncated,
        })
    }

    // -- time --------------------------------------------------------------

    pub async fn sync_time(&self) -> Result<i64, BinanceError> {
        let before = chrono_like_now_ms();
        // Single attempt: the retry path itself calls sync_time.
        let value = self
            .execute_once(Method::Get, "/fapi/v1/time", "", None)
            .await?;
        let after = chrono_like_now_ms();
        let server_time = value
            .get("serverTime")
            .and_then(|v| v.as_i64())
            .ok_or_else(|| BinanceError::Decode("missing serverTime".to_string()))?;
        let local_mid = (before + after) / 2;
        let offset = server_time - local_mid;
        self.time_offset_ms.store(offset, Ordering::Relaxed);
        Ok(offset)
    }

    // -- public endpoints --------------------------------------------------

    pub async fn exchange_info(&self) -> Result<Value, BinanceError> {
        self.send(Method::Get, "/fapi/v1/exchangeInfo", Vec::new(), false)
            .await
    }

    pub async fn klines(
        &self,
        symbol: &str,
        interval: &str,
        limit: u32,
    ) -> Result<Vec<Candle>, BinanceError> {
        let params = vec![
            ("symbol".to_string(), symbol.to_string()),
            ("interval".to_string(), interval.to_string()),
            ("limit".to_string(), limit.to_string()),
        ];
        let value = self
            .send(Method::Get, "/fapi/v1/klines", params, false)
            .await?;
        let rows = value
            .as_array()
            .ok_or_else(|| BinanceError::Decode("klines: not an array".to_string()))?;
        let mut out = Vec::with_capacity(rows.len());
        for row in rows {
            let arr = row
                .as_array()
                .ok_or_else(|| BinanceError::Decode("klines: row not array".to_string()))?;
            if arr.len() < 6 {
                return Err(BinanceError::Decode("klines: row too short".to_string()));
            }
            let open_time = json_to_f64(&arr[0])?;
            let open = json_to_f64(&arr[1])?;
            let high = json_to_f64(&arr[2])?;
            let low = json_to_f64(&arr[3])?;
            let close = json_to_f64(&arr[4])?;
            let volume = json_to_f64(&arr[5])?;
            let candle: Candle = [open_time, open, high, low, close, volume];
            out.push(candle);
        }
        Ok(out)
    }

    pub async fn ticker_24h(&self) -> Result<Value, BinanceError> {
        self.send(Method::Get, "/fapi/v1/ticker/24hr", Vec::new(), false)
            .await
    }

    pub async fn premium_index(&self, symbol: &str) -> Result<Value, BinanceError> {
        let params = vec![("symbol".to_string(), symbol.to_string())];
        self.send(Method::Get, "/fapi/v1/premiumIndex", params, false)
            .await
    }

    // -- signed endpoints --------------------------------------------------

    pub async fn account(&self) -> Result<Value, BinanceError> {
        self.send(Method::Get, "/fapi/v3/account", Vec::new(), true)
            .await
    }

    pub async fn balance(&self) -> Result<Value, BinanceError> {
        self.send(Method::Get, "/fapi/v3/balance", Vec::new(), true)
            .await
    }

    pub async fn position_risk(&self, symbol: Option<&str>) -> Result<Value, BinanceError> {
        let mut params = Vec::new();
        if let Some(s) = symbol {
            params.push(("symbol".to_string(), s.to_string()));
        }
        self.send(Method::Get, "/fapi/v3/positionRisk", params, true)
            .await
    }

    pub async fn position_side_dual(&self) -> Result<Value, BinanceError> {
        self.send(Method::Get, "/fapi/v1/positionSide/dual", Vec::new(), true)
            .await
    }

    pub async fn set_leverage(&self, symbol: &str, leverage: u32) -> Result<Value, BinanceError> {
        let params = vec![
            ("symbol".to_string(), symbol.to_string()),
            ("leverage".to_string(), leverage.to_string()),
        ];
        self.send(Method::Post, "/fapi/v1/leverage", params, true)
            .await
    }

    pub async fn set_margin_type(
        &self,
        symbol: &str,
        margin_type: &str,
    ) -> Result<Value, BinanceError> {
        let params = vec![
            ("symbol".to_string(), symbol.to_string()),
            ("marginType".to_string(), margin_type.to_string()),
        ];
        match self
            .send(Method::Post, "/fapi/v1/marginType", params, true)
            .await
        {
            Ok(v) => Ok(v),
            Err(BinanceError::Api { code: -4046, .. }) => {
                Ok(json!({"code": -4046, "msg": "No need to change margin type."}))
            }
            Err(e) => Err(e),
        }
    }

    pub async fn commission_rate(&self, symbol: &str) -> Result<Value, BinanceError> {
        let params = vec![("symbol".to_string(), symbol.to_string())];
        self.send(Method::Get, "/fapi/v1/commissionRate", params, true)
            .await
    }

    pub async fn new_order(&self, order: &NewOrder) -> Result<Value, BinanceError> {
        let mut params = Vec::new();
        params.push(("symbol".to_string(), order.symbol.clone()));
        params.push(("side".to_string(), order.side.clone()));
        params.push(("type".to_string(), order.order_type.clone()));
        if let Some(q) = &order.quantity {
            params.push(("quantity".to_string(), q.clone()));
        }
        if let Some(p) = &order.price {
            params.push(("price".to_string(), p.clone()));
        }
        if let Some(tif) = &order.time_in_force {
            params.push(("timeInForce".to_string(), tif.clone()));
        }
        if order.reduce_only {
            params.push(("reduceOnly".to_string(), "true".to_string()));
        }
        if let Some(ps) = &order.position_side {
            params.push(("positionSide".to_string(), ps.clone()));
        }
        params.push((
            "newClientOrderId".to_string(),
            order.new_client_order_id.clone(),
        ));
        if let Some(rt) = &order.new_order_resp_type {
            params.push(("newOrderRespType".to_string(), rt.clone()));
        }
        self.send(Method::Post, "/fapi/v1/order", params, true)
            .await
    }

    pub async fn query_order(
        &self,
        symbol: &str,
        orig_client_order_id: &str,
    ) -> Result<Value, BinanceError> {
        let params = vec![
            ("symbol".to_string(), symbol.to_string()),
            (
                "origClientOrderId".to_string(),
                orig_client_order_id.to_string(),
            ),
        ];
        self.send(Method::Get, "/fapi/v1/order", params, true).await
    }

    pub async fn cancel_order(
        &self,
        symbol: &str,
        orig_client_order_id: &str,
    ) -> Result<Value, BinanceError> {
        let params = vec![
            ("symbol".to_string(), symbol.to_string()),
            (
                "origClientOrderId".to_string(),
                orig_client_order_id.to_string(),
            ),
        ];
        self.send(Method::Delete, "/fapi/v1/order", params, true)
            .await
    }

    pub async fn open_orders(&self, symbol: &str) -> Result<Value, BinanceError> {
        let params = vec![("symbol".to_string(), symbol.to_string())];
        self.send(Method::Get, "/fapi/v1/openOrders", params, true)
            .await
    }

    pub async fn user_trades(
        &self,
        symbol: &str,
        start_time_ms: Option<i64>,
        limit: u32,
    ) -> Result<Value, BinanceError> {
        let mut params = vec![("symbol".to_string(), symbol.to_string())];
        if let Some(st) = start_time_ms {
            params.push(("startTime".to_string(), st.to_string()));
        }
        params.push(("limit".to_string(), limit.to_string()));
        self.send(Method::Get, "/fapi/v1/userTrades", params, true)
            .await
    }

    pub async fn income(
        &self,
        symbol: Option<&str>,
        income_type: Option<&str>,
        start_time_ms: Option<i64>,
        limit: u32,
    ) -> Result<Value, BinanceError> {
        let mut params = Vec::new();
        if let Some(s) = symbol {
            params.push(("symbol".to_string(), s.to_string()));
        }
        if let Some(t) = income_type {
            params.push(("incomeType".to_string(), t.to_string()));
        }
        if let Some(st) = start_time_ms {
            params.push(("startTime".to_string(), st.to_string()));
        }
        params.push(("limit".to_string(), limit.to_string()));
        self.send(Method::Get, "/fapi/v1/income", params, true)
            .await
    }

    pub async fn new_algo_order(&self, order: &NewAlgoOrder) -> Result<Value, BinanceError> {
        // Validation mirrors the Python place_conditional exactly.
        let order_type_value = order.order_type.to_uppercase();
        if !CONDITIONAL_TYPES.contains(&order_type_value.as_str()) {
            return Err(BinanceError::Invalid(format!(
                "unsupported Binance conditional order type: {}",
                order.order_type
            )));
        }
        if order.close_position
            && !matches!(
                order_type_value.as_str(),
                "STOP_MARKET" | "TAKE_PROFIT_MARKET"
            )
        {
            return Err(BinanceError::Invalid(
                "closePosition=true is only valid for STOP_MARKET or TAKE_PROFIT_MARKET"
                    .to_string(),
            ));
        }
        if order.close_position && order.price.is_some() {
            return Err(BinanceError::Invalid(
                "closePosition=true market triggers must not include price".to_string(),
            ));
        }
        if !order.close_position && order.quantity.is_none() {
            return Err(BinanceError::Invalid(
                "quantity is required when closePosition is false".to_string(),
            ));
        }

        let client_algo_id: String = order.client_algo_id.chars().take(36).collect();
        let mut params: Vec<(String, String)> = vec![
            ("algoType".to_string(), "CONDITIONAL".to_string()),
            ("symbol".to_string(), order.symbol.clone()),
            ("side".to_string(), order.side.to_uppercase()),
            ("type".to_string(), order_type_value.clone()),
            ("triggerPrice".to_string(), order.trigger_price.clone()),
            ("clientAlgoId".to_string(), client_algo_id),
            ("workingType".to_string(), order.working_type.to_uppercase()),
            ("priceProtect".to_string(), bool_text(order.price_protect)),
        ];
        if order.close_position {
            params.push(("closePosition".to_string(), "true".to_string()));
        } else {
            if let Some(q) = &order.quantity {
                params.push(("quantity".to_string(), q.clone()));
            }
            params.push(("reduceOnly".to_string(), bool_text(order.reduce_only)));
        }
        if let Some(ps) = &order.position_side {
            if !ps.is_empty() {
                params.push(("positionSide".to_string(), ps.to_uppercase()));
            }
        }
        if let Some(p) = &order.price {
            params.push(("price".to_string(), p.clone()));
        }
        self.send(Method::Post, "/fapi/v1/algoOrder", params, true)
            .await
    }

    pub async fn query_algo_order(&self, client_algo_id: &str) -> Result<Value, BinanceError> {
        let id: String = client_algo_id.chars().take(36).collect();
        let params = vec![("clientAlgoId".to_string(), id)];
        self.send(Method::Get, "/fapi/v1/algoOrder", params, true)
            .await
    }

    pub async fn open_algo_orders(&self, symbol: Option<&str>) -> Result<Value, BinanceError> {
        let mut params = Vec::new();
        if let Some(s) = symbol {
            params.push(("symbol".to_string(), s.to_string()));
        }
        self.send(Method::Get, "/fapi/v1/openAlgoOrders", params, true)
            .await
    }

    pub async fn cancel_algo_order(&self, client_algo_id: &str) -> Result<Value, BinanceError> {
        let id: String = client_algo_id.chars().take(36).collect();
        let params = vec![("clientAlgoId".to_string(), id)];
        self.send(Method::Delete, "/fapi/v1/algoOrder", params, true)
            .await
    }
}

fn json_to_f64(v: &Value) -> Result<f64, BinanceError> {
    if let Some(n) = v.as_f64() {
        return Ok(n);
    }
    if let Some(s) = v.as_str() {
        return s
            .parse::<f64>()
            .map_err(|e| BinanceError::Decode(e.to_string()));
    }
    Err(BinanceError::Decode("expected number".to_string()))
}

fn chrono_like_now_ms() -> i64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(d) => d.as_millis() as i64,
        Err(_) => 0,
    }
}
