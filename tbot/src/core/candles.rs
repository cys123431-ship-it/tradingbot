//! Closed-candle utilities ported from `btc_ema_pullback/signals.py`.
//!
//! Candle rows are `[open_ms, open, high, low, close, volume]`; the timestamp is
//! kept as `f64` and cast with `as i64` when a wall-clock value is needed.

/// Candle: (ts, open, high, low, close, volume).
pub type Candle = [f64; 6];

/// Milliseconds per timeframe; `None` for unknown timeframes.
pub fn timeframe_ms(tf: &str) -> Option<i64> {
    let ms = match tf {
        "1m" => 60_000,
        "3m" => 180_000,
        "5m" => 300_000,
        "15m" => 900_000,
        "30m" => 1_800_000,
        "1h" => 3_600_000,
        "2h" => 7_200_000,
        "4h" => 14_400_000,
        _ => return None,
    };
    Some(ms)
}

/// Drop the in-progress candle (and anything malformed), sort by open time.
pub fn closed_candles(rows: &[Candle], timeframe: &str, now_ms: i64) -> Vec<Candle> {
    let span = timeframe_ms(timeframe).unwrap_or(0);
    let mut result: Vec<Candle> = Vec::new();
    for row in rows {
        let open_ms = row[0] as i64;
        // values = row[1..5]; the `as i64` above mirrors `int(row[0])`.
        let (o, h, l, c) = (row[1], row[2], row[3], row[4]);
        if !(o.is_finite() && h.is_finite() && l.is_finite() && c.is_finite()) {
            continue;
        }
        let min_value = o.min(h).min(l).min(c);
        if open_ms + span <= now_ms && min_value > 0.0 {
            result.push([row[0], o, h, l, c, row[5]]);
        }
    }
    result.sort_by(|a, b| a[0].partial_cmp(&b[0]).unwrap_or(std::cmp::Ordering::Equal));
    result
}
