//! Adaptive long-premium option signal, contract selection and capital-control helpers.
//! Port of options_trading/strategy.py and options_trading/risk.py.

use std::cmp::Ordering;

use rust_decimal::prelude::ToPrimitive;
use rust_decimal::Decimal;
use serde_json::{json, Map, Value};

use crate::core::candles::Candle;

// ---------------------------------------------------------------------------
// Scalar helpers
// ---------------------------------------------------------------------------

/// Python `_f(value, default)`: float(value) if finite else default.
pub fn pyf(value: Option<&Value>, default: f64) -> f64 {
    match value {
        None => default,
        Some(Value::Null) => default,
        Some(Value::Bool(b)) => {
            if *b {
                1.0
            } else {
                0.0
            }
        }
        Some(Value::Number(n)) => match n.as_f64() {
            Some(f) if f.is_finite() => f,
            _ => default,
        },
        Some(Value::String(s)) => {
            let trimmed = s.trim();
            match trimmed.parse::<f64>() {
                Ok(f) if f.is_finite() => f,
                _ => default,
            }
        }
        _ => default,
    }
}

/// Python `_clamp(value, low, high)` = min(high, max(low, value)).
pub fn clamp(value: f64, low: f64, high: f64) -> f64 {
    value.max(low).min(high)
}

/// Python `_ema(values, period)` (seeded with values[0]; 0.0 for empty).
pub fn ema_seeded(values: &[f64], period: usize) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    let period_i = if period == 0 { 0 } else { period };
    let denom = (period_i.max(1) as f64) + 1.0;
    let alpha = 2.0 / denom;
    let mut result = values[0];
    for value in &values[1..] {
        result = (alpha * value) + ((1.0 - alpha) * result);
    }
    result
}

// ---------------------------------------------------------------------------
// Python-like list helpers
// ---------------------------------------------------------------------------

/// Python list slicing `v[start:end]` (or `v[start:]` when end is None).
fn py_slice<T: Clone>(v: &[T], start: isize, end: Option<isize>) -> Vec<T> {
    let len = v.len() as isize;
    let norm = |idx: isize| -> isize {
        if idx < 0 {
            let shifted = len + idx;
            if shifted < 0 {
                0
            } else {
                shifted
            }
        } else {
            idx
        }
    };
    let s = norm(start).min(len).max(0);
    let e = match end {
        None => len,
        Some(end) => norm(end).min(len).max(0),
    };
    if s >= e {
        return Vec::new();
    }
    v[s as usize..e as usize].to_vec()
}

/// Python `min(recent)` on a non-empty slice.
fn min_slice(v: &[f64]) -> f64 {
    let mut acc = f64::INFINITY;
    let mut seen = false;
    for x in v {
        if !seen || *x < acc {
            acc = *x;
            seen = true;
        }
    }
    if seen {
        acc
    } else {
        0.0
    }
}

/// Python `max(recent)` on a non-empty slice.
fn max_slice(v: &[f64]) -> f64 {
    let mut acc = f64::NEG_INFINITY;
    let mut seen = false;
    for x in v {
        if !seen || *x > acc {
            acc = *x;
            seen = true;
        }
    }
    if seen {
        acc
    } else {
        0.0
    }
}

/// Python `statistics.fmean` on a non-empty slice.
fn fmean(v: &[f64]) -> f64 {
    if v.is_empty() {
        return 0.0;
    }
    let mut sum = 0.0;
    for x in v {
        sum += x;
    }
    sum / (v.len() as f64)
}

/// Python `statistics.pstdev`.
fn pstdev(v: &[f64]) -> f64 {
    if v.is_empty() {
        return 0.0;
    }
    let mean = fmean(v);
    let mut acc = 0.0;
    for x in v {
        let d = x - mean;
        acc += d * d;
    }
    (acc / (v.len() as f64)).sqrt()
}

/// Python `statistics.median` (mean of two middle values for even length).
fn median(v: &[f64]) -> f64 {
    if v.is_empty() {
        return 0.0;
    }
    let mut sorted = v.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(Ordering::Equal));
    let n = sorted.len();
    if n % 2 == 1 {
        sorted[n / 2]
    } else {
        (sorted[n / 2 - 1] + sorted[n / 2]) / 2.0
    }
}

/// Python `_first_row(payload)`.
fn first_row(payload: &Value) -> Value {
    match payload {
        Value::Array(arr) => {
            if let Some(first) = arr.first() {
                first.clone()
            } else {
                json!({})
            }
        }
        Value::Object(_) => payload.clone(),
        _ => json!({}),
    }
}

/// Helper for object lookups that returns None for missing keys.
fn get<'a>(v: &'a Value, key: &str) -> Option<&'a Value> {
    match v {
        Value::Object(m) => m.get(key),
        _ => None,
    }
}

fn get_str_upper(v: &Value, key: &str) -> String {
    match get(v, key) {
        Some(Value::String(s)) => s.to_uppercase(),
        Some(Value::Null) | None => String::new(),
        Some(other) => py_str(other).to_uppercase(),
    }
}

/// Python `str(x)` for a JSON value used inside `str(x or "")` patterns.
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
        Value::Array(_) => "[]".to_string(),
        Value::Object(_) => "{}".to_string(),
    }
}

/// Python `str(x or "")`: falsy values produce the empty string.
fn py_str_or_empty(value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::Bool(b) => {
            if *b {
                "True".to_string()
            } else {
                String::new()
            }
        }
        Value::Number(n) => match n.as_f64() {
            Some(0.0) => String::new(),
            _ => n.to_string(),
        },
        Value::String(s) => s.clone(),
        Value::Array(arr) => {
            if arr.is_empty() {
                String::new()
            } else {
                "[]".to_string()
            }
        }
        Value::Object(m) => {
            if m.is_empty() {
                String::new()
            } else {
                "{}".to_string()
            }
        }
    }
}

/// Python `_greek(mark, name)`: try keys name, name.lower(), name.upper().
fn greek(mark: &Value, name: &str) -> f64 {
    let candidates = [name.to_string(), name.to_lowercase(), name.to_uppercase()];
    for key in candidates.iter() {
        if let Some(v) = get(mark, key) {
            return pyf(Some(v), 0.0);
        }
    }
    0.0
}

/// Python `_lot_filter(contract)` -> (min_qty, step or min_qty).
fn lot_filter(contract: &Value) -> (f64, f64) {
    let mut min_qty = pyf(get(contract, "minQty"), 0.0);
    let mut step = 0.0;
    if let Some(Value::Array(filters)) = get(contract, "filters") {
        for item in filters {
            if get_str_upper(item, "filterType") == "LOT_SIZE" {
                min_qty = pyf(get(item, "minQty"), min_qty);
                step = pyf(get(item, "stepSize"), 0.0);
            }
        }
    }
    let step_or = if step != 0.0 { step } else { min_qty };
    (min_qty, step_or)
}

/// Python `_price_filter(contract)` -> tick.
fn price_filter(contract: &Value) -> f64 {
    let mut tick = 0.0;
    if let Some(Value::Array(filters)) = get(contract, "filters") {
        for item in filters {
            if get_str_upper(item, "filterType") == "PRICE_FILTER" {
                tick = pyf(get(item, "tickSize"), 0.0);
            }
        }
    }
    tick
}

// ---------------------------------------------------------------------------
// Technical indicators
// ---------------------------------------------------------------------------

/// True ranges from candle rows.
pub fn true_ranges(rows: &[Candle]) -> Vec<f64> {
    if rows.len() < 2 {
        return Vec::new();
    }
    let mut trs = Vec::with_capacity(rows.len() - 1);
    let mut previous = rows[0][4];
    if !previous.is_finite() {
        previous = 0.0;
    }
    for row in &rows[1..] {
        let high = finite_or_zero(row[2]);
        let low = finite_or_zero(row[3]);
        let close = finite_or_zero(row[4]);
        let tr = (high - low)
            .max((high - previous).abs())
            .max((low - previous).abs());
        trs.push(tr);
        previous = close;
    }
    trs
}

fn finite_or_zero(x: f64) -> f64 {
    if x.is_finite() {
        x
    } else {
        0.0
    }
}

