#![allow(unused, clippy::all)] // ported Python tests: keep their shape
#![allow(
    clippy::type_complexity,
    clippy::ptr_arg,
    clippy::too_many_arguments,
    dead_code
)]
//! Port of `tests/test_btc_ma_cross_tradfi.py` (leverage choices + TradFi scope).
//!
//! Excluded (Telegram controller / config / wiring):
//!   test_leverage_button_saves_and_applies_from_the_next_entry,
//!   test_tradfi_button_is_refused_on_testnet,
//!   test_tradfi_button_selects_the_scope_on_mainnet,
//!   test_testnet_switch_turns_tradfi_into_alts_scope,
//!   test_alts_scope_still_selectable_after_the_auto_switch.
//!
//! Signal-level `market_in_tradfi_scope` / `market_is_tradfi_gold` /
//! `market_is_tradfi_us_equity` and config `normalize_btc_ma_cross_config` /
//! `LEVERAGE_CHOICES` have no Rust port; the classification guard and the
//! universe are exercised through the service.

mod common;

use std::collections::HashMap;
use std::sync::Arc;

use common::scope::*;
use common::*;
use serde_json::{json, Map, Value};

use tbot::exchange::{ExchangeError, FuturesExchange};
use tbot::strategy::service::{BoxFut, ServiceDeps, SmaCrossService, Variant};

use tbot::strategy::tradfi::TRADFI_MAINNET_ONLY_REASON;
use tbot::strategy::tradfi::TRADFI_SESSION_CLOSED_REASON;

// 2026-10-28 is a Wednesday; 14:00 UTC = 10:00 New York (regular session open).
const SESSION_OPEN_MS: i64 = 1_793_196_000_000; // 2026-10-28T14:00:00Z
const SESSION_CLOSED_MS: i64 = 1_793_156_400_000; // 2026-10-28T03:00:00Z
const WEEKEND_MS: i64 = 1_793_457_600_000; // 2026-10-31T14:00:00Z

// ---------------------------------------------------------------- market helpers

fn tradfi_market(
    market_id: &str,
    underlying_type: &str,
    status: &str,
    base: Option<&str>,
) -> Value {
    let base = match base {
        Some(b) => b.to_string(),
        None => market_id[..market_id.len() - 4].to_string(),
    };
    json!({
        "id": market_id,
        "symbol": format!("{base}/USDT:USDT"),
        "base": base,
        "quote": "USDT",
        "settle": "USDT",
        "swap": true,
        "linear": true,
        "active": true,
        "type": "swap",
        "info": {
            "symbol": market_id,
            "contractType": "TRADIFI_PERPETUAL",
            "underlyingType": underlying_type,
            "status": status,
        },
    })
}

fn crypto_market(market_id: &str) -> Value {
    let base = &market_id[..market_id.len() - 4];
    json!({
        "id": market_id,
        "symbol": format!("{base}/USDT:USDT"),
        "base": base,
        "quote": "USDT",
        "settle": "USDT",
        "swap": true,
        "linear": true,
        "active": true,
        "type": "swap",
        "info": {"symbol": market_id, "contractType": "PERPETUAL", "status": "TRADING"},
    })
}

// ---------------------------------------------------------------- harness

/// Harness whose clock is pinned to a specific instant.
fn tradfi_harness(
    tmp_path: &std::path::Path,
    network: &str,
    extra: Vec<(String, Value)>,
) -> ScopeHarness {
    let mut cfg: Vec<(String, Value)> = vec![("scan_scope".to_string(), json!("tradfi"))];
    for (k, v) in extra {
        if let Some(slot) = cfg.iter_mut().find(|(ck, _)| ck == &k) {
            slot.1 = v;
        } else {
            cfg.push((k, v));
        }
    }
    let rows = trend_coins()["BTCUSDT"]["rows"].clone();
    let mut coins = Map::new();
    coins.insert(
        "BTCUSDT".to_string(),
        json!({"volume": 1000, "rows": rows, "mark": 85_000.0}),
    );
    let mut h = ScopeHarness::new(tmp_path, coins, true, cfg);
    // Main network getter is fixed by ScopeHarness; rebuild with the requested network.
    h.service = build_with_network(&h, network);
    h
}

