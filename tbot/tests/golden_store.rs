use std::collections::HashMap;
use std::path::Path;
use std::sync::Arc;

use chrono::TimeZone;
use serde_json::{Map, Value};
use tempfile::TempDir;

use tbot::store::clock::{py_isoformat, Clock, FixedClock};
use tbot::store::ledger::{
    compute_stats, daily_entry_block_reason, trading_day_bounds, LedgerError, StrategyLedger,
};
use tbot::store::order_state::{
    build_client_order_id, BuildClientOrderIdArgs, OrderRecord, SQLiteTradingStateStore, StoreError,
};

fn approx_eq(a: f64, b: f64) -> bool {
    if a.is_nan() && b.is_nan() {
        return true;
    }
    (a - b).abs() <= 1e-9
}

fn values_approx_eq(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => {
            let xf = x.as_f64().unwrap_or(0.0);
            let yf = y.as_f64().unwrap_or(0.0);
            approx_eq(xf, yf)
        }
        (Value::Array(xs), Value::Array(ys)) => {
            if xs.len() != ys.len() {
                return false;
            }
            xs.iter()
                .zip(ys.iter())
                .all(|(x, y)| values_approx_eq(x, y))
        }
        (Value::Object(xm), Value::Object(ym)) => {
            if xm.len() != ym.len() {
                return false;
            }
            for (k, xv) in xm.iter() {
                match ym.get(k) {
                    Some(yv) => {
                        if !values_approx_eq(xv, yv) {
                            return false;
                        }
                    }
                    None => return false,
                }
            }
            true
        }
        _ => a == b,
    }
}

fn dt_from_iso(iso: &str) -> chrono::DateTime<chrono::Utc> {
    chrono::DateTime::parse_from_rfc3339(iso)
        .unwrap()
        .with_timezone(&chrono::Utc)
}

fn record_to_json(rec: &OrderRecord) -> Value {
    Value::Object(rec.to_json_map())
}

fn ledger_err_to_value(e: &LedgerError) -> Value {
    Value::String(format!("LedgerError: {}", e))
}

fn store_err_to_value(e: &StoreError) -> Value {
    match e {
        StoreError::UnknownClientOrderId(id) => {
            Value::String(format!("KeyError: 'unknown client_order_id: {}'", id))
        }
        StoreError::Message(m) => Value::String(format!("ValueError: {}", m)),
        StoreError::Sqlite(s) => Value::String(format!("sqlite3.Error: {}", s)),
        StoreError::Io(io) => Value::String(format!("OSError: {}", io)),
    }
}

fn map_from_json(v: &Value) -> Map<String, Value> {
    match v {
        Value::Object(m) => m.clone(),
        _ => Map::new(),
    }
}

struct Ctx {
    ledger: StrategyLedger,
    store: SQLiteTradingStateStore,
    clock: Arc<FixedClock>,
    _tmp: TempDir,
}