/// Average true range over the last `max(2, period)` true ranges.
pub fn atr(rows: &[Candle], period: usize) -> f64 {
    let trs = true_ranges(rows);
    let n = period.max(2) as isize;
    let sample = py_slice(&trs, -n, None);
    if sample.is_empty() {
        0.0
    } else {
        fmean(&sample)
    }
}

/// Population-standard-deviation realized volatility annualized.
pub fn realized_volatility(closes: &[f64], periods_per_year: f64, sample_size: usize) -> f64 {
    let mut returns = Vec::new();
    for pair in closes.windows(2) {
        let previous = finite_or_zero(pair[0]);
        let current = finite_or_zero(pair[1]);
        if previous > 0.0 && current > 0.0 {
            returns.push((current / previous).ln());
        }
    }
    let n = (sample_size as isize).max(12);
    let sample = py_slice(&returns, -n, None);
    if sample.len() < 12 {
        return 0.0;
    }
    pstdev(&sample) * periods_per_year.sqrt()
}

/// Forecast realized volatility from 1h closes (periods_per_year = 24*365).
pub fn forecast_realized_volatility(closes: &[f64]) -> f64 {
    let periods_per_year = 24.0 * 365.0;
    let values: Vec<f64> = closes
        .iter()
        .map(|v| finite_or_zero(*v))
        .filter(|v| *v > 0.0)
        .collect();
    let mut returns = Vec::new();
    for pair in values.windows(2) {
        let previous = pair[0];
        let current = pair[1];
        if previous > 0.0 && current > 0.0 {
            returns.push((current / previous).ln());
        }
    }
    if returns.len() < 24 {
        return realized_volatility(&values, periods_per_year, 24);
    }

    let annualized = |window: usize| -> f64 {
        let n = (window as isize).min(returns.len() as isize).max(0);
        let sample = py_slice(&returns, -n, None);
        if sample.len() < 2 {
            return 0.0;
        }
        pstdev(&sample) * periods_per_year.sqrt()
    };

    let daily = annualized(24);
    let three_day = annualized(72);
    let weekly = annualized(168);
    let forecast = 0.50 * daily + 0.30 * three_day + 0.20 * weekly;
    let hourly_sigma = (weekly / periods_per_year.sqrt()).max(1e-9);
    let last_ret = *returns.last().unwrap_or(&0.0);
    let jump_score = (last_ret.abs() / hourly_sigma).min(3.0);
    let vol_acceleration = (daily / weekly.max(0.01) - 1.0).max(0.0);
    let reserve = 1.0 + (jump_score * 0.04 + vol_acceleration * 0.12).min(0.30);
    clamp(forecast * reserve, 0.03, 4.0)
}

/// Standard normal CDF via erf.
pub fn normal_cdf(x: f64) -> f64 {
    0.5 * (1.0 + libm::erf(x / 2.0_f64.sqrt()))
}

// ---------------------------------------------------------------------------
// Expected option edge
// ---------------------------------------------------------------------------

/// Estimate expiry intrinsic value net of fees and exit friction.
#[allow(clippy::too_many_arguments)]
pub fn expected_option_net_edge(
    side: &str,
    spot_price: f64,
    strike_price: f64,
    dte_days: f64,
    option_price: f64,
    forecast_volatility: f64,
    signal_score: f64,
    expected_return_24h: f64,
    spread: f64,
    directional_move_scale: f64,
) -> Value {
    let side_upper = side.to_uppercase();
    let spot = finite_or_zero(spot_price);
    let strike = finite_or_zero(strike_price);
    let premium = finite_or_zero(option_price);
    let volatility = clamp(finite_or_zero(forecast_volatility), 0.03, 4.0);
    let years = (1.0 / 365.0_f64).max(finite_or_zero(dte_days) / 365.0);
    if (side_upper != "CALL" && side_upper != "PUT")
        || spot <= 0.0
        || strike <= 0.0
        || premium <= 0.0
    {
        return json!({
            "accepted": false,
            "reason": "OPTION_EXPECTED_EDGE_INPUT_INVALID",
        });
    }

    let option_sign = if side_upper == "CALL" { 1.0 } else { -1.0 };
    let directional_strength = (option_sign * finite_or_zero(signal_score)).max(0.0);
    let volatility_move =
        (directional_strength * volatility * years.sqrt() * finite_or_zero(directional_move_scale))
            .min(0.60);
    let aligned_daily_return = (option_sign * finite_or_zero(expected_return_24h)).max(0.0);
    let dte_tenor = finite_or_zero(dte_days);
    let momentum_move = (volatility * years.sqrt() * 2.50)
        .min(aligned_daily_return * (3.0_f64).min((1.0_f64).max(dte_tenor)));
    let expected_move = (0.60_f64).min(volatility_move.max(momentum_move));
    let expected_simple_return = option_sign * expected_move;
    let expected_spot = (spot * 0.10).max(spot * (1.0 + expected_simple_return));
    let variance = (1e-12_f64).max(volatility * volatility * years);
    let sigma = variance.sqrt();
    let log_mean = expected_spot.ln() - 0.5 * variance;
    let d2 = (log_mean - strike.ln()) / sigma;
    let d1 = d2 + sigma;
    let mut expected_payoff = if side_upper == "CALL" {
        expected_spot * normal_cdf(d1) - strike * normal_cdf(d2)
    } else {
        strike * normal_cdf(-d2) - expected_spot * normal_cdf(-d1)
    };
    if expected_payoff < 0.0 {
        expected_payoff = 0.0;
    }

    let per_leg_fee = (spot * 0.0003).min(premium * 0.10);
    let round_trip_cost = 2.0 * per_leg_fee + (0.0_f64).max(finite_or_zero(spread)) * 0.50;
    let net_value = expected_payoff - premium - round_trip_cost;
    json!({
        "accepted": true,
        "expected_payoff": expected_payoff,
        "round_trip_cost": round_trip_cost,
        "expected_move_pct": expected_simple_return,
        "net_expected_edge_pct": net_value / premium,
    })
}

// ---------------------------------------------------------------------------
// Order-flow evaluation
// ---------------------------------------------------------------------------

/// Combine signed public trades and top-book imbalance for one option.
pub fn evaluate_option_flow(recent_trades: &Value, depth_payload: &Value) -> Map<String, Value> {
    let mut buy_quote = 0.0;
    let mut sell_quote = 0.0;

    if let Value::Array(trades) = recent_trades {
        for trade in trades {
            if !trade.is_object() {
                continue;
            }
            let mut quote = pyf(get(trade, "quoteQty"), 0.0);
            if quote <= 0.0 {
                let price = pyf(get(trade, "price"), 0.0);
                let qty_opt = get(trade, "qty");
                let qty = match qty_opt {
                    Some(q) if q.is_number() || matches!(q, Value::String(_)) => {
                        pyf(Some(q), pyf(get(trade, "quantity"), 0.0))
                    }
                    _ => pyf(get(trade, "quantity"), 0.0),
                };
                quote = price * qty.abs();
            }
            let side = get(trade, "side");
            let side_text = py_str_or_empty(side.unwrap_or(&Value::Null))
                .trim()
                .to_uppercase();
            let side_value = pyf(side, 0.0);
            let is_buy = side_text == "BUY"
                || side_text == "B"
                || side_text == "1"
                || side_text == "+1"
                || side_value > 0.0;
            if is_buy {
                buy_quote += (0.0_f64).max(quote);
            } else {
                let is_sell = side_text == "SELL"
                    || side_text == "S"
                    || side_text == "-1"
                    || side_value < 0.0;
                if is_sell {
                    sell_quote += (0.0_f64).max(quote);
                }
            }
        }
    }

    let total_quote = buy_quote + sell_quote;
    let trade_imbalance = if total_quote > 0.0 {
        (buy_quote - sell_quote) / total_quote
    } else {
        0.0
    };

    let empty = vec![];
    let bids: &Vec<Value> = match get(depth_payload, "bids") {
        Some(Value::Array(a)) => a,
        _ => &empty,
    };
    let asks: &Vec<Value> = match get(depth_payload, "asks") {
        Some(Value::Array(a)) => a,
        _ => &empty,
    };
    let bids5 = py_slice(bids, 0, Some(5));
    let asks5 = py_slice(asks, 0, Some(5));
    let mut bid_qty = 0.0;
    for row in &bids5 {
        if let Value::Array(arr) = row {
            if arr.len() >= 2 {
                bid_qty += pyf(Some(&arr[1]), 0.0);
            }
        }
    }
    let mut ask_qty = 0.0;
    for row in &asks5 {
        if let Value::Array(arr) = row {
            if arr.len() >= 2 {
                ask_qty += pyf(Some(&arr[1]), 0.0);
            }
        }
    }
    let depth_total = bid_qty + ask_qty;
    let depth_imbalance = if depth_total > 0.0 {
        (bid_qty - ask_qty) / depth_total
    } else {
        0.0
    };
    let trade_weight = (0.80_f64).min(0.35 + total_quote.ln_1p() / 25.0);
    let flow_score = clamp(
        trade_weight * trade_imbalance + (1.0 - trade_weight) * depth_imbalance,
        -1.0,
        1.0,
    );

    let mut out = Map::new();
    out.insert("flow_score".into(), json!(flow_score));
    out.insert("trade_imbalance".into(), json!(trade_imbalance));
    out.insert("depth_imbalance".into(), json!(depth_imbalance));
    out.insert("trade_quote_usdt".into(), json!(total_quote));
    out.insert("buy_quote_usdt".into(), json!(buy_quote));
    out.insert("sell_quote_usdt".into(), json!(sell_quote));
    out
}

