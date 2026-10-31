#![allow(
    clippy::type_complexity,
    clippy::ptr_arg,
    clippy::too_many_arguments,
    dead_code
)]
//! Port of `tests/test_btc_ma_cross.py` (service + signal tests).
//!
//! Excluded (Telegram controller / wiring / daily report / menus):
//!   test_telegram_menu_timeframe_emergency_stop_and_exclusivity,
//!   test_pullback_on_turns_ma_cross_off, test_wiring_keyboard_config_stop_and_report,
//!   test_daily_report_has_ma_cross_section, test_sma_buttons_and_header,
//!   test_preset_button_sets_the_fast_period_too,
//!   test_invalid_periods_are_rejected_without_changing_config,
//!   test_sma_help_lists_the_text_command, test_large_slow_period_lifts_candle_history.
//!
//! Signal-level `evaluate_cross` / `profit_lock_target` / `emergency_stop_price` /
//! `sma` / `market_is_flat` live in the Python `btc_ma_cross.signals` module which
//! has no Rust port; the cross/lock/emergency behaviour is exercised through the
//! service in the tests below (entry stop price, profit-lock ratchet, reverse).

mod common;

use std::collections::HashMap;

use common::*;
use serde_json::{json, Map, Value};
use tbot::exchange::FuturesExchange;

use tbot::strategy::service;

// ---------------------------------------------------------------- service tests

#[tokio::test]
async fn entry_uses_half_the_margin_at_5x_with_emergency_stop() {
    let root = temp_dir("entry");
    let h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    let r = h.run().await;
    assert_eq!(r["action"], json!("entered"));
    let state = h.state_live();
    let trade = state["trade"].as_object().cloned().unwrap();
    assert_eq!(trade["side"], json!("LONG"));
    let qty: f64 = trade["quantity"].as_str().unwrap().parse().unwrap();
    assert!((qty - 0.147).abs() < 1e-9);
    assert_eq!(h.fake.with_state(|s| s.leverage), 5);
    assert_eq!(h.fake.with_state(|s| s.margin_type.clone()), "isolated");
    let attempts = h.fake.with_state(|s| s.algo_attempts.clone());
    let stops: Vec<&Value> = attempts
        .iter()
        .filter(|a| a.get("type").and_then(|v| v.as_str()) == Some("STOP_MARKET"))
        .collect();
    assert_eq!(stops.len(), 1);
    assert_eq!(stops[0]["closePosition"], json!("true"));
    assert_eq!(stops[0]["triggerPrice"], json!("79900.0"));
    assert_eq!(stops[0]["workingType"], json!("MARK_PRICE"));
    assert!(attempts
        .iter()
        .all(|a| a.get("type").and_then(|v| v.as_str()) != Some("TAKE_PROFIT_MARKET")));
}

#[tokio::test]
async fn emergency_stop_off_places_no_stop() {
    let root = temp_dir("no_stop");
    let mut extra = HashMap::new();
    extra.insert("emergency_stop_roi_percent".to_string(), json!(0));
    let h = Harness::new(&root, true, "testnet", 86_000.0, extra);
    h.run().await;
    assert_eq!(h.state_live()["trade"]["stop_price"], json!(""));
    assert!(h.fake.with_state(|s| s.algo_attempts.clone()).is_empty());
}

#[tokio::test]
async fn profit_lock_ratchets_and_replaces_the_previous_lock() {
    let root = temp_dir("lock_ratchet");
    let h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    h.run().await;
    h.fake.with_state(|s| s.mark = 85_000.0 * 1.011);
    let _ = h.now_ms + 20_000;
    h.run().await;
    let trade = h.state_live()["trade"].as_object().cloned().unwrap();
    assert_eq!(trade["lock_roi"], json!("4.0"));
    assert_eq!(trade["lock_price"], json!("85680.0"));
    let attempts = h.fake.with_state(|s| s.algo_attempts.clone());
    let lock: Vec<&Value> = attempts
        .iter()
        .filter(|a| a.get("reduceOnly").and_then(|v| v.as_str()) == Some("true"))
        .collect();
    assert!(!lock.is_empty());
    assert_eq!(lock[0]["quantity"], trade["quantity"]);
    assert_eq!(lock[0]["workingType"], json!("CONTRACT_PRICE"));
    let first_lock = trade["lock_client_algo_id"].as_str().unwrap().to_string();

    h.fake.with_state(|s| s.mark = 85_000.0 * 1.021);
    h.run().await;
    let trade = h.state_live()["trade"].as_object().cloned().unwrap();
    assert_eq!(trade["lock_roi"], json!("9.0"));
    assert_eq!(trade["lock_price"], json!("86530.0"));
    let algos = h.fake.algos_snapshot();
    assert_eq!(algos[&first_lock]["algoStatus"], json!("CANCELED"));

    h.fake.with_state(|s| s.mark = 85_000.0 * 1.012);
    h.run().await;
    assert_eq!(h.state_live()["trade"]["lock_roi"], json!("9.0"));
}

