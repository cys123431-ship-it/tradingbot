//! Telegram-controlled SMA periods ported from `trading_safety/sma_periods.py`.

use serde_json::Value;
use thiserror::Error;

pub const DEFAULT_FAST_PERIOD: i64 = 3;
pub const DEFAULT_SLOW_PERIOD: i64 = 200;
pub const MAX_PERIOD: i64 = 500;
pub const MIN_WARMUP_BARS: i64 = 10;

pub const BTC_MA_PERIOD_COMMANDS: &[&str] = &["btcma", "btcmaperiod"];
pub const MA_CROSS_PERIOD_COMMANDS: &[&str] = &["map", "maperiod"];

/// One-tap presets: (label, fast, slow).
pub const PERIOD_PRESETS: &[(&str, i64, i64)] = &[
    ("3/120", 3, 120),
    ("3/200", 3, 200),
    ("5/60", 5, 60),
    ("1/200", 1, 200),
    ("9/21", 9, 21),
];

/// `PERIOD_COMMANDS`: BTC MA commands followed by MA cross commands.
pub fn period_commands() -> Vec<&'static str> {
    let mut out = Vec::with_capacity(BTC_MA_PERIOD_COMMANDS.len() + MA_CROSS_PERIOD_COMMANDS.len());
    out.extend_from_slice(BTC_MA_PERIOD_COMMANDS);
    out.extend_from_slice(MA_CROSS_PERIOD_COMMANDS);
    out
}

/// Invalid fast/slow pair; the message is a Telegram-ready reason.
#[derive(Debug, Error)]
pub enum PeriodError {
    #[error("정수만 입력할 수 있습니다.")]
    IntegerOnly,
    #[error("값이 비어 있습니다.")]
    Empty,
    #[error("숫자가 아닙니다: {0}")]
    NotANumber(String),
    #[error("소수는 사용할 수 없습니다: {0}")]
    Decimal(String),
    #[error("패스트는 1 이상이어야 합니다 (입력: {0}).")]
    FastTooSmall(i64),
    #[error("슬로우는 2 이상이어야 합니다 (입력: {0}).")]
    SlowTooSmall(i64),
    #[error("패스트({fast})는 슬로우({slow})보다 작아야 합니다. (같거나 크면 크로스가 발생하지 않습니다)")]
    NotStrictlyLess { fast: i64, slow: i64 },
    #[error("슬로우는 {MAX_PERIOD} 이하여야 합니다 (입력: {0}).")]
    SlowTooLarge(i64),
    #[error("값이 없습니다. 예) 1 200")]
    NoArgs,
    #[error("패스트와 슬로우 두 개를 입력하세요. 예) 1 200 (입력: {0})")]
    NotEnoughTokens(String),
    #[error("값이 너무 많습니다. 예) 1 200 (입력: {0})")]
    TooManyTokens(String),
}

/// Strict integer parse: `200 -> 200, `3.5` -> error.
pub fn as_int(value: &Value) -> Result<i64, PeriodError> {
    match value {
        Value::Bool(_) => Err(PeriodError::IntegerOnly),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Ok(i)
            } else if let Some(f) = n.as_f64() {
                if f == (f as i64) as f64 {
                    Ok(f as i64)
                } else {
                    Err(PeriodError::Decimal(f.to_string()))
                }
            } else {
                Err(PeriodError::NotANumber(n.to_string()))
            }
        }
        Value::Null => {
            let text = String::new();
            if text.is_empty() {
                Err(PeriodError::Empty)
            } else {
                parse_int_text(&text)
            }
        }
        Value::String(s) => {
            let text = s.trim();
            if text.is_empty() {
                Err(PeriodError::Empty)
            } else {
                parse_int_text(text)
            }
        }
        other => {
            let text = other.to_string();
            let text = text.trim().to_string();
            if text.is_empty() {
                Err(PeriodError::Empty)
            } else {
                parse_int_text(&text)
            }
        }
    }
}

fn parse_int_text(text: &str) -> Result<i64, PeriodError> {
    let number: f64 = match text.parse::<f64>() {
        Ok(v) => v,
        Err(_) => return Err(PeriodError::NotANumber(text.to_string())),
    };
    let truncated = number.trunc();
    if number != truncated {
        return Err(PeriodError::Decimal(text.to_string()));
    }
    Ok(truncated as i64)
}

