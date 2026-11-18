use std::collections::VecDeque;
use std::future::Future;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use chrono::DateTime;
use hmac::{Hmac, Mac};
use reqwest::Method;
use serde_json::{Map, Value};
use sha2::Sha256;

use crate::core::candles::Candle;
use crate::exchange::binance::{urlencode, BinanceClient, Credentials, Network};

#[derive(Debug, Clone, PartialEq)]
pub struct OptionsApiError {
    pub message: String,
    pub code: Option<i64>,
    pub status: Option<u16>,
    pub uncertain: bool,
    pub rate_limited: bool,
    pub retry_after: f64,
}

impl OptionsApiError {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            code: None,
            status: None,
            uncertain: false,
            rate_limited: false,
            retry_after: 0.0,
        }
    }

    pub fn uncertain(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            code: None,
            status: None,
            uncertain: true,
            rate_limited: false,
            retry_after: 0.0,
        }
    }
}

impl std::fmt::Display for OptionsApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for OptionsApiError {}

#[derive(Debug, Clone, PartialEq)]
pub enum OrderRef {
    Id(Value),
    Client(String),
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct OptionOrder {
    pub symbol: String,
    pub side: String,
    pub order_type: String,
    pub quantity: String,
    pub price: Option<String>,
    pub time_in_force: Option<String>,
    pub post_only: bool,
    pub reduce_only: bool,
    pub client_order_id: Option<String>,
}

pub type ApiResult = Result<Value, OptionsApiError>;

/// Everything the options service needs. Implemented by `BinanceOptionsClient` and by test fakes.
pub trait OptionsApi: Send + Sync {
    fn authenticated(&self) -> bool;
    fn sync_time(&self) -> impl Future<Output = ApiResult> + Send;
    fn ping(&self) -> impl Future<Output = ApiResult> + Send;
    fn exchange_info(&self) -> impl Future<Output = ApiResult> + Send;
    fn mark_price(&self, symbol: Option<&str>) -> impl Future<Output = ApiResult> + Send;
    fn ticker(&self, symbol: Option<&str>) -> impl Future<Output = ApiResult> + Send;
    fn depth(&self, symbol: &str, limit: u32) -> impl Future<Output = ApiResult> + Send;
    fn recent_trades(&self, symbol: &str, limit: u32) -> impl Future<Output = ApiResult> + Send;
    fn margin_account(&self) -> impl Future<Output = ApiResult> + Send;
    fn positions(&self, symbol: Option<&str>) -> impl Future<Output = ApiResult> + Send;
    fn open_orders(&self, symbol: Option<&str>) -> impl Future<Output = ApiResult> + Send;
    fn query_order(&self, symbol: &str, order: &OrderRef)
        -> impl Future<Output = ApiResult> + Send;
    fn new_order(&self, order: &OptionOrder) -> impl Future<Output = ApiResult> + Send;
    fn cancel_order(
        &self,
        symbol: &str,
        order: &OrderRef,
    ) -> impl Future<Output = ApiResult> + Send;
    fn user_trades(
        &self,
        symbol: &str,
        start_time: Option<i64>,
        limit: u32,
    ) -> impl Future<Output = ApiResult> + Send;
    fn exercise_records(
        &self,
        symbol: Option<&str>,
        start_time: Option<i64>,
        limit: u32,
    ) -> impl Future<Output = ApiResult> + Send;
    /// Public USD-M futures klines for the underlying (e.g. "BTCUSDT"), oldest first,
    /// including the still-forming candle (the service drops it).
    fn underlying_klines(
        &self,
        symbol: &str,
        interval: &str,
        limit: u32,
    ) -> impl Future<Output = Result<Vec<Candle>, OptionsApiError>> + Send;
}

pub type CredentialsGetter = Arc<dyn Fn() -> Option<Credentials> + Send + Sync>;

fn now_ms() -> i64 {
    match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(d) => d.as_millis() as i64,
        Err(_) => 0,
    }
}

fn sign_hex(secret: &str, query: &str) -> String {
    let mut mac = match Hmac::<Sha256>::new_from_slice(secret.as_bytes()) {
        Ok(m) => m,
        Err(_) => return String::new(),
    };
    mac.update(query.as_bytes());
    hex::encode(mac.finalize().into_bytes())
}

