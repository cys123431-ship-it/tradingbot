//! S1 smoke tests for `SmaCrossService`: config, paths, state, balances, rules.

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use serde_json::{json, Value};

use tbot::exchange::fake::{FakeBinance, FakeOptions};
use tbot::exchange::FuturesExchange;
use tbot::store::clock::FixedClock;
use tbot::strategy::service::{self, BoxFut, ServiceDeps, SmaCrossService, Variant};

fn temp_dir(tag: &str) -> PathBuf {
    static N: AtomicU64 = AtomicU64::new(0);
    let n = N.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!(
        "tbot_service_base_{tag}_{n}_{}",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn fixed_clock() -> Arc<dyn tbot::store::clock::Clock> {
    use chrono::TimeZone;
    let dt = chrono::Utc.timestamp_opt(1_700_000_000, 0).unwrap();
    Arc::new(FixedClock::new(dt, 1_700_000_000.0))
}

fn build_service(
    root: &PathBuf,
    config: Value,
    creds: Value,
    environ: HashMap<String, String>,
) -> SmaCrossService<FakeBinance> {
    let creds = Arc::new(creds);
    let exchange = FakeBinance::new(FakeOptions::default());
    let exchange = Arc::new(exchange);
    let config_getter: Arc<dyn Fn() -> Value + Send + Sync> = Arc::new(move || config.clone());
    let creds_arc = creds.clone();
    let credentials_getter: Arc<dyn Fn(&str) -> Value + Send + Sync> =
        Arc::new(move |_n: &str| (*creds_arc).clone());
    let ex = exchange.clone();
    let exchange_factory: Arc<
        dyn Fn(&str, &Value) -> Result<Arc<FakeBinance>, tbot::exchange::ExchangeError>
            + Send
            + Sync,
    > = Arc::new(move |_net: &str, _creds: &Value| Ok(ex.clone()));
    let notifier: Option<Arc<dyn Fn(String) -> BoxFut<()> + Send + Sync>> = None;
    let main_network_getter: Option<Arc<dyn Fn() -> Option<String> + Send + Sync>> = None;
    let clock: Arc<dyn Fn() -> f64 + Send + Sync> = Arc::new(|| 1_700_000_000.0);
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
        environ,
        config_writer: None,
        store_clock: fixed_clock(),
    };
    SmaCrossService::new(Variant::BtcMa, deps)
}

#[tokio::test]
async fn config_normalizes_via_variant() {
    let root = temp_dir("config");
    let mut raw = HashMap::new();
    raw.insert("network".to_string(), json!("testnet"));
    let cfg = json!(raw);
    let svc = build_service(&root, cfg, Value::Null, HashMap::new());
    let got = svc.config();
    assert!(got.is_object());
    assert!(got.get("leverage").is_some());
}

#[tokio::test]
async fn ledger_and_store_files_are_created() {
    let root = temp_dir("files");
    let svc = build_service(&root, json!({}), Value::Null, HashMap::new());
    let _ = svc.ledger("testnet").unwrap();
    let _ = svc.store("testnet").unwrap();
    let runtime = root.join(Variant::BtcMa.runtime_subdir());
    assert!(runtime.join("testnet_trades.sqlite3").exists());
    assert!(runtime.join("testnet_orders.sqlite3").exists());
}

#[tokio::test]
async fn load_save_state_roundtrip() {
    let root = temp_dir("state");
    let svc = build_service(&root, json!({}), Value::Null, HashMap::new());
    let mut state = svc.load_state("testnet", "DRY_RUN").unwrap();
    state.insert("last_error".to_string(), json!("hello"));
    svc.save_state("testnet", "DRY_RUN", &mut state).unwrap();
    let loaded = svc.load_state("testnet", "DRY_RUN").unwrap();
    assert_eq!(loaded["last_error"], json!("hello"));
    assert_eq!(loaded["phase"], json!("IDLE"));
}

#[tokio::test]
async fn balances_without_credentials_uses_dry_run() {
    let root = temp_dir("bal_paper");
    let svc = build_service(&root, json!({}), Value::Null, HashMap::new());
    let cfg = svc.config();
    let (wallet, _av, _eq, source) = svc.balances("testnet", &cfg, "DRY_RUN").await.unwrap();
    assert_eq!(source, "paper");
    assert!(wallet > 0.0);
}

#[tokio::test]
async fn balances_with_credentials_uses_exchange() {
    let root = temp_dir("bal_ex");
    let svc = build_service(
        &root,
        json!({}),
        json!({"api_key": "k", "secret_key": "s"}),
        HashMap::new(),
    );
    let cfg = svc.config();
    let (_w, _av, _eq, source) = svc.balances("testnet", &cfg, "DRY_RUN").await.unwrap();
    assert_eq!(source, "exchange");
}

#[tokio::test]
async fn trading_rules_from_exchange_info() {
    let root = temp_dir("rules");
    let svc = build_service(&root, json!({}), Value::Null, HashMap::new());
    let cfg = svc.config();
    let rules = svc.trading_rules("testnet", &cfg, "BTCUSDT").await.unwrap();
    assert_eq!(rules.symbol, "BTCUSDT");
}

// sanity: `FuturesExchange` is implemented on `FakeBinance`; keeps generic paths honest.
#[allow(dead_code)]
async fn _ex_compiles(ex: Arc<FakeBinance>) {
    let _ = ex.load_markets().await.is_ok();
}

// Keep `service`, `HashSet`, `Mutex` referenced when test bodies don't use them.
#[allow(dead_code)]
fn _touches() {
    let _: Option<BoxFut<()>> = None;
    let _ = service::CCXT_SYMBOL;
    let _: HashSet<String> = HashSet::new();
    let _ = Mutex::new(());
}
