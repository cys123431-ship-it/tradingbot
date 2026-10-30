use std::collections::HashSet;
use std::fs::OpenOptions;
use std::io::Write;
use std::path::Path;
use std::sync::Arc;

use chrono::{DateTime, Duration, NaiveDate, TimeZone, Utc};
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{Map, Value};

use super::clock::{py_isoformat, Clock};

pub const TRADE_COLUMNS: &[&str] = &[
    "trade_id",
    "signal_id",
    "mode",
    "network",
    "status",
    "entry_time",
    "exit_time",
    "side",
    "quantity",
    "entry_price",
    "stop_price",
    "take_profit_price",
    "exit_price",
    "gross_pnl",
    "commission",
    "funding",
    "net_pnl",
    "pnl_pct",
    "r_multiple",
    "initial_risk",
    "result",
    "exit_reason",
    "rule_violation",
    "strategy_version",
    "extra",
];

#[derive(Debug, thiserror::Error)]
pub enum LedgerError {
    #[error("sqlite: {0}")]
    Sqlite(#[from] rusqlite::Error),
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
}

/// Serialize like Python `json.dumps(..., ensure_ascii=False)` default separators.
pub fn py_json_dumps(v: &Value) -> String {
    let mut out = String::new();
    write_py_json(v, &mut out);
    out
}

fn write_py_json(v: &Value, out: &mut String) {
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
            out.push('[');
            for (i, item) in arr.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write_py_json(item, out);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            for (i, (k, val)) in map.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write_py_string(k, out);
                out.push_str(": ");
                write_py_json(val, out);
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

/// Python `repr(float)`: integers get ".0", others shortest round-trip.
pub fn py_float_repr(f: f64) -> String {
    if f.is_nan() {
        return "nan".to_string();
    }
    if f.is_infinite() {
        return if f > 0.0 { "inf".into() } else { "-inf".into() };
    }
    let s = format!("{}", f);
    if s.contains('.') || s.contains('e') || s.contains('E') {
        s
    } else {
        format!("{}.0", s)
    }
}

/// Python `str(value)` for arbitrary JSON-ish values used in upsert_trade.
pub fn py_str_of_value(v: &Value) -> Option<String> {
    match v {
        Value::Null => None,
        Value::String(s) => Some(s.clone()),
        Value::Bool(b) => Some(if *b { "True".into() } else { "False".into() }),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Some(i.to_string())
            } else if let Some(u) = n.as_u64() {
                Some(u.to_string())
            } else if let Some(f) = n.as_f64() {
                Some(py_float_repr(f))
            } else {
                Some(n.to_string())
            }
        }
        Value::Array(_) | Value::Object(_) => Some(py_json_dumps(v)),
    }
}

pub struct StrategyLedger {
    conn: Connection,
    events_path: std::path::PathBuf,
    clock: Arc<dyn Clock>,
    strategy_version: String,
}

