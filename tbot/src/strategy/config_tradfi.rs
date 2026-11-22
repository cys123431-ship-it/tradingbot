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
pub const MID_PERIOD: i64 = 5;
pub const SLOW_PERIOD: i64 = 120;
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
/// Keys of the retired -50% halt, dropped from stored configs.
const RETIRED_KEYS: &[&str] = &["drawdown_base_equity", "drawdown_halt_until_ms"];

pub fn default_tradfi_config() -> Value {
    let mut cfg = normalize_btc_ma_cross_config(None);
    if let Some(map) = cfg.as_object_mut() {
        apply_fixed(
            map,
            DEFAULT_EXIT_MA,
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
        apply_fixed(map, exit_ma, &entry_mode, &symbol, &timeframe);
        map.insert("profit_lock_enabled".into(), Value::Bool(profit_lock));
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
        "exit_ma_enabled": true,
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