/// Build the signed query string: `k=v` pairs joined by '&', both sides urlencoded,
/// then `&signature=<hex>`.
pub fn build_query(params: &[(String, String)], secret: &str) -> String {
    let mut parts: Vec<String> = Vec::with_capacity(params.len());
    for (k, v) in params.iter() {
        parts.push(format!("{}={}", urlencode(k), urlencode(v)));
    }
    let unsigned = parts.join("&");
    let signature = sign_hex(secret, &unsigned);
    if unsigned.is_empty() {
        format!("signature={}", signature)
    } else {
        format!("{}&signature={}", unsigned, signature)
    }
}

/// Parse the Retry-After header value into seconds (>= 0).
fn parse_retry_after(value: &str) -> f64 {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return 0.0;
    }
    if let Ok(secs) = trimmed.parse::<f64>() {
        if secs.is_nan() {
            return 0.0;
        }
        return if secs > 0.0 { secs } else { 0.0 };
    }
    if let Ok(dt) = DateTime::parse_from_rfc2822(trimmed) {
        let target = dt.timestamp() as f64;
        let now = now_ms() as f64 / 1000.0;
        let diff = target - now;
        return if diff > 0.0 { diff } else { 0.0 };
    }
    0.0
}

fn classify_http_error(
    method_is_write: bool,
    status: u16,
    body: &[u8],
    retry_after_header: Option<&str>,
) -> OptionsApiError {
    let parsed: Value = if body.is_empty() {
        Value::Object(Map::new())
    } else {
        serde_json::from_slice(body).unwrap_or_else(|_| Value::Object(Map::new()))
    };
    let (msg_str, code) = match &parsed {
        Value::Object(m) => {
            let msg = m
                .get("msg")
                .and_then(|v| v.as_str().map(|s| s.to_string()))
                .unwrap_or_default();
            let code = m.get("code").and_then(|v| v.as_i64());
            (msg, code)
        }
        _ => (String::new(), None),
    };
    let error_text = msg_str.to_lowercase();
    let rate_limited = status == 418
        || status == 429
        || code == Some(-1003)
        || error_text.contains("too many request");
    let mut retry_after = retry_after_header.map(parse_retry_after).unwrap_or(0.0);
    if rate_limited && (retry_after.is_nan() || retry_after <= 0.0) {
        retry_after = 60.0;
    }
    let uncertain =
        method_is_write && (status >= 500 || status == 408 || status == 409 || status == 425);
    let message = if msg_str.is_empty() {
        format!("OPTIONS_API_HTTP_{}", status)
    } else {
        msg_str
    };
    OptionsApiError {
        message,
        code,
        status: Some(status),
        uncertain,
        rate_limited,
        retry_after,
    }
}

fn order_id_param(order: &OrderRef) -> (String, String) {
    match order {
        OrderRef::Id(v) => {
            let text = match v {
                Value::Number(n) => {
                    if let Some(i) = n.as_i64() {
                        format!("{}", i)
                    } else if let Some(u) = n.as_u64() {
                        format!("{}", u)
                    } else if let Some(f) = n.as_f64() {
                        format!("{}", f)
                    } else {
                        n.to_string()
                    }
                }
                Value::String(s) => s.clone(),
                other => other.to_string(),
            };
            ("orderId".to_string(), text)
        }
        OrderRef::Client(c) => ("clientOrderId".to_string(), c.clone()),
    }
}

struct RateState {
    times: VecDeque<Instant>,
    cooldown_until: Option<Instant>,
}

pub struct BinanceOptionsClient {
    creds: CredentialsGetter,
    http: reqwest::Client,
    base_url: String,
    server_offset_ms: std::sync::atomic::AtomicI64,
    request_limit_per_minute: usize,
    rate: tokio::sync::Mutex<RateState>,
    futures: BinanceClient,
}

impl BinanceOptionsClient {
    pub const BASE_URL: &'static str = "https://eapi.binance.com";
    pub const TIMESTAMP_ERROR_CODE: i64 = -1021;
    pub const RECV_WINDOW_MS: i64 = 10000;

    /// `timeout_secs` clamped to >= 3; `request_limit_per_minute` clamped to [60, 300].
    pub fn new(creds: CredentialsGetter, timeout_secs: u64) -> Self {
        Self::with_base_url(Self::BASE_URL, creds, timeout_secs, 300)
    }

