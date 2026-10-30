//! Configuration contract for the BTC EMA pullback strategy (pure config helpers).

use std::collections::HashMap;

use serde_json::{json, Map, Value};

pub const STRATEGY_NAME: &str = "btc_ema_pullback_small_account";
pub const STRATEGY_VERSION: &str = "1.0.0";
pub const CLIENT_ID_STRATEGY: &str = "btcpb";

pub const TRADING_MODE_DRY_RUN: &str = "DRY_RUN";
pub const TRADING_MODE_LIVE: &str = "LIVE";
pub const TRADING_MODES: &[&str] = &[TRADING_MODE_DRY_RUN, TRADING_MODE_LIVE];
pub const TRADING_MODE_ENV: &str = "BTC_PULLBACK_TRADING_MODE";

pub const NETWORK_TESTNET: &str = "testnet";
pub const NETWORK_MAINNET: &str = "mainnet";
pub const NETWORKS: &[&str] = &[NETWORK_TESTNET, NETWORK_MAINNET];

pub const SIZING_FIXED: &str = "fixed";
pub const SIZING_RISK_BASED: &str = "risk_based";
pub const SIZING_MODES: &[&str] = &[SIZING_FIXED, SIZING_RISK_BASED];

pub fn default_btc_pullback_config() -> Value {
    json!({
        "enabled": false,
        "trading_mode": TRADING_MODE_DRY_RUN,
        "network": NETWORK_TESTNET,
        "symbol": "BTCUSDT",
        "strategy_name": STRATEGY_NAME,
        "leverage": 3,
        "margin_type": "ISOLATED",
        "position_mode": "ONE_WAY",
        "base_quantity": "0.001",
        "sizing_mode_mainnet": SIZING_FIXED,
        "sizing_mode_testnet": SIZING_RISK_BASED,
        "ema_fast": 20,
        "ema_slow": 50,
        "trend_timeframe": "1h",
        "entry_timeframe": "15m",
        "candle_history": 220,
        "stop_loss_pct": "0.008",
        "take_profit_pct": "0.016",
        "max_risk_per_trade_pct": "0.005",
        "max_daily_loss_pct": "0.015",
        "max_trades_per_day": 3,
        "max_consecutive_losses": 3,
        "day_timezone": "Asia/Seoul",
        "ema_separation_threshold": "0.0015",
        "ema_slope_threshold": "0.0002",
        "ema_slope_lookback": 3,
        "recent_cross_lookback": 24,
        "max_recent_crosses": 1,
        "pullback_lookback": 4,
        "pullback_tolerance_pct": "0.001",
        "ema50_break_tolerance_pct": "0.001",
        "atr_period": 14,
        "max_entry_candle_atr_multiple": "1.8",
        "max_entry_body_atr_multiple": "1.2",
        "signal_max_age_seconds": 180,
        "estimated_taker_fee_rate": "0.0005",
        "estimated_maker_fee_rate": "0.0002",
        "slippage_buffer_pct": "0.0005",
        "max_margin_usage_pct": "0.9",
        "working_type": "MARK_PRICE",
        "tp_working_type": "CONTRACT_PRICE",
        "protection_retry_attempts": 3,
        "protection_retry_delays_seconds": [0.5, 1.0, 2.0],
        "protection_check_interval_seconds": 60,
        "loop_interval_seconds": 10,
        "rules_refresh_seconds": 3600,
        "dry_run_balance_usdt": "210",
        "allow_pyramiding": false,
        "allow_martingale": false,
        "allow_averaging": false,
        "allow_auto_raise_to_exchange_minimum": false,
    })
}

/// Python `float(value)`-like conversion; `None` on failure.
fn py_float_raw(value: &Value) -> Option<f64> {
    match value {
        Value::Null => None,
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        Value::Number(n) => n.as_f64(),
        Value::String(s) => py_str_to_float(s),
        _ => None,
    }
}

