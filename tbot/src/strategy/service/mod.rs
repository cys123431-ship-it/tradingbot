// WIP port: lints are relaxed until the remaining service steps land.
#![allow(
    dead_code,
    unused_imports,
    clippy::type_complexity,
    clippy::redundant_closure,
    clippy::manual_async_fn,
    clippy::too_many_arguments
)]

//! Shared SMA-cross service skeleton (BtcMa / MaPlus variants).
//!
//! S1 only moves the type definitions and the parent (BtcEmaPullbackService)
//! base methods.  Later stages append to `base.rs`'s `impl` blocks.

pub mod base;
mod stubs;
mod stubs2;
pub mod trading;

use std::collections::HashMap;
use std::future::Future;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::{Arc, Mutex};

use serde_json::{Map, Value};

use crate::exchange::{ExchangeError, FuturesExchange};
use crate::safety::algo_gateway::BinanceAlgoOrderGateway;
use crate::safety::order_gateway::IdempotentOrderGateway;
use crate::store::clock::Clock;
use crate::store::ledger::StrategyLedger;
use crate::store::order_state::SQLiteTradingStateStore;
use crate::strategy::{config_btc_ma, config_ma_plus, tradfi};

/// `ServiceError` is `ExchangeError`; the class field carries the Python type name.
pub type ServiceError = ExchangeError;

pub type BoxFut<T> = Pin<Box<dyn Future<Output = T> + Send>>;

// ---------------------------------------------------------------------------
// Module-level constants (btc_ema_pullback/service.py, btc_ma_cross/service.py)
// ---------------------------------------------------------------------------

pub const CCXT_SYMBOL: &str = "BTC/USDT:USDT";
pub const MARKET_ID: &str = "BTCUSDT";

/// Python set: `{"NEW", "PARTIALLY_FILLED", "WORKING", ""}`.
pub const OPEN_ALGO_STATUSES: &[&str] = &["NEW", "PARTIALLY_FILLED", "WORKING", ""];

pub const IDLE: &str = "IDLE";
pub const SIGNAL_DETECTED: &str = "SIGNAL_DETECTED";
pub const PRE_TRADE_VALIDATION: &str = "PRE_TRADE_VALIDATION";
pub const ENTRY_SUBMITTED: &str = "ENTRY_SUBMITTED";
pub const ENTRY_FILLED: &str = "ENTRY_FILLED";
pub const PROTECTION_ORDERS_SUBMITTED: &str = "PROTECTION_ORDERS_SUBMITTED";
pub const POSITION_OPEN: &str = "POSITION_OPEN";
pub const EXIT_FILLED: &str = "EXIT_FILLED";
pub const TRADE_RECORDED: &str = "TRADE_RECORDED";
pub const ERROR_RECOVERY: &str = "ERROR_RECOVERY";
pub const PROTECTION_FAILED: &str = "PROTECTION_FAILED";
pub const POSITION_RECONCILIATION: &str = "POSITION_RECONCILIATION";

// btc_ma_cross/service.py extras.
pub const PROFIT_LOCK_EXIT_REASON: &str = "PROFIT_LOCK";
pub const FORCED_ENTRY_MODE: &str = "cross";
pub const ENTRY_MODE_SWITCH_STATE_KEYS: (&str, &str) =
    ("pending_entry_mode_switch", "last_entry_mode_switch");

// TRADFI_* live in `tbot::strategy::tradfi`; reuse them.
pub use tradfi::{
    BTC_MA_PROFIT_LOCK_EXIT_REASON, BTC_MA_TRADFI_MAINNET_ONLY_REASON,
    BTC_MA_TRADFI_SESSION_CLOSED_REASON, TRADFI_MAINNET_ONLY_REASON, TRADFI_SCOPE,
    TRADFI_SESSION_CLOSED_REASON,
};

// ---------------------------------------------------------------------------
// Helpers (renamed from Python `_f`, `_iso`, ...).
// ---------------------------------------------------------------------------

/// `float(value)` failing to the default (mirrors Python `_f`).
pub fn py_f(value: Option<&Value>, default: f64) -> f64 {
    match value {
        None | Some(Value::Null) => default,
        Some(Value::Bool(b)) => {
            if *b {
                1.0
            } else {
                0.0
            }
        }
        Some(Value::Number(n)) => n.as_f64().unwrap_or(default),
        Some(Value::String(s)) => s.trim().parse::<f64>().unwrap_or(default),
        Some(_) => default,
    }
}

/// ISO-8601 UTC timestamp (ms -> RFC3339).
pub fn iso_ms(ms: i64) -> String {
    use chrono::{TimeZone, Utc};
    Utc.timestamp_millis_opt(ms)
        .single()
        .map(|dt| dt.to_rfc3339_opts(chrono::SecondsFormat::Micros, false))
        .unwrap_or_default()
}

