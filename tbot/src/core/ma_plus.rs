use serde::Serialize;
use serde_json::Value;

use crate::core::candles::Candle;
use crate::core::indicators::{
    adx, atr, chandelier_stop, donchian, sma, trend_efficiency, volatility_target_scale,
};

pub const CROSS_LONG: &str = "LONG";
pub const CROSS_SHORT: &str = "SHORT";
pub const TRIGGER_SMA: &str = "SMA_CROSS";
pub const TRIGGER_DONCHIAN: &str = "DONCHIAN";

pub const FEATURES: [&str; 5] = ["A", "B", "C", "D", "E"];
pub const FEATURE_FIELDS: [(&str, &str); 5] = [
    ("A", "feature_a_trend_filter"),
    ("B", "feature_b_atr_stop"),
    ("C", "feature_c_vol_sizing"),
    ("D", "feature_d_chandelier"),
    ("E", "feature_e_donchian"),
];

/// Python-style truthiness for a JSON value.
fn py_truthy(value: &Value) -> bool {
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
                false
            }
        }
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Mirror of Python `_bool`: bool passthrough, otherwise lowercased membership.
fn py_bool(value: Option<&Value>, default: bool) -> bool {
    match value {
        None => default,
        Some(Value::Bool(b)) => *b,
        Some(Value::Null) => default,
        Some(v) => {
            let text = match v {
                Value::String(s) => s.clone(),
                other => other.to_string(),
            };
            let lowered = text.trim().to_lowercase();
            matches!(lowered.as_str(), "1" | "true" | "yes" | "on" | "enabled")
        }
    }
}

/// Python `_f`: float(value) or fallback.
fn f_opt(value: Option<&Value>) -> Option<f64> {
    let v = value?;
    match v {
        Value::Null => None,
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        Value::Number(n) => n.as_f64(),
        Value::String(s) => {
            let t = s.trim();
            if t.is_empty() {
                None
            } else {
                t.parse::<f64>().ok()
            }
        }
        _ => None,
    }
}

fn float_field(cfg: &Value, key: &str) -> f64 {
    f_opt(cfg.get(key)).unwrap_or(f64::NAN)
}

fn int_field(cfg: &Value, key: &str) -> i64 {
    match cfg.get(key) {
        Some(v) => match f_opt(Some(v)) {
            Some(f) => f as i64,
            None => 0, // non-numeric value; Python would raise, configs are normalized first
        },
        None => 0,
    }
}

/// Canonical feature string like `"ABD"` in A..E order.
pub fn feature_mode(cfg: &Value) -> String {
    let mut out = String::new();
    for (name, field) in FEATURE_FIELDS.iter() {
        let flag = py_bool(cfg.get(*field), false);
        if flag {
            out.push_str(name);
        }
    }
    out
}

#[derive(Serialize)]
pub struct Frame {
    pub closes: Vec<Option<f64>>,
    pub fast: Vec<Option<f64>>,
    pub slow: Vec<Option<f64>>,
    pub atr: Vec<Option<f64>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub adx: Option<Vec<Option<f64>>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub efficiency: Option<Vec<Option<f64>>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub donchian_upper: Option<Vec<Option<f64>>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub donchian_lower: Option<Vec<Option<f64>>>,
}

impl Frame {
    pub fn len(&self) -> usize {
        self.closes.len()
    }

    pub fn is_empty(&self) -> bool {
        self.closes.is_empty()
    }
}

fn aligned(values: &[Option<f64>], length: usize) -> Vec<Option<f64>> {
    if values.is_empty() {
        return vec![None; length];
    }
    if values.len() == length {
        return values.to_vec();
    }
    let missing = length as i64 - values.len() as i64;
    let pad = missing.max(0) as usize;
    let take = values.len().min(length);
    let tail = &values[values.len() - take..];
    let mut out: Vec<Option<f64>> = vec![None; pad];
    out.extend_from_slice(tail);
    out
}

/// All indicators the features need, aligned with `rows`.
pub fn indicator_frame(rows: &[Candle], cfg: &Value) -> Frame {
    let closes: Vec<f64> = rows.iter().map(|r| r[4]).collect();
    let length = rows.len();
    let fast_period = int_field(cfg, "fast_period").max(0) as usize;
    let slow_period = int_field(cfg, "slow_period").max(0) as usize;
    let fast = aligned(&sma(&closes, fast_period), length);
    let slow = aligned(&sma(&closes, slow_period), length);
    let atr_values = aligned(&atr(rows, 14), length);
    let mut frame = Frame {
        closes: closes.iter().map(|c| Some(*c)).collect(),
        fast,
        slow,
        atr: atr_values,
        adx: None,
        efficiency: None,
        donchian_upper: None,
        donchian_lower: None,
    };
    let mode = feature_mode(cfg);
    if mode.contains('A') {
        frame.adx = Some(aligned(&adx(rows, 14), length));
        frame.efficiency = Some(aligned(&trend_efficiency(&closes, 20), length));
    }
    if mode.contains('E') {
        let period = int_field(cfg, "feature_e_period").max(0) as usize;
        let (upper, lower) = donchian(rows, period);
        frame.donchian_upper = Some(upper);
        frame.donchian_lower = Some(lower);
    }
    frame
}

