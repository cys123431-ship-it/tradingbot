use serde_json::{json, Map, Value};

pub const OPTIONS_CAPITAL_LIMIT_USDT: f64 = 100.0;
pub const BTC_ONLY_UNDERLYING: &str = "BTCUSDT";
/// (key, (min_dte, target_dte, max_dte), label) in Python dict order.
pub const BTC_DTE_PRESETS: [(&str, (f64, f64, f64), &str); 3] = [
    ("weekly", (3.0, 5.0, 10.0), "주간 (3~10일)"),
    ("standard", (3.0, 8.0, 21.0), "표준 (3~21일)"),
    ("monthly", (7.0, 14.0, 35.0), "월간 (7~35일)"),
];
pub const BTC_SPREAD_CHOICES: [f64; 3] = [0.04, 0.06, 0.10];
pub const BTC_MIN_QUOTE_VOLUME_USDT: f64 = 500.0;

const MULTI_UNDERLYING_DEFAULTS: [&str; 6] = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
];

/// Python truthiness of a JSON value (None/False/0/0.0/""/[]/{} are falsy).
pub fn py_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i != 0
            } else if let Some(u) = n.as_u64() {
                u != 0
            } else if let Some(f) = n.as_f64() {
                f != 0.0
            } else {
                true
            }
        }
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Python `str(value)` for a JSON value.
pub fn py_str(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::String(s) => s.clone(),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                format!("{}", i)
            } else if let Some(u) = n.as_u64() {
                format!("{}", u)
            } else if let Some(f) = n.as_f64() {
                format_float_repr(f)
            } else {
                n.to_string()
            }
        }
        Value::Array(_) => "[...]".to_string(),
        Value::Object(_) => "{...}".to_string(),
    }
}

fn format_float_repr(f: f64) -> String {
    if f.is_nan() {
        return "NaN".to_string();
    }
    if f.is_infinite() {
        return if f > 0.0 {
            "inf".to_string()
        } else {
            "-inf".to_string()
        };
    }
    let mut s = format!("{}", f);
    if !s.contains('.') && !s.contains('e') && !s.contains('E') {
        s.push_str(".0");
    }
    s
}

/// Python `float(value)`: number -> f64, bool -> 1.0/0.0, string -> trimmed parse
/// (also "inf", "-inf", "nan", "1_000" is NOT needed), anything else -> None.
pub fn py_float(value: &Value) -> Option<f64> {
    match value {
        Value::Number(n) => n.as_f64(),
        Value::Bool(true) => Some(1.0),
        Value::Bool(false) => Some(0.0),
        Value::String(s) => {
            let t = s.trim();
            if t.is_empty() {
                return None;
            }
            let lower = t.to_lowercase();
            match lower.as_str() {
                "inf" | "+inf" | "infinity" | "+infinity" => return Some(f64::INFINITY),
                "-inf" | "-infinity" => return Some(f64::NEG_INFINITY),
                "nan" | "+nan" | "-nan" => return Some(f64::NAN),
                _ => {}
            }
            t.parse::<f64>().ok()
        }
        _ => None,
    }
}

pub fn btc_dte_preset(key: &str) -> Option<(f64, f64, f64)> {
    for (k, triple, _label) in BTC_DTE_PRESETS.iter() {
        if *k == key {
            return Some(*triple);
        }
    }
    None
}

pub fn btc_dte_preset_label(key: &str) -> Option<&'static str> {
    for (k, _triple, label) in BTC_DTE_PRESETS.iter() {
        if *k == key {
            return Some(*label);
        }
    }
    None
}

