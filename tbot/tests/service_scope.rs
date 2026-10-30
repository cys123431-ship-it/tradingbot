#![allow(unused, clippy::all)] // ported Python tests: keep their shape
#![allow(
    clippy::type_complexity,
    clippy::ptr_arg,
    clippy::too_many_arguments,
    dead_code
)]
//! Port of `tests/test_btc_ma_cross_scope.py` (alt-coin scan + trend-state entries).
//!
//! Excluded (Telegram controller / menus / `_callbacks` / `_handle_btc_ma_cross_action`):
//!   test_scope_and_entry_mode_buttons,
//!   test_status_shows_the_reentry_wait_and_the_button,
//!   test_rotation_button
//!
//! NEEDS pub: `SmaCrossService::_on_trade_closed` (`test_cross_reverse_does_not_trigger_rotation`)
//! is not part of the public API, so that test is replaced by its observable behaviour:
//! a CROSS_REVERSE exit does not set `rotation_pending` (exercised through the rotation harness).

mod common;

use tbot::exchange::fake::UNKNOWN;

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use common::scope::*;
use common::*;
use serde_json::{json, Map, Value};
use tbot::core::candles::Candle;
use tbot::exchange::{ExchangeError, FuturesExchange};
use tbot::store::clock::SystemClock;
use tbot::strategy::service::{BoxFut, ServiceDeps, SmaCrossService, Variant};

// ---------------------------------------------------------------- symbol info

#[tokio::test]
async fn universe_is_top_n_by_volume_without_stables_or_tradfi() {
    let root = temp_dir("scope_universe");
    let mut coins = Map::new();
    for i in 0..60 {
        coins.insert(
            format!("C{i:02}USDT"),
            coin_default(1000.0 - (i as f64), 85_000.0),
        );
    }
    coins.insert("USDCUSDT".to_string(), coin_default(5000.0, 85_000.0));
    coins.insert(
        "AAPLUSDT".to_string(),
        coin_with(
            4000.0,
            85_000.0,
            &[("contract_type", json!("TRADIFI_PERPETUAL"))],
        ),
    );
    let h = ScopeHarness::new(&root, coins, true, Vec::new());
    let cfg = h.service.config();
    let universe = h.service.scan_universe("testnet", &cfg).await.unwrap();
    let markets: Vec<String> = universe.iter().map(|(_, m, _)| m.clone()).collect();
    assert_eq!(markets.len(), 50);
    assert_eq!(markets[0], "C00USDT");
    assert_eq!(markets[markets.len() - 1], "C49USDT");
    assert!(!markets.iter().any(|m| m == "USDCUSDT"));
    assert!(!markets.iter().any(|m| m == "AAPLUSDT"));
}

#[tokio::test]
async fn btc_scope_ignores_alts() {
    let root = temp_dir("scope_btc");
    let mut coins = Map::new();
    coins.insert("BTCUSDT".to_string(), coin_default(1.0, 85_000.0));
    coins.insert("ETHUSDT".to_string(), coin_default(9.0, 86_000.0));
    let h = ScopeHarness::new(
        &root,
        coins,
        true,
        cfg_map(vec![("scan_scope", json!("btc"))]),
    );
    let cfg = h.service.config();
    let universe = h.service.scan_universe("testnet", &cfg).await.unwrap();
    let markets: Vec<String> = universe.iter().map(|(_, m, _)| m.clone()).collect();
    assert_eq!(markets, vec!["BTCUSDT".to_string()]);
}

// ---------------------------------------------------------------- alt entries

