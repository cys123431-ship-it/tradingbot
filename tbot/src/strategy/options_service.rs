use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use chrono::{TimeZone, Utc};
use serde_json::{json, Map, Value};
use tokio::sync::Mutex as AsyncMutex;

use crate::app::ipc::BoxFut;
use crate::app::telegram_bot::format_g;
use crate::core::candles::Candle;
use crate::core::options_strategy as st;
use crate::exchange::binance_options::{OptionOrder, OptionsApi, OptionsApiError, OrderRef};
use crate::store::clock::{py_isoformat, Clock};
use crate::strategy::options_config::{
    normalize_options_config, options_budget_text, options_spend_limit, py_truthy,
    OPTIONS_CAPITAL_LIMIT_USDT,
};

pub const BOT_CLIENT_PREFIX: &str = "tbopt";
pub const LEGACY_OPTIONS_CAPITAL_LIMIT_USDT: f64 = 20.0;

/// adaptive SCAN_OUTCOME_LABELS (code, label) in Python order; BUDGET label =
/// "옵션 지갑 잔고/최소수량 문제".
pub const SCAN_OUTCOME_LABELS: [(&str, &str); 15] = [
    ("DIRECTION_SIGNAL", "방향 신호 부족"),
    ("DTE", "DTE 부적합"),
    ("DELTA", "Delta 부적합"),
    ("IV", "IV/IV-RV·표면 과열"),
    ("EDGE", "비용 차감 후 기대수익 부족"),
    ("FLOW", "옵션 매수 흐름 반대"),
    ("SPREAD", "Spread 초과"),
    ("LIQUIDITY", "유동성 부족"),
    ("BUDGET", "옵션 지갑 잔고/최소수량 문제"),
    ("EXISTING_POSITION", "기존 옵션 포지션"),
    ("OPEN_ORDER", "기존 미체결 옵션 주문"),
    ("CAN_TRADE", "옵션 거래 권한/API 상태"),
    ("API", "API/시장데이터 오류"),
    ("OTHER", "기타 fail-closed 조건"),
    ("ORDERABLE_CANDIDATE", "주문 가능한 후보 발견"),
];

pub type Notifier = Arc<dyn Fn(String) -> BoxFut<()> + Send + Sync>;
pub type Sleeper = Arc<dyn Fn(f64) -> BoxFut<()> + Send + Sync>;
pub type ConfigGetter = Arc<dyn Fn() -> Value + Send + Sync>;

/// Internal error type mirroring Python exceptions.
#[derive(Debug)]
pub(crate) enum CycleError {
    Api(OptionsApiError),
    #[allow(dead_code)] // Python's generic `except Exception` arm; kept for parity
    Other(String),
}

impl From<OptionsApiError> for CycleError {
    fn from(value: OptionsApiError) -> Self {
        CycleError::Api(value)
    }
}

impl std::fmt::Display for CycleError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CycleError::Api(e) => write!(f, "{}", e),
            CycleError::Other(m) => write!(f, "{}", m),
        }
    }
}

pub(crate) fn log_line(msg: &str) {
    let now = Utc::now();
    eprintln!("{} options: {}", py_isoformat(&now), msg);
}

/// Python `_rows`.
pub(crate) fn py_rows(payload: &Value) -> Vec<Value> {
    if let Value::Array(items) = payload {
        return items.clone();
    }
    if let Value::Object(map) = payload {
        for key in ["data", "rows", "positions", "orders", "assets", "asset"] {
            if let Some(Value::Array(items)) = map.get(key) {
                return items.clone();
            }
        }
    }
    Vec::new()
}

/// Python `_first`.
pub(crate) fn py_first(payload: &Value) -> Value {
    let rows = py_rows(payload);
    if !rows.is_empty() {
        if rows[0].is_object() {
            return rows[0].clone();
        }
        return json!({});
    }
    if payload.is_object() {
        return payload.clone();
    }
    json!({})
}

/// Python `_profit_reference_price`.
pub(crate) fn profit_reference_price(cfg: &Value, mark_price: f64, bids: &Value) -> f64 {
    if py_truthy(cfg.get("profit_trigger_on_bid").unwrap_or(&Value::Null)) {
        let bid_rows = bids.as_array().cloned().unwrap_or_default();
        if !bid_rows.is_empty() {
            let mut bid = 0.0f64;
            if let Some(first) = bid_rows[0].as_array() {
                if let Some(inner) = first.first() {
                    bid = st::pyf(Some(inner), 0.0);
                }
            }
            if bid > 0.0 {
                return mark_price.min(bid);
            }
        }
    }
    mark_price
}

/// Python `_realized_pnl`.
pub(crate) fn realized_pnl(state: &Map<String, Value>) -> f64 {
    let mut total = 0.0;
    if let Some(Value::Array(trades)) = state.get("trades") {
        for trade in trades {
            if let Value::Object(map) = trade {
                total += st::pyf(map.get("pnl_usdt"), 0.0);
            }
        }
    }
    total
}

/// Python `_position_quantity`.
pub(crate) fn position_quantity(position: &Value) -> f64 {
    if let Value::Object(map) = position {
        for key in ["quantity", "positionAmt", "position", "qty"] {
            if let Some(value) = map.get(key) {
                return st::pyf(Some(value), 0.0);
            }
        }
    }
    0.0
}

/// Python `_order_executed_quantity`.
pub(crate) fn order_executed_quantity(order: &Value) -> f64 {
    if let Value::Object(map) = order {
        for key in [
            "executedQty",
            "executedQuantity",
            "cumQty",
            "filledQty",
            "quantity",
        ] {
            let value = st::pyf(map.get(key), 0.0);
            if value > 0.0 {
                if key == "quantity" {
                    let status = py_str(map.get("status")).to_uppercase();
                    if status != "FILLED" && status != "PARTIALLY_FILLED" {
                        continue;
                    }
                }
                return value;
            }
        }
    }
    0.0
}

/// Python `_order_average_price`.
pub(crate) fn order_average_price(order: &Value, fallback: f64) -> f64 {
    if let Value::Object(map) = order {
        for key in ["avgPrice", "averagePrice", "price"] {
            let value = st::pyf(map.get(key), 0.0);
            if value > 0.0 {
                return value;
            }
        }
    }
    st::pyf(Some(&json!(fallback)), 0.0)
}

/// Python `str(x)` for JSON values (numbers as plain text, null -> "None").
pub(crate) fn py_str(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => "None".to_string(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Bool(b)) => {
            if *b {
                "True".to_string()
            } else {
                "False".to_string()
            }
        }
        Some(Value::Number(n)) => n.to_string(),
        Some(other) => other.to_string(),
    }
}

/// Python `str(x or "")` for JSON values.
pub(crate) fn py_str_or_empty(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Bool(b)) => {
            if *b {
                "True".to_string()
            } else {
                "False".to_string()
            }
        }
        Some(Value::Number(n)) => n.to_string(),
        Some(other) => other.to_string(),
    }
}

/// Python `_option_balance`.
pub(crate) fn option_balance(account: &Value) -> Value {
    let mut scopes: Vec<Value> = Vec::new();
    if account.is_object() {
        scopes.push(account.clone());
    }
    scopes.extend(py_rows(account));
    let mut candidates: Vec<(f64, f64)> = Vec::new();
    for scope in scopes {
        let map = match scope.as_object() {
            Some(m) => m,
            None => continue,
        };
        let asset_raw = if let Some(v) = map.get("asset") {
            py_str_or_empty(Some(v))
        } else if let Some(v) = map.get("currency") {
            py_str_or_empty(Some(v))
        } else if let Some(v) = map.get("coin") {
            py_str_or_empty(Some(v))
        } else {
            String::new()
        };
        let asset = asset_raw.to_uppercase();
        if !asset.is_empty() && asset != "USDT" {
            continue;
        }
        let mut available: Option<f64> = None;
        for key in ["available", "availableBalance", "availableFunds", "free"] {
            if let Some(v) = map.get(key) {
                available = Some(st::pyf(Some(v), 0.0));
                break;
            }
        }
        let mut equity: Option<f64> = None;
        for key in ["equity", "marginBalance", "walletBalance", "balance"] {
            if let Some(v) = map.get(key) {
                equity = Some(st::pyf(Some(v), 0.0));
                break;
            }
        }
        if available.is_some() || equity.is_some() {
            let av = match available {
                Some(a) => a,
                None => {
                    let e = equity.unwrap_or(0.0);
                    if e != 0.0 {
                        e
                    } else {
                        0.0
                    }
                }
            };
            let eq = match equity {
                Some(e) => e,
                None => {
                    let a = available.unwrap_or(0.0);
                    if a != 0.0 {
                        a
                    } else {
                        0.0
                    }
                }
            };
            candidates.push((av, eq));
        }
    }
    match candidates.first() {
        Some((a, e)) => json!({"available": a, "equity": e}),
        None => json!({"available": 0.0, "equity": 0.0}),
    }
}

/// Python `_account_can_trade`.
pub(crate) fn account_can_trade_cfg(account: &Value) -> Option<bool> {
    if let Value::Object(map) = account {
        if let Some(v) = map.get("canTrade") {
            return Some(py_truthy(v));
        }
    }
    None
}

/// Python `_default_state`.
pub(crate) fn default_state(now_iso: &str) -> Map<String, Value> {
    let mut map = Map::new();
    map.insert("version".to_string(), json!(1));
    map.insert(
        "cash_bankroll_usdt".to_string(),
        json!(OPTIONS_CAPITAL_LIMIT_USDT),
    );
    map.insert("active_position".to_string(), Value::Null);
    map.insert("last_scan_ts".to_string(), json!(0.0));
    map.insert("last_manual_scan_ts".to_string(), json!(0.0));
    map.insert("last_manage_ts".to_string(), json!(0.0));
    map.insert("last_manage_success_ts".to_string(), json!(0.0));
    map.insert("manage_error_streak".to_string(), json!(0));
    map.insert("pending_entry".to_string(), Value::Null);
    map.insert("pending_exit".to_string(), Value::Null);
    map.insert(
        "last_reason".to_string(),
        json!("옵션 전략이 아직 실행되지 않았습니다."),
    );
    map.insert("last_error".to_string(), json!(""));
    map.insert("last_candidate".to_string(), Value::Null);
    map.insert("consumed_signal_keys".to_string(), json!([]));
    map.insert("trades".to_string(), json!([]));
    map.insert("updated_at".to_string(), json!(now_iso));
    map
}

/// Python `_format_price`.
pub(crate) fn format_price(value: f64, tick: f64) -> String {
    let tick_dec = if tick > 0.0 {
        st::dec(tick)
    } else {
        st::dec(0.0001)
    };
    let price = st::dec(value);
    let rounded = (price / tick_dec).trunc() * tick_dec;
    rounded.normalize().to_string()
}

static CLIENT_COUNTER: AtomicU32 = AtomicU32::new(0);

/// Python `_client_order_id`.
pub(crate) fn client_order_id(kind: &str, now_ms: i64) -> String {
    let counter = CLIENT_COUNTER.fetch_add(1, Ordering::Relaxed);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.subsec_nanos())
        .unwrap_or(0);
    let mixed = counter.wrapping_mul(2654435761).wrapping_add(nanos);
    let hex = mixed & 0xFFFF;
    let full = format!("{}{}{}{:04x}", BOT_CLIENT_PREFIX, kind, now_ms, hex);
    if full.len() > 32 {
        full[..32].to_string()
    } else {
        full
    }
}