pub fn default_options_config() -> Value {
    let mut m = Map::new();
    m.insert("enabled".to_string(), Value::Bool(false));
    m.insert("btc_only".to_string(), Value::Bool(false));
    m.insert("btc_dte_preset".to_string(), json!("weekly"));
    m.insert("btc_max_spread_pct".to_string(), json!(0.06));
    m.insert("profit_trigger_on_bid".to_string(), Value::Bool(false));
    m.insert(
        "strategy_profile_version".to_string(),
        json!("adaptive_convexity_trend_v2"),
    );
    m.insert("capital_limit_usdt".to_string(), json!(0.0));
    m.insert("entry_fraction".to_string(), json!(1.00));
    m.insert("scan_interval_seconds".to_string(), json!(300));
    m.insert("manage_interval_seconds".to_string(), json!(10));
    m.insert("manual_scan_cooldown_seconds".to_string(), json!(60));
    let underlyings: Vec<Value> = MULTI_UNDERLYING_DEFAULTS.iter().map(|s| json!(s)).collect();
    m.insert("underlyings".to_string(), Value::Array(underlyings));
    m.insert("signal_timeframe".to_string(), json!("1h"));
    m.insert("slow_timeframe".to_string(), json!("4h"));
    m.insert("min_abs_signal".to_string(), json!(0.46));
    m.insert("strong_signal".to_string(), json!(0.68));
    m.insert("squeeze_min_score".to_string(), json!(0.58));
    m.insert("squeeze_compression_ratio".to_string(), json!(0.78));
    m.insert("squeeze_volume_multiplier".to_string(), json!(1.15));
    m.insert("min_dte_days".to_string(), json!(2.0));
    m.insert("max_dte_days".to_string(), json!(21.0));
    m.insert("target_dte_days".to_string(), json!(8.0));
    m.insert("target_otm_pct".to_string(), json!(0.02));
    m.insert("min_abs_delta".to_string(), json!(0.20));
    m.insert("max_abs_delta".to_string(), json!(0.70));
    m.insert("preferred_delta_min".to_string(), json!(0.35));
    m.insert("preferred_delta_max".to_string(), json!(0.55));
    m.insert("max_spread_pct".to_string(), json!(0.18));
    m.insert("max_iv_to_realized".to_string(), json!(1.60));
    m.insert("hard_max_iv_to_realized".to_string(), json!(2.40));
    m.insert("squeeze_max_iv_to_realized".to_string(), json!(1.35));
    m.insert("max_surface_iv_premium_pct".to_string(), json!(0.30));
    m.insert("min_net_expected_edge_pct".to_string(), json!(0.04));
    m.insert("min_ioc_net_expected_edge_pct".to_string(), json!(0.10));
    m.insert("directional_move_scale".to_string(), json!(1.10));
    m.insert("min_flow_quote_usdt".to_string(), json!(25.0));
    m.insert("hard_negative_flow_score".to_string(), json!(-0.70));
    m.insert("min_quote_volume_usdt".to_string(), json!(50.0));
    m.insert("maker_first_enabled".to_string(), Value::Bool(true));
    m.insert("maker_wait_seconds".to_string(), json!(2.0));
    m.insert("take_profit_pct".to_string(), json!(3.00));
    m.insert("stop_loss_pct".to_string(), json!(0.55));
    m.insert("trail_activation_pct".to_string(), json!(0.50));
    m.insert("trail_drawdown_pct".to_string(), json!(0.40));
    m.insert("max_hold_hours".to_string(), json!(72.0));
    m.insert("adaptive_time_stop_hours".to_string(), json!(36.0));
    m.insert("expiry_exit_hours".to_string(), json!(8.0));
    m.insert("near_expiry_risk_days".to_string(), json!(2.0));
    m.insert("iv_collapse_exit_pct".to_string(), json!(0.30));
    m.insert("theta_burden_exit_pct".to_string(), json!(0.12));
    m.insert("delta_collapse_exit".to_string(), json!(0.15));
    m.insert("exit_signal_refresh_seconds".to_string(), json!(900));
    m.insert("max_candidates_per_underlying".to_string(), json!(10));
    m.insert("request_timeout_seconds".to_string(), json!(10));
    m.insert("rejection_stats_window".to_string(), json!(100));
    Value::Object(m)
}

fn clamp_float(v: Option<&Value>, default: f64, low: f64, high: f64) -> f64 {
    let result = match v {
        Some(val) => py_float(val).unwrap_or(default),
        None => default,
    };
    let hi = high;
    let lo = low;
    // Python max(low, nan) returns low (because nan comparisons are false).
    let mut m = if result.is_nan() {
        lo
    } else if result > lo {
        result
    } else {
        lo
    };
    if hi.is_nan() {
        // min(high, ...) with nan high returns the existing value in Python.
    } else if m > hi {
        m = hi;
    }
    m
}

fn clamp_int(v: Option<&Value>, default: i64, low: i64, high: i64) -> i64 {
    let raw = match v {
        Some(val) => match py_float(val) {
            Some(f) => {
                if f.is_finite() {
                    f.trunc() as i64
                } else {
                    default
                }
            }
            None => default,
        },
        None => default,
    };
    let mut m = raw;
    if m < low {
        m = low;
    }
    if m > high {
        m = high;
    }
    m
}