// ---------------------------------------------------------------------------
// Underlying trend signal
// ---------------------------------------------------------------------------

/// Score the retained 1h + 4h trend structure.
pub fn evaluate_underlying_trend(fast_rows: &[Candle], slow_rows: &[Candle], cfg: &Value) -> Value {
    if fast_rows.len() < 110 || slow_rows.len() < 55 {
        return json!({
            "accepted": false,
            "reason": "UNDERLYING_CANDLES_INSUFFICIENT",
            "strategy": "ADAPTIVE_TREND",
        });
    }
    let fast_closes: Vec<f64> = fast_rows.iter().map(|r| finite_or_zero(r[4])).collect();
    let slow_closes: Vec<f64> = slow_rows.iter().map(|r| finite_or_zero(r[4])).collect();
    let price = *fast_closes.last().unwrap_or(&0.0);
    let atr_val = atr(fast_rows, 14).max(price * 0.001);

    let fast_component = clamp(
        (ema_seeded(&fast_closes, 12) - ema_seeded(&fast_closes, 26)) / atr_val / 2.0,
        -1.0,
        1.0,
    );
    let medium_component = clamp(
        (ema_seeded(&fast_closes, 20) - ema_seeded(&fast_closes, 50)) / atr_val / 3.0,
        -1.0,
        1.0,
    );
    let slow_component = clamp(
        (price - ema_seeded(&fast_closes, 100)) / atr_val / 5.0,
        -1.0,
        1.0,
    );
    let recent = py_slice(&fast_closes, -21, Some(-1));
    let recent_low = min_slice(&recent);
    let recent_high = max_slice(&recent);
    let span = (recent_high - recent_low).max(atr_val);
    let breakout_component = clamp(((price - recent_low) / span - 0.5) * 2.0, -1.0, 1.0);
    let slow_tf_component = clamp(
        (ema_seeded(&slow_closes, 20) - ema_seeded(&slow_closes, 50))
            / atr(slow_rows, 14).max(price * 0.002)
            / 3.0,
        -1.0,
        1.0,
    );
    let forecast_volatility = forecast_realized_volatility(&fast_closes);
    let ret_24h = if fast_closes.len() >= 25 && fast_closes[fast_closes.len() - 25] > 0.0 {
        price / fast_closes[fast_closes.len() - 25] - 1.0
    } else {
        0.0
    };
    let ret_72h = if fast_closes.len() >= 73 && fast_closes[fast_closes.len() - 73] > 0.0 {
        price / fast_closes[fast_closes.len() - 73] - 1.0
    } else {
        ret_24h
    };
    let multi_horizon_momentum = clamp(
        (ret_24h + 0.50 * ret_72h) / (forecast_volatility / 365.0_f64.sqrt()).max(0.005) / 3.0,
        -1.0,
        1.0,
    );
    let score = 0.18 * fast_component
        + 0.18 * medium_component
        + 0.13 * slow_component
        + 0.18 * breakout_component
        + 0.23 * slow_tf_component
        + 0.10 * multi_horizon_momentum;
    let threshold = pyf(get(cfg, "min_abs_signal"), 0.46);
    let accepted = score.abs() >= threshold;
    let direction = if score > 0.0 { "CALL" } else { "PUT" };
    let last_ts = {
        let v = fast_rows.last().map(|r| r[0]).unwrap_or(0.0);
        if v.is_finite() {
            v as i64
        } else {
            0
        }
    };
    let atr_pct = if price > 0.0 { atr_val / price } else { 0.0 };
    json!({
        "accepted": accepted,
        "reason": if accepted { "ADAPTIVE_TREND_READY" } else { "TREND_SCORE_BELOW_THRESHOLD" },
        "strategy": "ADAPTIVE_TREND",
        "score": score,
        "direction": direction,
        "spot_price": price,
        "atr_pct": atr_pct,
        "realized_volatility": realized_volatility(&fast_closes, 24.0 * 365.0, 72),
        "forecast_volatility": forecast_volatility,
        "expected_return_24h": 0.65 * ret_24h + 0.35 * (ret_72h / 3.0),
        "signal_bar_ts": last_ts,
        "components": {
            "fast": fast_component,
            "medium": medium_component,
            "slow": slow_component,
            "breakout": breakout_component,
            "slow_timeframe": slow_tf_component,
            "multi_horizon_momentum": multi_horizon_momentum,
        },
    })
}

// ---------------------------------------------------------------------------
// Low-IV squeeze signal
// ---------------------------------------------------------------------------

