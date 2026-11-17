//! Read-only BTC options metrics (max pain, OI walls, dealer gamma) from Deribit data.

use serde_json::{json, Value};

#[derive(Clone, Debug, PartialEq)]
pub struct OptionRow {
    pub expiry_ms: i64,
    pub strike: f64,
    pub is_call: bool,
    pub open_interest: f64,
    pub mark_iv: f64,
}

/// Month codes, index 0 = JAN.
const MONTHS: [&str; 12] = [
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
];

/// days-from-civil algorithm (Howard Hinnant). Returns days since 1970-01-01.
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

/// Parse a Deribit instrument name like "BTC-20NOV26-64000-P".
pub fn parse_instrument(name: &str) -> Option<(i64, f64, bool)> {
    let mut parts = name.split('-');
    let root = parts.next()?;
    let date = parts.next()?;
    let strike_s = parts.next()?;
    let kind = parts.next()?;
    if parts.next().is_some() {
        return None;
    }
    if root != "BTC" {
        return None;
    }
    if kind != "C" && kind != "P" {
        return None;
    }

    // date like "20NOV26" or "7NOV26"
    let bytes = date.as_bytes();
    let mut i = 0;
    while i < bytes.len() && bytes[i].is_ascii_digit() {
        i += 1;
    }
    if i == 0 || i > 2 {
        return None;
    }
    let day: i64 = date[0..i].parse().ok()?;
    if date.len() < i + 5 {
        return None;
    }
    let mon_s = &date[i..i + 3];
    let yr_s = &date[i + 3..];
    if yr_s.len() != 2 || !yr_s.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let mon_idx = MONTHS.iter().position(|&m| m == mon_s)? as i64;
    let month = mon_idx + 1;
    let yy: i64 = yr_s.parse().ok()?;
    let year = 2000 + yy;

    if !(1..=31).contains(&day) {
        return None;
    }
    if !(1..=12).contains(&month) {
        return None;
    }

    let strike: f64 = strike_s.parse().ok()?;
    if !strike.is_finite() || strike <= 0.0 {
        return None;
    }

    let days = days_from_civil(year, month, day);
    let expiry_ms = days * 86_400_000 + 8 * 3_600_000;
    let is_call = kind == "C";
    Some((expiry_ms, strike, is_call))
}

/// Build rows from the `result` array of Deribit get_book_summary_by_currency.
pub fn rows_from_book_summary(result: &Value) -> Vec<OptionRow> {
    let mut out = Vec::new();
    let arr = match result.as_array() {
        Some(a) => a,
        None => return out,
    };
    for item in arr {
        let name = match item.get("instrument_name").and_then(|v| v.as_str()) {
            Some(s) => s,
            None => continue,
        };
        let (expiry_ms, strike, is_call) = match parse_instrument(name) {
            Some(p) => p,
            None => continue,
        };
        let oi = match item.get("open_interest").and_then(|v| v.as_f64()) {
            Some(v) => v,
            None => continue,
        };
        if oi <= 0.0 {
            continue;
        }
        let iv = match item.get("mark_iv").and_then(|v| v.as_f64()) {
            Some(v) => v,
            None => continue,
        };
        if iv <= 0.0 {
            continue;
        }
        out.push(OptionRow {
            expiry_ms,
            strike,
            is_call,
            open_interest: oi,
            mark_iv: iv,
        });
    }
    out
}

pub fn norm_pdf(x: f64) -> f64 {
    let inv = 1.0 / (2.0 * std::f64::consts::PI).sqrt();
    inv * (-0.5 * x * x).exp()
}

/// Black-Scholes gamma (r = 0) of one option on one unit of underlying.
pub fn bs_gamma(spot: f64, strike: f64, iv: f64, years: f64) -> f64 {
    // `<= 0.0 || is_nan` so a NaN input also returns 0.0.
    if [spot, strike, iv, years]
        .iter()
        .any(|x| x.is_nan() || *x <= 0.0)
    {
        return 0.0;
    }
    let sqrt_t = years.sqrt();
    let d1 = ((spot / strike).ln() + 0.5 * iv * iv * years) / (iv * sqrt_t);
    norm_pdf(d1) / (spot * iv * sqrt_t)
}

