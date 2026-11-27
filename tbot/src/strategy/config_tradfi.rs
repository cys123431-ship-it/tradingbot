//! Configuration for the TradFi-only SMA 1/5/120 alignment strategy (`/tradfi`).
//!
//! The coin is user-selectable but must be a Binance TradFi perpetual (gold,
//! oil, US equities/ETFs); crypto symbols are refused by the service and the
//! menu. Default XAUUSDT.
//!
//! Rules (closed candles of the chosen timeframe, 3m..1d, default 4h; SMA 1 = the close):
//! - Long entry: bullish alignment close > SMA5 > SMA120 with the close above
//!   the exit line. Entry mode "cross" = only on the bar where the alignment
//!   newly forms; "trend" = whenever it holds (after one bar of re-entry wait).
//! - Long exit: the close below the exit line SMA(n), n = 2..=120 chosen by the
//!   user (default 30). The cap is the SMA120 entry line: any market with
//!   enough candles to enter also has enough to draw the exit line, so a
//!   position can never be opened without its exit rule.
//! - Short is the mirror image.
//! - Leverage 5x and 50% of the available balance as margin are fixed.
//!
//! The order path, ledger, protection and reconciliation are the shared
//! SMA-cross service; this module only pins the settings.

use serde_json::{json, Map, Value};

use crate::strategy::config_btc_ma::normalize_btc_ma_cross_config;

pub const STRATEGY_NAME: &str = "tradfi_sma1_5_120_align";
pub const STRATEGY_VERSION: &str = "1.0.0";
pub const CLIENT_ID_STRATEGY: &str = "tfma";
pub const CONFIG_KEY: &str = "tradfi_ma";
pub const DEFAULT_SYMBOL: &str = "XAUUSDT";
/// Entry/exit candle; one is chosen in the menu.
pub const TIMEFRAMES: &[&str] = &[
    "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d",
];
pub const DEFAULT_TIMEFRAME: &str = "4h";
/// Default alignment periods: fast (1 = the close), slow ("SMA5") and long
/// ("SMA120"). All three are user-chosen since 2026-11-27 (`/tfma 1 5 120`).
pub const FAST_PERIOD: i64 = 1;
pub const MID_PERIOD: i64 = 5;
pub const SLOW_PERIOD: i64 = 120;
/// Longest alignment period: candle_history 260 keeps it computable.
pub const MA_PERIOD_MAX: i64 = 250;
/// User keys of the alignment periods (fast / slow / long).
pub const MA_KEYS: [&str; 3] = ["ma_fast_period", "ma_slow_period", "ma_long_period"];

/// fast < slow, and slow < long while the long MA is part of the alignment.
pub fn ma_periods_ok(fast: i64, slow: i64, long: i64, use_long: bool) -> bool {
    let range = |p: i64| (1..=MA_PERIOD_MAX).contains(&p);
    range(fast)
        && range(slow)
        && range(long)
        && long >= 2
        && fast < slow
        && (!use_long || slow < long)
}

/// "1 5 120" / "1/5/120" / "1,5" (two numbers keep `long`).
pub fn parse_ma_periods(text: &str, long: i64) -> Option<(i64, i64, i64)> {
    let nums: Vec<i64> = text
        .split(|c: char| !c.is_ascii_digit())
        .filter(|s| !s.is_empty())
        .map(|s| s.parse::<i64>().ok())
        .collect::<Option<Vec<_>>>()?;
    match nums.as_slice() {
        [f, s] => Some((*f, *s, long)),
        [f, s, l] => Some((*f, *s, *l)),
        _ => None,
    }
}

/// (fast, slow, long) from a config or switch, defaults when missing.
pub fn ma_periods_of(v: &Value) -> (i64, i64, i64) {
    let get = |k: &str, d: i64| i64_of(v.get(k)).unwrap_or(d);
    (
        get(MA_KEYS[0], FAST_PERIOD),
        get(MA_KEYS[1], MID_PERIOD),
        get(MA_KEYS[2], SLOW_PERIOD),
    )
}
/// Preset buttons in the menu; any period in EXIT_MA_MIN..=EXIT_MA_MAX is allowed.
pub const EXIT_MA_CHOICES: &[i64] = &[20, 30, 50];
pub const EXIT_MA_MIN: i64 = 2;
/// Never above the SMA120 entry line (see the module doc).
pub const EXIT_MA_MAX: i64 = SLOW_PERIOD;
pub const DEFAULT_EXIT_MA: i64 = 30;

