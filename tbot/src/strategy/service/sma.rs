//! SMA-cross strategy-specific methods (universe, signal, entry, re-entry,
//! TradFi gate, profit-lock entry-mode switch).

use std::collections::HashSet;
use std::str::FromStr;
use std::sync::Arc;

use rust_decimal::Decimal;
use serde_json::{json, Map, Value};

use crate::core::candles::timeframe_ms;
use crate::core::sma_cross::{evaluate_cross, profit_lock_target, CrossResult};
use crate::exchange::FuturesExchange;
use crate::strategy::coinselect;
use crate::strategy::market_session;
use crate::strategy::service::base::py_truthy;
use crate::strategy::service::{
    iso_ms, market_key, py_f, rules_from_value, symbol_pair, ServiceError, SmaCrossService,
    Variant, CCXT_SYMBOL, FORCED_ENTRY_MODE, IDLE, MARKET_ID, PRE_TRADE_VALIDATION,
    SIGNAL_DETECTED, TRADFI_MAINNET_ONLY_REASON, TRADFI_SCOPE, TRADFI_SESSION_CLOSED_REASON,
};
use crate::strategy::tradfi;

pub const PROFIT_LOCK_EXIT_REASON: &str = "PROFIT_LOCK";

const SCAN_CONCURRENCY: usize = 5;
const UNIVERSE_TTL_MS: i64 = 600_000;

const STABLE_BASES: &[&str] = &[
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "PYUSD", "USD1",
];

// Skip reasons recoverable within minutes: reverse/rotation retries them.
const TRANSIENT_ENTRY_BLOCKS: &[&str] = &[
    "OPEN_ORDERS_UNKNOWN",
    "OPEN_ORDERS_CANCEL_FAILED",
    "POSITION_STATE_UNCERTAIN",
    "ACCOUNT_POSITION_EXISTS",
    "BALANCE_UNAVAILABLE",
    "TRADING_RULES_UNAVAILABLE",
    "ACCOUNT_SETUP_UNVERIFIED",
    "POSITION_MODE_UNKNOWN",
];

// Skip reasons tied to a single coin: the scan tries the next candidate.
const SYMBOL_SPECIFIC_BLOCKS: &[&str] = &[
    "BELOW_EXCHANGE_MINIMUM",
    "ABOVE_MAX_QTY",
    "ACCOUNT_SETUP_MISMATCH",
    "TRADING_RULES_UNAVAILABLE",
    "INSUFFICIENT_MARGIN",
    "FOREIGN_OPEN_ORDERS",
];

fn starts_with_any(text: &str, prefixes: &[&str]) -> bool {
    prefixes.iter().any(|p| text.starts_with(p))
}

fn market_base(market_id: &str) -> &str {
    market_id.strip_suffix("USDT").unwrap_or(market_id)
}

fn value_fmt(value: &Value) -> String {
    format_g(value, py_f(Some(value), 0.0))
}

fn format_g(value: &Value, fallback: f64) -> String {
    // `:.8g`-ish formatting: trim trailing zeros, Python float repr for text.
    let _ = value;
    if fallback.is_finite() && fallback.fract() == 0.0 && fallback.abs() < 1e15 {
        return format!("{}", fallback as i64);
    }
    let s = format!("{:.8}", fallback);
    trim_zeros(s)
}

fn trim_zeros(s: String) -> String {
    if !s.contains('.') {
        return s;
    }
    let t = s.trim_end_matches('0').to_string();
    t.strip_suffix('.').map(|v| v.to_string()).unwrap_or(t)
}

/// `int_or_zero`: int(float(value)), 0 on conversion failure.
fn int_or_zero(value: Option<&Value>) -> i64 {
    match value {
        None | Some(Value::Null) => 0,
        Some(Value::Bool(b)) => *b as i64,
        Some(Value::Number(n)) => n.as_f64().map(|f| f as i64).unwrap_or(0),
        Some(Value::String(s)) => s.trim().parse::<f64>().map(|f| f as i64).unwrap_or(0),
        Some(_) => 0,
    }
}

/// Strips a value down to uppercase alphanumerics (Python `str(value)`).
fn alnum_upper(value: &Value) -> String {
    let text = match value {
        Value::Null => String::new(),
        Value::Bool(b) => {
            if *b {
                "True".to_string()
            } else {
                "False".to_string()
            }
        }
        Value::String(s) => s.clone(),
        Value::Number(n) => n.to_string(),
        other => other.to_string(),
    };
    text.to_uppercase()
        .chars()
        .filter(|c| c.is_alphanumeric())
        .collect()
}

fn value_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().map(|f| f != 0.0).unwrap_or(true),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Python `D(value)`: invalid input raises instead of becoming zero.
fn py_dec(value: Option<&Value>) -> Result<Decimal, ServiceError> {
    crate::core::rules::d(value, None)
        .map_err(|e| ServiceError::new("TradingRulesError", e.to_string()))
}

/// Python `D(str(float_value))`.
fn dec_from_f64(f: f64) -> Result<Decimal, ServiceError> {
    py_dec(Some(&Value::String(crate::store::ledger::py_float_repr(f))))
}

/// Python `list(await ...)`: universe tuple list used by rotation.
type UniverseItem = (String, String, Option<f64>);