#[test]
fn golden_store_matches_python() {
    let manifest_dir = env!("CARGO_MANIFEST_DIR");
    let golden_path = Path::new(manifest_dir).join("tests/golden_store.json");
    let text = std::fs::read_to_string(&golden_path)
        .unwrap_or_else(|e| panic!("read golden {}: {}", golden_path.display(), e));
    let golden: Value = serde_json::from_str(&text).expect("parse golden");
    let ops = golden
        .get("ops")
        .and_then(|v| v.as_array())
        .expect("ops array");
    let expected_tables = golden.get("tables").cloned().unwrap_or(Value::Null);
    let expected_events = golden.get("events").cloned().unwrap_or(Value::Null);

    let tmp = TempDir::new().expect("tmp");
    let db_path = tmp.path().join("ledger.sqlite3");
    let events_path = tmp.path().join("events.log");
    let state_path = tmp.path().join("state.sqlite3");

    let base = chrono::Utc.with_ymd_and_hms(2026, 10, 30, 1, 2, 3).unwrap()
        + chrono::Duration::microseconds(123456);
    let clock = Arc::new(FixedClock::new(
        base,
        base.timestamp_micros() as f64 / 1_000_000.0,
    ));
    let clock_trait: Arc<dyn Clock> = clock.clone();

    let ledger = StrategyLedger::open(&db_path, &events_path, clock_trait.clone(), "1.0.0")
        .expect("open ledger");
    let store =
        SQLiteTradingStateStore::open(&state_path, clock_trait.clone()).expect("open store");
    let store = store;

    let mut ctx = Ctx {
        ledger,
        store,
        clock,
        _tmp: tmp,
    };

    let _ = &mut ctx;

    let cfg: Map<String, Value> = {
        let mut m = Map::new();
        m.insert("max_trades_per_day".into(), Value::from(5));
        m.insert("max_consecutive_losses".into(), Value::from(2));
        m.insert("max_daily_loss_pct".into(), Value::from(0.03));
        m
    };

    let mut idx = 0usize;
    while idx < ops.len() {
        let op = &ops[idx];
        let name = op.get("op").and_then(|v| v.as_str()).unwrap_or("");
        let now_iso = op.get("now").and_then(|v| v.as_str()).unwrap_or("");
        if !now_iso.is_empty() {
            let dt = dt_from_iso(now_iso);
            let epoch = dt.timestamp_micros() as f64 / 1_000_000.0;
            ctx.clock.set(dt, epoch);
        }
        let expected = if let Some(err) = op.get("error") {
            Err(err.clone())
        } else {
            Ok(op.get("result").cloned().unwrap_or(Value::Null))
        };
        let actual: Result<Value, Value> = run_op(name, &mut ctx, &cfg);
        match (expected, actual) {
            (Err(exp), Err(act)) => {
                if exp != act {
                    panic!(
                        "op[{}] {}: error mismatch\n exp: {}\n act: {}",
                        idx, name, exp, act
                    );
                }
            }
            (Ok(exp), Ok(act)) => {
                if !values_approx_eq(&exp, &act) {
                    panic!(
                        "op[{}] {}: value mismatch\n exp: {}\n act: {}",
                        idx, name, exp, act
                    );
                }
            }
            (Err(exp), Ok(act)) => {
                panic!("op[{}] {}: expected error {}, got {}", idx, name, exp, act);
            }
            (Ok(exp), Err(act)) => {
                panic!("op[{}] {}: expected {}, got error {}", idx, name, exp, act);
            }
        }
        idx += 1;
    }

    // Dump tables and compare.
    let actual_tables = dump_tables(&[
        ("ledger", ctx.ledger_db_path()),
        ("state", ctx.store_db_path()),
    ]);
    let expected_tables = match expected_tables {
        Value::Object(m) => Value::Object(m),
        _ => Map::new().into(),
    };
    compare_string_structurally(&actual_tables, &expected_tables, "tables");

    // Events as raw lines.
    let actual_events: Vec<String> = std::fs::read_to_string(ctx.events_path())
        .unwrap_or_default()
        .lines()
        .map(|s| s.to_string())
        .collect();
    let expected_events: Vec<String> = match expected_events {
        Value::Array(arr) => arr
            .iter()
            .map(|v| v.as_str().unwrap_or("").to_string())
            .collect(),
        _ => Vec::new(),
    };
    assert_eq!(
        actual_events.len(),
        expected_events.len(),
        "event line count mismatch"
    );
    for (i, (a, e)) in actual_events.iter().zip(expected_events.iter()).enumerate() {
        assert_eq!(a, e, "event line[{}] mismatch", i);
    }
}

impl Ctx {
    fn ledger_db_path(&self) -> std::path::PathBuf {
        self._tmp.path().join("ledger.sqlite3")
    }
    fn store_db_path(&self) -> std::path::PathBuf {
        self._tmp.path().join("state.sqlite3")
    }
    fn events_path(&self) -> std::path::PathBuf {
        self._tmp.path().join("events.log")
    }
}