/// Python `_scan_category`.
pub(crate) fn scan_category(reason: &str) -> &'static str {
    let text = reason.to_uppercase();
    if text.contains("DELTA") {
        return "DELTA";
    }
    if text.contains("IV") {
        return "IV";
    }
    if text.contains("EXPECTED_EDGE") {
        return "EDGE";
    }
    if text.contains("FLOW") {
        return "FLOW";
    }
    if text.contains("SPREAD") || text.contains("ORDERBOOK") {
        return "SPREAD";
    }
    if text.contains("VOLUME") || text.contains("DEPTH") || text.contains("LIQUID") {
        return "LIQUIDITY";
    }
    if text.contains("MINIMUM")
        || text.contains("HARD_CAP")
        || text.contains("BUDGET")
        || text.contains("PREMIUM_PLUS_FEE")
    {
        return "BUDGET";
    }
    if text.contains("DTE") || text.contains("EXPIR") {
        return "DTE";
    }
    "OTHER"
}

/// Python `_direction_wait_text`.
pub(crate) fn direction_wait_text(weak_signals: &[Value], cfg: &Value) -> String {
    let min_abs = st::pyf(cfg.get("min_abs_signal"), 0.46);
    let squeeze_min = st::pyf(cfg.get("squeeze_min_score"), 0.58);
    let mut parts: Vec<String> = Vec::new();
    for row in weak_signals.iter().take(3) {
        let underlying = py_str_or_empty(row.get("underlying")).replace("USDT", "");
        let fallback = st::pyf(row.get("score"), 0.0);
        let trend = st::pyf(row.get("trend_score"), fallback);
        let squeeze = st::pyf(row.get("squeeze_score"), 0.0).abs();
        parts.push(format!(
            "{} 추세 {:+.2} (기준 ±{:.2}) · 압축돌파 {:.2} (기준 {:.2})",
            underlying, trend, min_abs, squeeze, squeeze_min
        ));
    }
    format!("방향 신호 대기 — {}", parts.join(" / "))
}

/// Python `_dominant_rejection`.
pub(crate) fn dominant_rejection(rejections: &[String]) -> String {
    if rejections.is_empty() {
        return "OTHER".to_string();
    }
    let mut counts: HashMap<&str, i64> = HashMap::new();
    let mut order: Vec<&str> = Vec::new();
    for r in rejections {
        let entry = counts.entry(r.as_str()).or_insert(0);
        if *entry == 0 && !order.contains(&r.as_str()) {
            order.push(r.as_str());
        }
        *entry += 1;
    }
    let priority = [
        "BUDGET",
        "LIQUIDITY",
        "SPREAD",
        "EDGE",
        "FLOW",
        "IV",
        "DELTA",
        "DTE",
        "DIRECTION_SIGNAL",
        "API",
        "OTHER",
    ];
    let best_count = counts.values().copied().max().unwrap_or(0);
    for key in priority {
        if counts.get(key).copied() == Some(best_count) {
            return key.to_string();
        }
    }
    for key in &order {
        if counts.get(key).copied() == Some(best_count) {
            return key.to_string();
        }
    }
    "OTHER".to_string()
}

/// Python `_order_is_terminal`.
pub(crate) fn order_is_terminal(order: &Value) -> bool {
    let status = py_str_or_empty(order.get("status")).to_uppercase();
    matches!(
        status.as_str(),
        "FILLED" | "CANCELED" | "REJECTED" | "EXPIRED"
    )
}

pub struct OptionsService<C: OptionsApi> {
    pub(crate) cfg_getter: ConfigGetter,
    pub(crate) client: Arc<C>,
    pub(crate) state_path: PathBuf,
    pub(crate) notifier: Option<Notifier>,
    pub(crate) clock: Arc<dyn Clock>,
    pub(crate) sleeper: Sleeper,
    pub(crate) cycle_lock: AsyncMutex<()>,
    pub(crate) state: Mutex<Map<String, Value>>,
    pub(crate) state_unreadable: Mutex<bool>,
    pub(crate) exchange_info: Mutex<Option<(Value, f64)>>,
    pub(crate) ownership_recovery_done: Mutex<bool>,
    pub(crate) mark_cache: Mutex<HashMap<String, Value>>,
    pub(crate) ticker_cache: Mutex<HashMap<String, Value>>,
}

impl<C: OptionsApi> OptionsService<C> {
    /// Loads (and migrates/creates) the state file exactly like the Python constructor.
    pub fn new(
        cfg_getter: ConfigGetter,
        client: Arc<C>,
        state_path: PathBuf,
        notifier: Option<Notifier>,
        clock: Arc<dyn Clock>,
        sleeper: Sleeper,
    ) -> Self {
        let state = Mutex::new(Map::new());
        let state_unreadable = Mutex::new(false);
        let exchange_info = Mutex::new(None);
        let ownership_recovery_done = Mutex::new(false);
        let mark_cache = Mutex::new(HashMap::new());
        let ticker_cache = Mutex::new(HashMap::new());
        let service = OptionsService {
            cfg_getter,
            client,
            state_path,
            notifier,
            clock,
            sleeper,
            cycle_lock: AsyncMutex::new(()),
            state,
            state_unreadable,
            exchange_info,
            ownership_recovery_done,
            mark_cache,
            ticker_cache,
        };
        let loaded = service.load_state_migrated();
        if let Ok(mut guard) = service.state.lock() {
            *guard = loaded;
        }
        service
    }

    /// Python `config()`.
    pub fn config(&self) -> Value {
        let raw = (self.cfg_getter)();
        normalize_options_config(&raw)
    }

    /// A clone of the current state map.
    pub fn state(&self) -> Map<String, Value> {
        self.state.lock().map(|g| g.clone()).unwrap_or_default()
    }

    pub fn state_unreadable(&self) -> bool {
        self.state_unreadable.lock().map(|g| *g).unwrap_or(false)
    }

    pub fn client(&self) -> &Arc<C> {
        &self.client
    }

    pub(crate) fn st_get(&self, key: &str) -> Value {
        self.state
            .lock()
            .map(|g| g.get(key).cloned().unwrap_or(Value::Null))
            .unwrap_or(Value::Null)
    }

    pub(crate) fn st_set(&self, key: &str, value: Value) {
        if let Ok(mut guard) = self.state.lock() {
            guard.insert(key.to_string(), value);
        }
    }

    pub(crate) fn now_secs(&self) -> f64 {
        self.clock.now_epoch_secs()
    }

    pub(crate) fn now_ms(&self) -> i64 {
        (self.now_secs() * 1000.0) as i64
    }

    pub(crate) fn now_iso(&self) -> String {
        py_isoformat(&self.clock.now_utc())
    }

    pub(crate) fn load_state_migrated(&self) -> Map<String, Value> {
        // service layer: capture existence before base load
        let existed_before_load = self.state_path.exists();
        let mut state = self.load_state_base_and_adaptive();
        if self.state_unreadable() {
            return state;
        }
        let current_limit = OPTIONS_CAPITAL_LIMIT_USDT;
        let recorded = state.get("capital_limit_usdt").cloned();
        let previous_limit = match &recorded {
            None | Some(Value::Null) => {
                if existed_before_load {
                    LEGACY_OPTIONS_CAPITAL_LIMIT_USDT
                } else {
                    current_limit
                }
            }
            Some(v) => st::pyf(Some(v), current_limit).max(0.0),
        };
        let bankroll_raw = st::pyf(state.get("cash_bankroll_usdt"), previous_limit).max(0.0);
        let mut bankroll = bankroll_raw;
        if current_limit > previous_limit {
            bankroll += current_limit - previous_limit;
        } else if current_limit < previous_limit {
            bankroll = (bankroll - (previous_limit - current_limit)).max(0.0);
        }
        state.insert("cash_bankroll_usdt".to_string(), json!(bankroll));
        state.insert("capital_limit_usdt".to_string(), json!(current_limit));
        let _ = self.save_state_map(&mut state);
        state
    }

    pub(crate) fn load_state_base_and_adaptive(&self) -> Map<String, Value> {
        let mut state = self.load_state_base();
        // adaptive layer
        let has_recent = matches!(state.get("recent_scan_outcomes"), Some(Value::Array(_)));
        if !has_recent {
            state.insert("recent_scan_outcomes".to_string(), json!([]));
        }
        if !state.contains_key("last_scan_diagnostics") {
            state.insert("last_scan_diagnostics".to_string(), json!({}));
        }
        state
    }

    pub(crate) fn load_state_base(&self) -> Map<String, Value> {
        if !self.state_path.exists() {
            let mut state = default_state(&self.now_iso());
            let _ = self.save_state_map(&mut state);
            return state;
        }
        let raw = std::fs::read_to_string(&self.state_path);
        let text = match raw {
            Ok(t) => t,
            Err(e) => return self.state_unreadable_fallback(&format!("{}", e)),
        };
        let parsed: Result<Value, _> = serde_json::from_str(&text);
        let value = match parsed {
            Ok(v) => v,
            Err(e) => return self.state_unreadable_fallback(&format!("{}", e)),
        };
        let mut map = match value {
            Value::Object(m) => m,
            _ => return self.state_unreadable_fallback("unsupported options state"),
        };
        let version = st::pyf(map.get("version"), 0.0) as i64;
        if version != 1 {
            return self.state_unreadable_fallback("unsupported options state");
        }
        // defaults identical to base-load setdefaults
        let bankroll = st::pyf(map.get("cash_bankroll_usdt"), OPTIONS_CAPITAL_LIMIT_USDT).max(0.0);
        map.insert("cash_bankroll_usdt".to_string(), json!(bankroll));
        for (key, default) in [
            ("active_position", Value::Null),
            ("trades", json!([])),
            ("last_reason", json!("")),
            ("last_error", json!("")),
            ("last_candidate", Value::Null),
            ("consumed_signal_keys", json!([])),
            ("last_manual_scan_ts", json!(0.0)),
            ("last_manage_success_ts", json!(0.0)),
            ("manage_error_streak", json!(0)),
            ("pending_entry", Value::Null),
            ("pending_exit", Value::Null),
            ("last_scan_ts", json!(0.0)),
            ("last_manage_ts", json!(0.0)),
        ] {
            if !map.contains_key(key) {
                map.insert(key.to_string(), default);
            }
        }
        map
    }

    pub(crate) fn state_unreadable_fallback(&self, err: &str) -> Map<String, Value> {
        if let Ok(mut guard) = self.state_unreadable.lock() {
            *guard = true;
        }
        log_line(&format!(
            "Options state is unreadable; trading fail-closed: {}",
            err
        ));
        let mut state = default_state(&self.now_iso());
        state.insert(
            "last_error".to_string(),
            json!(format!("OPTIONS_STATE_UNREADABLE: {}", err)),
        );
        state
    }

    /// Python `_save_state`.
    pub(crate) fn save_state(&self) -> Result<(), String> {
        // Deliberate hardening over Python: never replace an unreadable state file
        // with the in-memory default (it may describe a live bot position).
        if self.state_unreadable() {
            return Err("OPTIONS_STATE_UNREADABLE: not overwriting the state file".to_string());
        }
        let mut guard = match self.state.lock() {
            Ok(g) => g,
            Err(_) => return Err("state poisoned".to_string()),
        };
        self.save_state_map(&mut guard)
    }