fn normalized_symbol(value: &Value) -> String {
    let s = if py_truthy(value) {
        py_str(value)
    } else {
        String::new()
    };
    s.trim().to_uppercase().replace('/', "")
}

fn as_bool_field(v: Option<&Value>, default: bool) -> bool {
    match v {
        None => default,
        Some(Value::Bool(b)) => *b,
        Some(Value::Null) => default,
        Some(other) => {
            let l = py_str(other).trim().to_lowercase();
            matches!(l.as_str(), "1" | "true" | "yes" | "on" | "enabled")
        }
    }
}

pub fn normalize_btc_spread_choice(value: &Value) -> f64 {
    let requested = py_float(value).unwrap_or(0.06);
    let mut best = BTC_SPREAD_CHOICES[0];
    let mut best_dist = (best - requested).abs();
    for choice in BTC_SPREAD_CHOICES.iter().skip(1) {
        let dist = (*choice - requested).abs();
        if dist < best_dist {
            best = *choice;
            best_dist = dist;
        }
    }
    best
}

fn apply_btc_only_profile(cfg: &mut Map<String, Value>) {
    let btc_only = as_bool_field(cfg.get("btc_only"), false);
    cfg.insert("btc_only".to_string(), Value::Bool(btc_only));

    let preset_raw = cfg.get("btc_dte_preset");
    let preset_base = match preset_raw {
        Some(v) if py_truthy(v) => py_str(v),
        _ => "weekly".to_string(),
    };
    let preset = preset_base.trim().to_lowercase();
    let preset = if btc_dte_preset(&preset).is_some() {
        preset
    } else {
        "weekly".to_string()
    };
    cfg.insert("btc_dte_preset".to_string(), Value::String(preset.clone()));

    let spread_value = cfg
        .get("btc_max_spread_pct")
        .cloned()
        .unwrap_or(Value::Null);
    let spread = normalize_btc_spread_choice(&spread_value);
    cfg.insert("btc_max_spread_pct".to_string(), json!(spread));

    let profit_bid = as_bool_field(cfg.get("profit_trigger_on_bid"), false);
    cfg.insert("profit_trigger_on_bid".to_string(), Value::Bool(profit_bid));

    if !btc_only {
        return;
    }

    cfg.insert(
        "underlyings".to_string(),
        Value::Array(vec![json!(BTC_ONLY_UNDERLYING)]),
    );
    if let Some((min_dte, target_dte, max_dte)) = btc_dte_preset(&preset) {
        cfg.insert("min_dte_days".to_string(), json!(min_dte));
        cfg.insert("target_dte_days".to_string(), json!(target_dte));
        cfg.insert("max_dte_days".to_string(), json!(max_dte));
    }
    cfg.insert("max_spread_pct".to_string(), json!(spread));
    let mq = cfg
        .get("min_quote_volume_usdt")
        .and_then(py_float)
        .filter(|_| {
            cfg.get("min_quote_volume_usdt")
                .map(py_truthy)
                .unwrap_or(false)
        })
        .unwrap_or(0.0);
    let mq = if mq > BTC_MIN_QUOTE_VOLUME_USDT {
        mq
    } else {
        BTC_MIN_QUOTE_VOLUME_USDT
    };
    cfg.insert("min_quote_volume_usdt".to_string(), json!(mq));
    cfg.insert("profit_trigger_on_bid".to_string(), Value::Bool(true));
}