fn py_str_to_float(s: &str) -> Option<f64> {
    let trimmed = s.trim();
    // Python float() accepts inf/nan/nan (case-insensitive).
    let lower = trimmed.to_ascii_lowercase();
    if lower == "inf" || lower == "+inf" || lower == "infinity" || lower == "+infinity" {
        return Some(f64::INFINITY);
    }
    if lower == "-inf" || lower == "-infinity" {
        return Some(f64::NEG_INFINITY);
    }
    if lower == "nan" || lower == "+nan" || lower == "-nan" {
        return Some(f64::NAN);
    }
    // Disallow underscores (Python rejects them in float()); Rust parses them out.
    if trimmed.contains('_') {
        return None;
    }
    // Rust rejects empty and accepts "1e5" style; matches Python well enough.
    if trimmed.is_empty() {
        return None;
    }
    // Guard against Rust accepting forms Python does not (e.g. "0x1p3").
    for ch in trimmed.chars() {
        let ok =
            ch.is_ascii_digit() || ch == '.' || ch == '+' || ch == '-' || ch == 'e' || ch == 'E';
        if !ok {
            return None;
        }
    }
    trimmed.parse::<f64>().ok()
}

fn default_float(default: f64) -> f64 {
    default
}

/// Python `_float(value, default, low, high)`.
pub fn py_float(value: Option<&Value>, default: f64, low: f64, high: f64) -> f64 {
    let mut result = match value.and_then(py_float_raw) {
        Some(v) => v,
        None => default_float(default),
    };
    if result.is_nan() {
        result = default;
    }
    high.min(low.max(result))
}

/// Python `int(float(value))`-like conversion; `None` on failure.
fn py_int_raw(value: &Value) -> Option<i64> {
    let f = py_float_raw(value)?;
    if f.is_nan() || f.is_infinite() {
        return None;
    }
    Some(f.trunc() as i64)
}

/// Python `_int(value, default, low, high)`.
pub fn py_int(value: Option<&Value>, default: i64, low: i64, high: i64) -> i64 {
    let result = match value.and_then(py_int_raw) {
        Some(v) => v,
        None => default,
    };
    high.min(low.max(result))
}

/// Python `_bool(value, default)`.
pub fn py_bool(value: Option<&Value>, default: bool) -> bool {
    let Some(v) = value else {
        return default;
    };
    match v {
        Value::Bool(b) => *b,
        Value::Null => default,
        _ => {
            let text = py_str(v);
            let trimmed = text.trim().to_ascii_lowercase();
            matches!(trimmed.as_str(), "1" | "true" | "yes" | "on" | "enabled")
        }
    }
}

/// Python `str(value)`.
fn py_str(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::Bool(b) => {
            if *b {
                "True".to_string()
            } else {
                "False".to_string()
            }
        }
        Value::Number(n) => n.to_string(),
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

/// Python `format(x, ".10g")` for a finite float.
pub fn py_decimal_text(value: Option<&Value>, default: f64, low: f64, high: f64) -> String {
    let f = py_float(value, default, low, high);
    format_g(f, 10)
}

/// Approximate Python `format(f, ".{precision}g")`.
fn format_g(f: f64, precision: usize) -> String {
    if f == 0.0 {
        // Preserve negative zero as "-0" (Python prints "-0").
        if f.is_sign_negative() {
            return "-0".to_string();
        }
        return "0".to_string();
    }
    if f.is_nan() {
        return "nan".to_string();
    }
    if f.is_infinite() {
        return if f < 0.0 { "-inf" } else { "inf" }.to_string();
    }
    // %g drops trailing zeros: use the scientific form to decide the exponent.
    let sci = format!("{:.*e}", precision.saturating_sub(1), f);
    let (mantissa_str, exp_str) = match sci.split_once('e') {
        Some((m, e)) => (m, e),
        None => return sci,
    };
    let exp: i32 = exp_str.parse().unwrap_or(0);
    if exp < -4 || exp >= precision as i32 {
        // Scientific notation: strip trailing zeros in the mantissa.
        let m = strip_trailing_zeros(mantissa_str);
        let exp_sign = if exp < 0 { '-' } else { '+' };
        let exp_abs = exp.abs();
        let exp_digits = if exp_abs < 10 {
            format!("0{}", exp_abs)
        } else {
            format!("{}", exp_abs)
        };
        format!("{}e{}{}", m, exp_sign, exp_digits)
    } else {
        // Fixed notation with (precision - 1 - exp) decimals.
        let decimals = (precision as i32 - 1 - exp).max(0) as usize;
        let fixed = format!("{:.*}", decimals, f);
        strip_trailing_zeros_fixed(&fixed)
    }
}

fn strip_trailing_zeros(mantissa: &str) -> String {
    if let Some((int_part, frac_part)) = mantissa.split_once('.') {
        let frac = frac_part.trim_end_matches('0');
        if frac.is_empty() {
            int_part.to_string()
        } else {
            format!("{}.{}", int_part, frac)
        }
    } else {
        mantissa.to_string()
    }
}

fn strip_trailing_zeros_fixed(fixed: &str) -> String {
    if fixed.contains('.') {
        let trimmed = fixed.trim_end_matches('0');
        let trimmed = trimmed.trim_end_matches('.');
        trimmed.to_string()
    } else {
        fixed.to_string()
    }
}

fn as_object(value: &Value) -> Option<&Map<String, Value>> {
    value.as_object()
}

fn get_field<'a>(map: &'a Map<String, Value>, key: &str) -> Option<&'a Value> {
    map.get(key)
}