/// Dealer gamma exposure in USD per 1% move.
pub fn net_gex(rows: &[OptionRow], spot: f64, now_ms: i64) -> f64 {
    if spot.is_nan() || spot <= 0.0 {
        return 0.0;
    }
    let floor_years = 1.0 / 8760.0;
    let mut total = 0.0;
    for r in rows {
        if r.expiry_ms <= now_ms {
            continue;
        }
        let dt_ms = (r.expiry_ms - now_ms) as f64;
        let years_ms = 365.0 * 24.0 * 3600.0 * 1000.0;
        let mut years = dt_ms / years_ms;
        if years < floor_years {
            years = floor_years;
        }
        let iv = r.mark_iv / 100.0;
        let g = bs_gamma(spot, r.strike, iv, years);
        let sign = if r.is_call { 1.0 } else { -1.0 };
        total += sign * g * r.open_interest * spot * spot * 0.01;
    }
    total
}

/// Zero-gamma level: closest sign change of net_gex to spot.
pub fn zero_gamma(rows: &[OptionRow], spot: f64, now_ms: i64) -> Option<f64> {
    if spot.is_nan() || spot <= 0.0 {
        return None;
    }
    let mut prev_s = 0.0;
    let mut prev_g = 0.0;
    let mut crossings: Vec<f64> = Vec::new();
    for k in 0..=120 {
        let s = spot * (0.70 + 0.005 * k as f64);
        let g = net_gex(rows, s, now_ms);
        if k > 0 {
            let changed = (prev_g > 0.0 && g < 0.0)
                || (prev_g < 0.0 && g > 0.0)
                || (prev_g == 0.0 && g != 0.0);
            if changed && prev_g != g {
                let denom = g - prev_g;
                if denom != 0.0 {
                    let x = prev_s + (0.0 - prev_g) / denom * (s - prev_s);
                    crossings.push(x);
                }
            }
        }
        prev_s = s;
        prev_g = g;
    }
    if crossings.is_empty() {
        return None;
    }
    let mut best = crossings[0];
    let mut best_d = (best - spot).abs();
    for &x in &crossings[1..] {
        let d = (x - spot).abs();
        if d < best_d {
            best_d = d;
            best = x;
        }
    }
    Some(best)
}

/// Max pain: strike minimising total intrinsic paid to holders.
pub fn max_pain(rows: &[OptionRow]) -> Option<f64> {
    if rows.is_empty() {
        return None;
    }
    let mut strikes: Vec<f64> = rows.iter().map(|r| r.strike).collect();
    strikes.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    strikes.dedup_by(|a, b| (*a - *b).abs() < f64::EPSILON);

    let mut best: Option<f64> = None;
    let mut best_val = f64::INFINITY;
    for &k in &strikes {
        let mut val = 0.0;
        for r in rows {
            if r.is_call {
                if k > r.strike {
                    val += r.open_interest * (k - r.strike);
                }
            } else if r.strike > k {
                val += r.open_interest * (r.strike - k);
            }
        }
        if val < best_val {
            best_val = val;
            best = Some(k);
        }
    }
    best
}

/// Top n strikes by summed OI for calls or puts.
pub fn oi_walls(rows: &[OptionRow], is_call: bool, n: usize) -> Vec<(f64, f64)> {
    use std::collections::BTreeMap;
    let mut by_strike: BTreeMap<u64, (f64, f64)> = BTreeMap::new();
    for r in rows {
        if r.is_call != is_call {
            continue;
        }
        let key = (r.strike * 1e6).round() as u64;
        let e = by_strike.entry(key).or_insert((r.strike, 0.0));
        e.1 += r.open_interest;
    }
    let mut v: Vec<(f64, f64)> = by_strike.values().cloned().collect();
    v.sort_by(|a, b| {
        b.1.partial_cmp(&a.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal))
    });
    v.truncate(n);
    v
}

/// Put/call open-interest ratio; None when call OI is 0.
pub fn put_call_ratio(rows: &[OptionRow]) -> Option<f64> {
    let mut call = 0.0;
    let mut put = 0.0;
    for r in rows {
        if r.is_call {
            call += r.open_interest;
        } else {
            put += r.open_interest;
        }
    }
    if call == 0.0 {
        None
    } else {
        Some(put / call)
    }
}

/// Distinct expiries > now_ms, ascending.
pub fn expiries(rows: &[OptionRow], now_ms: i64) -> Vec<i64> {
    let mut v: Vec<i64> = rows
        .iter()
        .map(|r| r.expiry_ms)
        .filter(|&e| e > now_ms)
        .collect();
    v.sort();
    v.dedup();
    v
}

fn expiry_label(expiry_ms: i64) -> String {
    // Convert epoch ms (UTC, at 08:00) to civil date.
    let days = expiry_ms.div_euclid(86_400_000);
    // days_from_civil inverse (civil_from_days).
    let z = days + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = if m <= 2 { y + 1 } else { y };
    let mon = MONTHS[(m - 1) as usize];
    let yy = year % 100;
    format!("{}{}{:02}", d, mon, yy)
}