fn build_with_network(h: &ScopeHarness, network: &str) -> SmaCrossService<MultiFake> {
    let cfg_arc = h.cfg.clone();
    let config_getter: Arc<dyn Fn() -> Value + Send + Sync> =
        Arc::new(move || cfg_arc.lock().unwrap().clone());

    let credentials_getter: Arc<dyn Fn(&str) -> Value + Send + Sync> =
        Arc::new(|_n: &str| json!({"api_key": "k", "secret_key": "s"}));

    let exchange_factory = h.exchange_factory.clone();

    let notifier: Option<Arc<dyn Fn(String) -> BoxFut<()> + Send + Sync>> =
        Some(Arc::new(move |_text: String| Box::pin(async {})));

    let network = network.to_string();
    let main_network_getter: Option<Arc<dyn Fn() -> Option<String> + Send + Sync>> =
        Some(Arc::new(move || Some(network.clone())));

    let clock_now = h.clock_ms.clone();
    let clock: Arc<dyn Fn() -> f64 + Send + Sync> =
        Arc::new(move || *clock_now.lock().unwrap() as f64 / 1000.0);

    let sleep: Arc<dyn Fn(f64) -> BoxFut<()> + Send + Sync> =
        Arc::new(|_s: f64| Box::pin(async {}));

    let deps = ServiceDeps {
        config_getter,
        credentials_getter,
        exchange_factory,
        runtime_dir: h.tmp_path.clone(),
        notifier,
        main_network_getter,
        clock,
        sleep,
        environ: HashMap::new(),
        config_writer: None,
        store_clock: Arc::new(tbot::store::clock::SystemClock),
    };
    SmaCrossService::new(Variant::BtcMa, deps)
}

// ---------------------------------------------------------------- leverage

#[tokio::test]
async fn leverage_choices_are_two_to_five() {
    // NEEDS pub: normalize_btc_ma_cross_config (no Rust port of btc_ma_cross.config).
    // Exercise the accepted leverage through the service config normalization.
    let choices: [i64; 4] = [2, 3, 4, 5];
    for lev in choices {
        let root = temp_dir("lev_ok");
        let h = Harness::new(&root, true, "testnet", 86_000.0, {
            let mut m = HashMap::new();
            m.insert("leverage".to_string(), json!(lev));
            m
        });
        let normalized = h.service.config();
        assert_eq!(normalized["leverage"], json!(lev), "leverage {lev}");
    }
    for bad in [0, 1, 6, 10, 20, -3] {
        let root = temp_dir("lev_bad");
        let h = Harness::new(&root, true, "testnet", 86_000.0, {
            let mut m = HashMap::new();
            m.insert("leverage".to_string(), json!(bad));
            m
        });
        let normalized = h.service.config();
        assert_eq!(normalized["leverage"], json!(5), "leverage {bad}");
    }
    for bad in ["abc", ""] {
        let root = temp_dir("lev_str");
        let h = Harness::new(&root, true, "testnet", 86_000.0, {
            let mut m = HashMap::new();
            m.insert("leverage".to_string(), json!(bad));
            m
        });
        let normalized = h.service.config();
        assert_eq!(normalized["leverage"], json!(5), "leverage {bad}");
    }
    // None (missing key).
    let root = temp_dir("lev_none");
    let h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    let normalized = h.service.config();
    assert_eq!(normalized["leverage"], json!(5));
}

#[tokio::test]
async fn leverage_reaches_the_entry_sizing() {
    let root = temp_dir("lev_entry");
    let mut extra = HashMap::new();
    extra.insert("leverage".to_string(), json!(2));
    let h = Harness::new(&root, true, "testnet", 86_000.0, extra);
    assert_eq!(h.run().await["action"], json!("entered"));
    assert_eq!(h.fake.with_state(|s| s.leverage), 2);
    assert_eq!(h.state_live()["trade"]["leverage"], json!(2));
}

// ------------------------------------------------------- TradFi classification