    pub(crate) fn save_state_map(&self, state: &mut Map<String, Value>) -> Result<(), String> {
        state.insert("updated_at".to_string(), json!(self.now_iso()));
        let dir = match self.state_path.parent() {
            Some(d) => d.to_path_buf(),
            None => PathBuf::from("."),
        };
        if let Err(e) = std::fs::create_dir_all(&dir) {
            log_line(&format!("Options save_state mkdir failed: {}", e));
            return Err(e.to_string());
        }
        let pid = std::process::id();
        let n = CLIENT_COUNTER.fetch_add(1, Ordering::Relaxed);
        let temp_path = dir.join(format!(".options_state_{}_{}.tmp", pid, n));
        let text = match serde_json::to_string_pretty(&Value::Object(state.clone())) {
            Ok(s) => s,
            Err(e) => {
                log_line(&format!("Options save_state serialize failed: {}", e));
                return Err(e.to_string());
            }
        };
        let mut file = match std::fs::File::create(&temp_path) {
            Ok(f) => f,
            Err(e) => {
                log_line(&format!("Options save_state create failed: {}", e));
                return Err(e.to_string());
            }
        };
        use std::io::Write;
        if let Err(e) = file
            .write_all(text.as_bytes())
            .and_then(|_| file.write_all(b"\n"))
        {
            log_line(&format!("Options save_state write failed: {}", e));
            let _ = std::fs::remove_file(&temp_path);
            return Err(e.to_string());
        }
        if let Err(e) = file.flush().and_then(|_| file.sync_all()) {
            log_line(&format!("Options save_state sync failed: {}", e));
            let _ = std::fs::remove_file(&temp_path);
            return Err(e.to_string());
        }
        drop(file);
        if let Err(e) = std::fs::rename(&temp_path, &self.state_path) {
            log_line(&format!("Options save_state rename failed: {}", e));
            let _ = std::fs::remove_file(&temp_path);
            return Err(e.to_string());
        }
        Ok(())
    }

    pub(crate) async fn notify(&self, text: &str) {
        if let Some(notifier) = &self.notifier {
            let fut = notifier(text.to_string());
            fut.await;
        }
    }

    pub(crate) async fn private_snapshot(&self) -> Result<Value, CycleError> {
        if !self.client.authenticated() {
            return Err(CycleError::Api(OptionsApiError::new(
                "OPTIONS_API_CREDENTIALS_MISSING",
            )));
        }
        self.client.sync_time().await?;
        let account_fut = self.client.margin_account();
        let positions_fut = self.client.positions(None);
        let orders_fut = self.client.open_orders(None);
        let (account_res, positions_res, orders_res) =
            futures::join!(account_fut, positions_fut, orders_fut);
        let account = account_res?;
        let positions = positions_res?;
        let orders = orders_res?;
        let mut filtered: Vec<Value> = Vec::new();
        for row in py_rows(&positions) {
            if position_quantity(&row).abs() > 1e-12 {
                filtered.push(row);
            }
        }
        Ok(json!({
            "account": account,
            "balance": option_balance(&account),
            "can_trade": account_can_trade_cfg(&account),
            "positions": filtered,
            "orders": py_rows(&orders),
        }))
    }

    pub async fn preflight(&self) -> Value {
        let ping = self.client.ping().await;
        if let Err(e) = ping {
            return json!({
                "ok": false,
                "account": {},
                "balance": {"available": 0.0, "equity": 0.0},
                "positions": [],
                "orders": [],
                "error": format!("{}", e),
            });
        }
        match self.private_snapshot().await {
            Ok(snapshot) => {
                let account = snapshot.get("account").cloned().unwrap_or(json!({}));
                let balance = snapshot.get("balance").cloned().unwrap_or(json!({}));
                let can_trade = snapshot.get("can_trade").cloned().unwrap_or(Value::Null);
                let positions = snapshot.get("positions").cloned().unwrap_or(json!([]));
                let orders = snapshot.get("orders").cloned().unwrap_or(json!([]));
                json!({
                    "ok": true,
                    "account": account,
                    "balance": balance,
                    "can_trade": can_trade,
                    "positions": positions,
                    "orders": orders,
                    "error": "",
                })
            }
            Err(e) => json!({
                "ok": false,
                "account": {},
                "balance": {"available": 0.0, "equity": 0.0},
                "positions": [],
                "orders": [],
                "error": format!("{}", e),
            }),
        }
    }

    /// Python `_sync_scan_universe` (adaptive). Returns true when reset.
    pub(crate) fn sync_scan_universe(&self) -> bool {
        let cfg = self.config();
        let mut universe: Vec<String> = Vec::new();
        if let Some(Value::Array(items)) = cfg.get("underlyings") {
            for item in items {
                universe.push(py_str_or_empty(Some(item)));
            }
        }
        universe.sort();
        let universe_value = json!(universe);
        let existing = self.st_get("scan_universe");
        if existing == universe_value {
            return false;
        }
        {
            if let Ok(mut guard) = self.state.lock() {
                guard.insert("scan_universe".to_string(), universe_value);
                guard.insert("recent_scan_outcomes".to_string(), json!([]));
                guard.insert("last_scan_diagnostics".to_string(), json!({}));
                guard.insert("last_candidate".to_string(), Value::Null);
            }
        }
        let _ = self.save_state();
        true
    }

    /// Python `_record_scan_outcome` (adaptive).
    pub(crate) fn record_scan_outcome(&self, category: &str, diagnostics: Option<Value>) {
        self.sync_scan_universe();
        let cat = if SCAN_OUTCOME_LABELS.iter().any(|(c, _)| *c == category) {
            category.to_string()
        } else {
            "OTHER".to_string()
        };
        let cfg = self.config();
        let window_raw = st::pyf(cfg.get("rejection_stats_window"), 100.0);
        let window = if window_raw <= 0.0 {
            100
        } else {
            window_raw as i64
        };
        let mut history: Vec<Value> = match self.st_get("recent_scan_outcomes") {
            Value::Array(v) => v,
            _ => Vec::new(),
        };
        history.push(json!(cat));
        let len = history.len();
        let start = if (len as i64) > window {
            len - window as usize
        } else {
            0
        };
        let trimmed: Vec<Value> = history[start..].to_vec();
        {
            if let Ok(mut guard) = self.state.lock() {
                guard.insert("recent_scan_outcomes".to_string(), json!(trimmed));
                if let Some(d) = diagnostics {
                    guard.insert("last_scan_diagnostics".to_string(), d);
                }
            }
        }
        let _ = self.save_state();
    }

    pub(crate) fn record_reason(
        &self,
        reason: &str,
        error: bool,
        candidate: Option<&Value>,
    ) -> Value {
        {
            if let Ok(mut guard) = self.state.lock() {
                guard.insert("last_reason".to_string(), json!(reason));
                if error {
                    guard.insert("last_error".to_string(), json!(reason));
                } else if let Some(c) = candidate {
                    if !c.is_null() {
                        guard.insert("last_candidate".to_string(), c.clone());
                        guard.insert("last_error".to_string(), json!(""));
                    }
                }
            }
        }
        let _ = self.save_state();
        json!({
            "action": if error { "blocked" } else { "waiting" },
            "reason": reason,
        })
    }

    pub(crate) async fn get_exchange_info(&self) -> Result<Value, CycleError> {
        let now = self.now_secs();
        {
            if let Ok(guard) = self.exchange_info.lock() {
                if let Some((payload, ts)) = guard.as_ref() {
                    if now - *ts < 600.0 {
                        return Ok(payload.clone());
                    }
                }
            }
        }
        let payload = self.client.exchange_info().await?;
        if let Ok(mut guard) = self.exchange_info.lock() {
            *guard = Some((payload.clone(), now));
        }
        Ok(payload)
    }

    pub(crate) async fn underlying_signal(
        &self,
        underlying: &str,
        cfg: &Value,
    ) -> Result<Value, CycleError> {
        let fast_tf = match cfg.get("signal_timeframe") {
            Some(Value::String(s)) => s.clone(),
            _ => "1h".to_string(),
        };
        let slow_tf = match cfg.get("slow_timeframe") {
            Some(Value::String(s)) => s.clone(),
            _ => "4h".to_string(),
        };
        let fast_fut = self.client.underlying_klines(underlying, &fast_tf, 240);
        let slow_fut = self.client.underlying_klines(underlying, &slow_tf, 160);
        let (fast_res, slow_res) = futures::join!(fast_fut, slow_fut);
        let fast: Vec<Candle> = fast_res?;
        let slow: Vec<Candle> = slow_res?;
        let fast_use: Vec<Candle> = if fast.len() > 1 {
            fast[..fast.len() - 1].to_vec()
        } else {
            fast
        };
        let slow_use: Vec<Candle> = if slow.len() > 1 {
            slow[..slow.len() - 1].to_vec()
        } else {
            slow
        };
        let mut result = st::choose_underlying_signal(&fast_use, &slow_use, cfg);
        if let Value::Object(map) = &mut result {
            map.insert("underlying".to_string(), json!(underlying));
            let direction = py_str_or_empty(map.get("direction"));
            let sig_ts = st::pyf(map.get("signal_bar_ts"), 0.0) as i64;
            let strategy = py_str_or_empty(map.get("strategy"));
            map.insert(
                "signal_key".to_string(),
                json!(format!(
                    "{}:{}:{}:{}",
                    underlying, strategy, direction, sig_ts
                )),
            );
        }
        Ok(result)
    }

    pub(crate) async fn cached_mark(&self, symbol: &str) -> Result<Value, CycleError> {
        {
            if let Ok(guard) = self.mark_cache.lock() {
                if let Some(v) = guard.get(symbol) {
                    return Ok(v.clone());
                }
            }
        }
        let payload = self.client.mark_price(Some(symbol)).await?;
        if let Ok(mut guard) = self.mark_cache.lock() {
            guard.insert(symbol.to_string(), payload.clone());
        }
        Ok(payload)
    }

    pub(crate) async fn cached_ticker(&self, symbol: &str) -> Result<Value, CycleError> {
        {
            if let Ok(guard) = self.ticker_cache.lock() {
                if let Some(v) = guard.get(symbol) {
                    return Ok(v.clone());
                }
            }
        }
        let payload = self.client.ticker(Some(symbol)).await?;
        if let Ok(mut guard) = self.ticker_cache.lock() {
            guard.insert(symbol.to_string(), payload.clone());
        }
        Ok(payload)
    }

    pub(crate) async fn prime_market_caches(&self) {
        let marks_fut = self.client.mark_price(None);
        let tickers_fut = self.client.ticker(None);
        let (marks_res, tickers_res) = futures::join!(marks_fut, tickers_fut);
        let marks = match marks_res {
            Ok(v) => v,
            Err(e) => {
                log_line(&format!("Options batch quote preload unavailable: {}", e));
                return;
            }
        };
        let tickers = match tickers_res {
            Ok(v) => v,
            Err(e) => {
                log_line(&format!("Options batch quote preload unavailable: {}", e));
                return;
            }
        };
        if let Ok(mut guard) = self.mark_cache.lock() {
            for row in py_rows(&marks) {
                if let Value::Object(map) = &row {
                    let symbol = py_str_or_empty(map.get("symbol"));
                    if !symbol.is_empty() {
                        guard.insert(symbol, json!([row]));
                    }
                }
            }
        }
        if let Ok(mut guard) = self.ticker_cache.lock() {
            for row in py_rows(&tickers) {
                if let Value::Object(map) = &row {
                    let symbol = py_str_or_empty(map.get("symbol"));
                    if !symbol.is_empty() {
                        guard.insert(symbol, json!([row]));
                    }
                }
            }
        }
    }