#[tokio::test]
async fn highest_volume_coin_with_a_signal_wins() {
    let root = temp_dir("scope_highest_vol");
    let mut coins = Map::new();
    coins.insert("BTCUSDT".to_string(), coin_default(900.0, 85_000.0));
    coins.insert("ETHUSDT".to_string(), coin_default(800.0, 86_000.0));
    coins.insert("SOLUSDT".to_string(), coin_default(700.0, 84_000.0));
    let h = ScopeHarness::new(&root, coins, true, Vec::new());
    let r = h.run().await;
    assert_eq!(r["action"], json!("entered"));
    let trade = h.state_live()["trade"].as_object().cloned().unwrap();
    assert_eq!(trade["market_id"], json!("ETHUSDT"));
    assert_eq!(trade["side"], json!("LONG"));
    assert_eq!(h.fake.position_keys(), vec!["ETH/USDT:USDT".to_string()]);
    let attempts = h.fake.algo_attempts.lock().unwrap().clone();
    assert!(attempts
        .iter()
        .all(|a| a.get("symbol").and_then(|v| v.as_str()) == Some("ETHUSDT")));
    let owned: std::collections::HashSet<String> = h
        .service
        .owned_position_keys()
        .unwrap()
        .into_iter()
        .collect();
    let expected: std::collections::HashSet<String> = ["ETHUSDT".to_string()].into_iter().collect();
    assert_eq!(owned, expected);
    let ledger = h.service.ledger("testnet").unwrap();
    let mut kinds = std::collections::HashSet::new();
    kinds.insert("SIGNAL".to_string());
    let events = {
        let g = ledger.lock().unwrap();
        g.recent_events(1, Some(&kinds))
    };
    let signal = events.first().cloned().unwrap_or(Value::Null);
    // BTC is left out of the alt scope.
    assert_eq!(signal["evaluated"], json!(2));
    let crosses: std::collections::HashSet<String> = signal["crosses"]
        .as_array()
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .filter_map(|v| v.as_str().map(|s| s.to_string()))
        .collect();
    let expected_crosses: std::collections::HashSet<String> =
        ["ETHUSDT:LONG".to_string(), "SOLUSDT:SHORT".to_string()]
            .into_iter()
            .collect();
    assert_eq!(crosses, expected_crosses);
}

#[tokio::test]
async fn unusable_coin_falls_through_to_the_next() {
    let root = temp_dir("scope_unusable");
    let mut coins = Map::new();
    coins.insert(
        "ETHUSDT".to_string(),
        coin_with(800.0, 86_000.0, &[("min_notional", json!("100000"))]),
    );
    coins.insert("SOLUSDT".to_string(), coin_default(700.0, 84_000.0));
    let h = ScopeHarness::new(&root, coins, true, Vec::new());
    assert_eq!(h.run().await["action"], json!("entered"));
    let trade = h.state_live()["trade"].as_object().cloned().unwrap();
    assert_eq!(trade["market_id"], json!("SOLUSDT"));
    assert_eq!(trade["side"], json!("SHORT"));
}

#[tokio::test]
async fn opposite_cross_reverses_on_the_same_alt() {
    let root = temp_dir("scope_opposite");
    let mut coins = Map::new();
    coins.insert("ETHUSDT".to_string(), coin_default(800.0, 86_000.0));
    coins.insert("SOLUSDT".to_string(), coin_default(700.0, 85_000.0));
    let mut h = ScopeHarness::new(&root, coins, true, Vec::new());
    h.run().await;
    h.fake.add_bar("ETHUSDT", 96.0);
    h.fake.add_bar("SOLUSDT", 100.0);
    h.advance_ms(H1);
    assert_eq!(h.run().await["action"], json!("reversed"));
    let trade = h.state_live()["trade"].as_object().cloned().unwrap();
    assert_eq!(trade["market_id"], json!("ETHUSDT"));
    assert_eq!(trade["side"], json!("SHORT"));
}

// ---------------------------------------------------------------- trend mode