    pub fn with_base_url(
        base_url: &str,
        creds: CredentialsGetter,
        timeout_secs: u64,
        request_limit_per_minute: u32,
    ) -> Self {
        let timeout = timeout_secs.max(3);
        let http = reqwest::Client::builder()
            .timeout(Duration::from_secs(timeout))
            .connect_timeout(Duration::from_secs(5))
            .build()
            .unwrap_or_else(|_| reqwest::Client::new());
        let limit = request_limit_per_minute.clamp(60, 300) as usize;
        let base = if base_url.is_empty() {
            Self::BASE_URL.to_string()
        } else {
            base_url.trim_end_matches('/').to_string()
        };
        BinanceOptionsClient {
            creds,
            http,
            base_url: base,
            server_offset_ms: std::sync::atomic::AtomicI64::new(0),
            request_limit_per_minute: limit,
            rate: tokio::sync::Mutex::new(RateState {
                times: VecDeque::new(),
                cooldown_until: None,
            }),
            futures: BinanceClient::new(Network::Mainnet, None),
        }
    }

    pub fn server_offset_ms(&self) -> i64 {
        self.server_offset_ms
            .load(std::sync::atomic::Ordering::Relaxed)
    }

    fn current_creds(&self) -> Option<Credentials> {
        (self.creds)()
    }

    fn normalised_creds(&self) -> Option<(String, String)> {
        match (self.creds)() {
            Some(c) => {
                let k = c.api_key.trim().to_string();
                let s = c.secret.trim().to_string();
                if !k.is_empty() && !s.is_empty() {
                    Some((k, s))
                } else {
                    None
                }
            }
            None => None,
        }
    }

    async fn acquire_request_slot(&self) {
        loop {
            let wait_for = {
                let mut guard = self.rate.lock().await;
                let now = Instant::now();
                while let Some(front) = guard.times.front() {
                    if now.duration_since(*front) >= Duration::from_secs(60) {
                        guard.times.pop_front();
                    } else {
                        break;
                    }
                }
                let mut wait_for = match guard.cooldown_until {
                    Some(until) if until > now => until.duration_since(now).as_secs_f64(),
                    _ => 0.0,
                };
                if guard.times.len() >= self.request_limit_per_minute {
                    if let Some(front) = guard.times.front() {
                        let elapsed = now.duration_since(*front).as_secs_f64();
                        let v = 60.0 - elapsed + 0.01;
                        if v > wait_for {
                            wait_for = v;
                        }
                    }
                }
                if wait_for <= 0.0 {
                    guard.times.push_back(now);
                    return;
                }
                wait_for
            };
            let sleep_for = if wait_for < 1.0 { wait_for } else { 1.0 };
            let dur = if sleep_for > 0.0 {
                Duration::from_secs_f64(sleep_for)
            } else {
                Duration::from_millis(1)
            };
            tokio::time::sleep(dur).await;
        }
    }

    async fn apply_rate_backoff(&self, seconds: f64) {
        let duration = if seconds.is_finite() && seconds > 1.0 {
            seconds
        } else {
            1.0
        };
        let mut guard = self.rate.lock().await;
        let now = Instant::now();
        let target = now + Duration::from_secs_f64(duration);
        let new_until = match guard.cooldown_until {
            Some(existing) => {
                if existing > target {
                    existing
                } else {
                    target
                }
            }
            None => target,
        };
        guard.cooldown_until = Some(new_until);
    }

    fn decode_body(body: &[u8]) -> Result<Value, OptionsApiError> {
        if body.is_empty() {
            return Ok(Value::Object(Map::new()));
        }
        match serde_json::from_slice::<Value>(body) {
            Ok(v) => Ok(v),
            Err(_) => Err(OptionsApiError::new("OPTIONS_API_INVALID_JSON")),
        }
    }

    async fn request(
        &self,
        method: &str,
        path: &str,
        params: Vec<(String, String)>,
        signed: bool,
    ) -> ApiResult {
        let first = self
            .request_once(method, path, params.clone(), signed)
            .await;
        match first {
            Ok(v) => Ok(v),
            Err(err) => {
                if !signed || err.code != Some(Self::TIMESTAMP_ERROR_CODE) {
                    return Err(err);
                }
                // Resync the clock and retry once; -1021 means the request was
                // rejected before processing, so this cannot duplicate an order.
                let _ = self.sync_time().await;
                self.request_once(method, path, params, signed).await
            }
        }
    }