    pub(crate) async fn scan_contract_adaptive(
        &self,
        contract: &Value,
        signal: &Value,
        cfg: &Value,
        snapshot: &Value,
        exchange_info: &Value,
    ) -> Result<(Option<Value>, Option<String>, String), CycleError> {
        let symbol = py_str_or_empty(contract.get("symbol"));
        let symbol_for_mark = symbol.clone();
        let symbol_for_ticker = symbol.clone();
        let symbol_for_depth = symbol.clone();
        let mark_fut = self.cached_mark(&symbol_for_mark);
        let ticker_fut = self.cached_ticker(&symbol_for_ticker);
        let depth_fut = self.client.depth(&symbol_for_depth, 20);
        let (mark_res, ticker_res, depth_res) = futures::join!(mark_fut, ticker_fut, depth_fut);
        let mark = mark_res?;
        let ticker = ticker_res?;
        let depth = depth_res?;
        let recent_trades = match self.client.recent_trades(&symbol, 100).await {
            Ok(v) => v,
            Err(e) => {
                log_line(&format!(
                    "Option recent trades unavailable for {}: {}",
                    symbol, e
                ));
                json!([])
            }
        };
        let peer = st::find_skew_peer(exchange_info, contract);
        let mut peer_mark: Option<Value> = None;
        if let Some(peer_val) = &peer {
            let peer_symbol = py_str_or_empty(peer_val.get("symbol"));
            if !peer_symbol.is_empty() {
                match self.cached_mark(&peer_symbol).await {
                    Ok(v) => peer_mark = Some(v),
                    Err(e) => {
                        log_line(&format!(
                            "Option skew peer mark unavailable for {}: {}",
                            symbol, e
                        ));
                    }
                }
            }
        }
        let surface_peers = st::find_surface_peers(exchange_info, contract, 4);
        let mut surface_marks: Vec<Value> = Vec::new();
        if !surface_peers.is_empty() {
            let peer_symbols: Vec<String> = surface_peers
                .iter()
                .map(|row| py_str_or_empty(row.get("symbol")))
                .collect();
            let mut futs = Vec::new();
            for peer_symbol in &peer_symbols {
                futs.push(self.cached_mark(peer_symbol));
            }
            let results = futures::future::join_all(futs).await;
            surface_marks.extend(results.into_iter().flatten());
        }
        let scored = st::score_option_contract(
            contract,
            &mark,
            &ticker,
            &depth,
            signal,
            cfg,
            peer_mark.as_ref(),
            &surface_marks,
            &recent_trades,
        );
        let accepted = py_truthy(scored.get("accepted").unwrap_or(&Value::Null));
        if !accepted {
            let reason = py_str_or_empty(scored.get("reason"));
            let category = scan_category(&reason);
            return Ok((None, Some(category.to_string()), reason));
        }
        let ask = st::pyf(scored.get("ask"), 0.0);
        let tick = st::pyf(contract.get("tick_size"), 0.0);
        let limit_price = ask + (if tick > 0.0 { tick } else { 0.0 });
        let available = st::pyf(
            snapshot.get("balance").and_then(|b| b.get("available")),
            0.0,
        )
        .max(0.0);
        let bankroll = options_spend_limit(cfg, available);
        let spot_price = signal.get("spot_price").cloned().unwrap_or(Value::Null);
        let unit = contract.get("unit").cloned().unwrap_or(json!(1));
        let unit_f = st::pyf(Some(&unit), 1.0);
        let min_qty = contract
            .get("min_qty")
            .filter(|v| py_truthy(v))
            .or_else(|| contract.get("minQty"))
            .cloned();
        let step_size = contract
            .get("step_size")
            .filter(|v| py_truthy(v))
            .or_else(|| contract.get("minQty"))
            .cloned();
        let min_qty_f = st::pyf(min_qty.as_ref(), 0.0);
        let step_size_f = st::pyf(step_size.as_ref(), 0.0);
        let entry_fraction = st::pyf(cfg.get("entry_fraction"), 1.00)
            .min(st::pyf(scored.get("entry_fraction"), 1.00));
        let cap = st::pyf(cfg.get("capital_limit_usdt"), 0.0);
        let _ = unit_f;
        let plan = st::build_long_option_entry_plan(
            limit_price,
            st::pyf(Some(&spot_price), 0.0),
            unit_f,
            min_qty_f,
            step_size_f,
            bankroll,
            entry_fraction,
            cap,
        );
        if !py_truthy(plan.get("accepted").unwrap_or(&Value::Null)) {
            return Ok((
                None,
                Some("BUDGET".to_string()),
                py_str_or_empty(plan.get("reason")),
            ));
        }
        let plan_cost = st::pyf(plan.get("total_entry_cost_usdt"), 0.0);
        if plan_cost > available + 1e-9 {
            return Ok((
                None,
                Some("BUDGET".to_string()),
                "OPTION_AVAILABLE_BALANCE_BELOW_PLAN".to_string(),
            ));
        }
        let mut candidate = Map::new();
        if let Value::Object(m) = contract {
            for (k, v) in m {
                candidate.insert(k.clone(), v.clone());
            }
        }
        if let Value::Object(m) = &scored {
            for (k, v) in m {
                candidate.insert(k.clone(), v.clone());
            }
        }
        candidate.insert("signal".to_string(), signal.clone());
        candidate.insert("entry_plan".to_string(), plan);
        Ok((
            Some(Value::Object(candidate)),
            None,
            "OPTION_ORDERABLE".to_string(),
        ))
    }

    pub(crate) async fn scan_and_maybe_enter(&self) -> Value {
        let cfg = self.config();
        {
            if let Ok(mut guard) = self.mark_cache.lock() {
                guard.clear();
            }
            if let Ok(mut guard) = self.ticker_cache.lock() {
                guard.clear();
            }
        }
        let mut diagnostics = json!({
            "signal_rejections": {},
            "contract_rejections": {},
            "orderable_candidates": 0,
        });
        let snapshot = match self.private_snapshot().await {
            Ok(s) => s,
            Err(CycleError::Api(e)) => {
                self.record_scan_outcome("API", Some(diagnostics.clone()));
                return self.record_reason(&format!("옵션 API 진입 점검 실패: {}", e), true, None);
            }
            Err(CycleError::Other(m)) => {
                log_line("Adaptive options scan cycle failed");
                self.record_scan_outcome("OTHER", Some(diagnostics.clone()));
                return self.record_reason(&format!("옵션 스캔 오류: {}", m), true, None);
            }
        };
        if let Some(can_trade) = snapshot.get("can_trade") {
            if can_trade == &json!(false) {
                self.record_scan_outcome("CAN_TRADE", Some(diagnostics.clone()));
                return self.record_reason(
                    "옵션 계정 canTrade=false — European Options 거래 권한을 다시 확인하세요.",
                    true,
                    None,
                );
            }
        }
        let positions_nonempty = snapshot
            .get("positions")
            .and_then(|v| v.as_array())
            .map(|a| !a.is_empty())
            .unwrap_or(false);
        if positions_nonempty {
            self.record_scan_outcome("EXISTING_POSITION", Some(diagnostics.clone()));
            return self.record_reason(
                "기존 옵션 포지션이 있어 신규 진입을 차단했습니다. 봇 소유 포지션만 자동 관리합니다.",
                false,
                None,
            );
        }
        let orders_nonempty = snapshot
            .get("orders")
            .and_then(|v| v.as_array())
            .map(|a| !a.is_empty())
            .unwrap_or(false);
        if orders_nonempty {
            self.record_scan_outcome("OPEN_ORDER", Some(diagnostics.clone()));
            return self.record_reason(
                "기존 옵션 미체결 주문이 있어 신규 진입을 차단했습니다.",
                false,
                None,
            );
        }
        let available = st::pyf(
            (snapshot.get("balance")).and_then(|b| b.get("available")),
            0.0,
        );
        if available <= 0.0 {
            self.record_scan_outcome("BUDGET", Some(diagnostics.clone()));
            return self.record_reason("옵션 계좌 사용 가능 USDT가 없습니다.", true, None);
        }
        let exchange_info = match self.get_exchange_info().await {
            Ok(v) => v,
            Err(CycleError::Api(e)) => {
                self.record_scan_outcome("API", Some(diagnostics.clone()));
                return self.record_reason(&format!("옵션 API 진입 점검 실패: {}", e), true, None);
            }
            Err(CycleError::Other(m)) => {
                log_line("Adaptive options scan cycle failed");
                self.record_scan_outcome("OTHER", Some(diagnostics.clone()));
                return self.record_reason(&format!("옵션 스캔 오류: {}", m), true, None);
            }
        };
        let mut signals: Vec<Value> = Vec::new();
        let mut scan_rejections: Vec<String> = Vec::new();
        let mut weak_signals: Vec<Value> = Vec::new();
        let underlyings: Vec<String> = match cfg.get("underlyings") {
            Some(Value::Array(items)) => items.iter().map(|v| py_str_or_empty(Some(v))).collect(),
            _ => Vec::new(),
        };
        for underlying in underlyings {
            match self.underlying_signal(&underlying, &cfg).await {
                Ok(result) => {
                    if py_truthy(result.get("accepted").unwrap_or(&Value::Null)) {
                        signals.push(result);
                    } else {
                        scan_rejections.push("DIRECTION_SIGNAL".to_string());
                        let reason_raw = match result.get("reason") {
                            Some(Value::String(s)) if !s.is_empty() => s.clone(),
                            _ => "NO_ADAPTIVE_OPTION_SIGNAL".to_string(),
                        };
                        increment_diag(&mut diagnostics, "signal_rejections", &reason_raw);
                        weak_signals.push(result);
                    }
                }
                Err(e) => {
                    log_line(&format!(
                        "Options underlying signal failed for {}: {}",
                        underlying, e
                    ));
                    scan_rejections.push("API".to_string());
                    increment_diag(
                        &mut diagnostics,
                        "signal_rejections",
                        "UNDERLYING_DATA_ERROR",
                    );
                }
            }
        }
        signals.sort_by(|a, b| {
            let sa = st::pyf(a.get("score"), 0.0).abs();
            let sb = st::pyf(b.get("score"), 0.0).abs();
            sb.partial_cmp(&sa).unwrap_or(std::cmp::Ordering::Equal)
        });
        if signals.is_empty() && !weak_signals.is_empty() {
            let all_dir = scan_rejections.iter().all(|r| r == "DIRECTION_SIGNAL");
            if all_dir {
                self.record_scan_outcome("DIRECTION_SIGNAL", Some(diagnostics.clone()));
                return self.record_reason(&direction_wait_text(&weak_signals, &cfg), false, None);
            }
        }
        if !signals.is_empty() {
            self.prime_market_caches().await;
        }
        let mut candidates: Vec<Value> = Vec::new();
        let consumed: HashSet<String> = match self.st_get("consumed_signal_keys") {
            Value::Array(items) => items.iter().map(|v| py_str(Some(v))).collect(),
            _ => HashSet::new(),
        };
        let top_signals: Vec<Value> = signals.iter().take(3).cloned().collect();
        for signal in top_signals {
            let signal_key = py_str_or_empty(signal.get("signal_key"));
            if consumed.contains(&signal_key) {
                continue;
            }
            let underlying = signal.get("underlying").cloned().unwrap_or(Value::Null);
            let direction = signal.get("direction").cloned().unwrap_or(Value::Null);
            let spot = signal.get("spot_price").cloned().unwrap_or(Value::Null);
            let now_ms = self.now_ms();
            let (contracts, shortlist_diag) = st::shortlist_option_contracts_with_diagnostics(
                &exchange_info,
                &py_str_or_empty(Some(&underlying)),
                &py_str_or_empty(Some(&direction)),
                st::pyf(Some(&spot), 0.0),
                &signal,
                &cfg,
                now_ms,
            );
            if contracts.is_empty() {
                let dte = shortlist_diag.get("DTE").cloned().unwrap_or(json!(0));
                if st::pyf(Some(&dte), 0.0) > 0.0 {
                    scan_rejections.push("DTE".to_string());
                } else {
                    scan_rejections.push("OTHER".to_string());
                }
            }
            for (key, value) in &shortlist_diag {
                let v_count = st::pyf(Some(value), 0.0);
                if v_count != 0.0 {
                    increment_diag_count(
                        &mut diagnostics,
                        "contract_rejections",
                        &format!("SHORTLIST_{}", key),
                        v_count as i64,
                    );
                }
            }
            for contract in contracts {
                let contr_result = self
                    .scan_contract_adaptive(&contract, &signal, &cfg, &snapshot, &exchange_info)
                    .await;
                match contr_result {
                    Ok((Some(candidate), _, _)) => {
                        candidates.push(candidate);
                        increment_diag(&mut diagnostics, "orderable_candidates", "count");
                    }
                    Ok((None, category, raw_reason)) => {
                        let cat = category.unwrap_or_else(|| "OTHER".to_string());
                        scan_rejections.push(cat);
                        let key = if raw_reason.is_empty() {
                            "UNKNOWN".to_string()
                        } else {
                            raw_reason
                        };
                        increment_diag(&mut diagnostics, "contract_rejections", &key);
                    }
                    Err(e) => {
                        log_line(&format!(
                            "Option contract scan failed for {}: {}",
                            py_str_or_empty(contract.get("symbol")),
                            e
                        ));
                        scan_rejections.push("API".to_string());
                        increment_diag(
                            &mut diagnostics,
                            "contract_rejections",
                            "CONTRACT_DATA_ERROR",
                        );
                    }
                }
            }
        }
        if candidates.is_empty() {
            let outcome = dominant_rejection(&scan_rejections);
            self.record_scan_outcome(&outcome, Some(diagnostics.clone()));
            return self.record_reason(
                "Adaptive Convexity Trend 후보 중 옵션 지갑 잔고·순기대수익·DTE·Delta·IV표면·Spread·흐름·유동성 조건을 모두 만족한 계약이 없습니다.",
                false,
                None,
            );
        }
        candidates.sort_by(|a, b| {
            let sa = st::pyf(a.get("score"), 0.0);
            let sb = st::pyf(b.get("score"), 0.0);
            sb.partial_cmp(&sa).unwrap_or(std::cmp::Ordering::Equal)
        });
        let selected = candidates[0].clone();
        let signal = selected.get("signal").cloned().unwrap_or(json!({}));
        let plan = selected.get("entry_plan").cloned().unwrap_or(json!({}));
        let expiry_ms = st::pyf(selected.get("expiryDate"), 0.0) as i64;
        let summary = json!({
            "symbol": selected.get("symbol").cloned().unwrap_or(Value::Null),
            "underlying": selected.get("underlying").cloned().unwrap_or(Value::Null),
            "direction": selected.get("side").cloned().unwrap_or(Value::Null),
            "strategy": signal.get("strategy").cloned().unwrap_or(Value::Null),
            "signal_score": st::pyf(signal.get("score"), 0.0),
            "option_score": st::pyf(selected.get("score"), 0.0),
            "ask": st::pyf(selected.get("ask"), 0.0),
            "spread_pct": st::pyf(selected.get("spread_pct"), 0.0),
            "delta": st::pyf(selected.get("delta"), 0.0),
            "target_delta": st::pyf(selected.get("target_delta"), 0.0),
            "dte_days": st::pyf(selected.get("dte_days"), 0.0),
            "target_dte_days": st::pyf(selected.get("target_dte_days"), 0.0),
            "iv_to_realized": st::pyf(selected.get("iv_to_realized"), 0.0),
            "skew_ratio": st::pyf(selected.get("skew_ratio"), 0.0),
            "surface_iv_premium_pct": st::pyf(selected.get("surface_iv_premium_pct"), 0.0),
            "net_expected_edge_pct": st::pyf(selected.get("net_expected_edge_pct"), 0.0),
            "ioc_net_expected_edge_pct": st::pyf(selected.get("ioc_net_expected_edge_pct"), 0.0),
            "flow_score": st::pyf(selected.get("flow_score"), 0.0),
            "entry_fraction": st::pyf(selected.get("entry_fraction"), 1.0),
            "planned_cost_usdt": st::pyf(plan.get("total_entry_cost_usdt"), 0.0),
            "expiry_date_ms": expiry_ms,
            "found_at_ms": self.now_ms(),
        });
        self.sync_scan_universe();
        self.st_set("last_candidate", summary);
        let _ = self.save_state();
        self.record_scan_outcome("ORDERABLE_CANDIDATE", Some(diagnostics));
        self.enter(selected, snapshot).await
    }