impl StrategyLedger {
    pub fn open<P: AsRef<Path>, Q: AsRef<Path>>(
        db_path: P,
        events_path: Q,
        clock: Arc<dyn Clock>,
        strategy_version: &str,
    ) -> Result<Self, LedgerError> {
        let db_path = db_path.as_ref();
        let events_path = events_path.as_ref();
        if let Some(parent) = db_path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        if let Some(parent) = events_path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let conn = Connection::open(db_path)?;
        let mut cols: Vec<String> = Vec::new();
        for c in TRADE_COLUMNS {
            if *c == "trade_id" {
                cols.push("trade_id TEXT PRIMARY KEY".to_string());
            } else {
                cols.push(format!("{} TEXT", c));
            }
        }
        conn.execute(
            &format!("CREATE TABLE IF NOT EXISTS trades ({})", cols.join(", ")),
            [],
        )?;
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)",
            [],
        )?;
        Ok(Self {
            conn,
            events_path: events_path.to_path_buf(),
            clock,
            strategy_version: strategy_version.to_string(),
        })
    }

    pub fn get_state(&self, key: &str, default: Option<Value>) -> Option<Value> {
        let value: Option<String> = self
            .conn
            .query_row("SELECT value FROM kv WHERE key=?", params![key], |r| {
                r.get(0)
            })
            .optional()
            .ok()
            .flatten();
        let raw = match value {
            Some(v) => v,
            None => return default,
        };
        match serde_json::from_str::<Value>(&raw) {
            Ok(v) => Some(v),
            Err(_) => default,
        }
    }

    pub fn set_state(&self, key: &str, value: &Value) -> Result<(), LedgerError> {
        let payload = py_json_dumps(value);
        self.conn.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            params![key, payload],
        )?;
        Ok(())
    }

    pub fn event(&self, kind: &str, fields: Map<String, Value>) -> Value {
        let mut record = Map::new();
        record.insert(
            "ts".into(),
            Value::String(py_isoformat(&self.clock.now_utc())),
        );
        record.insert("event".into(), Value::String(kind.to_string()));
        for (k, v) in fields {
            record.insert(k, v);
        }
        let line = py_json_dumps(&Value::Object(record.clone()));
        if let Ok(mut handle) = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.events_path)
        {
            let _ = writeln!(handle, "{}", line);
        }
        Value::Object(record)
    }

    pub fn recent_events(&self, limit: usize, kinds: Option<&HashSet<String>>) -> Vec<Value> {
        let text = match std::fs::read_to_string(&self.events_path) {
            Ok(t) => t,
            Err(_) => return Vec::new(),
        };
        let lines: Vec<&str> = text.lines().collect();
        let start = lines.len().saturating_sub(2000);
        let mut rows: Vec<Value> = Vec::new();
        for line in lines[start..].iter().rev() {
            let row: Value = match serde_json::from_str(line) {
                Ok(v) => v,
                Err(_) => continue,
            };
            if let Some(filter) = kinds {
                let ev = row.get("event").and_then(|v| v.as_str()).unwrap_or("");
                if !filter.contains(ev) {
                    continue;
                }
            }
            rows.push(row);
            if rows.len() >= limit {
                break;
            }
        }
        rows
    }

    pub fn upsert_trade(&self, trade: &Map<String, Value>) -> Result<(), LedgerError> {
        let mut row: Map<String, Value> = Map::new();
        for c in TRADE_COLUMNS {
            let v = trade.get(*c).cloned().unwrap_or(Value::Null);
            row.insert((*c).to_string(), v);
        }
        let sv_empty = row
            .get("strategy_version")
            .map(|v| match v {
                Value::Null => true,
                Value::String(s) => s.is_empty(),
                _ => false,
            })
            .unwrap_or(true);
        if sv_empty {
            row.insert(
                "strategy_version".into(),
                Value::String(self.strategy_version.clone()),
            );
        }
        // Apply py str() rules, but keep nulls as null.
        for (k, v) in row.iter_mut() {
            if v.is_null() {
                continue;
            }
            let as_str = py_str_of_value(v);
            if let Some(s) = as_str {
                *v = Value::String(s);
            }
            let _ = k;
        }
        let columns = TRADE_COLUMNS.join(", ");
        let marks = TRADE_COLUMNS
            .iter()
            .map(|_| "?")
            .collect::<Vec<_>>()
            .join(", ");
        let updates = TRADE_COLUMNS
            .iter()
            .filter(|c| **c != "trade_id")
            .map(|c| format!("{}=COALESCE(excluded.{}, trades.{})", c, c, c))
            .collect::<Vec<_>>()
            .join(", ");
        let sql = format!(
            "INSERT INTO trades({}) VALUES({}) ON CONFLICT(trade_id) DO UPDATE SET {}",
            columns, marks, updates
        );
        let values: Vec<Option<String>> = TRADE_COLUMNS
            .iter()
            .map(|c| row.get(*c).and_then(|v| v.as_str().map(|s| s.to_string())))
            .collect();
        let params_ref: Vec<&dyn rusqlite::ToSql> =
            values.iter().map(|v| v as &dyn rusqlite::ToSql).collect();
        self.conn.execute(&sql, params_ref.as_slice())?;
        Ok(())
    }

    pub fn get_trade(&self, trade_id: &str) -> Result<Option<Map<String, Value>>, LedgerError> {
        let mut stmt = self.conn.prepare("SELECT * FROM trades WHERE trade_id=?")?;
        let col_names: Vec<String> = stmt.column_names().iter().map(|s| s.to_string()).collect();
        let mut rows = stmt.query(params![trade_id])?;
        if let Some(row) = rows.next()? {
            let mut out = Map::new();
            for (i, name) in col_names.iter().enumerate() {
                let v: Option<String> = row.get(i)?;
                out.insert(name.clone(), v.map(Value::String).unwrap_or(Value::Null));
            }
            Ok(Some(out))
        } else {
            Ok(None)
        }
    }

    pub fn trades(
        &self,
        mode: Option<&str>,
        network: Option<&str>,
        since: Option<DateTime<Utc>>,
        until: Option<DateTime<Utc>>,
        status: Option<&str>,
    ) -> Result<Vec<Map<String, Value>>, LedgerError> {
        let mut query = "SELECT * FROM trades WHERE 1=1".to_string();
        let mut args: Vec<String> = Vec::new();
        if let Some(v) = mode {
            query.push_str(" AND mode=?");
            args.push(v.to_string());
        }
        if let Some(v) = network {
            query.push_str(" AND network=?");
            args.push(v.to_string());
        }
        if let Some(v) = status {
            query.push_str(" AND status=?");
            args.push(v.to_string());
        }
        if let Some(s) = since {
            query.push_str(" AND entry_time>=?");
            args.push(py_isoformat(&s));
        }
        if let Some(u) = until {
            query.push_str(" AND entry_time<?");
            args.push(py_isoformat(&u));
        }
        query.push_str(" ORDER BY entry_time");
        let mut stmt = self.conn.prepare(&query)?;
        let col_names: Vec<String> = stmt.column_names().iter().map(|s| s.to_string()).collect();
        let params_ref: Vec<&dyn rusqlite::ToSql> =
            args.iter().map(|s| s as &dyn rusqlite::ToSql).collect();
        let mut rows = stmt.query(params_ref.as_slice())?;
        let mut out = Vec::new();
        while let Some(row) = rows.next()? {
            let mut map = Map::new();
            for (i, name) in col_names.iter().enumerate() {
                let v: Option<String> = row.get(i)?;
                map.insert(name.clone(), v.map(Value::String).unwrap_or(Value::Null));
            }
            out.push(map);
        }
        Ok(out)
    }
}