fn oi_totals(rows: &[OptionRow]) -> (f64, f64) {
    let mut call = 0.0;
    let mut put = 0.0;
    for r in rows {
        if r.is_call {
            call += r.open_interest;
        } else {
            put += r.open_interest;
        }
    }
    (call, put)
}

fn walls_json(w: &[(f64, f64)]) -> Value {
    let arr: Vec<Value> = w.iter().map(|&(k, o)| json!([k, o])).collect();
    Value::Array(arr)
}

fn opt_num(v: Option<f64>) -> Value {
    match v {
        Some(x) => json!(x),
        None => Value::Null,
    }
}

fn opt_pair(v: Option<(f64, f64)>) -> Value {
    match v {
        Some((a, b)) => json!([a, b]),
        None => Value::Null,
    }
}

pub fn report(rows: &[OptionRow], spot: f64, now_ms: i64, max_expiries: usize) -> Value {
    let live: Vec<OptionRow> = rows
        .iter()
        .filter(|r| r.expiry_ms > now_ms)
        .cloned()
        .collect();

    let (all_call, all_put) = oi_totals(&live);
    let all_max_pain = max_pain(&live);
    let all_pcr = put_call_ratio(&live);
    let all_gex = net_gex(&live, spot, now_ms);
    let all_zg = zero_gamma(&live, spot, now_ms);
    let call_walls = oi_walls(&live, true, 3);
    let put_walls = oi_walls(&live, false, 3);

    let all = json!({
        "max_pain": opt_num(all_max_pain),
        "pcr": opt_num(all_pcr),
        "net_gex": all_gex,
        "zero_gamma": opt_num(all_zg),
        "call_walls": walls_json(&call_walls),
        "put_walls": walls_json(&put_walls),
        "call_oi": all_call,
        "put_oi": all_put,
    });

    let exp_list = expiries(&live, now_ms);
    let mut exp_json: Vec<Value> = Vec::new();
    for &e in exp_list.iter().take(max_expiries) {
        let erows: Vec<OptionRow> = live.iter().filter(|r| r.expiry_ms == e).cloned().collect();
        let (c, p) = oi_totals(&erows);
        let mp = max_pain(&erows);
        let pcr = put_call_ratio(&erows);
        let gex = net_gex(&erows, spot, now_ms);
        let cw = oi_walls(&erows, true, 1).into_iter().next();
        let pw = oi_walls(&erows, false, 1).into_iter().next();
        let days = (e - now_ms) as f64 / 86_400_000.0;
        exp_json.push(json!({
            "expiry_ms": e,
            "label": expiry_label(e),
            "days": days,
            "max_pain": opt_num(mp),
            "pcr": opt_num(pcr),
            "net_gex": gex,
            "call_wall": opt_pair(cw),
            "put_wall": opt_pair(pw),
            "call_oi": c,
            "put_oi": p,
        }));
    }

    json!({
        "spot": spot,
        "now_ms": now_ms,
        "all": all,
        "expiries": exp_json,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_instrument() {
        assert_eq!(
            parse_instrument("BTC-20NOV26-64000-P"),
            Some((1795161600000, 64000.0, false))
        );
        let r = parse_instrument("BTC-7NOV26-60000-C");
        assert!(r.is_some());
        let (ms, strike, is_call) = r.unwrap();
        assert_eq!(strike, 60000.0);
        assert!(is_call);
        // 2026-11-07 08:00 UTC = day diff from 2026-11-20 is 13 days earlier
        assert_eq!(ms, 1795161600000 - 13 * 86_400_000);
        assert!(parse_instrument("BTC-PERPETUAL").is_none());
        assert!(parse_instrument("BTC-20XYZ26-1-C").is_none());
        assert_eq!(expiry_label(1795161600000), "20NOV26");
        assert_eq!(expiry_label(1795161600000 - 13 * 86_400_000), "7NOV26");
    }

    #[test]
    fn test_bs_gamma_atm() {
        let g = bs_gamma(100.0, 100.0, 0.5, 1.0);
        let d1 = 0.25;
        let expected = norm_pdf(d1) / (100.0 * 0.5);
        assert!(
            (g - expected).abs() < 1e-12,
            "g={} expected={}",
            g,
            expected
        );
    }

    #[test]
    fn test_max_pain() {
        let rows = vec![
            OptionRow {
                expiry_ms: 1000,
                strike: 100.0,
                is_call: true,
                open_interest: 100.0,
                mark_iv: 50.0,
            },
            OptionRow {
                expiry_ms: 1000,
                strike: 120.0,
                is_call: false,
                open_interest: 100.0,
                mark_iv: 50.0,
            },
        ];
        let mp = max_pain(&rows).unwrap();
        // Brute force over grid to check answer is the minimiser.
        let mut best_k = 0.0;
        let mut best_v = f64::INFINITY;
        for k in 90..=130 {
            let k = k as f64;
            let v = 100.0 * (k - 100.0).max(0.0) + 100.0 * (120.0 - k).max(0.0);
            if v < best_v {
                best_v = v;
                best_k = k;
            }
        }
        // Pain is flat between 100 and 120; first strike achieving minimum is 100.
        let v_mp = 100.0 * (mp - 100.0).max(0.0) + 100.0 * (120.0 - mp).max(0.0);
        assert!((v_mp - best_v).abs() < 1e-9, "mp={} best={}", mp, best_k);
        assert!((100.0..=120.0).contains(&mp));
    }

    #[test]
    fn test_oi_walls_and_pcr() {
        let rows = vec![
            OptionRow {
                expiry_ms: 1000,
                strike: 100.0,
                is_call: true,
                open_interest: 5.0,
                mark_iv: 50.0,
            },
            OptionRow {
                expiry_ms: 1000,
                strike: 100.0,
                is_call: true,
                open_interest: 10.0,
                mark_iv: 50.0,
            },
            OptionRow {
                expiry_ms: 1000,
                strike: 110.0,
                is_call: true,
                open_interest: 20.0,
                mark_iv: 50.0,
            },
            OptionRow {
                expiry_ms: 1000,
                strike: 90.0,
                is_call: false,
                open_interest: 30.0,
                mark_iv: 50.0,
            },
        ];
        let cw = oi_walls(&rows, true, 3);
        assert_eq!(cw.len(), 2);
        assert_eq!(cw[0], (110.0, 20.0));
        assert_eq!(cw[1], (100.0, 15.0));
        let pw = oi_walls(&rows, false, 3);
        assert_eq!(pw, vec![(90.0, 30.0)]);

        let pcr = put_call_ratio(&rows).unwrap();
        assert!((pcr - 30.0 / 35.0).abs() < 1e-12);
    }

    #[test]
    fn test_zero_gamma() {
        let now_ms = 0;
        let day_ms = 86_400_000i64;
        let rows = vec![
            OptionRow {
                expiry_ms: 30 * day_ms,
                strike: 90.0,
                is_call: false,
                open_interest: 10.0,
                mark_iv: 50.0,
            },
            OptionRow {
                expiry_ms: 30 * day_ms,
                strike: 110.0,
                is_call: true,
                open_interest: 10.0,
                mark_iv: 50.0,
            },
        ];
        let zg = zero_gamma(&rows, 100.0, now_ms);
        assert!(zg.is_some());
        let x = zg.unwrap();
        assert!(x > 90.0 && x < 110.0, "x={}", x);
    }

    #[test]
    fn test_report_keys() {
        let rows = vec![
            OptionRow {
                expiry_ms: 30 * 86_400_000,
                strike: 100.0,
                is_call: true,
                open_interest: 10.0,
                mark_iv: 50.0,
            },
            OptionRow {
                expiry_ms: 30 * 86_400_000,
                strike: 90.0,
                is_call: false,
                open_interest: 5.0,
                mark_iv: 50.0,
            },
        ];
        let v = report(&rows, 100.0, 0, 5);
        assert!(v.get("spot").is_some());
        assert!(v.get("now_ms").is_some());
        assert!(v.get("all").is_some());
        assert!(v.get("expiries").is_some());
        let all = v.get("all").unwrap();
        assert!(all.get("max_pain").is_some());
        assert!(all.get("pcr").is_some());
        assert!(all.get("net_gex").is_some());
        assert!(all.get("zero_gamma").is_some());
        assert!(all.get("call_walls").is_some());
        assert!(all.get("put_walls").is_some());
        assert!(all.get("call_oi").is_some());
        assert!(all.get("put_oi").is_some());
        let exps = v.get("expiries").unwrap().as_array().unwrap();
        assert!(!exps.is_empty());
        let e0 = &exps[0];
        assert!(e0.get("expiry_ms").is_some());
        assert!(e0.get("label").is_some());
        assert!(e0.get("days").is_some());
        assert!(e0.get("max_pain").is_some());
        assert!(e0.get("pcr").is_some());
        assert!(e0.get("net_gex").is_some());
        assert!(e0.get("call_wall").is_some());
        assert!(e0.get("put_wall").is_some());
        assert!(e0.get("call_oi").is_some());
        assert!(e0.get("put_oi").is_some());
    }
}
