//! Golden test: run the Rust port against results produced by the original
//! Python functions (`scripts/tbot_golden.py`).

use std::collections::BTreeSet;
use std::str::FromStr;

use rust_decimal::Decimal;
use serde_json::{json, Map, Value};
use tbot::core::candles::{closed_candles, timeframe_ms, Candle};
use tbot::core::periods as periods_mod;
use tbot::core::rules as rules_mod;
use tbot::core::sma_cross as sc;

const GOLDEN: &str = include_str!("golden.json");

fn cmp_f64(got: f64, want: f64) -> bool {
    if got == want {
        return true;
    }
    let diff = (got - want).abs();
    if diff <= 1e-9 {
        return true;
    }
    let denom = got.abs().max(want.abs());
    if denom == 0.0 {
        return diff <= 1e-9;
    }
    diff / denom <= 1e-9
}

fn is_decimal_like(s: &str) -> bool {
    Decimal::from_str(s).is_ok()
}

fn dec_eq(a: &str, b: &str) -> bool {
    match (Decimal::from_str(a), Decimal::from_str(b)) {
        (Ok(x), Ok(y)) => x.normalize() == y.normalize(),
        _ => false,
    }
}

fn assert_json_close(path: &str, got: &Value, want: &Value) -> Result<(), String> {
    match (got, want) {
        (Value::Number(a), Value::Number(b)) => {
            let (af, bf) = (
                a.as_f64().ok_or_else(|| format!("{}: not f64", path))?,
                b.as_f64().ok_or_else(|| format!("{}: not f64", path))?,
            );
            if cmp_f64(af, bf) {
                Ok(())
            } else {
                Err(format!("{}: {} != {}", path, af, bf))
            }
        }
        (Value::String(a), Value::String(b)) => {
            if a == b {
                return Ok(());
            }
            if is_decimal_like(a) && is_decimal_like(b) && dec_eq(a, b) {
                return Ok(());
            }
            Err(format!("{}: {:?} != {:?}", path, a, b))
        }
        (Value::Bool(a), Value::Bool(b)) => {
            if a == b {
                Ok(())
            } else {
                Err(format!("{}: {} != {}", path, a, b))
            }
        }
        (Value::Null, Value::Null) => Ok(()),
        (Value::Array(a), Value::Array(b)) => {
            if a.len() != b.len() {
                return Err(format!("{}: array length {} != {}", path, a.len(), b.len()));
            }
            for (i, (ga, wa)) in a.iter().zip(b.iter()).enumerate() {
                assert_json_close(&format!("{}[{}]", path, i), ga, wa)?;
            }
            Ok(())
        }
        (Value::Object(g), Value::Object(w)) => {
            let gk: BTreeSet<&String> = g.keys().collect();
            let wk: BTreeSet<&String> = w.keys().collect();
            if gk != wk {
                return Err(format!(
                    "{}: key set mismatch got={:?} want={:?}",
                    path, gk, wk
                ));
            }
            for (k, wv) in w.iter() {
                let gv = g.get(k).unwrap();
                assert_json_close(&format!("{}.{}", path, k), gv, wv)?;
            }
            Ok(())
        }
        _ => Err(format!("{}: type mismatch got={} want={}", path, got, want)),
    }
}

fn rows_from(v: &Value) -> Vec<Candle> {
    let arr = v.as_array().expect("rows array");
    arr.iter()
        .map(|r| {
            let a = r.as_array().expect("row array");
            let mut c = [0.0f64; 6];
            for i in 0..6 {
                c[i] = a[i].as_f64().unwrap_or(0.0);
            }
            c
        })
        .collect()
}

fn cross_cfg_from(v: &Value) -> sc::CrossConfig {
    serde_json::from_value(v.clone()).expect("cross cfg")
}

fn sizing_cfg_from(v: &Value) -> rules_mod::SizingConfig {
    serde_json::from_value(v.clone()).expect("sizing cfg")
}

fn protection_cfg_from(v: &Value) -> rules_mod::ProtectionConfig {
    serde_json::from_value(v.clone()).expect("protection cfg")
}

fn build_rules(symbol_info: &Value) -> rules_mod::TradingRules {
    rules_mod::parse_trading_rules(symbol_info).expect("parsed rules")
}

fn dec_to_value(d: Decimal) -> Value {
    Value::String(d.to_string())
}

