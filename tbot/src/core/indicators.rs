use crate::core::candles::Candle;

/// Simple moving average aligned with `values` (None before the first full window).
pub fn sma(values: &[f64], period: usize) -> Vec<Option<f64>> {
    if period == 0 || values.len() < period {
        return Vec::new();
    }
    let mut out: Vec<Option<f64>> = vec![None; period - 1];
    let mut window: f64 = values[..period].iter().sum();
    out.push(Some(window / period as f64));
    for i in period..values.len() {
        window += values[i] - values[i - period];
        out.push(Some(window / period as f64));
    }
    out
}

/// True range per candle (first candle uses high-low).
pub fn true_range(rows: &[Candle]) -> Vec<f64> {
    let mut out = Vec::with_capacity(rows.len());
    let mut prev_close: Option<f64> = None;
    for row in rows {
        let high = row[2];
        let low = row[3];
        let close = row[4];
        match prev_close {
            None => out.push(high - low),
            Some(pc) => out.push(f64::max(
                high - low,
                f64::max((high - pc).abs(), (low - pc).abs()),
            )),
        }
        prev_close = Some(close);
    }
    out
}

/// Wilder-smoothed ATR aligned with `rows` (None until `period` values).
pub fn atr(rows: &[Candle], period: usize) -> Vec<Option<f64>> {
    let tr = true_range(rows);
    if period == 0 || tr.len() < period {
        return Vec::new();
    }
    let mut out: Vec<Option<f64>> = vec![None; period - 1];
    let mut current: f64 = tr[..period].iter().sum::<f64>() / period as f64;
    out.push(Some(current));
    for &value in &tr[period..] {
        current = (current * (period as f64 - 1.0) + value) / period as f64;
        out.push(Some(current));
    }
    out
}

/// Kaufman efficiency ratio: |net move| / sum(|bar-to-bar moves|) over `period`.
pub fn trend_efficiency(values: &[f64], period: usize) -> Vec<Option<f64>> {
    let mut out: Vec<Option<f64>> = vec![None; values.len()];
    if period == 0 {
        return out;
    }
    for i in period..values.len() {
        let window = &values[i - period..=i];
        let net = (window[window.len() - 1] - window[0]).abs();
        let mut path = 0.0;
        for j in 1..window.len() {
            path += (window[j] - window[j - 1]).abs();
        }
        out[i] = Some(if path != 0.0 { net / path } else { 0.0 });
    }
    out
}

/// (upper, lower) of the previous `period` candles, excluding the current one.
pub fn donchian(rows: &[Candle], period: usize) -> (Vec<Option<f64>>, Vec<Option<f64>>) {
    let mut upper: Vec<Option<f64>> = vec![None; rows.len()];
    let mut lower: Vec<Option<f64>> = vec![None; rows.len()];
    if period == 0 {
        return (upper, lower);
    }
    for i in period..rows.len() {
        let window = &rows[i - period..i];
        let mut hi = f64::NEG_INFINITY;
        let mut lo = f64::INFINITY;
        for r in window {
            if r[2] > hi {
                hi = r[2];
            }
            if r[3] < lo {
                lo = r[3];
            }
        }
        upper[i] = Some(hi);
        lower[i] = Some(lo);
    }
    (upper, lower)
}

pub fn avg_volume(rows: &[Candle], period: usize) -> Vec<Option<f64>> {
    let mut out: Vec<Option<f64>> = vec![None; rows.len()];
    if period == 0 {
        return out;
    }
    for i in (period - 1)..rows.len() {
        let window = &rows[i + 1 - period..=i];
        let sum: f64 = window.iter().map(|r| r[5]).sum();
        out[i] = Some(sum / period as f64);
    }
    out
}

pub fn floor_to_step(value: f64, step: f64) -> f64 {
    if step == 0.0 || step <= 0.0 {
        return value;
    }
    (value / step).floor() * step
}

