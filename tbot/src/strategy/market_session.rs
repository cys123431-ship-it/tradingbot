//! US equity and regional primary-market regular-session helpers.

use chrono::{DateTime, Datelike, Duration, NaiveDate, TimeZone, Timelike, Utc, Weekday};
use chrono_tz::America::New_York;
use chrono_tz::Asia::{Hong_Kong, Seoul, Shanghai};
use chrono_tz::Tz;
use serde_json::{json, Value};

pub const US_EQUITY_REGULAR_OPEN: (u32, u32) = (9, 30);
pub const US_EQUITY_REGULAR_CLOSE: (u32, u32) = (16, 0);
pub const US_EQUITY_EARLY_CLOSE: (u32, u32) = (13, 0);

fn weekday_num(wd: Weekday) -> i64 {
    wd.num_days_from_monday() as i64
}

fn nth_weekday_of_month(year: i32, month: u32, weekday: i64, n: i64) -> NaiveDate {
    let first = NaiveDate::from_ymd_opt(year, month, 1).unwrap();
    let offset = (weekday - weekday_num(first.weekday())).rem_euclid(7);
    first + Duration::days(offset + (n - 1) * 7)
}

fn last_weekday_of_month(year: i32, month: u32, weekday: i64) -> NaiveDate {
    let next_month = if month == 12 {
        NaiveDate::from_ymd_opt(year + 1, 1, 1).unwrap()
    } else {
        NaiveDate::from_ymd_opt(year, month + 1, 1).unwrap()
    };
    let last = next_month - Duration::days(1);
    last - Duration::days((weekday_num(last.weekday()) - weekday).rem_euclid(7))
}

fn observed_fixed_us_holiday(year: i32, month: u32, day: u32) -> NaiveDate {
    let raw = NaiveDate::from_ymd_opt(year, month, day).unwrap();
    match raw.weekday() {
        Weekday::Sat => raw - Duration::days(1),
        Weekday::Sun => raw + Duration::days(1),
        _ => raw,
    }
}

fn western_easter_date(year: i32) -> NaiveDate {
    let a = year % 19;
    let b = year / 100;
    let c = year % 100;
    let d = b / 4;
    let e = b % 4;
    let f = (b + 8) / 25;
    let g = (b - f + 1) / 3;
    let h = (19 * a + b - d - g + 15) % 30;
    let i = c / 4;
    let k = c % 4;
    let l = (32 + 2 * e + 2 * i - h - k) % 7;
    let m = (a + 11 * h + 22 * l) / 451;
    let month = (h + l - 7 * m + 114) / 31;
    let day = ((h + l - 7 * m + 114) % 31) + 1;
    NaiveDate::from_ymd_opt(year, month as u32, day as u32).unwrap()
}

fn previous_weekday(day: NaiveDate) -> NaiveDate {
    let mut candidate = day - Duration::days(1);
    while candidate.weekday().num_days_from_monday() >= 5 {
        candidate -= Duration::days(1);
    }
    candidate
}

fn us_equity_market_closed_dates(year: i32) -> Vec<NaiveDate> {
    let candidates = vec![
        observed_fixed_us_holiday(year, 1, 1),
        nth_weekday_of_month(year, 1, 0, 3),
        nth_weekday_of_month(year, 2, 0, 3),
        western_easter_date(year) - Duration::days(2),
        last_weekday_of_month(year, 5, 0),
        observed_fixed_us_holiday(year, 6, 19),
        observed_fixed_us_holiday(year, 7, 4),
        nth_weekday_of_month(year, 9, 0, 1),
        nth_weekday_of_month(year, 11, 3, 4),
        observed_fixed_us_holiday(year, 12, 25),
        observed_fixed_us_holiday(year + 1, 1, 1),
    ];
    candidates
        .into_iter()
        .filter(|d| d.year() == year)
        .collect()
}

fn us_equity_market_early_close_dates(year: i32) -> Vec<NaiveDate> {
    let closed = us_equity_market_closed_dates(year);
    let thanksgiving = nth_weekday_of_month(year, 11, 3, 4);
    let mut candidates = vec![
        thanksgiving + Duration::days(1),
        previous_weekday(observed_fixed_us_holiday(year, 7, 4)),
        NaiveDate::from_ymd_opt(year, 12, 24).unwrap(),
    ];
    candidates.dedup();
    candidates
        .into_iter()
        .filter(|d| {
            d.year() == year && d.weekday().num_days_from_monday() < 5 && !closed.contains(d)
        })
        .collect()
}

fn py_isoformat(dt: DateTime<Tz>) -> String {
    // Python isoformat without microseconds if zero.
    if dt.nanosecond() == 0 {
        dt.format("%Y-%m-%dT%H:%M:%S%:z").to_string()
    } else {
        let micros = dt.nanosecond() / 1000;
        format!(
            "{}.{:06}{}",
            dt.format("%Y-%m-%dT%H:%M:%S"),
            micros,
            dt.format("%:z")
        )
    }
}

fn resolve_now(now: Option<DateTime<Utc>>) -> DateTime<Utc> {
    now.unwrap_or_else(Utc::now)
}