fn compare_string_structurally(actual: &Value, expected: &Value, path: &str) {
    // Compare strictly as strings for row content, allowing numeric comparisons.
    match (actual, expected) {
        (Value::Object(am), Value::Object(em)) => {
            assert_eq!(
                am.len(),
                em.len(),
                "{}: object size mismatch\n actual: {}\n expected: {}",
                path,
                actual,
                expected
            );
            for (k, av) in am.iter() {
                let ev = em
                    .get(k)
                    .unwrap_or_else(|| panic!("{}: missing expected key {}", path, k));
                compare_string_structurally(av, ev, &format!("{}.{}", path, k));
            }
        }
        (Value::Array(aa), Value::Array(ea)) => {
            assert_eq!(
                aa.len(),
                ea.len(),
                "{}: array size mismatch\n actual: {}\n expected: {}",
                path,
                actual,
                expected
            );
            for (i, (av, ev)) in aa.iter().zip(ea.iter()).enumerate() {
                compare_string_structurally(av, ev, &format!("{}[{}]", path, i));
            }
        }
        (Value::String(a), Value::String(e)) => {
            assert_eq!(a, e, "{}: string mismatch", path);
        }
        (Value::Number(a), Value::Number(e)) => {
            let af = a.as_f64().unwrap_or(0.0);
            let ef = e.as_f64().unwrap_or(0.0);
            assert!(
                approx_eq(af, ef),
                "{}: number mismatch {} vs {}",
                path,
                a,
                e
            );
        }
        (Value::Null, Value::Null) => {}
        (Value::Bool(a), Value::Bool(e)) => {
            assert_eq!(a, e, "{}: bool mismatch", path);
        }
        _ => {
            panic!("{}: type mismatch: {} vs {}", path, actual, expected);
        }
    }
}

fn dump_tables(dbs: &[(&str, std::path::PathBuf)]) -> Value {
    let mut out = Map::new();
    for (name, path) in dbs {
        let conn = rusqlite::Connection::open(path).expect("open db");
        let mut stmt = conn
            .prepare("SELECT name FROM sqlite_master WHERE type='table'")
            .expect("list tables");
        let names: Vec<String> = stmt
            .query_map([], |r| r.get::<_, String>(0))
            .expect("tables")
            .filter_map(|r| r.ok())
            .collect();
        for t in names {
            if t.starts_with("sqlite_") {
                continue;
            }
            let sql = format!("SELECT rowid, * FROM \"{}\" ORDER BY rowid", t);
            let mut stmt = conn.prepare(&sql).expect("prepare dump");
            let col_count = stmt.column_count();
            let col_names: Vec<String> =
                stmt.column_names().iter().map(|s| s.to_string()).collect();
            let mut rows = stmt.query([]).expect("rows");
            let mut table_rows: Vec<Value> = Vec::new();
            while let Some(row) = rows.next().expect("next") {
                let mut m = Map::new();
                #[allow(clippy::needless_range_loop)] // index also reads the row
                for i in 1..col_count {
                    let name = &col_names[i];
                    let v: Value = match row.get_ref(i).expect("get") {
                        rusqlite::types::ValueRef::Null => Value::Null,
                        rusqlite::types::ValueRef::Integer(n) => Value::from(n),
                        rusqlite::types::ValueRef::Real(f) => Value::from(f),
                        rusqlite::types::ValueRef::Text(t) => {
                            Value::String(String::from_utf8_lossy(t).to_string())
                        }
                        rusqlite::types::ValueRef::Blob(_) => Value::Null,
                    };
                    m.insert(name.clone(), v);
                }
                table_rows.push(Value::Object(m));
            }
            out.insert(format!("{}:{}", name, t), Value::Array(table_rows));
        }
    }
    Value::Object(out)
}