/// Detect compressed realized volatility followed by a confirmed range break.
pub fn evaluate_low_iv_squeeze(fast_rows: &[Candle], slow_rows: &[Candle], cfg: &Value) -> Value {
    if fast_rows.len() < 90 || slow_rows.len() < 30 {
        return json!({
            "accepted": false,
            "reason": "SQUEEZE_CANDLES_INSUFFICIENT",
            "strategy": "LOW_IV_SQUEEZE",
        });
    }

    let closes: Vec<f64> = fast_rows.iter().map(|r| finite_or_zero(r[4])).collect();
    let highs: Vec<f64> = fast_rows.iter().map(|r| finite_or_zero(r[2])).collect();
    let lows: Vec<f64> = fast_rows.iter().map(|r| finite_or_zero(r[3])).collect();
    let volumes: Vec<f64> = fast_rows.iter().map(|r| finite_or_zero(r[5])).collect();
    let price = *closes.last().unwrap_or(&0.0);
    if price <= 0.0 {
        return json!({
            "accepted": false,
            "reason": "SQUEEZE_PRICE_INVALID",
            "strategy": "LOW_IV_SQUEEZE",
        });
    }

    let trs = true_ranges(fast_rows);
    let recent_atr = if trs.len() >= 12 {
        fmean(&py_slice(&trs, -12, None))
    } else {
        atr(fast_rows, 14)
    };
    let baseline_sample = if trs.len() >= 72 {
        py_slice(&trs, -72, Some(-12))
    } else {
        py_slice(&trs, 0, Some(-12))
    };
    let baseline_atr = if baseline_sample.is_empty() {
        recent_atr
    } else {
        fmean(&baseline_sample)
    };
    let compression_ratio = recent_atr / baseline_atr.max(price * 1e-6);

    let short_rv = realized_volatility(&closes, 24.0 * 365.0, 18);
    let medium_rv = realized_volatility(&closes, 24.0 * 365.0, 72);
    let rv_ratio = short_rv / medium_rv.max(0.01);

    let prior_high = max_slice(&py_slice(&highs, -21, Some(-1)));
    let prior_low = min_slice(&py_slice(&lows, -21, Some(-1)));
    let up_break = (price - prior_high) / recent_atr.max(price * 0.001);
    let down_break = (prior_low - price) / recent_atr.max(price * 0.001);
    let direction = if up_break >= down_break {
        "CALL"
    } else {
        "PUT"
    };
    let breakout_strength = up_break.max(down_break);

    let vol_window = py_slice(&volumes, -21, Some(-1));
    let has_any_volume = vol_window.iter().any(|v| *v != 0.0);
    let volume_baseline = if has_any_volume {
        fmean(&vol_window)
    } else {
        0.0
    };
    let last_volume = *volumes.last().unwrap_or(&0.0);
    let volume_ratio = if volume_baseline > 0.0 {
        last_volume / volume_baseline.max(1e-12)
    } else {
        1.0
    };
    let momentum = if closes.len() >= 4 && closes[closes.len() - 4] > 0.0 {
        price / closes[closes.len() - 4] - 1.0
    } else {
        0.0
    };
    let momentum_in_atr = (momentum * price).abs() / recent_atr.max(price * 0.001);

    let slow_closes: Vec<f64> = slow_rows.iter().map(|r| finite_or_zero(r[4])).collect();
    let slow_bias = clamp(
        (ema_seeded(&slow_closes, 12) - ema_seeded(&slow_closes, 26))
            / atr(slow_rows, 14).max(price * 0.002)
            / 2.5,
        -1.0,
        1.0,
    );
    let direction_sign = if direction == "CALL" { 1.0 } else { -1.0 };
    let alignment = clamp(slow_bias * direction_sign, -1.0, 1.0);

    let compression_limit = pyf(get(cfg, "squeeze_compression_ratio"), 0.78);
    let volume_min = pyf(get(cfg, "squeeze_volume_multiplier"), 1.15);
    let compressed = compression_ratio <= compression_limit && rv_ratio <= 0.90;
    let breakout = breakout_strength >= 0.15;
    let confirmed = volume_ratio >= volume_min || momentum_in_atr >= 0.65;

    let compression_score = clamp(
        (compression_limit - compression_ratio) / compression_limit.max(0.01) + 0.55,
        0.0,
        1.0,
    );
    let rv_score = clamp((0.95 - rv_ratio) / 0.55, 0.0, 1.0);
    let breakout_score = clamp(breakout_strength / 1.25, 0.0, 1.0);
    let volume_score = clamp((volume_ratio - 1.0) / 0.8, 0.0, 1.0);
    let momentum_score = clamp(momentum_in_atr / 1.5, 0.0, 1.0);
    let raw = 0.24 * compression_score
        + 0.16 * rv_score
        + 0.28 * breakout_score
        + 0.14 * volume_score
        + 0.10 * momentum_score
        + 0.08 * (0.0_f64).max(alignment);
    let signed_score = raw * direction_sign;
    let threshold = pyf(get(cfg, "squeeze_min_score"), 0.58);
    let accepted = compressed && breakout && confirmed && raw >= threshold;
    let last_ts = {
        let v = fast_rows.last().map(|r| r[0]).unwrap_or(0.0);
        if v.is_finite() {
            v as i64
        } else {
            0
        }
    };
    json!({
        "accepted": accepted,
        "reason": if accepted { "LOW_IV_SQUEEZE_READY" } else { "LOW_IV_SQUEEZE_NOT_READY" },
        "strategy": "LOW_IV_SQUEEZE",
        "score": signed_score,
        "direction": direction,
        "spot_price": price,
        "atr_pct": recent_atr / price,
        "realized_volatility": short_rv.max(medium_rv * 0.65),
        "forecast_volatility": forecast_realized_volatility(&closes),
        "expected_return_24h": direction_sign * (momentum.abs() * 4.0).min(0.25),
        "signal_bar_ts": last_ts,
        "requires_low_iv": true,
        "components": {
            "compression_ratio": compression_ratio,
            "rv_ratio": rv_ratio,
            "breakout_strength": breakout_strength,
            "volume_ratio": volume_ratio,
            "momentum_in_atr": momentum_in_atr,
            "slow_alignment": alignment,
        },
    })
}

// ---------------------------------------------------------------------------
// Signal selection
// ---------------------------------------------------------------------------

/// Return the strongest accepted Trend/Squeeze setup for one underlying.
pub fn choose_underlying_signal(fast_rows: &[Candle], slow_rows: &[Candle], cfg: &Value) -> Value {
    let trend = evaluate_underlying_trend(fast_rows, slow_rows, cfg);
    let squeeze = evaluate_low_iv_squeeze(fast_rows, slow_rows, cfg);
    let is_accepted = |row: &Value| -> bool {
        match get(row, "accepted") {
            Some(Value::Bool(b)) => *b,
            _ => false,
        }
    };
    let score_of = |row: &Value| -> f64 { pyf(get(row, "score"), 0.0) };

    let mut accepted: Vec<Value> = Vec::new();
    if is_accepted(&trend) {
        accepted.push(trend.clone());
    }
    if is_accepted(&squeeze) {
        accepted.push(squeeze.clone());
    }
    if !accepted.is_empty() {
        accepted.sort_by(|a, b| {
            score_of(b)
                .abs()
                .partial_cmp(&score_of(a).abs())
                .unwrap_or(Ordering::Equal)
        });
        let mut result = match accepted[0].as_object() {
            Some(m) => m.clone(),
            None => Map::new(),
        };
        let alternate = if accepted.len() > 1 {
            get(&accepted[1], "strategy")
                .map(py_str)
                .unwrap_or_default()
        } else {
            String::new()
        };
        result.insert("alternate_signal".into(), json!(alternate));
        return Value::Object(result);
    }

    let pick = if score_of(&trend).abs() >= score_of(&squeeze).abs() {
        &trend
    } else {
        &squeeze
    };
    let mut result = match pick.as_object() {
        Some(m) => m.clone(),
        None => Map::new(),
    };
    result.insert("accepted".into(), json!(false));
    result.insert("reason".into(), json!("NO_ADAPTIVE_OPTION_SIGNAL"));
    result.insert("trend_score".into(), json!(score_of(&trend)));
    result.insert("squeeze_score".into(), json!(score_of(&squeeze)));
    Value::Object(result)
}

// ---------------------------------------------------------------------------
// Dynamic contract targets
// ---------------------------------------------------------------------------

/// Map signal type/strength to a DTE and Delta target inside hard bounds.
pub fn derive_dynamic_contract_targets(signal: &Value, cfg: &Value) -> Map<String, Value> {
    let strength = pyf(get(signal, "score"), 0.0).abs();
    let strategy = match get(signal, "strategy") {
        Some(v) => {
            let s = py_str_or_empty(v);
            if s.is_empty() {
                "ADAPTIVE_TREND".to_string()
            } else {
                s
            }
        }
        None => "ADAPTIVE_TREND".to_string(),
    }
    .to_uppercase();
    let components = match get(signal, "components") {
        Some(v @ Value::Object(_)) => v.clone(),
        _ => json!({}),
    };
    let strong = pyf(get(cfg, "strong_signal"), 0.68);

    let (mut target_dte, mut target_delta) = if strategy == "LOW_IV_SQUEEZE" {
        if strength >= strong {
            (3.5, 0.42)
        } else {
            (5.0, 0.46)
        }
    } else {
        let persistent = pyf(get(&components, "slow_timeframe"), 0.0).abs() >= 0.45
            && pyf(get(&components, "medium"), 0.0).abs() >= 0.40
            && pyf(get(&components, "breakout"), 0.0).abs() < 0.55;
        if strength >= (0.78_f64).max(strong + 0.08) {
            (5.0, 0.43)
        } else if persistent {
            (14.0, 0.50)
        } else if strength >= strong {
            (7.0, 0.45)
        } else if strength >= 0.56 {
            (9.0, 0.47)
        } else {
            (12.0, 0.50)
        }
    };

    let min_dte = pyf(get(cfg, "min_dte_days"), 2.0);
    let max_dte = pyf(get(cfg, "max_dte_days"), 21.0);
    target_dte = max_dte.min(min_dte.max(target_dte));
    let preferred_min = pyf(get(cfg, "preferred_delta_min"), 0.35);
    let preferred_max = pyf(get(cfg, "preferred_delta_max"), 0.55);
    target_delta = preferred_max.min(preferred_min.max(target_delta));

    let mut out = Map::new();
    out.insert("target_dte_days".into(), json!(target_dte));
    out.insert("target_delta".into(), json!(target_delta));
    out.insert("preferred_delta_min".into(), json!(preferred_min));
    out.insert("preferred_delta_max".into(), json!(preferred_max));
    out
}