    pub(crate) async fn submit_ioc_entry(
        &self,
        selected: &Value,
        quantity: &str,
        limit_price: f64,
        tick: f64,
    ) -> Result<(Value, String, f64, String), CycleError> {
        let symbol = py_str_or_empty(selected.get("symbol"));
        let cid = client_order_id("b", self.now_ms());
        let price_text = format_price(limit_price, tick);
        self.set_pending_entry(selected, quantity, limit_price, &cid, "IOC");
        let order_req = OptionOrder {
            symbol: symbol.clone(),
            side: "BUY".to_string(),
            order_type: "LIMIT".to_string(),
            quantity: quantity.to_string(),
            price: Some(price_text),
            time_in_force: Some("IOC".to_string()),
            post_only: false,
            reduce_only: false,
            client_order_id: Some(cid.clone()),
        };
        let response = match self.client.new_order(&order_req).await {
            Ok(v) => v,
            Err(e) => {
                if !e.uncertain {
                    self.clear_pending_entry(Some(&cid));
                    return Err(CycleError::Api(e));
                }
                match self
                    .client
                    .query_order(&symbol, &OrderRef::Client(cid.clone()))
                    .await
                {
                    Ok(v) => v,
                    Err(_) => {
                        return Err(CycleError::Api(OptionsApiError::uncertain(
                            "OPTIONS_IOC_ORDER_STATUS_UNKNOWN",
                        )));
                    }
                }
            }
        };
        let order = self
            .resolve_submitted_order(&symbol, &cid, &response, limit_price)
            .await?;
        Ok((order, cid, limit_price, "IOC".to_string()))
    }

    pub(crate) async fn submit_entry_order(
        &self,
        selected: &Value,
        quantity: &str,
        ioc_price: f64,
        tick: f64,
        cfg: &Value,
    ) -> Result<(Value, String, f64, String), CycleError> {
        let symbol = py_str_or_empty(selected.get("symbol"));
        let bid = st::pyf(selected.get("bid"), 0.0).max(0.0);
        let ask = st::pyf(selected.get("ask"), 0.0).max(0.0);
        let maker_enabled =
            py_truthy(cfg.get("maker_first_enabled").unwrap_or(&Value::Null)) && bid > 0.0;
        if !maker_enabled {
            return self
                .submit_ioc_entry(selected, quantity, ioc_price, tick)
                .await;
        }
        let mut maker_price = if tick > 0.0 && bid + tick < ask {
            bid + tick
        } else {
            bid
        };
        let tick_or_eps = if tick != 0.0 { tick } else { 1e-12 };
        if maker_price < tick_or_eps {
            maker_price = tick_or_eps;
        }
        let maker_client_id = client_order_id("m", self.now_ms());
        self.set_pending_entry(selected, quantity, maker_price, &maker_client_id, "MAKER");
        let maker_req = OptionOrder {
            symbol: symbol.clone(),
            side: "BUY".to_string(),
            order_type: "LIMIT".to_string(),
            quantity: quantity.to_string(),
            price: Some(format_price(maker_price, tick)),
            time_in_force: Some("GTC".to_string()),
            post_only: true,
            reduce_only: false,
            client_order_id: Some(maker_client_id.clone()),
        };
        let mut response = match self.client.new_order(&maker_req).await {
            Ok(v) => v,
            Err(e) => {
                if e.uncertain {
                    match self
                        .client
                        .query_order(&symbol, &OrderRef::Client(maker_client_id.clone()))
                        .await
                    {
                        Ok(v) => v,
                        Err(_) => {
                            return Err(CycleError::Api(OptionsApiError::uncertain(
                                "OPTIONS_MAKER_ORDER_STATUS_UNKNOWN",
                            )));
                        }
                    }
                } else if py_truthy(selected.get("ioc_eligible").unwrap_or(&Value::Null)) {
                    self.clear_pending_entry(Some(&maker_client_id));
                    return self
                        .submit_ioc_entry(selected, quantity, ioc_price, tick)
                        .await;
                } else {
                    self.clear_pending_entry(Some(&maker_client_id));
                    return Err(CycleError::Api(e));
                }
            }
        };
        let mut order = if response.is_object() {
            response.clone()
        } else {
            json!({})
        };
        if order_is_terminal(&order) {
            if order_executed_quantity(&order) > 0.0 {
                return Ok((order, maker_client_id, maker_price, "MAKER".to_string()));
            }
        } else {
            let wait = st::pyf(cfg.get("maker_wait_seconds"), 2.0);
            (self.sleeper)(wait).await;
            match self
                .client
                .query_order(&symbol, &OrderRef::Client(maker_client_id.clone()))
                .await
            {
                Ok(v) => {
                    if v.is_object() {
                        order = v.clone();
                        response = v;
                    }
                }
                Err(e) => {
                    log_line(&format!(
                        "Options maker order query failed before cancel {}: {}",
                        maker_client_id, e
                    ));
                }
            }
        }
        let _ = response;
        if !order_is_terminal(&order) {
            let canceled = self
                .client
                .cancel_order(&symbol, &OrderRef::Client(maker_client_id.clone()))
                .await;
            match canceled {
                Ok(v) => {
                    if v.is_object() {
                        order = v;
                    }
                }
                Err(e) => {
                    log_line(&format!(
                        "Options maker cancel failed {}: {}",
                        maker_client_id, e
                    ));
                    return Err(CycleError::Api(OptionsApiError::uncertain(
                        "OPTIONS_MAKER_CANCEL_FAILED",
                    )));
                }
            }
            match self
                .client
                .query_order(&symbol, &OrderRef::Client(maker_client_id.clone()))
                .await
            {
                Ok(v) => {
                    if v.is_object()
                        && order_executed_quantity(&v) >= order_executed_quantity(&order)
                    {
                        order = v;
                    }
                }
                Err(e) => {
                    log_line(&format!(
                        "Options maker reconciliation query failed after confirmed cancel {}: {}",
                        maker_client_id, e
                    ));
                }
            }
        }
        if order_executed_quantity(&order) > 0.0 {
            return Ok((order, maker_client_id, maker_price, "MAKER".to_string()));
        }
        if !py_truthy(selected.get("ioc_eligible").unwrap_or(&Value::Null)) {
            self.clear_pending_entry(Some(&maker_client_id));
            return Ok((
                json!({}),
                maker_client_id,
                maker_price,
                "MAKER_ONLY".to_string(),
            ));
        }
        self.clear_pending_entry(Some(&maker_client_id));
        self.submit_ioc_entry(selected, quantity, ioc_price, tick)
            .await
    }

    pub(crate) async fn resolve_submitted_order(
        &self,
        symbol: &str,
        client_id: &str,
        response: &Value,
        fallback: f64,
    ) -> Result<Value, CycleError> {
        let order = if response.is_object() {
            response.clone()
        } else {
            json!({})
        };
        let executed = order_executed_quantity(&order);
        if executed > 0.0 {
            return Ok(order);
        }
        match self
            .client
            .query_order(symbol, &OrderRef::Client(client_id.to_string()))
            .await
        {
            Ok(v) => {
                if v.is_object() {
                    Ok(v)
                } else {
                    Ok(order)
                }
            }
            Err(e) => {
                log_line(&format!(
                    "Options order verification failed for {}: {}",
                    client_id, e
                ));
                let _ = fallback;
                if order.is_object() && !order.as_object().map(|m| m.is_empty()).unwrap_or(true) {
                    Ok(order)
                } else {
                    Err(CycleError::Api(OptionsApiError::uncertain(
                        "OPTIONS_ORDER_STATUS_UNKNOWN",
                    )))
                }
            }
        }
    }