#[tokio::test]
async fn profit_lock_exit_then_wait_for_the_next_cross() {
    let root = temp_dir("lock_exit");
    let h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    h.run().await;
    h.fake.with_state(|s| s.mark = 85_000.0 * 1.021);
    h.run().await;
    let lock_id = h.state_live()["trade"]["lock_client_algo_id"]
        .as_str()
        .unwrap()
        .to_string();
    h.fake.fire(&lock_id, 86_530.0);
    let result = h.run().await;
    assert_eq!(result["trade"]["exit_reason"], json!("PROFIT_LOCK"));
    let net_pnl: f64 = result["trade"]["net_pnl"]
        .as_str()
        .unwrap()
        .parse()
        .unwrap();
    assert!(net_pnl > 0.0);
    let orders = h.fake.with_state(|s| s.orders.len());
    assert_eq!(h.run().await["reason"], json!("WAITING_NEXT_CLOSE"));
    assert_eq!(h.fake.with_state(|s| s.orders.len()), orders);
    let algos = h.fake.algos_snapshot();
    assert!(algos
        .values()
        .all(|o| o.get("algoStatus").and_then(|v| v.as_str()) != Some("NEW")));
}

#[tokio::test]
async fn opposite_cross_closes_and_reverses_immediately() {
    let root = temp_dir("opposite");
    let mut h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    h.run().await;
    h.next_bar(83_000.0);
    let result = h.run().await;
    assert_eq!(result["action"], json!("reversed"), "{}", result);
    let ledger = h.service.ledger("testnet").unwrap();
    let closed = ledger
        .lock()
        .unwrap()
        .trades(Some("LIVE"), None, None, None, Some("CLOSED"))
        .unwrap();
    let last = closed.last().unwrap();
    assert_eq!(last["side"], json!("LONG"));
    assert_eq!(last["exit_reason"], json!("CROSS_REVERSE"));
    let trade = h.state_live();
    assert_eq!(trade["trade"]["side"], json!("SHORT"));
    assert_eq!(
        h.fake
            .with_state(|s| s.position.as_ref().map(|p| p["side"].clone())),
        Some(json!("short"))
    );
    let orders = h.fake.with_state(|s| s.orders.clone());
    assert!(orders
        .iter()
        .any(|o| o["clientOrderId"].as_str().unwrap_or("").contains("close")));
}

#[tokio::test]
async fn emergency_stop_failure_triggers_failsafe() {
    let root = temp_dir("failsafe");
    let h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    h.fake.with_state(|s| s.fail_sl_creates = 99);
    let result = h.run().await;
    assert_eq!(result["action"], json!("exited"));
    assert_eq!(result["trade"]["exit_reason"], json!("FAILSAFE"));
    assert!(h.fake.with_state(|s| s.position.is_none()));
}

#[tokio::test]
async fn below_exchange_minimum_is_skipped() {
    let root = temp_dir("below_min");
    let h = Harness::new(&root, true, "mainnet", 86_000.0, HashMap::new());
    h.fake.with_state(|s| s.wallet = 10.0);
    let result = h.run().await;
    let reason = result["reason"].as_str().unwrap_or("");
    assert!(reason.starts_with("BELOW_EXCHANGE_MINIMUM"), "{reason}");
    assert!(h.fake.with_state(|s| s.orders.is_empty()));
}

#[tokio::test]
async fn any_account_position_blocks_entry() {
    let root = temp_dir("account_pos");
    let h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    h.fake.with_state(|s| {
        s.other_positions = vec![json!({
            "symbol": "ETH/USDT:USDT",
            "side": "long",
            "contracts": 1.0,
            "entryPrice": 3000
        })];
    });
    let result = h.run().await;
    let reason = result["reason"].as_str().unwrap_or("");
    assert!(reason.starts_with("ACCOUNT_POSITION_EXISTS"), "{reason}");
}