fn get_at(values: Option<&Vec<Option<f64>>>, index: usize) -> Option<f64> {
    let v = values?;
    if index < v.len() {
        v[index]
    } else {
        None
    }
}

/// (side, trigger) of a fresh cross / breakout at `index`, else (None, None).
pub fn cross_side(frame: &Frame, index: usize) -> (Option<&'static str>, Option<&'static str>) {
    let fast = &frame.fast;
    let slow = &frame.slow;
    if index < 1 || index >= fast.len() {
        return (None, None);
    }
    if fast[index].is_none()
        || slow[index].is_none()
        || fast[index - 1].is_none()
        || slow[index - 1].is_none()
    {
        return (None, None);
    }
    let f_prev = fast[index - 1].unwrap();
    let s_prev = slow[index - 1].unwrap();
    let f_cur = fast[index].unwrap();
    let s_cur = slow[index].unwrap();
    if f_prev <= s_prev && f_cur > s_cur {
        return (Some(CROSS_LONG), Some(TRIGGER_SMA));
    }
    if f_prev >= s_prev && f_cur < s_cur {
        return (Some(CROSS_SHORT), Some(TRIGGER_SMA));
    }
    if let (Some(upper), Some(lower)) = (&frame.donchian_upper, &frame.donchian_lower) {
        let close = frame.closes[index];
        if let Some(close) = close {
            if let Some(u) = get_at(Some(upper), index) {
                if close > u {
                    return (Some(CROSS_LONG), Some(TRIGGER_DONCHIAN));
                }
            }
            if let Some(l) = get_at(Some(lower), index) {
                if close < l {
                    return (Some(CROSS_SHORT), Some(TRIGGER_DONCHIAN));
                }
            }
        }
    }
    (None, None)
}

/// Feature A: SMA120 side must agree and the market must not be chopping.
pub fn trend_filter_ok(
    frame: &Frame,
    index: usize,
    side: &str,
    cfg: &Value,
) -> (bool, &'static str) {
    let slow = frame.slow.get(index).copied().flatten();
    let close = frame
        .closes
        .get(index)
        .copied()
        .flatten()
        .unwrap_or(f64::NAN);
    let slow = match slow {
        Some(s) => s,
        None => return (false, "NO_SLOW_SMA"),
    };
    if side == CROSS_LONG && close <= slow {
        return (false, "REJECTED_TREND_FILTER_SLOW_SMA");
    }
    if side == CROSS_SHORT && close >= slow {
        return (false, "REJECTED_TREND_FILTER_SLOW_SMA");
    }
    let adx_value = get_at(frame.adx.as_ref(), index);
    let efficiency = get_at(frame.efficiency.as_ref(), index);
    let (adx_value, efficiency) = match (adx_value, efficiency) {
        (Some(a), Some(e)) => (a, e),
        _ => return (false, "REJECTED_TREND_FILTER_NO_DATA"),
    };
    let adx_min = float_field(cfg, "feature_a_adx_min");
    if adx_value < adx_min {
        return (false, "REJECTED_TREND_FILTER_LOW_ADX");
    }
    let eff_min = float_field(cfg, "feature_a_efficiency_min");
    if efficiency < eff_min {
        return (false, "REJECTED_TREND_FILTER_LOW_EFFICIENCY");
    }
    (true, "")
}

/// Feature B: ATR structural stop.  Returns (price, distance) or (None, None).
pub fn initial_stop(
    side: &str,
    entry_price: Option<f64>,
    atr_value: Option<f64>,
    cfg: &Value,
) -> (Option<f64>, Option<f64>) {
    let entry = match entry_price {
        Some(e) if e > 0.0 => e,
        _ => return (None, None),
    };
    let atr_val = match atr_value {
        Some(a) if a > 0.0 => a,
        _ => return (None, None),
    };
    let multiplier = float_field(cfg, "feature_b_atr_multiplier");
    let distance = atr_val * multiplier;
    let price = if side == CROSS_LONG {
        entry - distance
    } else {
        entry + distance
    };
    if price <= 0.0 {
        return (None, None);
    }
    (Some(price), Some(distance))
}

/// Feature C: volatility-target sizing multiplier (1.0 when disabled).
pub fn size_multiplier(atr_value: Option<f64>, close: Option<f64>, cfg: &Value) -> f64 {
    if !py_truthy(cfg.get("feature_c_vol_sizing").unwrap_or(&Value::Null)) {
        return 1.0;
    }
    volatility_target_scale(
        atr_value,
        close,
        f_opt(cfg.get("feature_c_target_atr_pct")),
        f_opt(cfg.get("feature_c_min_scale")).unwrap_or(0.0),
        f_opt(cfg.get("feature_c_max_scale")).unwrap_or(0.0),
    )
}