/// Upper-cased symbol without the settle suffix: `BTC/USDT:USDT` -> `BTCUSDT`.
pub fn market_key(symbol: Option<&str>) -> String {
    let s = symbol.unwrap_or("");
    s.to_uppercase()
        .split_once(':')
        .map(|(a, _)| a.to_string())
        .unwrap_or_else(|| s.to_uppercase())
        .replace('/', "")
}

/// `(ccxt_symbol, market_id)` of a trade or signal; BTCUSDT when unspecified.
pub fn symbol_pair(item: Option<&Value>) -> (String, String) {
    let map = item.and_then(|v| v.as_object());
    let ccxt_symbol = map
        .and_then(|m| m.get("ccxt_symbol"))
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let market = map
        .and_then(|m| m.get("market_id"))
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
        .unwrap_or_default();
    if ccxt_symbol.is_empty() && market.is_empty() {
        return (CCXT_SYMBOL.to_string(), MARKET_ID.to_string());
    }
    let market = if market.is_empty() {
        market_key(Some(&ccxt_symbol))
    } else {
        market
    };
    let ccxt_symbol = if ccxt_symbol.is_empty() {
        if market.ends_with("USDT") && market.len() > 4 {
            format!("{}/USDT:USDT", &market[..market.len() - 4])
        } else {
            CCXT_SYMBOL.to_string()
        }
    } else {
        ccxt_symbol
    };
    (ccxt_symbol, market)
}

/// Absolute position size from `contracts` or `info.positionAmt`.
pub fn position_qty(position: Option<&Value>) -> f64 {
    let Some(pos) = position else {
        return 0.0;
    };
    let contracts = pos.get("contracts");
    if let Some(v) = contracts {
        if !is_empty_value(v) {
            return py_f(Some(v), 0.0).abs();
        }
    }
    let amt = pos.get("info").and_then(|i| i.get("positionAmt"));
    if let Some(v) = amt {
        if !is_empty_value(v) {
            return py_f(Some(v), 0.0).abs();
        }
    }
    0.0
}

/// `LONG` / `SHORT` / "" from the position side or amount sign.
pub fn position_side(position: Option<&Value>) -> String {
    let side = position
        .and_then(|p| p.get("side"))
        .and_then(|v| v.as_str())
        .map(|s| s.to_lowercase())
        .unwrap_or_default();
    if side == "long" {
        return "LONG".to_string();
    }
    if side == "short" {
        return "SHORT".to_string();
    }
    let amount = position
        .and_then(|p| p.get("info"))
        .and_then(|i| i.get("positionAmt"))
        .map(|v| py_f(Some(v), 0.0))
        .unwrap_or(0.0);
    if amount > 0.0 {
        "LONG".to_string()
    } else if amount < 0.0 {
        "SHORT".to_string()
    } else {
        String::new()
    }
}

/// Entry price from `entryPrice` or `info.entryPrice`.
pub fn position_entry(position: Option<&Value>) -> f64 {
    if let Some(p) = position {
        if let Some(v) = p.get("entryPrice") {
            if !is_empty_value(v) {
                return py_f(Some(v), 0.0);
            }
        }
        if let Some(v) = p.get("info").and_then(|i| i.get("entryPrice")) {
            return py_f(Some(v), 0.0);
        }
    }
    0.0
}

fn is_empty_value(v: &Value) -> bool {
    match v {
        Value::Null => true,
        Value::String(s) => s.is_empty(),
        _ => false,
    }
}

// ---------------------------------------------------------------------------
// Strategy identity
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Variant {
    BtcMa,
    MaPlus,
}

impl Variant {
    pub fn client_id(self) -> &'static str {
        match self {
            Variant::BtcMa => config_btc_ma::CLIENT_ID_STRATEGY,
            Variant::MaPlus => config_ma_plus::CLIENT_ID_STRATEGY,
        }
    }

    pub fn runtime_subdir(self) -> &'static str {
        match self {
            Variant::BtcMa => "btc_ma_cross",
            Variant::MaPlus => "ma_cross",
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Variant::BtcMa => "3/200 SMA",
            Variant::MaPlus => "SMA3/120 C",
        }
    }

    pub fn protection_kinds(self) -> &'static [&'static str] {
        match self {
            Variant::BtcMa => &["sl", "lock"],
            Variant::MaPlus => &["lock"],
        }
    }

    pub fn exit_reason_for_kind(self, kind: &str) -> Option<&'static str> {
        match (self, kind) {
            (Variant::BtcMa, "sl") => Some("EMERGENCY_STOP"),
            (Variant::BtcMa, "lock") | (Variant::MaPlus, "lock") => Some("PROFIT_LOCK"),
            _ => None,
        }
    }

    pub fn strategy_version(self) -> &'static str {
        match self {
            Variant::BtcMa => config_btc_ma::STRATEGY_VERSION,
            Variant::MaPlus => config_ma_plus::STRATEGY_VERSION,
        }
    }

    pub fn normalize(self, raw: &Value) -> Value {
        match self {
            Variant::BtcMa => config_btc_ma::normalize_btc_ma_cross_config(Some(raw)),
            Variant::MaPlus => config_ma_plus::normalize_ma_cross_config(Some(raw)),
        }
    }
}

