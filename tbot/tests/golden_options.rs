//! Golden comparison of the options-trading port against the Python originals.
//!
//! Fixtures come from `scripts/tbot_golden_options.py` (Python `options_trading`
//! strategy / risk / config run on fixed pseudo-random inputs).

use std::path::PathBuf;

use rust_decimal::prelude::ToPrimitive;
use serde_json::{json, Map, Value};
use tbot::core::candles::Candle;
use tbot::core::options_strategy as st;
use tbot::strategy::options_config as oc;

fn cases() -> Vec<Value> {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push("tests");
    p.push("golden_options.json");
    let text = std::fs::read_to_string(p).expect("golden_options.json");
    serde_json::from_str::<Vec<Value>>(&text).expect("golden json")
}

fn num(v: &Value) -> f64 {
    v.as_f64().unwrap_or(0.0)
}

fn rows(v: &Value) -> Vec<Candle> {
    v.as_array()
        .map(|a| {
            a.iter()
                .map(|r| {
                    let r = r.as_array().expect("candle row");
                    let mut c = [0.0; 6];
                    for (i, x) in r.iter().take(6).enumerate() {
                        c[i] = num(x);
                    }
                    c
                })
                .collect()
        })
        .unwrap_or_default()
}

fn floats(v: &Value) -> Vec<f64> {
    v.as_array()
        .map(|a| a.iter().map(num).collect())
        .unwrap_or_default()
}

/// Deep compare: numbers within 1e-9 relative (int vs float ignored), everything else exact.
fn diff(path: &str, expected: &Value, actual: &Value, out: &mut Vec<String>) {
    match (expected, actual) {
        (Value::Number(a), Value::Number(b)) => {
            let (a, b) = (
                a.as_f64().unwrap_or(f64::NAN),
                b.as_f64().unwrap_or(f64::NAN),
            );
            let tol = 1e-9 * a.abs().max(b.abs()).max(1.0);
            if (a - b).abs() > tol {
                out.push(format!("{path}: expected {a} got {b}"));
            }
        }
        (Value::Object(a), Value::Object(b)) => {
            for (k, v) in a {
                match b.get(k) {
                    Some(w) => diff(&format!("{path}.{k}"), v, w, out),
                    None => out.push(format!("{path}.{k}: missing")),
                }
            }
            for k in b.keys() {
                if !a.contains_key(k) {
                    out.push(format!("{path}.{k}: unexpected key"));
                }
            }
        }
        (Value::Array(a), Value::Array(b)) => {
            if a.len() != b.len() {
                out.push(format!("{path}: len {} vs {}", a.len(), b.len()));
                return;
            }
            for (i, (x, y)) in a.iter().zip(b).enumerate() {
                diff(&format!("{path}[{i}]"), x, y, out);
            }
        }
        _ => {
            if expected != actual {
                out.push(format!("{path}: expected {expected} got {actual}"));
            }
        }
    }
}