/// True when `period` is a usable exit line period for this strategy.
pub fn exit_ma_allowed(period: i64) -> bool {
    (EXIT_MA_MIN..=EXIT_MA_MAX).contains(&period)
}
/// "cross" = new alignment only, "trend" = while the alignment holds.
pub const ENTRY_MODES: &[&str] = &["cross", "trend"];
pub const LEVERAGE: i64 = 5;
pub const MARGIN_FRACTION: &str = "0.5";
/// Exchange-side catastrophe stop in % of margin (-10% price at 5x).
pub const EMERGENCY_STOP_ROI: i64 = 50;

/// Account guard (user rule 2026-11-22, replaces the -50%/30-day halt):
/// while the futures account's total equity (wallet + unrealized PnL,
/// Binance `totalMarginBalance`) is below this, LIVE /tradfi opens nothing.
/// An open position is still managed and closed as usual, the strategy stays
/// ON, and entries resume by themselves once equity is back at or above it.
pub const MIN_EQUITY_USDT: i64 = 18;
/// Settings switch booked for the close of the current bar (user rule
/// 2026-11-27): {timeframe, exit_ma_period, exit_ma_off, align_slow_enabled,
/// from_timeframe, apply_at_ms}. The engine applies it once that bar closed
/// and was judged under the old settings; the first decision under the new
/// settings is the first new-timeframe close after the switch (a bar that was
/// already closed at the switch is never acted on).
pub const NEXT_SWITCH_KEY: &str = "next_switch";
/// Menu draft of a switch, not booked yet (same fields without the timing).
pub const NEXT_DRAFT_KEY: &str = "next_switch_draft";
/// Apply a booked switch anyway this long after its close when the old bar
/// was never evaluated (strategy off and flat, market data down...).
pub const SWITCH_GRACE_MS: i64 = 120_000;

fn bool_of(v: Option<&Value>, default: bool) -> bool {
    match v {
        Some(Value::Bool(b)) => *b,
        Some(Value::String(s)) => match s.trim().to_lowercase().as_str() {
            "true" | "1" | "on" => true,
            "false" | "0" | "off" => false,
            _ => default,
        },
        Some(Value::Number(n)) => n.as_f64().map(|f| f != 0.0).unwrap_or(default),
        _ => default,
    }
}

fn i64_of(v: Option<&Value>) -> Option<i64> {
    v.and_then(|v| {
        v.as_i64()
            .or_else(|| v.as_f64().map(|f| f as i64))
            .or_else(|| v.as_str().and_then(|s| s.trim().parse::<i64>().ok()))
    })
}

fn span_ms(tf: &str) -> i64 {
    crate::core::candles::timeframe_ms(tf).unwrap_or(0)
}

/// Close time of the `tf` bar that is forming at `now_ms`.
pub fn current_bar_close_ms(tf: &str, now_ms: i64) -> i64 {
    let span = span_ms(tf);
    if span <= 0 {
        return now_ms;
    }
    (now_ms.div_euclid(span) + 1) * span
}

/// Cleaned switch (or draft when `booked` is false); None when unusable.
pub fn clean_switch(v: &Value, booked: bool) -> Option<Value> {
    let m = v.as_object()?;
    let tf = m.get("timeframe")?.as_str()?.trim().to_lowercase();
    if !TIMEFRAMES.contains(&tf.as_str()) {
        return None;
    }
    let exit = i64_of(m.get("exit_ma_period")).filter(|p| exit_ma_allowed(*p))?;
    let use_long = bool_of(m.get("align_slow_enabled"), true);
    let mut out = json!({
        "timeframe": tf,
        "exit_ma_period": exit,
        "exit_ma_off": bool_of(m.get("exit_ma_off"), false),
        "align_slow_enabled": use_long,
    });
    if MA_KEYS.iter().any(|k| m.contains_key(*k)) {
        let (f, s, l) = ma_periods_of(v);
        if !ma_periods_ok(f, s, l, use_long) {
            return None;
        }
        out[MA_KEYS[0]] = json!(f);
        out[MA_KEYS[1]] = json!(s);
        out[MA_KEYS[2]] = json!(l);
    }
    if booked {
        let from = m.get("from_timeframe")?.as_str()?.trim().to_lowercase();
        let at = i64_of(m.get("apply_at_ms")).filter(|t| *t > 0)?;
        if !TIMEFRAMES.contains(&from.as_str()) {
            return None;
        }
        out["from_timeframe"] = json!(from);
        out["apply_at_ms"] = json!(at);
    }
    Some(out)
}