pub fn normalize_options_config(raw: &Value) -> Value {
    let supplied: Map<String, Value> = match raw {
        Value::Object(o) => o.clone(),
        _ => Map::new(),
    };

    let mut cfg_all: Map<String, Value> = match default_options_config() {
        Value::Object(o) => o,
        _ => Map::new(),
    };
    if !supplied.is_empty() {
        for (k, v) in supplied.iter() {
            cfg_all.insert(k.clone(), v.clone());
        }
    }

    let migrating_v2 = !matches!(
        supplied.get("adaptive_convexity_v2_migration_complete"),
        Some(Value::Bool(true))
    );

    cfg_all.insert(
        "strategy_profile_version".to_string(),
        json!("adaptive_convexity_trend_v2"),
    );

    if migrating_v2 {
        let legacy_exit_defaults: [(&str, f64); 4] = [
            ("take_profit_pct", 0.80),
            ("stop_loss_pct", 0.45),
            ("trail_activation_pct", 0.35),
            ("trail_drawdown_pct", 0.25),
        ];
        let defaults = default_options_config();
        for (key, legacy_value) in legacy_exit_defaults.iter() {
            let value = supplied.get(*key).cloned().unwrap_or(json!(*legacy_value));
            let unchanged = match py_float(&value) {
                Some(v) => (v - *legacy_value).abs() <= 1e-12,
                None => false,
            };
            if unchanged {
                if let Some(dv) = defaults.get(*key) {
                    cfg_all.insert(key.to_string(), dv.clone());
                }
            }
        }
    }
    cfg_all.insert(
        "adaptive_convexity_v2_migration_complete".to_string(),
        Value::Bool(true),
    );

    let enabled = cfg_all.get("enabled").map(py_truthy).unwrap_or(false);
    cfg_all.insert("enabled".to_string(), Value::Bool(enabled));

    if !matches!(
        supplied.get("wallet_budget_migration_complete"),
        Some(Value::Bool(true))
    ) {
        cfg_all.insert("capital_limit_usdt".to_string(), json!(0.0));
    }
    cfg_all.insert(
        "wallet_budget_migration_complete".to_string(),
        Value::Bool(true),
    );
    let cl = clamp_float(cfg_all.get("capital_limit_usdt"), 0.0, 0.0, 1_000_000.0);
    cfg_all.insert("capital_limit_usdt".to_string(), json!(cl));

    cfg_all.insert("entry_fraction".to_string(), json!(1.00));
    let scan = clamp_int(cfg_all.get("scan_interval_seconds"), 300, 60, 3600);
    cfg_all.insert("scan_interval_seconds".to_string(), json!(scan));
    let manage = clamp_int(cfg_all.get("manage_interval_seconds"), 10, 5, 10);
    cfg_all.insert("manage_interval_seconds".to_string(), json!(manage));
    let cooldown = clamp_int(cfg_all.get("manual_scan_cooldown_seconds"), 60, 15, 300);
    cfg_all.insert("manual_scan_cooldown_seconds".to_string(), json!(cooldown));

    // underlyings (Python would crash on a non-list; we fall back to defaults)
    let underlyings_arr: Option<Vec<Value>> = match cfg_all.get("underlyings") {
        Some(Value::Array(a)) => Some(a.clone()),
        _ => None,
    };
    let mut current_underlyings = match underlyings_arr {
        Some(a) => a,
        None => {
            // Python would crash on non-list; we fall back to defaults.
            vec![
                json!("BTCUSDT"),
                json!("ETHUSDT"),
                json!("SOLUSDT"),
                json!("BNBUSDT"),
                json!("XRPUSDT"),
                json!("DOGEUSDT"),
            ]
        }
    };
    if migrating_v2 {
        let legacy_order = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"];
        let matches_legacy = current_underlyings.len() == legacy_order.len()
            && current_underlyings
                .iter()
                .zip(legacy_order.iter())
                .all(|(v, exp)| v.as_str().map(|s| s == *exp).unwrap_or(false));
        if matches_legacy {
            current_underlyings = vec![
                json!("BTCUSDT"),
                json!("ETHUSDT"),
                json!("SOLUSDT"),
                json!("BNBUSDT"),
                json!("XRPUSDT"),
                json!("DOGEUSDT"),
            ];
        }
    }
    let mut normalized: Vec<String> = Vec::new();
    for value in current_underlyings.iter() {
        let symbol = normalized_symbol(value);
        if !symbol.is_empty() && symbol.ends_with("USDT") && !normalized.contains(&symbol) {
            normalized.push(symbol);
        }
    }
    let final_underlyings: Vec<Value> = if normalized.is_empty() {
        vec![json!("BTCUSDT"), json!("ETHUSDT")]
    } else {
        normalized
            .iter()
            .take(8)
            .map(|s| json!(s.clone()))
            .collect()
    };
    cfg_all.insert("underlyings".to_string(), Value::Array(final_underlyings));

    cfg_all.insert("signal_timeframe".to_string(), json!("1h"));
    cfg_all.insert("slow_timeframe".to_string(), json!("4h"));

    let min_abs = clamp_float(cfg_all.get("min_abs_signal"), 0.46, 0.30, 0.90);
    cfg_all.insert("min_abs_signal".to_string(), json!(min_abs));
    let mut strong = clamp_float(cfg_all.get("strong_signal"), 0.68, 0.50, 0.98);
    if strong <= min_abs {
        strong = (min_abs + 0.15).min(0.98);
    }
    cfg_all.insert("strong_signal".to_string(), json!(strong));

    let squeeze_min = clamp_float(cfg_all.get("squeeze_min_score"), 0.58, 0.35, 0.95);
    cfg_all.insert("squeeze_min_score".to_string(), json!(squeeze_min));
    let squeeze_ratio = clamp_float(cfg_all.get("squeeze_compression_ratio"), 0.78, 0.45, 0.95);
    cfg_all.insert(
        "squeeze_compression_ratio".to_string(),
        json!(squeeze_ratio),
    );
    let squeeze_vol = clamp_float(cfg_all.get("squeeze_volume_multiplier"), 1.15, 1.0, 3.0);
    cfg_all.insert("squeeze_volume_multiplier".to_string(), json!(squeeze_vol));

    let min_dte = clamp_float(cfg_all.get("min_dte_days"), 2.0, 1.0, 30.0);
    cfg_all.insert("min_dte_days".to_string(), json!(min_dte));
    let mut max_dte = clamp_float(cfg_all.get("max_dte_days"), 21.0, 2.0, 90.0);
    if max_dte <= min_dte {
        max_dte = min_dte + 1.0;
    }
    cfg_all.insert("max_dte_days".to_string(), json!(max_dte));

    let target_dte = clamp_float(cfg_all.get("target_dte_days"), 8.0, min_dte, max_dte);
    cfg_all.insert("target_dte_days".to_string(), json!(target_dte));

    let target_otm = clamp_float(cfg_all.get("target_otm_pct"), 0.02, 0.0, 0.15);
    cfg_all.insert("target_otm_pct".to_string(), json!(target_otm));

    let min_delta = clamp_float(cfg_all.get("min_abs_delta"), 0.20, 0.10, 0.60);
    cfg_all.insert("min_abs_delta".to_string(), json!(min_delta));
    let mut max_delta = clamp_float(cfg_all.get("max_abs_delta"), 0.70, 0.30, 0.95);
    if max_delta <= min_delta {
        max_delta = (min_delta + 0.20).min(0.95);
    }
    cfg_all.insert("max_abs_delta".to_string(), json!(max_delta));

    let pref_min = clamp_float(
        cfg_all.get("preferred_delta_min"),
        0.35,
        min_delta,
        max_delta,
    );
    cfg_all.insert("preferred_delta_min".to_string(), json!(pref_min));
    let mut pref_max = clamp_float(
        cfg_all.get("preferred_delta_max"),
        0.55,
        min_delta,
        max_delta,
    );
    if pref_max <= pref_min {
        pref_max = (pref_min + 0.15).min(max_delta);
    }
    cfg_all.insert("preferred_delta_max".to_string(), json!(pref_max));

    let spread = clamp_float(cfg_all.get("max_spread_pct"), 0.18, 0.02, 0.40);
    cfg_all.insert("max_spread_pct".to_string(), json!(spread));

    let max_iv = clamp_float(cfg_all.get("max_iv_to_realized"), 1.60, 0.70, 3.00);
    cfg_all.insert("max_iv_to_realized".to_string(), json!(max_iv));
    let hard_iv = clamp_float(cfg_all.get("hard_max_iv_to_realized"), 2.40, max_iv, 5.00);
    cfg_all.insert("hard_max_iv_to_realized".to_string(), json!(hard_iv));
    let squeeze_iv = clamp_float(
        cfg_all.get("squeeze_max_iv_to_realized"),
        1.35,
        0.60,
        hard_iv,
    );
    cfg_all.insert("squeeze_max_iv_to_realized".to_string(), json!(squeeze_iv));

    let surface = clamp_float(cfg_all.get("max_surface_iv_premium_pct"), 0.30, 0.05, 1.00);
    cfg_all.insert("max_surface_iv_premium_pct".to_string(), json!(surface));

    let net_edge = clamp_float(cfg_all.get("min_net_expected_edge_pct"), 0.04, 0.0, 1.00);
    cfg_all.insert("min_net_expected_edge_pct".to_string(), json!(net_edge));

    let ioc_edge = clamp_float(
        cfg_all.get("min_ioc_net_expected_edge_pct"),
        0.10,
        net_edge,
        1.50,
    );
    cfg_all.insert("min_ioc_net_expected_edge_pct".to_string(), json!(ioc_edge));

    let directional = clamp_float(cfg_all.get("directional_move_scale"), 1.10, 0.20, 2.00);
    cfg_all.insert("directional_move_scale".to_string(), json!(directional));

    let min_flow = clamp_float(cfg_all.get("min_flow_quote_usdt"), 25.0, 0.0, 100000.0);
    cfg_all.insert("min_flow_quote_usdt".to_string(), json!(min_flow));

    let neg_flow = clamp_float(cfg_all.get("hard_negative_flow_score"), -0.70, -1.0, -0.20);
    cfg_all.insert("hard_negative_flow_score".to_string(), json!(neg_flow));

    let mut min_qv = clamp_float(cfg_all.get("min_quote_volume_usdt"), 50.0, 0.0, 100000.0);
    cfg_all.insert("min_quote_volume_usdt".to_string(), json!(min_qv));

    let maker_first_val = cfg_all
        .get("maker_first_enabled")
        .cloned()
        .unwrap_or(Value::Bool(true));
    let maker_first = match &maker_first_val {
        Value::Bool(b) => *b,
        _ => {
            let l = py_str(&maker_first_val).trim().to_lowercase();
            matches!(l.as_str(), "1" | "true" | "yes" | "on" | "enabled")
        }
    };
    cfg_all.insert("maker_first_enabled".to_string(), Value::Bool(maker_first));

    let maker_wait = clamp_float(cfg_all.get("maker_wait_seconds"), 2.0, 0.5, 10.0);
    cfg_all.insert("maker_wait_seconds".to_string(), json!(maker_wait));

    let take_profit = clamp_float(cfg_all.get("take_profit_pct"), 3.00, 0.50, 5.00);
    cfg_all.insert("take_profit_pct".to_string(), json!(take_profit));
    let stop_loss = clamp_float(cfg_all.get("stop_loss_pct"), 0.55, 0.10, 0.90);
    cfg_all.insert("stop_loss_pct".to_string(), json!(stop_loss));
    let trail_act = clamp_float(cfg_all.get("trail_activation_pct"), 0.50, 0.10, 3.00);
    cfg_all.insert("trail_activation_pct".to_string(), json!(trail_act));
    let trail_dd = clamp_float(cfg_all.get("trail_drawdown_pct"), 0.40, 0.05, 0.75);
    cfg_all.insert("trail_drawdown_pct".to_string(), json!(trail_dd));

    let max_hold = clamp_float(cfg_all.get("max_hold_hours"), 72.0, 2.0, 720.0);
    cfg_all.insert("max_hold_hours".to_string(), json!(max_hold));
    let adaptive = clamp_float(cfg_all.get("adaptive_time_stop_hours"), 36.0, 4.0, max_hold);
    cfg_all.insert("adaptive_time_stop_hours".to_string(), json!(adaptive));

    let expiry = clamp_float(cfg_all.get("expiry_exit_hours"), 8.0, 1.0, 48.0);
    cfg_all.insert("expiry_exit_hours".to_string(), json!(expiry));
    let near_expiry = clamp_float(cfg_all.get("near_expiry_risk_days"), 2.0, 0.5, 7.0);
    cfg_all.insert("near_expiry_risk_days".to_string(), json!(near_expiry));
    let iv_collapse = clamp_float(cfg_all.get("iv_collapse_exit_pct"), 0.30, 0.10, 0.80);
    cfg_all.insert("iv_collapse_exit_pct".to_string(), json!(iv_collapse));
    let theta = clamp_float(cfg_all.get("theta_burden_exit_pct"), 0.12, 0.03, 0.50);
    cfg_all.insert("theta_burden_exit_pct".to_string(), json!(theta));
    let delta_collapse = clamp_float(cfg_all.get("delta_collapse_exit"), 0.15, 0.05, min_delta);
    cfg_all.insert("delta_collapse_exit".to_string(), json!(delta_collapse));

    let exit_refresh = clamp_int(cfg_all.get("exit_signal_refresh_seconds"), 900, 300, 3600);
    cfg_all.insert(
        "exit_signal_refresh_seconds".to_string(),
        json!(exit_refresh),
    );
    let max_cand = clamp_int(cfg_all.get("max_candidates_per_underlying"), 10, 1, 20);
    cfg_all.insert("max_candidates_per_underlying".to_string(), json!(max_cand));
    let timeout = clamp_int(cfg_all.get("request_timeout_seconds"), 10, 3, 30);
    cfg_all.insert("request_timeout_seconds".to_string(), json!(timeout));
    let rejection = clamp_int(cfg_all.get("rejection_stats_window"), 100, 20, 500);
    cfg_all.insert("rejection_stats_window".to_string(), json!(rejection));

    // Recompute min_qv reference after previous clamps but keep btc_only behavior.
    let _ = &mut min_qv;

    apply_btc_only_profile(&mut cfg_all);

    Value::Object(cfg_all)
}

