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
    /// Stop SMA period (0 / missing = no stop line).
    #[serde(default)]
    pub stop_ma_period: Option<i64>,
    /// 2 = also report a cross on the bar before the last one.
    #[serde(default)]
    pub cross_lookback_bars: Option<i64>,
    /// Optional exit line: SMA(fast) against SMA(exit_ma_period).
    #[serde(default)]
    pub exit_ma_enabled: Option<bool>,
    #[serde(default, deserialize_with = "de_lenient_opt_i64")]
    pub exit_ma_period: Option<i64>,
    /// "align" = SMA(fast)/SMA(mid)/SMA(slow) alignment signals (XAU strategy).
    #[serde(default)]
    pub signal_mode: Option<String>,
    #[serde(default, deserialize_with = "de_lenient_opt_i64")]
    pub align_mid_period: Option<i64>,
    /// false = alignment ignores SMA(slow): close > SMA(mid) only (/tradfi toggle).
    #[serde(default)]
    pub align_slow_enabled: Option<bool>,
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
    pub sma_stop: Option<f64>,
    /// Middle SMA of the alignment mode.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sma_mid: Option<f64>,
    /// Exit line state; this, `exit_ma_period`, `exit_side` and `exit_trend_side`
    /// are always present (null when unused), like Python.
    pub exit_ma_enabled: bool,
    pub exit_ma_period: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sma_exit: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sma_exit_prev: Option<f64>,
    /// SMA(fast) crossed the exit line against the held side on this close.
    pub exit_side: Option<String>,
    /// Which side SMA(fast) currently sits on relative to the exit line.
    pub exit_trend_side: Option<String>,
    /// Cross on the bar before the last one that still holds on the last bar.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub prev_cross_side: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub prev_cross_bar_ms: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub signal_age_seconds: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub skip_reason: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub signal_id: Option<String>,
}

/// Lenient integer read: a JSON number or a numeric string, anything else is
/// `None`. A corrupt value must disable the feature, never reject the whole
/// config (which would stop the bot on every poll).
fn de_lenient_opt_i64<'de, D>(deserializer: D) -> Result<Option<i64>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let value: Option<serde_json::Value> = Option::deserialize(deserializer)?;
    Ok(match value {
        Some(serde_json::Value::Number(n)) => n.as_i64(),
        Some(serde_json::Value::String(s)) => s.trim().parse::<f64>().ok().map(|f| f as i64),
        _ => None,
    })
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