/// True once a booked switch may be applied: its close has passed and the bar
/// ending there was evaluated under the old settings (or the grace ran out).
pub fn switch_due(sw: &Value, now_ms: i64, last_evaluated_bar: i64) -> bool {
    let at = i64_of(sw.get("apply_at_ms")).unwrap_or(0);
    if at <= 0 || now_ms < at {
        return false;
    }
    let from = span_ms(
        sw.get("from_timeframe")
            .and_then(|v| v.as_str())
            .unwrap_or(""),
    );
    let judged = from > 0 && last_evaluated_bar >= at - from;
    judged || now_ms >= at + SWITCH_GRACE_MS
}

/// Open time of the newest `tf` bar closed at or before `apply_at_ms`. Marking
/// it evaluated makes the first decision under the new settings the first
/// close after the switch (no catch-up on a bar that was already closed).
pub fn bar_before_switch(tf: &str, apply_at_ms: i64) -> i64 {
    let span = span_ms(tf);
    if span <= 0 {
        return 0;
    }
    apply_at_ms.div_euclid(span) * span - span
}

/// "3m" -> "3분봉" ... "1d" -> "1일봉".
pub fn timeframe_label(tf: &str) -> String {
    let (num, unit) = tf.split_at(tf.len().saturating_sub(1));
    match unit {
        "m" => format!("{num}분봉"),
        "h" => format!("{num}시간봉"),
        "d" => format!("{num}일봉"),
        _ => tf.to_string(),
    }
}

/// "MA 1·5·120" or "MA 1·5 (긴MA 안 씀)".
pub fn ma_label(fast: i64, slow: i64, long: i64, use_long: bool) -> String {
    if use_long {
        format!("MA {fast}·{slow}·{long}")
    } else {
        format!("MA {fast}·{slow} (긴MA 안 씀)")
    }
}

/// "4시간봉 · MA 1·5·120 · 청산 30MA" (with "30분봉 → " when `from` is given).
pub fn switch_summary(sw: &Value, from: Option<&str>) -> String {
    let tf = sw.get("timeframe").and_then(|v| v.as_str()).unwrap_or("");
    let exit = if bool_of(sw.get("exit_ma_off"), false) {
        "청산MA 없음".to_string()
    } else {
        format!("청산 {}MA", i64_of(sw.get("exit_ma_period")).unwrap_or(0))
    };
    let (f, s, l) = ma_periods_of(sw);
    let ma = ma_label(f, s, l, bool_of(sw.get("align_slow_enabled"), true));
    let head = match from.filter(|f| !f.is_empty()) {
        Some(f) => format!("{} → ", timeframe_label(f)),
        None => String::new(),
    };
    format!("{head}{} · {ma} · {exit}", timeframe_label(tf))
}

/// Config changes that apply a booked switch (and clear it).
pub fn switch_changes(sw: &Value) -> Map<String, Value> {
    let mut changes = Map::new();
    for key in [
        "timeframe",
        "exit_ma_period",
        "exit_ma_off",
        "align_slow_enabled",
        MA_KEYS[0],
        MA_KEYS[1],
        MA_KEYS[2],
    ] {
        if let Some(v) = sw.get(key) {
            changes.insert(key.to_string(), v.clone());
        }
    }
    changes.insert(NEXT_SWITCH_KEY.to_string(), Value::Null);
    changes
}

/// Keys of the retired -50% halt, dropped from stored configs.
const RETIRED_KEYS: &[&str] = &["drawdown_base_equity", "drawdown_halt_until_ms"];