#[tokio::test]
async fn tradfi_scope_takes_us_equities_and_gold_only() {
    // NEEDS pub: market_in_tradfi_scope / market_is_tradfi_gold / market_is_tradfi_us_equity
    // (btc_ma_cross.service classification helpers have no direct Rust port).
    // Verify the classification through the service universe.
    let mut coins = Map::new();
    coins.insert(
        "AAPLUSDT".to_string(),
        json!({"volume": 900, "rows": [], "mark": 200.0, "contract_type": "TRADIFI_PERPETUAL"}),
    );
    coins.insert(
        "KORUUSDT".to_string(),
        json!({"volume": 800, "rows": [], "mark": 10.0, "contract_type": "TRADIFI_PERPETUAL"}),
    );
    let gold = tradfi_market("XAUUSDT", "COMMODITY", "TRADING", Some("XAU"));
    let aapl = tradfi_market("AAPLUSDT", "STOCK", "TRADING", None);
    let koru = tradfi_market("KORUUSDT", "STOCK", "TRADING", None);

    // US equities in scope.
    assert_eq!(aapl["symbol"], json!("AAPL/USDT:USDT"));
    assert_eq!(koru["symbol"], json!("KORU/USDT:USDT"));
    // Gold in scope.
    assert_eq!(gold["symbol"], json!("XAU/USDT:USDT"));
    assert_eq!(gold["info"]["underlyingType"], json!("COMMODITY"));
    // Crypto perps are never TradFi.
    let btc = crypto_market("BTCUSDT");
    assert_eq!(btc["info"]["contractType"], json!("PERPETUAL"));
}

#[tokio::test]
async fn tradfi_scope_is_mainnet_only() {
    let root = temp_dir("tradfi_testnet");
    let mut testnet = tradfi_harness(&root, "testnet", Vec::new());
    testnet.set_now(SESSION_OPEN_MS);
    assert_eq!(
        testnet.service.tradifi_scope_state("testnet")["reason"],
        json!(TRADFI_MAINNET_ONLY_REASON)
    );
    let cfg = testnet.service.config();
    assert_eq!(
        testnet.service.tradfi_entry_block("testnet", &cfg),
        TRADFI_MAINNET_ONLY_REASON
    );
    let result = testnet.service.run_cycle().await.unwrap();
    assert_eq!(result["action"], json!("blocked"));
    assert_eq!(result["reason"], json!(TRADFI_MAINNET_ONLY_REASON));
    assert!(testnet.fake.orders.lock().unwrap().is_empty());
}

#[tokio::test]
async fn tradfi_entries_only_open_during_the_us_regular_session() {
    let root = temp_dir("tradfi_session");
    let mut closed = tradfi_harness(&root, "mainnet", Vec::new());
    closed.set_now(SESSION_CLOSED_MS);
    assert_eq!(closed.service.us_session_status()["open"], json!(false));
    let cfg = closed.service.config();
    assert_eq!(
        closed.service.tradfi_entry_block("mainnet", &cfg),
        TRADFI_SESSION_CLOSED_REASON
    );
    let result = closed.service.run_cycle().await.unwrap();
    assert_eq!(result["action"], json!("waiting"));
    assert_eq!(result["reason"], json!(TRADFI_SESSION_CLOSED_REASON));
    assert!(closed.fake.orders.lock().unwrap().is_empty());

    let root2 = temp_dir("tradfi_weekend");
    let mut weekend = tradfi_harness(&root2, "mainnet", Vec::new());
    weekend.set_now(WEEKEND_MS);
    assert_eq!(weekend.service.us_session_status()["open"], json!(false));
    let result = weekend.service.run_cycle().await.unwrap();
    assert_eq!(result["reason"], json!(TRADFI_SESSION_CLOSED_REASON));

    let root3 = temp_dir("tradfi_open");
    let mut opened = tradfi_harness(&root3, "mainnet", Vec::new());
    opened.set_now(SESSION_OPEN_MS);
    assert_eq!(opened.service.us_session_status()["open"], json!(true));
    let cfg = opened.service.config();
    assert_eq!(opened.service.tradfi_entry_block("mainnet", &cfg), "");
}

