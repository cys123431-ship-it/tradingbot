use serde_json::Value;
use std::collections::HashSet;

use tbot::core::candles::Candle;
use tbot::core::indicators as ind;
use tbot::core::ma_plus::{self, Frame, TradeState};

fn load() -> Value {
    let text = include_str!("golden_plus.json");
    serde_json::from_str(text).expect("golden json parse")
}

fn approx_eq(a: f64, b: f64) -> bool {
    if a.is_nan() && b.is_nan() {
        return true;
    }
    if a.is_infinite() || b.is_infinite() {
        return a == b;
    }
    let rel = (a - b).abs();
    let denom = a.abs().max(b.abs()).max(1.0);
    rel <= 1e-9 * denom || (a - b).abs() <= 1e-9
}

fn float_from_value(v: &Value) -> Option<f64> {
    match v {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => match s.as_str() {
            "nan" => Some(f64::NAN),
            "inf" => Some(f64::INFINITY),
            "-inf" => Some(f64::NEG_INFINITY),
            other => other.parse::<f64>().ok(),
        },
        _ => None,
    }
}

fn compare(
    path: &str,
    expect: &Value,
    actual: &Value,
    case: usize,
    fn_name: &str,
) -> Result<(), String> {
    match expect {
        Value::Null => {
            if actual.is_null() {
                Ok(())
            } else {
                Err(format!(
                    "case {} fn {} path {}: expected null, got {:?}",
                    case, fn_name, path, actual
                ))
            }
        }
        Value::Bool(b) => {
            if let Value::Bool(a) = actual {
                if a == b {
                    Ok(())
                } else {
                    Err(format!(
                        "case {} fn {} path {}: expected {}, got {}",
                        case, fn_name, path, b, a
                    ))
                }
            } else {
                Err(format!(
                    "case {} fn {} path {}: expected bool {}, got {:?}",
                    case, fn_name, path, b, actual
                ))
            }
        }
        Value::Number(n) => {
            let e = n.as_f64().unwrap_or(f64::NAN);
            match actual {
                Value::Number(an) => {
                    let a = an.as_f64().unwrap_or(f64::NAN);
                    if approx_eq(e, a) {
                        Ok(())
                    } else {
                        Err(format!(
                            "case {} fn {} path {}: expected {}, got {}",
                            case, fn_name, path, e, a
                        ))
                    }
                }
                Value::String(s) if s == "nan" || s == "inf" || s == "-inf" => {
                    let a = float_from_value(actual).unwrap_or(f64::NAN);
                    if approx_eq(e, a) {
                        Ok(())
                    } else {
                        Err(format!(
                            "case {} fn {} path {}: expected {}, got {}",
                            case, fn_name, path, e, s
                        ))
                    }
                }
                _ => match float_from_value(actual) {
                    Some(a) if approx_eq(e, a) => Ok(()),
                    _ => Err(format!(
                        "case {} fn {} path {}: expected {}, got {:?}",
                        case, fn_name, path, e, actual
                    )),
                },
            }
        }
        Value::String(s) => match actual {
            Value::String(a) => {
                if a == s {
                    Ok(())
                } else {
                    Err(format!(
                        "case {} fn {} path {}: expected {:?}, got {:?}",
                        case, fn_name, path, s, a
                    ))
                }
            }
            Value::Number(an) => {
                if s == "nan" || s == "inf" || s == "-inf" {
                    let a = an.as_f64().unwrap_or(f64::NAN);
                    let e = float_from_value(expect).unwrap_or(f64::NAN);
                    if approx_eq(e, a) {
                        Ok(())
                    } else {
                        Err(format!(
                            "case {} fn {} path {}: expected {}, got {}",
                            case, fn_name, path, s, a
                        ))
                    }
                } else {
                    Err(format!(
                        "case {} fn {} path {}: expected {:?}, got number {}",
                        case, fn_name, path, s, an
                    ))
                }
            }
            _ => Err(format!(
                "case {} fn {} path {}: expected {:?}, got {:?}",
                case, fn_name, path, s, actual
            )),
        },
        Value::Array(ea) => match actual {
            Value::Array(aa) => {
                if ea.len() != aa.len() {
                    return Err(format!(
                        "case {} fn {} path {}: length {} != {}",
                        case,
                        fn_name,
                        path,
                        ea.len(),
                        aa.len()
                    ));
                }
                for (i, (e, a)) in ea.iter().zip(aa.iter()).enumerate() {
                    compare(&format!("{}[{}]", path, i), e, a, case, fn_name)?;
                }
                Ok(())
            }
            _ => Err(format!(
                "case {} fn {} path {}: expected array, got {:?}",
                case, fn_name, path, actual
            )),
        },
        Value::Object(eo) => match actual {
            Value::Object(ao) => {
                let ek: HashSet<&String> = eo.keys().collect();
                let ak: HashSet<&String> = ao.keys().collect();
                if ek != ak {
                    return Err(format!(
                        "case {} fn {} path {}: key set mismatch expected {:?} got {:?}",
                        case, fn_name, path, ek, ak
                    ));
                }
                for (k, ev) in eo.iter() {
                    let av = &ao[k];
                    compare(&format!("{}.{}", path, k), ev, av, case, fn_name)?;
                }
                Ok(())
            }
            _ => Err(format!(
                "case {} fn {} path {}: expected object, got {:?}",
                case, fn_name, path, actual
            )),
        },
    }
}