// ---------------------------------------------------------------------------
// Dependency bundle
// ---------------------------------------------------------------------------

pub struct ServiceDeps<E> {
    pub config_getter: Arc<dyn Fn() -> Value + Send + Sync>,
    pub credentials_getter: Arc<dyn Fn(&str) -> Value + Send + Sync>,
    pub exchange_factory: Arc<dyn Fn(&str, &Value) -> Result<Arc<E>, ExchangeError> + Send + Sync>,
    /// Parent path; the service appends `variant.runtime_subdir()`.
    pub runtime_dir: PathBuf,
    pub notifier: Option<Arc<dyn Fn(String) -> BoxFut<()> + Send + Sync>>,
    pub main_network_getter: Option<Arc<dyn Fn() -> Option<String> + Send + Sync>>,
    /// Seconds since the epoch.
    pub clock: Arc<dyn Fn() -> f64 + Send + Sync>,
    pub sleep: Arc<dyn Fn(f64) -> BoxFut<()> + Send + Sync>,
    pub environ: HashMap<String, String>,
    /// Implemented as the `set_entry_mode` method.
    pub config_writer: Option<Arc<dyn Fn(String) -> BoxFut<bool> + Send + Sync>>,
    /// Timestamps used by the ledger/order store.
    pub store_clock: Arc<dyn Clock>,
}

// ---------------------------------------------------------------------------
// Service struct
// ---------------------------------------------------------------------------

pub struct SmaCrossService<E: FuturesExchange + 'static> {
    pub variant: Variant,
    pub deps: ServiceDeps<E>,
    pub runtime_dir: PathBuf,
    exchanges: Mutex<HashMap<String, Arc<E>>>,
    ledgers: Mutex<HashMap<String, Arc<Mutex<StrategyLedger>>>>,
    stores: Mutex<HashMap<String, Arc<Mutex<SQLiteTradingStateStore>>>>,
    gateways: tokio::sync::Mutex<
        HashMap<
            String,
            (
                Arc<IdempotentOrderGateway<E>>,
                Arc<BinanceAlgoOrderGateway<E>>,
            ),
        >,
    >,
    /// `_rules[key] = (now_ms, payload_or_rules)` cache, keyed as Python.
    rules: Mutex<HashMap<String, (i64, Value)>>,
    /// Last verified `account_setup` timestamp (ms) per `network:market`.
    account_verified: Mutex<HashMap<String, Value>>,
    cycle_lock: tokio::sync::Mutex<()>,
    /// Deduplicated notifier text per key.
    last_notice: Mutex<HashMap<String, String>>,
    /// Last stale-record sweep time (ms) per network.
    last_stale_check: Mutex<HashMap<String, i64>>,
    /// `variant` naming keeps later stages honest: it is a `(now_ms, list, count)`.
    pub(crate) universe_cache: Mutex<HashMap<String, Value>>,
}

impl<E: FuturesExchange + 'static> SmaCrossService<E> {
    pub fn new(variant: Variant, deps: ServiceDeps<E>) -> Self {
        let runtime_dir = deps.runtime_dir.join(variant.runtime_subdir());
        Self {
            variant,
            deps,
            runtime_dir,
            exchanges: Mutex::new(HashMap::new()),
            ledgers: Mutex::new(HashMap::new()),
            stores: Mutex::new(HashMap::new()),
            gateways: tokio::sync::Mutex::new(HashMap::new()),
            rules: Mutex::new(HashMap::new()),
            account_verified: Mutex::new(HashMap::new()),
            cycle_lock: tokio::sync::Mutex::new(()),
            last_notice: Mutex::new(HashMap::new()),
            last_stale_check: Mutex::new(HashMap::new()),
            universe_cache: Mutex::new(HashMap::new()),
        }
    }

    pub(crate) fn client_id(&self) -> &'static str {
        self.variant.client_id()
    }

    pub(crate) fn label(&self) -> &'static str {
        self.variant.label()
    }

    pub(crate) fn cycle_lock(&self) -> &tokio::sync::Mutex<()> {
        &self.cycle_lock
    }
}

// Suppress unused warning for the Map import when later stages haven't needed it yet.
#[allow(dead_code)]
fn _assert_map_type(_m: Option<&Map<String, Value>>) {}

/// `decision["rules"]` holds a serialized `TradingRules` (Python kept the object).
pub fn rules_from_value(value: &Value) -> Result<crate::core::rules::TradingRules, ServiceError> {
    serde_json::from_value(value.clone())
        .map_err(|e| ServiceError::new("ValueError", format!("invalid trading rules: {e}")))
}