// ---------------------------------------------------------------------------
// Contract shortlisting
// ---------------------------------------------------------------------------

/// Shortlist option contracts and produce diagnostics counters.
pub fn shortlist_option_contracts_with_diagnostics(
    exchange_info: &Value,
    underlying: &str,
    direction: &str,
    spot_price: f64,
    signal: &Value,
    cfg: &Value,
    now_ms: i64,
) -> (Vec<Value>, Map<String, Value>) {
    let direction = direction.to_uppercase();
    let spot = finite_or_zero(spot_price);
    let targets = derive_dynamic_contract_targets(signal, cfg);
    let target_dte_days = pyf(targets.get("target_dte_days"), 0.0);
    let underlying_upper = underlying.to_uppercase();

    let mut dte_diag = 0i64;
    let mut otm_diag = 0i64;
    let mut symbol_diag = 0i64;
    let mut rows: Vec<Value> = Vec::new();

    let symbols: &Vec<Value> = match get(exchange_info, "optionSymbols") {
        Some(Value::Array(a)) => a,
        _ => {
            let empty = Vec::new();
            // Need to own until end; use a static empty vec via block.
            let mut diag = Map::new();
            diag.insert("DTE".into(), json!(0));
            diag.insert("OTM".into(), json!(0));
            diag.insert("SYMBOL".into(), json!(0));
            return (empty, diag);
        }
    };

    let min_dte = pyf(get(cfg, "min_dte_days"), 2.0);
    let max_dte = pyf(get(cfg, "max_dte_days"), 21.0);
    let target_otm = pyf(get(cfg, "target_otm_pct"), 0.02);
    let now_f = now_ms as f64;

    for contract in symbols {
        let contract_underlying = match get(contract, "underlying") {
            Some(v) => py_str_or_empty(v).to_uppercase(),
            None => String::new(),
        };
        if contract_underlying != underlying_upper {
            continue;
        }
        if get_str_upper(contract, "side") != direction {
            continue;
        }
        if get_str_upper(contract, "status") != "TRADING" {
            continue;
        }
        let underlying_type = {
            let s = get_str_upper(contract, "underlyingType");
            if s.is_empty() {
                "CRYPTO".to_string()
            } else {
                s
            }
        };
        if underlying_type != "CRYPTO" {
            continue;
        }
        let contract_type = {
            let s = get_str_upper(contract, "contractType");
            if s.is_empty() {
                "CRYPTO_OPTIONS".to_string()
            } else {
                s
            }
        };
        if contract_type != "CRYPTO_OPTIONS" {
            continue;
        }
        symbol_diag += 1;
        let expiry = pyf(get(contract, "expiryDate"), 0.0) as i64;
        let dte = (expiry as f64 - now_f) / 86_400_000.0;
        if dte < min_dte || dte > max_dte {
            dte_diag += 1;
            continue;
        }
        let strike = pyf(get(contract, "strikePrice"), 0.0);
        if spot <= 0.0 || strike <= 0.0 {
            otm_diag += 1;
            continue;
        }
        let otm = if direction == "CALL" {
            strike / spot - 1.0
        } else {
            spot / strike - 1.0
        };
        if otm < -0.08 || otm > 0.18 {
            otm_diag += 1;
            continue;
        }
        let (min_qty, step) = lot_filter(contract);
        let tick = price_filter(contract);

        let mut row = match contract.as_object() {
            Some(m) => m.clone(),
            None => Map::new(),
        };
        row.insert("dte_days".into(), json!(dte));
        row.insert("otm_pct".into(), json!(otm));
        row.insert("min_qty".into(), json!(min_qty));
        row.insert("step_size".into(), json!(step));
        row.insert("tick_size".into(), json!(tick));
        for (k, v) in targets.iter() {
            row.insert(k.clone(), v.clone());
        }
        let distance = (dte - target_dte_days).abs() / target_dte_days.max(1.0)
            + (otm - target_otm).abs() * 6.0;
        row.insert("shortlist_distance".into(), json!(distance));
        rows.push(Value::Object(row));
    }

    rows.sort_by(|a, b| {
        let da = pyf(get(a, "shortlist_distance"), 0.0);
        let db = pyf(get(b, "shortlist_distance"), 0.0);
        let cmp = da.partial_cmp(&db).unwrap_or(Ordering::Equal);
        if cmp != Ordering::Equal {
            return cmp;
        }
        let ta = pyf(get(a, "dte_days"), 0.0);
        let tb = pyf(get(b, "dte_days"), 0.0);
        ta.partial_cmp(&tb).unwrap_or(Ordering::Equal)
    });

    let max_candidates = {
        let v = get(cfg, "max_candidates_per_underlying");
        let f = pyf(v, 10.0);
        if f == 0.0 {
            10usize
        } else {
            f as usize
        }
    };
    if rows.len() > max_candidates {
        rows.truncate(max_candidates);
    }

    let mut diag = Map::new();
    diag.insert("DTE".into(), json!(dte_diag));
    diag.insert("OTM".into(), json!(otm_diag));
    diag.insert("SYMBOL".into(), json!(symbol_diag));
    (rows, diag)
}

// ---------------------------------------------------------------------------
// Contract scoring
// ---------------------------------------------------------------------------

