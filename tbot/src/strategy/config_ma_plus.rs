//! Configuration for the /macross strategy (SMA3/SMA120 + selectable features).

use serde_json::{json, Map, Value};

pub use crate::core::ma_plus::{feature_mode, FEATURES, FEATURE_FIELDS};
use crate::core::periods::apply_periods;
use crate::strategy::config_base::{
    py_bool, py_decimal_text, py_int, NETWORKS, NETWORK_TESTNET, TRADING_MODES,
    TRADING_MODE_DRY_RUN,
};
use crate::strategy::tradfi::TRADFI_SCOPE;

pub use crate::core::ma_plus::{
    feature_mode as feature_mode_pub, FEATURE_FIELDS as FEATURE_FIELDS_PUB,
};
#[allow(unused_imports)]
pub use crate::core::ma_plus::{FEATURES as _, FEATURE_FIELDS as _};

pub const STRATEGY_NAME: &str = "sma3_sma120_macross";
pub const STRATEGY_VERSION: &str = "1.0.0";
pub const CLIENT_ID_STRATEGY: &str = "mcross";
pub const CONFIG_KEY: &str = "ma_cross";

pub const TIMEFRAMES: &[&str] = &["15m", "30m", "1h", "2h", "4h"];
pub const DEFAULT_FEATURES: &[&str] = &["B"];
pub const FEATURE_LABELS: &[(&str, &str)] = &[
    ("A", "추세필터(SMA120+ADX/ER)"),
    ("B", "ATR 초기손절"),
    ("C", "변동성 타겟 사이징"),
    ("D", "ATR 샹들리에 트레일링"),
    ("E", "Donchian 돌파 트리거"),
];
pub const SCAN_SCOPES: &[&str] = &["btc", "alts", "single", TRADFI_SCOPE];
pub const ENTRY_MODES: &[&str] = &["cross", "trend"];
pub const EMERGENCY_STOP_CHOICES: &[i64] = &[0, 10, 20, 30, 50];
pub const LEVERAGE_CHOICES: &[i64] = &[2, 3, 4, 5];
pub const DEFAULT_LEVERAGE: i64 = 5;
pub const REENTRY_WAIT_CHOICES: &[i64] = &[1, 3, 6, 0];

// Re-export the canonical feature strings/fields.
#[allow(unused_imports)]
pub use crate::core::ma_plus::{FEATURES as FEATURES_REE, FEATURE_FIELDS as FEATURE_FIELDS_REE};

pub use crate::core::ma_plus::feature_mode as feature_str;

// Make FEATURES/FEATURE_FIELDS public via re-exports.
pub use crate::core::ma_plus::FEATURES as features;
pub use crate::core::ma_plus::FEATURE_FIELDS as feature_fields;

#[allow(dead_code)]
fn _use_imports() {
    let _ = (feature_mode, FEATURE_FIELDS, FEATURES);
}

