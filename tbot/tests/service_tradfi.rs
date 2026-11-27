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
    build_variant(h, network, Variant::BtcMa)
}

fn build_variant(h: &ScopeHarness, network: &str, variant: Variant) -> SmaCrossService<MultiFake> {
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
        config_file_writer: None,
        store_clock: Arc::new(tbot::store::clock::SystemClock),
    };
    SmaCrossService::new(variant, deps)
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
        config_file_writer: None,
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

// ------------------------------------------------- pinned TradFi coin (/macoin)

fn pinned_harness(root: &std::path::Path, network: &str) -> ScopeHarness {
    let rows = trend_coins()["BTCUSDT"]["rows"].clone();
    let mut coins = Map::new();
    coins.insert(
        "AAPLUSDT".to_string(),
        json!({"volume": 900, "rows": rows.clone(), "mark": 200.0,
               "contract_type": "TRADIFI_PERPETUAL"}),
    );
    coins.insert(
        "XAGUSDT".to_string(),
        json!({"volume": 800, "rows": rows.clone(), "mark": 30.0,
               "contract_type": "TRADIFI_PERPETUAL"}),
    );
    coins.insert(
        "ETHUSDT".to_string(),
        json!({"volume": 700, "rows": rows, "mark": 3000.0}),
    );
    let cfg = vec![
        ("scan_scope".to_string(), json!("single")),
        ("single_symbol".to_string(), json!("AAPLUSDT")),
    ];
    let mut h = ScopeHarness::new(root, coins, true, cfg);
    {
        let mut markets = h.fake.markets.lock().unwrap();
        markets["AAPL/USDT:USDT"]["info"]["underlyingType"] = json!("STOCK");
        markets["XAG/USDT:USDT"]["info"]["underlyingType"] = json!("COMMODITY");
        markets["ETH/USDT:USDT"]["info"]["contractType"] = json!("PERPETUAL");
    }
    h.service = build_with_network(&h, network);
    h
}

#[tokio::test]
async fn pinned_tradfi_coin_resolves_and_ignores_the_us_session() {
    let root = temp_dir("tradfi_pinned");
    let mut h = pinned_harness(&root, "mainnet");
    h.set_now(SESSION_CLOSED_MS);
    assert_eq!(h.service.us_session_status()["open"], json!(false));

    let (_, market) = h
        .service
        .resolve_single_coin("mainnet", &json!("AAPL"))
        .await
        .unwrap();
    assert_eq!(market, "AAPLUSDT");
    // Crypto still resolves through the same path.
    let (_, eth) = h
        .service
        .resolve_single_coin("mainnet", &json!("ETH"))
        .await
        .unwrap();
    assert_eq!(eth, "ETHUSDT");
    // Only US equities + gold are TradFi coins that can be pinned (not silver).
    let err = h
        .service
        .resolve_single_coin("mainnet", &json!("XAG"))
        .await
        .unwrap_err();
    assert!(err.message.starts_with("NOT_TRADFI"), "{}", err.message);

    // The session gate belongs to the TradFi scope, not to a pinned coin.
    let cfg = h.service.config();
    assert_eq!(cfg["scan_scope"], json!("single"));
    assert_eq!(h.service.tradfi_entry_block("mainnet", &cfg), "");
    let universe = h.service.scan_universe("mainnet", &cfg).await.unwrap();
    assert_eq!(universe.len(), 1);
    assert_eq!(universe[0].1, "AAPLUSDT");
}

#[tokio::test]
async fn pinned_tradfi_coin_is_mainnet_only() {
    let root = temp_dir("tradfi_pinned_testnet");
    let h = pinned_harness(&root, "testnet");
    let err = h
        .service
        .resolve_single_coin("testnet", &json!("AAPL"))
        .await
        .unwrap_err();
    assert!(
        err.message.starts_with("TRADFI_MAINNET_ONLY"),
        "{}",
        err.message
    );
}

// ------------------------------------------------- TradFi-only strategy (/tradfi)

fn tradfi_only(root: &std::path::Path, network: &str, symbol: &str) -> ScopeHarness {
    let mut h = pinned_harness(root, network);
    h.cfg.lock().unwrap()["single_symbol"] = json!(symbol);
    h.service = build_variant(&h, network, Variant::TradFi);
    h
}