/// Apply hard safety gates, then rank survivable contracts with soft penalties.
#[allow(clippy::too_many_arguments)]
pub fn score_option_contract(
    contract: &Value,
    mark_payload: &Value,
    ticker_payload: &Value,
    depth_payload: &Value,
    signal: &Value,
    cfg: &Value,
    skew_mark_payload: Option<&Value>,
    surface_mark_payloads: &[Value],
    recent_trades: &Value,
) -> Value {
    let mark = first_row(mark_payload);
    let ticker = first_row(ticker_payload);
    let peer_mark = match skew_mark_payload {
        Some(v) => first_row(v),
        None => json!({}),
    };

    let bids: Vec<Value> = match get(depth_payload, "bids") {
        Some(Value::Array(a)) => a.clone(),
        _ => Vec::new(),
    };
    let asks: Vec<Value> = match get(depth_payload, "asks") {
        Some(Value::Array(a)) => a.clone(),
        _ => Vec::new(),
    };
    let bid_row0_len = match bids.first() {
        Some(Value::Array(a)) => a.len(),
        _ => 0,
    };
    let ask_row0_len = match asks.first() {
        Some(Value::Array(a)) => a.len(),
        _ => 0,
    };
    let bid = if !bids.is_empty() && bid_row0_len >= 2 {
        match bids.first() {
            Some(Value::Array(a)) => pyf(a.first(), 0.0),
            _ => 0.0,
        }
    } else {
        pyf(get(&ticker, "bidPrice"), 0.0)
    };
    let ask = if !asks.is_empty() && ask_row0_len >= 2 {
        match asks.first() {
            Some(Value::Array(a)) => pyf(a.first(), 0.0),
            _ => 0.0,
        }
    } else {
        pyf(get(&ticker, "askPrice"), 0.0)
    };
    if bid <= 0.0 || ask <= 0.0 || ask <= bid {
        return json!({"accepted": false, "reason": "OPTION_ORDERBOOK_NOT_TRADEABLE"});
    }
    let mid = (bid + ask) / 2.0;
    let spread = if mid > 0.0 { (ask - bid) / mid } else { 1.0 };
    let max_spread = pyf(get(cfg, "max_spread_pct"), 0.18);
    if spread > max_spread {
        return json!({
            "accepted": false,
            "reason": "OPTION_SPREAD_TOO_WIDE",
            "spread_pct": spread,
        });
    }

    let delta = pyf(get(&mark, "delta"), 0.0).abs();
    let min_delta = pyf(get(cfg, "min_abs_delta"), 0.20);
    let max_delta = pyf(get(cfg, "max_abs_delta"), 0.70);
    if delta < min_delta || delta > max_delta {
        return json!({
            "accepted": false,
            "reason": "OPTION_DELTA_OUTSIDE_TARGET",
            "delta": delta,
        });
    }

    let quote_volume = pyf(get(&ticker, "amount"), 0.0);
    let min_volume = pyf(get(cfg, "min_quote_volume_usdt"), 50.0);
    if quote_volume < min_volume {
        return json!({
            "accepted": false,
            "reason": "OPTION_VOLUME_TOO_LOW",
            "quote_volume": quote_volume,
        });
    }

    let iv = pyf(get(&mark, "markIV"), 0.0);
    if iv <= 0.0 {
        return json!({"accepted": false, "reason": "OPTION_IV_UNAVAILABLE"});
    }
    let realized = pyf(get(signal, "realized_volatility"), 0.0).max(0.01);
    let iv_ratio = iv / realized;
    let strategy = {
        let s = get(signal, "strategy")
            .map(py_str_or_empty)
            .unwrap_or_default();
        if s.is_empty() {
            "ADAPTIVE_TREND".to_string()
        } else {
            s
        }
    }
    .to_uppercase();
    let mut hard_iv_ratio = pyf(get(cfg, "hard_max_iv_to_realized"), 2.40);
    if strategy == "LOW_IV_SQUEEZE" {
        hard_iv_ratio = hard_iv_ratio.min(pyf(get(cfg, "squeeze_max_iv_to_realized"), 1.35));
    }
    if iv_ratio > hard_iv_ratio {
        return json!({
            "accepted": false,
            "reason": "OPTION_IV_TOO_EXPENSIVE",
            "iv_to_realized": iv_ratio,
        });
    }

    let depth_qty = if !bids.is_empty() && !asks.is_empty() {
        let b = match bids.first() {
            Some(Value::Array(a)) => pyf(a.get(1), 0.0),
            _ => 0.0,
        };
        let a = match asks.first() {
            Some(Value::Array(a)) => pyf(a.get(1), 0.0),
            _ => 0.0,
        };
        b.min(a)
    } else {
        0.0
    };
    if depth_qty <= 0.0 {
        return json!({"accepted": false, "reason": "OPTION_DEPTH_EMPTY"});
    }

    let flow = evaluate_option_flow(recent_trades, depth_payload);
    let flow_trade_quote = pyf(flow.get("trade_quote_usdt"), 0.0);
    let flow_score_val = pyf(flow.get("flow_score"), 0.0);
    if flow_trade_quote >= pyf(get(cfg, "min_flow_quote_usdt"), 25.0)
        && flow_score_val <= pyf(get(cfg, "hard_negative_flow_score"), -0.70)
    {
        let mut out = Map::new();
        out.insert("accepted".into(), json!(false));
        out.insert("reason".into(), json!("OPTION_FLOW_STRONGLY_OPPOSED"));
        for (k, v) in flow.iter() {
            out.insert(k.clone(), v.clone());
        }
        return Value::Object(out);
    }

    let targets = derive_dynamic_contract_targets(signal, cfg);
    let target_delta = pyf(
        get(contract, "target_delta"),
        pyf(targets.get("target_delta"), 0.0),
    );
    let target_dte = pyf(
        get(contract, "target_dte_days"),
        pyf(targets.get("target_dte_days"), 0.0),
    );
    let dte = pyf(get(contract, "dte_days"), target_dte);
    let signal_strength = pyf(get(signal, "score"), 0.0).abs();

    let soft_iv = pyf(get(cfg, "max_iv_to_realized"), 1.60);
    let spread_score = 1.0 - (spread / max_spread.max(0.01)).min(1.0);
    let delta_score = 1.0 - ((delta - target_delta).abs() / 0.30).min(1.0);
    let dte_score = 1.0 - ((dte - target_dte).abs() / target_dte.max(2.0)).min(1.0);
    let iv_score = if iv_ratio <= 1.0 {
        1.0
    } else if iv_ratio <= soft_iv {
        1.0 - 0.35 * ((iv_ratio - 1.0) / (soft_iv - 1.0).max(0.01))
    } else {
        ((0.65 * (hard_iv_ratio - iv_ratio)) / (hard_iv_ratio - soft_iv).max(0.01)).max(0.0)
    };

    let peer_iv = pyf(get(&peer_mark, "markIV"), 0.0);
    let mut skew_ratio = 0.0;
    let mut skew_score = 0.5;
    if peer_iv > 0.0 {
        skew_ratio = (iv - peer_iv) / peer_iv.max(0.01);
        skew_score = clamp(0.5 - skew_ratio / 0.8, 0.0, 1.0);
    }

    let mut surface_ivs: Vec<f64> = Vec::new();
    for payload in surface_mark_payloads {
        let row = first_row(payload);
        let peer_surface_iv = pyf(get(&row, "markIV"), 0.0);
        if peer_surface_iv > 0.0 {
            surface_ivs.push(peer_surface_iv);
        }
    }
    let surface_reference_iv = if surface_ivs.is_empty() {
        0.0
    } else {
        median(&surface_ivs)
    };
    let mut surface_iv_premium_pct = 0.0;
    let mut surface_score = 0.50;
    if surface_reference_iv > 0.0 {
        surface_iv_premium_pct = iv / surface_reference_iv - 1.0;
        if surface_iv_premium_pct > pyf(get(cfg, "max_surface_iv_premium_pct"), 0.30) {
            return json!({
                "accepted": false,
                "reason": "OPTION_IV_SURFACE_PREMIUM_TOO_HIGH",
                "surface_iv_premium_pct": surface_iv_premium_pct,
            });
        }
        surface_score = clamp(0.60 - surface_iv_premium_pct / 0.60, 0.0, 1.0);
    }

    let open_interest = pyf(get(&ticker, "openInterest"), 0.0)
        .max(pyf(get(&mark, "openInterest"), 0.0))
        .max(pyf(get(contract, "openInterest"), 0.0));
    // log1p(volume) / log1p(max(1000.0, min_volume * 20.0)), capped at 1.
    let mut liquidity_score =
        (quote_volume.max(0.0).ln_1p() / (1000.0_f64).max(min_volume * 20.0).ln_1p()).min(1.0);
    let depth_score = (depth_qty / (pyf(get(contract, "min_qty"), 0.01) * 50.0).max(1.0)).min(1.0);
    if open_interest > 0.0 {
        liquidity_score =
            (liquidity_score * 0.75 + (open_interest.ln_1p() / 8.0).min(1.0) * 0.25).min(1.0);
    }

    let gamma = greek(&mark, "gamma");
    let theta = greek(&mark, "theta");
    let vega = greek(&mark, "vega");
    let theta_burden = if theta != 0.0 {
        theta.abs() / mid.max(1e-9)
    } else {
        0.0
    };
    let mut greek_score = 1.0 - (theta_burden / 0.25).min(1.0);
    if gamma > 0.0 && strategy == "LOW_IV_SQUEEZE" {
        greek_score = (greek_score + 0.10).min(1.0);
    }

    let forecast_volatility = (0.03_f64).max(pyf(get(signal, "forecast_volatility"), realized));
    let tick = (0.0_f64).max(pyf(get(contract, "tick_size"), 0.0));
    let maker_price = if tick > 0.0 && bid + tick < ask {
        bid + tick
    } else {
        bid
    };
    let side_str = py_str_or_empty(get(contract, "side").unwrap_or(&Value::Null));
    let spot_sig = pyf(get(signal, "spot_price"), 0.0);
    let strike_contract = pyf(get(contract, "strikePrice"), 0.0);
    let score_sig = pyf(get(signal, "score"), 0.0);
    let exp_ret = pyf(get(signal, "expected_return_24h"), 0.0);
    let dir_scale = pyf(get(cfg, "directional_move_scale"), 1.10);

    let maker_edge = expected_option_net_edge(
        &side_str,
        spot_sig,
        strike_contract,
        dte,
        maker_price,
        forecast_volatility,
        score_sig,
        exp_ret,
        ask - bid,
        dir_scale,
    );
    let ioc_edge = expected_option_net_edge(
        &side_str,
        spot_sig,
        strike_contract,
        dte,
        ask + tick,
        forecast_volatility,
        score_sig,
        exp_ret,
        ask - bid,
        dir_scale,
    );
    let maker_accepted = match get(&maker_edge, "accepted") {
        Some(Value::Bool(b)) => *b,
        _ => false,
    };
    if !maker_accepted {
        return maker_edge;
    }
    let net_edge = pyf(get(&maker_edge, "net_expected_edge_pct"), -1.0);
    let min_edge = pyf(get(cfg, "min_net_expected_edge_pct"), 0.04);
    if net_edge < min_edge {
        return json!({
            "accepted": false,
            "reason": "OPTION_NET_EXPECTED_EDGE_TOO_LOW",
            "net_expected_edge_pct": net_edge,
        });
    }
    let ioc_net_edge = pyf(get(&ioc_edge, "net_expected_edge_pct"), -1.0);
    let ioc_eligible = ioc_net_edge >= pyf(get(cfg, "min_ioc_net_expected_edge_pct"), 0.10);
    let entry_fraction = if net_edge >= 0.45 {
        1.00
    } else if net_edge >= 0.25 {
        0.85
    } else {
        0.70
    };

    let edge_score = clamp((net_edge - min_edge) / 0.50, 0.0, 1.0);
    let flow_score_norm = clamp((flow_score_val + 1.0) / 2.0, 0.0, 1.0);

    let score = signal_strength * 19.0
        + edge_score * 20.0
        + spread_score * 11.0
        + delta_score * 11.0
        + dte_score * 9.0
        + iv_score * 10.0
        + skew_score * 5.0
        + surface_score * 6.0
        + flow_score_norm * 4.0
        + liquidity_score * 3.0
        + depth_score * 1.0
        + greek_score * 1.0;

    let mut out = Map::new();
    out.insert("accepted".into(), json!(true));
    out.insert("reason".into(), json!("OPTION_CONTRACT_READY"));
    out.insert("score".into(), json!(score));
    out.insert("bid".into(), json!(bid));
    out.insert("ask".into(), json!(ask));
    out.insert("mid".into(), json!(mid));
    out.insert("spread_pct".into(), json!(spread));
    out.insert("delta".into(), json!(delta));
    out.insert("mark_iv".into(), json!(iv));
    out.insert("iv_to_realized".into(), json!(iv_ratio));
    out.insert("quote_volume".into(), json!(quote_volume));
    out.insert("depth_qty".into(), json!(depth_qty));
    out.insert("open_interest".into(), json!(open_interest));
    out.insert("skew_peer_iv".into(), json!(peer_iv));
    out.insert("skew_ratio".into(), json!(skew_ratio));
    out.insert("target_delta".into(), json!(target_delta));
    out.insert("target_dte_days".into(), json!(target_dte));
    out.insert("gamma".into(), json!(gamma));
    out.insert("theta".into(), json!(theta));
    out.insert("vega".into(), json!(vega));
    out.insert("theta_burden".into(), json!(theta_burden));
    out.insert("forecast_volatility".into(), json!(forecast_volatility));
    out.insert("surface_reference_iv".into(), json!(surface_reference_iv));
    out.insert(
        "surface_iv_premium_pct".into(),
        json!(surface_iv_premium_pct),
    );
    out.insert("net_expected_edge_pct".into(), json!(net_edge));
    out.insert("ioc_net_expected_edge_pct".into(), json!(ioc_net_edge));
    out.insert("ioc_eligible".into(), json!(ioc_eligible));
    out.insert(
        "expected_option_payoff".into(),
        json!(pyf(get(&maker_edge, "expected_payoff"), 0.0)),
    );
    out.insert(
        "expected_move_pct".into(),
        json!(pyf(get(&maker_edge, "expected_move_pct"), 0.0)),
    );
    out.insert("entry_fraction".into(), json!(entry_fraction));
    for (k, v) in flow.iter() {
        out.insert(k.clone(), v.clone());
    }
    Value::Object(out)
}