    pub(crate) fn pending_entry_value(
        &self,
        selected: &Value,
        quantity: &str,
        price: f64,
        cid: &str,
        mode: &str,
    ) -> Value {
        let signal = selected.get("signal").cloned().unwrap_or(json!({}));
        let underlying = selected
            .get("underlying")
            .filter(|v| py_truthy(v))
            .cloned()
            .or_else(|| signal.get("underlying").cloned())
            .unwrap_or(Value::Null);
        json!({
            "symbol": selected.get("symbol").cloned().unwrap_or(Value::Null),
            "underlying": underlying,
            "side": selected.get("side").cloned().unwrap_or(Value::Null),
            "quantity": st::pyf(Some(&json!(quantity)), 0.0),
            "submitted_price": price,
            "unit": st::pyf(selected.get("unit"), 1.0),
            "tick_size": st::pyf(selected.get("tick_size"), 0.0),
            "expiry_date_ms": st::pyf(selected.get("expiryDate"), 0.0) as i64,
            "client_order_id": cid,
            "execution_mode": mode,
            "created_at_ms": self.now_ms(),
            "signal_key": py_str_or_empty(signal.get("signal_key")),
            "signal_score": st::pyf(signal.get("score"), 0.0),
            "signal_strategy": signal.get("strategy").filter(|v| py_truthy(v)).cloned().unwrap_or(json!("ADAPTIVE_TREND")),
            "spot_price": st::pyf(signal.get("spot_price"), 0.0),
            "option_score": st::pyf(selected.get("score"), 0.0),
            "entry_mark_iv": st::pyf(selected.get("mark_iv"), 0.0),
            "entry_delta": st::pyf(selected.get("delta"), 0.0),
            "entry_gamma": st::pyf(selected.get("gamma"), 0.0),
            "entry_theta": st::pyf(selected.get("theta"), 0.0),
            "entry_vega": st::pyf(selected.get("vega"), 0.0),
            "target_delta": st::pyf(selected.get("target_delta"), 0.0),
            "target_dte_days": st::pyf(selected.get("target_dte_days"), 0.0),
            "entry_iv_to_realized": st::pyf(selected.get("iv_to_realized"), 0.0),
            "entry_skew_ratio": st::pyf(selected.get("skew_ratio"), 0.0),
            "entry_surface_iv_premium_pct": st::pyf(selected.get("surface_iv_premium_pct"), 0.0),
            "entry_net_expected_edge_pct": st::pyf(selected.get("net_expected_edge_pct"), 0.0),
            "entry_ioc_net_expected_edge_pct": st::pyf(selected.get("ioc_net_expected_edge_pct"), 0.0),
            "entry_flow_score": st::pyf(selected.get("flow_score"), 0.0),
        })
    }

    pub(crate) fn set_pending_entry(
        &self,
        selected: &Value,
        quantity: &str,
        price: f64,
        cid: &str,
        mode: &str,
    ) {
        let pending = self.pending_entry_value(selected, quantity, price, cid, mode);
        self.st_set("pending_entry", pending);
        let _ = self.save_state();
    }

    pub(crate) fn clear_pending_entry(&self, client_id: Option<&str>) {
        if let Some(cid) = client_id {
            let pending = self.st_get("pending_entry");
            let existing_cid = py_str_or_empty(pending.get("client_order_id"));
            if existing_cid != cid {
                return;
            }
        }
        self.st_set("pending_entry", Value::Null);
        let _ = self.save_state();
    }

    pub(crate) async fn enter(&self, selected: Value, snapshot: Value) -> Value {
        let cfg = self.config();
        let ask = st::pyf(selected.get("ask"), 0.0);
        let tick = st::pyf(selected.get("tick_size"), 0.0);
        let limit_price = ask + (if tick > 0.0 { tick } else { 0.0 });
        let available = st::pyf(
            snapshot.get("balance").and_then(|b| b.get("available")),
            0.0,
        )
        .max(0.0);
        let planning_bankroll = options_spend_limit(&cfg, available);
        let spot_price_raw = selected
            .get("signal")
            .and_then(|s| s.get("spot_price"))
            .cloned()
            .unwrap_or(Value::Null);
        let unit = selected.get("unit").cloned().unwrap_or(json!(1));
        let unit_f = st::pyf(Some(&unit), 1.0);
        let min_qty = selected
            .get("min_qty")
            .filter(|v| py_truthy(v))
            .or_else(|| selected.get("minQty"))
            .cloned();
        let step_size = selected
            .get("step_size")
            .filter(|v| py_truthy(v))
            .or_else(|| selected.get("minQty"))
            .cloned();
        let min_qty_f = st::pyf(min_qty.as_ref(), 0.0);
        let step_size_f = st::pyf(step_size.as_ref(), 0.0);
        let entry_fraction = st::pyf(cfg.get("entry_fraction"), 1.00)
            .min(st::pyf(selected.get("entry_fraction"), 1.00));
        let cap = st::pyf(cfg.get("capital_limit_usdt"), 0.0);
        let plan = st::build_long_option_entry_plan(
            limit_price,
            st::pyf(Some(&spot_price_raw), 0.0),
            unit_f,
            min_qty_f,
            step_size_f,
            planning_bankroll,
            entry_fraction,
            cap,
        );
        if !py_truthy(plan.get("accepted").unwrap_or(&Value::Null)) {
            let cand = self.st_get("last_candidate");
            return self.record_reason(
                &format!(
                    "옵션 지갑 가용 {:.2} USDT로 주문 수량을 만들 수 없습니다: {}",
                    planning_bankroll,
                    py_str_or_empty(plan.get("reason"))
                ),
                false,
                Some(&cand),
            );
        }
        let plan_total = st::pyf(plan.get("total_entry_cost_usdt"), 0.0);
        if plan_total > available + 1e-9 {
            return self.record_reason(
                "옵션 계좌 사용 가능 잔고가 계획 금액보다 적습니다.",
                false,
                None,
            );
        }
        let symbol = py_str_or_empty(selected.get("symbol"));
        let quantity_value = plan.get("quantity").cloned().unwrap_or(Value::Null);
        let quantity_text = py_str_or_empty(Some(&quantity_value));
        let submit_res = self
            .submit_entry_order(&selected, &quantity_text, limit_price, tick, &cfg)
            .await;
        let (order, cid, submitted_price, execution_mode) = match submit_res {
            Ok(v) => v,
            Err(CycleError::Api(e)) => {
                if e.uncertain {
                    return self.record_reason(
                        "옵션 주문 상태 또는 메이커 취소 상태가 불확실해 신규 주문을 중단했습니다. 거래소 주문을 확인하세요.",
                        true,
                        None,
                    );
                }
                return self.record_reason(&format!("옵션 진입 주문 거절: {}", e), true, None);
            }
            Err(CycleError::Other(m)) => {
                return self.record_reason(&format!("옵션 진입 주문 거절: {}", m), true, None);
            }
        };
        let filled_qty = order_executed_quantity(&order);
        if filled_qty <= 0.0 {
            self.clear_pending_entry(Some(&cid));
            return self.record_reason(
                "옵션 메이커 주문이 미체결됐고 IOC 기준 순기대수익도 부족해 추격 진입하지 않았습니다.",
                false,
                None,
            );
        }
        let fill_price = order_average_price(&order, submitted_price);
        let signal_key = py_str_or_empty(selected.get("signal").and_then(|s| s.get("signal_key")));
        if !signal_key.is_empty() {
            let mut consumed: Vec<String> = match self.st_get("consumed_signal_keys") {
                Value::Array(items) => items.iter().map(|v| py_str(Some(v))).collect(),
                _ => Vec::new(),
            };
            if !consumed.contains(&signal_key) {
                consumed.push(signal_key);
            }
            let len = consumed.len();
            let start = len.saturating_sub(200);
            let trimmed: Vec<Value> = consumed[start..].iter().map(|s| json!(s)).collect();
            self.st_set("consumed_signal_keys", json!(trimmed));
            let _ = self.save_state();
        }
        let unit_value = st::pyf(selected.get("unit"), 1.0);
        let premium = fill_price * filled_qty * unit_value;
        let entry_fee = st::estimate_option_fee(
            fill_price,
            st::pyf(
                selected.get("signal").and_then(|s| s.get("spot_price")),
                0.0,
            ),
            filled_qty,
            unit_value,
        );
        let total_cost = premium + entry_fee;
        let planned_cap = st::pyf(plan.get("hard_cap_usdt"), 0.0);
        if planned_cap > 0.0 && total_cost > planned_cap * 1.01 + 1e-8 {
            self.st_set("last_error", json!("OPTIONS_FILLED_COST_EXCEEDED_HARD_CAP"));
            let _ = self.save_state();
            self.notify(&format!(
                "🚨 옵션 체결 비용 {:.4} USDT가 계획 한도 {:.4} USDT를 넘었습니다. 추가 진입을 차단하고 현재 포지션만 관리합니다.",
                total_cost, planned_cap
            ))
            .await;
        }
        let new_bankroll =
            (st::pyf(Some(&self.st_get("cash_bankroll_usdt")), 0.0) - total_cost).max(0.0);
        self.st_set("cash_bankroll_usdt", json!(new_bankroll));
        let order_id = order.get("orderId").cloned().unwrap_or(Value::Null);
        let entry_ms = self.now_ms();
        let expiry_ms = st::pyf(selected.get("expiryDate"), 0.0) as i64;
        let signal = selected.get("signal").cloned().unwrap_or(json!({}));
        let strategy = signal
            .get("strategy")
            .filter(|v| py_truthy(v))
            .cloned()
            .unwrap_or(json!("ADAPTIVE_TREND"));
        let mut position = Map::new();
        position.insert("symbol".to_string(), json!(symbol.clone()));
        position.insert(
            "underlying".to_string(),
            selected.get("underlying").cloned().unwrap_or(Value::Null),
        );
        position.insert(
            "side".to_string(),
            selected.get("side").cloned().unwrap_or(Value::Null),
        );
        position.insert("quantity".to_string(), json!(filled_qty));
        position.insert("original_quantity".to_string(), json!(filled_qty));
        position.insert("entry_price".to_string(), json!(fill_price));
        position.insert("entry_premium_usdt".to_string(), json!(premium));
        position.insert("entry_fee_usdt".to_string(), json!(entry_fee));
        position.insert("entry_total_usdt".to_string(), json!(total_cost));
        position.insert("entry_time_ms".to_string(), json!(entry_ms));
        position.insert("expiry_date_ms".to_string(), json!(expiry_ms));
        position.insert("unit".to_string(), json!(unit_value));
        position.insert("tick_size".to_string(), json!(tick));
        position.insert("peak_mark".to_string(), json!(fill_price));
        position.insert("client_order_id".to_string(), json!(cid));
        position.insert("order_id".to_string(), order_id);
        position.insert("entry_execution_mode".to_string(), json!(execution_mode));
        position.insert(
            "signal_score".to_string(),
            json!(st::pyf(signal.get("score"), 0.0)),
        );
        position.insert(
            "option_score".to_string(),
            json!(st::pyf(selected.get("score"), 0.0)),
        );
        position.insert("status".to_string(), json!("OPEN"));
        position.insert("signal_strategy".to_string(), strategy);
        for (key, src) in [
            ("entry_mark_iv", "mark_iv"),
            ("entry_delta", "delta"),
            ("entry_gamma", "gamma"),
            ("entry_theta", "theta"),
            ("entry_vega", "vega"),
            ("target_delta", "target_delta"),
            ("target_dte_days", "target_dte_days"),
            ("entry_iv_to_realized", "iv_to_realized"),
            ("entry_skew_ratio", "skew_ratio"),
            ("entry_surface_iv_premium_pct", "surface_iv_premium_pct"),
            ("entry_net_expected_edge_pct", "net_expected_edge_pct"),
            (
                "entry_ioc_net_expected_edge_pct",
                "ioc_net_expected_edge_pct",
            ),
            ("entry_flow_score", "flow_score"),
        ] {
            position.insert(key.to_string(), json!(st::pyf(selected.get(src), 0.0)));
        }
        let position_value = Value::Object(position);
        self.st_set("active_position", position_value.clone());
        self.st_set("pending_entry", Value::Null);
        self.st_set("last_reason", json!(format!("{} 옵션 매수 체결", symbol)));
        self.st_set("last_error", json!(""));
        let _ = self.save_state();
        let side_text = py_str_or_empty(selected.get("side"));
        let text = [
            "🟣 옵션 자동매매 진입".to_string(),
            format!("종목: {}", symbol),
            format!("방향: {} | 수량 {}", side_text, format_g(filled_qty)),
            format!(
                "프리미엄: {:.4} USDT | 예상 수수료 {:.4}",
                premium, entry_fee
            ),
            format!(
                "사용 예산: {} · 진입 시 가용 {:.4} USDT",
                options_budget_text(&cfg),
                planning_bankroll
            ),
            "네이키드 매도 없이 매수 프리미엄만 위험에 노출됩니다.".to_string(),
        ]
        .join("\n");
        self.notify(&text).await;
        json!({"action": "entered", "position": position_value})
    }