fn opt_or_default<'a>(map: &'a Map<String, Value>, key: &str, default: &'a Value) -> &'a Value {
    match map.get(key) {
        Some(Value::Null) => default,
        Some(v) if !is_falsy(v) => v,
        _ => default,
    }
}

fn is_falsy(value: &Value) -> bool {
    match value {
        Value::Null => true,
        Value::Bool(b) => !*b,
        Value::String(s) => s.is_empty(),
        Value::Number(n) => n.as_f64() == Some(0.0),
        Value::Array(a) => a.is_empty(),
        Value::Object(o) => o.is_empty(),
    }
}

/// Python `str(cfg.get(key) or X).strip().lower()` semantics.
fn trimmed_lower(value: &Value) -> String {
    py_str(value).trim().to_lowercase()
}

fn trimmed_upper(value: &Value) -> String {
    py_str(value).trim().to_uppercase()
}

pub fn normalize_btc_pullback_config(raw: Option<&Value>) -> Value {
    let supplied: Map<String, Value> = match raw.and_then(as_object) {
        Some(obj) => obj.clone(),
        None => Map::new(),
    };
    let defaults = default_btc_pullback_config();
    let defaults_obj = defaults.as_object().cloned().unwrap_or_default();

    let mut cfg = default_btc_pullback_config();
    if let Some(map) = cfg.as_object_mut() {
        map.extend(supplied);
    }
    let cfg_obj_mut = cfg.as_object_mut().unwrap_or_else(|| unreachable!());

    // enabled
    let enabled = py_bool(cfg_obj_mut.get("enabled"), false);
    cfg_obj_mut.insert("enabled".into(), Value::Bool(enabled));

    // trading_mode
    let mode_val = match cfg_obj_mut.get("trading_mode") {
        Some(v) if !is_falsy(v) => trimmed_upper(v),
        _ => TRADING_MODE_DRY_RUN.to_string(),
    };
    let mode_out = if TRADING_MODES.contains(&mode_val.as_str()) {
        mode_val
    } else {
        TRADING_MODE_DRY_RUN.to_string()
    };
    cfg_obj_mut.insert("trading_mode".into(), Value::String(mode_out));

    // network
    let network_val = match cfg_obj_mut.get("network") {
        Some(v) if !is_falsy(v) => trimmed_lower(v),
        _ => NETWORK_TESTNET.to_string(),
    };
    let network_out = if NETWORKS.contains(&network_val.as_str()) {
        network_val
    } else {
        NETWORK_TESTNET.to_string()
    };
    cfg_obj_mut.insert("network".into(), Value::String(network_out));

    cfg_obj_mut.insert("symbol".into(), Value::String("BTCUSDT".into()));
    cfg_obj_mut.insert("strategy_name".into(), Value::String(STRATEGY_NAME.into()));

    let leverage = py_int(cfg_obj_mut.get("leverage"), 3, 1, 3);
    cfg_obj_mut.insert("leverage".into(), json!(leverage));
    cfg_obj_mut.insert("margin_type".into(), Value::String("ISOLATED".into()));
    cfg_obj_mut.insert("position_mode".into(), Value::String("ONE_WAY".into()));

    let base_quantity = py_decimal_text(
        cfg_obj_mut.get("base_quantity"),
        defaults_obj
            .get("base_quantity")
            .and_then(crate::core::num::as_f64)
            .unwrap_or(0.001),
        0.0001,
        1.0,
    );
    cfg_obj_mut.insert("base_quantity".into(), Value::String(base_quantity));

    for key in ["sizing_mode_mainnet", "sizing_mode_testnet"] {
        let default_str = defaults_obj
            .get(key)
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let cur = cfg_obj_mut.get(key);
        let value = match cur {
            Some(v) if !is_falsy(v) => trimmed_lower(v),
            _ => default_str.clone(),
        };
        let out = if SIZING_MODES.contains(&value.as_str()) {
            value
        } else {
            default_str
        };
        cfg_obj_mut.insert(key.into(), Value::String(out));
    }

    let ema_fast = py_int(cfg_obj_mut.get("ema_fast"), 20, 2, 200);
    cfg_obj_mut.insert("ema_fast".into(), json!(ema_fast));
    let ema_slow = py_int(cfg_obj_mut.get("ema_slow"), 50, 3, 400);
    let ema_slow = if ema_slow <= ema_fast {
        ema_fast + 1
    } else {
        ema_slow
    };
    cfg_obj_mut.insert("ema_slow".into(), json!(ema_slow));

    cfg_obj_mut.insert("trend_timeframe".into(), Value::String("1h".into()));
    cfg_obj_mut.insert("entry_timeframe".into(), Value::String("15m".into()));

    let low_hist = ema_slow * 3;
    let candle_history = py_int(cfg_obj_mut.get("candle_history"), 220, low_hist, 1000);
    cfg_obj_mut.insert("candle_history".into(), json!(candle_history));

    let stop_loss = py_decimal_text(cfg_obj_mut.get("stop_loss_pct"), 0.008, 0.001, 0.05);
    cfg_obj_mut.insert("stop_loss_pct".into(), Value::String(stop_loss));
    let tp = py_decimal_text(cfg_obj_mut.get("take_profit_pct"), 0.016, 0.001, 0.2);
    cfg_obj_mut.insert("take_profit_pct".into(), Value::String(tp));
    let risk = py_decimal_text(
        cfg_obj_mut.get("max_risk_per_trade_pct"),
        0.005,
        0.0005,
        0.02,
    );
    cfg_obj_mut.insert("max_risk_per_trade_pct".into(), Value::String(risk));
    let daily = py_decimal_text(cfg_obj_mut.get("max_daily_loss_pct"), 0.015, 0.002, 0.1);
    cfg_obj_mut.insert("max_daily_loss_pct".into(), Value::String(daily));

    let trades = py_int(cfg_obj_mut.get("max_trades_per_day"), 3, 1, 20);
    cfg_obj_mut.insert("max_trades_per_day".into(), json!(trades));
    let losses = py_int(cfg_obj_mut.get("max_consecutive_losses"), 3, 1, 20);
    cfg_obj_mut.insert("max_consecutive_losses".into(), json!(losses));

    let tz = match cfg_obj_mut.get("day_timezone") {
        Some(v) if !is_falsy(v) => py_str(v),
        _ => "Asia/Seoul".to_string(),
    };
    cfg_obj_mut.insert("day_timezone".into(), Value::String(tz));

    let sep = py_decimal_text(
        cfg_obj_mut.get("ema_separation_threshold"),
        0.0015,
        0.0,
        0.05,
    );
    cfg_obj_mut.insert("ema_separation_threshold".into(), Value::String(sep));
    let slope = py_decimal_text(cfg_obj_mut.get("ema_slope_threshold"), 0.0002, 0.0, 0.01);
    cfg_obj_mut.insert("ema_slope_threshold".into(), Value::String(slope));

    let slope_lb = py_int(cfg_obj_mut.get("ema_slope_lookback"), 3, 1, 48);
    cfg_obj_mut.insert("ema_slope_lookback".into(), json!(slope_lb));
    let cross_lb = py_int(cfg_obj_mut.get("recent_cross_lookback"), 24, 2, 200);
    cfg_obj_mut.insert("recent_cross_lookback".into(), json!(cross_lb));
    let max_crosses = py_int(cfg_obj_mut.get("max_recent_crosses"), 1, 0, 20);
    cfg_obj_mut.insert("max_recent_crosses".into(), json!(max_crosses));
    let pullback_lb = py_int(cfg_obj_mut.get("pullback_lookback"), 4, 1, 20);
    cfg_obj_mut.insert("pullback_lookback".into(), json!(pullback_lb));
    let pullback_tol = py_decimal_text(cfg_obj_mut.get("pullback_tolerance_pct"), 0.001, 0.0, 0.02);
    cfg_obj_mut.insert("pullback_tolerance_pct".into(), Value::String(pullback_tol));
    let break_tol = py_decimal_text(
        cfg_obj_mut.get("ema50_break_tolerance_pct"),
        0.001,
        0.0,
        0.02,
    );
    cfg_obj_mut.insert("ema50_break_tolerance_pct".into(), Value::String(break_tol));

    let atr = py_int(cfg_obj_mut.get("atr_period"), 14, 2, 100);
    cfg_obj_mut.insert("atr_period".into(), json!(atr));
    let candle_mult = py_decimal_text(
        cfg_obj_mut.get("max_entry_candle_atr_multiple"),
        1.8,
        0.3,
        10.0,
    );
    cfg_obj_mut.insert(
        "max_entry_candle_atr_multiple".into(),
        Value::String(candle_mult),
    );
    let body_mult = py_decimal_text(
        cfg_obj_mut.get("max_entry_body_atr_multiple"),
        1.2,
        0.2,
        10.0,
    );
    cfg_obj_mut.insert(
        "max_entry_body_atr_multiple".into(),
        Value::String(body_mult),
    );
    let max_age = py_int(cfg_obj_mut.get("signal_max_age_seconds"), 180, 30, 900);
    cfg_obj_mut.insert("signal_max_age_seconds".into(), json!(max_age));

    let taker = py_decimal_text(
        cfg_obj_mut.get("estimated_taker_fee_rate"),
        0.0005,
        0.0,
        0.005,
    );
    cfg_obj_mut.insert("estimated_taker_fee_rate".into(), Value::String(taker));
    let maker = py_decimal_text(
        cfg_obj_mut.get("estimated_maker_fee_rate"),
        0.0002,
        0.0,
        0.005,
    );
    cfg_obj_mut.insert("estimated_maker_fee_rate".into(), Value::String(maker));
    let slip = py_decimal_text(cfg_obj_mut.get("slippage_buffer_pct"), 0.0005, 0.0, 0.01);
    cfg_obj_mut.insert("slippage_buffer_pct".into(), Value::String(slip));
    let margin_usage = py_decimal_text(cfg_obj_mut.get("max_margin_usage_pct"), 0.9, 0.1, 1.0);
    cfg_obj_mut.insert("max_margin_usage_pct".into(), Value::String(margin_usage));

    let wt_val = match cfg_obj_mut.get("working_type") {
        Some(v) if !is_falsy(v) => trimmed_upper(v),
        _ => "MARK_PRICE".to_string(),
    };
    let wt_out = if wt_val == "MARK_PRICE" || wt_val == "CONTRACT_PRICE" {
        wt_val
    } else {
        "MARK_PRICE".to_string()
    };
    cfg_obj_mut.insert("working_type".into(), Value::String(wt_out));

    let tp_wt_val = match cfg_obj_mut.get("tp_working_type") {
        Some(v) if !is_falsy(v) => trimmed_upper(v),
        _ => "CONTRACT_PRICE".to_string(),
    };
    let tp_wt_out = if tp_wt_val == "MARK_PRICE" || tp_wt_val == "CONTRACT_PRICE" {
        tp_wt_val
    } else {
        "CONTRACT_PRICE".to_string()
    };
    cfg_obj_mut.insert("tp_working_type".into(), Value::String(tp_wt_out));

    let retry = py_int(cfg_obj_mut.get("protection_retry_attempts"), 3, 1, 6);
    cfg_obj_mut.insert("protection_retry_attempts".into(), json!(retry));

    let delays_value = match cfg_obj_mut.get("protection_retry_delays_seconds") {
        Some(Value::Array(arr)) if !arr.is_empty() => Value::Array(arr.clone()),
        _ => defaults_obj
            .get("protection_retry_delays_seconds")
            .cloned()
            .unwrap_or_else(|| json!([0.5, 1.0, 2.0])),
    };
    let delay_arr: Vec<Value> = match &delays_value {
        Value::Array(arr) => arr.iter().take(6).cloned().collect(),
        _ => Vec::new(),
    };
    let converted: Vec<Value> = delay_arr
        .iter()
        .map(|v| {
            let f = py_float(Some(v), 1.0, 0.0, 10.0);
            number_from_f64(f)
        })
        .collect();
    cfg_obj_mut.insert(
        "protection_retry_delays_seconds".into(),
        Value::Array(converted),
    );

    let check_interval = py_int(
        cfg_obj_mut.get("protection_check_interval_seconds"),
        60,
        10,
        600,
    );
    cfg_obj_mut.insert(
        "protection_check_interval_seconds".into(),
        json!(check_interval),
    );
    let loop_interval = py_int(cfg_obj_mut.get("loop_interval_seconds"), 10, 5, 60);
    cfg_obj_mut.insert("loop_interval_seconds".into(), json!(loop_interval));
    let rules_refresh = py_int(cfg_obj_mut.get("rules_refresh_seconds"), 3600, 300, 86400);
    cfg_obj_mut.insert("rules_refresh_seconds".into(), json!(rules_refresh));

    let dry_balance = py_decimal_text(
        cfg_obj_mut.get("dry_run_balance_usdt"),
        210.0,
        10.0,
        10_000_000.0,
    );
    cfg_obj_mut.insert("dry_run_balance_usdt".into(), Value::String(dry_balance));

    for key in ["allow_pyramiding", "allow_martingale", "allow_averaging"] {
        cfg_obj_mut.insert(key.into(), Value::Bool(false));
    }
    let raise = py_bool(
        cfg_obj_mut.get("allow_auto_raise_to_exchange_minimum"),
        false,
    );
    cfg_obj_mut.insert(
        "allow_auto_raise_to_exchange_minimum".into(),
        Value::Bool(raise),
    );
    cfg_obj_mut.remove("allow_shared_account_with_main_bot");

    let _ = get_field;
    let _ = opt_or_default;
    cfg
}