fn run_op(name: &str, ctx: &mut Ctx, cfg: &Map<String, Value>) -> Result<Value, Value> {
    match name {
        "ledger_set_state" => {
            let mut nested = Map::new();
            nested.insert("nested".into(), Value::String("값".into()));
            let mut signals_a =
                Value::Array(vec![Value::from(1), Value::from(2), Value::Object(nested)]);
            let _ = &mut signals_a;
            let mut state = Map::new();
            state.insert("status".into(), Value::String("READY".into()));
            state.insert("equity_krw".into(), Value::String("1234567.89".into()));
            state.insert(
                "signals".into(),
                Value::Array(vec![Value::from(1), Value::from(2), {
                    let mut n = Map::new();
                    n.insert("nested".into(), Value::String("값".into()));
                    Value::Object(n)
                }]),
            );
            state.insert("memo".into(), Value::String("한글 테스트 😀".into()));
            state.insert("score".into(), Value::from(1.5));
            let st = Value::Object(state);
            ctx.ledger
                .set_state("day_state", &st)
                .map_err(|e| ledger_err_to_value(&e))?;
            Ok(ctx
                .ledger
                .get_state("day_state", None)
                .unwrap_or(Value::Null))
        }
        "ledger_get_state_missing" => Ok(ctx
            .ledger
            .get_state("missing", Some(Value::String("fallback".into())))
            .unwrap_or(Value::Null)),
        "ledger_upsert_trade" => {
            let mut t1 = Map::new();
            t1.insert("trade_id".into(), Value::String("T1".into()));
            t1.insert("signal_id".into(), Value::String("S1".into()));
            t1.insert("mode".into(), Value::String("paper".into()));
            t1.insert("network".into(), Value::String("mainnet".into()));
            t1.insert("status".into(), Value::String("OPEN".into()));
            t1.insert(
                "entry_time".into(),
                Value::String("2026-10-30T01:00:00+00:00".into()),
            );
            t1.insert("side".into(), Value::String("LONG".into()));
            t1.insert("quantity".into(), Value::String("0.125".into()));
            t1.insert("entry_price".into(), Value::from(100_000_000i64));
            t1.insert("stop_price".into(), Value::from(99_000_000.0));
            t1.insert("take_profit_price".into(), Value::from(101_000_000i64));
            t1.insert("r_multiple".into(), Value::from(1.0));
            t1.insert("strategy_version".into(), Value::Null);
            let mut extra = Map::new();
            extra.insert("tag".into(), Value::String("첫 거래".into()));
            t1.insert("extra".into(), Value::Object(extra));
            ctx.ledger
                .upsert_trade(&t1)
                .map_err(|e| ledger_err_to_value(&e))?;
            let mut t2 = Map::new();
            t2.insert("trade_id".into(), Value::String("T1".into()));
            t2.insert("status".into(), Value::String("CLOSED".into()));
            t2.insert(
                "exit_time".into(),
                Value::String("2026-10-30T02:00:00+00:00".into()),
            );
            t2.insert("exit_price".into(), Value::from(101_000_000i64));
            t2.insert("gross_pnl".into(), Value::from(500.5));
            t2.insert("commission".into(), Value::from(0.5));
            t2.insert("funding".into(), Value::from(0.0));
            t2.insert("net_pnl".into(), Value::from(500.0));
            t2.insert("pnl_pct".into(), Value::from(0.5));
            t2.insert("result".into(), Value::String("WIN".into()));
            t2.insert("exit_reason".into(), Value::String("TP".into()));
            ctx.ledger
                .upsert_trade(&t2)
                .map_err(|e| ledger_err_to_value(&e))?;
            let got = ctx
                .ledger
                .get_trade("T1")
                .map_err(|e| ledger_err_to_value(&e))?;
            Ok(got.map(Value::Object).unwrap_or(Value::Null))
        }
        "ledger_trades_mode" => {
            let rows = ctx
                .ledger
                .trades(Some("paper"), None, None, None, None)
                .map_err(|e| ledger_err_to_value(&e))?;
            Ok(Value::Array(rows.into_iter().map(Value::Object).collect()))
        }
        "ledger_stats" => {
            let rows = ctx
                .ledger
                .trades(None, None, None, None, None)
                .map_err(|e| ledger_err_to_value(&e))?;
            Ok(Value::Object(compute_stats(&rows)))
        }
        "ledger_events" => {
            let mut f1 = Map::new();
            f1.insert("bot".into(), Value::String("tbot".into()));
            f1.insert("version".into(), Value::String("1".into()));
            ctx.ledger.event("started", f1);
            let mut f2 = Map::new();
            f2.insert("note".into(), Value::String("한글".into()));
            f2.insert("value".into(), Value::from(1.5));
            ctx.ledger.event("check", f2);
            let mut f3 = Map::new();
            f3.insert("ok".into(), Value::Bool(true));
            ctx.ledger.event("done", f3);
            let evs = ctx.ledger.recent_events(2, None);
            Ok(Value::Array(evs))
        }
        "ledger_daily_block_consecutive" => {
            let trades = vec![
                map_from_json(&serde_json::json!({"status":"CLOSED","net_pnl":"-100.0"})),
                map_from_json(&serde_json::json!({"status":"CLOSED","net_pnl":"-50.0"})),
            ];
            Ok(Value::String(daily_entry_block_reason(
                &trades,
                cfg,
                Some(&Value::from(1000.0)),
                Some(&Value::from(990.0)),
            )))
        }
        "ledger_daily_block_loss_pct" => {
            let trades = vec![map_from_json(
                &serde_json::json!({"status":"CLOSED","net_pnl":100.0}),
            )];
            Ok(Value::String(daily_entry_block_reason(
                &trades,
                cfg,
                Some(&Value::from(1000.0)),
                Some(&Value::from(900.0)),
            )))
        }
        "ledger_daily_block_count" => {
            let trades: Vec<Map<String, Value>> = (10..=50)
                .step_by(10)
                .map(|n| {
                    let mut m = Map::new();
                    m.insert("status".into(), Value::String("CLOSED".into()));
                    m.insert("net_pnl".into(), Value::String(n.to_string()));
                    m
                })
                .collect();
            Ok(Value::String(daily_entry_block_reason(
                &trades,
                cfg,
                Some(&Value::from(1000.0)),
                Some(&Value::from(1000.0)),
            )))
        }
        "ledger_bounds_kst_before" => {
            let now = dt_from_iso("2026-10-30T14:59:59+00:00");
            let (s, e, k) = trading_day_bounds(now, "Asia/Seoul");
            Ok(Value::Array(vec![
                Value::String(py_isoformat(&s)),
                Value::String(py_isoformat(&e)),
                Value::String(k),
            ]))
        }
        "ledger_bounds_kst_after" => {
            let now = dt_from_iso("2026-10-30T15:00:01+00:00");
            let (s, e, k) = trading_day_bounds(now, "Asia/Seoul");
            Ok(Value::Array(vec![
                Value::String(py_isoformat(&s)),
                Value::String(py_isoformat(&e)),
                Value::String(k),
            ]))
        }
        "store_build_ids" => {
            let ts1 = Value::from(1_700_000_000i64);
            let id1 = build_client_order_id(BuildClientOrderIdArgs {
                strategy: "btc_pullback",
                symbol: "BTC/USDT:USDT",
                side: "buy",
                signal_timestamp: Some(&ts1),
                stage: "entry",
                leg: None,
                revision: None,
                max_length: 36,
            });
            let ts2 = Value::String("1700000000000".into());
            let id2 = build_client_order_id(BuildClientOrderIdArgs {
                strategy: "btc_pullback",
                symbol: "BTC/USDT:USDT",
                side: "sell",
                signal_timestamp: Some(&ts2),
                stage: "entry",
                leg: None,
                revision: None,
                max_length: 36,
            });
            let ts3 = Value::String("sig-123".into());
            let id3 = build_client_order_id(BuildClientOrderIdArgs {
                strategy: "btc_pullback",
                symbol: "BTC/USDT:USDT",
                side: "buy",
                signal_timestamp: Some(&ts3),
                stage: "entry",
                leg: Some("tp1"),
                revision: Some("2"),
                max_length: 36,
            });
            let id4 = build_client_order_id(BuildClientOrderIdArgs {
                strategy: "x",
                symbol: "Y",
                side: "z",
                signal_timestamp: None,
                stage: "entry",
                leg: None,
                revision: None,
                max_length: 12,
            });
            Ok(Value::Array(vec![
                Value::String(id1),
                Value::String(id2),
                Value::String(id3),
                Value::String(id4),
            ]))
        }
        "store_upsert_new" => {
            let mut metadata = Map::new();
            metadata.insert("note".into(), Value::String("신규".into()));
            let now = py_isoformat(&ctx.clock.now_utc());
            let mut rec = OrderRecord {
                client_order_id: "btcp-BTCUSDT-l-entr-AbCdEf123456".into(),
                symbol: "BTC/USDT:USDT".into(),
                side: "buy".into(),
                strategy: "btc_pullback".into(),
                signal_timestamp: "1700000000".into(),
                requested_qty: 0.01,
                order_intent: "ENTRY".into(),
                order_purpose: "entry".into(),
                position_qty_before: 0.0,
                position_signature: None,
                order_state: "PLANNED".into(),
                exchange_order_id: None,
                filled_qty: 0.0,
                average_fill_price: 0.0,
                stop_order_id: None,
                take_profit_order_ids: Vec::new(),
                created_at: now.clone(),
                updated_at: now,
                last_error: None,
                retry_count: 0,
                metadata,
            };
            rec.created_at = py_isoformat(&ctx.clock.now_utc());
            let res = ctx.store.upsert(rec).map_err(|e| store_err_to_value(&e))?;
            Ok(record_to_json(&res))
        }
        "store_transition_ack" => {
            let mut changes = Map::new();
            changes.insert("exchange_order_id".into(), Value::String("EX-1".into()));
            changes.insert("filled_qty".into(), Value::from(0.005));
            let res = ctx
                .store
                .transition("btcp-BTCUSDT-l-entr-AbCdEf123456", "ACKNOWLEDGED", changes)
                .map_err(|e| store_err_to_value(&e))?;
            Ok(record_to_json(&res))
        }
        "store_transition_unknown" => {
            let mut changes = Map::new();
            changes.insert("ordersize".into(), Value::from(1));
            let res = ctx
                .store
                .transition("does-not-exist", "FILLED_UNPROTECTED", changes)
                .map_err(|e| store_err_to_value(&e));
            match res {
                Ok(r) => Ok(record_to_json(&r)),
                Err(v) => Err(v),
            }
        }
        "store_get" => {
            let rec = ctx
                .store
                .get("btcp-BTCUSDT-l-entr-AbCdEf123456")
                .map_err(|e| store_err_to_value(&e))?;
            Ok(rec.as_ref().map(record_to_json).unwrap_or(Value::Null))
        }
        "store_list_by_states" => {
            let rows = ctx
                .store
                .list_by_states(&["ACKNOWLEDGED", "PLANNED"])
                .map_err(|e| store_err_to_value(&e))?;
            Ok(Value::Array(rows.iter().map(record_to_json).collect()))
        }
        "store_active" => {
            let rows = ctx
                .store
                .active_for_symbol("BTCUSDT")
                .map_err(|e| store_err_to_value(&e))?;
            Ok(Value::Array(rows.iter().map(record_to_json).collect()))
        }
        "store_entry_block_reason" => {
            let res = ctx
                .store
                .entry_block_reason(Some("XRP/USDT:USDT"), false, false)
                .map_err(|e| store_err_to_value(&e))?;
            Ok(res.map(Value::String).unwrap_or(Value::Null))
        }
        "store_lease" => {
            let mut out: Vec<Value> = Vec::new();
            out.push(Value::Bool(
                ctx.store
                    .try_acquire_entry_lease("owner-1", 60.0, "BTC/USDT", "cid-1", false, false)
                    .map_err(|e| store_err_to_value(&e))?,
            ));
            out.push(Value::Bool(
                ctx.store
                    .try_acquire_entry_lease("owner-2", 60.0, "BTC/USDT", "cid-2", false, false)
                    .map_err(|e| store_err_to_value(&e))?,
            ));
            let now = ctx.clock.now_utc() + chrono::Duration::seconds(120);
            let epoch = now.timestamp_micros() as f64 / 1_000_000.0;
            ctx.clock.set(now, epoch);
            out.push(Value::Bool(
                ctx.store
                    .try_acquire_entry_lease("owner-3", 60.0, "BTC/USDT", "cid-3", true, false)
                    .map_err(|e| store_err_to_value(&e))?,
            ));
            let lease = ctx
                .store
                .get_entry_lease()
                .map_err(|e| store_err_to_value(&e))?;
            out.push(lease.map(Value::Object).unwrap_or(Value::Null));
            out.push(Value::Bool(
                ctx.store
                    .release_entry_lease("cid-3", "FAILED")
                    .map_err(|e| store_err_to_value(&e))?,
            ));
            let lease2 = ctx
                .store
                .get_entry_lease()
                .map_err(|e| store_err_to_value(&e))?;
            out.push(lease2.map(Value::Object).unwrap_or(Value::Null));
            Ok(Value::Array(out))
        }
        "store_runtime" => {
            let mut v1 = Map::new();
            v1.insert("a".into(), Value::from(1));
            v1.insert("b".into(), Value::String("값".into()));
            ctx.store
                .set_runtime_state("k1", &Value::Object(v1))
                .map_err(|e| store_err_to_value(&e))?;
            ctx.store
                .set_runtime_state("k2", &Value::String("first".into()))
                .map_err(|e| store_err_to_value(&e))?;
            let ins1 = ctx
                .store
                .create_runtime_state_if_absent("k2", &Value::String("second".into()))
                .map_err(|e| store_err_to_value(&e))?;
            let ins2 = ctx
                .store
                .create_runtime_state_if_absent(
                    "k3",
                    &Value::Array(vec![Value::String("x".into()), Value::String("y".into())]),
                )
                .map_err(|e| store_err_to_value(&e))?;
            let got = ctx
                .store
                .get_runtime_state("k1", None)
                .map_err(|e| store_err_to_value(&e))?
                .unwrap_or(Value::Null);
            let deleted = ctx
                .store
                .delete_runtime_state("k3")
                .map_err(|e| store_err_to_value(&e))?;
            let got3 = ctx
                .store
                .get_runtime_state("k3", None)
                .map_err(|e| store_err_to_value(&e))?
                .unwrap_or(Value::Null);
            Ok(Value::Array(vec![
                Value::Bool(ins1),
                Value::Bool(ins2),
                got,
                Value::Bool(deleted),
                got3,
            ]))
        }
        "store_daily_loss_lock" => {
            let mut m = Map::new();
            m.insert("date".into(), Value::String("2026-10-30".into()));
            m.insert("reason".into(), Value::String("LOSS_3".into()));
            ctx.store
                .set_runtime_state("daily_loss_entry_lock", &Value::Object(m))
                .map_err(|e| store_err_to_value(&e))?;
            let res = ctx
                .store
                .daily_loss_entry_lock_reason(None)
                .map_err(|e| store_err_to_value(&e))?;
            Ok(res.map(Value::String).unwrap_or(Value::Null))
        }
        "store_daily_loss_stale" => {
            let mut m = Map::new();
            m.insert("date".into(), Value::String("2026-10-29".into()));
            m.insert("reason".into(), Value::String("OLD".into()));
            ctx.store
                .set_runtime_state("daily_loss_entry_lock", &Value::Object(m))
                .map_err(|e| store_err_to_value(&e))?;
            let reason = ctx
                .store
                .daily_loss_entry_lock_reason(None)
                .map_err(|e| store_err_to_value(&e))?;
            let state = ctx
                .store
                .get_runtime_state("daily_loss_entry_lock", None)
                .map_err(|e| store_err_to_value(&e))?
                .unwrap_or(Value::Null);
            Ok(Value::Array(vec![
                reason.map(Value::String).unwrap_or(Value::Null),
                state,
            ]))
        }
        other => {
            let _ = HashMap::<String, ()>::new();
            let _ = Path::new("");
            panic!("unhandled op: {}", other);
        }
    }
}
