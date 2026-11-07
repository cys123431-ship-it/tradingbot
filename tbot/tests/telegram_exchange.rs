#![allow(clippy::type_complexity, clippy::too_many_arguments)]
//! `/exchange`: switching the futures strategies between Binance testnet and
//! mainnet (Python `/setup` -> "22. 거래소/네트워크 전환").

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde_json::{json, Value};

use tbot::app::config_file::{dump_indent4, ConfigFile};
#[allow(unused_imports)]
use tbot::app::ipc::EngineApi;
use tbot::app::engine::Engine;
use tbot::app::telegram_api::TelegramApi;
use tbot::app::telegram_bot::TelegramBot;

mod common;
use common::Fake;

struct Ctx {
    bot: TelegramBot<Arc<Engine<Fake>>>,
    cfg_path: PathBuf,
    fake: Arc<Fake>,
}

impl Ctx {
    fn cfg(&self) -> Value {
        ConfigFile::new(self.cfg_path.clone()).load()
    }
    fn api(&self) -> Value {
        self.cfg().get("api").cloned().unwrap_or(Value::Null)
    }
    async fn send(&self, text: &str) -> (String, Value) {
        let msg = json!({"update_id": 1, "message": {"chat": {"id": 1}, "text": text}});
        let out = self.bot.handle_update(&msg).await;
        assert_eq!(out.len(), 1, "{text} must answer");
        let tbot::app::telegram_bot::Outgoing::Send { text, markup, .. } = &out[0] else {
            panic!("expected a Send reply");
        };
        (text.clone(), markup.clone().unwrap_or(Value::Null))
    }
    /// A callback press answers the query and edits the message.
    async fn press(&self, data: &str) -> (String, Value) {
        let cb = json!({"update_id": 2, "callback_query": {"id": "q", "data": data,
            "from": {"id": 1}, "message": {"chat": {"id": 1}, "message_id": 5}}});
        let out = self.bot.handle_update(&cb).await;
        for o in &out {
            if let tbot::app::telegram_bot::Outgoing::Edit { text, markup, .. } = o {
                return (text.clone(), markup.clone().unwrap_or(Value::Null));
            }
        }
        panic!("{data} must answer with an edit: {out:?}");
    }
}

fn buttons(markup: &Value) -> Vec<String> {
    markup
        .get("inline_keyboard")
        .and_then(|v| v.as_array())
        .map(|rows| {
            rows.iter()
                .flat_map(|r| r.as_array().cloned().unwrap_or_default())
                .filter_map(|b| b.get("callback_data").and_then(|v| v.as_str()).map(String::from))
                .collect()
        })
        .unwrap_or_default()
}

fn write_cfg(path: &Path, data: &Value) {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).unwrap();
    }
    std::fs::write(path, dump_indent4(data)).unwrap();
}

/// Both services answer the same fake exchange and read the same config file.
async fn controller(tmp_path: &Path, api: Value) -> Ctx {
    controller_with(tmp_path, api, false).await
}

async fn controller_with(tmp_path: &Path, api: Value, live: bool) -> Ctx {
    use tbot::store::clock::SystemClock;
    use tbot::strategy::service::{
        BoxFut, SmaCrossService, Variant,
    };
    use tbot::strategy::service::ServiceDeps;

    let cfg_path = tmp_path.join("config.json");
    write_cfg(
        &cfg_path,
        &json!({
            "api": api,
            "btc_ma_cross": {},
            "ma_cross": {},
            "telegram": {"token": "x", "chat_id": 1},
        }),
    );
    // An uptrend enters on the last bar, so `live` leaves a real position open.
    let price = if live { 86_000.0 } else { 85_200.0 };
    let h = common::Harness::new(tmp_path, live, "testnet", price, HashMap::new());
    let fake = h.fake.clone();
    let clock_ms = h.clock_ms.clone();
    if live {
        // One cycle on the harness service enters on the fake exchange, so the
        // account really holds a position (the harness config is not a file).
        h.set_time_from_now();
        for _ in 0..3 {
            h.run().await;
        }
    }

    let build = |section: &'static str, variant: Variant| {
        let cfg_file = Arc::new(ConfigFile::new(cfg_path.clone()));
        let writer_cfg = Arc::new(ConfigFile::new(cfg_path.clone()));
        let fake = fake.clone();
        let clock_ms = clock_ms.clone();
        let deps = ServiceDeps {
            config_getter: Arc::new(move || cfg_file.section(section)),
            credentials_getter: Arc::new(|_n: &str| json!({"api_key": "k", "secret_key": "s"})),
            exchange_factory: Arc::new(move |_n: &str, _c: &Value| Ok(fake.clone())),
            runtime_dir: tmp_path.to_path_buf(),
            notifier: None,
            // The engine resolves the network from the same config file.
            main_network_getter: {
                let cfg = Arc::new(ConfigFile::new(cfg_path.clone()));
                Some(Arc::new(move || {
                    tbot::app::config_file::main_network(&cfg.load())
                }))
            },
            clock: Arc::new(move || *clock_ms.lock().unwrap() as f64 / 1000.0),
            sleep: Arc::new(|_s: f64| Box::pin(async {}) as BoxFut<()>),
            environ: HashMap::new(),
            config_writer: None,
            config_file_writer: Some(Arc::new(
                move |section: &str, changes: serde_json::Map<String, Value>| {
                    let cfg = writer_cfg.clone();
                    let section = section.to_string();
                    Box::pin(async move { cfg.update_values(&section, changes).await }) as BoxFut<_>
                },
            )),
            store_clock: Arc::new(SystemClock),
        };
        Arc::new(SmaCrossService::new(variant, deps))
    };
    let btc = build("btc_ma_cross", Variant::BtcMa);
    let plus = build("ma_cross", Variant::MaPlus);
    let engine = Arc::new(Engine::with_services(
        Arc::new(ConfigFile::new(cfg_path.clone())),
        tmp_path.to_path_buf(),
        btc,
        plus,
    ));
    Ctx {
        bot: TelegramBot::new(TelegramApi::new("test-token".into()), engine.into(), 1),
        cfg_path,
        fake,
    }
}