fn opt_f64(v: &Value) -> Option<f64> {
    match v {
        Value::Null => None,
        Value::Number(n) => n.as_f64(),
        Value::String(s) => match s.as_str() {
            "nan" => Some(f64::NAN),
            "inf" => Some(f64::INFINITY),
            "-inf" => Some(f64::NEG_INFINITY),
            other => other.parse::<f64>().ok(),
        },
        _ => None,
    }
}

fn rows_from_ref(data: &Value, ref_idx: usize) -> Vec<Candle> {
    let sets = data
        .get("candle_sets")
        .and_then(|v| v.as_array())
        .expect("candle_sets");
    let set = sets[ref_idx].as_array().expect("set array");
    set.iter()
        .map(|r| {
            let a = r.as_array().expect("row");
            let mut c = [0.0f64; 6];
            for (i, v) in a.iter().enumerate().take(6) {
                c[i] = v.as_f64().unwrap_or(0.0);
            }
            c
        })
        .collect()
}

fn rows_for_case(data: &Value, args: &Value) -> Vec<Candle> {
    let ref_idx = args.get("rows_ref").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
    let mut rows = rows_from_ref(data, ref_idx);
    if let Some(len) = args.get("rows_len").and_then(|v| v.as_u64()) {
        let n = len as usize;
        if n < rows.len() {
            rows.truncate(n);
        }
    }
    rows
}

fn opt_vec_from_option(values: &[Option<f64>]) -> Value {
    Value::Array(
        values
            .iter()
            .map(|v| match v {
                None => Value::Null,
                Some(f) => serde_json::Number::from_f64(*f)
                    .map(Value::Number)
                    .unwrap_or_else(|| {
                        Value::String(if f.is_nan() {
                            "nan".into()
                        } else if f.is_infinite() {
                            if *f > 0.0 {
                                "inf".into()
                            } else {
                                "-inf".into()
                            }
                        } else {
                            "nan".into()
                        })
                    }),
            })
            .collect(),
    )
}

fn frame_to_value(frame: &Frame) -> Value {
    let mut obj = serde_json::Map::new();
    obj.insert("closes".into(), opt_vec_from_option(&frame.closes));
    obj.insert("fast".into(), opt_vec_from_option(&frame.fast));
    obj.insert("slow".into(), opt_vec_from_option(&frame.slow));
    obj.insert("atr".into(), opt_vec_from_option(&frame.atr));
    if let Some(adx) = &frame.adx {
        obj.insert("adx".into(), opt_vec_from_option(adx));
    }
    if let Some(eff) = &frame.efficiency {
        obj.insert("efficiency".into(), opt_vec_from_option(eff));
    }
    if let Some(u) = &frame.donchian_upper {
        obj.insert("donchian_upper".into(), opt_vec_from_option(u));
    }
    if let Some(l) = &frame.donchian_lower {
        obj.insert("donchian_lower".into(), opt_vec_from_option(l));
    }
    Value::Object(obj)
}

fn value_from_f64(v: f64) -> Value {
    if v.is_nan() {
        Value::String("nan".into())
    } else if v.is_infinite() {
        Value::String(if v > 0.0 { "inf".into() } else { "-inf".into() })
    } else {
        serde_json::Number::from_f64(v)
            .map(Value::Number)
            .unwrap_or(Value::Null)
    }
}

fn value_from_option_f64(v: Option<f64>) -> Value {
    match v {
        None => Value::Null,
        Some(f) => value_from_f64(f),
    }
}