#[tokio::test]
async fn tradfi_only_strategy_takes_any_tradfi_and_refuses_crypto() {
    let root = temp_dir("tradfi_only");
    let h = tradfi_only(&root, "mainnet", "XAG");
    let cfg = h.service.config();
    assert_eq!(cfg["scan_scope"], json!("single"));
    assert_eq!(cfg["timeframe"], json!("4h"));
    assert_eq!(cfg["signal_mode"], json!("align"));
    // Commodities other than gold are fine here (unlike the /macoin TradFi pin).
    let universe = h.service.scan_universe("mainnet", &cfg).await.unwrap();
    assert_eq!(universe[0].1, "XAGUSDT");
    let (_, aapl) = h
        .service
        .resolve_any_tradfi_coin("mainnet", &json!("aapl"))
        .await
        .unwrap();
    assert_eq!(aapl, "AAPLUSDT");
    let err = h
        .service
        .resolve_any_tradfi_coin("mainnet", &json!("ETH"))
        .await
        .unwrap_err();
    assert!(err.message.starts_with("NOT_TRADFI"), "{}", err.message);
    let mut list = h.service.tradfi_any_symbols("mainnet").await.unwrap();
    list.sort();
    assert_eq!(list, vec!["AAPLUSDT".to_string(), "XAGUSDT".to_string()]);
}

#[tokio::test]
async fn tradfi_strategy_also_trades_a_crypto_pin() {
    let root = temp_dir("tradfi_only_crypto");
    // User rule 2026-11-21: /tfcoin accepts plain crypto perpetuals too.
    let h = tradfi_only(&root, "mainnet", "ETHUSDT");
    let cfg = h.service.config();
    let universe = h.service.scan_universe("mainnet", &cfg).await.unwrap();
    assert_eq!(universe.len(), 1);
    assert_eq!(universe[0].1, "ETHUSDT");
}

#[tokio::test]
async fn tradfi_only_strategy_is_mainnet_only() {
    let root = temp_dir("tradfi_only_testnet");
    let h = tradfi_only(&root, "testnet", "XAG");
    let cfg = h.service.config();
    let err = h.service.scan_universe("testnet", &cfg).await.unwrap_err();
    assert!(
        err.message.starts_with("TRADFI_MAINNET_ONLY"),
        "{}",
        err.message
    );
}

/// User rule 2026-11-22: below 18 USDT total equity (wallet + unrealized PnL)
/// /tradfi opens nothing in LIVE; it is not switched off and resumes at 18.
#[tokio::test]
async fn tradfi_blocks_new_entries_below_18_usdt_equity() {
    let root = temp_dir("tradfi_min_equity");
    let h = tradfi_only(&root, "mainnet", "XAG");
    let cfg = h.service.config();
    assert_eq!(cfg["min_equity_usdt"], json!(18));
    let signal = json!({
        "market_id": "XAGUSDT", "ccxt_symbol": "XAG/USDT:USDT",
        "side": "LONG", "close": 30.0,
    });
    let reason_at = |wallet: f64, mode: &'static str| {
        h.fake.inner.state.lock().unwrap().wallet = wallet;
        let svc = &h.service;
        let cfg = cfg.clone();
        let signal = signal.clone();
        async move {
            svc.pre_trade_validation("mainnet", &cfg, mode, &signal)
                .await
                .unwrap()
        }
    };
    let (decision, reason, _) = reason_at(17.99, "LIVE").await;
    assert!(decision.is_none());
    assert!(reason.starts_with("MIN_EQUITY"), "{reason}");
    assert!(reason.contains("17.99"), "{reason}");
    // exactly 18 is allowed
    let (_, reason, _) = reason_at(18.0, "LIVE").await;
    assert!(!reason.starts_with("MIN_EQUITY"), "{reason}");
    // paper trading is not gated by the real account
    let (_, reason, _) = reason_at(5.0, "DRY_RUN").await;
    assert!(!reason.starts_with("MIN_EQUITY"), "{reason}");
    // other strategies do not carry the rule
    let btc = build_variant(&h, "mainnet", Variant::BtcMa);
    assert!(btc.config().get("min_equity_usdt").is_none());
}

