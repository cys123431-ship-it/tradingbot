use std::collections::{HashMap, HashSet};
use std::io::Write;
use std::path::Path;
use std::str::FromStr;
use std::sync::Arc;

use chrono::Utc;
use rusqlite::{params, Connection, OptionalExtension, Row};
use serde_json::{Map, Value};

use super::clock::{py_isoformat, Clock};
use super::ledger::{py_float_repr, py_json_dumps};

pub const DAILY_LOSS_ENTRY_LOCK_KEY: &str = "daily_loss_entry_lock";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum OrderState {
    Planned,
    Submitting,
    SubmittedUnknown,
    Acknowledged,
    PartiallyFilled,
    FilledUnverifiedLiquidation,
    FilledLiquidationConflict,
    FilledUnprotected,
    Protected,
    Closing,
    PartiallyClosed,
    EmergencyCloseFailed,
    Closed,
    Canceled,
    Failed,
}

impl OrderState {
    pub fn as_str(&self) -> &'static str {
        match self {
            OrderState::Planned => "PLANNED",
            OrderState::Submitting => "SUBMITTING",
            OrderState::SubmittedUnknown => "SUBMITTED_UNKNOWN",
            OrderState::Acknowledged => "ACKNOWLEDGED",
            OrderState::PartiallyFilled => "PARTIALLY_FILLED",
            OrderState::FilledUnverifiedLiquidation => "FILLED_UNVERIFIED_LIQUIDATION",
            OrderState::FilledLiquidationConflict => "FILLED_LIQUIDATION_CONFLICT",
            OrderState::FilledUnprotected => "FILLED_UNPROTECTED",
            OrderState::Protected => "PROTECTED",
            OrderState::Closing => "CLOSING",
            OrderState::PartiallyClosed => "PARTIALLY_CLOSED",
            OrderState::EmergencyCloseFailed => "EMERGENCY_CLOSE_FAILED",
            OrderState::Closed => "CLOSED",
            OrderState::Canceled => "CANCELED",
            OrderState::Failed => "FAILED",
        }
    }
}

impl FromStr for OrderState {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        Ok(match s {
            "PLANNED" => OrderState::Planned,
            "SUBMITTING" => OrderState::Submitting,
            "SUBMITTED_UNKNOWN" => OrderState::SubmittedUnknown,
            "ACKNOWLEDGED" => OrderState::Acknowledged,
            "PARTIALLY_FILLED" => OrderState::PartiallyFilled,
            "FILLED_UNVERIFIED_LIQUIDATION" => OrderState::FilledUnverifiedLiquidation,
            "FILLED_LIQUIDATION_CONFLICT" => OrderState::FilledLiquidationConflict,
            "FILLED_UNPROTECTED" => OrderState::FilledUnprotected,
            "PROTECTED" => OrderState::Protected,
            "CLOSING" => OrderState::Closing,
            "PARTIALLY_CLOSED" => OrderState::PartiallyClosed,
            "EMERGENCY_CLOSE_FAILED" => OrderState::EmergencyCloseFailed,
            "CLOSED" => OrderState::Closed,
            "CANCELED" => OrderState::Canceled,
            "FAILED" => OrderState::Failed,
            other => return Err(format!("invalid OrderState: {}", other)),
        })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum OrderIntent {
    Entry,
    PositionAdd,
    Close,
    EmergencyClose,
    ManualClose,
    ProtectionSl,
    ProtectionTp,
    GridEntry,
    GridExit,
}

impl OrderIntent {
    pub fn as_str(&self) -> &'static str {
        match self {
            OrderIntent::Entry => "ENTRY",
            OrderIntent::PositionAdd => "POSITION_ADD",
            OrderIntent::Close => "CLOSE",
            OrderIntent::EmergencyClose => "EMERGENCY_CLOSE",
            OrderIntent::ManualClose => "MANUAL_CLOSE",
            OrderIntent::ProtectionSl => "PROTECTION_SL",
            OrderIntent::ProtectionTp => "PROTECTION_TP",
            OrderIntent::GridEntry => "GRID_ENTRY",
            OrderIntent::GridExit => "GRID_EXIT",
        }
    }
}

impl FromStr for OrderIntent {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        Ok(match s {
            "ENTRY" => OrderIntent::Entry,
            "POSITION_ADD" => OrderIntent::PositionAdd,
            "CLOSE" => OrderIntent::Close,
            "EMERGENCY_CLOSE" => OrderIntent::EmergencyClose,
            "MANUAL_CLOSE" => OrderIntent::ManualClose,
            "PROTECTION_SL" => OrderIntent::ProtectionSl,
            "PROTECTION_TP" => OrderIntent::ProtectionTp,
            "GRID_ENTRY" => OrderIntent::GridEntry,
            "GRID_EXIT" => OrderIntent::GridExit,
            other => return Err(format!("invalid OrderIntent: {}", other)),
        })
    }
}

pub fn active_order_states() -> HashSet<&'static str> {
    [
        OrderState::Planned,
        OrderState::Submitting,
        OrderState::SubmittedUnknown,
        OrderState::Acknowledged,
        OrderState::PartiallyFilled,
        OrderState::FilledUnverifiedLiquidation,
        OrderState::FilledLiquidationConflict,
        OrderState::FilledUnprotected,
        OrderState::Protected,
        OrderState::Closing,
        OrderState::PartiallyClosed,
        OrderState::EmergencyCloseFailed,
    ]
    .iter()
    .map(|s| s.as_str())
    .collect()
}

pub fn entry_blocking_states() -> HashSet<&'static str> {
    active_order_states()
}

#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error("sqlite: {0}")]
    Sqlite(#[from] rusqlite::Error),
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
    #[error("{0}")]
    Message(String),
    #[error("unknown client_order_id: {0}")]
    UnknownClientOrderId(String),
}