    async fn request_once(
        &self,
        method: &str,
        path: &str,
        params: Vec<(String, String)>,
        signed: bool,
    ) -> ApiResult {
        let method_upper = if method.is_empty() {
            "GET".to_string()
        } else {
            method.to_uppercase()
        };
        self.acquire_request_slot().await;

        let mut query: Vec<(String, String)> = params;
        let api_key: Option<String> = match self.current_creds() {
            Some(c) => {
                let k = c.api_key.trim().to_string();
                if k.is_empty() {
                    None
                } else {
                    Some(k)
                }
            }
            None => None,
        };

        if signed {
            let (key, secret) = match self.normalised_creds() {
                Some(kv) => kv,
                None => {
                    return Err(OptionsApiError::new("OPTIONS_API_CREDENTIALS_MISSING"));
                }
            };
            let _ = key;
            let has_recv = query.iter().any(|(k, _)| k == "recvWindow");
            if !has_recv {
                query.push(("recvWindow".to_string(), Self::RECV_WINDOW_MS.to_string()));
            }
            let ts = now_ms() + self.server_offset_ms();
            query.retain(|(k, _)| k != "timestamp");
            query.push(("timestamp".to_string(), ts.to_string()));
            let encoded = build_query(&query, &secret);
            // Replace signature-adding by using the built query directly.
            let url = format!("{}{}?{}", self.base_url, path, encoded);
            return self
                .send_request(&method_upper, &url, api_key.as_deref())
                .await;
        }

        let mut parts: Vec<String> = Vec::with_capacity(query.len());
        for (k, v) in query.iter() {
            parts.push(format!("{}={}", urlencode(k), urlencode(v)));
        }
        let encoded = parts.join("&");
        let url = if encoded.is_empty() {
            format!("{}{}", self.base_url, path)
        } else {
            format!("{}{}?{}", self.base_url, path, encoded)
        };
        self.send_request(&method_upper, &url, api_key.as_deref())
            .await
    }

    async fn send_request(
        &self,
        method_upper: &str,
        url: &str,
        api_key: Option<&str>,
    ) -> ApiResult {
        let is_write = matches!(method_upper, "POST" | "PUT" | "DELETE");
        let req_method = match method_upper {
            "GET" => Method::GET,
            "POST" => Method::POST,
            "DELETE" => Method::DELETE,
            "PUT" => Method::PUT,
            _ => Method::GET,
        };
        let mut builder = self
            .http
            .request(req_method, url)
            .header("Accept", "application/json");
        if let Some(key) = api_key {
            builder = builder.header("X-MBX-APIKEY", key);
        }
        let response = match builder.send().await {
            Ok(r) => r,
            Err(e) => {
                let msg = format!("OPTIONS_API_NETWORK_ERROR: {}", e);
                return Err(if is_write {
                    OptionsApiError::uncertain(msg)
                } else {
                    OptionsApiError::new(msg)
                });
            }
        };
        let status = response.status().as_u16();
        let retry_after_header = response
            .headers()
            .get("Retry-After")
            .and_then(|v| v.to_str().ok())
            .map(|s| s.to_string());
        let body = match response.bytes().await {
            Ok(b) => b,
            Err(e) => {
                let msg = format!("OPTIONS_API_NETWORK_ERROR: {}", e);
                return Err(if is_write {
                    OptionsApiError::uncertain(msg)
                } else {
                    OptionsApiError::new(msg)
                });
            }
        };

        if (200..300).contains(&status) {
            return match Self::decode_body(&body) {
                Ok(v) => Ok(v),
                Err(_) => {
                    let msg = "OPTIONS_API_INVALID_JSON";
                    if is_write {
                        Err(OptionsApiError::uncertain(msg))
                    } else {
                        Err(OptionsApiError::new(msg))
                    }
                }
            };
        }

        let err = classify_http_error(is_write, status, &body, retry_after_header.as_deref());
        if err.rate_limited {
            self.apply_rate_backoff(err.retry_after).await;
        }
        Err(err)
    }
}

impl OptionsApi for BinanceOptionsClient {
    fn authenticated(&self) -> bool {
        self.normalised_creds().is_some()
    }