/// Python `_exit_period`: the validated exit SMA period, or `None` when the
/// feature is off or the value is unusable. The line must be a real average
/// (>= 2) and needs two of its own values to detect a cross, so a bad config
/// disables it instead of raising.
pub fn exit_period(cfg: &CrossConfig) -> Option<i64> {
    if !cfg.exit_ma_enabled.unwrap_or(false) {
        return None;
    }
    let period = cfg.exit_ma_period?;
    if period < 2 || period > 500.max(cfg.slow_period) {
        return None;
    }
    Some(period)
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
        sma_stop: None,
        sma_mid: None,
        exit_ma_enabled: cfg.exit_ma_enabled.unwrap_or(false),
        exit_ma_period: None,
        sma_exit: None,
        sma_exit_prev: None,
        exit_side: None,
        exit_trend_side: None,
        prev_cross_side: None,
        prev_cross_bar_ms: None,
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
    if let Some(p) = cfg.stop_ma_period.filter(|p| *p > 0) {
        if let Some(Some(v)) = sma_opt(&closes, p as usize).last() {
            result.sma_stop = Some(*v);
        }
    }
    result.signal_age_seconds = Some((now_ms - bar[0] as i64 - span) as f64 / 1000.0);
    // Exit line: not enough history means never trade on a half-computed line.
    if let Some(p) = exit_period(cfg).filter(|p| closed.len() > *p as usize) {
        let line = sma(&closes, p as usize);
        if line.len() >= 2 {
            let now = line[line.len() - 1];
            let prev = line[line.len() - 2];
            result.exit_ma_period = Some(p);
            result.sma_exit = Some(now);
            result.sma_exit_prev = Some(prev);
            result.exit_trend_side = if f_now > now {
                Some("LONG".to_string())
            } else if f_now < now {
                Some("SHORT".to_string())
            } else {
                None
            };
            result.exit_side = if f_prev <= prev && f_now > now {
                Some("LONG".to_string())
            } else if f_prev >= prev && f_now < now {
                Some("SHORT".to_string())
            } else {
                None
            };
        }
    }
    if cfg.signal_mode.as_deref() == Some("align") {
        return finish_align(result, cfg, &closes, &fast, &slow, bar, span, symbol);
    }
    result.trend_side = if f_now > s_now {
        Some("LONG".to_string())
    } else if f_now < s_now {
        Some("SHORT".to_string())
    } else {
        None
    };
    if cfg.cross_lookback_bars.unwrap_or(1) >= 2 && fast.len() >= 3 && slow.len() >= 3 {
        if let (Some(f2), Some(s2)) = (fast[fast.len() - 3], slow[slow.len() - 3]) {
            let prev = if f2 <= s2 && f_prev > s_prev && f_now > s_now {
                Some("LONG")
            } else if f2 >= s2 && f_prev < s_prev && f_now < s_now {
                Some("SHORT")
            } else {
                None
            };
            if let Some(p) = prev {
                result.prev_cross_side = Some(p.to_string());
                result.prev_cross_bar_ms = Some(closed[closed.len() - 2][0] as i64);
            }
        }
    }
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

/// Alignment signals: bullish = SMA(fast) > SMA(mid) > SMA(slow), bearish the
/// mirror. `trend_side` is the alignment that holds on the last closed bar with
/// SMA(fast) beyond the exit line on that side; `side` is set only on the bar
/// where that alignment newly forms (it did not hold on the bar before).
#[allow(clippy::too_many_arguments)]
fn finish_align(
    mut result: CrossResult,
    cfg: &CrossConfig,
    closes: &[f64],
    fast: &[Option<f64>],
    slow: &[Option<f64>],
    bar: Candle,
    span: i64,
    symbol: &str,
) -> CrossResult {
    let mid_p = cfg.align_mid_period.unwrap_or(5).max(2) as usize;
    let mid = sma_opt(closes, mid_p);
    let at = |v: &[Option<f64>], back: usize| -> Option<f64> {
        v.len().checked_sub(back + 1).and_then(|i| v[i])
    };
    let (Some(f_now), Some(f_prev), Some(m_now), Some(m_prev), Some(s_now), Some(s_prev)) = (
        at(fast, 0),
        at(fast, 1),
        at(&mid, 0),
        at(&mid, 1),
        at(slow, 0),
        at(slow, 1),
    ) else {
        result.skip_reason = Some("INSUFFICIENT_CANDLES:ALIGN".to_string());
        return result;
    };
    result.sma_mid = Some(m_now);
    let use_slow = cfg.align_slow_enabled.unwrap_or(true);
    let bull = |f: f64, m: f64, s: f64| f > m && (!use_slow || m > s);
    let bear = |f: f64, m: f64, s: f64| f < m && (!use_slow || m < s);
    let beyond_long = result.sma_exit.map(|x| f_now > x).unwrap_or(true);
    let beyond_short = result.sma_exit.map(|x| f_now < x).unwrap_or(true);
    let bull_now = bull(f_now, m_now, s_now);
    let bear_now = bear(f_now, m_now, s_now);
    result.trend_side = if bull_now && beyond_long {
        Some("LONG".to_string())
    } else if bear_now && beyond_short {
        Some("SHORT".to_string())
    } else {
        None
    };
    let max_age = cfg.signal_max_age_seconds.min(span as f64 / 2000.0);
    result.stale = result.signal_age_seconds.unwrap_or(0.0) > max_age;
    let side = if bull_now && beyond_long && !bull(f_prev, m_prev, s_prev) {
        Some("LONG")
    } else if bear_now && beyond_short && !bear(f_prev, m_prev, s_prev) {
        Some("SHORT")
    } else {
        None
    };
    let Some(side) = side else {
        let reason = match result.trend_side.as_deref() {
            Some("LONG") => "ALIGNMENT_HELD_LONG",
            Some("SHORT") => "ALIGNMENT_HELD_SHORT",
            _ if bull_now || bear_now => "ALIGNED_BEHIND_EXIT_LINE",
            _ => "NOT_ALIGNED",
        };
        result.skip_reason = Some(reason.to_string());
        return result;
    };
    if result.stale {
        result.skip_reason = Some("SIGNAL_STALE".to_string());
        return result;
    }
    result.side = Some(side.to_string());
    result.skip_reason = Some(String::new());
    let slow_tag = if use_slow { cfg.slow_period } else { 0 };
    result.signal_id = Some(format!(
        "{}:ALIGN{}/{}/{}:{}:{}:{}",
        symbol, cfg.fast_period, mid_p, slow_tag, cfg.timeframe, side, bar[0] as i64
    ));
    result
}

/// True when `close` sits on the losing side of the stop SMA for `side`
/// (LONG below it, SHORT above it).
pub fn beyond_stop_ma(side: &str, close: f64, sma_stop: f64) -> bool {
    if side == "LONG" {
        close < sma_stop
    } else {
        close > sma_stop
    }
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

/// Averaging in: (margin loss % vs the first entry at a bar close, fraction of the first quantity).
pub const SCALE_IN_STEPS: &[(f64, f64)] = &[(10.0, 0.5), (20.0, 0.5)];
/// With averaging in, the stop closes everything at this loss in % of the first margin.
pub const SCALE_IN_MAX_LOSS_ROI: f64 = 50.0;

/// Loss budget (% of the first margin): the emergency stop if tighter, else 50.
pub fn scale_in_loss_roi(emergency_roi: f64) -> f64 {
    if emergency_roi > 0.0 && emergency_roi < SCALE_IN_MAX_LOSS_ROI {
        emergency_roi
    } else {
        SCALE_IN_MAX_LOSS_ROI
    }
}

/// Fraction of the first quantity to add at this close after `done` adds, or `None`.
pub fn scale_in_fraction(
    side: &str,
    first_entry: f64,
    close: f64,
    leverage: f64,
    done: usize,
) -> Option<f64> {
    let (step, fraction) = SCALE_IN_STEPS.get(done)?;
    let roi = margin_roi(side, first_entry, close, leverage);
    if roi <= -*step + 1e-9 {
        Some(*fraction)
    } else {
        None
    }
}

/// Price at which the whole position has lost `loss_roi` % of the first margin.
pub fn scale_in_stop_price(
    side: &str,
    avg_entry: f64,
    quantity: f64,
    first_entry: f64,
    first_quantity: f64,
    leverage: f64,
    loss_roi: f64,
) -> Option<f64> {
    if quantity <= 0.0 || leverage <= 0.0 {
        return None;
    }
    let direction = if side == "LONG" { 1.0 } else { -1.0 };
    let first_margin = first_entry * first_quantity / leverage;
    Some(avg_entry - direction * (loss_roi / 100.0 * first_margin) / quantity)
}

#[cfg(test)]
mod align_tests {
    use super::*;

    const SPAN: i64 = 14_400_000;

    fn cfg(exit_p: i64) -> CrossConfig {
        serde_json::from_value(serde_json::json!({
            "timeframe": "4h", "fast_period": 1, "slow_period": 120,
            "signal_max_age_seconds": 180, "exit_ma_enabled": true,
            "exit_ma_period": exit_p, "signal_mode": "align", "align_mid_period": 5,
        }))
        .unwrap()
    }

    /// Deterministic wavy series: long down leg, long up leg, down again.
    fn rows(n: usize) -> Vec<Candle> {
        let mut seed: u64 = 7;
        let mut price = 100.0;
        (0..n)
            .map(|i| {
                seed = seed
                    .wrapping_mul(6364136223846793005)
                    .wrapping_add(1442695040888963407);
                let noise = ((seed >> 33) as f64 / (1u64 << 31) as f64 - 0.5) * 0.8;
                let drift = if i < 200 {
                    -0.15
                } else if i < 420 {
                    0.22
                } else {
                    -0.2
                };
                let open = price;
                price = (price + drift + noise).max(1.0);
                [
                    i as f64 * SPAN as f64,
                    open,
                    open.max(price) + 0.3,
                    open.min(price) - 0.3,
                    price,
                    10.0,
                ]
            })
            .collect()
    }

    fn sma_at(c: &[f64], p: usize, i: usize) -> Option<f64> {
        (i + 1 >= p).then(|| c[i + 1 - p..=i].iter().sum::<f64>() / p as f64)
    }

    /// Reference: (trend_side, fresh side) on bar i, written independently.
    fn reference(
        c: &[f64],
        i: usize,
        exit_p: usize,
    ) -> (Option<&'static str>, Option<&'static str>) {
        let state = |j: usize| -> Option<&'static str> {
            let (m, s) = (sma_at(c, 5, j)?, sma_at(c, 120, j)?);
            if c[j] > m && m > s {
                Some("LONG")
            } else if c[j] < m && m < s {
                Some("SHORT")
            } else {
                None
            }
        };
        let x = sma_at(c, exit_p, i).unwrap();
        let now = state(i).filter(|side| if *side == "LONG" { c[i] > x } else { c[i] < x });
        let fresh = now.filter(|side| state(i - 1) != Some(*side));
        (now, fresh)
    }

    #[test]
    fn alignment_signals_match_reference_on_every_bar() {
        let all = rows(600);
        let closes: Vec<f64> = all.iter().map(|r| r[4]).collect();
        let (mut fresh_seen, mut held_seen) = (0, 0);
        for exit_p in [20usize, 30, 50] {
            let c = cfg(exit_p as i64);
            for i in 130..all.len() {
                let now_ms = i as i64 * SPAN + SPAN + 1_000; // 1 s after bar i closes
                let r = evaluate_cross(&all[..=i], &c, now_ms, "XAUUSDT", None);
                let (trend, fresh) = reference(&closes, i, exit_p);
                assert_eq!(
                    r.trend_side.as_deref(),
                    trend,
                    "trend bar {i} exit {exit_p}"
                );
                assert_eq!(r.side.as_deref(), fresh, "side bar {i} exit {exit_p}");
                if fresh.is_some() {
                    fresh_seen += 1;
                    assert!(r
                        .signal_id
                        .as_deref()
                        .unwrap_or("")
                        .contains(":ALIGN1/5/120:4h:"));
                } else if trend.is_some() {
                    held_seen += 1;
                    assert!(r
                        .skip_reason
                        .as_deref()
                        .unwrap_or("")
                        .starts_with("ALIGNMENT_HELD_"));
                }
            }
        }
        assert!(
            fresh_seen >= 6 && held_seen >= 100,
            "fresh {fresh_seen} held {held_seen}"
        );
    }

    #[test]
    fn alignment_without_slow_uses_close_and_mid_only() {
        let all = rows(600);
        let c: Vec<f64> = all.iter().map(|r| r[4]).collect();
        let mut cfg_off = cfg(30);
        cfg_off.align_slow_enabled = Some(false);
        let state = |j: usize| -> Option<&'static str> {
            let m = sma_at(&c, 5, j)?;
            if c[j] > m {
                Some("LONG")
            } else if c[j] < m {
                Some("SHORT")
            } else {
                None
            }
        };
        let (mut extra, mut fresh_seen) = (0, 0);
        for i in 130..all.len() {
            let now_ms = i as i64 * SPAN + SPAN + 1_000;
            let r = evaluate_cross(&all[..=i], &cfg_off, now_ms, "XAUUSDT", None);
            let x = sma_at(&c, 30, i).unwrap();
            let now = state(i).filter(|side| if *side == "LONG" { c[i] > x } else { c[i] < x });
            let fresh = now.filter(|side| state(i - 1) != Some(*side));
            assert_eq!(r.trend_side.as_deref(), now, "trend bar {i}");
            assert_eq!(r.side.as_deref(), fresh, "side bar {i}");
            if fresh.is_some() {
                fresh_seen += 1;
                assert!(r
                    .signal_id
                    .as_deref()
                    .unwrap_or("")
                    .contains(":ALIGN1/5/0:4h:"));
            }
            if now.is_some() && reference(&c, i, 30).0.is_none() {
                extra += 1;
            }
        }
        assert!(
            fresh_seen >= 6 && extra >= 10,
            "fresh {fresh_seen} extra {extra}"
        );
    }

    #[test]
    fn stale_alignment_does_not_signal() {
        let all = rows(600);
        let closes: Vec<f64> = all.iter().map(|r| r[4]).collect();
        let i = (130..600)
            .find(|&i| reference(&closes, i, 30).1.is_some())
            .unwrap();
        let late = i as i64 * SPAN + SPAN + 600_000; // 10 min after the close
        let r = evaluate_cross(&all[..=i], &cfg(30), late, "XAUUSDT", None);
        assert_eq!(r.side, None);
        assert_eq!(r.skip_reason.as_deref(), Some("SIGNAL_STALE"));
    }
}