fn run_case(case: &Value) -> Result<(), String> {
    let fname = case["fn"].as_str().unwrap_or("");
    let args = &case["args"];
    let expect = &case["expect"];

    let got: Value = match fname {
        "closed_candles" => {
            let rows = rows_from(&args["rows"]);
            let tf = args["timeframe"].as_str().unwrap();
            let now = args["now_ms"].as_i64().unwrap();
            let out = closed_candles(&rows, tf, now);
            json!(out.iter().map(|c| json!(c)).collect::<Vec<_>>())
        }
        "sma" => {
            let values: Vec<f64> = args["values"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_f64().unwrap())
                .collect();
            let period = args["period"].as_u64().unwrap() as usize;
            let out = sc::sma_opt(&values, period);
            json!(out)
        }
        "evaluate_cross" => {
            let rows = rows_from(&args["rows"]);
            let cfg = cross_cfg_from(&args["cfg"]);
            let now = args["now_ms"].as_i64().unwrap();
            let symbol = args["symbol"].as_str().unwrap_or("BTCUSDT");
            let ccxt = args["ccxt_symbol"].as_str();
            let res = sc::evaluate_cross(&rows, &cfg, now, symbol, ccxt);
            serde_json::to_value(&res).unwrap()
        }
        "market_is_flat" => {
            let rows = rows_from(&args["closed"]);
            let cfg = cross_cfg_from(&args["cfg"]);
            json!(sc::market_is_flat(&rows, &cfg, 100))
        }
        "margin_roi" => {
            let side = args["side"].as_str().unwrap();
            let entry = args["entry_price"].as_f64().unwrap();
            let price = args["price"].as_f64().unwrap();
            let lev = args["leverage"].as_f64().unwrap();
            json!(sc::margin_roi(side, entry, price, lev))
        }
        "profit_lock_target" => {
            let side = args["side"].as_str().unwrap();
            let entry = args["entry_price"].as_f64().unwrap();
            let best = args["best_price"].as_f64().unwrap();
            let lev = args["leverage"].as_f64().unwrap();
            let start = args["start"].as_f64().unwrap();
            let step = args["step"].as_f64().unwrap();
            let gap = args["gap"].as_f64().unwrap();
            match sc::profit_lock_target(side, entry, best, lev, start, step, gap) {
                Some((a, b, c)) => json!([a, b, c]),
                None => Value::Null,
            }
        }
        "emergency_stop_price" => {
            let side = args["side"].as_str().unwrap();
            let entry = args["entry_price"].as_f64().unwrap();
            let lev = args["leverage"].as_f64().unwrap();
            let roi = args["roi_percent"].as_f64().unwrap();
            match sc::emergency_stop_price(side, entry, lev, roi) {
                Some(v) => json!(v),
                None => Value::Null,
            }
        }
        "parse_trading_rules" => match rules_mod::parse_trading_rules(&args["symbol_info"]) {
            Ok(r) => Value::Object(r.as_log()),
            Err(e) => json!({"error": e.to_string()}),
        },
        "floor_to_step" => {
            let value = args["value"].as_str().unwrap();
            let step = args["step"].as_str().unwrap();
            match rules_mod::floor_to_step(value, step) {
                Ok(d) => dec_to_value(d),
                Err(e) => json!({"error": e.to_string()}),
            }
        }
        "ceil_to_step" => {
            let value = args["value"].as_str().unwrap();
            let step = args["step"].as_str().unwrap();
            match rules_mod::ceil_to_step(value, step) {
                Ok(d) => dec_to_value(d),
                Err(e) => json!({"error": e.to_string()}),
            }
        }
        "round_price_to_tick" => {
            let price = args["price"].as_str().unwrap();
            let tick = args["tick"].as_str().unwrap();
            let direction = args["direction"].as_str().unwrap();
            match rules_mod::round_price_to_tick(price, tick, direction) {
                Ok(d) => dec_to_value(d),
                Err(e) => json!({"error": e.to_string()}),
            }
        }
        "protection_prices" => {
            let side = args["side"].as_str().unwrap();
            let entry = args["avg_entry_price"].as_str().unwrap();
            let cfg = protection_cfg_from(&args["cfg"]);
            let symbol_info = json!({
                "symbol": "BTCUSDT",
                "quantityPrecision": 3,
                "pricePrecision": 2,
                "status": "TRADING",
                "contractType": "PERPETUAL",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.10", "minPrice": "0.10", "maxPrice": "1000000"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.002", "minQty": "0.002", "maxQty": "500"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ],
            });
            let rules = build_rules(&symbol_info);
            match rules_mod::protection_prices(side, entry, &cfg, &rules) {
                Ok((stop, take)) => {
                    let mut m = Map::new();
                    m.insert("stop".to_string(), Value::String(stop.to_string()));
                    m.insert("take".to_string(), Value::String(take.to_string()));
                    Value::Object(m)
                }
                Err(e) => json!({"error": e.to_string()}),
            }
        }
        "estimate_trade_risk" => {
            let price = args["price"].as_str().unwrap();
            let qty = args["quantity"].as_str().unwrap();
            let cfg = sizing_cfg_from(&args["cfg"]);
            let fee = args["taker_fee_rate"].as_str().unwrap();
            match rules_mod::estimate_trade_risk(price, qty, &cfg, fee) {
                Ok(r) => serde_json::to_value(&r).unwrap(),
                Err(e) => json!({"error": e.to_string()}),
            }
        }
        "validate_and_normalize_order_quantity" => {
            let price = args["price"].as_str().unwrap();
            let wallet = args["wallet_balance"].as_str().unwrap();
            let available = args["available_balance"].as_str().unwrap();
            let cfg = sizing_cfg_from(&args["cfg"]);
            let fee = args["taker_fee_rate"].as_str().unwrap();
            let mode = args["sizing_mode"].as_str().unwrap_or("fixed");
            let symbol_info = json!({
                "symbol": "BTCUSDT",
                "quantityPrecision": 3,
                "pricePrecision": 2,
                "status": "TRADING",
                "contractType": "PERPETUAL",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.10", "minPrice": "0.10", "maxPrice": "1000000"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.002", "minQty": "0.002", "maxQty": "500"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ],
            });
            let rules = build_rules(&symbol_info);
            match rules_mod::validate_and_normalize_order_quantity(
                &rules, price, wallet, available, &cfg, fee, mode,
            ) {
                Ok(d) => serde_json::to_value(&d).unwrap(),
                Err(e) => json!({"error": e.to_string()}),
            }
        }
        "validate_periods" => {
            let fast = &args["fast"];
            let slow = &args["slow"];
            match periods_mod::validate_periods(fast, slow) {
                Ok((f, s)) => json!([f, s]),
                Err(e) => json!({"error": e.to_string()}),
            }
        }
        "parse_period_command" => {
            let args_list: Vec<String> = args["args"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap().to_string())
                .collect();
            match periods_mod::parse_period_command(&args_list) {
                Ok((f, s)) => json!([f, s]),
                Err(e) => json!({"error": e.to_string()}),
            }
        }
        "periods_label" => {
            let fast = args["fast"].as_i64().unwrap();
            let slow = args["slow"].as_i64().unwrap();
            json!(periods_mod::periods_label(fast, slow))
        }
        "required_candle_history" => {
            let slow = args["slow_period"].as_i64().unwrap();
            json!(periods_mod::required_candle_history(slow))
        }
        other => return Err(format!("unhandled fn: {}", other)),
    };

    assert_json_close("/", &got, expect).map_err(|e| format!("fn={} case={}: {}", fname, case, e))
}