#[tokio::test]
async fn dry_run_simulates_lock_exit_without_orders() {
    let root = temp_dir("dry_lock");
    let h = Harness::new(&root, false, "testnet", 86_000.0, HashMap::new());
    assert_eq!(h.run().await["action"], json!("entered"));
    let trade = h.state("DRY_RUN")["trade"].as_object().cloned().unwrap();
    let entry: f64 = trade["entry_price"].as_str().unwrap().parse().unwrap();
    let entry_ms = trade["entry_ms"].as_i64().unwrap();
    let t1 = entry_ms + M1;
    h.fake.with_state(|s| {
        s.m1 = vec![
            json!([t1, entry, entry * 1.021, entry, entry * 1.02, 1.0]),
            json!([
                t1 + M1,
                entry * 1.02,
                entry * 1.02,
                entry * 1.015,
                entry * 1.016,
                1.0
            ]),
        ];
    });
    let result = h.run().await;
    assert_eq!(result["action"], json!("exited"));
    assert_eq!(result["trade"]["exit_reason"], json!("PROFIT_LOCK"));
    assert!(h.fake.with_state(|s| s.orders.is_empty()));
    assert!(h.fake.with_state(|s| s.algo_attempts.is_empty()));
    let net_pnl: f64 = result["trade"]["net_pnl"]
        .as_str()
        .unwrap()
        .parse()
        .unwrap();
    assert!(net_pnl > 0.0);
}

#[tokio::test]
async fn dry_run_reverses_on_opposite_cross() {
    let root = temp_dir("dry_reverse");
    let mut h = Harness::new(&root, false, "testnet", 86_000.0, HashMap::new());
    h.run().await;
    h.next_bar(83_000.0);
    assert_eq!(h.run().await["action"], json!("reversed"));
    assert_eq!(h.state("DRY_RUN")["trade"]["side"], json!("SHORT"));
    assert!(h.fake.with_state(|s| s.orders.is_empty()));
}

#[tokio::test]
async fn owned_keys_and_client_prefix() {
    let root = temp_dir("owned_keys");
    let h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    assert!(h.service.owned_position_keys().unwrap().is_empty());
    h.run().await;
    let keys = h.service.owned_position_keys().unwrap();
    assert_eq!(keys, vec!["BTCUSDT".to_string()]);
    let attempts = h.fake.with_state(|s| s.algo_attempts.clone());
    assert!(attempts.iter().all(|a| a["clientAlgoId"]
        .as_str()
        .unwrap_or("")
        .starts_with("btcma-")));
    let orders = h.fake.with_state(|s| s.orders.clone());
    assert!(orders.iter().all(|o| o["clientOrderId"]
        .as_str()
        .unwrap_or("")
        .starts_with("btcma-")));
}

// ------------------------------------------- reversal cancels old protection

fn open_algos(fake: &Fake) -> Vec<Value> {
    fake.algos_snapshot()
        .into_values()
        .filter(|o| o.get("algoStatus").and_then(|v| v.as_str()) == Some("NEW"))
        .collect()
}

#[tokio::test]
async fn reversal_cancels_the_old_emergency_stop_and_places_a_new_one() {
    let root = temp_dir("rev_stop");
    let mut h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    h.run().await;
    let old_stop = h.state_live()["trade"]["sl_client_algo_id"]
        .as_str()
        .unwrap()
        .to_string();
    assert_eq!(h.fake.algos_snapshot()[&old_stop]["side"], json!("SELL"));
    h.next_bar(83_000.0);
    assert_eq!(h.run().await["action"], json!("reversed"));
    assert_eq!(
        h.fake.algos_snapshot()[&old_stop]["algoStatus"],
        json!("CANCELED")
    );
    let open = open_algos(&h.fake);
    assert_eq!(open.len(), 1);
    let new_stop = &open[0];
    let trade = h.state_live();
    let sl = trade["trade"]["sl_client_algo_id"].as_str().unwrap();
    assert_eq!(new_stop["clientAlgoId"].as_str().unwrap(), sl);
    assert_ne!(sl, old_stop);
    assert_eq!(new_stop["side"], json!("BUY"));
    assert_eq!(new_stop["orderType"], json!("STOP_MARKET"));
    let trigger: f64 = new_stop["triggerPrice"]
        .as_str()
        .map(|s| s.parse().unwrap())
        .unwrap_or_else(|| new_stop["triggerPrice"].as_f64().unwrap_or(0.0));
    let entry: f64 = trade["trade"]["entry_price"]
        .as_str()
        .unwrap()
        .parse()
        .unwrap();
    assert!(trigger > entry);
}