/// Mutable trade-like state used by `trailing_stop`.
/// Mirrors the Python dict keys `highest_price` / `lowest_price` (string values).
#[derive(Debug, Default, Clone)]
pub struct TradeState {
    pub highest_price: Option<String>,
    pub lowest_price: Option<String>,
    pub entry_price: Option<String>,
    pub trail_price: Option<String>,
}

/// Feature D: chandelier stop for a held trade (never loosens).
pub fn trailing_stop(
    side: &str,
    trade: &mut TradeState,
    price: f64,
    atr_value: Option<f64>,
    cfg: &Value,
) -> Option<f64> {
    if !py_truthy(cfg.get("feature_d_chandelier").unwrap_or(&Value::Null)) {
        return None;
    }
    let highest = match trade
        .highest_price
        .as_ref()
        .and_then(|s| s.trim().parse::<f64>().ok())
    {
        Some(v) => Some(v),
        None => Some(price),
    };
    let lowest = match trade
        .lowest_price
        .as_ref()
        .and_then(|s| s.trim().parse::<f64>().ok())
    {
        Some(v) => Some(v),
        None => Some(price),
    };
    let (highest, lowest) = if side == CROSS_LONG {
        let h = f64::max(highest.unwrap_or(price), price);
        trade.highest_price = Some(format_py_float(h));
        (Some(h), lowest)
    } else {
        let l = f64::min(lowest.unwrap_or(price), price);
        trade.lowest_price = Some(format_py_float(l));
        (highest, Some(l))
    };
    let entry = trade
        .entry_price
        .as_ref()
        .and_then(|s| s.trim().parse::<f64>().ok());
    let previous = trade
        .trail_price
        .as_ref()
        .and_then(|s| s.trim().parse::<f64>().ok());
    let mult = float_field(cfg, "feature_d_atr_multiplier");
    chandelier_stop(side, highest, lowest, atr_value, mult, previous, entry)
}

/// Format a float the way Python's `str(float)` would for typical values.
fn format_py_float(v: f64) -> String {
    if v.is_nan() {
        return "nan".to_string();
    }
    if v.is_infinite() {
        return if v > 0.0 {
            "inf".to_string()
        } else {
            "-inf".to_string()
        };
    }
    let s = format!("{}", v);
    s
}

#[derive(Serialize)]
pub struct EvaluateResult {
    pub side: Option<String>,
    pub trigger: Option<String>,
    pub skip_reason: String,
    pub features: String,
    pub lock_enabled: bool,
    pub atr: Option<f64>,
    pub size_multiplier: f64,
    pub initial_stop: Option<f64>,
    pub atr_distance: Option<f64>,
}

/// Decision for the candle at `index` (default: last closed candle).
pub fn evaluate(rows: &[Candle], cfg: &Value, index: Option<usize>) -> EvaluateResult {
    let mode = feature_mode(cfg);
    let mut result = EvaluateResult {
        side: None,
        trigger: None,
        skip_reason: String::new(),
        features: mode.clone(),
        lock_enabled: py_bool(cfg.get("locking"), true),
        atr: None,
        size_multiplier: 1.0,
        initial_stop: None,
        atr_distance: None,
    };
    if mode.is_empty() {
        result.skip_reason = "NO_FEATURE_SELECTED".to_string();
        return result;
    }
    if rows.is_empty() {
        result.skip_reason = "NO_DATA".to_string();
        return result;
    }
    let idx = match index {
        Some(i) => i,
        None => rows.len() - 1,
    };
    if idx < 1 || idx >= rows.len() {
        result.skip_reason = "NO_DATA".to_string();
        return result;
    }
    let frame = indicator_frame(rows, cfg);
    let (side, trigger) = cross_side(&frame, idx);
    let atr_value = frame.atr.get(idx).copied().flatten();
    result.atr = atr_value;
    let side = match side {
        Some(s) => s,
        None => {
            result.skip_reason = "NO_SIGNAL".to_string();
            return result;
        }
    };
    if mode.contains('A') {
        let (ok, reason) = trend_filter_ok(&frame, idx, side, cfg);
        if !ok {
            result.skip_reason = reason.to_string();
            return result;
        }
    }
    let close = frame.closes.get(idx).copied().flatten();
    result.side = Some(side.to_string());
    result.trigger = trigger.map(|t| t.to_string());
    result.skip_reason = String::new();
    result.size_multiplier = size_multiplier(atr_value, close, cfg);
    if mode.contains('B') {
        let (stop_price, distance) = initial_stop(side, close, atr_value, cfg);
        result.initial_stop = stop_price;
        result.atr_distance = distance;
        if stop_price.is_none() {
            result.skip_reason = "REJECTED_NO_ATR_FOR_STOP".to_string();
            result.side = None;
        }
    }
    result
}