impl<E: FuturesExchange + 'static> SmaCrossService<E> {
    // ------------------------------------------------------------------ universe
    pub async fn scan_universe(
        &self,
        network: &str,
        cfg: &Value,
    ) -> Result<Vec<(String, String, Option<f64>)>, ServiceError> {
        let scope = cfg.get("scan_scope").and_then(|v| v.as_str()).unwrap_or("");
        if scope == TRADFI_SCOPE {
            return self.tradfi_universe(network, cfg).await;
        }
        if scope == "single" {
            let text = cfg.get("single_symbol").cloned().unwrap_or(Value::Null);
            let (ccxt_symbol, market_id) = self.resolve_coin(network, &text).await?;
            return Ok(vec![(ccxt_symbol, market_id, None)]);
        }
        if scope != "alts" {
            return Ok(vec![(CCXT_SYMBOL.to_string(), MARKET_ID.to_string(), None)]);
        }
        self.volume_universe(network, cfg).await
    }

    pub async fn resolve_coin(
        &self,
        network: &str,
        text: &Value,
    ) -> Result<(String, String), ServiceError> {
        self.resolve_market(network, text, "crypto").await
    }

    pub async fn resolve_tradfi_coin(
        &self,
        network: &str,
        text: &Value,
    ) -> Result<(String, String), ServiceError> {
        self.resolve_market(network, text, "tradfi").await
    }

    async fn resolve_market(
        &self,
        network: &str,
        text: &Value,
        requirement: &str,
    ) -> Result<(String, String), ServiceError> {
        let raw_text = match text {
            Value::Null => String::new(),
            Value::String(s) => s.clone(),
            other => other.to_string(),
        };
        let mut raw: String = raw_text
            .to_uppercase()
            .replace(":USDT", "")
            .replace("PERP", "");
        raw = raw.chars().filter(|c| c.is_alphanumeric()).collect();
        let base = if raw.ends_with("USDT") && raw.len() > 4 {
            raw[..raw.len() - 4].to_string()
        } else {
            raw.clone()
        };
        if base.is_empty() {
            return Err(ServiceError::new("ValueError", "SYMBOL_EMPTY"));
        }
        if requirement == "tradfi" && !self.tradfi_network_allowed("tradfi", network) {
            return Err(ServiceError::new("ValueError", TRADFI_MAINNET_ONLY_REASON));
        }
        let exchange = self.exchange(network).await?;
        let markets = match exchange.markets() {
            Some(m) => m,
            None => exchange.load_markets().await?,
        };
        let mut by_id: std::collections::HashMap<String, (String, Map<String, Value>)> =
            std::collections::HashMap::new();
        for (symbol, market) in markets.iter() {
            let Some(market_obj) = market.as_object() else {
                continue;
            };
            let swap = market_obj.get("swap").and_then(|v| v.as_bool());
            let linear = market_obj.get("linear").and_then(|v| v.as_bool());
            let quote = market_obj.get("quote").and_then(|v| v.as_str());
            let settle = market_obj.get("settle").and_then(|v| v.as_str());
            if swap == Some(true)
                && linear == Some(true)
                && quote == Some("USDT")
                && settle == Some("USDT")
            {
                let id = market_obj
                    .get("id")
                    .map(|v| match v {
                        Value::Null => String::new(),
                        Value::String(s) => s.clone(),
                        other => other.to_string(),
                    })
                    .unwrap_or_default()
                    .to_uppercase();
                by_id.insert(id, (symbol.clone(), market_obj.clone()));
            }
        }
        for market_id in [format!("{base}USDT"), format!("1000{base}USDT")] {
            let Some((symbol, market)) = by_id.get(&market_id) else {
                continue;
            };
            let status = market
                .get("info")
                .and_then(|i| i.as_object())
                .and_then(|m| m.get("status"))
                .map(|v| match v {
                    Value::String(s) => s.clone(),
                    Value::Null => String::new(),
                    other => other.to_string(),
                })
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| "TRADING".to_string())
                .to_uppercase();
            if market.get("active").and_then(|v| v.as_bool()) == Some(false) || status != "TRADING"
            {
                return Err(ServiceError::new(
                    "ValueError",
                    format!("NOT_TRADING:{market_id}"),
                ));
            }
            let market_value = Value::Object(market.clone());
            if requirement == "tradfi" {
                if !tradfi::market_in_tradfi_scope(symbol, Some(&market_value)) {
                    return Err(ServiceError::new(
                        "ValueError",
                        format!("NOT_TRADFI:{market_id}"),
                    ));
                }
                return Ok((symbol.clone(), market_id.clone()));
            }
            let base_str = market
                .get("base")
                .map(|v| match v {
                    Value::Null => String::new(),
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                })
                .unwrap_or_default()
                .to_uppercase();
            if STABLE_BASES.contains(&base_str.as_str()) {
                return Err(ServiceError::new(
                    "ValueError",
                    format!("STABLECOIN:{market_id}"),
                ));
            }
            if coinselect::market_is_tradifi_perpetual(symbol, Some(&market_value)) {
                return Err(ServiceError::new(
                    "ValueError",
                    format!("TRADFI:{market_id}"),
                ));
            }
            return Ok((symbol.clone(), market_id.clone()));
        }
        Err(ServiceError::new(
            "ValueError",
            format!("SYMBOL_NOT_FOUND:{base}USDT"),
        ))
    }

    async fn volume_universe(
        &self,
        network: &str,
        cfg: &Value,
    ) -> Result<Vec<(String, String, Option<f64>)>, ServiceError> {
        let universe_size = cfg.get("alt_universe_size").cloned().unwrap_or(Value::Null);
        let cache_key = network.to_string();
        if let Some(cached) = self.universe_cache.lock().unwrap().get(&cache_key).cloned() {
            let ts = cached.get(0).and_then(|v| v.as_i64()).unwrap_or(0);
            let cached_size = cached.get(2).cloned().unwrap_or(Value::Null);
            if self.now_ms() - ts < UNIVERSE_TTL_MS && cached_size == universe_size {
                if let Some(list) = cached.get(1) {
                    return Ok(universe_from_value(list));
                }
            }
        }
        let exchange = self.exchange(network).await?;
        let ours_markets = match exchange.markets() {
            Some(m) => m,
            None => exchange.load_markets().await?,
        };
        let mut markets = ours_markets.clone();
        let rank_by_mainnet = network != "mainnet"
            && cfg
                .get("rank_testnet_by_mainnet_volume")
                .map(value_truthy)
                .unwrap_or(false);
        let mut ranking = exchange.clone();
        if rank_by_mainnet {
            ranking = self.ranking_exchange().await?;
            markets = match ranking.markets() {
                Some(m) => m,
                None => ranking.load_markets().await?,
            };
        }
        let tickers = ranking.fetch_tickers().await?;
        let mut rows: Vec<(String, String, f64)> = Vec::new();
        let exclude_btc = cfg.get("alt_exclude_btc").map(value_truthy).unwrap_or(true);
        for (symbol, ticker) in tickers.iter() {
            let Some(market) = markets.get(symbol).and_then(|v| v.as_object()) else {
                continue;
            };
            let Some(ticker_obj) = ticker.as_object() else {
                continue;
            };
            let active_ok = market.get("active").and_then(|v| v.as_bool()) != Some(false);
            let swap = market.get("swap").and_then(|v| v.as_bool());
            let linear = market.get("linear").and_then(|v| v.as_bool());
            let quote = market.get("quote").and_then(|v| v.as_str());
            let settle = market.get("settle").and_then(|v| v.as_str());
            if !(active_ok
                && swap == Some(true)
                && linear == Some(true)
                && quote == Some("USDT")
                && settle == Some("USDT"))
            {
                continue;
            }
            let Some(listed) = ours_markets.get(symbol).and_then(|v| v.as_object()) else {
                continue;
            };
            if listed.get("active").and_then(|v| v.as_bool()) == Some(false) {
                continue;
            }
            let status = listed
                .get("info")
                .and_then(|i| i.as_object())
                .and_then(|m| m.get("status"))
                .map(|v| match v {
                    Value::String(s) => s.clone(),
                    Value::Null => String::new(),
                    other => other.to_string(),
                })
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| "TRADING".to_string())
                .to_uppercase();
            if status != "TRADING" {
                continue;
            }
            let base_str = market
                .get("base")
                .map(|v| match v {
                    Value::Null => String::new(),
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                })
                .unwrap_or_default()
                .to_uppercase();
            if STABLE_BASES.contains(&base_str.as_str()) {
                continue;
            }
            let market_value = Value::Object(market.clone());
            if coinselect::market_is_tradifi_perpetual(symbol, Some(&market_value)) {
                continue;
            }
            let info = ticker_obj.get("info").and_then(|v| v.as_object());
            let qv = ticker_obj.get("quoteVolume");
            let volume_value = match qv {
                Some(v) if !v.is_null() => v.clone(),
                _ => info
                    .and_then(|m| m.get("quoteVolume"))
                    .cloned()
                    .unwrap_or(Value::Null),
            };
            let volume = py_f(Some(&volume_value), 0.0);
            let market_id = market
                .get("id")
                .map(|v| match v {
                    Value::Null => String::new(),
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                })
                .unwrap_or_default()
                .to_uppercase();
            let market_id = if market_id.is_empty() {
                symbol.split(':').next().unwrap_or("").replace('/', "")
            } else {
                market_id
            };
            if exclude_btc && market_id == MARKET_ID {
                continue;
            }
            if volume > 0.0 {
                rows.push((symbol.clone(), market_id, volume));
            }
        }
        rows.sort_by(|a, b| {
            b.2.partial_cmp(&a.2)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| a.1.cmp(&b.1))
        });
        let size = int_or_zero(cfg.get("alt_universe_size")) as usize;
        let universe: Vec<(String, String, Option<f64>)> = rows
            .into_iter()
            .take(size)
            .map(|(s, m, v)| (s, m, Some(v)))
            .collect();
        if universe.is_empty() {
            return Err(ServiceError::new("ValueError", "empty volume universe"));
        }
        let cached = json!([
            self.now_ms(),
            universe_to_value(&universe),
            cfg.get("alt_universe_size").cloned().unwrap_or(Value::Null),
        ]);
        self.universe_cache
            .lock()
            .unwrap()
            .insert(cache_key, cached);
        Ok(universe)
    }

    async fn ranking_exchange(&self) -> Result<Arc<E>, ServiceError> {
        let cached = self.ranking_exchange_cache.lock().unwrap().clone();
        if let Some(ex) = cached {
            return Ok(ex);
        }
        let factory = self.deps.exchange_factory.clone();
        let exchange = factory("mainnet", &json!({}))?;
        exchange.load_markets().await?;
        *self.ranking_exchange_cache.lock().unwrap() = Some(exchange.clone());
        Ok(exchange)
    }

    // ---------------------------------------------------------------- tradfi gates
    pub fn tradfi_network_allowed(&self, _variant_dispatch_marker: &str, network: &str) -> bool {
        // identical in TradFiScopeMixin
        network.trim().to_lowercase() == "mainnet"
    }

    pub fn us_session_status(&self) -> Value {
        // identical in TradFiScopeMixin
        let now = self.now_utc();
        match market_session::us_equity_regular_session_status(now) {
            v @ Value::Object(_) => v,
            _ => json!({"open": false, "reason": "session_unavailable: ValueError"}),
        }
    }

    fn now_utc(&self) -> Option<chrono::DateTime<chrono::Utc>> {
        use chrono::TimeZone;
        let secs = (self.deps.clock)();
        let ms = (secs * 1000.0) as i64;
        chrono::Utc.timestamp_millis_opt(ms).single()
    }

    pub fn tradifi_scope_state(&self, network: &str) -> Value {
        // identical in TradFiScopeMixin
        if !self.tradfi_network_allowed("tradfi", network) {
            return json!({
                "allowed": false,
                "reason": TRADFI_MAINNET_ONLY_REASON,
                "detail": "TradFi는 바이낸스 메인넷에서만 지원됩니다.",
            });
        }
        let session = self.us_session_status();
        let open = session
            .get("open")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        if !open {
            let reason = session
                .get("reason")
                .map(|v| match v {
                    Value::Null => "None".to_string(),
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                })
                .unwrap_or_else(|| "None".to_string());
            return json!({
                "allowed": false,
                "reason": TRADFI_SESSION_CLOSED_REASON,
                "detail": format!("미국 정규장 마감 ({reason})"),
                "session": session,
            });
        }
        json!({"allowed": true, "reason": "", "detail": "", "session": session})
    }

    pub fn tradfi_entry_block(&self, network: &str, cfg: &Value) -> String {
        // identical in TradFiScopeMixin
        if cfg.get("scan_scope").and_then(|v| v.as_str()) != Some(TRADFI_SCOPE) {
            return String::new();
        }
        let state = self.tradifi_scope_state(network);
        let allowed = state
            .get("allowed")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        if allowed {
            return String::new();
        }
        state
            .get("reason")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .unwrap_or(TRADFI_MAINNET_ONLY_REASON)
            .to_string()
    }

    fn tradfi_gates(&self, _state: &Map<String, Value>, network: &str) -> Map<String, Value> {
        let session = if self.tradfi_network_allowed("tradfi", network) {
            self.us_session_status()
        } else {
            json!({})
        };
        let scope_state = self.tradifi_scope_state(network);
        let mut out = Map::new();
        out.insert(
            "tradfi_gate".to_string(),
            Value::String(
                scope_state
                    .get("reason")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string(),
            ),
        );
        out.insert(
            "tradfi_gate_detail".to_string(),
            Value::String(
                scope_state
                    .get("detail")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string(),
            ),
        );
        out.insert("us_session".to_string(), session);
        out.insert(
            "scan_scope".to_string(),
            Value::String(TRADFI_SCOPE.to_string()),
        );
        out
    }

    pub(crate) async fn tradfi_universe(
        &self,
        network: &str,
        cfg: &Value,
    ) -> Result<Vec<(String, String, Option<f64>)>, ServiceError> {
        if !self.tradfi_network_allowed("tradfi", network) {
            return Err(ServiceError::new("ValueError", TRADFI_MAINNET_ONLY_REASON));
        }
        let cache_key = format!("tradfi:{network}");
        if let Some(cached) = self.universe_cache.lock().unwrap().get(&cache_key).cloned() {
            let ts = cached.get(0).and_then(|v| v.as_i64()).unwrap_or(0);
            if self.now_ms() - ts < UNIVERSE_TTL_MS {
                if let Some(list) = cached.get(1) {
                    return Ok(universe_from_value(list));
                }
            }
        }
        let exchange = self.exchange(network).await?;
        let markets = match exchange.markets() {
            Some(m) => m,
            None => exchange.load_markets().await?,
        };
        let tickers = exchange.fetch_tickers().await?;
        let use_mixin_volume = self.variant == Variant::BtcMa;
        let mut rows: Vec<(String, String, f64)> = Vec::new();
        for (symbol, market) in markets.iter() {
            let Some(market_obj) = market.as_object() else {
                continue;
            };
            if !tradfi::market_in_tradfi_scope(symbol, Some(market)) {
                continue;
            }
            if market_obj.get("active").and_then(|v| v.as_bool()) == Some(false) {
                continue;
            }
            let status = market_obj
                .get("info")
                .and_then(|i| i.as_object())
                .and_then(|m| m.get("status"))
                .map(|v| match v {
                    Value::String(s) => s.clone(),
                    Value::Null => String::new(),
                    other => other.to_string(),
                })
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| "TRADING".to_string())
                .to_uppercase();
            if status != "TRADING" {
                continue;
            }
            let ticker = tickers.get(symbol).cloned().unwrap_or_else(|| json!({}));
            let ticker_obj = ticker.as_object().cloned().unwrap_or_default();
            let info = ticker_obj.get("info").and_then(|v| v.as_object());
            let qv = ticker_obj.get("quoteVolume");
            let volume = if use_mixin_volume {
                // TradFiScopeMixin: quoteVolume else info.quoteVolume or 0.0
                let candidate = match qv {
                    Some(v) if !v.is_null() => Some(v.clone()),
                    _ => {
                        let info_v = info
                            .and_then(|m| m.get("quoteVolume"))
                            .cloned()
                            .unwrap_or(Value::Null);
                        if info_v.is_null() {
                            None
                        } else {
                            Some(info_v)
                        }
                    }
                };
                match candidate {
                    Some(v) => py_f(Some(&v), 0.0),
                    None => 0.0,
                }
            } else {
                py_f(qv, 0.0)
            };
            let market_id = market_obj
                .get("id")
                .map(|v| match v {
                    Value::Null => String::new(),
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                })
                .unwrap_or_default()
                .to_uppercase();
            let market_id = if market_id.is_empty() {
                symbol.split(':').next().unwrap_or("").replace('/', "")
            } else {
                market_id
            };
            rows.push((symbol.clone(), market_id, volume));
        }
        rows.sort_by(|a, b| {
            b.2.partial_cmp(&a.2)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| a.1.cmp(&b.1))
        });
        let size = int_or_zero(cfg.get("alt_universe_size")) as usize;
        let universe: Vec<(String, String, Option<f64>)> = rows
            .into_iter()
            .take(size)
            .map(|(s, m, v)| (s, m, Some(v)))
            .collect();
        if universe.is_empty() {
            return Err(ServiceError::new("ValueError", "empty tradfi universe"));
        }
        let cached = json!([
            self.now_ms(),
            universe_to_value(&universe),
            universe.len() as i64,
        ]);
        self.universe_cache
            .lock()
            .unwrap()
            .insert(cache_key, cached);
        Ok(universe)
    }

    // ----------------------------------------------------------------- signal
    pub async fn cross_signal(
        &self,
        network: &str,
        cfg: &Value,
        ccxt_symbol: &str,
        market_id: &str,
    ) -> Result<Value, ServiceError> {
        let exchange = self.exchange(network).await?;
        let timeframe = cfg
            .get("timeframe")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let limit = int_or_zero(cfg.get("candle_history")) as u32;
        let rows = exchange
            .fetch_ohlcv(ccxt_symbol, &timeframe, None, Some(limit))
            .await?;
        let cross_cfg: crate::core::sma_cross::CrossConfig = serde_json::from_value(cfg.clone())
            .map_err(|e| {
                ServiceError::new("ValueError", format!("invalid strategy config: {e}"))
            })?;
        // Python: evaluate_cross(rows, cfg, now, symbol=market_id, ccxt_symbol=ccxt_symbol)
        let result = evaluate_cross(
            &rows,
            &cross_cfg,
            self.now_ms(),
            market_id,
            Some(ccxt_symbol),
        );
        serde_json::to_value(result).map_err(|e| ServiceError::new("ValueError", e.to_string()))
    }

    pub fn latest_closed_bar(&self, cfg: &Value) -> i64 {
        let tf = cfg.get("timeframe").and_then(|v| v.as_str()).unwrap_or("");
        let span = timeframe_ms(tf).unwrap_or(0);
        if span <= 0 {
            return 0;
        }
        let now = self.now_ms();
        (now / span) * span - span
    }

    async fn evaluate_new_bar(
        &self,
        network: &str,
        cfg: &Value,
        mode: &str,
        state: &mut Map<String, Value>,
        trade: Option<&Map<String, Value>>,
    ) -> Result<(Option<Value>, String), ServiceError> {
        let trade_value = trade.map(|t| Value::Object(t.clone()));
        let (ccxt_symbol, market) = symbol_pair(trade_value.as_ref());
        let signal = match self.cross_signal(network, cfg, &ccxt_symbol, &market).await {
            Ok(s) => s,
            Err(err) => {
                let reason = format!("MARKET_DATA_UNAVAILABLE: {}: {}", err.class, err.message);
                state.insert("last_error".to_string(), Value::String(reason.clone()));
                self.save_state(network, mode, state)?;
                let ledger = self.ledger(network)?;
                ledger.lock().unwrap().event(
                    "SKIP",
                    Map::from_iter([
                        ("mode".to_string(), Value::String(mode.to_string())),
                        ("network".to_string(), Value::String(network.to_string())),
                        ("skip_reason".to_string(), Value::String(reason.clone())),
                    ]),
                );
                return Ok((None, reason));
            }
        };
        let bar = signal
            .get("bar_open_ms")
            .map(|v| int_or_zero(Some(v)))
            .unwrap_or(0);
        let last_eval = int_or_zero(state.get("last_evaluated_bar"));
        if bar != 0 && bar == last_eval {
            return Ok((None, "WAITING_NEXT_CLOSE".to_string()));
        }
        if bar != 0 {
            state.insert("last_evaluated_bar".to_string(), json!(bar));
        }
        state.insert("last_error".to_string(), Value::String(String::new()));
        self.update_reentry_blocks(state, std::slice::from_ref(&signal));
        let ledger = self.ledger(network)?;
        let mut fields = signal.as_object().cloned().unwrap_or_default();
        fields.insert("mode".to_string(), Value::String(mode.to_string()));
        fields.insert("network".to_string(), Value::String(network.to_string()));
        ledger.lock().unwrap().event("SIGNAL", fields);
        self.save_state(network, mode, state)?;
        let skip = signal
            .get("skip_reason")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        Ok((Some(signal), skip))
    }

    fn block_side_and_exit(block: Option<&Value>) -> (String, i64) {
        match block {
            None | Some(Value::Null) => (String::new(), 0),
            Some(Value::Object(m)) => {
                let side = m
                    .get("side")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let exit_ms = int_or_zero(m.get("exit_ms"));
                (side, exit_ms)
            }
            Some(Value::String(s)) => (s.clone(), 0),
            Some(other) => {
                let side = match other {
                    Value::Null => String::new(),
                    v => v.to_string(),
                };
                (side, 0)
            }
        }
    }

    fn bars_since_exit(&self, cfg: &Value, result: &Value, exit_ms: i64) -> i64 {
        let tf = cfg.get("timeframe").and_then(|v| v.as_str()).unwrap_or("");
        let span = timeframe_ms(tf).unwrap_or(0);
        let bar_open = result
            .get("bar_open_ms")
            .map(|v| int_or_zero(Some(v)))
            .unwrap_or(0);
        let bar_close = bar_open + span;
        if bar_close <= exit_ms {
            return 0;
        }
        (bar_close - exit_ms - 1).div_euclid(span) + 1
    }

    fn reentry_wait(
        &self,
        cfg: &Value,
        state: &Map<String, Value>,
        result: &Value,
    ) -> (bool, i64, i64) {
        let market_id = result.get("market_id").cloned().unwrap_or(Value::Null);
        let block = state
            .get("reentry_block")
            .and_then(|v| v.as_object())
            .and_then(|m| match &market_id {
                Value::String(s) => m.get(s),
                _ => None,
            });
        let (side, exit_ms) = Self::block_side_and_exit(block);
        let trend_side = result
            .get("trend_side")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if side.is_empty() || side != trend_side {
            return (false, 0, 0);
        }
        let needed = int_or_zero(cfg.get("reentry_wait_bars"));
        let waited = self.bars_since_exit(cfg, result, exit_ms);
        if needed == 0 {
            return (true, waited, 0);
        }
        (waited < needed, waited, needed)
    }

    fn update_reentry_blocks(&self, state: &mut Map<String, Value>, results: &[Value]) {
        let mut blocks = state
            .get("reentry_block")
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default();
        for result in results {
            let market_id = match result.get("market_id") {
                Some(Value::String(s)) => s.clone(),
                Some(Value::Null) | None => continue,
                Some(other) => other.to_string(),
            };
            let block = blocks.get(&market_id);
            let (blocked, _exit_ms) = Self::block_side_and_exit(block);
            if blocked.is_empty() {
                continue;
            }
            let side = result.get("side").and_then(|v| v.as_str()).unwrap_or("");
            let trend = result
                .get("trend_side")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let cross_against = !side.is_empty();
            let trend_flipped = !trend.is_empty() && trend != blocked;
            if cross_against || trend_flipped {
                blocks.remove(&market_id);
            }
        }
        state.insert("reentry_block".to_string(), Value::Object(blocks));
    }

    pub fn reentry_wait_lines(&self, cfg: &Value, state: &Map<String, Value>) -> Vec<String> {
        let mut lines = Vec::new();
        let latest = json!({"bar_open_ms": self.latest_closed_bar(cfg)});
        let mut blocks: Vec<(&String, &Value)> = state
            .get("reentry_block")
            .and_then(|v| v.as_object())
            .map(|m| m.iter().collect())
            .unwrap_or_default();
        blocks.sort_by(|a, b| a.0.cmp(b.0));
        for (market, block) in blocks {
            let (side, exit_ms) = Self::block_side_and_exit(Some(block));
            if side.is_empty() {
                continue;
            }
            let side_text = if side == "LONG" { "롱" } else { "숏" };
            let needed = int_or_zero(cfg.get("reentry_wait_bars"));
            if needed == 0 {
                lines.push(format!("{market} {side_text} 재진입 대기: 반대 크로스까지"));
                continue;
            }
            let waited = if exit_ms != 0 {
                self.bars_since_exit(cfg, &latest, exit_ms)
            } else {
                needed
            };
            if waited >= needed {
                lines.push(format!(
                    "{market} {side_text}: 대기 끝 (다음 봉 마감 때 방향 유지 시 재진입)"
                ));
            } else {
                lines.push(format!(
                    "{market} {side_text} 재진입 대기: {waited}/{needed}봉 (반대 크로스 시 즉시 해제)"
                ));
            }
        }
        lines
    }

    fn opposite_signal(
        &self,
        cfg: &Value,
        trade: &Map<String, Value>,
        signal: Option<&Value>,
    ) -> Option<Value> {
        let signal = signal?;
        if signal.is_null() {
            return None;
        }
        let trade_side = trade.get("side").and_then(|v| v.as_str()).unwrap_or("");
        let sig_side = signal.get("side").and_then(|v| v.as_str()).unwrap_or("");
        if !sig_side.is_empty() && sig_side != trade_side {
            return Some(signal.clone());
        }
        let trend = signal
            .get("trend_side")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let flat = signal.get("flat").map(value_truthy).unwrap_or(false);
        if trend.is_empty() || trend == trade_side || flat {
            return None;
        }
        let mut realigned = signal.as_object().cloned().unwrap_or_default();
        let fast = int_or_zero(cfg.get("fast_period"));
        let slow = int_or_zero(cfg.get("slow_period"));
        let tf = cfg.get("timeframe").and_then(|v| v.as_str()).unwrap_or("");
        let market = signal
            .get("market_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let bar = signal.get("bar_open_ms").cloned().unwrap_or(Value::Null);
        let bar_text = match &bar {
            Value::String(s) => s.clone(),
            other => {
                let n = int_or_zero(Some(other));
                if n == 0 {
                    "None".to_string()
                } else {
                    n.to_string()
                }
            }
        };
        realigned.insert("side".to_string(), Value::String(trend.to_string()));
        realigned.insert(
            "entry_type".to_string(),
            Value::String("REALIGN".to_string()),
        );
        realigned.insert("skip_reason".to_string(), Value::String(String::new()));
        realigned.insert(
            "signal_id".to_string(),
            Value::String(format!(
                "{market}:SMA{fast}/{slow}:{tf}:REALIGN:{trend}:{bar_text}"
            )),
        );
        Some(Value::Object(realigned))
    }

    /// Python mutates `result["skip_reason"]` when the coin is waiting to re-enter.
    fn entry_signal(
        &self,
        cfg: &Value,
        state: &Map<String, Value>,
        result: &mut Value,
    ) -> Option<Value> {
        if result
            .get("side")
            .map(|v| !v.is_null() && v.as_str() != Some(""))
            .unwrap_or(false)
        {
            return Some(result.clone());
        }
        let mode = cfg.get("entry_mode").and_then(|v| v.as_str()).unwrap_or("");
        let stale = result.get("stale").map(value_truthy).unwrap_or(false);
        let trend = result
            .get("trend_side")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        if mode != "trend" || stale || trend.is_empty() {
            return None;
        }
        let (blocked, waited, needed) = self.reentry_wait(cfg, state, result);
        if blocked {
            // Exited by profit lock / stop / manual: this coin waits N candles
            // (or for its next cross) before re-entering the same direction.
            let market = result
                .get("market_id")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let tail = if needed != 0 {
                format!("{waited}/{needed}봉")
            } else {
                "반대 크로스까지".to_string()
            };
            if let Some(obj) = result.as_object_mut() {
                obj.insert(
                    "skip_reason".to_string(),
                    Value::String(format!("REENTRY_WAIT: {market} {trend} {tail}")),
                );
            }
            return None;
        }
        let mut signal = result.as_object().cloned().unwrap_or_default();
        let fast = int_or_zero(cfg.get("fast_period"));
        let slow = int_or_zero(cfg.get("slow_period"));
        let tf = cfg.get("timeframe").and_then(|v| v.as_str()).unwrap_or("");
        let market = result
            .get("market_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let bar = result
            .get("bar_open_ms")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        signal.insert("side".to_string(), Value::String(trend.to_string()));
        signal.insert("entry_type".to_string(), Value::String("TREND".to_string()));
        signal.insert("skip_reason".to_string(), Value::String(String::new()));
        signal.insert(
            "signal_id".to_string(),
            Value::String(format!(
                "{market}:SMA{fast}/{slow}:{tf}:TREND:{trend}:{bar}"
            )),
        );
        Some(Value::Object(signal))
    }

    pub(crate) async fn scan(
        &self,
        network: &str,
        cfg: &Value,
        universe: Vec<UniverseItem>,
    ) -> Result<(Vec<UniverseItem>, Vec<Value>, Vec<Value>), ServiceError> {
        let mut signal_futures = Vec::new();
        for (ccxt_symbol, market_id, volume) in universe.iter() {
            let fut = async move {
                let res = self
                    .cross_signal(network, cfg, ccxt_symbol, market_id)
                    .await;
                (res, market_id.clone(), *volume)
            };
            signal_futures.push(fut);
        }
        // Simple sequential scan, preserving volume order (concurrency is a
        // performance detail, not a behavioural one).
        let mut ok = Vec::new();
        let mut errors = Vec::new();
        let _ = SCAN_CONCURRENCY;
        for fut in signal_futures {
            let (res, market_id, volume) = fut.await;
            match res {
                Ok(mut result) => {
                    if let Some(obj) = result.as_object_mut() {
                        obj.insert(
                            "quote_volume".to_string(),
                            volume
                                .and_then(serde_json::Number::from_f64)
                                .map(Value::Number)
                                .unwrap_or(Value::Null),
                        );
                    }
                    ok.push(result);
                }
                Err(err) => {
                    errors.push(json!({
                        "market_id": market_id,
                        "error": format!("{}: {}", err.class, err.message),
                    }));
                }
            }
        }
        Ok((universe, ok, errors))
    }

    // ------------------------------------------------------------------ entry
    pub async fn maybe_enter(
        &self,
        network: &str,
        cfg: &Value,
        mode: &str,
        state: &mut Map<String, Value>,
    ) -> Result<Value, ServiceError> {
        let cfg = self
            .run_pending_entry_mode_switch(network, mode, cfg, state)
            .await?;

        let guard_reason = self.tradfi_entry_block(network, &cfg);
        if !guard_reason.is_empty() {
            let action = if guard_reason == TRADFI_SESSION_CLOSED_REASON {
                "waiting"
            } else {
                "blocked"
            };
            let mut out = Map::new();
            out.insert("action".to_string(), Value::String(action.to_string()));
            out.insert("reason".to_string(), Value::String(guard_reason));
            for (k, v) in self.tradfi_gates(state, network) {
                out.insert(k, v);
            }
            return Ok(Value::Object(out));
        }

        if let Some(pending) = state.get("pending_reverse_entry").cloned() {
            if !pending.is_null() {
                return self
                    .retry_reverse_entry(network, &cfg, mode, state, &pending)
                    .await;
            }
        }

        if let Some(rotation) = state.get("rotation_pending").cloned() {
            if !rotation.is_null() {
                if let Some(rotated) = self
                    .rotate_after_exit(network, &cfg, mode, state, &rotation)
                    .await?
                {
                    return Ok(rotated);
                }
                let reloaded = self.load_state(network, mode)?;
                *state = reloaded;
            }
        }

        let latest = self.latest_closed_bar(&cfg);
        if latest == int_or_zero(state.get("last_evaluated_bar")) {
            return Ok(json!({"action": "waiting", "reason": "WAITING_NEXT_CLOSE"}));
        }

        let universe = match self.scan_universe(network, &cfg).await {
            Ok(u) => u,
            Err(err) => {
                let reason = format!("MARKET_DATA_UNAVAILABLE: {}: {}", err.class, err.message);
                state.insert("last_error".to_string(), Value::String(reason.clone()));
                self.save_state(network, mode, state)?;
                let ledger = self.ledger(network)?;
                ledger.lock().unwrap().event(
                    "SKIP",
                    Map::from_iter([
                        ("mode".to_string(), Value::String(mode.to_string())),
                        ("network".to_string(), Value::String(network.to_string())),
                        ("skip_reason".to_string(), Value::String(reason.clone())),
                    ]),
                );
                return Ok(json!({"action": "skip", "reason": reason}));
            }
        };
        let (_universe, mut results, errors) = match self.scan(network, &cfg, universe).await {
            Ok(r) => r,
            Err(err) => {
                let reason = format!("MARKET_DATA_UNAVAILABLE: {}: {}", err.class, err.message);
                state.insert("last_error".to_string(), Value::String(reason.clone()));
                self.save_state(network, mode, state)?;
                let ledger = self.ledger(network)?;
                ledger.lock().unwrap().event(
                    "SKIP",
                    Map::from_iter([
                        ("mode".to_string(), Value::String(mode.to_string())),
                        ("network".to_string(), Value::String(network.to_string())),
                        ("skip_reason".to_string(), Value::String(reason.clone())),
                    ]),
                );
                return Ok(json!({"action": "skip", "reason": reason}));
            }
        };
        if results.is_empty() {
            let reason = format!("MARKET_DATA_UNAVAILABLE: {} symbols failed", errors.len());
            state.insert("last_error".to_string(), Value::String(reason.clone()));
            self.save_state(network, mode, state)?;
            let ledger = self.ledger(network)?;
            ledger.lock().unwrap().event(
                "SKIP",
                Map::from_iter([
                    ("mode".to_string(), Value::String(mode.to_string())),
                    ("network".to_string(), Value::String(network.to_string())),
                    ("skip_reason".to_string(), Value::String(reason.clone())),
                ]),
            );
            return Ok(json!({"action": "skip", "reason": reason}));
        }
        let has_bar = results
            .iter()
            .any(|r| int_or_zero(r.get("bar_open_ms")) == latest);
        if !has_bar {
            return Ok(json!({"action": "waiting", "reason": "BAR_NOT_READY"}));
        }
        state.insert("last_evaluated_bar".to_string(), json!(latest));
        state.insert("last_error".to_string(), Value::String(String::new()));
        self.update_reentry_blocks(state, &results);

        let mut candidates: Vec<Value> = Vec::new();
        for r in results.iter_mut() {
            if let Some(sig) = self.entry_signal(&cfg, state, r) {
                candidates.push(sig);
            }
        }
        let crosses: Vec<String> = results
            .iter()
            .filter(|r| {
                r.get("side")
                    .and_then(|v| v.as_str())
                    .map(|s| !s.is_empty())
                    .unwrap_or(false)
            })
            .map(|r| {
                format!(
                    "{}:{}",
                    r.get("market_id").and_then(|v| v.as_str()).unwrap_or(""),
                    r.get("side").and_then(|v| v.as_str()).unwrap_or("")
                )
            })
            .take(10)
            .collect();
        let cand_text: Vec<String> = candidates
            .iter()
            .take(5)
            .map(|s| {
                format!(
                    "{}:{}",
                    s.get("market_id").and_then(|v| v.as_str()).unwrap_or(""),
                    s.get("side").and_then(|v| v.as_str()).unwrap_or("")
                )
            })
            .collect();
        let scope = cfg.get("scan_scope").and_then(|v| v.as_str()).unwrap_or("");
        let mode_str = cfg.get("entry_mode").and_then(|v| v.as_str()).unwrap_or("");
        let summary = json!({
            "scope": scope,
            "entry_mode": mode_str,
            "evaluated": results.len(),
            "failed": errors.len(),
            "crosses": crosses,
            "candidates": cand_text,
        });
        if scope == "btc" || scope == "single" {
            let chosen = candidates
                .first()
                .cloned()
                .or_else(|| results.first().cloned())
                .unwrap_or(Value::Null);
            let ledger = self.ledger(network)?;
            let mut fields = chosen.as_object().cloned().unwrap_or_default();
            if let Some(summary_obj) = summary.as_object() {
                for (k, v) in summary_obj {
                    fields.insert(k.clone(), v.clone());
                }
            }
            fields.insert("mode".to_string(), Value::String(mode.to_string()));
            fields.insert("network".to_string(), Value::String(network.to_string()));
            ledger.lock().unwrap().event("SIGNAL", fields);
        } else {
            let first = candidates.first().cloned().unwrap_or_else(|| json!({}));
            let mut fields = Map::new();
            fields.insert("mode".to_string(), Value::String(mode.to_string()));
            fields.insert("network".to_string(), Value::String(network.to_string()));
            if let Some(summary_obj) = summary.as_object() {
                for (k, v) in summary_obj {
                    fields.insert(k.clone(), v.clone());
                }
            }
            let get = |key: &str| first.get(key).cloned().unwrap_or(Value::Null);
            fields.insert("side".to_string(), get("side"));
            let mid = get("market_id");
            fields.insert("market_id".to_string(), mid.clone());
            fields.insert("symbol".to_string(), mid);
            fields.insert("sma_fast".to_string(), get("sma_fast"));
            fields.insert("sma_slow".to_string(), get("sma_slow"));
            let skip = if candidates.is_empty() {
                format!("NO_SIGNAL ({}종목 평가)", results.len())
            } else {
                String::new()
            };
            fields.insert("skip_reason".to_string(), Value::String(skip));
            let ledger = self.ledger(network)?;
            ledger.lock().unwrap().event("SIGNAL", fields);
        }
        self.save_state(network, mode, state)?;
        if candidates.is_empty() {
            return Ok(json!({"action": "skip", "reason": "NO_SIGNAL"}));
        }
        let mut last: Option<Value> = None;
        for signal in candidates {
            let entered = self
                .enter_on_signal(network, &cfg, mode, state, &signal)
                .await?;
            let action = entered.get("action").and_then(|v| v.as_str()).unwrap_or("");
            let reason = entered.get("reason").and_then(|v| v.as_str()).unwrap_or("");
            if action == "skip" && starts_with_any(reason, SYMBOL_SPECIFIC_BLOCKS) {
                let reloaded = self.load_state(network, mode)?;
                *state = reloaded;
                last = Some(entered);
                continue;
            }
            return Ok(entered);
        }
        Ok(last.unwrap_or(Value::Null))
    }

    async fn rotation_universe(
        &self,
        network: &str,
        cfg: &Value,
    ) -> Result<Vec<UniverseItem>, ServiceError> {
        let scope = cfg.get("scan_scope").and_then(|v| v.as_str()).unwrap_or("");
        if scope == TRADFI_SCOPE {
            match self.tradfi_universe(network, cfg).await {
                Ok(u) => return Ok(u),
                Err(err) => {
                    eprintln!("tradfi rotation universe failed: {err}");
                    return Ok(Vec::new());
                }
            }
        }
        let alts = self.volume_universe(network, cfg).await?;
        if scope == "alts" {
            return Ok(alts);
        }
        let mut result = vec![(CCXT_SYMBOL.to_string(), MARKET_ID.to_string(), None)];
        result.extend(alts.into_iter().filter(|item| item.1 != MARKET_ID));
        Ok(result)
    }

    async fn rotate_after_exit(
        &self,
        network: &str,
        cfg: &Value,
        mode: &str,
        state: &mut Map<String, Value>,
        rotation: &Value,
    ) -> Result<Option<Value>, ServiceError> {
        let entry_mode = cfg.get("entry_mode").and_then(|v| v.as_str()).unwrap_or("");
        let rotate = cfg
            .get("rotate_after_exit")
            .map(value_truthy)
            .unwrap_or(false);
        let scope = cfg.get("scan_scope").and_then(|v| v.as_str()).unwrap_or("");
        if entry_mode != "trend" || !rotate || scope == "single" {
            state.insert("rotation_pending".to_string(), Value::Null);
            self.save_state(network, mode, state)?;
            return Ok(None);
        }
        let exit_ms = int_or_zero(rotation.get("exit_ms"));
        let window = int_or_zero(cfg.get("reverse_retry_window_seconds")) * 1000;
        if self.now_ms() - exit_ms > window {
            state.insert("rotation_pending".to_string(), Value::Null);
            self.save_state(network, mode, state)?;
            let ledger = self.ledger(network)?;
            ledger.lock().unwrap().event(
                "ROTATION_EXPIRED",
                Map::from_iter([
                    ("mode".to_string(), Value::String(mode.to_string())),
                    ("network".to_string(), Value::String(network.to_string())),
                    (
                        "exclude".to_string(),
                        rotation.get("exclude").cloned().unwrap_or(Value::Null),
                    ),
                ]),
            );
            return Ok(None);
        }
        let excluded = rotation
            .get("exclude")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let scan = async {
            let universe = self.rotation_universe(network, cfg).await?;
            let filtered: Vec<UniverseItem> = universe
                .into_iter()
                .filter(|item| item.1 != excluded)
                .collect();
            self.scan(network, cfg, filtered).await
        }
        .await;
        let (_u, results, errors) = match scan {
            Ok(r) => r,
            Err(err) => {
                let ledger = self.ledger(network)?;
                ledger.lock().unwrap().event(
                    "SKIP",
                    Map::from_iter([
                        ("mode".to_string(), Value::String(mode.to_string())),
                        ("network".to_string(), Value::String(network.to_string())),
                        (
                            "skip_reason".to_string(),
                            Value::String(format!(
                                "ROTATION_DATA_UNAVAILABLE: {}: {}",
                                err.class, err.message
                            )),
                        ),
                    ]),
                );
                return Ok(Some(
                    json!({"action": "waiting", "reason": "ROTATION_RETRY"}),
                ));
            }
        };
        self.update_reentry_blocks(state, &results);
        let mut candidates: Vec<Value> = Vec::new();
        for result in &results {
            let side_opt = result
                .get("side")
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty())
                .map(|s| s.to_string())
                .or_else(|| {
                    result
                        .get("trend_side")
                        .and_then(|v| v.as_str())
                        .filter(|s| !s.is_empty())
                        .map(|s| s.to_string())
                });
            let Some(side) = side_opt else {
                continue;
            };
            if result.get("flat").map(value_truthy).unwrap_or(false) {
                continue;
            }
            let mut probe = result.as_object().cloned().unwrap_or_default();
            probe.insert("trend_side".to_string(), Value::String(side.clone()));
            let probe_value = Value::Object(probe);
            if self.reentry_wait(cfg, state, &probe_value).0 {
                continue;
            }
            let mut signal = result.as_object().cloned().unwrap_or_default();
            let fast = int_or_zero(cfg.get("fast_period"));
            let slow = int_or_zero(cfg.get("slow_period"));
            let tf = cfg.get("timeframe").and_then(|v| v.as_str()).unwrap_or("");
            let market = result
                .get("market_id")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            signal.insert("side".to_string(), Value::String(side.clone()));
            signal.insert(
                "entry_type".to_string(),
                Value::String("ROTATE".to_string()),
            );
            signal.insert("skip_reason".to_string(), Value::String(String::new()));
            signal.insert(
                "signal_id".to_string(),
                Value::String(format!(
                    "{market}:SMA{fast}/{slow}:{tf}:ROTATE:{side}:{exit_ms}"
                )),
            );
            candidates.push(Value::Object(signal));
        }
        let cand_text: Vec<String> = candidates
            .iter()
            .take(5)
            .map(|s| {
                format!(
                    "{}:{}",
                    s.get("market_id").and_then(|v| v.as_str()).unwrap_or(""),
                    s.get("side").and_then(|v| v.as_str()).unwrap_or("")
                )
            })
            .collect();
        let first_side = candidates
            .first()
            .and_then(|s| s.get("side").cloned())
            .unwrap_or(Value::Null);
        let first_market = candidates
            .first()
            .and_then(|s| s.get("market_id").cloned())
            .unwrap_or(Value::Null);
        let skip = if candidates.is_empty() {
            format!(
                "NO_ROTATION_CANDIDATE ({excluded} 제외 {}종목 평가)",
                results.len()
            )
        } else {
            String::new()
        };
        let ledger = self.ledger(network)?;
        ledger.lock().unwrap().event(
            "SIGNAL",
            Map::from_iter([
                ("mode".to_string(), Value::String(mode.to_string())),
                ("network".to_string(), Value::String(network.to_string())),
                ("rotation".to_string(), Value::Bool(true)),
                ("exclude".to_string(), Value::String(excluded.clone())),
                ("evaluated".to_string(), json!(results.len())),
                ("failed".to_string(), json!(errors.len())),
                ("candidates".to_string(), json!(cand_text)),
                ("side".to_string(), first_side),
                ("market_id".to_string(), first_market),
                ("skip_reason".to_string(), Value::String(skip)),
            ]),
        );
        state.insert("rotation_pending".to_string(), Value::Null);
        self.save_state(network, mode, state)?;
        if candidates.is_empty() {
            return Ok(Some(
                json!({"action": "skip", "reason": "NO_ROTATION_CANDIDATE"}),
            ));
        }
        let mut last: Option<Value> = None;
        for signal in candidates {
            let entered = self
                .enter_on_signal(network, cfg, mode, state, &signal)
                .await?;
            let action = entered.get("action").and_then(|v| v.as_str()).unwrap_or("");
            let reason = entered
                .get("reason")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            if action == "skip" && starts_with_any(&reason, SYMBOL_SPECIFIC_BLOCKS) {
                let reloaded = self.load_state(network, mode)?;
                *state = reloaded;
                last = Some(entered);
                continue;
            }
            if action == "skip" && starts_with_any(&reason, TRANSIENT_ENTRY_BLOCKS) {
                let mut reloaded = self.load_state(network, mode)?;
                let sig_id = signal
                    .get("signal_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                if let Some(list) = reloaded
                    .get_mut("processed_signals")
                    .and_then(|v| v.as_array_mut())
                {
                    list.retain(|s| s.as_str() != Some(sig_id.as_str()));
                }
                reloaded.insert("rotation_pending".to_string(), rotation.clone());
                self.save_state(network, mode, &mut reloaded)?;
                *state = reloaded;
                return Ok(Some(
                    json!({"action": "waiting", "reason": format!("ROTATION_RETRY: {reason}")}),
                ));
            }
            return Ok(Some(entered));
        }
        Ok(last)
    }

    pub(crate) async fn enter_on_signal(
        &self,
        network: &str,
        cfg: &Value,
        mode: &str,
        state: &mut Map<String, Value>,
        signal: &Value,
    ) -> Result<Value, ServiceError> {
        let signal_id = signal
            .get("signal_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let processed = state
            .get("processed_signals")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();
        if processed
            .iter()
            .any(|s| s.as_str() == Some(signal_id.as_str()))
        {
            return Ok(json!({"action": "skip", "reason": "DUPLICATE_SIGNAL"}));
        }
        self.set_phase(
            network,
            mode,
            state,
            SIGNAL_DETECTED,
            Map::from_iter([(
                "last_signal_id".to_string(),
                Value::String(signal_id.clone()),
            )]),
        )?;
        self.set_phase(network, mode, state, PRE_TRADE_VALIDATION, Map::new())?;
        let (decision, reason, context) = self
            .pre_trade_validation(network, cfg, mode, signal)
            .await?;
        if let Some(list) = state
            .get_mut("processed_signals")
            .and_then(|v| v.as_array_mut())
        {
            list.push(Value::String(signal_id.clone()));
        }
        if !reason.is_empty() {
            let ledger = self.ledger(network)?;
            let mut fields = context.clone();
            fields.insert("mode".to_string(), Value::String(mode.to_string()));
            fields.insert("network".to_string(), Value::String(network.to_string()));
            fields.insert("signal_id".to_string(), Value::String(signal_id.clone()));
            fields.insert(
                "side".to_string(),
                signal.get("side").cloned().unwrap_or(Value::Null),
            );
            fields.insert("skip_reason".to_string(), Value::String(reason.clone()));
            fields.insert(
                "symbol".to_string(),
                signal.get("market_id").cloned().unwrap_or(Value::Null),
            );
            ledger.lock().unwrap().event("SKIP", fields);
            self.set_phase(network, mode, state, IDLE, Map::new())?;
            let market = signal
                .get("market_id")
                .map(py_string)
                .unwrap_or_else(|| "None".to_string());
            let side = signal.get("side").and_then(|v| v.as_str()).unwrap_or("");
            self.notify(
                format!(
                    "⚠️ {}: {market} {side} 진입 건너뜀 — {reason}",
                    self.label()
                ),
                Some(&format!("skip:{signal_id}")),
            )
            .await;
            return Ok(json!({"action": "skip", "reason": reason}));
        }
        let decision = decision.unwrap_or(Value::Null);
        if self.mode(Some(cfg)) == "live" {
            return self
                .enter_live(network, cfg, state, signal, &decision, &context)
                .await;
        }
        self.enter_paper(network, cfg, state, signal, &decision, &context)
            .await
    }

    pub async fn pre_trade_validation(
        &self,
        network: &str,
        cfg: &Value,
        mode: &str,
        signal: &Value,
    ) -> Result<(Option<Value>, String, Map<String, Value>), ServiceError> {
        let mut context = Map::new();
        let (ccxt_symbol, market_id) = symbol_pair(Some(signal));
        let (wallet, available, equity, balance_source) =
            match self.balances(network, cfg, mode).await {
                Ok(v) => v,
                Err(err) => {
                    return Ok((
                        None,
                        format!("BALANCE_UNAVAILABLE: {}", err.message),
                        context,
                    ))
                }
            };
        context.insert("wallet_balance".to_string(), num_to_value(wallet));
        context.insert("available_balance".to_string(), num_to_value(available));
        context.insert("equity".to_string(), num_to_value(equity));
        context.insert(
            "balance_source".to_string(),
            Value::String(balance_source.to_string()),
        );
        if mode == "live" {
            let positions = match self.fetch_account_positions(network).await {
                Ok(p) => p,
                Err(err) => {
                    return Ok((
                        None,
                        format!("POSITION_STATE_UNCERTAIN: {}", err.message),
                        context,
                    ))
                }
            };
            if !positions.is_empty() {
                let held = positions
                    .iter()
                    .take(3)
                    .map(|p| {
                        format!(
                            "{} {} {}",
                            py_string(p.get("symbol").unwrap_or(&Value::Null)),
                            crate::strategy::service::position_side(Some(p)),
                            crate::strategy::service::position_qty(Some(p))
                        )
                    })
                    .collect::<Vec<_>>()
                    .join(", ");
                return Ok((None, format!("ACCOUNT_POSITION_EXISTS: {held}"), context));
            }
            let setup_error = self
                .ensure_account_setup(network, cfg, &ccxt_symbol)
                .await?;
            if !setup_error.is_empty() {
                return Ok((None, setup_error, context));
            }
            let stray = self.cancel_stray_protection(network, &market_id).await?;
            if !stray.is_empty() {
                return Ok((None, stray, context));
            }
        }
        let (rules, market) = match async {
            let rules = self.trading_rules(network, cfg, &market_id).await?;
            let market = self.premium_index(network, &market_id).await?;
            Ok::<_, ServiceError>((rules, market))
        }
        .await
        {
            Ok(v) => v,
            Err(err) => {
                return Ok((
                    None,
                    format!("TRADING_RULES_UNAVAILABLE: {}: {}", err.class, err.message),
                    context,
                ))
            }
        };
        let (fee_rate, fee_source) = self.taker_fee_rate(network, cfg, &market_id).await?;
        let market_obj = market;
        let mark_price_value = market_obj.get("mark_price").cloned().unwrap_or(Value::Null);
        let price = py_dec(Some(&mark_price_value))?;
        if price.is_zero() {
            // Python raises decimal.DivisionByZero here; never panic in Rust.
            return Err(ServiceError::new("DivisionByZero", "mark price is zero"));
        }
        let leverage = py_dec(cfg.get("leverage"))?;
        if leverage.is_zero() {
            return Err(ServiceError::new("DivisionByZero", "leverage is zero"));
        }
        let margin = dec_from_f64(available)? * py_dec(cfg.get("margin_fraction"))?;
        let step = rules.effective_step();
        let quantity = crate::core::rules::floor_to_step(
            &(margin * leverage / price).to_string(),
            &step.to_string(),
        )
        .map_err(|e| ServiceError::new("ValueError", e.to_string()))?;
        let min_qty = rules.effective_min_qty();
        let min_notional_ceil = crate::core::rules::ceil_to_step(
            &(rules.min_notional / price).to_string(),
            &step.to_string(),
        )
        .map_err(|e| ServiceError::new("ValueError", e.to_string()))?;
        let required = min_qty.max(min_notional_ceil);
        context.insert("symbol".to_string(), Value::String(market_id.clone()));
        context.insert("mark_price".to_string(), mark_price_value);
        context.insert(
            "funding_rate".to_string(),
            market_obj
                .get("funding_rate")
                .cloned()
                .unwrap_or(Value::Null),
        );
        context.insert(
            "taker_fee_rate".to_string(),
            Value::String(fee_rate.to_string()),
        );
        context.insert(
            "fee_source".to_string(),
            Value::String(fee_source.to_string()),
        );
        context.insert("margin".to_string(), Value::String(margin.to_string()));
        context.insert("leverage".to_string(), Value::String(leverage.to_string()));
        context.insert("quantity".to_string(), Value::String(quantity.to_string()));
        context.insert(
            "exchange_minimum_quantity".to_string(),
            Value::String(required.to_string()),
        );
        context.insert("step_size".to_string(), Value::String(step.to_string()));
        context.insert(
            "min_notional".to_string(),
            Value::String(rules.min_notional.to_string()),
        );
        let mut quantity = quantity;
        if quantity < required {
            if !cfg
                .get("allow_auto_raise_to_exchange_minimum")
                .map(value_truthy)
                .unwrap_or(false)
            {
                return Ok((
                    None,
                    format!(
                        "BELOW_EXCHANGE_MINIMUM: {market_id} {quantity} < {required} (증거금 {margin:.2} USDT x {leverage}배, 최소 주문 {} USDT)",
                        rules.min_notional
                    ),
                    context,
                ));
            }
            quantity = required;
        }
        if quantity > rules.effective_max_qty() {
            return Ok((
                None,
                format!("ABOVE_MAX_QTY: {quantity} > {}", rules.effective_max_qty()),
                context,
            ));
        }
        let notional = quantity * price;
        let needed = notional / leverage;
        let available_dec = dec_from_f64(available)?;
        let max_usage = py_dec(cfg.get("max_margin_usage_pct"))?;
        if needed > available_dec * max_usage {
            return Ok((
                None,
                format!("INSUFFICIENT_MARGIN: needs {needed:.4} > available {available:.4} USDT"),
                context,
            ));
        }
        let rules_value = serde_json::to_value(&rules)
            .map_err(|e| ServiceError::new("ValueError", format!("invalid trading rules: {e}")))?;
        let fee_rate_dec = Decimal::from_str_exact(&fee_rate).unwrap_or(Decimal::ZERO);
        let estimated_entry_fee = notional * fee_rate_dec;
        let margin_fraction = cfg
            .get("margin_fraction")
            .map(py_string)
            .unwrap_or_else(|| "None".to_string());
        let leverage_text = cfg
            .get("leverage")
            .map(py_string)
            .unwrap_or_else(|| "None".to_string());
        let decision = json!({
            "accepted": true,
            "quantity": quantity.to_string(),
            "notional": notional.to_string(),
            "margin": needed.to_string(),
            "estimated_entry_fee": estimated_entry_fee.to_string(),
            "estimated_total_risk": Value::Null,
            "sizing_mode": format!("margin_{margin_fraction}x{leverage_text}"),
            "rules": rules_value,
            "fee_rate": fee_rate,
        });
        Ok((Some(decision), String::new(), context))
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) async fn new_trade(
        &self,
        network: &str,
        mode: &str,
        signal: &Value,
        decision: &Value,
        _context: &Map<String, Value>,
        trade_id: &str,
        entry_price: &str,
        quantity: Decimal,
        entry_ms: i64,
        rules: &crate::core::rules::TradingRules,
        cfg: &Value,
    ) -> Result<Map<String, Value>, ServiceError> {
        let entry = Decimal::from_str_exact(entry_price)
            .or_else(|_| Decimal::from_scientific(entry_price))
            .unwrap_or(Decimal::ZERO);
        let qty = quantity;
        let leverage = int_or_zero(cfg.get("leverage"));
        let (ccxt_symbol, market_id) = symbol_pair(Some(signal));
        let side = signal.get("side").and_then(|v| v.as_str()).unwrap_or("");
        let emergency_roi = py_f(cfg.get("emergency_stop_roi_percent"), 0.0);
        let stop = crate::core::sma_cross::emergency_stop_price(
            side,
            dec_to_f64(entry),
            leverage as f64,
            emergency_roi,
        );
        let mut stop_text = String::new();
        if let Some(stop_val) = stop {
            let direction = if side == "LONG" { "up" } else { "down" };
            let rounded = crate::core::rules::round_price_to_tick(
                &format_f64(stop_val),
                &rules.tick_size.to_string(),
                direction,
            )
            .map_err(|e| ServiceError::new("ValueError", e.to_string()))?;
            stop_text = rounded.to_string();
        }
        let margin = entry * qty / Decimal::from(leverage);
        let signal_bar = Self::signal_bar(signal);
        let mut trade = Map::new();
        trade.insert("trade_id".to_string(), Value::String(trade_id.to_string()));
        trade.insert(
            "signal_id".to_string(),
            signal.get("signal_id").cloned().unwrap_or(Value::Null),
        );
        trade.insert("signal_bar_ms".to_string(), json!(signal_bar));
        trade.insert("symbol".to_string(), Value::String(market_id.clone()));
        trade.insert("market_id".to_string(), Value::String(market_id));
        trade.insert("ccxt_symbol".to_string(), Value::String(ccxt_symbol));
        trade.insert("mode".to_string(), Value::String(mode.to_string()));
        trade.insert("network".to_string(), Value::String(network.to_string()));
        trade.insert("status".to_string(), Value::String("OPEN".to_string()));
        trade.insert("side".to_string(), Value::String(side.to_string()));
        let entry_type = signal
            .get("entry_type")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .unwrap_or("CROSS");
        trade.insert(
            "entry_type".to_string(),
            Value::String(entry_type.to_string()),
        );
        trade.insert("quantity".to_string(), Value::String(qty.to_string()));
        trade.insert("entry_price".to_string(), Value::String(entry.to_string()));
        trade.insert("stop_price".to_string(), Value::String(stop_text));
        trade.insert(
            "take_profit_price".to_string(),
            Value::String(String::new()),
        );
        trade.insert("entry_time".to_string(), Value::String(iso_ms(entry_ms)));
        trade.insert("entry_ms".to_string(), json!(entry_ms));
        trade.insert(
            "initial_risk".to_string(),
            Value::String(margin.to_string()),
        );
        trade.insert("margin_used".to_string(), Value::String(margin.to_string()));
        trade.insert("leverage".to_string(), json!(leverage));
        let fee_rate_decision = decision
            .get("fee_rate")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .map(|s| s.to_string());
        let fee_rate = fee_rate_decision.unwrap_or_else(|| {
            cfg.get("estimated_taker_fee_rate")
                .map(py_string)
                .unwrap_or_default()
        });
        trade.insert("fee_rate".to_string(), Value::String(fee_rate));
        trade.insert(
            "strategy_version".to_string(),
            Value::String(self.variant.strategy_version().to_string()),
        );
        trade.insert("rule_violation".to_string(), Value::String(String::new()));
        trade.insert(
            "emergency_stop_roi".to_string(),
            cfg.get("emergency_stop_roi_percent")
                .cloned()
                .unwrap_or(Value::Null),
        );
        trade.insert("best_price".to_string(), Value::String(entry.to_string()));
        trade.insert("lock_roi".to_string(), Value::String(String::new()));
        trade.insert("lock_price".to_string(), Value::String(String::new()));
        trade.insert("sl_revision".to_string(), json!(0));
        trade.insert("lock_revision".to_string(), json!(0));
        trade.insert(
            "timeframe".to_string(),
            cfg.get("timeframe").cloned().unwrap_or(Value::Null),
        );
        trade.insert(
            "slow_period".to_string(),
            cfg.get("slow_period").cloned().unwrap_or(Value::Null),
        );
        trade.insert(
            "fast_period".to_string(),
            cfg.get("fast_period").cloned().unwrap_or(Value::Null),
        );
        trade.insert(
            "tick_size".to_string(),
            Value::String(rules.tick_size.to_string()),
        );
        Ok(trade)
    }

    pub(crate) fn on_trade_closed(
        &self,
        _network: &str,
        _mode: &str,
        state: &mut Map<String, Value>,
        trade: &Map<String, Value>,
    ) {
        self.count_profit_lock_exits(state, trade);
        let mut blocks = state
            .get("reentry_block")
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default();
        let market = trade
            .get("market_id")
            .and_then(|v| v.as_str())
            .or_else(|| trade.get("symbol").and_then(|v| v.as_str()))
            .unwrap_or(MARKET_ID)
            .to_string();
        let exit_reason = trade
            .get("exit_reason")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if exit_reason == "CROSS_REVERSE" {
            blocks.remove(&market);
        } else {
            let mut block = Map::new();
            block.insert(
                "side".to_string(),
                trade.get("side").cloned().unwrap_or(Value::Null),
            );
            block.insert("exit_ms".to_string(), json!(self.now_ms()));
            block.insert(
                "exit_reason".to_string(),
                trade.get("exit_reason").cloned().unwrap_or(Value::Null),
            );
            blocks.insert(market.clone(), Value::Object(block));
            let cfg = self.config();
            let entry_mode = cfg.get("entry_mode").and_then(|v| v.as_str()).unwrap_or("");
            let rotate = cfg
                .get("rotate_after_exit")
                .map(value_truthy)
                .unwrap_or(false);
            let scope = cfg.get("scan_scope").and_then(|v| v.as_str()).unwrap_or("");
            if entry_mode == "trend" && rotate && scope != "single" {
                let mut rotation = Map::new();
                rotation.insert("exclude".to_string(), Value::String(market));
                rotation.insert("exit_ms".to_string(), json!(self.now_ms()));
                rotation.insert(
                    "exit_reason".to_string(),
                    trade.get("exit_reason").cloned().unwrap_or(Value::Null),
                );
                state.insert("rotation_pending".to_string(), Value::Object(rotation));
            }
        }
        state.insert("reentry_block".to_string(), Value::Object(blocks));
    }

    fn count_profit_lock_exits(
        &self,
        state: &mut Map<String, Value>,
        trade: &Map<String, Value>,
    ) -> bool {
        let mut count = int_or_zero(state.get("profit_lock_exit_count"));
        let switch_count = int_or_zero(self.config().get("profit_lock_switch_count"));
        let forced = state
            .get("entry_mode_forced_cross")
            .map(value_truthy)
            .unwrap_or(false);
        let mut changed = false;
        let exit_reason = trade
            .get("exit_reason")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if exit_reason == PROFIT_LOCK_EXIT_REASON {
            count = (count + 1) & 0x7FFF;
            if switch_count != 0 && !forced && count >= switch_count {
                let from_mode = self
                    .config()
                    .get("entry_mode")
                    .cloned()
                    .unwrap_or(Value::Null);
                let mut switch = Map::new();
                switch.insert("entry_mode".to_string(), Value::String("cross".to_string()));
                switch.insert("from_mode".to_string(), from_mode);
                switch.insert("exit_count".to_string(), json!(count));
                switch.insert("ts".to_string(), json!(self.now_ms()));
                switch.insert(
                    "trade_id".to_string(),
                    trade.get("trade_id").cloned().unwrap_or(Value::Null),
                );
                let market = trade
                    .get("market_id")
                    .cloned()
                    .or_else(|| trade.get("symbol").cloned())
                    .unwrap_or(Value::Null);
                switch.insert("market_id".to_string(), market);
                state.insert(
                    "pending_entry_mode_switch".to_string(),
                    Value::Object(switch),
                );
                state.insert("entry_mode_forced_cross".to_string(), Value::Bool(true));
                count = 0;
                changed = true;
            }
        }
        let current = int_or_zero(state.get("profit_lock_exit_count"));
        if count != current {
            changed = true;
        }
        state.insert("profit_lock_exit_count".to_string(), json!(count));
        changed
    }

    pub fn entry_mode_switch_status(
        &self,
        cfg: Option<&Value>,
        state: Option<&Map<String, Value>>,
    ) -> Value {
        let owned;
        let cfg_ref: &Value = match cfg {
            Some(c) => c,
            None => {
                owned = self.config();
                &owned
            }
        };
        let owned_state;
        let state_ref: &Map<String, Value> = match state {
            Some(s) => s,
            None => {
                owned_state = match self.current_network(Some(cfg_ref)) {
                    Some(network) => self
                        .load_state(&network, &self.mode(Some(cfg_ref)))
                        .unwrap_or_default(),
                    None => Map::new(),
                };
                &owned_state
            }
        };
        json!({
            "switch_count": int_or_zero(cfg_ref.get("profit_lock_switch_count")),
            "exit_count": int_or_zero(state_ref.get("profit_lock_exit_count")),
            "forced": state_ref.get("entry_mode_forced_cross").map(value_truthy).unwrap_or(false),
            "last_switch": state_ref.get("last_entry_mode_switch").and_then(|v| v.as_object()).cloned().unwrap_or_default(),
            "pending": state_ref.get("pending_entry_mode_switch").and_then(|v| v.as_object()).cloned().unwrap_or_default(),
        })
    }

    fn apply_switch_to_config(cfg: &Value, switch: &Value) -> Value {
        let mut forced = cfg.as_object().cloned().unwrap_or_default();
        let entry_mode = switch
            .get("entry_mode")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .unwrap_or(FORCED_ENTRY_MODE)
            .to_string();
        forced.insert("entry_mode".to_string(), Value::String(entry_mode));
        Value::Object(forced)
    }

    pub async fn run_pending_entry_mode_switch(
        &self,
        network: &str,
        mode: &str,
        cfg: &Value,
        state: &mut Map<String, Value>,
    ) -> Result<Value, ServiceError> {
        let switch = state
            .get("pending_entry_mode_switch")
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default();
        if switch.is_empty() {
            let forced = state
                .get("entry_mode_forced_cross")
                .map(value_truthy)
                .unwrap_or(false);
            let entry_mode = cfg.get("entry_mode").and_then(|v| v.as_str()).unwrap_or("");
            if forced && entry_mode != FORCED_ENTRY_MODE {
                state.insert("entry_mode_forced_cross".to_string(), Value::Bool(false));
                state.insert("profit_lock_exit_count".to_string(), json!(0));
                self.save_state(network, mode, state)?;
            }
            return Ok(cfg.clone());
        }
        let mut ok = false;
        let mut error = String::new();
        let target_mode = switch
            .get("entry_mode")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .unwrap_or(FORCED_ENTRY_MODE)
            .to_string();
        match self.deps.config_writer.clone() {
            None => {
                error = "RuntimeError: ENTRY_MODE_WRITER_MISSING".to_string();
                eprintln!("entry mode switch write failed: {error}");
            }
            Some(writer) => {
                ok = writer(target_mode.clone()).await;
            }
        }
        let switch_value = Value::Object(switch.clone());
        state.insert(
            "pending_entry_mode_switch".to_string(),
            if ok {
                Value::Null
            } else {
                switch_value.clone()
            },
        );
        let mut last = switch.clone();
        last.insert("applied_ms".to_string(), json!(self.now_ms()));
        last.insert("applied".to_string(), Value::Bool(ok));
        last.insert("error".to_string(), Value::String(error.clone()));
        state.insert("last_entry_mode_switch".to_string(), Value::Object(last));
        state.insert("last_evaluated_bar".to_string(), json!(0));
        self.save_state(network, mode, state)?;
        {
            let ledger = self.ledger(network)?;
            let entry_mode = switch.get("entry_mode").cloned().unwrap_or(Value::Null);
            let from_mode = switch.get("from_mode").cloned().unwrap_or(Value::Null);
            let exit_count = switch.get("exit_count").cloned().unwrap_or(Value::Null);
            let market_id = switch.get("market_id").cloned().unwrap_or(Value::Null);
            ledger.lock().unwrap().event(
                "ENTRY_MODE_SWITCH",
                Map::from_iter([
                    ("mode".to_string(), Value::String(mode.to_string())),
                    ("network".to_string(), Value::String(network.to_string())),
                    ("applied".to_string(), Value::Bool(ok)),
                    ("error".to_string(), Value::String(error)),
                    ("entry_mode".to_string(), entry_mode),
                    ("from_mode".to_string(), from_mode),
                    ("exit_count".to_string(), exit_count),
                    ("market_id".to_string(), market_id),
                ]),
            );
        }
        if ok {
            let exit_count = switch
                .get("exit_count")
                .map(py_string)
                .unwrap_or_else(|| "None".to_string());
            self.notify(
                format!(
                    "🔁 {}: 수익 확보 익절 {exit_count}회 달성 — 다음 진입부터 '돌파 시 진입' 모드로 전환했습니다.",
                    self.label()
                ),
                Some("entry_mode_switch"),
            )
            .await;
        }
        Ok(Self::apply_switch_to_config(cfg, &switch_value))
    }
}

// ----------------------------------------------------------------- helpers

fn dec_to_f64(d: Decimal) -> f64 {
    d.to_string().parse::<f64>().unwrap_or(0.0)
}

fn format_f64(f: f64) -> String {
    if f.is_finite() && f.fract() == 0.0 && f.abs() < 1e15 {
        return format!("{}", f as i64);
    }
    f.to_string()
}

fn py_string(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::Bool(b) => {
            if *b {
                "True".to_string()
            } else {
                "False".to_string()
            }
        }
        Value::String(s) => s.clone(),
        Value::Number(n) => n.to_string(),
        other => other.to_string(),
    }
}

fn num_to_value(f: f64) -> Value {
    serde_json::Number::from_f64(f)
        .map(Value::Number)
        .unwrap_or(Value::Null)
}

fn universe_to_value(items: &[UniverseItem]) -> Value {
    let arr: Vec<Value> = items
        .iter()
        .map(|(s, m, v)| {
            json!([
                s,
                m,
                v.and_then(serde_json::Number::from_f64)
                    .map(Value::Number)
                    .unwrap_or(Value::Null)
            ])
        })
        .collect();
    Value::Array(arr)
}

fn universe_from_value(value: &Value) -> Vec<UniverseItem> {
    value
        .as_array()
        .map(|arr| {
            arr.iter()
                .filter_map(|row| {
                    let items = row.as_array()?;
                    let s = items.first()?.as_str()?.to_string();
                    let m = items.get(1)?.as_str()?.to_string();
                    let v = items.get(2).and_then(|v| v.as_f64());
                    Some((s, m, v))
                })
                .collect()
        })
        .unwrap_or_default()
}