// ---------------------------------------------------------------------------
// Peer lookups
// ---------------------------------------------------------------------------

/// Find the opposite-side contract with the closest strike at the same expiry.
pub fn find_skew_peer(exchange_info: &Value, contract: &Value) -> Option<Value> {
    let side = get_str_upper(contract, "side");
    let opposite = if side == "CALL" { "PUT" } else { "CALL" };
    let underlying = get_str_upper(contract, "underlying");
    let expiry = pyf(get(contract, "expiryDate"), 0.0) as i64;
    let strike = pyf(get(contract, "strikePrice"), 0.0);

    let mut peers: Vec<Value> = Vec::new();
    if let Some(Value::Array(rows)) = get(exchange_info, "optionSymbols") {
        for row in rows {
            if get_str_upper(row, "underlying") != underlying {
                continue;
            }
            if get_str_upper(row, "side") != opposite {
                continue;
            }
            if pyf(get(row, "expiryDate"), 0.0) as i64 != expiry {
                continue;
            }
            if get_str_upper(row, "status") != "TRADING" {
                continue;
            }
            peers.push(row.clone());
        }
    }
    if peers.is_empty() {
        return None;
    }
    peers.sort_by(|a, b| {
        let da = (pyf(get(a, "strikePrice"), 0.0) - strike).abs();
        let db = (pyf(get(b, "strikePrice"), 0.0) - strike).abs();
        da.partial_cmp(&db).unwrap_or(Ordering::Equal)
    });
    peers.first().cloned()
}

/// Return nearby same-side strikes/expiries for an IV-surface sanity check.
pub fn find_surface_peers(exchange_info: &Value, contract: &Value, max_peers: usize) -> Vec<Value> {
    let side = get_str_upper(contract, "side");
    let underlying = get_str_upper(contract, "underlying");
    let expiry = pyf(get(contract, "expiryDate"), 0.0) as i64;
    let strike = pyf(get(contract, "strikePrice"), 0.0);
    let symbol = match get(contract, "symbol") {
        Some(v) => py_str_or_empty(v),
        None => String::new(),
    };

    let mut peers: Vec<(f64, Value)> = Vec::new();
    if let Some(Value::Array(rows)) = get(exchange_info, "optionSymbols") {
        for row in rows {
            let row_symbol = match get(row, "symbol") {
                Some(v) => py_str_or_empty(v),
                None => String::new(),
            };
            if row_symbol == symbol {
                continue;
            }
            if get_str_upper(row, "underlying") != underlying {
                continue;
            }
            if get_str_upper(row, "side") != side {
                continue;
            }
            if get_str_upper(row, "status") != "TRADING" {
                continue;
            }
            let row_expiry = pyf(get(row, "expiryDate"), 0.0) as i64;
            let row_strike = pyf(get(row, "strikePrice"), 0.0);
            let strike_distance = (row_strike - strike).abs() / strike.max(1e-9);
            let expiry_distance = (row_expiry - expiry).abs() as f64 / 86_400_000.0;
            let distance = strike_distance * 8.0 + expiry_distance * 0.08;
            if row_expiry == expiry || strike_distance <= 0.08 {
                peers.push((distance, row.clone()));
            }
        }
    }
    peers.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(Ordering::Equal));
    let limit = max_peers.max(1);
    peers.into_iter().take(limit).map(|(_, row)| row).collect()
}

// ---------------------------------------------------------------------------
// risk.py
// ---------------------------------------------------------------------------

/// Python `Decimal(str(x))` for a float.
pub fn dec(x: f64) -> Decimal {
    if !x.is_finite() {
        return Decimal::ZERO;
    }
    let formatted = format!("{}", x);
    match Decimal::from_str_exact(&formatted) {
        Ok(d) => d,
        Err(_) => Decimal::ZERO,
    }
}

/// Python `floor_to_step(value, step)`.
pub fn floor_to_step(value: Decimal, step: Decimal) -> Decimal {
    let value_d = if value < Decimal::ZERO {
        Decimal::ZERO
    } else {
        value
    };
    let step_d = step;
    if step_d <= Decimal::ZERO {
        return value_d;
    }
    let div = match value_d.checked_div(step_d) {
        Some(d) => d,
        None => return value_d,
    };
    div.trunc() * step_d
}