fn tz_kind(name: &str) -> &'static str {
    match name {
        "UTC" => "UTC",
        _ => "KST",
    }
}

fn offset_secs(kind: &str) -> i64 {
    match kind {
        "UTC" => 0,
        _ => 9 * 3600,
    }
}

pub fn trading_day_bounds(
    now: DateTime<Utc>,
    tz_name: &str,
) -> (DateTime<Utc>, DateTime<Utc>, String) {
    let kind = tz_kind(tz_name);
    let off = offset_secs(kind);
    let local = now + Duration::seconds(off);
    let naive = local.naive_utc();
    let date: NaiveDate = naive.date();
    let start_local_naive = date.and_hms_opt(0, 0, 0).unwrap();
    let end_local_naive = start_local_naive + Duration::days(1);
    let start_utc = Utc.from_utc_datetime(&start_local_naive) - Duration::seconds(off);
    let end_utc = Utc.from_utc_datetime(&end_local_naive) - Duration::seconds(off);
    (start_utc, end_utc, date.format("%Y-%m-%d").to_string())
}

pub fn num_value(v: Option<&Value>) -> f64 {
    let v = match v {
        Some(v) => v,
        None => return 0.0,
    };
    match v {
        Value::Null => 0.0,
        Value::Number(n) => n.as_f64().unwrap_or(0.0),
        Value::String(s) => s.trim().parse::<f64>().unwrap_or(0.0),
        Value::Bool(b) if *b => 1.0,
        _ => 0.0,
    }
}

pub fn daily_entry_block_reason(
    trades_today: &[Map<String, Value>],
    cfg: &Map<String, Value>,
    day_start_equity: Option<&Value>,
    current_equity: Option<&Value>,
) -> String {
    let count = trades_today.len();
    let max_trades = cfg.get("max_trades_per_day").map(as_i64).unwrap_or(0);
    if (count as i64) >= max_trades {
        let raw = cfg
            .get("max_trades_per_day")
            .map(raw_repr)
            .unwrap_or_else(|| "0".to_string());
        return format!("DAILY_TRADE_LIMIT: {}/{} trades today", count, raw);
    }
    let mut streak: i64 = 0;
    for trade in trades_today.iter().rev() {
        let st = trade.get("status").and_then(|v| v.as_str()).unwrap_or("");
        if st != "CLOSED" {
            continue;
        }
        if num_value(trade.get("net_pnl")) < 0.0 {
            streak += 1;
        } else {
            break;
        }
    }
    let max_losses = cfg.get("max_consecutive_losses").map(as_i64).unwrap_or(0);
    if streak >= max_losses {
        return format!("CONSECUTIVE_LOSS_LIMIT: {} losses in a row today", streak);
    }
    let start = num_value(day_start_equity);
    let current = num_value(current_equity);
    if start > 0.0 && current > 0.0 {
        let change = (current - start) / start;
        let limit = cfg
            .get("max_daily_loss_pct")
            .map(|v| num_value(Some(v)))
            .unwrap_or(0.0);
        if change <= -limit {
            let raw = cfg
                .get("max_daily_loss_pct")
                .map(raw_repr)
                .unwrap_or_else(|| "0".to_string());
            let limit_val = cfg
                .get("max_daily_loss_pct")
                .map(|v| num_value(Some(v)))
                .unwrap_or(0.0);
            return format!(
                "DAILY_LOSS_LIMIT: equity {:.2}% vs day start (limit -{:.2}%)",
                change * 100.0,
                limit_val * 100.0
            )
            .replace(raw.as_str(), "");
        }
    }
    String::new()
}