fn number_from_f64(f: f64) -> Value {
    if f.is_finite() && f.fract() == 0.0 && f.abs() < 9_007_199_254_740_992.0 {
        let as_int = f as i64;
        if (as_int as f64) == f {
            return json!(as_int);
        }
    }
    match serde_json::Number::from_f64(f) {
        Some(n) => Value::Number(n),
        None => Value::Null,
    }
}

pub fn effective_trading_mode(cfg: Option<&Value>, environ: &HashMap<String, String>) -> String {
    let forced = environ
        .get(TRADING_MODE_ENV)
        .map(|s| s.trim().to_uppercase())
        .unwrap_or_default();
    if forced == TRADING_MODE_DRY_RUN {
        return TRADING_MODE_DRY_RUN.to_string();
    }
    let current = cfg
        .and_then(|v| v.as_object())
        .and_then(|m| m.get("trading_mode"))
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if current == TRADING_MODE_LIVE {
        TRADING_MODE_LIVE.to_string()
    } else {
        TRADING_MODE_DRY_RUN.to_string()
    }
}

pub fn sizing_mode_for(cfg: &Value) -> String {
    let obj = match cfg.as_object() {
        Some(o) => o,
        None => return SIZING_FIXED.to_string(),
    };
    let network = obj.get("network").and_then(|v| v.as_str()).unwrap_or("");
    let key = if network == NETWORK_TESTNET {
        "sizing_mode_testnet"
    } else {
        "sizing_mode_mainnet"
    };
    match obj.get(key) {
        Some(v) if !is_falsy(v) => py_str(v),
        _ => SIZING_FIXED.to_string(),
    }
}
