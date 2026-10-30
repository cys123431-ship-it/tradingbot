//! SMA cross signal engine ported from `btc_ma_cross/signals.py`.

use serde::{Deserialize, Serialize};

use super::candles::{closed_candles, timeframe_ms, Candle};

use super::num::{de_f64, de_i64, de_opt_f64};

/// Config keys read by the SMA-cross engine.  Python key names are kept.
#[derive(Debug, Clone, Deserialize)]
pub struct CrossConfig {
    pub timeframe: String,
    #[serde(deserialize_with = "de_i64")]
    pub fast_period: i64,
    #[serde(deserialize_with = "de_i64")]
    pub slow_period: i64,
    #[serde(default, deserialize_with = "de_opt_f64")]
    pub min_median_bar_range_pct: Option<f64>,
    #[serde(default, deserialize_with = "de_opt_f64")]
    pub max_zero_range_bar_share: Option<f64>,
    #[serde(deserialize_with = "de_f64")]
    pub signal_max_age_seconds: f64,
}

/// Result of `evaluate_cross`.
#[derive(Debug, Clone, Serialize)]
pub struct CrossResult {
    pub symbol: String,
    pub market_id: String,
    pub ccxt_symbol: String,
    pub timeframe: String,
    pub timestamp_ms: i64,
    pub side: Option<String>,
    pub trend_side: Option<String>,
    pub stale: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub flat: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bar_open_ms: Option<i64>, // Python: int candle open time
    #[serde(skip_serializing_if = "Option::is_none")]
    pub close: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sma_fast: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sma_slow: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sma_fast_prev: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sma_slow_prev: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub signal_age_seconds: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub skip_reason: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub signal_id: Option<String>,
}

/// Simple moving average; length equals `values.len()` once `period` is met.
/// The leading `period - 1` entries are `None` (Python `None`).
pub fn sma_opt(values: &[f64], period: usize) -> Vec<Option<f64>> {
    if period == 0 || values.len() < period {
        return Vec::new();
    }
    let mut out: Vec<Option<f64>> = Vec::with_capacity(values.len());
    out.extend(std::iter::repeat_n(None, period - 1));
    let mut window: f64 = values[..period].iter().sum();
    out.push(Some(window / period as f64));
    for i in period..values.len() {
        window += values[i] - values[i - period];
        out.push(Some(window / period as f64));
    }
    out
}

/// Same as `sma_opt` but returns only the finite values (drop the leading None).
pub fn sma(values: &[f64], period: usize) -> Vec<f64> {
    sma_opt(values, period).into_iter().flatten().collect()
}

fn timeframe_scale(timeframe: &str) -> f64 {
    let ms = timeframe_ms(timeframe).unwrap_or(3_600_000) as f64;
    (ms / 3_600_000.0).powf(0.5)
}

/// True for a frozen / untraded market (a horizontal-line chart).
pub fn market_is_flat(closed: &[Candle], cfg: &CrossConfig, lookback: usize) -> bool {
    let start = closed.len().saturating_sub(lookback);
    let recent = &closed[start..];
    if recent.is_empty() {
        return true;
    }
    let mut ranges: Vec<f64> = recent
        .iter()
        .filter(|r| r[4] > 0.0)
        .map(|r| (r[2] - r[3]) / r[4])
        .collect();
    if ranges.is_empty() {
        return true;
    }
    ranges.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let median = ranges[ranges.len() / 2];
    let zero_share = ranges.iter().filter(|v| **v <= 0.0).count() as f64 / ranges.len() as f64;
    let min_pct = cfg.min_median_bar_range_pct.unwrap_or(0.0005);
    let min_range = min_pct * timeframe_scale(&cfg.timeframe);
    let max_zero = cfg.max_zero_range_bar_share.unwrap_or(0.1);
    median < min_range || zero_share > max_zero
}