#[tokio::test]
async fn reversal_also_cancels_an_active_profit_lock() {
    let root = temp_dir("rev_lock");
    let mut h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    h.run().await;
    h.fake.with_state(|s| s.mark = 85_000.0 * 1.021);
    h.run().await;
    let trade = h.state_live()["trade"].as_object().cloned().unwrap();
    let sl = trade["sl_client_algo_id"].as_str().unwrap().to_string();
    let lk = trade["lock_client_algo_id"].as_str().unwrap().to_string();
    assert_eq!(open_algos(&h.fake).len(), 2);
    h.next_bar(83_000.0);
    assert_eq!(h.run().await["action"], json!("reversed"));
    let algos = h.fake.algos_snapshot();
    for id in [&sl, &lk] {
        assert_eq!(algos[id]["algoStatus"], json!("CANCELED"));
    }
    let remaining = open_algos(&h.fake);
    assert_eq!(remaining.len(), 1);
    assert_eq!(remaining[0]["side"], json!("BUY"));
    assert_eq!(h.state_live()["trade"]["lock_price"], json!(""));
}

#[tokio::test]
async fn reversal_never_enters_while_an_old_order_could_not_be_cancelled() {
    let root = temp_dir("rev_cancel_fail");
    let mut h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    h.run().await;
    let old_stop = h.state_live()["trade"]["sl_client_algo_id"]
        .as_str()
        .unwrap()
        .to_string();

    *h.fake.fail_algo_delete.lock().unwrap() = true;

    h.next_bar(83_000.0);
    let result = h.run().await;
    let reason = result["reason"].as_str().unwrap_or("");
    assert!(reason.contains("반대 진입 보류"), "{reason}");
    assert!(h.fake.with_state(|s| s.position.is_none()));
    assert_eq!(
        h.fake.algos_snapshot()[&old_stop]["algoStatus"],
        json!("NEW")
    );
    let entries: Vec<Value> = h
        .fake
        .with_state(|s| s.orders.clone())
        .into_iter()
        .filter(|o| o["clientOrderId"].as_str().unwrap_or("").contains("entry"))
        .collect();
    assert_eq!(entries.len(), 1);
    assert_eq!(
        h.state_live()["pending_reverse_entry"]["side"],
        json!("SHORT")
    );
    h.advance_ms(20_000);
    let reason2 = h.run().await["reason"].as_str().unwrap_or("").to_string();
    assert!(
        reason2.starts_with("REVERSE_ENTRY_RETRY: OPEN_ORDERS_CANCEL_FAILED"),
        "{reason2}"
    );
    // The exchange recovers: the old stop is cleaned up and the SHORT is taken.
    *h.fake.fail_algo_delete.lock().unwrap() = false;
    h.advance_ms(20_000);
    assert_eq!(h.run().await["action"], json!("entered"));
    assert_eq!(
        h.fake.algos_snapshot()[&old_stop]["algoStatus"],
        json!("CANCELED")
    );
    assert_eq!(
        h.fake
            .with_state(|s| s.position.as_ref().map(|p| p["side"].clone())),
        Some(json!("short"))
    );
    assert_eq!(h.state_live()["trade"]["side"], json!("SHORT"));
    assert!(h.state_live()["pending_reverse_entry"].is_null());
    assert_eq!(open_algos(&h.fake).len(), 1);
}

#[tokio::test]
async fn reverse_retry_gives_up_after_the_window() {
    let root = temp_dir("rev_expire");
    let mut h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    h.run().await;
    *h.fake.fail_algo_delete.lock().unwrap() = true;
    h.next_bar(83_000.0);
    h.run().await;
    h.advance_ms(11 * 60_000); // past the 10-minute retry window
    assert_eq!(h.run().await["reason"], json!("REVERSE_ENTRY_EXPIRED"));
    assert!(h.state_live()["pending_reverse_entry"].is_null());
    let notes = h.notes.lock().unwrap().clone();
    assert!(notes.iter().any(|n| n.contains("포기")));
    h.advance_ms(20_000);
    assert_eq!(h.run().await["reason"], json!("WAITING_NEXT_CLOSE"));
}

// --------------------------------------- stuck order records