fn json_default(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "null".to_string(),
        _ => py_json_dumps(v),
    }
}

/// Sort keys and ensure_ascii, like Python's `_safe_json`.
pub fn safe_json(v: &Value) -> String {
    let sorted = sort_value(v);
    // ensure_ascii=True: escape non-ascii to \uXXXX
    let raw = py_json_dumps(&sorted);
    escape_non_ascii(&raw)
}

fn escape_non_ascii(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for ch in s.chars() {
        let cp = ch as u32;
        if cp < 0x80 {
            out.push(ch);
        } else if cp <= 0xFFFF {
            out.push_str(&format!("\\u{:04x}", cp));
        } else {
            let v = cp - 0x10000;
            let hi = 0xD800 + (v >> 10);
            let lo = 0xDC00 + (v & 0x3FF);
            out.push_str(&format!("\\u{:04x}\\u{:04x}", hi, lo));
        }
    }
    out
}

fn sort_value(v: &Value) -> Value {
    match v {
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            let mut out = Map::new();
            for k in keys {
                out.insert(k.clone(), sort_value(&map[k]));
            }
            Value::Object(out)
        }
        Value::Array(arr) => Value::Array(arr.iter().map(sort_value).collect()),
        other => other.clone(),
    }
}

pub fn atomic_write_json(path: &Path, payload: &Value) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("tmp");
    let pid = std::process::id();
    let tid = thread_id_str();
    let temp_name = format!(".{}.{}.{}.tmp", name, pid, tid);
    let temp_path = path.with_file_name(temp_name);
    {
        let file = std::fs::File::create(&temp_path)?;
        let mut writer = std::io::BufWriter::new(file);
        let text = safe_json_pp(payload, 2);
        writer.write_all(text.as_bytes())?;
        writer.flush()?;
        let f = writer.into_inner().map_err(|e| e.into_error())?;
        f.sync_all()?;
    }
    std::fs::rename(&temp_path, path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
        if let Some(parent) = path.parent() {
            if let Ok(dir) = std::fs::File::open(parent) {
                let _ = dir.sync_all();
            }
        }
    }
    if temp_path.exists() {
        let _ = std::fs::remove_file(&temp_path);
    }
    Ok(())
}

fn thread_id_str() -> String {
    let id = std::thread::current().id();
    let s = format!("{:?}", id);
    let digits: String = s.chars().filter(|c| c.is_ascii_digit()).collect();
    if digits.is_empty() {
        "0".to_string()
    } else {
        digits
    }
}

/// Pretty-print with indent, sorted keys. Matches Python json.dump(indent=2, sort_keys=True).
pub fn safe_json_pp(v: &Value, indent: usize) -> String {
    let sorted = sort_value(v);
    let mut out = String::new();
    write_pp(&sorted, &mut out, 0, indent);
    escape_non_ascii(&out)
}

fn write_pp(v: &Value, out: &mut String, level: usize, indent: usize) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                out.push_str(&i.to_string());
            } else if let Some(u) = n.as_u64() {
                out.push_str(&u.to_string());
            } else if let Some(f) = n.as_f64() {
                out.push_str(&py_float_repr(f));
            } else {
                out.push_str(&n.to_string());
            }
        }
        Value::String(s) => write_py_string(s, out),
        Value::Array(arr) => {
            if arr.is_empty() {
                out.push_str("[]");
                return;
            }
            out.push('[');
            for (i, item) in arr.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push('\n');
                for _ in 0..(level + 1) * indent {
                    out.push(' ');
                }
                write_pp(item, out, level + 1, indent);
            }
            out.push('\n');
            for _ in 0..level * indent {
                out.push(' ');
            }
            out.push(']');
        }
        Value::Object(map) => {
            if map.is_empty() {
                out.push_str("{}");
                return;
            }
            out.push('{');
            for (i, (k, val)) in map.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push('\n');
                for _ in 0..(level + 1) * indent {
                    out.push(' ');
                }
                out.push('"');
                for ch in k.chars() {
                    match ch {
                        '"' => out.push_str("\\\""),
                        '\\' => out.push_str("\\\\"),
                        '\n' => out.push_str("\\n"),
                        '\r' => out.push_str("\\r"),
                        '\t' => out.push_str("\\t"),
                        '\u{08}' => out.push_str("\\b"),
                        '\u{0c}' => out.push_str("\\f"),
                        c if (c as u32) < 0x20 => {
                            out.push_str(&format!("\\u{:04x}", c as u32));
                        }
                        c => out.push(c),
                    }
                }
                out.push('"');
                out.push_str(": ");
                write_pp(val, out, level + 1, indent);
            }
            out.push('\n');
            for _ in 0..level * indent {
                out.push(' ');
            }
            out.push('}');
        }
    }
}

fn write_py_string(s: &str, out: &mut String) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

fn _unused_json_default(v: &Value) -> String {
    json_default(v)
}

fn normalize_signal_timestamp(value: Option<&Value>) -> String {
    let v = match value {
        None => return "unknown".to_string(),
        Some(v) => v,
    };
    if let Value::Number(n) = v {
        if let Some(f) = n.as_f64() {
            let mut number = f;
            if number > 10_000_000_000.0 {
                number /= 1000.0;
            }
            return format!("{}", number as i64);
        }
    }
    if let Value::String(s) = v {
        if let Ok(f) = s.parse::<f64>() {
            let mut number = f;
            if number > 10_000_000_000.0 {
                number /= 1000.0;
            }
            return format!("{}", number as i64);
        }
    }
    let text = match v {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        _ => "unknown".to_string(),
    };
    let stripped = if text.is_empty() {
        "unknown".to_string()
    } else {
        text
    };
    let cleaned: String = stripped
        .trim()
        .chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .collect();
    let truncated: String = cleaned.chars().take(16).collect();
    if truncated.is_empty() {
        "unknown".to_string()
    } else {
        truncated
    }
}