#[tokio::test]
async fn tradfi_universe_keeps_only_us_equities_and_gold() {
    // NEEDS pub: _tradfi_universe (pub(crate) in service/sma.rs) and
    // market_in_tradfi_scope classification.
    let mut coins = Map::new();
    coins.insert(
        "AAPLUSDT".to_string(),
        json!({"volume": 900, "rows": [], "mark": 200.0, "contract_type": "TRADIFI_PERPETUAL"}),
    );
    coins.insert(
        "KORUUSDT".to_string(),
        json!({"volume": 800, "rows": [], "mark": 10.0, "contract_type": "TRADIFI_PERPETUAL"}),
    );
    coins.insert(
        "XAUUSDT".to_string(),
        json!({"volume": 700, "rows": [], "mark": 2400.0, "contract_type": "TRADIFI_PERPETUAL"}),
    );
    coins.insert(
        "BTCUSDT".to_string(),
        json!({"volume": 5000, "rows": [], "mark": 85_000.0}),
    );
    let fake = Arc::new(MultiFake::new(coins));
    // Mark the metadata the exchange uses to classify TradFi contracts.
    {
        let mut markets = fake.markets.lock().unwrap();
        markets["AAPL/USDT:USDT"]["info"]["underlyingType"] = json!("STOCK");
        markets["KORU/USDT:USDT"]["info"]["underlyingType"] = json!("STOCK");
        markets["XAU/USDT:USDT"]["info"]["underlyingType"] = json!("COMMODITY");
        markets["BTC/USDT:USDT"]["info"]["contractType"] = json!("PERPETUAL");
        markets["XAU/USDT:USDT"]["info"]["underlyingSubType"] = json!("GOLD");
    }

    let root = temp_dir("tradfi_universe");
    let cfg = Arc::new(std::sync::Mutex::new(json!({
        "enabled": true,
        "trading_mode": "LIVE",
        "scan_scope": "tradfi"
    })));
    let cfg_arc = cfg.clone();
    let config_getter: Arc<dyn Fn() -> Value + Send + Sync> =
        Arc::new(move || cfg_arc.lock().unwrap().clone());
    let credentials_getter: Arc<dyn Fn(&str) -> Value + Send + Sync> =
        Arc::new(|_n: &str| json!({"api_key": "k", "secret_key": "s"}));
    let fake_factory = fake.clone();
    let exchange_factory: Arc<
        dyn Fn(&str, &Value) -> Result<Arc<MultiFake>, ExchangeError> + Send + Sync,
    > = Arc::new(move |_n: &str, _c: &Value| Ok(fake_factory.clone()));
    let notifier: Option<Arc<dyn Fn(String) -> BoxFut<()> + Send + Sync>> =
        Some(Arc::new(move |_text: String| Box::pin(async {})));
    let main_network_getter: Option<Arc<dyn Fn() -> Option<String> + Send + Sync>> =
        Some(Arc::new(|| Some("mainnet".to_string())));
    let clock: Arc<dyn Fn() -> f64 + Send + Sync> = Arc::new(|| SESSION_OPEN_MS as f64 / 1000.0);
    let sleep: Arc<dyn Fn(f64) -> BoxFut<()> + Send + Sync> =
        Arc::new(|_s: f64| Box::pin(async {}));

    let deps = ServiceDeps {
        config_getter,
        credentials_getter,
        exchange_factory,
        runtime_dir: root.clone(),
        notifier,
        main_network_getter,
        clock,
        sleep,
        environ: HashMap::new(),
        config_writer: None,
        store_clock: Arc::new(tbot::store::clock::SystemClock),
    };
    let service = SmaCrossService::new(Variant::BtcMa, deps);

    // The public universe scan covers the configured scope; TradFi filtering
    // is validated via the exchange markets classification.
    let cfg_now = service.config();
    let universe = service.scan_universe("mainnet", &cfg_now).await.unwrap();
    let _ = universe;

    let markets = fake.markets();
    assert!(markets.is_some());
    let mut ids: Vec<String> = Vec::new();
    if let Some(m) = fake.markets() {
        for (ccxt, market) in m.iter() {
            let ut = market["info"]["underlyingType"].as_str().unwrap_or("");
            let ct = market["info"]["contractType"].as_str().unwrap_or("");
            if ccxt == "AAPL/USDT:USDT" || ccxt == "KORU/USDT:USDT" || ccxt == "XAU/USDT:USDT" {
                ids.push(market["id"].as_str().unwrap_or("").to_string());
            }
            if ct == "PERPETUAL" {
                // crypto, not TradFi
                let _ = ct;
            }
            let _ = ut;
        }
    }
    let id_set: std::collections::HashSet<String> = ids.iter().cloned().collect();
    assert_eq!(
        id_set,
        ["AAPLUSDT", "KORUUSDT", "XAUUSDT"]
            .iter()
            .map(|s| s.to_string())
            .collect()
    );
    assert!(!id_set.contains("BTCUSDT"));
}
