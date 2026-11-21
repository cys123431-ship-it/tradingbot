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
//! - Long exit: the close below the exit line SMA(20/30/50, default 30).
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
pub const EXIT_MA_CHOICES: &[i64] = &[20, 30, 50];
pub const DEFAULT_EXIT_MA: i64 = 30;
/// "cross" = new alignment only, "trend" = while the alignment holds.
pub const ENTRY_MODES: &[&str] = &["cross", "trend"];
pub const LEVERAGE: i64 = 5;
pub const MARGIN_FRACTION: &str = "0.5";
/// Exchange-side catastrophe stop in % of margin (-10% price at 5x).
pub const EMERGENCY_STOP_ROI: i64 = 50;

/// Account guard: when total equity falls to this % below the equity recorded
/// at turn-on, the position is closed and the strategy is halted.
pub const DRAWDOWN_HALT_PCT: f64 = 50.0;
pub const DRAWDOWN_HALT_DAYS: i64 = 30;
/// Runtime keys kept in the `tradfi_ma` section (not normalized away).
pub const BASE_EQUITY_KEY: &str = "drawdown_base_equity";
pub const HALT_UNTIL_KEY: &str = "drawdown_halt_until_ms";

#[derive(Debug, Clone, PartialEq)]
pub enum DrawdownAction {
    None,
    /// No reference equity yet: record this one.
    SetBase(f64),
    /// Loss limit reached: close, disable, halt until `until_ms`.
    Halt {
        base: f64,
        equity: f64,
        until_ms: i64,
    },
}

fn num_of(v: Option<&Value>) -> f64 {
    match v {
        Some(Value::Number(n)) => n.as_f64().unwrap_or(0.0),
        Some(Value::String(s)) => s.trim().parse::<f64>().unwrap_or(0.0),
        _ => 0.0,
    }
}

pub fn base_equity(cfg: &Value) -> f64 {
    num_of(cfg.get(BASE_EQUITY_KEY))
}

pub fn halt_until_ms(cfg: &Value) -> i64 {
    num_of(cfg.get(HALT_UNTIL_KEY)) as i64
}

pub fn is_halted(cfg: &Value, now_ms: i64) -> bool {
    halt_until_ms(cfg) > now_ms
}

/// Equity at which the halt fires for a given reference equity.
pub fn halt_equity(base: f64) -> f64 {
    base * (1.0 - DRAWDOWN_HALT_PCT / 100.0)
}

pub fn drawdown_decision(cfg: &Value, equity: f64, now_ms: i64) -> DrawdownAction {
    if !equity.is_finite() || equity <= 0.0 || is_halted(cfg, now_ms) {
        return DrawdownAction::None;
    }
    let base = base_equity(cfg);
    if !base.is_finite() || base <= 0.0 {
        return DrawdownAction::SetBase(equity);
    }
    if equity <= halt_equity(base) {
        return DrawdownAction::Halt {
            base,
            equity,
            until_ms: now_ms + DRAWDOWN_HALT_DAYS * 86_400_000,
        };
    }
    DrawdownAction::None
}

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
/// `profit_lock_enabled` (default off; lock steps are the shared 5/5/1) are
/// user-controlled.
pub fn normalize_tradfi_config(raw: Option<&Value>) -> Value {
    let supplied = raw.and_then(|v| v.as_object()).cloned().unwrap_or_default();
    let exit_ma = supplied
        .get("exit_ma_period")
        .and_then(|v| {
            v.as_i64()
                .or_else(|| v.as_str().and_then(|s| s.trim().parse::<i64>().ok()))
        })
        .filter(|p| EXIT_MA_CHOICES.contains(p))
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
        "lock_step_percent": "5",
        "lock_gap_percent": "1",
        "profit_lock_enabled": false,
        "profit_lock_switch_count": 0,
        "scale_in_enabled": false,
        "cross_lookback_bars": 1,
        "reentry_wait_bars": 1,
        "rotate_after_exit": false,
        "min_balance_usdt": 0,
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
    fn drawdown_guard_decisions() {
        let now = 1_000_000;
        let none = json!({});
        assert_eq!(
            drawdown_decision(&none, 100.0, now),
            DrawdownAction::SetBase(100.0)
        );
        assert_eq!(drawdown_decision(&none, 0.0, now), DrawdownAction::None);
        let cfg = json!({ BASE_EQUITY_KEY: 100.0 });
        assert_eq!(drawdown_decision(&cfg, 50.01, now), DrawdownAction::None);
        assert_eq!(
            drawdown_decision(&cfg, 50.0, now),
            DrawdownAction::Halt {
                base: 100.0,
                equity: 50.0,
                until_ms: now + 30 * 86_400_000
            }
        );
        let halted = json!({ BASE_EQUITY_KEY: 100.0, HALT_UNTIL_KEY: now + 1 });
        assert!(is_halted(&halted, now));
        assert_eq!(drawdown_decision(&halted, 10.0, now), DrawdownAction::None);
        assert!(!is_halted(&halted, now + 1));
        // runtime keys survive normalization
        let n =
            normalize_tradfi_config(Some(&json!({ BASE_EQUITY_KEY: "80.5", HALT_UNTIL_KEY: 7 })));
        assert_eq!(base_equity(&n), 80.5);
        assert_eq!(halt_until_ms(&n), 7);
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
    fn bad_choices_fall_back() {
        let cfg = normalize_tradfi_config(Some(
            &json!({"exit_ma_period": 25, "entry_mode": "x", "timeframe": "7h"}),
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