fn as_i64(v: &Value) -> i64 {
    match v {
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i
            } else if let Some(f) = n.as_f64() {
                f as i64
            } else {
                0
            }
        }
        Value::String(s) => s.trim().parse::<i64>().unwrap_or(0),
        Value::Bool(b) => {
            if *b {
                1
            } else {
                0
            }
        }
        Value::Null => 0,
        _ => 0,
    }
}

fn raw_repr(v: &Value) -> String {
    match v {
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
        Value::String(s) => s.clone(),
        _ => String::new(),
    }
}

pub fn compute_stats(trades: &[Map<String, Value>]) -> Map<String, Value> {
    let closed: Vec<&Map<String, Value>> = trades
        .iter()
        .filter(|t| t.get("status").and_then(|v| v.as_str()) == Some("CLOSED"))
        .collect();
    let pnls: Vec<f64> = closed.iter().map(|t| num_value(t.get("net_pnl"))).collect();
    let wins: Vec<f64> = pnls.iter().copied().filter(|p| *p > 0.0).collect();
    let losses: Vec<f64> = pnls.iter().copied().filter(|p| *p < 0.0).collect();
    let rs: Vec<f64> = closed
        .iter()
        .filter(|t| match t.get("r_multiple") {
            None | Some(Value::Null) => false,
            Some(Value::String(s)) if s.is_empty() => false,
            _ => true,
        })
        .map(|t| num_value(t.get("r_multiple")))
        .collect();
    let mut max_w: i64 = 0;
    let mut max_l: i64 = 0;
    let mut cur_w: i64 = 0;
    let mut cur_l: i64 = 0;
    for p in &pnls {
        if *p > 0.0 {
            cur_w += 1;
            cur_l = 0;
        } else if *p < 0.0 {
            cur_l += 1;
            cur_w = 0;
        } else {
            cur_w = 0;
            cur_l = 0;
        }
        if cur_w > max_w {
            max_w = cur_w;
        }
        if cur_l > max_l {
            max_l = cur_l;
        }
    }
    let mut equity = 0.0;
    let mut peak = 0.0;
    let mut drawdown = 0.0;
    for p in &pnls {
        equity += p;
        if equity > peak {
            peak = equity;
        }
        if peak - equity > drawdown {
            drawdown = peak - equity;
        }
    }
    let gross_profit: f64 = wins.iter().sum();
    let gross_loss: f64 = losses.iter().sum();
    let total = closed.len();
    let mut out = Map::new();
    out.insert("total_trades".into(), Value::from(total as i64));
    out.insert("wins".into(), Value::from(wins.len() as i64));
    out.insert("losses".into(), Value::from(losses.len() as i64));
    out.insert(
        "win_rate".into(),
        Value::from(if total > 0 {
            wins.len() as f64 / total as f64
        } else {
            0.0
        }),
    );
    out.insert("gross_profit".into(), Value::from(gross_profit));
    out.insert("gross_loss".into(), Value::from(gross_loss));
    let net: f64 = pnls.iter().sum();
    out.insert("net_pnl".into(), Value::from(net));
    out.insert(
        "average_win".into(),
        Value::from(if !wins.is_empty() {
            gross_profit / wins.len() as f64
        } else {
            0.0
        }),
    );
    out.insert(
        "average_loss".into(),
        Value::from(if !losses.is_empty() {
            gross_loss / losses.len() as f64
        } else {
            0.0
        }),
    );
    let profit_factor: f64 = if gross_loss != 0.0 {
        gross_profit / gross_loss.abs()
    } else if gross_profit != 0.0 {
        f64::INFINITY
    } else {
        0.0
    };
    // JSON has no Infinity: a loss-free record reports the string "inf".
    let profit_factor_value = if profit_factor.is_infinite() {
        Value::String("inf".into())
    } else {
        Value::from(profit_factor)
    };
    out.insert("profit_factor".into(), profit_factor_value);
    out.insert(
        "expectancy".into(),
        Value::from(if total > 0 { net / total as f64 } else { 0.0 }),
    );
    out.insert(
        "average_r".into(),
        Value::from(if !rs.is_empty() {
            rs.iter().sum::<f64>() / rs.len() as f64
        } else {
            0.0
        }),
    );
    out.insert("max_consecutive_wins".into(), Value::from(max_w));
    out.insert("max_consecutive_losses".into(), Value::from(max_l));
    out.insert("max_drawdown".into(), Value::from(drawdown));
    out
}
