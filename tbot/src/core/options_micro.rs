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

/// Standard normal CDF via erf (Abramowitz-Stegun 7.1.26).
pub fn norm_cdf(x: f64) -> f64 {
    if x.is_nan() {
        return f64::NAN;
    }
    let sign = if x < 0.0 { -1.0 } else { 1.0 };
    // N(x) = (1 + erf(x / sqrt 2)) / 2
    let ax = x.abs() / std::f64::consts::SQRT_2;
    let p = 0.3275911;
    let a1 = 0.254829592;
    let a2 = -0.284496736;
    let a3 = 1.421413741;
    let a4 = -1.453152027;
    let a5 = 1.061405429;
    let t = 1.0 / (1.0 + p * ax);
    let poly = (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t;
    let erf = 1.0 - poly * (-ax * ax).exp();
    0.5 * (1.0 + sign * erf)
}

/// Black-Scholes delta (r = 0) of one option on one unit of underlying.
pub fn bs_delta(spot: f64, strike: f64, iv: f64, years: f64, is_call: bool) -> f64 {
    if [spot, strike, iv, years]
        .iter()
        .any(|x| x.is_nan() || *x <= 0.0)
    {
        return 0.0;
    }
    let sqrt_t = years.sqrt();
    let d1 = ((spot / strike).ln() + 0.5 * iv * iv * years) / (iv * sqrt_t);
    if is_call {
        norm_cdf(d1)
    } else {
        norm_cdf(d1) - 1.0
    }
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

/// 25-delta skew (put IV - call IV, in vol points) for ONE expiry.
/// Caller filters rows to a single expiry.
pub fn skew_25d(rows: &[OptionRow], spot: f64, now_ms: i64) -> Option<f64> {
    if spot.is_nan() || spot <= 0.0 {
        return None;
    }
    let floor_years = 1.0 / 8760.0;
    let years_ms = 365.0 * 24.0 * 3600.0 * 1000.0;
    let mut best_put: Option<(f64, f64)> = None; // (distance to -0.25, mark_iv)
    let mut best_call: Option<(f64, f64)> = None; // (distance to +0.25, mark_iv)
    for r in rows {
        if r.expiry_ms <= now_ms {
            continue;
        }
        let mut years = (r.expiry_ms - now_ms) as f64 / years_ms;
        if years < floor_years {
            years = floor_years;
        }
        let iv = r.mark_iv / 100.0;
        let d = bs_delta(spot, r.strike, iv, years, r.is_call);
        if r.is_call {
            let dist = (d - 0.25).abs();
            if best_call.is_none_or(|(bd, _)| dist < bd) {
                best_call = Some((dist, r.mark_iv));
            }
        } else {
            let dist = (d + 0.25).abs();
            if best_put.is_none_or(|(bd, _)| dist < bd) {
                best_put = Some((dist, r.mark_iv));
            }
        }
    }
    match (best_put, best_call) {
        (Some((_, p)), Some((_, c))) => Some(p - c),
        _ => None,
    }
}

/// (support, resistance): strike with largest summed put OI strictly below spot
/// and strike with largest summed call OI strictly above spot.
pub fn walls_around(rows: &[OptionRow], spot: f64) -> (Option<f64>, Option<f64>) {
    use std::collections::BTreeMap;
    if spot.is_nan() || spot <= 0.0 {
        return (None, None);
    }
    let mut put_map: BTreeMap<u64, (f64, f64)> = BTreeMap::new();
    let mut call_map: BTreeMap<u64, (f64, f64)> = BTreeMap::new();
    for r in rows {
        let key = (r.strike * 1e6).round() as u64;
        if r.is_call {
            if r.strike > spot {
                let e = call_map.entry(key).or_insert((r.strike, 0.0));
                e.1 += r.open_interest;
            }
        } else if r.strike < spot {
            let e = put_map.entry(key).or_insert((r.strike, 0.0));
            e.1 += r.open_interest;
        }
    }
    let pick = |m: &BTreeMap<u64, (f64, f64)>| -> Option<f64> {
        let mut best: Option<(f64, f64)> = None;
        for &(k, oi) in m.values() {
            if best.is_none_or(|(bo, bk)| oi > bo || (oi == bo && k < bk)) {
                best = Some((oi, k));
            }
        }
        best.map(|(_, k)| k)
    };
    (pick(&put_map), pick(&call_map))
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

/// Format a number with thousands separators: 64000 -> "64,000".
fn fmt_thousands(x: f64) -> String {
    if !x.is_finite() {
        return "0".to_string();
    }
    let neg = x < 0.0;
    let units = x.abs().round() as u64;
    let s = units.to_string();
    let bytes = s.as_bytes();
    let mut out = String::new();
    if neg {
        out.push('-');
    }
    let n = bytes.len();
    for (i, b) in bytes.iter().enumerate() {
        if i > 0 && (n - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(*b as char);
    }
    out
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
    let (all_support, all_resistance) = walls_around(&live, spot);

    let exp_list = expiries(&live, now_ms);

    // First expiry with days >= 7 drives the "all" skew.
    let mut all_skew: Option<f64> = None;
    let mut all_skew_label: Option<String> = None;
    for &e in exp_list.iter() {
        let days = (e - now_ms) as f64 / 86_400_000.0;
        if days >= 7.0 {
            let erows: Vec<OptionRow> = live.iter().filter(|r| r.expiry_ms == e).cloned().collect();
            all_skew = skew_25d(&erows, spot, now_ms);
            all_skew_label = Some(expiry_label(e));
            break;
        }
    }

    let all = json!({
        "max_pain": opt_num(all_max_pain),
        "pcr": opt_num(all_pcr),
        "net_gex": all_gex,
        "zero_gamma": opt_num(all_zg),
        "call_walls": walls_json(&call_walls),
        "put_walls": walls_json(&put_walls),
        "call_oi": all_call,
        "put_oi": all_put,
        "support": opt_num(all_support),
        "resistance": opt_num(all_resistance),
        "skew_25d": opt_num(all_skew),
        "skew_label": match &all_skew_label {
            Some(s) => json!(s),
            None => Value::Null,
        },
    });

    let mut exp_json: Vec<Value> = Vec::new();
    for &e in exp_list.iter().take(max_expiries) {
        let erows: Vec<OptionRow> = live.iter().filter(|r| r.expiry_ms == e).cloned().collect();
        let (c, p) = oi_totals(&erows);
        let mp = max_pain(&erows);
        let pcr = put_call_ratio(&erows);
        let gex = net_gex(&erows, spot, now_ms);
        let cw = oi_walls(&erows, true, 1).into_iter().next();
        let pw = oi_walls(&erows, false, 1).into_iter().next();
        let sk = skew_25d(&erows, spot, now_ms);
        let (sup, res) = walls_around(&erows, spot);
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
            "skew_25d": opt_num(sk),
            "support": opt_num(sup),
            "resistance": opt_num(res),
        }));
    }

    json!({
        "spot": spot,
        "now_ms": now_ms,
        "all": all,
        "expiries": exp_json,
    })
}

fn get_f64(v: &Value, key: &str) -> Option<f64> {
    v.get(key).and_then(|x| x.as_f64())
}

/// Rule-based interpretation of a report for the "all" section.
pub fn interpret(report: &Value) -> Value {
    let spot = get_f64(report, "spot");
    let dvol = get_f64(report, "dvol");
    let dvol_chg = get_f64(report, "dvol_change_24h");
    let all = report.get("all").cloned().unwrap_or(Value::Null);

    let mp = get_f64(&all, "max_pain");
    let flip = get_f64(&all, "zero_gamma");
    let gex = get_f64(&all, "net_gex");
    let pcr = get_f64(&all, "pcr");
    let skew = get_f64(&all, "skew_25d");
    let call_wall = get_f64(&all, "resistance");
    let put_wall = get_f64(&all, "support");
    let days: Option<f64> = None; // "all" has no single expiry
    let funding: Option<f64> = None; // web only input; not available here

    // Per-metric notes.
    let note_max_pain: Option<String> = match (mp, spot) {
        (Some(m), Some(s)) if s > 0.0 => {
            let d = (m - s) / s;
            if d.abs() < 0.01 {
                Some("지수와 거의 같음 · 만기까지 이 근처에 머물려는 힘".to_string())
            } else {
                let dir = if d > 0.0 { "위" } else { "아래" };
                let tail = match days {
                    Some(dd) if dd <= 7.0 => "만기가 가까워 그쪽으로 끌릴 수 있음",
                    _ => "만기가 멀어 끌림은 약함",
                };
                Some(format!(
                    "지수보다 {:.1}% {} · {}",
                    d.abs() * 100.0,
                    dir,
                    tail
                ))
            }
        }
        _ => None,
    };

    let note_flip: Option<String> = match (flip, spot) {
        (None, _) => Some("범위 안에 없음 · 딜러 감마가 한쪽으로 치우침".to_string()),
        (Some(f), Some(s)) if f > 0.0 => {
            let dist = (s - f).abs() / f * 100.0;
            if s >= f {
                Some(format!(
                    "지수가 {:.1}% 위 · 딜러가 반대로 헤지해 움직임이 줄어드는 구간",
                    dist
                ))
            } else {
                Some(format!(
                    "지수가 {:.1}% 아래 · 딜러가 같은 방향으로 헤지해 움직임이 커지는 구간",
                    dist
                ))
            }
        }
        _ => None,
    };

    let note_gex: Option<String> = match (gex, spot) {
        (Some(g), Some(s)) if s > 0.0 => {
            let btc = g.abs() / s;
            if g < 0.0 {
                Some(format!(
                    "1% 움직이면 딜러가 약 {} BTC를 같은 방향으로 거래 (변동성 증폭)",
                    fmt_thousands(btc)
                ))
            } else {
                Some(format!(
                    "1% 움직이면 딜러가 약 {} BTC를 반대 방향으로 거래 (변동성 완충)",
                    fmt_thousands(btc)
                ))
            }
        }
        _ => None,
    };

    let note_pcr: Option<String> = match pcr {
        Some(p) => {
            let s = if p < 0.7 {
                "콜이 훨씬 많음 · 상승 기대 쏠림"
            } else if p < 1.0 {
                "균형에 가까움 (콜 약간 우세)"
            } else if p <= 1.3 {
                "풋이 많음 · 하락 헤지 수요"
            } else {
                "풋 쏠림이 큼 · 과도한 비관 (반대 신호가 되기도 함)"
            };
            Some(s.to_string())
        }
        None => None,
    };

    let note_dvol: Option<String> = match dvol {
        Some(d) => {
            let level = if d < 40.0 {
                "낮음"
            } else if d <= 60.0 {
                "보통"
            } else {
                "높음"
            };
            let change = match dvol_chg {
                Some(c) if c >= 3.0 => " · 24시간 급등, 불안 확대",
                Some(c) if c <= -3.0 => " · 24시간 하락, 안정",
                _ => " · 24시간 변화 작음",
            };
            Some(format!("변동성 {}{}", level, change))
        }
        None => None,
    };

    let note_skew: Option<String> = match skew {
        Some(s) => {
            let t = if s > 5.0 {
                "풋이 비쌈 · 하락 대비 수요 강함"
            } else if s >= 0.0 {
                "풋이 약간 비쌈 · 평소 수준"
            } else {
                "콜이 비쌈 · 상승 베팅 우세"
            };
            Some(t.to_string())
        }
        None => None,
    };

    // Each indicator votes -1 / 0 / +1 on one axis, like a technical-rating gauge.
    // dir: -1 하방, 0 중립, +1 상방. vol: -1 축소, 0 보통, +1 확대.
    let mut signals: Vec<Value> = Vec::new();
    let mut push = |key: &str,
                    label: &str,
                    axis: &str,
                    score: i64,
                    reason: Option<&str>,
                    note: Option<String>| {
        signals.push(json!({
            "key": key,
            "label": label,
            "axis": axis,
            "score": score,
            "reason": reason,
            "note": note,
        }));
    };

    // Direction: max pain pulls only near expiry.
    if let (Some(m), Some(s)) = (mp, spot) {
        if s > 0.0 {
            let d = (m - s) / s;
            let near = matches!(days, Some(dd) if dd <= 7.0);
            let (score, reason) = if near && d.abs() >= 0.01 {
                if d > 0.0 {
                    (1, Some("맥스페인 위 끌림"))
                } else {
                    (-1, Some("맥스페인 아래 끌림"))
                }
            } else {
                (0, None)
            };
            push(
                "max_pain",
                "맥스페인",
                "dir",
                score,
                reason,
                note_max_pain.clone(),
            );
        }
    }
    if let Some(p) = pcr {
        let (score, reason) = if p < 0.7 {
            (1, Some("콜 쏠림"))
        } else if p > 1.3 {
            (1, Some("풋 과열 · 역발상"))
        } else if p > 1.0 {
            (-1, Some("풋 헤지 많음"))
        } else {
            (0, None)
        };
        push("pcr", "풋/콜", "dir", score, reason, note_pcr.clone());
    }
    if let Some(s) = skew {
        let (score, reason) = if s > 5.0 {
            (-1, Some("하락 대비 수요"))
        } else if s < 0.0 {
            (1, Some("상승 베팅 우세"))
        } else {
            (0, None)
        };
        push("skew", "25Δ 스큐", "dir", score, reason, note_skew.clone());
    }
    if let Some(f) = funding {
        let (score, reason, note) = if f > 0.0005 {
            (-1, Some("롱 과열"), "롱 과열")
        } else if f < 0.0 {
            (-1, Some("숏 우세"), "숏 우세")
        } else {
            (0, None, "보통")
        };
        push(
            "funding",
            "펀딩",
            "dir",
            score,
            reason,
            Some(note.to_string()),
        );
    }
    if let (Some(cw), Some(pw), Some(s)) = (call_wall, put_wall, spot) {
        if s > 0.0 {
            let up = (cw - s) / s;
            let down = (s - pw) / s;
            let (score, reason, note) = if down < up / 2.0 {
                (
                    1,
                    Some("지지선 근처"),
                    "지지선(풋 벽)이 더 가까움 · 아래가 받쳐짐",
                )
            } else if up < down / 2.0 {
                (
                    -1,
                    Some("저항선 근처"),
                    "저항선(콜 벽)이 더 가까움 · 위가 막힘",
                )
            } else {
                (0, None, "지지와 저항 사이 가운데쯤")
            };
            push(
                "walls",
                "지지·저항",
                "dir",
                score,
                reason,
                Some(note.to_string()),
            );
        }
    }

    // Volatility.
    if let Some(s) = spot {
        let (score, reason) = match flip {
            Some(f) if s < f => (1, Some("숏감마 구간")),
            Some(_) => (-1, Some("롱감마 구간")),
            None => (0, None),
        };
        push("flip", "감마 플립", "vol", score, reason, note_flip.clone());
    }
    if let Some(g) = gex {
        let (score, reason) = if g < 0.0 {
            (1, Some("딜러 숏감마"))
        } else if g > 0.0 {
            (-1, Some("딜러 롱감마"))
        } else {
            (0, None)
        };
        push("gex", "딜러 감마", "vol", score, reason, note_gex.clone());
    }
    if let Some(d) = dvol {
        let (score, reason) = match dvol_chg {
            Some(c) if c >= 3.0 => (1, Some("DVOL 급등")),
            Some(c) if c <= -3.0 => (-1, Some("DVOL 하락")),
            _ if d > 60.0 => (1, Some("DVOL 높음")),
            _ if d < 40.0 => (-1, Some("DVOL 낮음")),
            _ => (0, None),
        };
        push("dvol", "DVOL", "vol", score, reason, note_dvol.clone());
    }

    let tally = |axis: &str, labels: [&str; 5]| -> Value {
        let scores: Vec<i64> = signals
            .iter()
            .filter(|s| s["axis"] == axis)
            .map(|s| s["score"].as_i64().unwrap_or(0))
            .collect();
        let n = scores.len();
        let net: i64 = scores.iter().sum();
        let avg = if n == 0 { 0.0 } else { net as f64 / n as f64 };
        json!({
            "score": avg,
            "net": net,
            "label": rating_label(avg, labels),
            "down": scores.iter().filter(|&&x| x < 0).count(),
            "neutral": scores.iter().filter(|&&x| x == 0).count(),
            "up": scores.iter().filter(|&&x| x > 0).count(),
        })
    };
    let direction = tally("dir", ["강한 하방", "하방", "중립", "상방", "강한 상방"]);
    let volatility = tally("vol", ["크게 축소", "축소", "보통", "확대", "크게 확대"]);
    let reasons: Vec<String> = signals
        .iter()
        .filter_map(|s| s["reason"].as_str().map(str::to_string))
        .collect();
    let verdict_bias = direction["label"].as_str().unwrap_or("중립").to_string();
    let verdict_vol = format!("변동성 {}", volatility["label"].as_str().unwrap_or("보통"));

    let range_line: Option<String> = match (put_wall, call_wall, spot) {
        (Some(pw), Some(cw), Some(s)) => Some(format!(
            "지지 ${} · 저항 ${} (지수 ${})",
            fmt_thousands(pw),
            fmt_thousands(cw),
            fmt_thousands(s)
        )),
        _ => None,
    };

    let mut summary = format!("종합: {} · {}.", verdict_bias, verdict_vol);
    if !reasons.is_empty() {
        summary.push_str(" 근거: ");
        summary.push_str(&reasons.join(", "));
        summary.push('.');
    }
    summary.push(' ');
    summary.push_str("규칙 기반 해석이며 매매 신호가 아닙니다.");

    json!({
        "notes": {
            "max_pain": note_max_pain,
            "flip": note_flip,
            "gex": note_gex,
            "pcr": note_pcr,
            "dvol": note_dvol,
            "skew": note_skew,
        },
        "signals": signals,
        "direction": direction,
        "volatility": volatility,
        "bias": direction["net"],
        "vol": volatility["net"],
        "reasons": reasons,
        "verdict_bias": verdict_bias,
        "verdict_vol": verdict_vol,
        "range": range_line,
        "summary": summary,
    })
}

/// Five-step label for an average vote in [-1, 1] (like a technical-rating gauge).
pub fn rating_label(avg: f64, labels: [&str; 5]) -> &str {
    if avg >= 0.6 {
        labels[4]
    } else if avg >= 0.3 {
        labels[3]
    } else if avg > -0.3 {
        labels[2]
    } else if avg > -0.6 {
        labels[1]
    } else {
        labels[0]
    }
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

    #[test]
    fn test_norm_cdf() {
        assert!((norm_cdf(0.0) - 0.5).abs() < 1e-7); // A&S 7.1.26 is good to ~1.5e-7
        assert!((norm_cdf(1.96) - 0.975).abs() < 1e-4);
    }

    #[test]
    fn test_bs_delta_atm_call() {
        let d = bs_delta(100.0, 100.0, 0.5, 1.0, true);
        let expected = norm_cdf(0.25);
        assert!((d - expected).abs() < 1e-9, "d={} expected={}", d, expected);
    }

    #[test]
    fn test_skew_25d_positive() {
        let now_ms = 0;
        let day_ms = 86_400_000i64;
        let exp = 30 * day_ms;
        // Puts priced with higher IV than calls -> positive skew.
        let rows = vec![
            OptionRow {
                expiry_ms: exp,
                strike: 90.0,
                is_call: false,
                open_interest: 10.0,
                mark_iv: 70.0,
            },
            OptionRow {
                expiry_ms: exp,
                strike: 85.0,
                is_call: false,
                open_interest: 10.0,
                mark_iv: 68.0,
            },
            OptionRow {
                expiry_ms: exp,
                strike: 110.0,
                is_call: true,
                open_interest: 10.0,
                mark_iv: 55.0,
            },
            OptionRow {
                expiry_ms: exp,
                strike: 115.0,
                is_call: true,
                open_interest: 10.0,
                mark_iv: 54.0,
            },
        ];
        let s = skew_25d(&rows, 100.0, now_ms).unwrap();
        assert!(s > 0.0, "skew={}", s);
    }

    #[test]
    fn test_walls_around() {
        let rows = vec![
            OptionRow {
                expiry_ms: 1000,
                strike: 90.0,
                is_call: false,
                open_interest: 40.0,
                mark_iv: 50.0,
            },
            OptionRow {
                expiry_ms: 1000,
                strike: 95.0,
                is_call: false,
                open_interest: 10.0,
                mark_iv: 50.0,
            },
            OptionRow {
                expiry_ms: 1000,
                strike: 110.0,
                is_call: true,
                open_interest: 30.0,
                mark_iv: 50.0,
            },
            OptionRow {
                expiry_ms: 1000,
                strike: 120.0,
                is_call: true,
                open_interest: 5.0,
                mark_iv: 50.0,
            },
            // Strikes at/above spot for puts and at/below for calls must be ignored.
            OptionRow {
                expiry_ms: 1000,
                strike: 100.0,
                is_call: false,
                open_interest: 999.0,
                mark_iv: 50.0,
            },
            OptionRow {
                expiry_ms: 1000,
                strike: 100.0,
                is_call: true,
                open_interest: 999.0,
                mark_iv: 50.0,
            },
        ];
        let (sup, res) = walls_around(&rows, 100.0);
        assert_eq!(sup, Some(90.0));
        assert_eq!(res, Some(110.0));
    }

    #[test]
    fn test_interpret_rules() {
        // S below flip, pcr 1.2, skew 6, support close below S,
        // resistance far above S -> vol expansion, bearish bias, neutral verdict.
        let rep = json!({
            "spot": 64000.0,
            "dvol": 55.0,
            "dvol_change_24h": 0.5,
            "all": {
                "max_pain": 70000.0,
                "zero_gamma": 66000.0,
                "net_gex": -1000000.0,
                "pcr": 1.2,
                "skew_25d": 6.0,
                "support": 63000.0,
                "resistance": 80000.0,
            }
        });
        let out = interpret(&rep);
        assert_eq!(out["bias"].as_i64().unwrap(), -1);
        assert_eq!(out["verdict_bias"].as_str().unwrap(), "중립");
        // flip +1, gex +1, dvol 0 -> avg 0.67
        assert_eq!(out["verdict_vol"].as_str().unwrap(), "변동성 크게 확대");
        // dir votes: max pain 0 (no expiry), pcr -1, skew -1, walls +1
        assert_eq!(out["direction"]["down"].as_u64().unwrap(), 2);
        assert_eq!(out["direction"]["neutral"].as_u64().unwrap(), 1);
        assert_eq!(out["direction"]["up"].as_u64().unwrap(), 1);
        assert_eq!(out["volatility"]["up"].as_u64().unwrap(), 2);
        assert_eq!(out["signals"].as_array().unwrap().len(), 7);
        let rs: Vec<String> = out["reasons"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_string())
            .collect();
        assert!(rs.iter().any(|s| s == "숏감마 구간"), "reasons={:?}", rs);
        assert!(rs.iter().any(|s| s == "풋 헤지 많음"), "reasons={:?}", rs);
        assert!(rs.iter().any(|s| s == "하락 대비 수요"), "reasons={:?}", rs);
        assert!(rs.iter().any(|s| s == "지지선 근처"), "reasons={:?}", rs);
        let summary = out["summary"].as_str().unwrap();
        assert!(summary.contains("규칙 기반 해석이며 매매 신호가 아닙니다."));
    }

    #[test]
    fn test_rating_label_steps() {
        let l = ["강한 하방", "하방", "중립", "상방", "강한 상방"];
        assert_eq!(rating_label(1.0, l), "강한 상방");
        assert_eq!(rating_label(0.5, l), "상방");
        assert_eq!(rating_label(0.25, l), "중립");
        assert_eq!(rating_label(-0.25, l), "중립");
        assert_eq!(rating_label(-0.5, l), "하방");
        assert_eq!(rating_label(-0.75, l), "강한 하방");
    }
}