pub fn default_ma_cross_config() -> Value {
    json!({
        "enabled": false,
        "trading_mode": TRADING_MODE_DRY_RUN,
        "network": NETWORK_TESTNET,
        "symbol": "BTCUSDT",
        "strategy_name": STRATEGY_NAME,
        "timeframe": "4h",
        "scan_scope": "btc",
        "single_symbol": "BTCUSDT",
        "alt_universe_size": 50,
        "rank_testnet_by_mainnet_volume": true,
        "min_median_bar_range_pct": "0.0005",
        "max_zero_range_bar_share": "0.1",
        "entry_mode": "cross",
        "reentry_wait_bars": 3,
        "rotate_after_exit": false,
        "alt_exclude_btc": true,
        "fast_period": 3,
        "slow_period": 120,
        "candle_history": 500,
        "leverage": DEFAULT_LEVERAGE,
        "margin_type": "ISOLATED",
        "position_mode": "ONE_WAY",
        "margin_fraction": "0.5",
        "emergency_stop_roi_percent": 0,
        "feature_a_trend_filter": 0,
        "feature_a_adx_min": "18",
        "feature_a_efficiency_min": "0.20",
        "feature_b_atr_stop": 0,
        "feature_b_atr_multiplier": "2.0",
        "feature_c_vol_sizing": 1,
        "feature_c_target_atr_pct": "0.03",
        "feature_c_min_scale": "0.5",
        "feature_c_max_scale": "1.5",
        "feature_d_chandelier": 0,
        "feature_d_atr_multiplier": "3.0",
        "feature_e_donchian": 0,
        "feature_e_period": 20,
        "locking": true,
        "lock_start_roi_percent": "5",
        "lock_step_percent": "5",
        "lock_gap_percent": "1",
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

fn get_or(map: &Map<String, Value>, key: &str, default: &str) -> String {
    match map.get(key) {
        Some(v) if !is_falsy(v) => py_str(v).trim().to_lowercase(),
        _ => default.to_string(),
    }
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

pub fn normalize_ma_cross_config(raw: Option<&Value>) -> Value {
    let supplied: Map<String, Value> = match raw.and_then(|v| v.as_object()) {
        Some(obj) => obj.clone(),
        None => Map::new(),
    };
    let defaults = default_ma_cross_config();
    let defaults_obj = defaults.as_object().cloned().unwrap_or_default();

    let mut cfg = default_ma_cross_config();
    if let Some(map) = cfg.as_object_mut() {
        map.extend(supplied);
    }
    let cfg_obj = cfg.as_object_mut().unwrap_or_else(|| unreachable!());

    let enabled = py_bool(cfg_obj.get("enabled"), false);
    cfg_obj.insert("enabled".into(), Value::Bool(enabled));

    let mode_val = match cfg_obj.get("trading_mode") {
        Some(v) if !is_falsy(v) => py_str(v).trim().to_uppercase(),
        _ => TRADING_MODE_DRY_RUN.to_string(),
    };
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

    let tf_val = get_or(cfg_obj, "timeframe", "4h");
    let tf_out = if TIMEFRAMES.contains(&tf_val.as_str()) {
        tf_val
    } else {
        "4h".to_string()
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
    let wait_out = if REENTRY_WAIT_CHOICES.contains(&wait) {
        wait
    } else {
        3
    };
    cfg_obj.insert("reentry_wait_bars".into(), json!(wait_out));

    let rotate = py_bool(cfg_obj.get("rotate_after_exit"), false);
    cfg_obj.insert("rotate_after_exit".into(), Value::Bool(rotate));
    let alt_exclude = py_bool(cfg_obj.get("alt_exclude_btc"), true);
    cfg_obj.insert("alt_exclude_btc".into(), Value::Bool(alt_exclude));

    apply_periods(&mut cfg, 3, 120);
    let cfg_obj = cfg.as_object_mut().unwrap_or_else(|| unreachable!());

    let candle_history = py_int(cfg_obj.get("candle_history"), 500, 210, 2000);
    cfg_obj.insert("candle_history".into(), json!(candle_history));

    let leverage = match cfg_obj.get("leverage").and_then(int_of_value) {
        Some(v) => v,
        None => DEFAULT_LEVERAGE,
    };
    let leverage_out = if LEVERAGE_CHOICES.contains(&leverage) {
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
        .unwrap_or(0);
    let stop = match cfg_obj
        .get("emergency_stop_roi_percent")
        .and_then(int_of_value)
    {
        Some(v) => v,
        None => default_stop,
    };
    let stop_out = if EMERGENCY_STOP_CHOICES.contains(&stop) {
        stop
    } else {
        default_stop
    };
    cfg_obj.insert("emergency_stop_roi_percent".into(), json!(stop_out));

    for (_, field) in FEATURE_FIELDS.iter() {
        let v = py_bool(cfg_obj.get(*field), false);
        cfg_obj.insert(
            (*field).to_string(),
            Value::Number(serde_json::Number::from(if v { 1 } else { 0 })),
        );
    }

    cfg_obj.insert("feature_c_vol_sizing".into(), json!(1));
    for name in ["A", "B", "D", "E"] {
        if let Some((_, field)) = FEATURE_FIELDS.iter().find(|(n, _)| *n == name) {
            cfg_obj.insert((*field).to_string(), json!(0));
        }
    }

    cfg_obj.insert("locking".into(), Value::Bool(true));
    cfg_obj.insert("emergency_stop_roi_percent".into(), json!(0));

    let adx = py_decimal_text(cfg_obj.get("feature_a_adx_min"), 18.0, 0.0, 60.0);
    cfg_obj.insert("feature_a_adx_min".into(), Value::String(adx));
    let eff = py_decimal_text(cfg_obj.get("feature_a_efficiency_min"), 0.20, 0.0, 1.0);
    cfg_obj.insert("feature_a_efficiency_min".into(), Value::String(eff));
    let bmult = py_decimal_text(cfg_obj.get("feature_b_atr_multiplier"), 2.0, 0.5, 6.0);
    cfg_obj.insert("feature_b_atr_multiplier".into(), Value::String(bmult));
    let target = py_decimal_text(cfg_obj.get("feature_c_target_atr_pct"), 0.03, 0.001, 0.5);
    cfg_obj.insert("feature_c_target_atr_pct".into(), Value::String(target));
    let min_scale = py_decimal_text(cfg_obj.get("feature_c_min_scale"), 0.5, 0.1, 1.0);
    cfg_obj.insert("feature_c_min_scale".into(), Value::String(min_scale));
    let max_scale = py_decimal_text(cfg_obj.get("feature_c_max_scale"), 1.5, 1.0, 3.0);
    cfg_obj.insert("feature_c_max_scale".into(), Value::String(max_scale));
    let dmult = py_decimal_text(cfg_obj.get("feature_d_atr_multiplier"), 3.0, 1.0, 8.0);
    cfg_obj.insert("feature_d_atr_multiplier".into(), Value::String(dmult));
    let eperiod = py_int(cfg_obj.get("feature_e_period"), 20, 5, 100);
    cfg_obj.insert("feature_e_period".into(), json!(eperiod));

    cfg
}