pub fn normalize_order_symbol(value: &str) -> String {
    let upper = value.to_uppercase();
    let trimmed = upper.trim();
    let base = match trimmed.split_once(':') {
        Some((head, _)) => head,
        None => trimmed,
    };
    base.chars()
        .filter(|c| c.is_ascii_uppercase() || c.is_ascii_digit())
        .collect()
}

fn normalize_id_component(value: &str) -> String {
    value
        .to_lowercase()
        .chars()
        .filter(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
        .collect()
}

pub struct BuildClientOrderIdArgs<'a> {
    pub strategy: &'a str,
    pub symbol: &'a str,
    pub side: &'a str,
    pub signal_timestamp: Option<&'a Value>,
    pub stage: &'a str,
    pub leg: Option<&'a str>,
    pub revision: Option<&'a str>,
    pub max_length: usize,
}

pub fn build_client_order_id(args: BuildClientOrderIdArgs<'_>) -> String {
    let strategy_full = if args.strategy.is_empty() {
        "bot".to_string()
    } else {
        let v = normalize_id_component(args.strategy);
        if v.is_empty() {
            "bot".to_string()
        } else {
            v
        }
    };
    let sym_norm = normalize_id_component(args.symbol);
    let sym_replaced = sym_norm.replace("usdtusdt", "usdt");
    let symbol_full = if sym_replaced.is_empty() {
        "coin".to_string()
    } else {
        sym_replaced
    };
    let side_norm = normalize_id_component(args.side);
    let side_full = if side_norm.is_empty() {
        "unknown".to_string()
    } else {
        side_norm
    };
    let stage_norm = normalize_id_component(args.stage);
    let stage_full = if stage_norm.is_empty() {
        "entry".to_string()
    } else {
        stage_norm
    };
    let leg_full = args.leg.map(normalize_id_component).unwrap_or_default();
    let revision_full = args
        .revision
        .map(normalize_id_component)
        .unwrap_or_default();
    let timestamp_key = normalize_signal_timestamp(args.signal_timestamp);
    let digest_input = format!(
        "{}|{}|{}|{}|{}|{}|{}",
        strategy_full, symbol_full, side_full, timestamp_key, stage_full, leg_full, revision_full
    );
    let digest = sha_hex12(&digest_input);
    let visible_side = if matches!(side_full.as_str(), "long" | "buy" | "l") {
        "l"
    } else {
        "s"
    };
    let visible_stage: String = stage_full.chars().take(5).collect();
    let visible_leg: String = leg_full.chars().take(3).collect();
    let strategy_prefix: String = strategy_full.chars().take(5).collect();
    let symbol_prefix: String = symbol_full.chars().take(10).collect();
    let mut prefix_parts: Vec<String> = vec![
        strategy_prefix,
        symbol_prefix,
        visible_side.to_string(),
        visible_stage,
    ];
    if !visible_leg.is_empty() {
        prefix_parts.push(visible_leg);
    }
    let prefix = prefix_parts.join("-");
    let mut result = format!("{}-{}", prefix, digest);
    if result.chars().count() > args.max_length {
        let room = (args.max_length as i64 - digest.len() as i64 - 1).max(1) as usize;
        let prefix_trim: String = prefix.chars().take(room).collect();
        result = format!("{}-{}", prefix_trim, digest);
    }
    let filtered: String = result
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || *c == '_' || *c == '-')
        .collect();
    filtered.chars().take(args.max_length).collect()
}

fn sha_hex12(input: &str) -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(input.as_bytes());
    let result = hasher.finalize();
    let hex = hex::encode(result);
    hex.chars().take(12).collect()
}

#[derive(Debug, Clone)]
pub struct OrderRecord {
    pub client_order_id: String,
    pub symbol: String,
    pub side: String,
    pub strategy: String,
    pub signal_timestamp: String,
    pub requested_qty: f64,
    pub order_intent: String,
    pub order_purpose: String,
    pub position_qty_before: f64,
    pub position_signature: Option<String>,
    pub order_state: String,
    pub exchange_order_id: Option<String>,
    pub filled_qty: f64,
    pub average_fill_price: f64,
    pub stop_order_id: Option<String>,
    pub take_profit_order_ids: Vec<String>,
    pub created_at: String,
    pub updated_at: String,
    pub last_error: Option<String>,
    pub retry_count: i64,
    pub metadata: Map<String, Value>,
}

impl OrderRecord {
    pub fn normalized(mut self, now_iso: impl Fn() -> String) -> Self {
        self.order_purpose = if self.order_purpose.is_empty() {
            "entry".to_string()
        } else {
            self.order_purpose.clone()
        };
        self.position_qty_before = self.position_qty_before.abs();
        if self.updated_at.is_empty() {
            self.updated_at = now_iso();
        }
        self
    }