    async fn sync_time(&self) -> ApiResult {
        let started_ms = now_ms();
        let payload = self
            .request_once("GET", "/eapi/v1/time", Vec::new(), false)
            .await?;
        let finished_ms = now_ms();
        let server_time = payload
            .get("serverTime")
            .and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|f| f as i64)))
            .unwrap_or(0);
        if server_time > 0 {
            let mid = (started_ms + finished_ms) / 2;
            let offset = server_time - mid - 250;
            self.server_offset_ms
                .store(offset, std::sync::atomic::Ordering::Relaxed);
        }
        Ok(payload)
    }

    async fn ping(&self) -> ApiResult {
        self.request("GET", "/eapi/v1/ping", Vec::new(), false)
            .await
    }

    async fn exchange_info(&self) -> ApiResult {
        self.request("GET", "/eapi/v1/exchangeInfo", Vec::new(), false)
            .await
    }

    async fn mark_price(&self, symbol: Option<&str>) -> ApiResult {
        let params = match symbol {
            Some(s) if !s.is_empty() => vec![("symbol".to_string(), s.to_string())],
            _ => Vec::new(),
        };
        self.request("GET", "/eapi/v1/mark", params, false).await
    }

    async fn ticker(&self, symbol: Option<&str>) -> ApiResult {
        let params = match symbol {
            Some(s) if !s.is_empty() => vec![("symbol".to_string(), s.to_string())],
            _ => Vec::new(),
        };
        self.request("GET", "/eapi/v1/ticker", params, false).await
    }

    async fn depth(&self, symbol: &str, limit: u32) -> ApiResult {
        let params = vec![
            ("symbol".to_string(), symbol.to_string()),
            ("limit".to_string(), limit.to_string()),
        ];
        self.request("GET", "/eapi/v1/depth", params, false).await
    }

    async fn recent_trades(&self, symbol: &str, limit: u32) -> ApiResult {
        let clamped = limit.clamp(1, 100);
        let params = vec![
            ("symbol".to_string(), symbol.to_string()),
            ("limit".to_string(), clamped.to_string()),
        ];
        self.request("GET", "/eapi/v1/trades", params, false).await
    }

    async fn margin_account(&self) -> ApiResult {
        self.request("GET", "/eapi/v1/marginAccount", Vec::new(), true)
            .await
    }

    async fn positions(&self, symbol: Option<&str>) -> ApiResult {
        let params = match symbol {
            Some(s) if !s.is_empty() => vec![("symbol".to_string(), s.to_string())],
            _ => Vec::new(),
        };
        self.request("GET", "/eapi/v1/position", params, true).await
    }

    async fn open_orders(&self, symbol: Option<&str>) -> ApiResult {
        let params = match symbol {
            Some(s) if !s.is_empty() => vec![("symbol".to_string(), s.to_string())],
            _ => Vec::new(),
        };
        self.request("GET", "/eapi/v1/openOrders", params, true)
            .await
    }

    async fn query_order(&self, symbol: &str, order: &OrderRef) -> ApiResult {
        let (id_key, id_value) = order_id_param(order);
        let params = vec![
            ("symbol".to_string(), symbol.to_string()),
            (id_key, id_value),
        ];
        self.request("GET", "/eapi/v1/order", params, true).await
    }

    async fn new_order(&self, order: &OptionOrder) -> ApiResult {
        let mut params: Vec<(String, String)> = Vec::new();
        params.push(("symbol".to_string(), order.symbol.clone()));
        params.push(("side".to_string(), order.side.to_uppercase()));
        params.push(("type".to_string(), order.order_type.to_uppercase()));
        params.push(("quantity".to_string(), order.quantity.clone()));
        params.push((
            "reduceOnly".to_string(),
            if order.reduce_only { "true" } else { "false" }.to_string(),
        ));
        params.push(("newOrderRespType".to_string(), "RESULT".to_string()));
        params.push((
            "selfTradePreventionMode".to_string(),
            "EXPIRE_TAKER".to_string(),
        ));
        if let Some(ref p) = order.price {
            params.push(("price".to_string(), p.clone()));
        }
        if let Some(ref tif) = order.time_in_force {
            if !tif.is_empty() {
                params.push(("timeInForce".to_string(), tif.to_uppercase()));
            }
        }
        if order.post_only {
            params.push(("postOnly".to_string(), "true".to_string()));
        }
        if let Some(ref cid) = order.client_order_id {
            if !cid.is_empty() {
                params.push(("clientOrderId".to_string(), cid.clone()));
            }
        }
        self.request("POST", "/eapi/v1/order", params, true).await
    }

    async fn cancel_order(&self, symbol: &str, order: &OrderRef) -> ApiResult {
        let (id_key, id_value) = order_id_param(order);
        let params = vec![
            ("symbol".to_string(), symbol.to_string()),
            (id_key, id_value),
        ];
        self.request("DELETE", "/eapi/v1/order", params, true).await
    }

    async fn user_trades(&self, symbol: &str, start_time: Option<i64>, limit: u32) -> ApiResult {
        let clamped = limit.clamp(1, 1000);
        let mut params: Vec<(String, String)> = Vec::new();
        params.push(("symbol".to_string(), symbol.to_string()));
        params.push(("limit".to_string(), clamped.to_string()));
        if let Some(st) = start_time {
            params.push(("startTime".to_string(), st.to_string()));
        }
        self.request("GET", "/eapi/v1/userTrades", params, true)
            .await
    }

    async fn exercise_records(
        &self,
        symbol: Option<&str>,
        start_time: Option<i64>,
        limit: u32,
    ) -> ApiResult {
        let clamped = limit.clamp(1, 1000);
        let mut params: Vec<(String, String)> = Vec::new();
        params.push(("limit".to_string(), clamped.to_string()));
        if let Some(s) = symbol {
            if !s.is_empty() {
                params.push(("symbol".to_string(), s.to_string()));
            }
        }
        if let Some(st) = start_time {
            params.push(("startTime".to_string(), st.to_string()));
        }
        self.request("GET", "/eapi/v1/exerciseRecord", params, true)
            .await
    }

    async fn underlying_klines(
        &self,
        symbol: &str,
        interval: &str,
        limit: u32,
    ) -> Result<Vec<Candle>, OptionsApiError> {
        match self.futures.klines(symbol, interval, limit).await {
            Ok(v) => Ok(v),
            Err(e) => Err(OptionsApiError::new(format!(
                "UNDERLYING_KLINES_ERROR: {}",
                e
            ))),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn build_query_signs_with_hmac() {
        let params = vec![
            ("symbol".to_string(), "BTC-250919-10000-C".to_string()),
            ("recvWindow".to_string(), "10000".to_string()),
            ("timestamp".to_string(), "1700000000000".to_string()),
        ];
        let q = build_query(&params, "secret");
        assert!(q.starts_with(
            "symbol=BTC-250919-10000-C&recvWindow=10000&timestamp=1700000000000&signature="
        ));
        let sig = q.split("signature=").nth(1).unwrap_or("");
        assert_eq!(sig.len(), 64);
        assert!(sig.chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn urlencode_handles_symbol_dash() {
        assert_eq!(urlencode("BTC-250919-10000-C"), "BTC-250919-10000-C");
    }

    #[test]
    fn retry_after_seconds_parse() {
        assert!((parse_retry_after("30") - 30.0).abs() < 1e-9);
        assert!((parse_retry_after("0") - 0.0).abs() < 1e-9);
        assert!((parse_retry_after("-5") - 0.0).abs() < 1e-9);
        assert!((parse_retry_after("garbage") - 0.0).abs() < 1e-9);
    }

    #[test]
    fn retry_after_http_date_parse() {
        let future = chrono::Utc::now() + chrono::Duration::seconds(120);
        let text = future.to_rfc2822();
        let secs = parse_retry_after(&text);
        assert!(secs > 60.0 && secs <= 125.0, "got {}", secs);
    }

    #[test]
    fn classify_rate_limit_headers() {
        let e = classify_http_error(true, 429, b"{}", None);
        assert!(e.rate_limited);
        assert!((e.retry_after - 60.0).abs() < 1e-9);
        assert_eq!(e.status, Some(429));
        assert_eq!(e.message, "OPTIONS_API_HTTP_429");
        assert!(!e.uncertain);
    }

    #[test]
    fn classify_too_many_requests_message() {
        let body = br#"{"code":-1003,"msg":"Too Many Requests"}"#;
        let e = classify_http_error(false, 400, body, Some("30"));
        assert!(e.rate_limited);
        assert_eq!(e.code, Some(-1003));
        assert!((e.retry_after - 30.0).abs() < 1e-9);
    }

    #[test]
    fn classify_server_error_write_uncertain() {
        let e = classify_http_error(true, 500, b"", None);
        assert!(e.uncertain);
        assert!(!e.rate_limited);
        assert_eq!(e.message, "OPTIONS_API_HTTP_500");
    }

    #[test]
    fn classify_get_500_not_uncertain() {
        let e = classify_http_error(false, 500, b"", None);
        assert!(!e.uncertain);
    }

    #[test]
    fn classify_408_write_uncertain() {
        let e = classify_http_error(true, 408, b"", None);
        assert!(e.uncertain);
    }
}