pub fn options_spend_limit(cfg: &Value, available_usdt: f64) -> f64 {
    let available = {
        let a = if available_usdt.is_finite() && available_usdt != 0.0 {
            available_usdt
        } else {
            0.0
        };
        if a > 0.0 {
            a
        } else {
            0.0
        }
    };
    let cap = match cfg {
        Value::Object(m) => match m.get("capital_limit_usdt") {
            Some(v) if py_truthy(v) => py_float(v).unwrap_or(0.0),
            _ => 0.0,
        },
        _ => 0.0,
    };
    let cap = if cap > 0.0 { cap } else { 0.0 };
    if cap > 0.0 {
        if cap < available {
            cap
        } else {
            available
        }
    } else {
        available
    }
}

pub fn options_budget_text(cfg: &Value) -> String {
    let cap = match cfg {
        Value::Object(m) => match m.get("capital_limit_usdt") {
            Some(v) if py_truthy(v) => py_float(v).unwrap_or(0.0),
            _ => 0.0,
        },
        _ => 0.0,
    };
    if cap > 0.0 {
        format!("옵션 지갑 잔고 (최대 {:.0} USDT)", cap)
    } else {
        "옵션 지갑 가용 잔고 전부 (고정 한도 없음)".to_string()
    }
}

pub fn multi_underlying_restore_values() -> Map<String, Value> {
    let defaults = default_options_config();
    let mut out = Map::new();
    let keys = [
        "underlyings",
        "min_dte_days",
        "target_dte_days",
        "max_dte_days",
        "max_spread_pct",
        "min_quote_volume_usdt",
        "profit_trigger_on_bid",
    ];
    for key in keys.iter() {
        if let Some(v) = defaults.get(*key) {
            out.insert((*key).to_string(), v.clone());
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn enabled_and_capital_migration() {
        let cfg = normalize_options_config(&json!({
            "enabled": true,
            "capital_limit_usdt": 999
        }));
        assert_eq!(cfg.get("enabled"), Some(&Value::Bool(true)));
        let cap = cfg
            .get("capital_limit_usdt")
            .and_then(|v| v.as_f64())
            .unwrap_or(-1.0);
        assert!((cap - 0.0).abs() < 1e-12);
    }

    #[test]
    fn default_enabled_false() {
        let cfg = normalize_options_config(&json!({}));
        assert_eq!(cfg.get("enabled"), Some(&Value::Bool(false)));
    }

    #[test]
    fn btc_only_profile() {
        let cfg = normalize_options_config(&json!({
            "btc_only": true,
            "btc_dte_preset": "monthly",
            "btc_max_spread_pct": 0.1
        }));
        assert_eq!(cfg.get("underlyings"), Some(&json!(["BTCUSDT"])));
        let min_dte = cfg
            .get("min_dte_days")
            .and_then(|v| v.as_f64())
            .unwrap_or(-1.0);
        assert!((min_dte - 7.0).abs() < 1e-12);
        let max_dte = cfg
            .get("max_dte_days")
            .and_then(|v| v.as_f64())
            .unwrap_or(-1.0);
        assert!((max_dte - 35.0).abs() < 1e-12);
        let mspread = cfg
            .get("max_spread_pct")
            .and_then(|v| v.as_f64())
            .unwrap_or(-1.0);
        assert!((mspread - 0.1).abs() < 1e-12);
        assert_eq!(cfg.get("profit_trigger_on_bid"), Some(&Value::Bool(true)));
        let mq = cfg
            .get("min_quote_volume_usdt")
            .and_then(|v| v.as_f64())
            .unwrap_or(-1.0);
        assert!((mq - 500.0).abs() < 1e-12);
    }
}
