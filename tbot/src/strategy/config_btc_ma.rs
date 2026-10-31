//! Configuration for the BTC 3/200 SMA cross strategy.

use serde_json::{json, Map, Value};

use crate::core::periods::apply_periods;
use crate::strategy::config_base::{
    py_bool, py_decimal_text, py_float, py_int, NETWORKS, NETWORK_TESTNET, TRADING_MODES,
    TRADING_MODE_DRY_RUN,
};

pub const STRATEGY_NAME: &str = "btc_sma3_sma200_cross";
pub const STRATEGY_VERSION: &str = "1.0.0";
pub const CLIENT_ID_STRATEGY: &str = "btcma";
pub const CONFIG_KEY: &str = "btc_ma_cross";

pub const TIMEFRAMES: &[&str] = &["3m", "5m", "15m", "30m", "1h", "2h", "4h"];
pub const SLOW_PERIOD_CHOICES: &[i64] = &[200, 120];
pub const FAST_PERIOD_CHOICES: &[i64] = &[1, 3, 5, 9];
pub const SCAN_SCOPES: &[&str] = &["btc", "alts", "single", "tradfi"];
pub const TRADFI_SCOPE: &str = "tradfi";
pub const ENTRY_MODES: &[&str] = &["cross", "trend"];
pub const EMERGENCY_STOP_CHOICES: &[i64] = &[0, 10, 20, 30, 50];
pub const REENTRY_WAIT_CHOICES: &[i64] = &[1, 3, 6, 0];
pub const PROFIT_LOCK_SWITCH_CHOICES: &[i64] = &[0, 1, 2, 3, 4, 5];
pub const LEVERAGE_CHOICES: &[i64] = &[2, 3, 4, 5];
pub const DEFAULT_LEVERAGE: i64 = 5;
/// Fixed stop line: close beyond SMA25 against the position exits it.
pub const STOP_MA_PERIOD: i64 = 25;

pub fn default_btc_ma_cross_config() -> Value {
    json!({
        "enabled": false,
        "trading_mode": TRADING_MODE_DRY_RUN,
        "network": NETWORK_TESTNET,
        "symbol": "BTCUSDT",
        "strategy_name": STRATEGY_NAME,
        "timeframe": "1h",
        "scan_scope": "btc",
        "single_symbol": "BTCUSDT",
        "alt_universe_size": 50,
        "rank_testnet_by_mainnet_volume": true,
        "min_median_bar_range_pct": "0.0005",
        "max_zero_range_bar_share": "0.1",
        "entry_mode": "cross",
        "reentry_wait_bars": 3,
        "profit_lock_switch_count": 3,
        "rotate_after_exit": false,
        "alt_exclude_btc": true,
        "fast_period": 3,
        "slow_period": 200,
        "candle_history": 260,
        "leverage": DEFAULT_LEVERAGE,
        "margin_type": "ISOLATED",
        "position_mode": "ONE_WAY",
        "margin_fraction": "0.5",
        "emergency_stop_roi_percent": 30,
        "lock_start_roi_percent": "5",
        "lock_step_percent": "5",
        "lock_gap_percent": "1",
        "stop_ma_period": STOP_MA_PERIOD,
        "working_type": "MARK_PRICE",
        "lock_working_type": "CONTRACT_PRICE",
        "tp_working_type": "CONTRACT_PRICE",
        "signal_max_age_seconds": 180,
        "reverse_retry_window_seconds": 600,
        "estimated_taker_fee_rate": "0.0005",
        "slippage_buffer_pct": "0.0005",
        "max_margin_usage_pct": "0.95",
        "protection_retry_attempts": 3,
        "protection_retry_delays_seconds": [0.5, 1.0, 2.0],
        "protection_check_interval_seconds": 60,
        "loop_interval_seconds": 10,
        "rules_refresh_seconds": 3600,
        "dry_run_balance_usdt": "100",
        "day_timezone": "Asia/Seoul",
        "allow_auto_raise_to_exchange_minimum": false,
    })
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

fn trimmed_lower_nz(value: &Value) -> String {
    py_str(value).trim().to_lowercase()
}

fn trimmed_upper_nz(value: &Value) -> String {
    py_str(value).trim().to_uppercase()
}

fn get_or<'a>(map: &'a Map<String, Value>, key: &str, default: &'a str) -> String {
    match map.get(key) {
        Some(v) if !is_falsy(v) => trimmed_lower_nz(v),
        _ => default.to_string(),
    }
}

