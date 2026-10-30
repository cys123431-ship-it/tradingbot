//! `config.json` access shared with the Python bot.
//!
//! Reload on mtime change, tolerate parse errors by serving the last good
//! value, and write atomically (temp file + rename) under a cross-process
//! lock file.

use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex as StdMutex;
use std::time::{Duration, SystemTime};

use serde_json::{Map, Value};

/// Lock acquisition limits (Python: `create_new` retry loop).
const LOCK_ACQUIRE_TIMEOUT: Duration = Duration::from_secs(5);
const LOCK_STALE_AFTER: Duration = Duration::from_secs(30);
const LOCK_RETRY_INTERVAL: Duration = Duration::from_millis(50);

pub struct ConfigFile {
    path: PathBuf,
    cache: StdMutex<(Option<SystemTime>, Value)>,
    #[allow(dead_code)]
    write_lock: tokio::sync::Mutex<u64>,
}

impl ConfigFile {
    pub fn new(path: PathBuf) -> Self {
        Self {
            path,
            cache: StdMutex::new((None, Value::Object(Map::new()))),
            write_lock: tokio::sync::Mutex::new(0),
        }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Reload only when the mtime changed; on parse failure keep the last
    /// successfully parsed value and log a warning.
    pub fn load(&self) -> Value {
        let mtime = std::fs::metadata(&self.path)
            .and_then(|m| m.modified())
            .ok();
        let mut guard = match self.cache.lock() {
            Ok(g) => g,
            Err(poisoned) => poisoned.into_inner(),
        };
        if guard.0 == mtime {
            return guard.1.clone();
        }
        let text = match std::fs::read_to_string(&self.path) {
            Ok(t) => t,
            Err(_) => {
                // File missing: keep whatever we had, cache the mtime gap.
                guard.0 = mtime;
                if let Err(e) =
                    write_stderr(&format!("config_file: read {} failed", self.path.display()))
                {
                    let _ = e; // python: ignored on purpose
                }
                return guard.1.clone();
            }
        };
        match serde_json::from_str::<Value>(&text) {
            Ok(v) => {
                guard.0 = mtime;
                guard.1 = v;
                guard.1.clone()
            }
            Err(e) => {
                if let Err(werr) = write_stderr(&format!(
                    "config_file: parse {} failed: {e}; keeping previous value",
                    self.path.display()
                )) {
                    let _ = werr; // python: ignored on purpose
                }
                guard.1.clone()
            }
        }
    }

    pub fn section(&self, key: &str) -> Value {
        let v = self.load();
        match v.get(key) {
            Some(Value::Object(_)) => v.get(key).cloned().unwrap_or(Value::Object(Map::new())),
            _ => Value::Object(Map::new()),
        }
    }

    /// `set_entry_mode`-style write: re-read the file, overwrite the section
    /// keys, dump and rename atomically. Returns the saved section.
    pub async fn update_values(
        &self,
        section: &str,
        mut changes: Map<String, Value>,
    ) -> Result<Value, String> {
        let _guard = self.write_lock.lock().await;
        write_lock_file(&self.path)?;
        let result = self.update_values_inner(section, &mut changes);
        release_lock_file(&self.path);
        result
    }

    fn update_values_inner(
        &self,
        section: &str,
        changes: &mut Map<String, Value>,
    ) -> Result<Value, String> {
        let text = std::fs::read_to_string(&self.path).unwrap_or_else(|_| "{}".to_string());
        let mut root: Value =
            serde_json::from_str(&text).map_err(|e| format!("ValueError: {e}"))?;
        if !root.is_object() {
            root = Value::Object(Map::new());
        }
        let root_map = root
            .as_object_mut()
            .ok_or_else(|| "ValueError: config root is not an object".to_string())?;
        let entry = root_map
            .entry(section.to_string())
            .or_insert_with(|| Value::Object(Map::new()));
        if !entry.is_object() {
            *entry = Value::Object(Map::new());
        }
        let section_map = entry
            .as_object_mut()
            .ok_or_else(|| "ValueError: config section is not an object".to_string())?;
        for (k, v) in changes.iter() {
            section_map.insert(k.clone(), v.clone());
        }
        let serialized = dump_indent4(&root);
        let parent = self
            .path
            .parent()
            .ok_or_else(|| "ValueError: config path has no parent".to_string())?;
        std::fs::create_dir_all(parent).map_err(|e| format!("OSError: {e}"))?;
        let tmp = tmp_sibling(&self.path);
        {
            let mut f = std::fs::File::create(&tmp).map_err(|e| format!("OSError: {e}"))?;
            f.write_all(serialized.as_bytes())
                .map_err(|e| format!("OSError: {e}"))?;
            f.sync_all().map_err(|e| format!("OSError: {e}"))?;
        }
        std::fs::rename(&tmp, &self.path).map_err(|e| format!("OSError: {e}"))?;
        // Refresh cache to the value we just wrote.
        let mtime = std::fs::metadata(&self.path)
            .and_then(|m| m.modified())
            .ok();
        if let Ok(mut guard) = self.cache.lock() {
            guard.0 = mtime;
            guard.1 = root.clone();
        }
        Ok(root
            .get(section)
            .cloned()
            .unwrap_or(Value::Object(Map::new())))
    }
}

fn tmp_sibling(path: &Path) -> PathBuf {
    let mut hasher = DefaultHasher::new();
    path.hash(&mut hasher);
    let n = hasher.finish();
    let name = path
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| "config.json".to_string());
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    parent.join(format!(".{name}.{n:016x}.tmp"))
}