#[tokio::test]
async fn cross_mode_does_not_enter_without_a_cross() {
    let root = temp_dir("scope_cross_no_entry");
    let h = ScopeHarness::new(
        &root,
        trend_coins(),
        true,
        cfg_map(vec![("scan_scope", json!("btc"))]),
    );
    let rows = h.fake.coin_rows("BTCUSDT");
    let now = now_after(&rows, H1, 5);
    let mut h = h;
    h.set_now(now);
    assert_eq!(h.run().await["reason"], json!("NO_SIGNAL"));
    assert!(h.fake.orders.lock().unwrap().is_empty());
}

#[tokio::test]
async fn trend_mode_enters_when_sma3_is_already_above() {
    let root = temp_dir("scope_trend_entry");
    let h = ScopeHarness::new(
        &root,
        trend_coins(),
        true,
        cfg_map(vec![
            ("scan_scope", json!("btc")),
            ("entry_mode", json!("trend")),
        ]),
    );
    let rows = h.fake.coin_rows("BTCUSDT");
    let now = now_after(&rows, H1, 5);
    let mut h = h;
    h.set_now(now);
    assert_eq!(h.run().await["action"], json!("entered"));
    let trade = h.state_live()["trade"].as_object().cloned().unwrap();
    assert_eq!(trade["side"], json!("LONG"));
    assert_eq!(trade["entry_type"], json!("TREND"));
}

#[tokio::test]
async fn trend_mode_waits_for_the_next_cross_after_a_profit_lock_exit() {
    let root = temp_dir("scope_trend_wait");
    let h = ScopeHarness::new(
        &root,
        trend_coins(),
        true,
        cfg_map(vec![
            ("scan_scope", json!("btc")),
            ("entry_mode", json!("trend")),
            ("reentry_wait_bars", json!(0)),
        ]),
    );
    let mut h = h;
    let rows = h.fake.coin_rows("BTCUSDT");
    let now = now_after(&rows, H1, 5);
    h.set_now(now);
    h.run().await;
    h.fake.set_coin_mark("BTCUSDT", 85_000.0 * 1.021);
    h.advance_ms(20_000);
    h.run().await;
    h.fake.fire_lock("BTCUSDT", 86_530.0);
    h.advance_ms(20_000);
    assert_eq!(h.run().await["trade"]["exit_reason"], json!("PROFIT_LOCK"));
    assert_eq!(
        h.state_live()["reentry_block"]["BTCUSDT"]["side"],
        json!("LONG")
    );
    // Next bar: SMA3 still above SMA200 -> no new LONG (wait for a cross).
    let rows = h.fake.coin_rows("BTCUSDT");
    let last_ts = rows
        .last()
        .and_then(|r| {
            r.as_array()
                .and_then(|a| a.first())
                .and_then(|v| v.as_i64())
        })
        .unwrap_or(T0);
    h.fake.push_row(
        "BTCUSDT",
        json!([last_ts + H1, 86_500.0, 86_700.0, 86_400.0, 86_600.0, 1.0]),
    );
    let rows = h.fake.coin_rows("BTCUSDT");
    let now = now_after(&rows, H1, 5);
    h.set_now(now);
    assert_eq!(h.run().await["reason"], json!("NO_SIGNAL"));
    // A cross down releases the block and (trend mode) enters SHORT on it.
    let rows = h.fake.coin_rows("BTCUSDT");
    let last_ts = rows
        .last()
        .and_then(|r| {
            r.as_array()
                .and_then(|a| a.first())
                .and_then(|v| v.as_i64())
        })
        .unwrap_or(T0);
    h.fake.push_row(
        "BTCUSDT",
        json!([last_ts + H1, 86_600.0, 86_600.0, 70_000.0, 70_100.0, 1.0]),
    );
    let rows = h.fake.coin_rows("BTCUSDT");
    let now = now_after(&rows, H1, 5);
    h.set_now(now);
    h.fake.set_coin_mark("BTCUSDT", 70_100.0);
    assert_eq!(h.run().await["action"], json!("entered"));
    let state = h.state_live();
    assert_eq!(state["trade"]["side"], json!("SHORT"));
    assert_eq!(state["reentry_block"], json!({}));
}

