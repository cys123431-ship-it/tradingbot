//! Golden comparison tests for pure strategy config / session / tradfi helpers.

use std::collections::HashMap;
use std::path::PathBuf;

use chrono::{DateTime, Utc};
use serde_json::{Map, Value};
use tbot::strategy::coinselect::{
    market_is_tradifi_perpetual, market_is_usdt_perpetual, market_tradifi_underlying_type,
    normalize_symbol, symbol_base,
};
use tbot::strategy::config_base::{
    effective_trading_mode, normalize_btc_pullback_config, sizing_mode_for,
};
use tbot::strategy::config_btc_ma::normalize_btc_ma_cross_config;
use tbot::strategy::config_ma_plus::normalize_ma_cross_config;
use tbot::strategy::market_session::{
    tradfi_primary_session_status, us_equity_regular_session_status,
};
use tbot::strategy::tradfi::{
    market_in_tradfi_scope, market_is_tradfi_gold, market_is_tradfi_us_equity,
};

fn golden_path() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push("tests");
    p.push("golden_config.json");
    p
}

fn json_get<'a>(args: &'a Map<String, Value>, key: &str) -> Option<&'a Value> {
    args.get(key)
}

fn as_opt_value(args: &Map<String, Value>, key: &str) -> Option<Value> {
    args.get(key).cloned()
}

fn as_opt_str(args: &Map<String, Value>, key: &str) -> Option<String> {
    match args.get(key) {
        Some(Value::String(s)) => Some(s.clone()),
        Some(Value::Null) => Some(String::new()),
        _ => None,
    }
}

fn parse_dt(text: &str) -> DateTime<Utc> {
    // Accept both "+00:00" and "Z" forms; Python isoformat uses +00:00.
    if let Ok(dt) = DateTime::parse_from_rfc3339(text) {
        return dt.with_timezone(&Utc);
    }
    // Fallback: parse with explicit offset in "%Y-%m-%dT%H:%M:%S%:z".
    DateTime::parse_from_str(text, "%Y-%m-%dT%H:%M:%S%:z")
        .expect("datetime")
        .with_timezone(&Utc)
}

fn compare_numbers(expected: f64, actual: f64) -> bool {
    let diff = (expected - actual).abs();
    if diff <= 1e-9 {
        return true;
    }
    let scale = expected.abs().max(actual.abs()).max(1.0);
    diff / scale <= 1e-9
}

fn values_equal(expected: &Value, actual: &Value) -> bool {
    match (expected, actual) {
        (Value::Number(a), Value::Number(b)) => {
            let fa = a.as_f64().unwrap_or(f64::NAN);
            let fb = b.as_f64().unwrap_or(f64::NAN);
            if fa.is_nan() && fb.is_nan() {
                return true;
            }
            compare_numbers(fa, fb)
        }
        (Value::Null, Value::Null) => true,
        (Value::Bool(a), Value::Bool(b)) => a == b,
        (Value::String(a), Value::String(b)) => a == b,
        (Value::Array(a), Value::Array(b)) => {
            a.len() == b.len() && a.iter().zip(b.iter()).all(|(x, y)| values_equal(x, y))
        }
        (Value::Object(a), Value::Object(b)) => {
            if a.len() != b.len() {
                return false;
            }
            for (k, v) in a {
                match b.get(k) {
                    Some(w) => {
                        if !values_equal(v, w) {
                            return false;
                        }
                    }
                    None => return false,
                }
            }
            true
        }
        _ => false,
    }
}

fn environ_from(value: Option<&Value>) -> HashMap<String, String> {
    let mut env = HashMap::new();
    if let Some(Value::Object(map)) = value {
        for (k, v) in map {
            let text = match v {
                Value::String(s) => s.clone(),
                Value::Null => String::new(),
                other => other.to_string(),
            };
            env.insert(k.clone(), text);
        }
    }
    env
}