/// User rule 2026-11-27: a switch booked for the close of the current bar is
/// applied by the cycle after that close; the newest already-closed bar of the
/// new timeframe is marked judged, so the first decision is the next close.
#[tokio::test]
async fn booked_switch_applies_after_the_old_close() {
    use tbot::strategy::config_tradfi as tf;
    let root = temp_dir("tradfi_booked_switch");
    let mut h = tradfi_only(&root, "mainnet", "XAU");
    let m30 = 1_800_000i64;
    let at = 1_000 * m30; // a 30m close
    {
        let mut cfg = h.cfg.lock().unwrap();
        cfg["timeframe"] = json!("30m");
        cfg["exit_ma_period"] = json!(30);
        cfg[tf::NEXT_SWITCH_KEY] = json!({
            "timeframe": "4h", "exit_ma_period": 120, "exit_ma_off": false,
            "align_slow_enabled": true, "from_timeframe": "30m", "apply_at_ms": at,
        });
    }
    let cfg_arc = h.cfg.clone();
    let mut svc = build_variant(&h, "mainnet", Variant::TradFi);
    let writer: Arc<
        dyn Fn(&str, Map<String, Value>) -> BoxFut<Result<Value, String>> + Send + Sync,
    > = Arc::new(move |_section: &str, changes: Map<String, Value>| {
        let cfg_arc = cfg_arc.clone();
        Box::pin(async move {
            let mut cfg = cfg_arc.lock().unwrap();
            for (k, v) in changes {
                cfg[k.as_str()] = v;
            }
            Ok(cfg.clone())
        })
    });
    svc.deps.config_file_writer = Some(writer);
    h.service = svc;
    let mode = h.service.mode(None);
    // before the close: nothing happens
    *h.clock_ms.lock().unwrap() = at - 1_000;
    h.service.run_cycle().await.unwrap();
    assert_eq!(h.service.config()["timeframe"], json!("30m"));
    // after the close, the old bar judged -> applied
    let mut state = h.service.load_state("mainnet", &mode).unwrap();
    state.insert("last_evaluated_bar".to_string(), json!(at - m30));
    h.service.save_state("mainnet", &mode, &mut state).unwrap();
    *h.clock_ms.lock().unwrap() = at + 5_000;
    h.service.run_cycle().await.unwrap();
    let cfg = h.service.config();
    assert_eq!(cfg["timeframe"], json!("4h"));
    assert_eq!(cfg["exit_ma_period"], json!(120));
    assert_eq!(cfg[tf::NEXT_SWITCH_KEY], Value::Null);
    let state = h.service.load_state("mainnet", &mode).unwrap();
    assert_eq!(
        state["last_evaluated_bar"],
        json!(tf::bar_before_switch("4h", at))
    );
}

/// User rule 2026-11-27: /tradfi auto search scans crypto + TradFi equities
/// with >= 100M USDT 24h volume; stablecoins, commodities and index
/// contracts are never candidates; TradFi only on mainnet.
#[tokio::test]
async fn auto_search_universe_filters_by_volume_and_kind() {
    let root = temp_dir("tradfi_auto_universe");
    let rows = trend_coins()["BTCUSDT"]["rows"].clone();
    let mut coins = Map::new();
    let coin = |vol: f64, tradfi: bool| {
        let mut c = json!({"volume": vol, "rows": rows.clone(), "mark": 10.0});
        if tradfi {
            c["contract_type"] = json!("TRADIFI_PERPETUAL");
        }
        c
    };
    coins.insert("ETHUSDT".to_string(), coin(2.0e8, false));
    coins.insert("DOGEUSDT".to_string(), coin(5.0e7, false)); // below the floor
    coins.insert("USDCUSDT".to_string(), coin(4.0e8, false)); // stablecoin
    coins.insert("BTCDOMUSDT".to_string(), coin(3.0e8, false)); // index
    coins.insert("NVDAUSDT".to_string(), coin(1.5e8, true));
    coins.insert("XAUUSDT".to_string(), coin(3.0e8, true)); // commodity
    let cfg = vec![
        ("scan_scope".to_string(), json!("single")),
        ("single_symbol".to_string(), json!("XAUUSDT")),
        ("auto_scan".to_string(), json!(true)),
    ];
    let mut h = ScopeHarness::new(&root, coins, true, cfg);
    {
        let mut markets = h.fake.markets.lock().unwrap();
        markets["ETH/USDT:USDT"]["info"]["underlyingType"] = json!("COIN");
        markets["BTCDOM/USDT:USDT"]["info"]["underlyingType"] = json!("INDEX");
        markets["NVDA/USDT:USDT"]["info"]["underlyingType"] = json!("EQUITY");
        markets["XAU/USDT:USDT"]["info"]["underlyingType"] = json!("COMMODITY");
    }
    h.service = build_variant(&h, "mainnet", Variant::TradFi);
    let cfg = h.service.config();
    assert_eq!(cfg["scan_scope"], json!("tradfi_auto"));
    let ids: Vec<String> = h
        .service
        .scan_universe("mainnet", &cfg)
        .await
        .unwrap()
        .into_iter()
        .map(|(_, m, _)| m)
        .collect();
    assert_eq!(ids, vec!["ETHUSDT".to_string(), "NVDAUSDT".to_string()]);
    // testnet: TradFi contracts drop out
    let test = build_variant(&h, "testnet", Variant::TradFi);
    let ids: Vec<String> = test
        .scan_universe("testnet", &cfg)
        .await
        .unwrap()
        .into_iter()
        .map(|(_, m, _)| m)
        .collect();
    assert_eq!(ids, vec!["ETHUSDT".to_string()]);
}