#[test]
fn golden_cases() {
    let data: Value = serde_json::from_str(GOLDEN).expect("golden.json");
    let cases = data["cases"].as_array().expect("cases array");
    assert!(!cases.is_empty(), "no golden cases");

    let mut seen: BTreeSet<String> = BTreeSet::new();
    let required: BTreeSet<&str> = [
        "closed_candles",
        "sma",
        "evaluate_cross",
        "market_is_flat",
        "margin_roi",
        "profit_lock_target",
        "emergency_stop_price",
        "parse_trading_rules",
        "floor_to_step",
        "ceil_to_step",
        "round_price_to_tick",
        "protection_prices",
        "estimate_trade_risk",
        "validate_and_normalize_order_quantity",
        "validate_periods",
        "parse_period_command",
        "periods_label",
        "required_candle_history",
    ]
    .into_iter()
    .collect();

    for case in cases {
        let fname = case["fn"].as_str().unwrap_or("").to_string();
        if let Err(e) = run_case(case) {
            panic!("{}", e);
        }
        seen.insert(fname);
    }

    for req in required.iter() {
        assert!(seen.contains(*req), "no golden case exercised fn={}", req);
    }

    // Sanity: timeframe_ms matches the table for the known timeframes.
    assert_eq!(timeframe_ms("1h"), Some(3_600_000));
    assert_eq!(timeframe_ms("15m"), Some(900_000));
}