fn run_case(case: &Value) -> bool {
    let obj = match case.as_object() {
        Some(o) => o,
        None => return false,
    };
    let fn_name = obj.get("fn").and_then(|v| v.as_str()).unwrap_or("");
    let args = obj
        .get("args")
        .and_then(|v| v.as_object())
        .cloned()
        .unwrap_or_default();
    let expect = obj.get("expect").cloned().unwrap_or(Value::Null);

    let actual: Value = match fn_name {
        "normalize_btc_pullback_config" => {
            let raw = as_opt_value(&args, "raw");
            normalize_btc_pullback_config(raw.as_ref())
        }
        "normalize_btc_ma_cross_config" => {
            let raw = as_opt_value(&args, "raw");
            let mut out = normalize_btc_ma_cross_config(raw.as_ref());
            // Rust-only addition (SMA25 stop line); Python never had it.
            if let Some(obj) = out.as_object_mut() {
                assert_eq!(obj.remove("stop_ma_period"), Some(serde_json::json!(25)));
            }
            out
        }
        "normalize_ma_cross_config" => {
            let raw = as_opt_value(&args, "raw");
            normalize_ma_cross_config(raw.as_ref())
        }
        "effective_trading_mode" => {
            let cfg = as_opt_value(&args, "cfg");
            let env = environ_from(args.get("environ"));
            Value::String(effective_trading_mode(cfg.as_ref(), &env))
        }
        "sizing_mode_for" => {
            let cfg = as_opt_value(&args, "cfg").unwrap_or(Value::Null);
            Value::String(sizing_mode_for(&cfg))
        }
        "us_equity_regular_session_status" => {
            let now = as_opt_str(&args, "now").map(|t| parse_dt(&t));
            us_equity_regular_session_status(now)
        }
        "tradfi_primary_session_status" => {
            let now = as_opt_str(&args, "now").map(|t| parse_dt(&t));
            let ut = match json_get(&args, "underlying_type") {
                Some(Value::String(s)) => Some(s.clone()),
                Some(Value::Null) | None => None,
                Some(other) => Some(other.to_string()),
            };
            tradfi_primary_session_status(ut.as_deref(), now)
        }
        "normalize_symbol" => {
            let sym = as_opt_str(&args, "symbol").unwrap_or_default();
            Value::String(normalize_symbol(&sym))
        }
        "symbol_base" => {
            let sym = as_opt_str(&args, "symbol").unwrap_or_default();
            Value::String(symbol_base(&sym))
        }
        "market_is_usdt_perpetual" => {
            let sym = as_opt_str(&args, "symbol").unwrap_or_default();
            let market = as_opt_value(&args, "market");
            Value::Bool(market_is_usdt_perpetual(&sym, market.as_ref()))
        }
        "market_is_tradifi_perpetual" => {
            let sym = as_opt_str(&args, "symbol").unwrap_or_default();
            let market = as_opt_value(&args, "market");
            Value::Bool(market_is_tradifi_perpetual(&sym, market.as_ref()))
        }
        "market_tradifi_underlying_type" => {
            let market = as_opt_value(&args, "market");
            Value::String(market_tradifi_underlying_type(market.as_ref()))
        }
        "market_is_tradfi_gold" => {
            let sym = as_opt_str(&args, "symbol").unwrap_or_default();
            let market = as_opt_value(&args, "market");
            Value::Bool(market_is_tradfi_gold(&sym, market.as_ref()))
        }
        "market_is_tradfi_us_equity" => {
            let sym = as_opt_str(&args, "symbol").unwrap_or_default();
            let market = as_opt_value(&args, "market");
            Value::Bool(market_is_tradfi_us_equity(&sym, market.as_ref()))
        }
        "market_in_tradfi_scope" => {
            let sym = as_opt_str(&args, "symbol").unwrap_or_default();
            let market = as_opt_value(&args, "market");
            Value::Bool(market_in_tradfi_scope(&sym, market.as_ref()))
        }
        _ => return false,
    };

    values_equal(&expect, &actual)
}

#[test]
fn golden_config() {
    let path = golden_path();
    let text =
        std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("read {}: {}", path.display(), e));
    let root: Value = serde_json::from_str(&text).expect("parse golden json");
    let cases = root
        .get("cases")
        .and_then(|v| v.as_array())
        .expect("cases array")
        .clone();

    let mut failures = Vec::new();
    for (idx, case) in cases.iter().enumerate() {
        let fn_name = case
            .get("fn")
            .and_then(|v| v.as_str())
            .unwrap_or("<unknown>")
            .to_string();
        // Reject unsupported fn names loudly.
        let supported = matches!(
            fn_name.as_str(),
            "normalize_btc_pullback_config"
                | "normalize_btc_ma_cross_config"
                | "normalize_ma_cross_config"
                | "effective_trading_mode"
                | "sizing_mode_for"
                | "us_equity_regular_session_status"
                | "tradfi_primary_session_status"
                | "normalize_symbol"
                | "symbol_base"
                | "market_is_usdt_perpetual"
                | "market_is_tradifi_perpetual"
                | "market_tradifi_underlying_type"
                | "market_is_tradfi_gold"
                | "market_is_tradfi_us_equity"
                | "market_in_tradfi_scope"
        );
        if !supported {
            failures.push(format!("case {}: unsupported fn {:?}", idx, fn_name));
            continue;
        }
        if !run_case(case) {
            failures.push(format!(
                "case {} ({}): mismatch\n  expect={}\n  case={}",
                idx,
                fn_name,
                case.get("expect").cloned().unwrap_or(Value::Null),
                case
            ));
        }
    }

    if !failures.is_empty() {
        let msg = failures.join("\n---\n");
        panic!("{} golden failures:\n{}", failures.len(), msg);
    }
}