/// Closed candles needed so the slow SMA and its previous value exist.
pub fn required_candle_history(slow_period: i64) -> i64 {
    (MIN_WARMUP_BARS + slow_period).max(210)
}

/// (fast, slow) as ints, or a Korean error.
pub fn validate_periods(fast: &Value, slow: &Value) -> Result<(i64, i64), PeriodError> {
    let fast_p = as_int(fast)?;
    let slow_p = as_int(slow)?;
    if fast_p < 1 {
        return Err(PeriodError::FastTooSmall(fast_p));
    }
    if slow_p < 2 {
        return Err(PeriodError::SlowTooSmall(slow_p));
    }
    if fast_p >= slow_p {
        return Err(PeriodError::NotStrictlyLess {
            fast: fast_p,
            slow: slow_p,
        });
    }
    if slow_p > MAX_PERIOD {
        return Err(PeriodError::SlowTooLarge(slow_p));
    }
    Ok((fast_p, slow_p))
}

/// Validated `(fast, slow)` from a raw config; never raises.
pub fn normalize_periods(cfg: &Value, default_fast: i64, default_slow: i64) -> (i64, i64) {
    let map = cfg.as_object();
    let fast = map
        .and_then(|m| m.get("fast_period"))
        .cloned()
        .unwrap_or(Value::Null);
    let slow = map
        .and_then(|m| m.get("slow_period"))
        .cloned()
        .unwrap_or(Value::Null);
    if let Ok(pair) = validate_periods(&fast, &slow) {
        return pair;
    }
    let df = Value::Number(default_fast.into());
    let ds = Value::Number(default_slow.into());
    match validate_periods(&df, &ds) {
        Ok(pair) => pair,
        Err(_) => (DEFAULT_FAST_PERIOD, DEFAULT_SLOW_PERIOD),
    }
}

/// Write validated periods into `cfg` and lift `candle_history` if needed.
pub fn apply_periods(cfg: &mut Value, default_fast: i64, default_slow: i64) -> (i64, i64) {
    let (fast_p, slow_p) = normalize_periods(cfg, default_fast, default_slow);
    let needed = required_candle_history(slow_p);
    let history_existing = cfg.get("candle_history").cloned();
    let history: i64 = match history_existing {
        Some(Value::Number(n)) => n.as_f64().map(|f| f as i64).unwrap_or(needed),
        Some(Value::String(s)) => s.parse::<f64>().ok().map(|f| f as i64).unwrap_or(needed),
        Some(Value::Null) | None => needed,
        _ => needed,
    };
    if let Some(obj) = cfg.as_object_mut() {
        obj.insert("fast_period".to_string(), Value::Number(fast_p.into()));
        obj.insert("slow_period".to_string(), Value::Number(slow_p.into()));
        obj.insert(
            "candle_history".to_string(),
            Value::Number(history.max(needed).into()),
        );
    }
    (fast_p, slow_p)
}

/// Parse Telegram command args into `(fast, slow)`.
pub fn parse_period_command(args: &[String]) -> Result<(i64, i64), PeriodError> {
    let mut tokens: Vec<String> = Vec::new();
    for arg in args {
        let replaced = arg.replace([',', '/'], " ");
        for part in replaced.split_whitespace() {
            if !part.is_empty() {
                tokens.push(part.to_string());
            }
        }
    }
    if tokens.is_empty() {
        return Err(PeriodError::NoArgs);
    }
    if tokens.len() == 1 {
        return Err(PeriodError::NotEnoughTokens(tokens[0].clone()));
    }
    if tokens.len() > 2 {
        return Err(PeriodError::TooManyTokens(tokens.join(" ")));
    }
    let fast = Value::String(tokens[0].clone());
    let slow = Value::String(tokens[1].clone());
    validate_periods(&fast, &slow)
}

/// Short label such as `3/120`.
pub fn periods_label(fast: i64, slow: i64) -> String {
    format!("{}/{}", fast, slow)
}

/// True when `(fast, slow)` equals the given defaults.
pub fn is_default_periods(fast: i64, slow: i64, default_fast: i64, default_slow: i64) -> bool {
    fast == default_fast && slow == default_slow
}