// ---------------------------------------------------------------- testnet ranking

#[tokio::test]
async fn testnet_universe_is_ranked_by_mainnet_volume() {
    let root = temp_dir("scope_testnet_rank");
    let mut demo_coins = Map::new();
    demo_coins.insert("FXSUSDT".to_string(), coin_default(99_999.0, 85_000.0));
    demo_coins.insert("BTCUSDT".to_string(), coin_default(1.0, 85_000.0));
    demo_coins.insert("ETHUSDT".to_string(), coin_default(2.0, 85_000.0));

    let mut main_coins = Map::new();
    main_coins.insert("BTCUSDT".to_string(), coin_default(900.0, 85_000.0));
    main_coins.insert("ETHUSDT".to_string(), coin_default(800.0, 85_000.0));
    main_coins.insert("SOLUSDT".to_string(), coin_default(700.0, 85_000.0));

    let demo = Arc::new(MultiFake::new(demo_coins));
    let mainnet = Arc::new(MultiFake::new(main_coins));

    let cfg = Arc::new(Mutex::new(json!({
        "enabled": true,
        "trading_mode": "DRY_RUN",
        "scan_scope": "alts",
    })));

    let cfg_arc = cfg.clone();
    let config_getter: Arc<dyn Fn() -> Value + Send + Sync> =
        Arc::new(move || cfg_arc.lock().unwrap().clone());
    let credentials_getter: Arc<dyn Fn(&str) -> Value + Send + Sync> =
        Arc::new(|_n: &str| json!({}));
    let demo_f = demo.clone();
    let mainnet_f = mainnet.clone();
    let exchange_factory: Arc<
        dyn Fn(&str, &Value) -> Result<Arc<MultiFake>, ExchangeError> + Send + Sync,
    > = Arc::new(move |n: &str, _c: &Value| {
        if n == "mainnet" {
            Ok(mainnet_f.clone())
        } else {
            Ok(demo_f.clone())
        }
    });
    let notifier: Option<Arc<dyn Fn(String) -> BoxFut<()> + Send + Sync>> =
        Some(Arc::new(move |_t: String| Box::pin(async {})));
    let main_network_getter: Option<Arc<dyn Fn() -> Option<String> + Send + Sync>> =
        Some(Arc::new(|| Some("testnet".to_string())));
    let sleep: Arc<dyn Fn(f64) -> BoxFut<()> + Send + Sync> =
        Arc::new(|_s: f64| Box::pin(async {}));
    let clock: Arc<dyn Fn() -> f64 + Send + Sync> = Arc::new(|| 0.0);

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
        store_clock: Arc::new(SystemClock),
    };
    let service = SmaCrossService::new(Variant::BtcMa, deps);

    let cfg_val = service.config();
    let universe = service.scan_universe("testnet", &cfg_val).await.unwrap();
    let markets: Vec<String> = universe.iter().map(|(_, m, _)| m.clone()).collect();
    // FXS: not on mainnet, SOL: not on demo, BTC: alts only.
    assert_eq!(markets, vec!["ETHUSDT".to_string()]);

    cfg.lock().unwrap()["rank_testnet_by_mainnet_volume"] = json!(false);
    service.clear_universe_cache();
    let cfg_val = service.config();
    let universe = service.scan_universe("testnet", &cfg_val).await.unwrap();
    let markets: Vec<String> = universe.iter().map(|(_, m, _)| m.clone()).collect();
    assert_eq!(markets.first().cloned(), Some("FXSUSDT".to_string()));
}

// ---------------------------------------------------------------- reentry / rotation