fn testnet_api() -> Value {
    json!({
        "exchange_mode": "binance_testnet",
        "use_testnet": true,
        "testnet": {"api_key": "tk", "secret_key": "ts"},
        "mainnet": {"api_key": "mk", "secret_key": "ms"},
    })
}

#[tokio::test]
async fn exchange_menu_shows_the_current_network_and_the_other_choice() {
    let tmp = common::temp_dir("exch_menu");
    let ctx = controller(&tmp, testnet_api()).await;
    let (text, markup) = ctx.send("/exchange").await;
    assert!(text.contains("거래소"), "{text}");
    let cbs = buttons(&markup);
    assert!(cbs.contains(&"bm:exch:testnet".to_string()), "{cbs:?}");
    assert!(cbs.contains(&"bm:exch:mainnet".to_string()), "{cbs:?}");
    assert!(cbs.contains(&"bm:exch:upbit".to_string()), "{cbs:?}");
    // `/setup` is the old name of the same screen.
    let (same, _) = ctx.send("/setup").await;
    assert_eq!(same, text);
}

#[tokio::test]
async fn choosing_the_same_network_changes_nothing() {
    let tmp = common::temp_dir("exch_same");
    let ctx = controller(&tmp, testnet_api()).await;
    let (text, _) = ctx.press("bm:exch:testnet").await;
    assert!(text.contains("이미"), "{text}");
    assert_eq!(ctx.api()["exchange_mode"], json!("binance_testnet"));
}

#[tokio::test]
async fn confirming_testnet_to_mainnet_writes_the_mode_and_switches() {
    let tmp = common::temp_dir("exch_switch");
    let ctx = controller(&tmp, testnet_api()).await;
    let (preview, markup) = ctx.press("bm:exch:mainnet").await;
    assert!(preview.contains("메인넷"), "{preview}");
    assert!(buttons(&markup).contains(&"bm:exch:confirm:binance_mainnet".to_string()));
    let (done, _) = ctx.press("bm:exch:confirm:binance_mainnet").await;
    assert!(done.contains("메인넷"), "{done}");
    let api = ctx.api();
    assert_eq!(api["exchange_mode"], json!("binance_mainnet"));
    assert_eq!(api["use_testnet"], json!(false));
    // The status screen follows the new network.
    let (status, _) = ctx.press("bm:status").await;
    assert!(status.contains("메인넷"), "{status}");
}

#[tokio::test]
async fn upbit_is_refused_because_the_strategies_are_futures_only() {
    let tmp = common::temp_dir("exch_upbit");
    let ctx = controller(&tmp, testnet_api()).await;
    let (text, _) = ctx.press("bm:exch:upbit").await;
    assert!(text.contains("업비트"), "{text}");
    assert!(text.contains("현물"), "{text}");
    assert_eq!(ctx.api()["exchange_mode"], json!("binance_testnet"));
}

#[tokio::test]
async fn a_second_switch_inside_the_cooldown_is_refused() {
    let tmp = common::temp_dir("exch_cooldown");
    let ctx = controller(&tmp, testnet_api()).await;
    let (done, _) = ctx.press("bm:exch:confirm:binance_mainnet").await;
    assert!(done.contains("메인넷"), "{done}");
    let (text, _) = ctx.press("bm:exch:confirm:binance_testnet").await;
    assert!(text.contains("너무 자주"), "{text}");
    assert_eq!(ctx.api()["exchange_mode"], json!("binance_mainnet"));
}

#[tokio::test]
async fn an_open_position_blocks_the_switch() {
    let tmp = common::temp_dir("exch_held");
    let ctx = controller_with(&tmp, testnet_api(), true).await;
    // A live entry on the fake exchange leaves a real position open.
    let seen = ctx.fake.with_state(|s| s.position.clone());
    assert!(seen.is_some(), "the harness cycle must open a position: {seen:?}");
    let (preview, markup) = ctx.press("bm:exch:mainnet").await;
    assert!(preview.contains("보유 포지션"), "{preview}");
    assert!(
        !buttons(&markup).contains(&"bm:exch:confirm:binance_mainnet".to_string()),
        "no confirm button while a position is open"
    );
    let (refused, _) = ctx.press("bm:exch:confirm:binance_mainnet").await;
    assert!(refused.contains("보유 포지션"), "{refused}");
    assert_eq!(ctx.api()["exchange_mode"], json!("binance_testnet"));
}
