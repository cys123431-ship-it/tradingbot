//! Golden test: raw Binance responses converted to ccxt shapes.

use serde_json::{json, Value};
use tbot::exchange::ccxt_shape::{
    parse_balance_linear, parse_market, parse_order, parse_position_risk, parse_ticker, Markets,
};

const REL_TOL: f64 = 1e-9;
const ABS_TOL: f64 = 1e-9;

fn load_golden() -> Value {
    let text = include_str!("golden_ccxt.json");
    serde_json::from_str(text).expect("golden_ccxt.json parse")
}

fn numbers_close(a: f64, b: f64) -> bool {
    if a == b {
        return true;
    }
    let diff = (a - b).abs();
    if diff <= ABS_TOL {
        return true;
    }
    let max = a.abs().max(b.abs());
    diff / max <= REL_TOL
}

fn compare(
    kind: &str,
    index: usize,
    path: &str,
    actual: &Value,
    expected: &Value,
    errs: &mut Vec<String>,
) {
    match (actual, expected) {
        (Value::Object(a), Value::Object(e)) => {
            let mut ak: Vec<&String> = a.keys().collect();
            let mut ek: Vec<&String> = e.keys().collect();
            ak.sort();
            ek.sort();
            if ak != ek {
                let missing: Vec<&&String> = ek.iter().filter(|k| !a.contains_key(**k)).collect();
                let extra: Vec<&&String> = ak.iter().filter(|k| !e.contains_key(**k)).collect();
                errs.push(format!(
                    "{kind}[{index}].{path}: key set mismatch missing={missing:?} extra={extra:?}"
                ));
                return;
            }
            for k in ek {
                let child_path = if path.is_empty() {
                    k.clone()
                } else {
                    format!("{path}.{k}")
                };
                compare(kind, index, &child_path, &a[k], &e[k], errs);
            }
        }
        (Value::Array(a), Value::Array(e)) => {
            if a.len() != e.len() {
                errs.push(format!(
                    "{kind}[{index}].{path}: array length mismatch actual={} expected={}",
                    a.len(),
                    e.len()
                ));
                return;
            }
            for (i, (av, ev)) in a.iter().zip(e.iter()).enumerate() {
                compare(kind, index, &format!("{path}[{i}]"), av, ev, errs);
            }
        }
        (Value::Number(a), Value::Number(e)) => {
            let af = a.as_f64().unwrap_or(f64::NAN);
            let ef = e.as_f64().unwrap_or(f64::NAN);
            if !numbers_close(af, ef) {
                errs.push(format!(
                    "{kind}[{index}].{path}: number mismatch actual={af} expected={ef}"
                ));
            }
        }
        _ => {
            if actual != expected {
                errs.push(format!(
                    "{kind}[{index}].{path}: value mismatch actual={actual} expected={expected}"
                ));
            }
        }
    }
}

fn expect_equal(kind: &str, index: usize, actual: &Value, expected: &Value) {
    let mut errs = Vec::new();
    compare(kind, index, "", actual, expected, &mut errs);
    if !errs.is_empty() {
        panic!("{}", errs.join("\n"));
    }
}

#[test]
fn golden_markets() {
    let golden = load_golden();
    let markets_json = golden
        .get("markets")
        .and_then(|v| v.as_array())
        .expect("markets");
    let raws: Vec<Value> = markets_json
        .iter()
        .map(|c| c.get("raw").cloned().expect("raw"))
        .collect();
    let info = json!({ "symbols": raws.clone() });
    let markets = Markets::from_exchange_info(&info);

    for (i, case) in markets_json.iter().enumerate() {
        let raw = case.get("raw").expect("raw");
        let expect = case.get("expect").expect("expect");
        let parsed = parse_market(raw);
        expect_equal("markets", i, &parsed, expect);

        // Also verify the Markets index reflects the same data.
        let symbol = expect.get("symbol").and_then(|v| v.as_str()).unwrap();
        let from_index = markets.market(symbol).expect("market by symbol");
        expect_equal("markets", i, from_index, expect);
    }
}

#[test]
fn golden_tickers() {
    let golden = load_golden();
    let markets_json = golden
        .get("markets")
        .and_then(|v| v.as_array())
        .expect("markets");
    let raws: Vec<Value> = markets_json
        .iter()
        .map(|c| c.get("raw").cloned().expect("raw"))
        .collect();
    let info = json!({ "symbols": raws });
    let markets = Markets::from_exchange_info(&info);

    let tickers = golden
        .get("tickers")
        .and_then(|v| v.as_array())
        .expect("tickers");
    for (i, case) in tickers.iter().enumerate() {
        let raw = case.get("raw").expect("raw");
        let expect = case.get("expect").expect("expect");
        let id = raw.get("symbol").and_then(|v| v.as_str()).unwrap_or("");
        let market = markets.market_by_id(id);
        let parsed = parse_ticker(raw, market);
        expect_equal("tickers", i, &parsed, expect);
    }
}

#[test]
fn golden_orders() {
    let golden = load_golden();
    let markets_json = golden
        .get("markets")
        .and_then(|v| v.as_array())
        .expect("markets");
    let raws: Vec<Value> = markets_json
        .iter()
        .map(|c| c.get("raw").cloned().expect("raw"))
        .collect();
    let info = json!({ "symbols": raws });
    let markets = Markets::from_exchange_info(&info);

    let orders = golden
        .get("orders")
        .and_then(|v| v.as_array())
        .expect("orders");
    for (i, case) in orders.iter().enumerate() {
        let raw = case.get("raw").expect("raw");
        let expect = case.get("expect").expect("expect");
        let id = raw.get("symbol").and_then(|v| v.as_str()).unwrap_or("");
        let market = markets.market_by_id(id);
        let parsed = parse_order(raw, market);
        expect_equal("orders", i, &parsed, expect);
    }
}

#[test]
fn golden_positions() {
    let golden = load_golden();
    let markets_json = golden
        .get("markets")
        .and_then(|v| v.as_array())
        .expect("markets");
    let raws: Vec<Value> = markets_json
        .iter()
        .map(|c| c.get("raw").cloned().expect("raw"))
        .collect();
    let info = json!({ "symbols": raws });
    let markets = Markets::from_exchange_info(&info);

    let positions = golden
        .get("positions")
        .and_then(|v| v.as_array())
        .expect("positions");
    for (i, case) in positions.iter().enumerate() {
        let raw = case.get("raw").expect("raw");
        let expect = case.get("expect").expect("expect");
        let id = raw.get("symbol").and_then(|v| v.as_str()).unwrap_or("");
        let market = markets.market_by_id(id);
        let parsed = parse_position_risk(raw, market);
        expect_equal("positions", i, &parsed, expect);
    }
}

#[test]
fn golden_balance() {
    let golden = load_golden();
    let case = golden.get("balance").expect("balance");
    let raw = case.get("raw").expect("raw");
    let expect = case.get("expect").expect("expect");
    let parsed = parse_balance_linear(raw);
    expect_equal("balance", 0, &parsed, expect);
}