/// Cross of SMA(fast) over SMA(slow) on the last *closed* candle.
pub fn evaluate_cross(
    rows: &[Candle],
    cfg: &CrossConfig,
    now_ms: i64,
    symbol: &str,
    ccxt_symbol: Option<&str>,
) -> CrossResult {
    let timeframe = cfg.timeframe.clone();
    let fast_p = cfg.fast_period as usize;
    let slow_p = cfg.slow_period as usize;
    let closed = closed_candles(rows, &timeframe, now_ms);
    let default_ccxt = format!("{}/USDT:USDT", &symbol[..symbol.len().saturating_sub(4)]);
    let mut result = CrossResult {
        symbol: symbol.to_string(),
        market_id: symbol.to_string(),
        ccxt_symbol: ccxt_symbol.map(|s| s.to_string()).unwrap_or(default_ccxt),
        timeframe: timeframe.clone(),
        timestamp_ms: now_ms,
        side: None,
        trend_side: None,
        stale: false,
        flat: None,
        bar_open_ms: None,
        close: None,
        sma_fast: None,
        sma_slow: None,
        sma_fast_prev: None,
        sma_slow_prev: None,
        signal_age_seconds: None,
        skip_reason: None,
        signal_id: None,
    };
    if closed.len() < slow_p + 1 {
        result.skip_reason = Some(format!(
            "INSUFFICIENT_CANDLES:{}<{}",
            closed.len(),
            slow_p + 1
        ));
        return result;
    }
    if market_is_flat(&closed, cfg, 100) {
        result.flat = Some(true);
        result.skip_reason = Some("FLAT_MARKET".to_string());
        return result;
    }
    let closes: Vec<f64> = closed.iter().map(|r| r[4]).collect();
    let fast = sma_opt(&closes, fast_p);
    let slow = sma_opt(&closes, slow_p);
    let f_now = fast[fast.len() - 1].unwrap_or(0.0);
    let s_now = slow[slow.len() - 1].unwrap_or(0.0);
    let f_prev = fast[fast.len() - 2].unwrap_or(0.0);
    let s_prev = slow[slow.len() - 2].unwrap_or(0.0);
    let bar = closed[closed.len() - 1];
    let span = timeframe_ms(&timeframe).unwrap_or(0);
    result.bar_open_ms = Some(bar[0] as i64);
    result.close = Some(bar[4]);
    result.sma_fast = Some(f_now);
    result.sma_slow = Some(s_now);
    result.sma_fast_prev = Some(f_prev);
    result.sma_slow_prev = Some(s_prev);
    result.signal_age_seconds = Some((now_ms - bar[0] as i64 - span) as f64 / 1000.0);
    result.trend_side = if f_now > s_now {
        Some("LONG".to_string())
    } else if f_now < s_now {
        Some("SHORT".to_string())
    } else {
        None
    };
    let max_age = cfg.signal_max_age_seconds.min(span as f64 / 2000.0);
    let age = result.signal_age_seconds.unwrap_or(0.0);
    result.stale = age > max_age;
    let side = if f_prev <= s_prev && f_now > s_now {
        Some("LONG")
    } else if f_prev >= s_prev && f_now < s_now {
        Some("SHORT")
    } else {
        None
    };
    let Some(side) = side else {
        result.skip_reason = Some(
            if f_now > s_now {
                "NO_CROSS_ABOVE"
            } else {
                "NO_CROSS_BELOW"
            }
            .to_string(),
        );
        return result;
    };
    if result.stale {
        result.skip_reason = Some("SIGNAL_STALE".to_string());
        return result;
    }
    result.side = Some(side.to_string());
    result.skip_reason = Some(String::new());
    result.signal_id = Some(format!(
        "{}:SMA{}/{}:{}:{}:{}",
        symbol, fast_p, slow_p, timeframe, side, bar[0] as i64
    ));
    result
}

/// Margin ROI in percent.
pub fn margin_roi(side: &str, entry_price: f64, price: f64, leverage: f64) -> f64 {
    let direction = if side == "LONG" { 1.0 } else { -1.0 };
    direction * (price / entry_price - 1.0) * leverage * 100.0
}

/// (step reached, locked ROI, stop price) for the best price seen, or `None`.
pub fn profit_lock_target(
    side: &str,
    entry_price: f64,
    best_price: f64,
    leverage: f64,
    start: f64,
    step: f64,
    gap: f64,
) -> Option<(f64, f64, f64)> {
    let peak = margin_roi(side, entry_price, best_price, leverage);
    if peak + 1e-9 < start {
        return None;
    }
    let reached = start + ((peak - start) / step + 1e-9).floor() * step;
    let locked = reached - gap;
    let direction = if side == "LONG" { 1.0 } else { -1.0 };
    let price = entry_price * (1.0 + direction * locked / (leverage * 100.0));
    Some((reached, locked, price))
}

/// Emergency stop price from a margin-ROI loss, or `None` if `roi_percent` is 0.
pub fn emergency_stop_price(
    side: &str,
    entry_price: f64,
    leverage: f64,
    roi_percent: f64,
) -> Option<f64> {
    if roi_percent == 0.0 {
        return None;
    }
    let direction = if side == "LONG" { 1.0 } else { -1.0 };
    Some(entry_price * (1.0 - direction * roi_percent / (leverage * 100.0)))
}