    pub(crate) async fn record_manage_health(&self, result: Value) -> Value {
        let action = py_str_or_empty(result.get("action"));
        let failed = action == "blocked";
        if failed {
            let streak = (st::pyf(Some(&self.st_get("manage_error_streak")), 0.0) as i64) + 1;
            self.st_set("manage_error_streak", json!(streak));
            if streak == 3 || streak == 10 {
                self.notify(&format!(
                    "🚨 옵션 포지션 관리 API 오류가 {}회 연속 발생했습니다. 신규 진입은 차단되고 기존 포지션 재확인을 계속합니다.",
                    streak
                ))
                .await;
            }
        } else {
            self.st_set("manage_error_streak", json!(0));
            self.st_set("last_manage_success_ts", json!(self.now_secs()));
            self.st_set("last_error", json!(""));
        }
        let _ = self.save_state();
        result
    }

    pub async fn status_snapshot(&self, refresh: bool) -> Value {
        self.sync_scan_universe();
        let snapshot_opt = if refresh {
            Some(self.preflight().await)
        } else {
            None
        };
        let cfg = self.config();
        let st_snapshot = self.state();
        let active = match st_snapshot.get("active_position") {
            Some(v) if py_truthy(v) => v.clone(),
            _ => Value::Null,
        };
        let api_ok = snapshot_opt
            .as_ref()
            .and_then(|s| s.get("ok"))
            .map(py_truthy)
            .unwrap_or(false);
        let api_error = snapshot_opt
            .as_ref()
            .and_then(|s| s.get("error"))
            .cloned()
            .unwrap_or(json!(""));
        let balance = snapshot_opt
            .as_ref()
            .and_then(|s| s.get("balance"))
            .cloned()
            .unwrap_or(json!({}));
        let can_trade = snapshot_opt
            .as_ref()
            .and_then(|s| s.get("can_trade"))
            .cloned()
            .unwrap_or(Value::Null);
        let pos_count = snapshot_opt
            .as_ref()
            .and_then(|s| s.get("positions"))
            .and_then(|v| v.as_array())
            .map(|a| a.len())
            .unwrap_or(0);
        let ord_count = snapshot_opt
            .as_ref()
            .and_then(|s| s.get("orders"))
            .and_then(|v| v.as_array())
            .map(|a| a.len())
            .unwrap_or(0);
        let cash = st::pyf(st_snapshot.get("cash_bankroll_usdt"), 0.0);
        let cap = st::pyf(cfg.get("capital_limit_usdt"), 0.0);
        let budget = options_budget_text(&cfg);
        let pnl = realized_pnl(&st_snapshot);
        let last_reason = st_snapshot.get("last_reason").cloned().unwrap_or(json!(""));
        let last_error = st_snapshot.get("last_error").cloned().unwrap_or(json!(""));
        let last_candidate = st_snapshot
            .get("last_candidate")
            .cloned()
            .unwrap_or(Value::Null);
        let last_success = st::pyf(st_snapshot.get("last_manage_success_ts"), 0.0);
        let streak = st::pyf(st_snapshot.get("manage_error_streak"), 0.0) as i64;
        let pending = py_truthy(st_snapshot.get("pending_entry").unwrap_or(&Value::Null))
            || py_truthy(st_snapshot.get("pending_exit").unwrap_or(&Value::Null));
        let enabled = py_truthy(cfg.get("enabled").unwrap_or(&Value::Null));
        let mut status = json!({
            "enabled": enabled,
            "state_ok": !self.state_unreadable(),
            "api_ok": api_ok,
            "api_error": api_error,
            "balance": balance,
            "can_trade": can_trade,
            "exchange_positions": pos_count,
            "exchange_orders": ord_count,
            "cash_bankroll_usdt": cash,
            "capital_limit_usdt": cap,
            "budget_text": budget,
            "realized_pnl_usdt": pnl,
            "active_position": active,
            "last_reason": last_reason,
            "last_error": last_error,
            "last_candidate": last_candidate,
            "last_manage_success_ts": last_success,
            "manage_error_streak": streak,
            "pending_order": pending,
        });
        let history: Vec<Value> = match st_snapshot.get("recent_scan_outcomes") {
            Some(Value::Array(v)) => v.clone(),
            _ => Vec::new(),
        };
        let window = history.len();
        let mut counts: HashMap<String, i64> = HashMap::new();
        for item in &history {
            *counts.entry(py_str(Some(item))).or_insert(0) += 1;
        }
        let mut stats = Map::new();
        let mut labels = Map::new();
        for (code, label) in SCAN_OUTCOME_LABELS.iter() {
            stats.insert(
                code.to_string(),
                json!(counts.get(*code).copied().unwrap_or(0)),
            );
            labels.insert(code.to_string(), json!(label));
        }
        let diagnostics = match st_snapshot.get("last_scan_diagnostics") {
            Some(v) if py_truthy(v) => v.clone(),
            _ => json!({}),
        };
        if let Value::Object(map) = &mut status {
            map.insert("scan_outcomes_window".to_string(), json!(window));
            map.insert("scan_rejection_stats".to_string(), Value::Object(stats));
            map.insert("scan_outcome_labels".to_string(), Value::Object(labels));
            map.insert("last_scan_diagnostics".to_string(), diagnostics);
        }
        status
    }

    pub async fn run_cycle(&self, force_scan: bool, force_exit: bool) -> Value {
        let guard = match self.cycle_lock.try_lock() {
            Ok(g) => g,
            Err(_) => {
                return json!({
                    "action": "busy",
                    "reason": "OPTIONS_CYCLE_ALREADY_RUNNING",
                });
            }
        };
        let _held = guard;
        let cfg = self.config();
        let now = self.now_secs();
        if self.state_unreadable() {
            return self.record_reason("OPTIONS_STATE_UNREADABLE_FAIL_CLOSED", true, None);
        }
        let mut active = self.st_get("active_position");
        if !py_truthy(&active) {
            let recovered = self.recover_runtime_ownership().await;
            if let Some(rec) = &recovered {
                active = self.st_get("active_position");
                if !py_truthy(&active) {
                    return rec.clone();
                }
            }
        }
        if py_truthy(&active) {
            let interval = st::pyf(cfg.get("manage_interval_seconds"), 10.0);
            let last_manage = st::pyf(Some(&self.st_get("last_manage_ts")), 0.0);
            if force_exit || force_scan || now - last_manage >= interval {
                self.st_set("last_manage_ts", json!(now));
                let _ = self.save_state();
                let result = self.manage_active_position(force_exit, None).await;
                return self.record_manage_health(result).await;
            }
            return json!({"action": "waiting", "reason": "OPTIONS_MANAGE_INTERVAL"});
        }
        if force_exit {
            return self.record_reason("청산할 봇 옵션 포지션이 없습니다.", false, None);
        }
        if !py_truthy(cfg.get("enabled").unwrap_or(&Value::Null)) {
            return self.record_reason("옵션 자동매매 OFF — 신규 진입하지 않습니다.", false, None);
        }
        if force_scan {
            let cooldown = st::pyf(cfg.get("manual_scan_cooldown_seconds"), 60.0);
            let elapsed = now - st::pyf(Some(&self.st_get("last_manual_scan_ts")), 0.0);
            if elapsed < cooldown {
                return json!({
                    "action": "waiting",
                    "reason": format!("OPTIONS_MANUAL_SCAN_COOLDOWN ({:.0}s)", cooldown - elapsed),
                });
            }
            self.st_set("last_manual_scan_ts", json!(now));
        } else {
            let scan_interval = st::pyf(cfg.get("scan_interval_seconds"), 300.0);
            if now - st::pyf(Some(&self.st_get("last_scan_ts")), 0.0) < scan_interval {
                return json!({"action": "waiting", "reason": "OPTIONS_SCAN_INTERVAL"});
            }
        }
        self.st_set("last_scan_ts", json!(now));
        let _ = self.save_state();
        self.scan_and_maybe_enter().await
    }

    // --- Delegated implementations (defined in options_manage.rs) ---
    pub(crate) async fn manage_active_position(
        &self,
        force_exit: bool,
        prefetched: Option<Value>,
    ) -> Value {
        self.manage_active_position_inner(force_exit, prefetched)
            .await
    }

    pub(crate) async fn recover_runtime_ownership(&self) -> Option<Value> {
        if self
            .ownership_recovery_done
            .lock()
            .map(|g| *g)
            .unwrap_or(false)
        {
            return None;
        }
        if !py_truthy(self.config().get("enabled").unwrap_or(&Value::Null))
            && !py_truthy(&self.st_get("pending_entry"))
        {
            if let Ok(mut guard) = self.ownership_recovery_done.lock() {
                *guard = true;
            }
            return None;
        }
        let snapshot = match self.private_snapshot().await {
            Ok(s) => s,
            Err(e) => {
                return Some(self.record_reason(
                    &format!("옵션 재시작 소유권 확인 실패: {}", e),
                    true,
                    None,
                ));
            }
        };
        let pending = self.st_get("pending_entry");
        if py_truthy(&pending) {
            let result = self.resolve_pending_entry_impl(&pending, &snapshot).await;
            if let Some(r) = result {
                if py_truthy(&self.st_get("active_position")) {
                    if let Ok(mut guard) = self.ownership_recovery_done.lock() {
                        *guard = true;
                    }
                }
                return Some(r);
            }
        }
        let recovered = self.recover_legacy_bot_position_impl(&snapshot).await;
        if let Ok(mut guard) = self.ownership_recovery_done.lock() {
            *guard = true;
        }
        recovered
    }
}

fn increment_diag(diag: &mut Value, section: &str, key: &str) {
    increment_diag_count(diag, section, key, 1);
}

fn increment_diag_count(diag: &mut Value, section: &str, key: &str, amount: i64) {
    if let Value::Object(map) = diag {
        if section == "orderable_candidates" {
            let current = st::pyf(map.get("orderable_candidates"), 0.0) as i64;
            map.insert("orderable_candidates".to_string(), json!(current + amount));
            return;
        }
        if let Some(Value::Object(inner)) = map.get_mut(section) {
            let current = st::pyf(inner.get(key), 0.0) as i64;
            inner.insert(key.to_string(), json!(current + amount));
        }
    }
}

/// adaptive `_adaptive_trailing_exit`.
pub(crate) fn adaptive_trailing_exit(entry_price: f64, peak_price: f64, mark_price: f64) -> bool {
    if entry_price <= 0.0 || peak_price <= entry_price {
        return false;
    }
    let peak_return = peak_price / entry_price - 1.0;
    let tiers = [
        (2.00f64, 0.30f64, 0.80f64),
        (1.00, 0.35, 0.35),
        (0.50, 0.40, 0.10),
    ];
    for (activation, drawdown, locked_return) in tiers {
        if peak_return < activation {
            continue;
        }
        let trailing_floor =
            (peak_price * (1.0 - drawdown)).max(entry_price * (1.0 + locked_return));
        return mark_price <= trailing_floor;
    }
    false
}

