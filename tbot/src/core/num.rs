//! Lenient number parsing for config values, matching Python's
//! `float(x)` / `int(x)` on numbers and numeric strings.

use serde::{de::Error, Deserialize, Deserializer};
use serde_json::Value;

/// `float(value)`; `None` when the value is missing, null or not numeric.
pub fn as_f64(value: &Value) -> Option<f64> {
    match value {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse::<f64>().ok(),
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        _ => None,
    }
}

/// `int(value)`: truncates floats like Python.
pub fn as_i64(value: &Value) -> Option<i64> {
    match value {
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().map(|f| f.trunc() as i64)),
        Value::String(s) => s.trim().parse::<i64>().ok(),
        Value::Bool(b) => Some(i64::from(*b)),
        _ => None,
    }
}

pub fn de_f64<'de, D: Deserializer<'de>>(d: D) -> Result<f64, D::Error> {
    let v = Value::deserialize(d)?;
    as_f64(&v).ok_or_else(|| D::Error::custom(format!("not a number: {v}")))
}

pub fn de_opt_f64<'de, D: Deserializer<'de>>(d: D) -> Result<Option<f64>, D::Error> {
    let v = Value::deserialize(d)?;
    if v.is_null() {
        return Ok(None);
    }
    as_f64(&v)
        .map(Some)
        .ok_or_else(|| D::Error::custom(format!("not a number: {v}")))
}

pub fn de_i64<'de, D: Deserializer<'de>>(d: D) -> Result<i64, D::Error> {
    let v = Value::deserialize(d)?;
    as_i64(&v).ok_or_else(|| D::Error::custom(format!("not an integer: {v}")))
}