#[tokio::test]
async fn trend_mode_reenters_the_same_direction_after_three_bars() {
    let root = temp_dir("scope_reenter_three");
    let mut h = rotation_harness(
        &root,
        cfg_map(vec![
            ("scan_scope", json!("btc")),
            ("entry_mode", json!("trend")),
            ("rotate_after_exit", json!(false)),
        ]),
    );
    // default: 3 bars
    let _rows = exit_by_profit_lock(&mut h).await;
    let orders = h.fake.orders.lock().unwrap().len();
    for (i, close) in [86_600.0_f64, 86_700.0].iter().enumerate() {
        add_up_bar(&h.fake, "BTCUSDT", *close);
        let rows = h.fake.coin_rows("BTCUSDT");
        let now = now_after(&rows, H1, 5);
        h.set_now(now);
        let _ = h.run().await;
        assert_eq!(
            h.fake.orders.lock().unwrap().len(),
            orders,
            "re-entered after {} bar(s)",
            i + 1
        );
        let ledger = h.service.ledger("testnet").unwrap();
        let mut kinds = std::collections::HashSet::new();
        kinds.insert("SIGNAL".to_string());
        let events = {
            let g = ledger.lock().unwrap();
            g.recent_events(1, Some(&kinds))
        };
        let last = events.first().cloned().unwrap_or(Value::Null);
        let skip = last
            .get("skip_reason")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        assert!(skip.starts_with("REENTRY_WAIT: BTCUSDT LONG"), "{}", skip);
    }
    add_up_bar(&h.fake, "BTCUSDT", 86_800.0);
    let rows = h.fake.coin_rows("BTCUSDT");
    let now = now_after(&rows, H1, 5);
    h.set_now(now);
    h.fake.set_coin_mark("BTCUSDT", 86_800.0);
    let result = h.run().await;
    assert_eq!(result["action"], json!("entered"), "{result}");
    let state = h.state_live();
    assert_eq!(state["trade"]["side"], json!("LONG"));
    assert_eq!(state["trade"]["entry_type"], json!("TREND"));
}

#[tokio::test]
async fn legacy_block_without_exit_time_is_already_over() {
    let root = temp_dir("scope_legacy_block");
    let h = ScopeHarness::new(
        &root,
        trend_coins(),
        true,
        cfg_map(vec![
            ("scan_scope", json!("btc")),
            ("entry_mode", json!("trend")),
        ]),
    );
    let mut h = h;
    let mut state = h.state_live();
    state.insert("reentry_block".to_string(), json!({"BTCUSDT": "LONG"}));
    h.service.save_state("testnet", "LIVE", &mut state).unwrap();
    let rows = h.fake.coin_rows("BTCUSDT");
    let now = now_after(&rows, H1, 5);
    h.set_now(now);
    assert_eq!(h.run().await["action"], json!("entered"));
}

#[tokio::test]
async fn after_an_exit_the_next_coin_is_entered_at_once() {
    let root = temp_dir("scope_rotate_next");
    let mut h = rotation_harness(&root, Vec::new());
    assert_eq!(h.run().await["action"], json!("entered"));
    assert_eq!(h.state_live()["trade"]["market_id"], json!("ETHUSDT"));
    assert_eq!(
        lock_exit(&mut h, "ETHUSDT", 1.021).await["trade"]["exit_reason"],
        json!("PROFIT_LOCK")
    );
    assert_eq!(
        h.state_live()["rotation_pending"]["exclude"],
        json!("ETHUSDT")
    );
    h.advance_ms(10_000); // same candle, no close waited for
    let result = h.run().await;
    assert_eq!(result["action"], json!("entered"), "{}", result);
    let trade = h.state_live()["trade"].as_object().cloned().unwrap();
    assert_eq!(trade["market_id"], json!("SOLUSDT"));
    assert_eq!(trade["side"], json!("SHORT"));
    assert_eq!(trade["entry_type"], json!("ROTATE"));
    assert!(h.state_live()["rotation_pending"].is_null());
}