fn get_or_upper<'a>(map: &'a Map<String, Value>, key: &str, default: &'a str) -> String {
    match map.get(key) {
        Some(v) if !is_falsy(v) => trimmed_upper_nz(v),
        _ => default.to_string(),
    }
}

fn is_int_in(value: i64, choices: &[i64]) -> bool {
    choices.contains(&value)
}

pub fn normalize_btc_ma_cross_config(raw: Option<&Value>) -> Value {
    let supplied: Map<String, Value> = match raw.and_then(|v| v.as_object()) {
        Some(obj) => obj.clone(),
        None => Map::new(),
    };
    let defaults = default_btc_ma_cross_config();
    let defaults_obj = defaults.as_object().cloned().unwrap_or_default();

    let mut cfg = default_btc_ma_cross_config();
    if let Some(map) = cfg.as_object_mut() {
        map.extend(supplied);
    }
    let cfg_obj = cfg.as_object_mut().unwrap_or_else(|| unreachable!());

    let enabled = py_bool(cfg_obj.get("enabled"), false);
    cfg_obj.insert("enabled".into(), Value::Bool(enabled));

    let mode_val = get_or_upper(cfg_obj, "trading_mode", TRADING_MODE_DRY_RUN);
    let mode_out = if TRADING_MODES.contains(&mode_val.as_str()) {
        mode_val
    } else {
        TRADING_MODE_DRY_RUN.to_string()
    };
    cfg_obj.insert("trading_mode".into(), Value::String(mode_out));

    let network_val = get_or(cfg_obj, "network", NETWORK_TESTNET);
    let network_out = if NETWORKS.contains(&network_val.as_str()) {
        network_val
    } else {
        NETWORK_TESTNET.to_string()
    };
    cfg_obj.insert("network".into(), Value::String(network_out));

    cfg_obj.insert("symbol".into(), Value::String("BTCUSDT".into()));
    cfg_obj.insert("strategy_name".into(), Value::String(STRATEGY_NAME.into()));

    let tf_val = get_or(cfg_obj, "timeframe", "1h");
    let tf_out = if TIMEFRAMES.contains(&tf_val.as_str()) {
        tf_val
    } else {
        "1h".to_string()
    };
    cfg_obj.insert("timeframe".into(), Value::String(tf_out));

    let scope_val = get_or(cfg_obj, "scan_scope", "btc");
    let scope_out = if SCAN_SCOPES.contains(&scope_val.as_str()) {
        scope_val
    } else {
        "btc".to_string()
    };
    cfg_obj.insert("scan_scope".into(), Value::String(scope_out));

    let single_input = match cfg_obj.get("single_symbol") {
        Some(v) if !is_falsy(v) => py_str(v).to_uppercase(),
        _ => String::new(),
    };
    let cleaned: String = single_input
        .chars()
        .filter(|c| c.is_alphanumeric())
        .collect();
    let single_out = if cleaned.ends_with("USDT") && cleaned.len() > 4 {
        cleaned
    } else {
        "BTCUSDT".to_string()
    };
    cfg_obj.insert("single_symbol".into(), Value::String(single_out));

    let alt_size = py_int(cfg_obj.get("alt_universe_size"), 50, 5, 100);
    cfg_obj.insert("alt_universe_size".into(), json!(alt_size));

    let rank = py_bool(cfg_obj.get("rank_testnet_by_mainnet_volume"), true);
    cfg_obj.insert("rank_testnet_by_mainnet_volume".into(), Value::Bool(rank));

    let med_range = py_decimal_text(cfg_obj.get("min_median_bar_range_pct"), 0.0005, 0.0, 0.01);
    cfg_obj.insert("min_median_bar_range_pct".into(), Value::String(med_range));
    let zero_share = py_decimal_text(cfg_obj.get("max_zero_range_bar_share"), 0.1, 0.0, 1.0);
    cfg_obj.insert("max_zero_range_bar_share".into(), Value::String(zero_share));

    let entry_val = get_or(cfg_obj, "entry_mode", "cross");
    let entry_out = if ENTRY_MODES.contains(&entry_val.as_str()) {
        entry_val
    } else {
        "cross".to_string()
    };
    cfg_obj.insert("entry_mode".into(), Value::String(entry_out));

    let wait = cfg_obj
        .get("reentry_wait_bars")
        .and_then(int_of_value)
        .unwrap_or(3);
    let wait_out = if is_int_in(wait, REENTRY_WAIT_CHOICES) {
        wait
    } else {
        3
    };
    cfg_obj.insert("reentry_wait_bars".into(), json!(wait_out));

    let default_switch = defaults_obj
        .get("profit_lock_switch_count")
        .and_then(|v| v.as_i64())
        .unwrap_or(3);
    let switch_count = match cfg_obj
        .get("profit_lock_switch_count")
        .and_then(int_of_value)
    {
        Some(v) => v,
        None => default_switch,
    };
    let switch_out = if switch_count != 0 && !is_int_in(switch_count, PROFIT_LOCK_SWITCH_CHOICES) {
        default_switch
    } else {
        switch_count
    };
    cfg_obj.insert("profit_lock_switch_count".into(), json!(switch_out));

    let rotate = py_bool(cfg_obj.get("rotate_after_exit"), false);
    cfg_obj.insert("rotate_after_exit".into(), Value::Bool(rotate));
    let alt_exclude = py_bool(cfg_obj.get("alt_exclude_btc"), true);
    cfg_obj.insert("alt_exclude_btc".into(), Value::Bool(alt_exclude));

    apply_periods(&mut cfg, 3, 200);
    let cfg_obj = cfg.as_object_mut().unwrap_or_else(|| unreachable!());

    let candle_history = py_int(cfg_obj.get("candle_history"), 260, 210, 1500);
    cfg_obj.insert("candle_history".into(), json!(candle_history));

    let leverage = match cfg_obj.get("leverage").and_then(int_of_value) {
        Some(v) => v,
        None => DEFAULT_LEVERAGE,
    };
    let leverage_out = if is_int_in(leverage, LEVERAGE_CHOICES) {
        leverage
    } else {
        DEFAULT_LEVERAGE
    };
    cfg_obj.insert("leverage".into(), json!(leverage_out));

    cfg_obj.insert("margin_type".into(), Value::String("ISOLATED".into()));
    cfg_obj.insert("position_mode".into(), Value::String("ONE_WAY".into()));

    let fraction = py_decimal_text(cfg_obj.get("margin_fraction"), 0.5, 0.05, 0.95);
    cfg_obj.insert("margin_fraction".into(), Value::String(fraction));

    let default_stop = defaults_obj
        .get("emergency_stop_roi_percent")
        .and_then(|v| v.as_i64())
        .unwrap_or(30);
    let stop = match cfg_obj
        .get("emergency_stop_roi_percent")
        .and_then(int_of_value)
    {
        Some(v) => v,
        None => default_stop,
    };
    let stop_out = if is_int_in(stop, EMERGENCY_STOP_CHOICES) {
        stop
    } else {
        default_stop
    };
    cfg_obj.insert("emergency_stop_roi_percent".into(), json!(stop_out));

    let lock_start = py_decimal_text(cfg_obj.get("lock_start_roi_percent"), 5.0, 1.0, 100.0);
    cfg_obj.insert(
        "lock_start_roi_percent".into(),
        Value::String(lock_start.clone()),
    );

    let lock_step = py_decimal_text(cfg_obj.get("lock_step_percent"), 5.0, 1.0, 50.0);
    cfg_obj.insert("lock_step_percent".into(), Value::String(lock_step));

    let lock_start_float = lock_start.parse::<f64>().unwrap_or(5.0);
    let lock_gap = py_decimal_text(cfg_obj.get("lock_gap_percent"), 1.0, 0.1, lock_start_float);
    cfg_obj.insert("lock_gap_percent".into(), Value::String(lock_gap));
    cfg_obj.insert("stop_ma_period".into(), json!(STOP_MA_PERIOD));

    for (key, default) in [
        ("working_type", "MARK_PRICE"),
        ("lock_working_type", "CONTRACT_PRICE"),
        ("tp_working_type", "CONTRACT_PRICE"),
    ] {
        let val = get_or_upper(cfg_obj, key, default);
        let out = if val == "MARK_PRICE" || val == "CONTRACT_PRICE" {
            val
        } else {
            default.to_string()
        };
        cfg_obj.insert(key.into(), Value::String(out));
    }

    let max_age = py_int(cfg_obj.get("signal_max_age_seconds"), 180, 30, 900);
    cfg_obj.insert("signal_max_age_seconds".into(), json!(max_age));
    let rev_retry = py_int(cfg_obj.get("reverse_retry_window_seconds"), 600, 60, 3600);
    cfg_obj.insert("reverse_retry_window_seconds".into(), json!(rev_retry));
    let taker = py_decimal_text(cfg_obj.get("estimated_taker_fee_rate"), 0.0005, 0.0, 0.005);
    cfg_obj.insert("estimated_taker_fee_rate".into(), Value::String(taker));
    let slip = py_decimal_text(cfg_obj.get("slippage_buffer_pct"), 0.0005, 0.0, 0.01);
    cfg_obj.insert("slippage_buffer_pct".into(), Value::String(slip));
    let margin_usage = py_decimal_text(cfg_obj.get("max_margin_usage_pct"), 0.95, 0.1, 1.0);
    cfg_obj.insert("max_margin_usage_pct".into(), Value::String(margin_usage));

    let retry = py_int(cfg_obj.get("protection_retry_attempts"), 3, 1, 6);
    cfg_obj.insert("protection_retry_attempts".into(), json!(retry));

    let delays_value = match cfg_obj.get("protection_retry_delays_seconds") {
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
    cfg_obj.insert(
        "protection_retry_delays_seconds".into(),
        Value::Array(converted),
    );

    let check = py_int(
        cfg_obj.get("protection_check_interval_seconds"),
        60,
        10,
        600,
    );
    cfg_obj.insert("protection_check_interval_seconds".into(), json!(check));
    let loop_int = py_int(cfg_obj.get("loop_interval_seconds"), 10, 5, 60);
    cfg_obj.insert("loop_interval_seconds".into(), json!(loop_int));
    let rules = py_int(cfg_obj.get("rules_refresh_seconds"), 3600, 300, 86400);
    cfg_obj.insert("rules_refresh_seconds".into(), json!(rules));

    let dry = py_decimal_text(
        cfg_obj.get("dry_run_balance_usdt"),
        100.0,
        10.0,
        10_000_000.0,
    );
    cfg_obj.insert("dry_run_balance_usdt".into(), Value::String(dry));

    let tz = match cfg_obj.get("day_timezone") {
        Some(v) if !is_falsy(v) => py_str(v),
        _ => "Asia/Seoul".to_string(),
    };
    cfg_obj.insert("day_timezone".into(), Value::String(tz));

    let raise = py_bool(cfg_obj.get("allow_auto_raise_to_exchange_minimum"), false);
    cfg_obj.insert(
        "allow_auto_raise_to_exchange_minimum".into(),
        Value::Bool(raise),
    );

    cfg
}

fn int_of_value(value: &Value) -> Option<i64> {
    match value {
        Value::Null => None,
        Value::Bool(b) => Some(if *b { 1 } else { 0 }),
        Value::Number(n) => n.as_f64().map(|f| f.trunc() as i64),
        Value::String(s) => s.trim().parse::<f64>().ok().map(|f| f.trunc() as i64),
        _ => None,
    }
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