fn trade_from_value(v: &Value) -> TradeState {
    let s = |k: &str| v.get(k).and_then(|x| x.as_str()).map(|x| x.to_string());
    TradeState {
        highest_price: s("highest_price"),
        lowest_price: s("lowest_price"),
        entry_price: s("entry_price"),
        trail_price: s("trail_price"),
    }
}

#[test]
fn golden_plus() {
    let data = load();
    let cases = data.get("cases").and_then(|v| v.as_array()).expect("cases");

    let mut seen: HashSet<String> = HashSet::new();
    let required = [
        "sma",
        "true_range",
        "atr",
        "trend_efficiency",
        "donchian",
        "avg_volume",
        "floor_to_step",
        "adx",
        "volatility_target_scale",
        "chandelier_stop",
        "indicator_frame",
        "cross_side",
        "trend_filter_ok",
        "initial_stop",
        "size_multiplier",
        "trailing_stop",
        "evaluate",
    ];

    // Cache frames keyed by (rows_ref, rows_len) so cross_side/trend_filter_ok can reuse.
    // Recompute per case for simplicity and correctness.
    let mut long_entries = 0usize;
    let mut short_entries = 0usize;

    for (case_idx, case) in cases.iter().enumerate() {
        let fn_name = case.get("fn").and_then(|v| v.as_str()).expect("fn");
        let args = case.get("args").expect("args");
        let expect = case.get("expect").expect("expect");
        seen.insert(fn_name.to_string());

        let actual: Value = match fn_name {
            "sma" => {
                let values: Vec<f64> = args
                    .get("values")
                    .and_then(|v| v.as_array())
                    .expect("values")
                    .iter()
                    .map(|v| v.as_f64().unwrap_or(0.0))
                    .collect();
                let period = args.get("period").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
                opt_vec_from_option(&ind::sma(&values, period))
            }
            "true_range" => {
                let rows = rows_for_case(&data, args);
                Value::Array(
                    ind::true_range(&rows)
                        .into_iter()
                        .map(value_from_f64)
                        .collect(),
                )
            }
            "atr" => {
                let rows = rows_for_case(&data, args);
                let period = args.get("period").and_then(|v| v.as_u64()).unwrap_or(14) as usize;
                opt_vec_from_option(&ind::atr(&rows, period))
            }
            "trend_efficiency" => {
                let values: Vec<f64> = args
                    .get("values")
                    .and_then(|v| v.as_array())
                    .expect("values")
                    .iter()
                    .map(|v| v.as_f64().unwrap_or(0.0))
                    .collect();
                let period = args.get("period").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
                opt_vec_from_option(&ind::trend_efficiency(&values, period))
            }
            "donchian" => {
                let rows = rows_for_case(&data, args);
                let period = args.get("period").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
                let (u, l) = ind::donchian(&rows, period);
                Value::Array(vec![opt_vec_from_option(&u), opt_vec_from_option(&l)])
            }
            "avg_volume" => {
                let rows = rows_for_case(&data, args);
                let period = args.get("period").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
                opt_vec_from_option(&ind::avg_volume(&rows, period))
            }
            "floor_to_step" => {
                let value = args.get("value").and_then(|v| v.as_f64()).unwrap_or(0.0);
                let step = args.get("step").and_then(|v| v.as_f64()).unwrap_or(0.0);
                value_from_f64(ind::floor_to_step(value, step))
            }
            "adx" => {
                let rows = rows_for_case(&data, args);
                let period = args.get("period").and_then(|v| v.as_u64()).unwrap_or(14) as usize;
                opt_vec_from_option(&ind::adx(&rows, period))
            }
            "volatility_target_scale" => {
                let atr_value = opt_f64(args.get("atr_value").unwrap_or(&Value::Null));
                let close = opt_f64(args.get("close").unwrap_or(&Value::Null));
                let target_pct = opt_f64(args.get("target_pct").unwrap_or(&Value::Null));
                let min_scale = args
                    .get("min_scale")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(0.0);
                let max_scale = args
                    .get("max_scale")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(0.0);
                value_from_f64(ind::volatility_target_scale(
                    atr_value, close, target_pct, min_scale, max_scale,
                ))
            }
            "chandelier_stop" => {
                let side = args.get("side").and_then(|v| v.as_str()).unwrap_or("");
                let hh = opt_f64(args.get("highest_high").unwrap_or(&Value::Null));
                let ll = opt_f64(args.get("lowest_low").unwrap_or(&Value::Null));
                let atr_value = opt_f64(args.get("atr_value").unwrap_or(&Value::Null));
                let multiplier = args
                    .get("multiplier")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(1.0);
                let previous = opt_f64(args.get("previous").unwrap_or(&Value::Null));
                let breakeven = opt_f64(args.get("breakeven").unwrap_or(&Value::Null));
                value_from_option_f64(ind::chandelier_stop(
                    side, hh, ll, atr_value, multiplier, previous, breakeven,
                ))
            }
            "indicator_frame" => {
                let rows = rows_for_case(&data, args);
                let cfg = args.get("cfg").expect("cfg").clone();
                let frame = ma_plus::indicator_frame(&rows, &cfg);
                frame_to_value(&frame)
            }
            "cross_side" => {
                let rows = rows_for_case(&data, args);
                let cfg = args
                    .get("cfg")
                    .cloned()
                    .unwrap_or(Value::Object(Default::default()));
                let frame = ma_plus::indicator_frame(&rows, &cfg);
                let index = args.get("index").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
                let (side, trigger) = ma_plus::cross_side(&frame, index);
                let mut obj = serde_json::Map::new();
                obj.insert(
                    "side".into(),
                    match side {
                        Some(s) => Value::String(s.into()),
                        None => Value::Null,
                    },
                );
                obj.insert(
                    "trigger".into(),
                    match trigger {
                        Some(s) => Value::String(s.into()),
                        None => Value::Null,
                    },
                );
                Value::Array(vec![
                    obj.remove("side").unwrap(),
                    obj.remove("trigger").unwrap(),
                ])
            }
            "trend_filter_ok" => {
                let rows = rows_for_case(&data, args);
                let cfg = args.get("cfg").expect("cfg").clone();
                let frame = ma_plus::indicator_frame(&rows, &cfg);
                let index = args.get("index").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
                let side = args.get("side").and_then(|v| v.as_str()).unwrap_or("");
                let (ok, reason) = ma_plus::trend_filter_ok(&frame, index, side, &cfg);
                Value::Array(vec![Value::Bool(ok), Value::String(reason.into())])
            }
            "initial_stop" => {
                let side = args.get("side").and_then(|v| v.as_str()).unwrap_or("");
                let entry = opt_f64(args.get("entry_price").unwrap_or(&Value::Null));
                let atr_value = opt_f64(args.get("atr_value").unwrap_or(&Value::Null));
                let cfg = args.get("cfg").expect("cfg").clone();
                let (p, d) = ma_plus::initial_stop(side, entry, atr_value, &cfg);
                Value::Array(vec![value_from_option_f64(p), value_from_option_f64(d)])
            }
            "size_multiplier" => {
                let atr_value = opt_f64(args.get("atr_value").unwrap_or(&Value::Null));
                let close = opt_f64(args.get("close").unwrap_or(&Value::Null));
                let cfg = args.get("cfg").expect("cfg").clone();
                value_from_f64(ma_plus::size_multiplier(atr_value, close, &cfg))
            }
            "trailing_stop" => {
                let side = args.get("side").and_then(|v| v.as_str()).unwrap_or("");
                let mut trade = trade_from_value(args.get("trade").expect("trade"));
                let price = args.get("price").and_then(|v| v.as_f64()).unwrap_or(0.0);
                let atr_value = opt_f64(args.get("atr_value").unwrap_or(&Value::Null));
                let cfg = args.get("cfg").expect("cfg").clone();
                value_from_option_f64(ma_plus::trailing_stop(
                    side, &mut trade, price, atr_value, &cfg,
                ))
            }
            "evaluate" => {
                let rows = rows_for_case(&data, args);
                let cfg = args.get("cfg").expect("cfg").clone();
                let index = args
                    .get("index")
                    .and_then(|v| v.as_u64())
                    .map(|v| v as usize);
                let result = ma_plus::evaluate(&rows, &cfg, index);
                if let Some(side) = &result.side {
                    if side == "LONG" {
                        long_entries += 1;
                    } else if side == "SHORT" {
                        short_entries += 1;
                    }
                }
                serde_json::to_value(&result).expect("serialize evaluate")
            }
            other => panic!("unknown fn {}", other),
        };

        if let Err(msg) = compare("$", expect, &actual, case_idx, fn_name) {
            panic!("{}", msg);
        }
    }

    for name in required.iter() {
        if !seen.contains(*name) {
            panic!("fn {} was not checked by any golden case", name);
        }
    }
    assert!(long_entries >= 1, "no LONG evaluate entries exercised");
    assert!(short_entries >= 1, "no SHORT evaluate entries exercised");
}