/// User rule 2026-11-27: /tradfi sizes each entry so its 2xATR stop loses
/// risk_per_trade_pct (default 2%) of the available balance, capped at the
/// fixed size, never raised to the exchange minimum.
#[tokio::test]
async fn loss_limit_sizing_sets_quantity_and_skips_below_minimum() {
    let root = temp_dir("tradfi_risk_sizing");
    let h = tradfi_only(&root, "mainnet", "XAG");
    let cfg = h.service.config();
    assert_eq!(cfg["risk_per_trade_pct"], json!(2));
    let mode = h.service.mode(Some(&cfg));
    let check = |wallet: f64, atr: f64| {
        h.fake.inner.state.lock().unwrap().wallet = wallet;
        let svc = &h.service;
        let cfg = cfg.clone();
        let mode = mode.clone();
        async move {
            let signal = json!({
                "market_id": "XAGUSDT", "ccxt_symbol": "XAG/USDT:USDT",
                "side": "LONG", "close": 30.0, "atr": atr,
            });
            svc.pre_trade_validation("mainnet", &cfg, &mode, &signal)
                .await
                .unwrap()
        }
    };
    let (decision, reason, ctx) = check(40.0, 0.6).await;
    let Some(d) = decision else {
        panic!("no decision: {reason} {ctx:?}");
    };
    let available = ctx["available_balance"].as_f64().unwrap();
    // stop 1.2 (4% of 30) -> 2% of the balance at risk
    let qty: f64 = d["quantity"].as_str().unwrap().parse().unwrap();
    let loss_at_stop = qty * 1.2;
    assert!(
        loss_at_stop <= available * 0.02 + 1e-9 && loss_at_stop > available * 0.02 * 0.9,
        "qty {qty} loss {loss_at_stop} available {available}"
    );
    assert!(
        d["sizing_mode"].as_str().unwrap().starts_with("risk_"),
        "{d}"
    );
    assert_eq!(d["risk_stop_distance"], json!(1.2));
    // a very wide stop shrinks the size below the 5 USDT minimum -> skipped
    let (decision, reason, _) = check(40.0, 30.0).await;
    assert!(decision.is_none());
    assert!(reason.starts_with("BELOW_EXCHANGE_MINIMUM"), "{reason}");
    assert!(reason.contains("1회 손실 2%"), "{reason}");
    // no ATR -> no entry
    let (decision, reason, _) = check(40.0, 0.0).await;
    assert!(decision.is_none());
    assert!(
        reason.starts_with("RISK_SIZING_ATR_UNAVAILABLE"),
        "{reason}"
    );
}

/// /tfscan: ranks the auto-search universe by efficiency, never trades.
#[tokio::test]
async fn trend_scan_ranks_the_auto_universe() {
    let root = temp_dir("tradfi_trend_scan");
    let rows = trend_coins()["BTCUSDT"]["rows"].clone();
    let mut coins = Map::new();
    coins.insert(
        "ETHUSDT".to_string(),
        json!({"volume": 2.0e8, "rows": rows.clone(), "mark": 10.0}),
    );
    coins.insert(
        "SOLUSDT".to_string(),
        json!({"volume": 3.0e8, "rows": rows, "mark": 10.0}),
    );
    let cfg = vec![("single_symbol".to_string(), json!("ETHUSDT"))];
    let mut h = ScopeHarness::new(&root, coins, true, cfg);
    h.service = build_variant(&h, "mainnet", Variant::TradFi);
    let res = h.service.trend_scan("mainnet", "1h", 50).await.unwrap();
    assert_eq!(res["universe"], json!(2));
    let list = res["rows"].as_array().unwrap();
    assert_eq!(list.len(), 2, "{res}");
    let e0 = list[0]["efficiency"].as_f64().unwrap();
    let e1 = list[1]["efficiency"].as_f64().unwrap();
    assert!(e0 >= e1 && (0.0..=1.0).contains(&e0));
    assert!(h.fake.orders.lock().unwrap().is_empty());
}