/// `_stuck_fxs_record`: insert a stuck SUBMITTED_UNKNOWN FXS entry record.
fn stuck_fxs_record(service: &SmaCrossService<Fake>, network: &str) -> (OrderRecord, String) {
    let cid = "btcma-fxsusdt-l-entry-b5cff459a7a".to_string();
    let store = service.store(network).unwrap();
    let mut store = store.lock().unwrap();
    let record = store
        .upsert(OrderRecord {
            client_order_id: cid.clone(),
            symbol: "FXS/USDT:USDT".into(),
            side: "LONG".into(),
            strategy: "btcma".into(),
            signal_timestamp: "1".into(),
            requested_qty: 10.0,
            order_state: "SUBMITTED_UNKNOWN".into(),
            ..Default::default()
        })
        .unwrap();
    store
        .try_acquire_entry_lease("old-process", 120.0, "FXS/USDT:USDT", &cid, false, false)
        .unwrap();
    (record, cid)
}

fn order_state_of(service: &SmaCrossService<Fake>, cid: &str) -> String {
    let store = service.store("testnet").unwrap();
    let store = store.lock().unwrap();
    store
        .get(cid)
        .unwrap()
        .map(|r| r.order_state)
        .unwrap_or_default()
}

#[tokio::test]
async fn stuck_unknown_entry_on_another_coin_no_longer_blocks_entries() {
    let root = temp_dir("stuck_ok");
    let h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    let (record, cid) = stuck_fxs_record(&h.service, "testnet");
    assert_eq!(record.order_state, "SUBMITTED_UNKNOWN");
    {
        let store = h.service.store("testnet").unwrap();
        let store = store.lock().unwrap();
        store
            .set_runtime_state(
                "entry_lock_reason",
                &json!("RECONCILIATION_REQUIRED:position_fetch:TimeoutError"),
            )
            .unwrap();
        assert!(store
            .entry_block_reason(Some("BTC/USDT:USDT"), false, false)
            .unwrap()
            .unwrap_or_default()
            .starts_with("RUNTIME_ENTRY_LOCK"));
    }
    let result = h.run().await;
    assert_eq!(result["action"], json!("entered"), "{}", result);
    assert_eq!(order_state_of(&h.service, &cid), "CANCELED");
    {
        let store = h.service.store("testnet").unwrap();
        let store = store.lock().unwrap();
        assert!(store
            .get_runtime_state("entry_lock_reason", None)
            .unwrap()
            .is_none());
    }
    let notes = h.notes.lock().unwrap().clone();
    assert!(notes.iter().any(|n| n.contains("🧹")));
}

#[tokio::test]
async fn stuck_record_is_kept_while_its_position_really_exists() {
    let root = temp_dir("stuck_keep");
    let h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    let (_record, cid) = stuck_fxs_record(&h.service, "testnet");
    h.fake.extra_positions.lock().unwrap().push(json!({
        "symbol": "FXS/USDT:USDT",
        "side": "long",
        "contracts": 10.0,
        "entryPrice": 1.0
    }));
    let resolved = h
        .service
        .resolve_stale_entry_records("testnet")
        .await
        .unwrap();
    assert!(resolved.is_empty());
    assert_eq!(order_state_of(&h.service, &cid), "SUBMITTED_UNKNOWN");
}

#[tokio::test]
async fn stuck_record_is_kept_when_the_exchange_cannot_be_checked() {
    let root = temp_dir("stuck_timeout");
    let h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    let (_record, cid) = stuck_fxs_record(&h.service, "testnet");
    h.fake.with_state(|s| s.raise_positions = true);
    assert!(h
        .service
        .resolve_stale_entry_records("testnet")
        .await
        .unwrap()
        .is_empty());
    assert_eq!(order_state_of(&h.service, &cid), "SUBMITTED_UNKNOWN");
}

// ----------------------------- reverse when the close confirmation lags

/// Simulate Binance still reporting the old position for a few reads.
fn lagging_close(h: &Harness, stale_reads: i64) {
    *h.fake.lag_reads.lock().unwrap() = stale_reads;
    *h.fake.lag_left.lock().unwrap() = 0;
    *h.fake.lag_snapshot.lock().unwrap() = None;
}