fn lock_path(path: &Path) -> PathBuf {
    let mut p = path.to_path_buf();
    let name = p
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| "config.json".to_string());
    p.set_file_name(format!("{name}.lock"));
    p
}

fn write_lock_file(path: &Path) -> Result<(), String> {
    let lock = lock_path(path);
    if let Some(parent) = lock.parent() {
        std::fs::create_dir_all(parent).map_err(|e| format!("OSError: {e}"))?;
    }
    let started = SystemTime::now();
    loop {
        match std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&lock)
        {
            Ok(f) => {
                drop(f);
                return Ok(());
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                if let Ok(meta) = std::fs::metadata(&lock) {
                    if let Ok(modified) = meta.modified() {
                        if SystemTime::now()
                            .duration_since(modified)
                            .map(|d| d > LOCK_STALE_AFTER)
                            .unwrap_or(false)
                        {
                            if let Err(_e) = std::fs::remove_file(&lock) {
                                // python: ignored on purpose
                            }
                            continue;
                        }
                    }
                }
                let elapsed = SystemTime::now()
                    .duration_since(started)
                    .unwrap_or(Duration::from_secs(0));
                if elapsed >= LOCK_ACQUIRE_TIMEOUT {
                    return Err("TimeoutError: config lock busy".to_string());
                }
                std::thread::sleep(LOCK_RETRY_INTERVAL);
            }
            Err(e) => return Err(format!("OSError: {e}")),
        }
    }
}

fn release_lock_file(path: &Path) {
    let lock = lock_path(path);
    if let Err(_e) = std::fs::remove_file(&lock) {
        // python: ignored on purpose
    }
}

fn write_stderr(msg: &str) -> std::io::Result<()> {
    let line = format!("{} {msg}\n", timestamp());
    use std::io::Write as _;
    std::io::stderr().write_all(line.as_bytes())
}

fn timestamp() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, false)
}

/// Same shape as Python `json.dump(..., indent=4, ensure_ascii=False)`.
pub fn dump_indent4(value: &Value) -> String {
    let mut out = String::new();
    write_pretty(value, 0, &mut out);
    out.push('\n');
    out
}

fn write_pretty(value: &Value, indent: usize, out: &mut String) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => out.push_str(&python_json_string(s)),
        Value::Array(items) => {
            if items.is_empty() {
                out.push_str("[]");
                return;
            }
            out.push_str("[\n");
            let child = indent + 4;
            for (i, item) in items.iter().enumerate() {
                out.push_str(&" ".repeat(child));
                write_pretty(item, child, out);
                if i + 1 != items.len() {
                    out.push(',');
                }
                out.push('\n');
            }
            out.push_str(&" ".repeat(indent));
            out.push(']');
        }
        Value::Object(map) => {
            if map.is_empty() {
                out.push_str("{}");
                return;
            }
            out.push_str("{\n");
            let child = indent + 4;
            let len = map.len();
            for (i, (k, v)) in map.iter().enumerate() {
                out.push_str(&" ".repeat(child));
                out.push_str(&python_json_string(k));
                out.push_str(": ");
                write_pretty(v, child, out);
                if i + 1 != len {
                    out.push(',');
                }
                out.push('\n');
            }
            out.push_str(&" ".repeat(indent));
            out.push('}');
        }
    }
}

fn python_json_string(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
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
    out
}

// ---------------------------------------------------------------------------
// Exchange mode / credential helpers (Python `_get_exchange_mode` etc.)
// ---------------------------------------------------------------------------

const BINANCE_TESTNET: &str = "binance_testnet";
const BINANCE_MAINNET: &str = "binance_mainnet";
const UPBIT: &str = "upbit";
const NETWORK_TESTNET: &str = "testnet";
const NETWORK_MAINNET: &str = "mainnet";

/// Mirrors Python `get_exchange_mode`.
pub fn exchange_mode(cfg: &Value) -> String {
    let api = cfg.get("api").and_then(|v| v.as_object());
    let raw = api
        .and_then(|m| m.get("exchange_mode"))
        .and_then(|v| v.as_str())
        .map(|s| s.trim().to_string())
        .unwrap_or_default();
    if matches!(raw.as_str(), BINANCE_MAINNET | BINANCE_TESTNET | UPBIT) {
        return raw;
    }
    let use_testnet = api
        .and_then(|m| m.get("use_testnet"))
        .and_then(|v| v.as_bool())
        .unwrap_or(true);
    if use_testnet {
        BINANCE_TESTNET.to_string()
    } else {
        BINANCE_MAINNET.to_string()
    }
}

/// Mirrors Python `main_network_getter`.
pub fn main_network(cfg: &Value) -> Option<String> {
    match exchange_mode(cfg).as_str() {
        BINANCE_MAINNET => Some(NETWORK_MAINNET.to_string()),
        BINANCE_TESTNET => Some(NETWORK_TESTNET.to_string()),
        _ => None,
    }
}

/// Mirrors Python `_get_exchange_credentials`.
pub fn credentials(cfg: &Value, network: &str) -> Value {
    let key = if network == NETWORK_MAINNET {
        "mainnet"
    } else {
        "testnet"
    };
    let map = cfg
        .get("api")
        .and_then(|v| v.as_object())
        .and_then(|m| m.get(key))
        .and_then(|v| v.as_object());
    let mut out = Map::new();
    if let Some(m) = map {
        for name in ["api_key", "secret_key"] {
            if let Some(v) = m.get(name) {
                out.insert(name.to_string(), v.clone());
            }
        }
    }
    Value::Object(out)
}