pub fn us_equity_regular_session_status(now: Option<DateTime<Utc>>) -> Value {
    let current = resolve_now(now);
    let local = current.with_timezone(&New_York);
    let local_date = local.date_naive();
    let closed_dates = us_equity_market_closed_dates(local_date.year());
    let early_close_dates = us_equity_market_early_close_dates(local_date.year());
    let (close_hour, close_minute) = if early_close_dates.contains(&local_date) {
        US_EQUITY_EARLY_CLOSE
    } else {
        US_EQUITY_REGULAR_CLOSE
    };

    let open_hour = US_EQUITY_REGULAR_OPEN.0;
    let open_minute = US_EQUITY_REGULAR_OPEN.1;
    let open_dt = New_York
        .with_ymd_and_hms(
            local_date.year(),
            local_date.month(),
            local_date.day(),
            open_hour,
            open_minute,
            0,
        )
        .single()
        .unwrap();
    let close_dt = New_York
        .with_ymd_and_hms(
            local_date.year(),
            local_date.month(),
            local_date.day(),
            close_hour,
            close_minute,
            0,
        )
        .single()
        .unwrap();

    let (is_open, reason) = if local.weekday().num_days_from_monday() >= 5 {
        (false, "weekend")
    } else if closed_dates.contains(&local_date) {
        (false, "holiday")
    } else if !(open_dt <= local && local < close_dt) {
        (false, "outside_regular_session")
    } else {
        (true, "regular_session_open")
    };

    json!({
        "open": is_open,
        "reason": reason,
        "timezone": "America/New_York",
        "local_time": py_isoformat(local),
        "regular_open": py_isoformat(open_dt),
        "regular_close": py_isoformat(close_dt),
        "early_close": early_close_dates.contains(&local_date),
    })
}

pub fn is_us_equity_regular_session_open(now: Option<DateTime<Utc>>) -> bool {
    us_equity_regular_session_status(now)
        .get("open")
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
}
/// One trading window: ((open hour, minute), (close hour, minute)) local time.
type SessionWindow = ((u32, u32), (u32, u32));

fn regional_equity_session_status(
    now: Option<DateTime<Utc>>,
    timezone_name: &str,
    tz: Tz,
    sessions: &[SessionWindow],
    market_region: &str,
    underlying_type: &str,
) -> Value {
    let current = resolve_now(now);
    let local = current.with_timezone(&tz);
    let local_date = local.date_naive();

    let windows: Vec<(DateTime<Tz>, DateTime<Tz>)> = sessions
        .iter()
        .map(|((oh, om), (ch, cm))| {
            let open_dt = tz
                .with_ymd_and_hms(
                    local_date.year(),
                    local_date.month(),
                    local_date.day(),
                    *oh,
                    *om,
                    0,
                )
                .single()
                .unwrap();
            let close_dt = tz
                .with_ymd_and_hms(
                    local_date.year(),
                    local_date.month(),
                    local_date.day(),
                    *ch,
                    *cm,
                    0,
                )
                .single()
                .unwrap();
            (open_dt, close_dt)
        })
        .collect();

    let (is_open, reason) = if local.weekday().num_days_from_monday() >= 5 {
        (false, "weekend")
    } else if windows
        .iter()
        .any(|(open_dt, close_dt)| *open_dt <= local && local < *close_dt)
    {
        (true, "regular_session_open")
    } else {
        (false, "outside_regular_session")
    };

    let first_open = windows.first().map(|(o, _)| *o);
    let last_close = windows.last().map(|(_, c)| *c);
    let sessions_json: Vec<Value> = windows
        .iter()
        .map(|(o, c)| json!({"open": py_isoformat(*o), "close": py_isoformat(*c)}))
        .collect();

    json!({
        "open": is_open,
        "reason": reason,
        "timezone": timezone_name,
        "local_time": py_isoformat(local),
        "regular_open": first_open.map(py_isoformat),
        "regular_close": last_close.map(py_isoformat),
        "regular_sessions": sessions_json,
        "early_close": false,
        "market_region": market_region,
        "underlying_type": underlying_type,
        "calendar_precision": "weekday_session_only",
        "local_date": local_date.format("%Y-%m-%d").to_string(),
    })
}

pub fn tradfi_primary_session_status(
    underlying_type: Option<&str>,
    now: Option<DateTime<Utc>>,
) -> Value {
    let kind_raw = underlying_type.unwrap_or("");
    let trimmed = kind_raw.trim().to_uppercase();
    let kind = if trimmed.is_empty() {
        "EQUITY".to_string()
    } else {
        trimmed
    };
    match kind.as_str() {
        "KR_EQUITY" => regional_equity_session_status(
            now,
            "Asia/Seoul",
            Seoul,
            &[((9, 0), (15, 30))],
            "KR",
            &kind,
        ),
        "HK_EQUITY" => regional_equity_session_status(
            now,
            "Asia/Hong_Kong",
            Hong_Kong,
            &[((9, 30), (12, 0)), ((13, 0), (16, 0))],
            "HK",
            &kind,
        ),
        "CN_EQUITY" => regional_equity_session_status(
            now,
            "Asia/Shanghai",
            Shanghai,
            &[((9, 30), (11, 30)), ((13, 0), (15, 0))],
            "CN",
            &kind,
        ),
        _ => {
            let mut status = us_equity_regular_session_status(now);
            if let Some(map) = status.as_object_mut() {
                map.insert("market_region".into(), Value::String("US".into()));
                map.insert("underlying_type".into(), Value::String(kind));
                map.insert(
                    "calendar_precision".into(),
                    Value::String("holiday_aware".into()),
                );
            }
            status
        }
    }
}