    pub fn to_json_map(&self) -> Map<String, Value> {
        let mut m = Map::new();
        m.insert(
            "client_order_id".into(),
            Value::String(self.client_order_id.clone()),
        );
        m.insert("symbol".into(), Value::String(self.symbol.clone()));
        m.insert("side".into(), Value::String(self.side.clone()));
        m.insert("strategy".into(), Value::String(self.strategy.clone()));
        m.insert(
            "signal_timestamp".into(),
            Value::String(self.signal_timestamp.clone()),
        );
        m.insert("requested_qty".into(), Value::from(self.requested_qty));
        m.insert(
            "order_intent".into(),
            Value::String(self.order_intent.clone()),
        );
        m.insert(
            "order_purpose".into(),
            Value::String(self.order_purpose.clone()),
        );
        m.insert(
            "position_qty_before".into(),
            Value::from(self.position_qty_before),
        );
        m.insert(
            "position_signature".into(),
            self.position_signature
                .clone()
                .map(Value::String)
                .unwrap_or(Value::Null),
        );
        m.insert(
            "order_state".into(),
            Value::String(self.order_state.clone()),
        );
        m.insert(
            "exchange_order_id".into(),
            self.exchange_order_id
                .clone()
                .map(Value::String)
                .unwrap_or(Value::Null),
        );
        m.insert("filled_qty".into(), Value::from(self.filled_qty));
        m.insert(
            "average_fill_price".into(),
            Value::from(self.average_fill_price),
        );
        m.insert(
            "stop_order_id".into(),
            self.stop_order_id
                .clone()
                .map(Value::String)
                .unwrap_or(Value::Null),
        );
        m.insert(
            "take_profit_order_ids".into(),
            Value::Array(
                self.take_profit_order_ids
                    .iter()
                    .map(|s| Value::String(s.clone()))
                    .collect(),
            ),
        );
        m.insert("created_at".into(), Value::String(self.created_at.clone()));
        m.insert("updated_at".into(), Value::String(self.updated_at.clone()));
        m.insert(
            "last_error".into(),
            self.last_error
                .clone()
                .map(Value::String)
                .unwrap_or(Value::Null),
        );
        m.insert("retry_count".into(), Value::from(self.retry_count));
        m.insert("metadata".into(), Value::Object(self.metadata.clone()));
        m
    }
}

pub struct SQLiteTradingStateStore {
    conn: Connection,
    clock: Arc<dyn Clock>,
}