pub fn default_tradfi_config() -> Value {
    let mut cfg = normalize_btc_ma_cross_config(None);
    if let Some(map) = cfg.as_object_mut() {
        apply_fixed(
            map,
            DEFAULT_EXIT_MA,
            true,
            "cross",
            DEFAULT_SYMBOL,
            DEFAULT_TIMEFRAME,
        );
    }
    cfg
}

/// Normalize the raw `tradfi_ma` section: shared SMA-cross normalization, then pin
/// every setting that defines this strategy. Only `enabled`, `trading_mode`,
/// `network`, `single_symbol`, `timeframe`, `entry_mode`, `exit_ma_period` and
/// `profit_lock_enabled` (default off; lock starts at +5%, then every +1%, 1% gap) are
/// user-controlled.
pub fn normalize_tradfi_config(raw: Option<&Value>) -> Value {
    let supplied = raw.and_then(|v| v.as_object()).cloned().unwrap_or_default();
    let exit_ma = supplied
        .get("exit_ma_period")
        .and_then(|v| {
            v.as_i64()
                .or_else(|| v.as_str().and_then(|s| s.trim().parse::<i64>().ok()))
        })
        .filter(|p| exit_ma_allowed(*p))
        .unwrap_or(DEFAULT_EXIT_MA);
    let entry_mode = supplied
        .get("entry_mode")
        .and_then(|v| v.as_str())
        .map(|s| s.trim().to_lowercase())
        .filter(|s| ENTRY_MODES.contains(&s.as_str()))
        .unwrap_or_else(|| "cross".to_string());
    let symbol = supplied
        .get("single_symbol")
        .and_then(|v| v.as_str())
        .map(clean_symbol)
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| DEFAULT_SYMBOL.to_string());
    let timeframe = supplied
        .get("timeframe")
        .and_then(|v| v.as_str())
        .map(|s| s.trim().to_lowercase())
        .filter(|s| TIMEFRAMES.contains(&s.as_str()))
        .unwrap_or_else(|| DEFAULT_TIMEFRAME.to_string());
    // SMA120 in the entry alignment (default on); exits never use it.
    let align_slow = match supplied.get("align_slow_enabled") {
        Some(Value::Bool(b)) => *b,
        Some(Value::String(s)) => {
            !matches!(s.trim().to_lowercase().as_str(), "false" | "0" | "off")
        }
        Some(Value::Number(n)) => n.as_f64().unwrap_or(1.0) != 0.0,
        _ => true,
    };
    // "청산MA 없음": no exit line; a position closes on the opposite
    // alignment (close only), the profit lock or the emergency stop.
    let exit_off = bool_of(supplied.get("exit_ma_off"), false);
    // Alignment periods; an inconsistent set falls back to 1/5/120.
    let (ma_f, ma_s, ma_l) = {
        let p = ma_periods_of(&Value::Object(supplied.clone()));
        if ma_periods_ok(p.0, p.1, p.2, align_slow) {
            p
        } else {
            (FAST_PERIOD, MID_PERIOD, SLOW_PERIOD)
        }
    };
    let switches: Vec<(&str, Value)> = [(NEXT_SWITCH_KEY, true), (NEXT_DRAFT_KEY, false)]
        .into_iter()
        .map(|(key, booked)| {
            let v = supplied
                .get(key)
                .and_then(|v| clean_switch(v, booked))
                .unwrap_or(Value::Null);
            (key, v)
        })
        .collect();
    let profit_lock = match supplied.get("profit_lock_enabled") {
        Some(Value::Bool(b)) => *b,
        Some(Value::String(s)) => matches!(s.trim().to_lowercase().as_str(), "true" | "1" | "on"),
        Some(Value::Number(n)) => n.as_f64().unwrap_or(0.0) != 0.0,
        _ => false,
    };
    let mut cfg = normalize_btc_ma_cross_config(Some(&Value::Object(supplied)));
    if let Some(map) = cfg.as_object_mut() {
        for key in RETIRED_KEYS {
            map.remove(*key);
        }
        apply_fixed(map, exit_ma, !exit_off, &entry_mode, &symbol, &timeframe);
        map.insert("profit_lock_enabled".into(), Value::Bool(profit_lock));
        map.insert("align_slow_enabled".into(), Value::Bool(align_slow));
        map.insert("exit_ma_off".into(), Value::Bool(exit_off));
        map.insert("fast_period".into(), json!(ma_f));
        map.insert("align_mid_period".into(), json!(ma_s));
        map.insert("slow_period".into(), json!(ma_l));
        map.insert(MA_KEYS[0].into(), json!(ma_f));
        map.insert(MA_KEYS[1].into(), json!(ma_s));
        map.insert(MA_KEYS[2].into(), json!(ma_l));
        for (key, v) in switches {
            map.insert(key.into(), v);
        }
    }
    cfg
}