fn run(fn_name: &str, a: &Map<String, Value>) -> Option<Value> {
    let g = |k: &str| a.get(k).cloned().unwrap_or(Value::Null);
    let r = match fn_name {
        "forecast_realized_volatility" => {
            json!(st::forecast_realized_volatility(&floats(&g("closes"))))
        }
        "realized_volatility" => json!(st::realized_volatility(
            &floats(&g("closes")),
            24.0 * 365.0,
            num(&g("sample_size")) as usize
        )),
        "expected_option_net_edge" => st::expected_option_net_edge(
            g("side").as_str().unwrap_or(""),
            num(&g("spot_price")),
            num(&g("strike_price")),
            num(&g("dte_days")),
            num(&g("option_price")),
            num(&g("forecast_volatility")),
            num(&g("signal_score")),
            num(&g("expected_return_24h")),
            num(&g("spread")),
            num(&g("directional_move_scale")),
        ),
        "evaluate_option_flow" => Value::Object(st::evaluate_option_flow(
            &g("recent_trades"),
            &g("depth_payload"),
        )),
        "evaluate_underlying_trend" => {
            st::evaluate_underlying_trend(&rows(&g("fast_rows")), &rows(&g("slow_rows")), &g("cfg"))
        }
        "evaluate_low_iv_squeeze" => {
            st::evaluate_low_iv_squeeze(&rows(&g("fast_rows")), &rows(&g("slow_rows")), &g("cfg"))
        }
        "choose_underlying_signal" => {
            st::choose_underlying_signal(&rows(&g("fast_rows")), &rows(&g("slow_rows")), &g("cfg"))
        }
        "derive_dynamic_contract_targets" => {
            Value::Object(st::derive_dynamic_contract_targets(&g("signal"), &g("cfg")))
        }
        "shortlist_option_contracts_with_diagnostics" => {
            let (rows, diag) = st::shortlist_option_contracts_with_diagnostics(
                &g("exchange_info"),
                g("underlying").as_str().unwrap_or(""),
                g("direction").as_str().unwrap_or(""),
                num(&g("spot_price")),
                &g("signal"),
                &g("cfg"),
                g("now_ms").as_i64().unwrap_or(0),
            );
            json!({"rows": rows, "diagnostics": diag})
        }
        "find_skew_peer" => {
            st::find_skew_peer(&g("exchange_info"), &g("contract")).unwrap_or(Value::Null)
        }
        "find_surface_peers" => json!(st::find_surface_peers(
            &g("exchange_info"),
            &g("contract"),
            num(&g("max_peers")) as usize
        )),
        "score_option_contract" => {
            let skew = g("skew_mark_payload");
            let surface: Vec<Value> = g("surface_mark_payloads")
                .as_array()
                .cloned()
                .unwrap_or_default();
            st::score_option_contract(
                &g("contract"),
                &g("mark_payload"),
                &g("ticker_payload"),
                &g("depth_payload"),
                &g("signal"),
                &g("cfg"),
                if skew.is_null() { None } else { Some(&skew) },
                &surface,
                &g("recent_trades"),
            )
        }
        "estimate_option_fee" => json!(st::estimate_option_fee(
            num(&g("option_price")),
            num(&g("index_price")),
            num(&g("quantity")),
            num(&g("unit"))
        )),
        "build_long_option_entry_plan" => st::build_long_option_entry_plan(
            num(&g("ask_price")),
            num(&g("index_price")),
            num(&g("unit")),
            num(&g("min_qty")),
            num(&g("step_size")),
            num(&g("cash_bankroll_usdt")),
            num(&g("entry_fraction")),
            num(&g("capital_limit_usdt")),
        ),
        "floor_to_step" => json!(st::floor_to_step(
            st::dec(num(&g("value"))),
            st::dec(num(&g("step")))
        )
        .to_f64()
        .unwrap_or(f64::NAN)),
        "default_options_config" => oc::default_options_config(),
        "normalize_options_config" => oc::normalize_options_config(&g("raw")),
        "normalize_btc_spread_choice" => json!(oc::normalize_btc_spread_choice(&g("value"))),
        "options_spend_limit" => {
            let available = oc::py_float(&g("available")).unwrap_or(0.0);
            json!(oc::options_spend_limit(&g("cfg"), available))
        }
        "options_budget_text" => json!(oc::options_budget_text(&g("cfg"))),
        "multi_underlying_restore_values" => Value::Object(oc::multi_underlying_restore_values()),
        _ => return None,
    };
    Some(r)
}

#[test]
fn options_port_matches_python_golden() {
    let all = cases();
    let mut failures: Vec<String> = Vec::new();
    let mut checked = 0usize;
    for (index, case) in all.iter().enumerate() {
        let name = case["fn"].as_str().unwrap_or("");
        let args = case["args"].as_object().cloned().unwrap_or_default();
        let Some(actual) = run(name, &args) else {
            failures.push(format!("#{index} {name}: not wired"));
            continue;
        };
        checked += 1;
        let mut out = Vec::new();
        diff("", &case["result"], &actual, &mut out);
        if !out.is_empty() {
            failures.push(format!("#{index} {name}: {}", out.join("; ")));
        }
    }
    let total = failures.len();
    failures.truncate(40);
    assert!(
        total == 0,
        "{total} of {checked} golden cases differ:\n{}",
        failures.join("\n")
    );
}