#[tokio::test]
async fn reverse_survives_a_short_position_lag_in_the_same_cycle() {
    let root = temp_dir("lag_short");
    let mut h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    h.run().await;
    lagging_close(&h, 3);
    h.next_bar(83_000.0);
    assert_eq!(h.run().await["action"], json!("reversed"));
    let ledger = h.service.ledger("testnet").unwrap();
    let closed = ledger
        .lock()
        .unwrap()
        .trades(Some("LIVE"), None, None, None, Some("CLOSED"))
        .unwrap();
    assert_eq!(
        closed.last().unwrap()["exit_reason"],
        json!("CROSS_REVERSE")
    );
    assert_eq!(h.state_live()["trade"]["side"], json!("SHORT"));
}

#[tokio::test]
async fn reverse_is_kept_when_the_close_shows_up_only_next_cycle() {
    let root = temp_dir("lag_long");
    let mut h = Harness::new(&root, true, "testnet", 86_000.0, HashMap::new());
    h.run().await;
    lagging_close(&h, 20);
    h.next_bar(83_000.0);
    let first = h.run().await;
    assert_eq!(first["action"], json!("closing"), "{}", first);
    let pending = h.state_live()["trade"]["pending_reverse"].clone();
    assert!(
        !pending.is_null() && pending != json!(false) && pending != json!({}),
        "{pending}"
    );
    for _ in 0..20 {
        let rows = h.fake.fetch_positions(None).await.unwrap_or_default();
        if rows.is_empty() {
            break;
        }
    }
    h.advance_ms(10_000);
    let result = h.run().await;
    assert_eq!(result["action"], json!("reversed"), "{}", result);
    let ledger = h.service.ledger("testnet").unwrap();
    let closed = ledger
        .lock()
        .unwrap()
        .trades(Some("LIVE"), None, None, None, Some("CLOSED"))
        .unwrap();
    assert_eq!(
        closed.last().unwrap()["exit_reason"],
        json!("CROSS_REVERSE")
    );
    assert_eq!(h.state_live()["trade"]["side"], json!("SHORT"));
}

// -------------------------------------------------- SMA200 / SMA120 selection

/// `_between_rows`: SMA3 (104) above SMA120 (~100) but below SMA200 (~108).
fn between_rows() -> Vec<Value> {
    let mut closes: Vec<f64> = Vec::with_capacity(220);
    closes.extend(std::iter::repeat_n(120.0, 100));
    closes.extend(std::iter::repeat_n(100.0, 117));
    closes.extend(std::iter::repeat_n(104.0, 3));
    closes
        .iter()
        .enumerate()
        .map(|(i, c)| json!([T0 + (i as i64) * H1, c, c * 1.001, c * 0.999, c, 10.0]))
        .collect()
}

/// Like `between_rows`, but the close also sits below SMA25 so the SHORT
/// entry passes the SMA25 side gate (SMA25 ~107.5, SMA120 ~101.8, SMA200 ~109.1).
fn between_rows_below_sma25() -> Vec<Value> {
    let mut closes: Vec<f64> = Vec::with_capacity(220);
    closes.extend(std::iter::repeat_n(120.0, 100));
    closes.extend(std::iter::repeat_n(100.0, 92));
    closes.extend(std::iter::repeat_n(108.0, 25));
    closes.extend(std::iter::repeat_n(104.0, 3));
    closes
        .iter()
        .enumerate()
        .map(|(i, c)| json!([T0 + (i as i64) * H1, c, c * 1.001, c * 0.999, c, 10.0]))
        .collect()
}

#[tokio::test]
async fn switching_to_sma120_while_holding_follows_the_new_line() {
    let root = temp_dir("switch_sma");
    let mut extra = HashMap::new();
    extra.insert("entry_mode".to_string(), json!("trend"));
    let mut h = Harness::new(&root, true, "testnet", 86_000.0, extra);
    h.rows = between_rows_below_sma25();
    h.fake
        .bars
        .lock()
        .unwrap()
        .insert("1h".to_string(), h.rows.clone());
    h.now_ms = now_after_default(&h.rows);
    h.set_time_from_now();
    assert_eq!(h.run().await["action"], json!("entered"));
    assert_eq!(h.state_live()["trade"]["side"], json!("SHORT"));

    {
        let mut cfg = h.cfg.lock().unwrap();
        let mut obj = cfg.as_object().cloned().unwrap();
        obj.insert("slow_period".to_string(), json!(120));
        *cfg = Value::Object(obj);
    }
    let last_ts = h
        .rows
        .last()
        .and_then(|r| {
            r.as_array()
                .and_then(|a| a.first())
                .and_then(|v| v.as_i64())
        })
        .unwrap();
    // Closes above SMA120 and SMA25: the reversal wins over the SMA25 stop.
    h.rows
        .push(json!([last_ts + H1, 104.0, 112.1, 103.9, 112.0, 10.0]));
    h.fake
        .bars
        .lock()
        .unwrap()
        .insert("1h".to_string(), h.rows.clone());
    h.now_ms = now_after_default(&h.rows);
    h.set_time_from_now();
    let result = h.run().await;
    assert_eq!(result["action"], json!("reversed"), "{}", result);
    let ledger = h.service.ledger("testnet").unwrap();
    let closed = ledger
        .lock()
        .unwrap()
        .trades(Some("LIVE"), None, None, None, Some("CLOSED"))
        .unwrap();
    let last = closed.last().unwrap();
    assert_eq!(last["side"], json!("SHORT"));
    assert_eq!(last["exit_reason"], json!("CROSS_REVERSE"));
    let trade = h.state_live()["trade"].as_object().cloned().unwrap();
    assert_eq!(trade["side"], json!("LONG"));
    assert_eq!(trade["entry_type"], json!("REALIGN"));
    assert_eq!(trade["slow_period"], json!(120));
}