/// "xau" / "XAU/USDT:USDT" / "xauusdt" -> "XAUUSDT"; anything that is not a
/// plain alphanumeric `...USDT` market id -> "".
pub fn clean_symbol(text: &str) -> String {
    let mut s = text.trim().to_uppercase();
    if let Some((base, _)) = s.split_once(':') {
        s = base.to_string();
    }
    s = s.replace('/', "");
    if !s.ends_with("USDT") {
        s.push_str("USDT");
    }
    let base_ok = s.len() > 4 && s.chars().all(|c| c.is_ascii_alphanumeric());
    if base_ok {
        s
    } else {
        String::new()
    }
}

fn apply_fixed(
    map: &mut Map<String, Value>,
    exit_ma: i64,
    exit_on: bool,
    entry_mode: &str,
    symbol: &str,
    timeframe: &str,
) {
    let fixed = json!({
        "strategy_name": STRATEGY_NAME,
        "symbol": symbol,
        "scan_scope": "single",
        "single_symbol": symbol,
        "timeframe": timeframe,
        "signal_mode": "align",
        "fast_period": 1,
        "align_mid_period": MID_PERIOD,
        "slow_period": SLOW_PERIOD,
        "candle_history": 260,
        "exit_ma_enabled": exit_on,
        "exit_ma_period": exit_ma,
        "entry_mode": entry_mode,
        "leverage": LEVERAGE,
        "margin_fraction": MARGIN_FRACTION,
        "emergency_stop_roi_percent": EMERGENCY_STOP_ROI,
        "stop_ma_period": 0,
        "lock_start_roi_percent": "5",
        // 1% steps: the lock trails 1% under every new whole-percent peak.
        "lock_step_percent": "1",
        "lock_gap_percent": "1",
        "profit_lock_enabled": false,
        "profit_lock_switch_count": 0,
        "scale_in_enabled": false,
        "cross_lookback_bars": 1,
        "reentry_wait_bars": 1,
        "rotate_after_exit": false,
        "min_balance_usdt": 0,
        "min_equity_usdt": MIN_EQUITY_USDT,
        "daily_loss_limit_pct": 0,
        "max_loss_streak": 0,
    });
    if let Some(obj) = fixed.as_object() {
        for (k, v) in obj {
            map.insert(k.clone(), v.clone());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pins_strategy_settings_and_keeps_user_choices() {
        let cfg = normalize_tradfi_config(Some(&json!({
            "enabled": true,
            "trading_mode": "LIVE",
            "leverage": 2,
            "margin_fraction": "0.1",
            "single_symbol": "BTCUSDT",
            "exit_ma_period": 50,
            "entry_mode": "trend",
            "profit_lock_enabled": true,
        })));
        assert_eq!(cfg["enabled"], json!(true));
        assert_eq!(cfg["trading_mode"], json!("LIVE"));
        assert_eq!(cfg["leverage"], json!(5));
        assert_eq!(cfg["margin_fraction"], json!("0.5"));
        assert_eq!(cfg["single_symbol"], json!("BTCUSDT")); // kept; the service refuses crypto
        assert_eq!(cfg["timeframe"], json!("4h"));
        assert_eq!(cfg["exit_ma_period"], json!(50));
        assert_eq!(cfg["entry_mode"], json!("trend"));
        assert_eq!(cfg["profit_lock_enabled"], json!(true)); // user toggle
        assert_eq!(cfg["lock_start_roi_percent"], json!("5"));
        assert_eq!(cfg["lock_step_percent"], json!("1"));
        assert_eq!(cfg["lock_gap_percent"], json!("1"));
        assert_eq!(cfg["profit_lock_switch_count"], json!(0));
        assert_eq!(cfg["signal_mode"], json!("align"));
    }

    #[test]
    fn timeframe_is_user_choice_from_3m_to_1d() {
        for tf in TIMEFRAMES {
            let cfg = normalize_tradfi_config(Some(&json!({ "timeframe": tf })));
            assert_eq!(cfg["timeframe"], json!(tf));
            assert!(crate::core::candles::timeframe_ms(tf).is_some(), "{tf}");
        }
        let cfg = normalize_tradfi_config(Some(&json!({"timeframe": " 1D "})));
        assert_eq!(cfg["timeframe"], json!("1d"));
    }

    #[test]
    fn min_equity_is_fixed_and_old_halt_keys_are_dropped() {
        let n = normalize_tradfi_config(Some(&json!({
            "drawdown_base_equity": "80.5",
            "drawdown_halt_until_ms": 7,
            "min_equity_usdt": 0,
        })));
        assert_eq!(n["min_equity_usdt"], json!(18));
        assert!(n.get("drawdown_base_equity").is_none());
        assert!(n.get("drawdown_halt_until_ms").is_none());
        assert_eq!(default_tradfi_config()["min_equity_usdt"], json!(18));
    }

    #[test]
    fn profit_lock_order_is_cleaned_up_and_labelled() {
        use crate::strategy::service::Variant;
        assert!(Variant::TradFi.protection_kinds().contains(&"lock"));
        assert_eq!(
            Variant::TradFi.exit_reason_for_kind("lock"),
            Some("PROFIT_LOCK")
        );
        assert_eq!(
            Variant::TradFi.exit_reason_for_kind("sl"),
            Some("EMERGENCY_STOP")
        );
    }

    #[test]
    fn sma120_alignment_toggle() {
        assert_eq!(
            normalize_tradfi_config(None)["align_slow_enabled"],
            json!(true)
        );
        let off = normalize_tradfi_config(Some(&json!({"align_slow_enabled": false})));
        assert_eq!(off["align_slow_enabled"], json!(false));
        assert_eq!(off["slow_period"], json!(SLOW_PERIOD));
    }

    #[test]
    fn alignment_periods_are_user_choice() {
        let d = normalize_tradfi_config(None);
        assert_eq!(
            (&d["fast_period"], &d["align_mid_period"], &d["slow_period"]),
            (&json!(1), &json!(5), &json!(120))
        );
        // a stale shared-config slow_period does not leak in
        let stale = normalize_tradfi_config(Some(&json!({"slow_period": 200, "fast_period": 3})));
        assert_eq!(stale["slow_period"], json!(120));
        assert_eq!(stale["fast_period"], json!(1));
        let c = normalize_tradfi_config(Some(&json!({
            "ma_fast_period": 2, "ma_slow_period": 20, "ma_long_period": 200,
        })));
        assert_eq!(
            (&c["fast_period"], &c["align_mid_period"], &c["slow_period"]),
            (&json!(2), &json!(20), &json!(200))
        );
        // slow >= long is fine only while the long MA is excluded
        let bad = normalize_tradfi_config(Some(&json!({
            "ma_fast_period": 1, "ma_slow_period": 120, "ma_long_period": 120,
        })));
        assert_eq!(bad["align_mid_period"], json!(5));
        let off = normalize_tradfi_config(Some(&json!({
            "ma_fast_period": 1, "ma_slow_period": 120, "ma_long_period": 120,
            "align_slow_enabled": false,
        })));
        assert_eq!(off["align_mid_period"], json!(120));
        assert!(!ma_periods_ok(5, 5, 120, true));
        assert!(!ma_periods_ok(1, 5, MA_PERIOD_MAX + 1, true));
        assert_eq!(parse_ma_periods("1 5 120", 9), Some((1, 5, 120)));
        assert_eq!(parse_ma_periods("2/30", 200), Some((2, 30, 200)));
        assert_eq!(parse_ma_periods("1", 200), None);
        assert_eq!(parse_ma_periods("a b", 200), None);
    }

    #[test]
    fn switch_carries_ma_periods() {
        let sw = clean_switch(
            &json!({"timeframe": "4h", "exit_ma_period": 30,
                    "ma_fast_period": 1, "ma_slow_period": 120, "ma_long_period": 200}),
            false,
        )
        .unwrap();
        assert_eq!(sw["ma_slow_period"], json!(120));
        assert_eq!(switch_changes(&sw)["ma_long_period"], json!(200));
        assert!(switch_summary(&sw, None).contains("MA 1·120·200"));
        assert!(clean_switch(
            &json!({"timeframe": "4h", "exit_ma_period": 30,
                    "ma_fast_period": 5, "ma_slow_period": 5}),
            false
        )
        .is_none());
    }

    #[test]
    fn exit_line_can_be_turned_off() {
        let cfg = normalize_tradfi_config(None);
        assert_eq!(cfg["exit_ma_enabled"], json!(true));
        assert_eq!(cfg["exit_ma_off"], json!(false));
        let off =
            normalize_tradfi_config(Some(&json!({"exit_ma_off": true, "exit_ma_period": 50})));
        assert_eq!(off["exit_ma_enabled"], json!(false));
        assert_eq!(off["exit_ma_period"], json!(50));
    }

    #[test]
    fn booked_switch_is_kept_and_cleaned() {
        let sw = json!({
            "timeframe": "4h", "exit_ma_period": 120, "exit_ma_off": false,
            "align_slow_enabled": true, "from_timeframe": "30m", "apply_at_ms": 1_800_000,
        });
        let cfg = normalize_tradfi_config(Some(&json!({ NEXT_SWITCH_KEY: sw.clone() })));
        assert_eq!(cfg[NEXT_SWITCH_KEY], sw);
        assert_eq!(cfg[NEXT_DRAFT_KEY], Value::Null);
        // a booked switch needs its timing; bad fields drop it
        let bad = normalize_tradfi_config(Some(&json!({
            NEXT_SWITCH_KEY: {"timeframe": "4h", "exit_ma_period": 30},
            NEXT_DRAFT_KEY: {"timeframe": "7h", "exit_ma_period": 30},
        })));
        assert_eq!(bad[NEXT_SWITCH_KEY], Value::Null);
        assert_eq!(bad[NEXT_DRAFT_KEY], Value::Null);
        let draft = normalize_tradfi_config(Some(&json!({
            NEXT_DRAFT_KEY: {"timeframe": "1H", "exit_ma_period": "45", "exit_ma_off": true},
        })));
        assert_eq!(
            draft[NEXT_DRAFT_KEY],
            json!({"timeframe": "1h", "exit_ma_period": 45, "exit_ma_off": true, "align_slow_enabled": true})
        );
    }

    #[test]
    fn switch_waits_for_the_old_close_then_skips_closed_new_bars() {
        let m30 = 1_800_000i64;
        let h4 = 14_400_000i64;
        // booked at 10:10 on 30m bars -> applies at the 10:30 close
        let at = current_bar_close_ms("30m", 20 * m30 + 600_000);
        assert_eq!(at, 21 * m30);
        let sw = json!({"timeframe": "4h", "exit_ma_period": 120, "from_timeframe": "30m", "apply_at_ms": at});
        assert!(!switch_due(&sw, at - 1, 20 * m30));
        // the 10:00-10:30 bar not judged yet -> wait
        assert!(!switch_due(&sw, at + 5_000, 19 * m30));
        // judged under the old settings -> apply
        assert!(switch_due(&sw, at + 5_000, 20 * m30));
        // never judged -> apply after the grace
        assert!(switch_due(&sw, at + SWITCH_GRACE_MS, 19 * m30));
        // 10:30 -> newest closed 4h bar opened 04:00 (closed 08:00): skipped
        assert_eq!(bar_before_switch("4h", at), (at / h4) * h4 - h4);
        // a 4h bar closing exactly at the switch is skipped too
        assert_eq!(bar_before_switch("4h", 3 * h4), 2 * h4);
        let changes = switch_changes(&sw);
        assert_eq!(changes["timeframe"], json!("4h"));
        assert_eq!(changes["exit_ma_period"], json!(120));
        assert_eq!(changes[NEXT_SWITCH_KEY], Value::Null);
        assert!(!changes.contains_key("from_timeframe"));
    }

    #[test]
    fn symbols_are_cleaned() {
        assert_eq!(clean_symbol("xau"), "XAUUSDT");
        assert_eq!(clean_symbol("CL/USDT:USDT"), "CLUSDT");
        assert_eq!(clean_symbol(" qqqusdt "), "QQQUSDT");
        assert_eq!(clean_symbol("x-y"), "");
        let cfg = normalize_tradfi_config(Some(&json!({"single_symbol": "??"})));
        assert_eq!(cfg["single_symbol"], json!("XAUUSDT"));
    }

    #[test]
    fn exit_ma_is_any_period_from_2_to_120() {
        for p in [2, 7, 25, 45, 99, 120] {
            let cfg = normalize_tradfi_config(Some(&json!({ "exit_ma_period": p })));
            assert_eq!(cfg["exit_ma_period"], json!(p));
            assert_eq!(cfg["exit_ma_enabled"], json!(true));
        }
        let cfg = normalize_tradfi_config(Some(&json!({ "exit_ma_period": " 45 " })));
        assert_eq!(cfg["exit_ma_period"], json!(45));
        for bad in [
            json!(0),
            json!(1),
            json!(121),
            json!(500),
            json!(-5),
            json!("x"),
        ] {
            let cfg = normalize_tradfi_config(Some(&json!({ "exit_ma_period": bad })));
            assert_eq!(cfg["exit_ma_period"], json!(DEFAULT_EXIT_MA), "{bad}");
        }
        assert!(exit_ma_allowed(EXIT_MA_MIN) && exit_ma_allowed(EXIT_MA_MAX));
        assert!(!exit_ma_allowed(EXIT_MA_MAX + 1));
    }

    /// The exit line must be computable on every bar that can open a position:
    /// entry needs SLOW_PERIOD + 1 closed candles, which also covers the
    /// longest allowed exit period.
    #[test]
    fn longest_exit_line_exists_whenever_entry_is_possible() {
        use crate::core::sma_cross::{evaluate_cross, CrossConfig};
        let cfg = normalize_tradfi_config(Some(&json!({
            "exit_ma_period": EXIT_MA_MAX, "timeframe": "1h"
        })));
        let cross: CrossConfig = serde_json::from_value(cfg.clone()).unwrap();
        let hour = 3_600_000i64;
        // SLOW_PERIOD + 1 closed candles plus the forming one.
        let n = SLOW_PERIOD as usize + 2;
        let rows: Vec<[f64; 6]> = (0..n)
            .map(|i| {
                let c = 100.0 + i as f64 + if i % 2 == 0 { 0.5 } else { 0.0 };
                [(i as i64 * hour) as f64, c, c + 1.0, c - 1.0, c, 10.0]
            })
            .collect();
        let now = n as i64 * hour - hour + 1_000;
        let r = evaluate_cross(&rows, &cross, now, "XAUUSDT", Some("XAU/USDT:USDT"));
        assert_eq!(r.exit_ma_period, Some(EXIT_MA_MAX), "{:?}", r.skip_reason);
        assert!(r.sma_exit.is_some());
    }

    #[test]
    fn bad_choices_fall_back() {
        let cfg = normalize_tradfi_config(Some(
            &json!({"exit_ma_period": 121, "entry_mode": "x", "timeframe": "7h"}),
        ));
        assert_eq!(cfg["exit_ma_period"], json!(30));
        assert_eq!(cfg["timeframe"], json!("4h"));
        assert_eq!(cfg["entry_mode"], json!("cross"));
        let d = default_tradfi_config();
        assert_eq!(d["enabled"], json!(false));
        assert_eq!(d["trading_mode"], json!("DRY_RUN"));
        assert_eq!(d["profit_lock_enabled"], json!(false));
        let cfg = normalize_tradfi_config(Some(&json!({"exit_ma_period": 20})));
        assert_eq!(cfg["profit_lock_enabled"], json!(false));
    }
}