/// Conservative Binance crypto-option fee estimate.
pub fn estimate_option_fee(option_price: f64, index_price: f64, quantity: f64, unit: f64) -> f64 {
    let price = dec(option_price).max(Decimal::ZERO);
    let index = dec(index_price).max(Decimal::ZERO);
    let qty = dec(quantity).max(Decimal::ZERO);
    let unit_d = dec(unit).max(Decimal::ZERO);
    let ten_pct = match Decimal::from_str_exact("0.10") {
        Ok(d) => d,
        Err(_) => Decimal::ZERO,
    };
    let fee_rate = match Decimal::from_str_exact("0.0003") {
        Ok(d) => d,
        Err(_) => Decimal::ZERO,
    };
    let premium_cap = price.checked_mul(ten_pct).unwrap_or(Decimal::ZERO);
    let per_unit = if index > Decimal::ZERO {
        let index_fee = index.checked_mul(fee_rate).unwrap_or(Decimal::ZERO);
        index_fee.min(premium_cap)
    } else {
        premium_cap
    };
    let product = per_unit
        .checked_mul(qty)
        .and_then(|v| v.checked_mul(unit_d))
        .unwrap_or(Decimal::ZERO);
    let result = product.max(Decimal::ZERO);
    result.to_f64().unwrap_or(0.0)
}

/// Build the long-option entry plan with hard capital controls.
#[allow(clippy::too_many_arguments)]
pub fn build_long_option_entry_plan(
    ask_price: f64,
    index_price: f64,
    unit: f64,
    min_qty: f64,
    step_size: f64,
    cash_bankroll_usdt: f64,
    entry_fraction: f64,
    capital_limit_usdt: f64,
) -> Value {
    let ask = dec(ask_price);
    let unit_d = {
        let d = dec(unit);
        if d == Decimal::ZERO && unit.is_nan() {
            Decimal::ONE
        } else {
            d
        }
    };
    let min_qty_d = dec(min_qty);
    let step_d = dec(step_size);
    let bankroll = dec(cash_bankroll_usdt).max(Decimal::ZERO);
    let cap = dec(capital_limit_usdt).max(Decimal::ZERO);
    let hard_cap = if cap > Decimal::ZERO {
        cap.min(bankroll)
    } else {
        bankroll
    };
    let one = Decimal::ONE;
    let fraction = {
        let entry = dec(entry_fraction);
        let lower = match Decimal::from_str_exact("0.10") {
            Ok(d) => d,
            Err(_) => Decimal::ZERO,
        };
        one.min(entry.max(lower))
    };
    let spend_cap = hard_cap.checked_mul(fraction).unwrap_or(Decimal::ZERO);
    if ask <= Decimal::ZERO
        || unit_d <= Decimal::ZERO
        || min_qty_d <= Decimal::ZERO
        || step_d <= Decimal::ZERO
        || spend_cap <= Decimal::ZERO
    {
        return json!({"accepted": false, "reason": "INVALID_OPTION_ORDER_INPUT"});
    }

    let denom = match ask.checked_mul(unit_d) {
        Some(d) if d != Decimal::ZERO => d,
        _ => {
            return json!({"accepted": false, "reason": "INVALID_OPTION_ORDER_INPUT"});
        }
    };
    let raw_qty = match spend_cap.checked_div(denom) {
        Some(v) => v,
        None => {
            return json!({"accepted": false, "reason": "INVALID_OPTION_ORDER_INPUT"});
        }
    };
    let mut qty = floor_to_step(raw_qty, step_d);
    if qty < min_qty_d {
        return json!({
            "accepted": false,
            "reason": "MINIMUM_OPTION_CONTRACT_EXCEEDS_AVAILABLE_BANKROLL",
            "spend_cap_usdt": spend_cap.to_f64().unwrap_or(0.0),
        });
    }

    let ask_f = ask.to_f64().unwrap_or(0.0);
    let unit_f = unit_d.to_f64().unwrap_or(1.0);

    for _ in 0..3 {
        let premium = ask
            .checked_mul(qty)
            .and_then(|v| v.checked_mul(unit_d))
            .unwrap_or(Decimal::ZERO);
        let fee_f = estimate_option_fee(ask_f, index_price, qty.to_f64().unwrap_or(0.0), unit_f);
        let fee = dec(fee_f);
        let total = premium.checked_add(fee).unwrap_or(Decimal::MAX);
        if total <= spend_cap && total <= hard_cap {
            break;
        }
        let adjusted = match spend_cap.checked_sub(fee) {
            Some(v) => v,
            None => Decimal::ZERO,
        };
        let raw = match adjusted.checked_div(denom) {
            Some(v) => v,
            None => Decimal::ZERO,
        };
        qty = floor_to_step(raw, step_d);
        if qty < Decimal::ZERO {
            qty = Decimal::ZERO;
        }
    }

    let premium = ask
        .checked_mul(qty)
        .and_then(|v| v.checked_mul(unit_d))
        .unwrap_or(Decimal::ZERO);
    let fee_f = estimate_option_fee(ask_f, index_price, qty.to_f64().unwrap_or(0.0), unit_f);
    let fee = dec(fee_f);
    let total = premium.checked_add(fee).unwrap_or(Decimal::MAX);
    if qty < min_qty_d || total <= Decimal::ZERO || total > spend_cap || total > hard_cap {
        return json!({
            "accepted": false,
            "reason": "OPTION_PREMIUM_PLUS_FEE_EXCEEDS_HARD_CAP",
            "spend_cap_usdt": spend_cap.to_f64().unwrap_or(0.0),
        });
    }

    // Python: format(qty.normalize(), "f")
    let normalized = qty.normalize();
    let quantity_str = normalized.to_string();

    json!({
        "accepted": true,
        "quantity": quantity_str,
        "premium_usdt": premium.to_f64().unwrap_or(0.0),
        "estimated_entry_fee_usdt": fee.to_f64().unwrap_or(0.0),
        "total_entry_cost_usdt": total.to_f64().unwrap_or(0.0),
        "spend_cap_usdt": spend_cap.to_f64().unwrap_or(0.0),
        "hard_cap_usdt": hard_cap.to_f64().unwrap_or(0.0),
    })
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_clamp() {
        assert_eq!(clamp(2.0, -1.0, 1.0), 1.0);
        assert_eq!(clamp(-2.0, -1.0, 1.0), -1.0);
        assert_eq!(clamp(0.5, -1.0, 1.0), 0.5);
    }

    #[test]
    fn test_py_slice() {
        let v = vec![0, 1, 2, 3, 4];
        assert_eq!(py_slice(&v, -3, None), vec![2, 3, 4]);
        assert_eq!(py_slice(&v, -3, Some(-1)), vec![2, 3]);
        assert_eq!(py_slice(&v, 0, Some(-1)), vec![0, 1, 2, 3]);
        assert_eq!(py_slice::<i32>(&v, 3, Some(1)), Vec::<i32>::new());
        assert_eq!(py_slice(&v, -100, None), vec![0, 1, 2, 3, 4]);
    }

    #[test]
    fn test_floor_to_step() {
        let v = dec(1.237);
        let s = dec(0.01);
        let r = floor_to_step(v, s);
        assert_eq!(r, dec(1.23));
    }

    #[test]
    fn test_normal_cdf_zero() {
        assert!((normal_cdf(0.0) - 0.5).abs() < 1e-12);
    }

    #[test]
    fn test_entry_plan_small_bankroll() {
        let plan = build_long_option_entry_plan(12.0, 2000.0, 1.0, 0.01, 0.01, 20.0, 0.90, 500.0);
        assert_eq!(plan.get("accepted").and_then(|v| v.as_bool()), Some(true));
        let total = plan
            .get("total_entry_cost_usdt")
            .and_then(|v| v.as_f64())
            .unwrap_or(f64::INFINITY);
        assert!(total <= 18.0 + 1e-9);
        let hard = plan
            .get("hard_cap_usdt")
            .and_then(|v| v.as_f64())
            .unwrap_or(0.0);
        assert!((hard - 20.0).abs() < 1e-9);
        let fee = plan
            .get("estimated_entry_fee_usdt")
            .and_then(|v| v.as_f64())
            .unwrap_or(0.0);
        assert!(fee > 0.0);
    }

    #[test]
    fn test_entry_plan_reject_minimum() {
        // entry_fraction defaults to 1.0, capital_limit_usdt defaults to 0.0
        let plan = build_long_option_entry_plan(100.0, 2000.0, 1.0, 1.0, 1.0, 20.0, 1.0, 0.0);
        assert_eq!(plan.get("accepted").and_then(|v| v.as_bool()), Some(false));
        let reason = plan.get("reason").and_then(|v| v.as_str()).unwrap_or("");
        assert!(reason.contains("MINIMUM"));
    }
}