#[tokio::test]
async fn holding_on_the_right_side_is_not_touched() {
    let root = temp_dir("holding");
    let mut extra = HashMap::new();
    extra.insert("entry_mode".to_string(), json!("trend"));
    extra.insert("slow_period".to_string(), json!(120));
    let mut h = Harness::new(&root, true, "testnet", 86_000.0, extra);
    h.rows = between_rows();
    h.fake
        .bars
        .lock()
        .unwrap()
        .insert("1h".to_string(), h.rows.clone());
    h.now_ms = now_after_default(&h.rows);
    h.set_time_from_now();
    h.run().await;
    assert_eq!(h.state_live()["trade"]["side"], json!("LONG"));
    let last_ts = h
        .rows
        .last()
        .and_then(|r| {
            r.as_array()
                .and_then(|a| a.first())
                .and_then(|v| v.as_i64())
        })
        .unwrap();
    h.rows
        .push(json!([last_ts + H1, 104.0, 104.1, 103.9, 104.0, 10.0]));
    h.fake
        .bars
        .lock()
        .unwrap()
        .insert("1h".to_string(), h.rows.clone());
    h.now_ms = now_after_default(&h.rows);
    h.set_time_from_now();
    assert_eq!(h.run().await["action"], json!("managed"));
    assert_eq!(h.state_live()["trade"]["side"], json!("LONG"));
}

// Keep `service`, `approx`, `Map` referenced even if unused by later bodies.
#[allow(dead_code)]
fn _touches() {
    let _: Option<BoxFut<()>> = None;
    let _ = service::CCXT_SYMBOL;
    let _: Option<Map<String, Value>> = None;
    assert!(approx(1.0, 1.0));
}

// ---------------------------------------------------------- SMA25 stop line

fn push_bar(h: &mut Harness, close: f64) {
    let last_ts = h
        .rows
        .last()
        .and_then(|r| {
            r.as_array()
                .and_then(|a| a.first())
                .and_then(|v| v.as_i64())
        })
        .unwrap();
    h.rows.push(json!([
        last_ts + H1,
        close,
        close * 1.001,
        close * 0.999,
        close,
        10.0
    ]));
    h.fake
        .bars
        .lock()
        .unwrap()
        .insert("1h".to_string(), h.rows.clone());
    h.now_ms = now_after_default(&h.rows);
    h.set_time_from_now();
}

fn trend_harness(name: &str, rows: Vec<Value>) -> Harness {
    let root = temp_dir(name);
    let mut extra = HashMap::new();
    extra.insert("entry_mode".to_string(), json!("trend"));
    let mut h = Harness::new(&root, true, "testnet", 86_000.0, extra);
    h.rows = rows;
    h.fake
        .bars
        .lock()
        .unwrap()
        .insert("1h".to_string(), h.rows.clone());
    h.now_ms = now_after_default(&h.rows);
    h.set_time_from_now();
    h
}

#[tokio::test]
async fn entry_is_skipped_when_close_is_on_the_wrong_side_of_sma25() {
    // SHORT trend (below SMA200) but the close 104 is above SMA25 (~100.5).
    let h = trend_harness("sma25_gate", between_rows());
    let result = h.run().await;
    assert_eq!(result["action"], json!("skip"), "{result}");
    let reason = result["reason"].as_str().unwrap_or("");
    assert!(reason.starts_with("STOP_MA_SIDE_MISMATCH"), "{reason}");
    assert!(!h
        .state_live()
        .get("trade")
        .map(|v| v.is_object())
        .unwrap_or(false));
}