/// Helper: returns true if no schedule needed. Used by clock conversions.
#[allow(dead_code)]
fn utc_from_epoch(epoch: f64) -> chrono::DateTime<Utc> {
    let secs = epoch as i64;
    let nanos = ((epoch - secs as f64) * 1_000_000_000.0) as u32;
    Utc.timestamp_opt(secs, nanos)
        .single()
        .unwrap_or_else(Utc::now)
}

impl<C: OptionsApi> OptionsService<C> {
    pub(crate) async fn exchange_info_cached(&self) -> Result<Value, CycleError> {
        self.get_exchange_info().await
    }
}

#[cfg(test)]
mod tests {
    #![allow(dead_code, unused_variables, clippy::manual_async_fn)]
    use super::*;
    use crate::exchange::binance_options::ApiResult;
    use crate::store::clock::FixedClock;
    use chrono::TimeZone;

    struct FakeApi {
        authenticated: bool,
        new_orders: Mutex<Vec<OptionOrder>>,
        current_positions: Mutex<Value>,
        trades: Mutex<Vec<Value>>,
        query_orders: Mutex<HashMap<String, Value>>,
        mark_price: f64,
        bid: f64,
    }

    impl FakeApi {
        fn new() -> Self {
            FakeApi {
                authenticated: true,
                new_orders: Mutex::new(Vec::new()),
                current_positions: Mutex::new(json!([])),
                trades: Mutex::new(Vec::new()),
                query_orders: Mutex::new(HashMap::new()),
                mark_price: 1.0,
                bid: 1.0,
            }
        }
        fn new_orders_len(&self) -> usize {
            self.new_orders.lock().map(|g| g.len()).unwrap_or(0)
        }
        fn orders_clone(&self) -> Vec<OptionOrder> {
            self.new_orders
                .lock()
                .map(|g| g.clone())
                .unwrap_or_default()
        }
    }

    impl OptionsApi for FakeApi {
        fn authenticated(&self) -> bool {
            self.authenticated
        }
        fn sync_time(&self) -> impl std::future::Future<Output = ApiResult> + Send {
            async { Ok(json!({"serverTime": 0})) }
        }
        fn ping(&self) -> impl std::future::Future<Output = ApiResult> + Send {
            async { Ok(json!({})) }
        }
        fn exchange_info(&self) -> impl std::future::Future<Output = ApiResult> + Send {
            async {
                Ok(json!({
                    "optionSymbols": [],
                }))
            }
        }
        fn mark_price(
            &self,
            _symbol: Option<&str>,
        ) -> impl std::future::Future<Output = ApiResult> + Send {
            let p = self.mark_price;
            async move {
                Ok(
                    json!([{"markPrice": p, "markIV": 0.5, "delta": 0.4, "theta": -0.1, "gamma": 0.02, "vega": 0.1}]),
                )
            }
        }
        fn ticker(
            &self,
            _symbol: Option<&str>,
        ) -> impl std::future::Future<Output = ApiResult> + Send {
            async { Ok(json!([{}])) }
        }
        fn depth(
            &self,
            _symbol: &str,
            _limit: u32,
        ) -> impl std::future::Future<Output = ApiResult> + Send {
            let b = self.bid;
            async move { Ok(json!({"bids": [[b, 100]], "asks": [[b, 100]]})) }
        }
        fn recent_trades(
            &self,
            _symbol: &str,
            _limit: u32,
        ) -> impl std::future::Future<Output = ApiResult> + Send {
            async { Ok(json!([])) }
        }
        fn margin_account(&self) -> impl std::future::Future<Output = ApiResult> + Send {
            async { Ok(json!({"canTrade": true, "available": 1000.0, "equity": 1000.0})) }
        }
        fn positions(
            &self,
            _symbol: Option<&str>,
        ) -> impl std::future::Future<Output = ApiResult> + Send {
            let current = self
                .current_positions
                .lock()
                .map(|g| g.clone())
                .unwrap_or(json!([]));
            async move { Ok(current) }
        }
        fn open_orders(
            &self,
            _symbol: Option<&str>,
        ) -> impl std::future::Future<Output = ApiResult> + Send {
            async { Ok(json!([])) }
        }
        fn query_order(
            &self,
            _symbol: &str,
            order_ref: &OrderRef,
        ) -> impl std::future::Future<Output = ApiResult> + Send {
            let key = match order_ref {
                OrderRef::Client(c) => c.clone(),
                OrderRef::Id(_) => String::new(),
            };
            let cached = self
                .query_orders
                .lock()
                .map(|g| g.get(&key).cloned())
                .unwrap_or(None);
            async move {
                match cached {
                    Some(v) => Ok(v),
                    None => Ok(
                        json!({"status": "FILLED", "executedQty": "1", "avgPrice": 1.0, "orderId": 1}),
                    ),
                }
            }
        }
        fn new_order(
            &self,
            order: &OptionOrder,
        ) -> impl std::future::Future<Output = ApiResult> + Send {
            if let Ok(mut guard) = self.new_orders.lock() {
                guard.push(order.clone());
            }
            let trades = self.trades.lock().map(|g| g.clone()).unwrap_or_default();
            let response = if order.side == "SELL" {
                let mut map = Map::new();
                map.insert("status".to_string(), json!("FILLED"));
                map.insert("executedQty".to_string(), json!(order.quantity));
                map.insert("avgPrice".to_string(), json!(1.0));
                map.insert("orderId".to_string(), json!(123));
                Value::Object(map)
            } else {
                let mut map = Map::new();
                map.insert("status".to_string(), json!("FILLED"));
                map.insert("executedQty".to_string(), json!(order.quantity));
                map.insert("avgPrice".to_string(), json!(1.0));
                map.insert("orderId".to_string(), json!(1));
                Value::Object(map)
            };
            let _ = trades;
            async move { Ok(response) }
        }
        fn cancel_order(
            &self,
            _symbol: &str,
            _order_ref: &OrderRef,
        ) -> impl std::future::Future<Output = ApiResult> + Send {
            async { Ok(json!({})) }
        }
        fn user_trades(
            &self,
            _symbol: &str,
            _start_time: Option<i64>,
            _limit: u32,
        ) -> impl std::future::Future<Output = ApiResult> + Send {
            async { Ok(json!([])) }
        }
        fn exercise_records(
            &self,
            _symbol: Option<&str>,
            _start_time: Option<i64>,
            _limit: u32,
        ) -> impl std::future::Future<Output = ApiResult> + Send {
            async { Ok(json!([])) }
        }
        fn underlying_klines(
            &self,
            _underlying: &str,
            _interval: &str,
            _limit: u32,
        ) -> impl std::future::Future<Output = Result<Vec<Candle>, OptionsApiError>> + Send
        {
            async { Ok(Vec::new()) }
        }
    }

    fn fixed_clock() -> Arc<dyn Clock> {
        let dt = Utc.with_ymd_and_hms(2024, 1, 1, 0, 0, 0).unwrap();
        Arc::new(FixedClock::new(dt, 1704067200.0))
    }

    fn no_op_sleeper() -> Sleeper {
        Arc::new(|_| Box::pin(async {}))
    }

    fn no_op_notifier() -> Notifier {
        Arc::new(|_| Box::pin(async {}))
    }

    struct TempDir {
        path: PathBuf,
    }

    impl TempDir {
        fn new() -> Self {
            let mut p = std::env::temp_dir();
            let n = CLIENT_COUNTER.fetch_add(1, Ordering::Relaxed);
            p.push(format!("optstest_{}_{}", std::process::id(), n));
            let _ = std::fs::remove_dir_all(&p);
            let _ = std::fs::create_dir_all(&p);
            TempDir { path: p }
        }
        fn path(&self) -> &PathBuf {
            &self.path
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.path);
        }
    }

    fn disabled_cfg_getter() -> ConfigGetter {
        Arc::new(|| {
            json!({
                "enabled": false,
                "capital_limit_usdt": 100.0,
            })
        })
    }

    #[tokio::test]
    async fn disabled_config_returns_off_reason() {
        let tmp = TempDir::new();
        let state_path = tmp.path().join("options_state.json");
        let api = Arc::new(FakeApi::new());
        let svc = OptionsService::new(
            disabled_cfg_getter(),
            api.clone(),
            state_path,
            Some(no_op_notifier()),
            fixed_clock(),
            no_op_sleeper(),
        );
        let result = svc.run_cycle(false, false).await;
        assert_eq!(
            result.get("reason").and_then(|v| v.as_str()),
            Some("옵션 자동매매 OFF — 신규 진입하지 않습니다.")
        );
        assert_eq!(api.new_orders_len(), 0);
    }

    #[tokio::test]
    async fn unreadable_state_blocks() {
        let tmp = TempDir::new();
        let state_path = tmp.path().join("options_state.json");
        let _ = std::fs::write(&state_path, "not json at all");
        let api = Arc::new(FakeApi::new());
        let svc = OptionsService::new(
            disabled_cfg_getter(),
            api.clone(),
            state_path,
            Some(no_op_notifier()),
            fixed_clock(),
            no_op_sleeper(),
        );
        assert!(svc.state_unreadable());
        let result = svc.run_cycle(false, false).await;
        assert_eq!(
            result.get("reason").and_then(|v| v.as_str()),
            Some("OPTIONS_STATE_UNREADABLE_FAIL_CLOSED")
        );
        assert_eq!(
            result.get("action").and_then(|v| v.as_str()),
            Some("blocked")
        );
        assert_eq!(api.new_orders_len(), 0);
    }

    #[tokio::test]
    async fn force_exit_sends_sell_reduce_only() {
        let tmp = TempDir::new();
        let state_path = tmp.path().join("options_state.json");
        let api = Arc::new(FakeApi::new());
        {
            let mut pos = api.current_positions.lock().unwrap();
            *pos = json!([{ "symbol": "BTC-240101-40000-C", "quantity": 1 }]);
        }
        let svc = OptionsService::new(
            disabled_cfg_getter(),
            api.clone(),
            state_path,
            Some(no_op_notifier()),
            fixed_clock(),
            no_op_sleeper(),
        );
        let position = json!({
            "symbol": "BTC-240101-40000-C",
            "underlying": "BTCUSDT",
            "side": "CALL",
            "quantity": 1.0,
            "original_quantity": 1.0,
            "entry_price": 1.0,
            "entry_total_usdt": 1.0,
            "entry_time_ms": 1704067200000i64,
            "expiry_date_ms": 1704070800000i64,
            "unit": 1.0,
            "tick_size": 0.0001,
            "peak_mark": 1.0,
            "status": "OPEN",
        });
        svc.st_set("active_position", position);
        let _ = svc.save_state();
        let result = svc.run_cycle(false, true).await;
        let action = result.get("action").and_then(|v| v.as_str()).unwrap_or("");
        assert_eq!(action, "exited");
        let orders = api.orders_clone();
        let sell_orders: Vec<&OptionOrder> = orders.iter().filter(|o| o.side == "SELL").collect();
        assert_eq!(sell_orders.len(), 1);
        assert!(sell_orders[0].reduce_only);
        assert_eq!(sell_orders[0].time_in_force.as_deref(), Some("IOC"));
        let trade_opt = result.get("trade");
        assert!(trade_opt.is_some());
    }

    #[test]
    fn adaptive_trailing_tier_table() {
        let entry = 1.0f64;
        let cases = vec![
            (1.49, 1.0, false),
            (1.50, 1.09, true),
            (2.0, 1.34, true),
            (3.0, 2.09, true),
        ];
        for (peak, mark, expected) in cases {
            let got = adaptive_trailing_exit(entry, peak, mark);
            assert_eq!(got, expected, "peak={} mark={}", peak, mark);
        }
    }
}