/// Wilder ADX aligned with `rows` (None until enough history).
pub fn adx(rows: &[Candle], period: usize) -> Vec<Option<f64>> {
    let count = rows.len();
    if period == 0 || count < period * 2 + 1 {
        return Vec::new();
    }
    let mut plus_dm: Vec<f64> = Vec::new();
    let mut minus_dm: Vec<f64> = Vec::new();
    let mut tr: Vec<f64> = Vec::new();
    for i in 1..count {
        let up = rows[i][2] - rows[i - 1][2];
        let down = rows[i - 1][3] - rows[i][3];
        plus_dm.push(if up > down && up > 0.0 { up } else { 0.0 });
        minus_dm.push(if down > up && down > 0.0 { down } else { 0.0 });
        let prev_close = rows[i - 1][4];
        tr.push(f64::max(
            rows[i][2] - rows[i][3],
            f64::max(
                (rows[i][2] - prev_close).abs(),
                (rows[i][3] - prev_close).abs(),
            ),
        ));
    }

    let wilder = |values: &[f64]| -> Vec<f64> {
        let mut smoothed: Vec<f64> = Vec::new();
        let first: f64 = values[..period].iter().sum();
        smoothed.push(first);
        for &value in &values[period..] {
            let last = *smoothed.last().unwrap();
            smoothed.push(last - last / period as f64 + value);
        }
        smoothed
    };

    let sm_tr = wilder(&tr);
    let sm_plus = wilder(&plus_dm);
    let sm_minus = wilder(&minus_dm);
    let mut dx: Vec<f64> = Vec::new();
    for i in 0..sm_tr.len() {
        if sm_tr[i] == 0.0 {
            dx.push(0.0);
            continue;
        }
        let di_plus = 100.0 * sm_plus[i] / sm_tr[i];
        let di_minus = 100.0 * sm_minus[i] / sm_tr[i];
        let total = di_plus + di_minus;
        dx.push(if total != 0.0 {
            100.0 * (di_plus - di_minus).abs() / total
        } else {
            0.0
        });
    }
    if dx.len() < period {
        return Vec::new();
    }
    let mut smoothed: Vec<f64> = Vec::new();
    let first: f64 = dx[..period].iter().sum::<f64>() / period as f64;
    smoothed.push(first);
    for &value in &dx[period..] {
        let last = *smoothed.last().unwrap();
        smoothed.push((last * (period as f64 - 1.0) + value) / period as f64);
    }
    let pad = count - smoothed.len();
    let mut out: Vec<Option<f64>> = vec![None; pad];
    out.extend(smoothed.into_iter().map(Some));
    out
}

/// Risk multiplier that keeps a fixed % risk per trade (feature C).
pub fn volatility_target_scale(
    atr_value: Option<f64>,
    close: Option<f64>,
    target_pct: Option<f64>,
    min_scale: f64,
    max_scale: f64,
) -> f64 {
    let (close, atr_value, target_pct) = match (close, atr_value, target_pct) {
        (Some(c), Some(a), Some(t)) => (c, a, t),
        _ => return 1.0,
    };
    if close <= 0.0 || atr_value <= 0.0 || target_pct <= 0.0 {
        return 1.0;
    }
    let current = atr_value / close;
    if current <= 0.0 {
        return 1.0;
    }
    f64::max(min_scale, f64::min(max_scale, target_pct / current))
}

/// ATR chandelier trailing stop that never loosens (feature D).
pub fn chandelier_stop(
    side: &str,
    highest_high: Option<f64>,
    lowest_low: Option<f64>,
    atr_value: Option<f64>,
    multiplier: f64,
    previous: Option<f64>,
    breakeven: Option<f64>,
) -> Option<f64> {
    let atr_value = atr_value?;
    if atr_value <= 0.0 {
        return None;
    }
    if side.to_uppercase() == "LONG" {
        let hh = highest_high?;
        let mut candidate = hh - atr_value * multiplier;
        if let Some(be) = breakeven {
            candidate = f64::max(candidate, be);
        }
        if let Some(prev) = previous {
            candidate = f64::max(candidate, prev);
        }
        return Some(candidate);
    }
    let ll = lowest_low?;
    let mut candidate = ll + atr_value * multiplier;
    if let Some(be) = breakeven {
        candidate = f64::min(candidate, be);
    }
    if let Some(prev) = previous {
        candidate = f64::min(candidate, prev);
    }
    Some(candidate)
}