#[tokio::test]
async fn sma25_stop_closes_the_position_and_waits_for_a_new_cross() {
    let mut h = trend_harness("sma25_stop", between_rows_below_sma25());
    assert_eq!(h.run().await["action"], json!("entered"));
    let trade = h.state_live()["trade"].as_object().cloned().unwrap();
    assert_eq!(trade["side"], json!("SHORT"));
    assert_eq!(trade["stop_ma_period"], json!(25));

    // Same bar evaluated again: nothing happens.
    assert_eq!(h.run().await["action"], json!("managed"));

    // 108.9 closes above SMA25 (~107.56) but stays below SMA200 (~109.00):
    // no SMA200 cross, so the SMA25 stop fires.
    push_bar(&mut h, 108.9);
    let result = h.run().await;
    assert_eq!(result["action"], json!("exited"), "{result}");
    let ledger = h.service.ledger("testnet").unwrap();
    let closed = ledger
        .lock()
        .unwrap()
        .trades(Some("LIVE"), None, None, None, Some("CLOSED"))
        .unwrap();
    let last = closed.last().unwrap();
    assert_eq!(last["side"], json!("SHORT"));
    assert_eq!(last["exit_reason"], json!("SMA25_STOP"));
    let state = h.state_live();
    assert!(!state.get("trade").map(|v| v.is_object()).unwrap_or(false));
    let block = &state["reentry_block"]["BTCUSDT"];
    assert_eq!(block["side"], json!("SHORT"));
    assert_eq!(block["until_cross"], json!(true));

    // Trend mode would normally re-enter after `reentry_wait_bars` (3); after
    // an SMA25 stop it keeps waiting even with the close back below SMA25.
    for _ in 0..5 {
        push_bar(&mut h, 104.0);
        let result = h.run().await;
        assert_ne!(result["action"], json!("entered"), "{result}");
    }
    let state = h.state_live();
    assert!(!state.get("trade").map(|v| v.is_object()).unwrap_or(false));
    assert_eq!(
        state["reentry_block"]["BTCUSDT"]["until_cross"],
        json!(true)
    );
    let lines = h.service.reentry_wait_lines(&h.service.config(), &state);
    assert!(lines.iter().any(|l| l.contains("SMA25 손절")), "{lines:?}");
}

#[tokio::test]
async fn sma25_stop_is_not_armed_for_trades_opened_before_the_rule() {
    let mut h = trend_harness("sma25_legacy", between_rows_below_sma25());
    assert_eq!(h.run().await["action"], json!("entered"));
    // Simulate a position opened by the previous version (no stop_ma_period).
    let mut state = h.state_live();
    state["trade"]
        .as_object_mut()
        .unwrap()
        .remove("stop_ma_period");
    h.service.save_state("testnet", "LIVE", &mut state).unwrap();

    push_bar(&mut h, 108.9);
    assert_eq!(h.run().await["action"], json!("managed"));
    assert_eq!(h.state_live()["trade"]["side"], json!("SHORT"));
}

#[tokio::test]
async fn sma25_stop_also_applies_in_dry_run() {
    let root = temp_dir("sma25_paper");
    let mut extra = HashMap::new();
    extra.insert("entry_mode".to_string(), json!("trend"));
    extra.insert("emergency_stop_roi_percent".to_string(), json!(0));
    let mut h = Harness::new(&root, false, "testnet", 86_000.0, extra);
    h.rows = between_rows_below_sma25();
    h.fake
        .bars
        .lock()
        .unwrap()
        .insert("1h".to_string(), h.rows.clone());
    h.now_ms = now_after_default(&h.rows);
    h.set_time_from_now();
    assert_eq!(h.run().await["action"], json!("entered"));
    assert_eq!(h.state("DRY_RUN")["trade"]["side"], json!("SHORT"));
    push_bar(&mut h, 108.9);
    let result = h.run().await;
    assert_eq!(result["action"], json!("exited"), "{result}");
    let ledger = h.service.ledger("testnet").unwrap();
    let closed = ledger
        .lock()
        .unwrap()
        .trades(Some("DRY_RUN"), None, None, None, Some("CLOSED"))
        .unwrap();
    assert_eq!(closed.last().unwrap()["exit_reason"], json!("SMA25_STOP"));
}