impl SQLiteTradingStateStore {
    pub fn open<P: AsRef<Path>>(path: P, clock: Arc<dyn Clock>) -> Result<Self, StoreError> {
        let path = path.as_ref();
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)?;
            }
        }
        let conn = Connection::open(path)?;
        conn.execute_batch(
            "PRAGMA journal_mode=WAL;
             PRAGMA synchronous=FULL;
             PRAGMA foreign_keys=ON;",
        )?;
        let store = Self { conn, clock };
        store.create_schema()?;
        Ok(store)
    }

    fn create_schema(&self) -> Result<(), StoreError> {
        self.conn.execute_batch(
            "
            CREATE TABLE IF NOT EXISTS crypto_orders (
                client_order_id TEXT PRIMARY KEY,
                exchange_order_id TEXT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                strategy TEXT NOT NULL,
                signal_timestamp TEXT NOT NULL,
                requested_qty REAL NOT NULL,
                order_intent TEXT NOT NULL DEFAULT 'ENTRY',
                order_purpose TEXT NOT NULL DEFAULT 'entry',
                position_qty_before REAL NOT NULL DEFAULT 0,
                position_signature TEXT,
                filled_qty REAL NOT NULL DEFAULT 0,
                average_fill_price REAL NOT NULL DEFAULT 0,
                order_state TEXT NOT NULL,
                stop_order_id TEXT,
                take_profit_order_ids_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_error TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_crypto_orders_symbol_state
                ON crypto_orders(symbol, order_state);
            CREATE TABLE IF NOT EXISTS order_state_audit (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_order_id TEXT NOT NULL,
                old_state TEXT,
                new_state TEXT NOT NULL,
                exchange_order_id TEXT,
                symbol TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS runtime_safety_state (
                state_key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS live_trade_results (
                trade_id TEXT PRIMARY KEY,
                client_order_id TEXT,
                strategy TEXT,
                engine TEXT,
                symbol TEXT NOT NULL,
                side TEXT,
                exit_time TEXT,
                payload_json TEXT NOT NULL,
                provisional INTEGER NOT NULL DEFAULT 0,
                finalized_at TEXT,
                accounting_revision INTEGER NOT NULL DEFAULT 0,
                last_accounting_error TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS global_entry_lease (
                lease_id INTEGER PRIMARY KEY CHECK (lease_id = 1),
                owner_id TEXT NOT NULL,
                acquired_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                symbol TEXT NOT NULL,
                client_order_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS order_event_high_water (
                event_scope TEXT PRIMARY KEY,
                client_order_id TEXT,
                exchange_order_id TEXT,
                last_event_time INTEGER NOT NULL DEFAULT 0,
                last_transaction_time INTEGER NOT NULL DEFAULT 0,
                last_status TEXT,
                last_cumulative_filled_qty REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            ",
        )?;
        self.migrate_schema()?;
        Ok(())
    }

    fn table_columns(&self, table: &str) -> Result<HashSet<String>, StoreError> {
        let valid = !table.is_empty()
            && table
                .chars()
                .next()
                .map(|c| c.is_ascii_alphabetic() || c == '_')
                .unwrap_or(false)
            && table.chars().all(|c| c.is_ascii_alphanumeric() || c == '_');
        if !valid {
            return Err(StoreError::Message(format!(
                "invalid SQLite table name: {}",
                table
            )));
        }
        let mut stmt = self
            .conn
            .prepare(&format!("PRAGMA table_info({})", table))?;
        let mut rows = stmt.query([])?;
        let mut out = HashSet::new();
        while let Some(row) = rows.next()? {
            let name: String = row.get(1)?;
            out.insert(name);
        }
        Ok(out)
    }

    fn migrate_schema(&self) -> Result<(), StoreError> {
        let migrations: &[(&str, &[(&str, &str)])] = &[
            (
                "crypto_orders",
                &[
                    ("order_intent", "TEXT NOT NULL DEFAULT 'ENTRY'"),
                    ("order_purpose", "TEXT NOT NULL DEFAULT 'entry'"),
                    ("position_qty_before", "REAL NOT NULL DEFAULT 0"),
                    ("position_signature", "TEXT"),
                ],
            ),
            (
                "live_trade_results",
                &[
                    ("finalized_at", "TEXT"),
                    ("accounting_revision", "INTEGER NOT NULL DEFAULT 0"),
                    ("last_accounting_error", "TEXT"),
                ],
            ),
        ];
        for (table, columns) in migrations {
            let existing = self.table_columns(table)?;
            for (name, def) in *columns {
                if existing.contains(*name) {
                    continue;
                }
                self.conn.execute(
                    &format!("ALTER TABLE {} ADD COLUMN {} {}", table, name, def),
                    [],
                )?;
            }
        }
        Ok(())
    }

    fn now_iso(&self) -> String {
        py_isoformat(&self.clock.now_utc())
    }

    fn row_to_record(&self, row: &Row<'_>) -> Result<OrderRecord, rusqlite::Error> {
        let take_profit_json: String = row.get("take_profit_order_ids_json")?;
        let metadata_json: String = row.get("metadata_json")?;
        let take_profit_order_ids: Vec<String> =
            serde_json::from_str(&take_profit_json).unwrap_or_default();
        let metadata: Map<String, Value> = serde_json::from_str(&metadata_json).unwrap_or_default();
        Ok(OrderRecord {
            client_order_id: row.get("client_order_id")?,
            exchange_order_id: row.get("exchange_order_id")?,
            symbol: row.get("symbol")?,
            side: row.get("side")?,
            strategy: row.get("strategy")?,
            signal_timestamp: row.get("signal_timestamp")?,
            requested_qty: row.get::<_, f64>("requested_qty")?,
            order_intent: row.get("order_intent")?,
            order_purpose: row.get("order_purpose")?,
            position_qty_before: row.get::<_, f64>("position_qty_before")?,
            position_signature: row.get("position_signature")?,
            filled_qty: row.get::<_, f64>("filled_qty")?,
            average_fill_price: row.get::<_, f64>("average_fill_price")?,
            order_state: row.get("order_state")?,
            stop_order_id: row.get("stop_order_id")?,
            take_profit_order_ids,
            created_at: row.get("created_at")?,
            updated_at: row.get("updated_at")?,
            last_error: row.get("last_error")?,
            retry_count: row.get::<_, i64>("retry_count")?,
            metadata,
        })
    }

    pub fn get(&self, client_order_id: &str) -> Result<Option<OrderRecord>, StoreError> {
        let mut stmt = self
            .conn
            .prepare("SELECT * FROM crypto_orders WHERE client_order_id = ?")?;
        let mut rows = stmt.query(params![client_order_id])?;
        if let Some(row) = rows.next()? {
            Ok(Some(self.row_to_record(row)?))
        } else {
            Ok(None)
        }
    }

    pub fn upsert(&mut self, record: OrderRecord) -> Result<OrderRecord, StoreError> {
        let clock = self.clock.clone();
        let mut rec = record.normalized(|| py_isoformat(&clock.now_utc()));
        let previous = self.get(&rec.client_order_id)?;
        rec.updated_at = self.now_iso();
        if let (Some(prev), true) = (previous.as_ref(), rec.created_at.is_empty()) {
            rec.created_at = prev.created_at.clone();
        }
        let tp_json = safe_json(&Value::Array(
            rec.take_profit_order_ids
                .iter()
                .map(|s| Value::String(s.clone()))
                .collect(),
        ));
        let meta_json = safe_json(&Value::Object(rec.metadata.clone()));
        let tx = self.conn.transaction()?;
        {
            let mut stmt = tx.prepare(
                "
                INSERT INTO crypto_orders (
                    client_order_id, exchange_order_id, symbol, side, strategy,
                    signal_timestamp, requested_qty, order_intent, order_purpose,
                    position_qty_before, position_signature, filled_qty, average_fill_price,
                    order_state, stop_order_id, take_profit_order_ids_json, created_at,
                    updated_at, last_error, retry_count, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_order_id) DO UPDATE SET
                    exchange_order_id=excluded.exchange_order_id,
                    symbol=excluded.symbol,
                    side=excluded.side,
                    strategy=excluded.strategy,
                    signal_timestamp=excluded.signal_timestamp,
                    requested_qty=excluded.requested_qty,
                    order_intent=excluded.order_intent,
                    order_purpose=excluded.order_purpose,
                    position_qty_before=excluded.position_qty_before,
                    position_signature=excluded.position_signature,
                    filled_qty=excluded.filled_qty,
                    average_fill_price=excluded.average_fill_price,
                    order_state=excluded.order_state,
                    stop_order_id=excluded.stop_order_id,
                    take_profit_order_ids_json=excluded.take_profit_order_ids_json,
                    updated_at=excluded.updated_at,
                    last_error=excluded.last_error,
                    retry_count=excluded.retry_count,
                    metadata_json=excluded.metadata_json
                ",
            )?;
            stmt.execute(params![
                rec.client_order_id,
                rec.exchange_order_id,
                rec.symbol,
                rec.side,
                rec.strategy,
                rec.signal_timestamp,
                rec.requested_qty,
                rec.order_intent,
                rec.order_purpose,
                rec.position_qty_before,
                rec.position_signature,
                rec.filled_qty,
                rec.average_fill_price,
                rec.order_state,
                rec.stop_order_id,
                tp_json,
                rec.created_at,
                rec.updated_at,
                rec.last_error,
                rec.retry_count,
                meta_json,
            ])?;
        }
        let changed = match &previous {
            None => true,
            Some(p) => p.order_state != rec.order_state,
        };
        if changed {
            Self::insert_audit(&tx, previous.as_ref(), &rec, &Map::new())?;
        }
        tx.commit()?;
        Ok(rec)
    }

    fn insert_audit(
        conn: &rusqlite::Transaction<'_>,
        previous: Option<&OrderRecord>,
        record: &OrderRecord,
        detail: &Map<String, Value>,
    ) -> Result<(), StoreError> {
        let detail_json = safe_json(&Value::Object(detail.clone()));
        let old_state = previous.map(|p| p.order_state.clone());
        conn.execute(
            "
            INSERT INTO order_state_audit (
                client_order_id, old_state, new_state, exchange_order_id,
                symbol, timestamp, detail_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ",
            params![
                record.client_order_id,
                old_state,
                record.order_state,
                record.exchange_order_id,
                record.symbol,
                record.updated_at,
                detail_json,
            ],
        )?;
        Ok(())
    }

    pub fn transition(
        &mut self,
        client_order_id: &str,
        new_state: &str,
        changes: Map<String, Value>,
    ) -> Result<OrderRecord, StoreError> {
        let record = self.get(client_order_id)?;
        let mut record = match record {
            Some(r) => r,
            None => {
                return Err(StoreError::UnknownClientOrderId(
                    client_order_id.to_string(),
                ));
            }
        };
        let old = record.clone();
        record.order_state = new_state.to_string();
        for (key, value) in changes.iter() {
            match key.as_str() {
                "client_order_id" => {
                    record.client_order_id = value_as_string(value);
                }
                "symbol" => {
                    record.symbol = value_as_string(value);
                }
                "side" => {
                    record.side = value_as_string(value);
                }
                "strategy" => {
                    record.strategy = value_as_string(value);
                }
                "signal_timestamp" => {
                    record.signal_timestamp = value_as_string(value);
                }
                "requested_qty" => {
                    record.requested_qty = value_as_f64(value);
                }
                "order_intent" => {
                    record.order_intent = value_as_string(value);
                }
                "order_purpose" => {
                    record.order_purpose = value_as_string(value);
                }
                "position_qty_before" => {
                    record.position_qty_before = value_as_f64(value);
                }
                "position_signature" => {
                    record.position_signature = value_as_opt_string(value);
                }
                "order_state" => {
                    record.order_state = value_as_string(value);
                }
                "exchange_order_id" => {
                    record.exchange_order_id = value_as_opt_string(value);
                }
                "filled_qty" => {
                    record.filled_qty = value_as_f64(value);
                }
                "average_fill_price" => {
                    record.average_fill_price = value_as_f64(value);
                }
                "stop_order_id" => {
                    record.stop_order_id = value_as_opt_string(value);
                }
                "take_profit_order_ids" => {
                    record.take_profit_order_ids = match value {
                        Value::Array(arr) => arr.iter().map(value_as_string).collect(),
                        _ => Vec::new(),
                    };
                }
                "created_at" => {
                    record.created_at = value_as_string(value);
                }
                "updated_at" => {
                    record.updated_at = value_as_string(value);
                }
                "last_error" => {
                    record.last_error = value_as_opt_string(value);
                }
                "retry_count" => {
                    record.retry_count = value_as_i64(value);
                }
                "metadata" => {
                    record.metadata = match value {
                        Value::Object(m) => m.clone(),
                        _ => Map::new(),
                    };
                }
                _ => {
                    record.metadata.insert(key.clone(), value.clone());
                }
            }
        }
        record.updated_at = self.now_iso();
        let tp_json = safe_json(&Value::Array(
            record
                .take_profit_order_ids
                .iter()
                .map(|s| Value::String(s.clone()))
                .collect(),
        ));
        let meta_json = safe_json(&Value::Object(record.metadata.clone()));
        let tx = self.conn.transaction()?;
        tx.execute(
            "
            UPDATE crypto_orders SET
                exchange_order_id=?, filled_qty=?, average_fill_price=?, order_state=?,
                order_intent=?, order_purpose=?, position_qty_before=?, position_signature=?,
                stop_order_id=?, take_profit_order_ids_json=?, updated_at=?, last_error=?,
                retry_count=?, metadata_json=? WHERE client_order_id=?
            ",
            params![
                record.exchange_order_id,
                record.filled_qty,
                record.average_fill_price,
                record.order_state,
                record.order_intent,
                record.order_purpose,
                record.position_qty_before,
                record.position_signature,
                record.stop_order_id,
                tp_json,
                record.updated_at,
                record.last_error,
                record.retry_count,
                meta_json,
                record.client_order_id,
            ],
        )?;
        if old.order_state != record.order_state {
            Self::insert_audit(&tx, Some(&old), &record, &changes)?;
        }
        tx.commit()?;
        Ok(record)
    }

    pub fn list_by_states(&self, states: &[&str]) -> Result<Vec<OrderRecord>, StoreError> {
        if states.is_empty() {
            return Ok(Vec::new());
        }
        let placeholders = states.iter().map(|_| "?").collect::<Vec<_>>().join(",");
        let sql = format!(
            "SELECT * FROM crypto_orders WHERE order_state IN ({}) ORDER BY created_at",
            placeholders
        );
        let mut stmt = self.conn.prepare(&sql)?;
        let params_ref: Vec<&dyn rusqlite::ToSql> =
            states.iter().map(|s| s as &dyn rusqlite::ToSql).collect();
        let mut rows = stmt.query(params_ref.as_slice())?;
        let mut out = Vec::new();
        while let Some(row) = rows.next()? {
            out.push(self.row_to_record(row)?);
        }
        Ok(out)
    }

    pub fn records_for_symbol(&self, symbol: &str) -> Result<Vec<OrderRecord>, StoreError> {
        let normalized = normalize_order_symbol(symbol);
        let mut stmt = self
            .conn
            .prepare("SELECT * FROM crypto_orders ORDER BY created_at")?;
        let mut rows = stmt.query([])?;
        let mut out = Vec::new();
        while let Some(row) = rows.next()? {
            let rec = self.row_to_record(row)?;
            if normalize_order_symbol(&rec.symbol) == normalized {
                out.push(rec);
            }
        }
        Ok(out)
    }

    pub fn active_for_symbol(&self, symbol: &str) -> Result<Vec<OrderRecord>, StoreError> {
        let normalized = normalize_order_symbol(symbol);
        let active = active_order_states();
        let states: Vec<&str> = active.iter().copied().collect();
        let records = self.list_by_states(&states)?;
        Ok(records
            .into_iter()
            .filter(|r| normalize_order_symbol(&r.symbol) == normalized)
            .collect())
    }

    pub fn entry_block_reason(
        &self,
        symbol: Option<&str>,
        for_position_add: bool,
        ignore_daily_loss_lock: bool,
    ) -> Result<Option<String>, StoreError> {
        let runtime_lock = self.get_runtime_state("entry_lock_reason", None)?;
        if let Some(v) = &runtime_lock {
            if !v.is_null() {
                let text = match v {
                    Value::String(s) => s.clone(),
                    _ => v.to_string(),
                };
                let trimmed = text.trim().to_string();
                return Ok(Some(if trimmed.is_empty() {
                    "RUNTIME_ENTRY_LOCK:INVALID_PERSISTED_STATE".to_string()
                } else {
                    format!("RUNTIME_ENTRY_LOCK:{}", trimmed)
                }));
            }
        }
        if !ignore_daily_loss_lock {
            if let Some(lock) = self.daily_loss_entry_lock_reason(None)? {
                return Ok(Some(lock));
            }
        }
        let blocking = entry_blocking_states();
        let state_list: Vec<&str> = blocking.iter().copied().collect();
        let mut records = self.list_by_states(&state_list)?;
        if for_position_add {
            if let Some(sym) = symbol {
                let normalized = normalize_order_symbol(sym);
                records.retain(|r| {
                    let sym_match = normalize_order_symbol(&r.symbol) == normalized;
                    let is_protected = r.order_state == OrderState::Protected.as_str();
                    let intent_ok = r.order_intent == OrderIntent::Entry.as_str()
                        || r.order_intent == OrderIntent::PositionAdd.as_str();
                    !(sym_match && is_protected && intent_ok)
                });
            }
        }
        if records.is_empty() {
            return Ok(None);
        }
        let selected = if let Some(sym) = symbol {
            let normalized = normalize_order_symbol(sym);
            let same: Vec<&OrderRecord> = records
                .iter()
                .filter(|r| normalize_order_symbol(&r.symbol) == normalized)
                .collect();
            if !same.is_empty() {
                same[0].clone()
            } else {
                records[0].clone()
            }
        } else {
            records[0].clone()
        };
        Ok(Some(format!(
            "{}:{}:{}",
            selected.order_state, selected.symbol, selected.client_order_id
        )))
    }

    pub fn daily_loss_entry_lock_reason(
        &self,
        current_date: Option<&str>,
    ) -> Result<Option<String>, StoreError> {
        let payload = self.get_runtime_state(DAILY_LOSS_ENTRY_LOCK_KEY, None)?;
        let payload = match payload {
            None => return Ok(None),
            Some(v) if v.is_null() => return Ok(None),
            Some(v) => v,
        };
        let map = match &payload {
            Value::Object(m) => m,
            _ => {
                return Ok(Some(
                    "DAILY_LOSS_LOCKED:INVALID_PERSISTED_STATE".to_string(),
                ))
            }
        };
        let today = match current_date {
            Some(d) => d.to_string(),
            None => self.clock.now_utc().format("%Y-%m-%d").to_string(),
        };
        let lock_date = map
            .get("date")
            .map(value_as_string)
            .unwrap_or_default()
            .trim()
            .to_string();
        if lock_date != today {
            self.delete_runtime_state(DAILY_LOSS_ENTRY_LOCK_KEY)?;
            return Ok(None);
        }
        let reason = match map.get("reason") {
            Some(v) if !v.is_null() => value_as_string(v),
            _ => "DAILY_LOSS_LIMIT".to_string(),
        };
        let reason = reason.trim().to_string();
        Ok(Some(format!("DAILY_LOSS_LOCKED:{}:{}", lock_date, reason)))
    }

    pub fn set_runtime_state(&self, key: &str, value: &Value) -> Result<(), StoreError> {
        let now = self.now_iso();
        let json = safe_json(value);
        self.conn.execute(
            "
            INSERT INTO runtime_safety_state(state_key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(state_key) DO UPDATE SET
                value_json=excluded.value_json, updated_at=excluded.updated_at
            ",
            params![key, json, now],
        )?;
        Ok(())
    }

    pub fn get_runtime_state(
        &self,
        key: &str,
        default: Option<Value>,
    ) -> Result<Option<Value>, StoreError> {
        let value: Option<String> = self
            .conn
            .query_row(
                "SELECT value_json FROM runtime_safety_state WHERE state_key=?",
                params![key],
                |r| r.get(0),
            )
            .optional()?;
        let raw = match value {
            Some(v) => v,
            None => return Ok(default),
        };
        match serde_json::from_str::<Value>(&raw) {
            Ok(v) => Ok(Some(v)),
            Err(_) => Ok(default),
        }
    }

    pub fn create_runtime_state_if_absent(
        &mut self,
        key: &str,
        value: &Value,
    ) -> Result<bool, StoreError> {
        let now = self.now_iso();
        let json = safe_json(value);
        let tx = self.conn.transaction()?;
        let inserted = tx.execute(
            "INSERT OR IGNORE INTO runtime_safety_state(state_key, value_json, updated_at) VALUES (?, ?, ?)",
            params![key, json, now],
        )? == 1;
        tx.commit()?;
        Ok(inserted)
    }

    pub fn delete_runtime_state(&self, key: &str) -> Result<bool, StoreError> {
        let n = self.conn.execute(
            "DELETE FROM runtime_safety_state WHERE state_key = ?",
            params![key],
        )?;
        Ok(n == 1)
    }

    pub fn try_acquire_entry_lease(
        &mut self,
        owner_id: &str,
        ttl_seconds: f64,
        symbol: &str,
        client_order_id: &str,
        reconciliation_confirmed: bool,
        allow_same_symbol: bool,
    ) -> Result<bool, StoreError> {
        let now = self.clock.now_epoch_secs();
        // Python: max(1.0, float(ttl_seconds or 0.0)); f64::max also maps NaN to 1.0.
        let expires_at = now + ttl_seconds.max(1.0);
        let tx = self.conn.transaction()?;
        let existing: Option<(String, String, f64, String)> = tx
            .query_row(
                "SELECT owner_id, symbol, expires_at, client_order_id FROM global_entry_lease WHERE lease_id=1",
                [],
                |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, f64>(2)?,
                        r.get::<_, String>(3)?,
                    ))
                },
            )
            .optional()?;
        if let Some((_owner, row_symbol, row_expires, row_client)) = existing {
            if row_client == client_order_id {
                tx.commit()?;
                return Ok(true);
            }
            if allow_same_symbol
                && normalize_order_symbol(&row_symbol) == normalize_order_symbol(symbol)
            {
                tx.commit()?;
                return Ok(true);
            }
            let expired = row_expires <= now;
            if !(expired && reconciliation_confirmed) {
                tx.commit()?;
                return Ok(false);
            }
            tx.execute("DELETE FROM global_entry_lease WHERE lease_id=1", [])?;
        }
        tx.execute(
            "
            INSERT INTO global_entry_lease(
                lease_id, owner_id, acquired_at, expires_at, symbol, client_order_id
            ) VALUES (1, ?, ?, ?, ?, ?)
            ",
            params![owner_id, now, expires_at, symbol, client_order_id],
        )?;
        tx.commit()?;
        Ok(true)
    }

    pub fn get_entry_lease(&self) -> Result<Option<Map<String, Value>>, StoreError> {
        let row: Option<(String, f64, f64, String, String)> = self
            .conn
            .query_row(
                "SELECT owner_id, acquired_at, expires_at, symbol, client_order_id FROM global_entry_lease WHERE lease_id=1",
                [],
                |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, f64>(1)?,
                        r.get::<_, f64>(2)?,
                        r.get::<_, String>(3)?,
                        r.get::<_, String>(4)?,
                    ))
                },
            )
            .optional()?;
        match row {
            None => Ok(None),
            Some((owner, acq, exp, sym, cid)) => {
                let mut m = Map::new();
                m.insert("lease_id".into(), Value::from(1));
                m.insert("owner_id".into(), Value::String(owner));
                m.insert("acquired_at".into(), Value::from(acq));
                m.insert("expires_at".into(), Value::from(exp));
                m.insert("symbol".into(), Value::String(sym));
                m.insert("client_order_id".into(), Value::String(cid));
                Ok(Some(m))
            }
        }
    }

    pub fn release_entry_lease(
        &self,
        client_order_id: &str,
        final_state: &str,
    ) -> Result<bool, StoreError> {
        if !matches!(final_state, "FAILED" | "CANCELED" | "CLOSED") {
            return Ok(false);
        }
        let n = self.conn.execute(
            "DELETE FROM global_entry_lease WHERE lease_id=1 AND client_order_id=?",
            params![client_order_id],
        )?;
        Ok(n > 0)
    }

    pub fn release_entry_lease_for_symbol(
        &self,
        symbol: &str,
        reconciliation_confirmed: bool,
    ) -> Result<bool, StoreError> {
        if !reconciliation_confirmed {
            return Ok(false);
        }
        let lease = match self.get_entry_lease()? {
            Some(l) => l,
            None => return Ok(false),
        };
        let lease_sym = lease.get("symbol").map(value_as_string).unwrap_or_default();
        if normalize_order_symbol(&lease_sym) != normalize_order_symbol(symbol) {
            return Ok(false);
        }
        let n = self
            .conn
            .execute("DELETE FROM global_entry_lease WHERE lease_id=1", [])?;
        Ok(n > 0)
    }
}

fn value_as_string(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".to_string(),
        Value::Bool(b) => {
            if *b {
                "True".to_string()
            } else {
                "False".to_string()
            }
        }
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i.to_string()
            } else if let Some(u) = n.as_u64() {
                u.to_string()
            } else if let Some(f) = n.as_f64() {
                py_float_repr(f)
            } else {
                n.to_string()
            }
        }
        Value::Array(_) | Value::Object(_) => py_json_dumps(v),
    }
}

fn value_as_opt_string(v: &Value) -> Option<String> {
    match v {
        Value::Null => None,
        _ => Some(value_as_string(v)),
    }
}

fn value_as_f64(v: &Value) -> f64 {
    match v {
        Value::Number(n) => n.as_f64().unwrap_or(0.0),
        Value::String(s) => s.parse::<f64>().unwrap_or(0.0),
        Value::Bool(b) if *b => 1.0,
        _ => 0.0,
    }
}

fn value_as_i64(v: &Value) -> i64 {
    match v {
        Value::Number(n) => n.as_i64().unwrap_or(0),
        Value::String(s) => s.parse::<i64>().unwrap_or(0),
        _ => 0,
    }
}

// Suppress unused warnings for helpers kept for parity.
#[allow(dead_code)]
fn _touch() {
    let _: Option<()> = HashMap::<String, ()>::new().insert("".to_string(), ());
    let _ = Utc::now;
    let _ = _unused_json_default;
}