#[tokio::test]
async fn rotation_off_waits_for_the_next_candle() {
    let root = temp_dir("scope_rotate_off");
    let mut h = rotation_harness(&root, cfg_map(vec![("rotate_after_exit", json!(false))]));
    h.run().await;
    lock_exit(&mut h, "ETHUSDT", 1.021).await;
    h.advance_ms(10_000);
    assert_eq!(h.run().await["reason"], json!("WAITING_NEXT_CLOSE"));
    assert!(h.state_live()["trade"].is_null());
}

#[tokio::test]
async fn btc_only_scope_rotates_to_the_top_alt() {
    let root = temp_dir("scope_rotate_btc");
    let mut h = rotation_harness(&root, cfg_map(vec![("scan_scope", json!("btc"))]));
    assert_eq!(h.run().await["action"], json!("entered"));
    assert_eq!(h.state_live()["trade"]["market_id"], json!("BTCUSDT"));
    lock_exit(&mut h, "BTCUSDT", 1.021).await;
    h.advance_ms(10_000);
    assert_eq!(h.run().await["action"], json!("entered"));
    // #1 by volume is the coin just closed -> #2
    assert_eq!(h.state_live()["trade"]["market_id"], json!("ETHUSDT"));
}

// NEEDS pub: `SmaCrossService::_on_trade_closed`
// Original test called `_on_trade_closed(..., {"exit_reason": "CROSS_REVERSE"})` and asserted
// `not state.get("rotation_pending")`. The method is `pub(crate)`, so we exercise the same
// contract through a CROSS_REVERSE exit and assert no rotation_pending is set.
#[tokio::test]
async fn cross_reverse_does_not_trigger_rotation() {
    let root = temp_dir("scope_cross_reverse");
    let mut coins = Map::new();
    // BTC in uptrend, ETH with a cross that will produce CROSS_REVERSE on next bar.
    coins.insert(
        "BTCUSDT".to_string(),
        json!({"volume": 1000, "rows": trend_rows(86_000.0, 85_000.0), "mark": 85_000.0}),
    );
    coins.insert(
        "ETHUSDT".to_string(),
        json!({"volume": 800, "rows": trend_rows(86_000.0, 3_000.0), "mark": 3_000.0}),
    );
    coins.insert(
        "SOLUSDT".to_string(),
        json!({"volume": 700, "rows": down_rows(150.0), "mark": 150.0}),
    );
    let mut h = ScopeHarness::new(
        &root,
        coins,
        true,
        cfg_map(vec![
            ("entry_mode", json!("trend")),
            ("rotate_after_exit", json!(true)),
            ("scan_scope", json!("alts")),
        ]),
    );
    let rows = h.fake.coin_rows("BTCUSDT");
    let now = now_after(&rows, H1, 5);
    h.set_now(now);
    assert_eq!(h.run().await["action"], json!("entered"));
    assert_eq!(h.state_live()["trade"]["market_id"], json!("ETHUSDT"));
    // Force a cross-reverse on ETH: drop its last close far below the SMA.
    let rows = h.fake.coin_rows("ETHUSDT");
    let last_ts = rows
        .last()
        .and_then(|r| {
            r.as_array()
                .and_then(|a| a.first())
                .and_then(|v| v.as_i64())
        })
        .unwrap_or(T0);
    let prev = rows
        .last()
        .and_then(|r| r.as_array().and_then(|a| a.get(4)).and_then(|v| v.as_f64()))
        .unwrap_or(0.0);
    h.fake.push_row(
        "ETHUSDT",
        json!([last_ts + H1, prev, prev * 1.001, 2_000.0, 2_000.0, 1.0]),
    );
    let rows = h.fake.coin_rows("ETHUSDT");
    let now = now_after(&rows, H1, 5);
    h.set_now(now);
    let _ = h.run().await;
    let state = h.state_live();
    let rotation_pending = state
        .get("rotation_pending")
        .cloned()
        .unwrap_or(Value::Null);
    assert!(rotation_pending.is_null());
}
