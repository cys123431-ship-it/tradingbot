#![allow(unused, clippy::all)] // ported Python tests: keep their shape
//! Telegram `/btcmacross` menu tests ported from the Python controller suite.
#![allow(clippy::type_complexity, clippy::too_many_arguments)]

mod common;

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde_json::{json, Map, Value};

use tbot::app::config_file::{dump_indent4, ConfigFile};
use tbot::app::engine::Engine;
use tbot::app::telegram_api::TelegramApi;
use tbot::app::telegram_bot::TelegramBot;
use tbot::store::order_state::OrderState;

use common::{Fake, Harness};
use tbot::app::ipc::EngineApi;

// ------------------------------------------- multi-coin controller (single tests)

struct SingleCtx {
    bot: TelegramBot<Arc<Engine<common::scope::MultiFake>>>,
    cfg_path: PathBuf,
}

impl SingleCtx {
    fn section(&self) -> Value {
        ConfigFile::new(self.cfg_path.clone()).section("btc_ma_cross")
    }
    async fn press(&self, action: &str) -> (String, Value) {
        self.bot.handle_btc_ma_cross_action(action).await
    }
}

/// Python `_coins()` from tests/test_btc_ma_cross_single.py.
fn single_coins() -> Map<String, Value> {
    use common::scope::{coin_default, coin_with};
    let mut m = Map::new();
    m.insert("BTCUSDT".into(), coin_default(900.0, 86_000.0));
    m.insert("ETHUSDT".into(), coin_default(800.0, 86_000.0));
    m.insert("SOLUSDT".into(), coin_default(700.0, 84_000.0));
    m.insert("1000PEPEUSDT".into(), coin_default(600.0, 85_000.0));
    m.insert("USDCUSDT".into(), coin_default(500.0, 85_000.0));
    m.insert(
        "AAPLUSDT".into(),
        coin_with(
            400.0,
            85_000.0,
            &[("contract_type", json!("TRADIFI_PERPETUAL"))],
        ),
    );
    m
}

fn service_over<X: tbot::exchange::FuturesExchange + 'static>(
    tmp: &Path,
    variant: tbot::strategy::service::Variant,
    section: &'static str,
    fake: Arc<X>,
) -> tbot::strategy::service::SmaCrossService<X> {
    use tbot::store::clock::SystemClock;
    use tbot::strategy::service::{BoxFut, ServiceDeps, SmaCrossService};
    let cfg_file = Arc::new(ConfigFile::new(tmp.join("config.json")));
    let deps = ServiceDeps {
        config_getter: Arc::new(move || cfg_file.section(section)),
        credentials_getter: Arc::new(|_n: &str| json!({"api_key": "k", "secret_key": "s"})),
        exchange_factory: Arc::new(move |_n: &str, _c: &Value| Ok(fake.clone())),
        runtime_dir: tmp.to_path_buf(),
        notifier: Some(Arc::new(|_t: String| Box::pin(async {}) as BoxFut<()>)),
        main_network_getter: Some(Arc::new(|| Some("testnet".to_string()))),
        clock: Arc::new(|| 1_790_000_000.0),
        sleep: Arc::new(|_s: f64| Box::pin(async {}) as BoxFut<()>),
        environ: HashMap::new(),
        config_writer: None,
        store_clock: Arc::new(SystemClock),
    };
    SmaCrossService::new(variant, deps)
}

/// Python `_single_controller(tmp_path)`.
fn single_controller(tmp: &Path) -> SingleCtx {
    use tbot::strategy::service::Variant;
    let cfg_path = tmp.join("config.json");
    write_cfg(
        &cfg_path,
        &json!({"btc_ma_cross": {}, "ma_cross": {}, "telegram": {"token": "x", "chat_id": 1}}),
    );
    let fake = Arc::new(common::scope::MultiFake::new(single_coins()));
    let btc = Arc::new(service_over(
        tmp,
        Variant::BtcMa,
        "btc_ma_cross",
        fake.clone(),
    ));
    let plus = Arc::new(service_over(tmp, Variant::MaPlus, "ma_cross", fake));
    let cfg = Arc::new(ConfigFile::new(cfg_path.clone()));
    let engine = Arc::new(Engine::with_services(cfg, tmp.to_path_buf(), btc, plus));
    SingleCtx {
        bot: TelegramBot::new(TelegramApi::new("test-token".into()), Arc::new(engine), 1),
        cfg_path,
    }
}

// ---------------------------------------------------------------- helpers

struct Ctx {
    bot: TelegramBot<Arc<Engine<Fake>>>,
    cfg_path: PathBuf,
    tmp: PathBuf,
    notes: Arc<std::sync::Mutex<Vec<String>>>,
}

impl Ctx {
    fn read_cfg(&self) -> Value {
        let cfg = ConfigFile::new(self.cfg_path.clone());
        cfg.load()
    }

    fn section(&self) -> Value {
        self.read_cfg()
            .get("btc_ma_cross")
            .cloned()
            .unwrap_or(Value::Object(Map::new()))
    }
}

fn write_cfg(path: &Path, data: &Value) {
    // write a well-formed config; the engine only touches btc_ma_cross.
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).unwrap();
    }
    std::fs::write(path, dump_indent4(data)).unwrap();
}

fn controller(tmp_path: &Path) -> Ctx {
    let cfg_path = tmp_path.join("config.json");
    let initial = json!({
        "btc_ma_cross": {},
        "ma_cross": {},
        "telegram": {"token": "x", "chat_id": 1},
    });
    write_cfg(&cfg_path, &initial);

    let h = Harness::new(tmp_path, false, "testnet", 85_200.0, HashMap::new());
    let h2 = Harness::new(tmp_path, false, "testnet", 85_200.0, HashMap::new());
    // Both services share the same exchange backing store.
    let fake = h.fake.clone();
    let btc_ma = Arc::new(build_btc(tmp_path, &h, fake.clone()));
    let ma_plus = Arc::new(build_ma(tmp_path, &h2, fake));
    let cfg = Arc::new(ConfigFile::new(cfg_path.clone()));
    let engine = Arc::new(Engine::with_services(
        cfg,
        tmp_path.to_path_buf(),
        btc_ma,
        ma_plus,
    ));
    let notes = h.notes.clone();
    let api = TelegramApi::new("test-token".to_string());
    Ctx {
        bot: TelegramBot::new(api, Arc::new(engine), 1),
        cfg_path,
        tmp: tmp_path.to_path_buf(),
        notes,
    }
}

fn build_btc(
    tmp: &Path,
    h: &Harness,
    fake: Arc<Fake>,
) -> tbot::strategy::service::SmaCrossService<Fake> {
    use tbot::store::clock::SystemClock;
    use tbot::strategy::service::{BoxFut, ServiceDeps, SmaCrossService, Variant};
    // Python: the service reads controller.cfg, the same config the buttons write.
    let cfg_file = Arc::new(ConfigFile::new(tmp.join("config.json")));
    let config_getter: Arc<dyn Fn() -> Value + Send + Sync> =
        Arc::new(move || cfg_file.section("btc_ma_cross"));
    let credentials_getter: Arc<dyn Fn(&str) -> Value + Send + Sync> =
        Arc::new(|_n: &str| json!({"api_key": "k", "secret_key": "s"}));
    let exchange_factory: Arc<
        dyn Fn(&str, &Value) -> Result<Arc<Fake>, tbot::exchange::ExchangeError> + Send + Sync,
    > = Arc::new(move |_n: &str, _c: &Value| Ok(fake.clone()));
    let notes = h.notes.clone();
    let notifier: Option<Arc<dyn Fn(String) -> BoxFut<()> + Send + Sync>> =
        Some(Arc::new(move |text: String| {
            let notes = notes.clone();
            Box::pin(async move {
                notes.lock().unwrap().push(text);
            })
        }));
    let network = h.network.clone();
    let main_network_getter: Option<Arc<dyn Fn() -> Option<String> + Send + Sync>> =
        Some(Arc::new(move || Some(network.clone())));
    let clock_ms = h.clock_ms.clone();
    let clock: Arc<dyn Fn() -> f64 + Send + Sync> =
        Arc::new(move || *clock_ms.lock().unwrap() as f64 / 1000.0);
    let sleep: Arc<dyn Fn(f64) -> BoxFut<()> + Send + Sync> =
        Arc::new(|_s: f64| Box::pin(async {}));
    let deps = ServiceDeps {
        config_getter,
        credentials_getter,
        exchange_factory,
        runtime_dir: tmp.to_path_buf(),
        notifier,
        main_network_getter,
        clock,
        sleep,
        environ: HashMap::new(),
        config_writer: None,
        store_clock: Arc::new(SystemClock),
    };
    SmaCrossService::new(Variant::BtcMa, deps)
}

fn build_ma(
    tmp: &Path,
    h: &Harness,
    fake: Arc<Fake>,
) -> tbot::strategy::service::SmaCrossService<Fake> {
    use tbot::store::clock::SystemClock;
    use tbot::strategy::service::{BoxFut, ServiceDeps, SmaCrossService, Variant};
    // Python: the service reads controller.cfg, the same config the buttons write.
    let cfg_file = Arc::new(ConfigFile::new(tmp.join("config.json")));
    let config_getter: Arc<dyn Fn() -> Value + Send + Sync> =
        Arc::new(move || cfg_file.section("ma_cross"));
    let credentials_getter: Arc<dyn Fn(&str) -> Value + Send + Sync> =
        Arc::new(|_n: &str| json!({"api_key": "k", "secret_key": "s"}));
    let exchange_factory: Arc<
        dyn Fn(&str, &Value) -> Result<Arc<Fake>, tbot::exchange::ExchangeError> + Send + Sync,
    > = Arc::new(move |_n: &str, _c: &Value| Ok(fake.clone()));
    let notes = h.notes.clone();
    let notifier: Option<Arc<dyn Fn(String) -> BoxFut<()> + Send + Sync>> =
        Some(Arc::new(move |text: String| {
            let notes = notes.clone();
            Box::pin(async move {
                notes.lock().unwrap().push(text);
            })
        }));
    let network = h.network.clone();
    let main_network_getter: Option<Arc<dyn Fn() -> Option<String> + Send + Sync>> =
        Some(Arc::new(move || Some(network.clone())));
    let clock_ms = h.clock_ms.clone();
    let clock: Arc<dyn Fn() -> f64 + Send + Sync> =
        Arc::new(move || *clock_ms.lock().unwrap() as f64 / 1000.0);
    let sleep: Arc<dyn Fn(f64) -> BoxFut<()> + Send + Sync> =
        Arc::new(|_s: f64| Box::pin(async {}));
    let deps = ServiceDeps {
        config_getter,
        credentials_getter,
        exchange_factory,
        runtime_dir: tmp.to_path_buf(),
        notifier,
        main_network_getter,
        clock,
        sleep,
        environ: HashMap::new(),
        config_writer: None,
        store_clock: Arc::new(SystemClock),
    };
    SmaCrossService::new(Variant::MaPlus, deps)
}

async fn press(ctx: &Ctx, action: &str) -> (String, Value) {
    ctx.bot.handle_btc_ma_cross_action(action).await
}

fn callbacks(markup: &Value) -> Vec<String> {
    markup
        .get("inline_keyboard")
        .and_then(|v| v.as_array())
        .map(|rows| {
            rows.iter()
                .flat_map(|row| row.as_array().cloned().unwrap_or_default())
                .filter_map(|b| {
                    b.get("callback_data")
                        .and_then(|v| v.as_str())
                        .map(|s| s.to_string())
                })
                .collect()
        })
        .unwrap_or_default()
}

fn labeled(markup: &Value) -> Vec<(String, String)> {
    markup
        .get("inline_keyboard")
        .and_then(|v| v.as_array())
        .map(|rows| {
            rows.iter()
                .flat_map(|row| row.as_array().cloned().unwrap_or_default())
                .filter_map(|b| {
                    let text = b.get("text").and_then(|v| v.as_str())?.to_string();
                    let data = b.get("callback_data").and_then(|v| v.as_str())?.to_string();
                    Some((text, data))
                })
                .collect()
        })
        .unwrap_or_default()
}

// ---------------------------------------------------------------- test_btc_ma_cross

#[tokio::test]
async fn telegram_menu_timeframe_emergency_stop_and_exclusivity() {
    let tmp = common::temp_dir("tg_menu");
    let ctx = controller(&tmp);
    let (text, markup) = press(&ctx, "status").await;
    assert!(text.contains("3/200 SMA") && text.contains("/setup"));
    let cbs = callbacks(&markup);
    for tf in ["15m", "30m", "1h", "2h", "4h"] {
        assert!(cbs.contains(&format!("bm:tf:{tf}")));
    }
    for choice in [0, 10, 20, 30, 50] {
        assert!(cbs.contains(&format!("bm:es:{choice}")));
    }
    press(&ctx, "tf:4h").await;
    assert_eq!(ctx.section()["timeframe"], json!("4h"));
    press(&ctx, "es:0").await;
    assert_eq!(ctx.section()["emergency_stop_roi_percent"], json!(0));
    assert!(press(&ctx, "status").await.0.contains("비상손절: OFF"));
    let (_, markup) = press(&ctx, "on").await;
    assert!(callbacks(&markup).contains(&"bm:confirm_on".to_string()));
    press(&ctx, "confirm_on").await;
    assert_eq!(ctx.section()["enabled"], json!(true));
    // skipped: options trading and btc pullback controllers are not ported.
    let rules = press(&ctx, "rules").await.0;
    assert!(rules.contains("+5%→+4%"), "{rules}");
}

#[tokio::test]
async fn pullback_on_turns_ma_cross_off() {
    let tmp = common::temp_dir("tg_pullback");
    let ctx = controller(&tmp);
    let mut raw = ctx.read_cfg();
    raw["btc_ma_cross"]["enabled"] = json!(true);
    write_cfg(&ctx.cfg_path, &raw);
    // Another futures strategy took over (Python: except_key="btc_ema_pullback").
    ctx.bot
        .engine()
        .call(
            "turn_off_others",
            None,
            json!({"except": "btc_ema_pullback"}),
        )
        .await
        .unwrap();
    assert_eq!(ctx.read_cfg()["btc_ma_cross"]["enabled"], json!(false));
    // skipped: btc_ema_pullback is not part of the Rust bot.
}

#[tokio::test]
async fn sma_buttons_and_header() {
    let tmp = common::temp_dir("tg_sma");
    let ctx = controller(&tmp);
    let (text, markup) = press(&ctx, "status").await;
    let cbs = callbacks(&markup);
    assert!(cbs.contains(&"bm:sma:3:200".to_string()));
    assert!(cbs.contains(&"bm:sma:3:120".to_string()));
    assert!(cbs.contains(&"bm:sma:1:200".to_string()));
    assert!(cbs.contains(&"bm:sma:5:60".to_string()));
    assert!(text.starts_with("📏 3/200 SMA"));
    let (text, markup) = press(&ctx, "sma:3:120").await;
    assert_eq!(ctx.section()["slow_period"], json!(120));
    assert!(text.contains("✅ SMA 기간 변경: 3/120") && text.contains("📏 3/120 SMA"));
    let labels: Vec<String> = labeled(&markup).into_iter().map(|(t, _)| t).collect();
    assert!(labels.contains(&"✅ SMA3/SMA120".to_string()));
}

#[tokio::test]
async fn preset_button_sets_the_fast_period_too() {
    let tmp = common::temp_dir("tg_preset");
    let ctx = controller(&tmp);
    let (text, _) = press(&ctx, "sma:1:200").await;
    assert_eq!(ctx.section()["fast_period"], json!(1));
    assert_eq!(ctx.section()["slow_period"], json!(200));
    assert!(text.contains("패스트 1 · 슬로우 200"));
    assert!(text.contains("📏 1/200 SMA"));
}

#[tokio::test]
async fn invalid_periods_are_rejected_without_changing_config() {
    let tmp = common::temp_dir("tg_invalid");
    let ctx = controller(&tmp);
    press(&ctx, "sma:3:120").await; // known-good baseline
    for action in [
        "sma:200:3",
        "sma:120:120",
        "sma:0:200",
        "sma:3:9999",
        "sma:2.5:120",
        "sma:3",
    ] {
        let (text, _) = press(&ctx, action).await;
        assert!(
            text.contains("기간 변경 실패") || text.contains("알 수 없는"),
            "{action}: {text}"
        );
    }
    let cfg = ctx.section();
    assert_eq!(cfg["fast_period"], json!(3));
    assert_eq!(cfg["slow_period"], json!(120));
}

#[tokio::test]
async fn sma_help_lists_the_text_command() {
    let tmp = common::temp_dir("tg_sma_help");
    let ctx = controller(&tmp);
    let (text, _) = press(&ctx, "sma_help").await;
    assert!(text.contains("/btcma <패스트> <슬로우>"));
    assert!(text.contains("/btcma 1 200"));
}

#[tokio::test]
async fn large_slow_period_lifts_candle_history() {
    let tmp = common::temp_dir("tg_large_slow");
    let ctx = controller(&tmp);
    press(&ctx, "sma:2:400").await;
    let cfg = ctx.section();
    assert_eq!(cfg["fast_period"], json!(2));
    assert_eq!(cfg["slow_period"], json!(400));
    assert!(
        cfg["candle_history"].as_i64().unwrap_or(0) >= 410,
        "candle_history={}",
        cfg["candle_history"]
    );
}

// ---------------------------------------------------------------- test_btc_ma_cross_single

#[tokio::test]
async fn timeframe_buttons_include_3m_and_5m() {
    let tmp = common::temp_dir("tg_tf");
    let ctx = controller(&tmp);
    let cbs = callbacks(&press(&ctx, "status").await.1);
    for tf in ["3m", "5m", "15m", "30m", "1h", "2h", "4h"] {
        assert!(cbs.contains(&format!("bm:tf:{tf}")));
    }
    let (text, _) = press(&ctx, "tf:3m").await;
    assert_eq!(ctx.section()["timeframe"], json!("3m"));
    assert!(text.contains("봉: 3분"));
    assert!(press(&ctx, "rules").await.0.contains("3분~4시간"));
}

#[tokio::test]
async fn coin_pick_buttons() {
    let tmp = common::temp_dir("tg_coin_pick");
    let ctx = single_controller(&tmp);
    assert!(callbacks(&ctx.press("status").await.1).contains(&"bm:pick".to_string()));
    let (text, markup) = ctx.press("pick").await;
    let picks = callbacks(&markup);
    assert!(text.contains("/macoin"), "{text}");
    assert_eq!(picks[0], "bm:coin:BTCUSDT");
    assert!(picks.contains(&"bm:coin:SOLUSDT".to_string()));
    assert!(!picks.contains(&"bm:coin:USDCUSDT".to_string()));
    assert!(!picks.contains(&"bm:coin:AAPLUSDT".to_string()));
    let (text, markup) = ctx.press("coin:SOLUSDT").await;
    let cfg = ctx.section();
    assert_eq!(cfg["scan_scope"], json!("single"));
    assert_eq!(cfg["single_symbol"], json!("SOLUSDT"));
    assert!(text.contains("종목: 지정 코인 SOLUSDT만"), "{text}");
    let labels: Vec<String> = labeled(&markup).into_iter().map(|(l, _)| l).collect();
    assert!(labels.contains(&"✅ 🎯 SOLUSDT".to_string()), "{labels:?}");
    assert!(labels.contains(&"🪙 BTC만".to_string()), "{labels:?}");
    let (text, _) = ctx.press("coin:AAPLUSDT").await;
    assert!(text.contains("TradFi"), "{text}");
    assert_eq!(ctx.section()["single_symbol"], json!("SOLUSDT"));
    ctx.press("scope:alts").await;
    assert_eq!(ctx.section()["scan_scope"], json!("alts"));
}

#[tokio::test]
async fn macoin_command_sets_the_symbol() {
    let tmp = common::temp_dir("tg_macoin");
    let ctx = single_controller(&tmp);
    let reply_text = |out: &Vec<tbot::app::telegram_bot::Outgoing>| -> (String, Value) {
        match out.last().expect("reply") {
            tbot::app::telegram_bot::Outgoing::Send { text, markup, .. } => {
                (text.clone(), markup.clone().unwrap_or(Value::Null))
            }
            other => panic!("expected Send, got {other:?}"),
        }
    };
    let update = |text: &str| json!({"update_id": 1, "message": {"chat": {"id": 1}, "text": text}});
    let out = ctx.bot.handle_update(&update("/macoin pepe")).await;
    assert_eq!(ctx.section()["single_symbol"], json!("1000PEPEUSDT"));
    assert!(reply_text(&out).0.contains("1000PEPEUSDT만 거래"));
    let out = ctx.bot.handle_update(&update("/macoin")).await;
    assert!(callbacks(&reply_text(&out).1).contains(&"bm:coin:BTCUSDT".to_string()));
}

#[tokio::test]
async fn manual_trend_choice_releases_the_forced_mode() {
    let tmp = common::temp_dir("tg_pls_trend");
    let ctx = controller(&tmp);
    let state = load_state(&ctx).await;
    for _ in 0..3 {
        close_profit_lock(&ctx, state.clone()).await;
    }
    let saved = load_state(&ctx).await;
    assert_eq!(saved["entry_mode_forced_cross"], json!(true));
    press(&ctx, "entry:trend").await;
    let saved = load_state(&ctx).await;
    assert_eq!(saved["entry_mode_forced_cross"], json!(false));
    assert_eq!(saved["profit_lock_exit_count"], json!(0));
    assert!(saved["pending_entry_mode_switch"].is_null());
    assert_eq!(ctx.section()["entry_mode"], json!("trend"));
}

#[tokio::test]
async fn telegram_switch_button_toggles_and_shows_the_count() {
    let tmp = common::temp_dir("tg_pls_btn");
    let ctx = controller(&tmp);
    let (text, markup) = press(&ctx, "status").await;
    let buttons = labeled(&markup);
    assert!(buttons.iter().any(|(label, data)| data == "bm:pls:off"
        && label.starts_with("🔁")
        && label.contains("ON (0/3)")));
    assert!(text.contains("익절 자동 전환"));
    let (_, markup) = press(&ctx, "pls:off").await;
    assert_eq!(ctx.section()["profit_lock_switch_count"], json!(0));
    assert!(labeled(&markup)
        .iter()
        .any(|(label, data)| data == "bm:pls:on" && label.contains("OFF")));
    let (_, markup) = press(&ctx, "pls:on").await;
    assert_eq!(ctx.section()["profit_lock_switch_count"], json!(3));
    assert!(labeled(&markup)
        .iter()
        .any(|(label, data)| data == "bm:pls:off" && label.contains("ON (0/3)")));
    assert!(press(&ctx, "rules")
        .await
        .0
        .contains("익절 자동 전환: 수익 확보 익절 3회마다"));
}

// ---------------------------------------------------------------- test_btc_ma_cross_tradfi

#[tokio::test]
async fn leverage_button_saves_and_applies_from_the_next_entry() {
    let tmp = common::temp_dir("tg_lev");
    let ctx = controller(&tmp);
    let (_, markup) = press(&ctx, "status").await;
    let cbs = callbacks(&markup);
    for lev in [2, 3, 4, 5] {
        assert!(cbs.contains(&format!("bm:lev:{lev}")));
    }
    let (text, _) = press(&ctx, "lev:3").await;
    assert_eq!(ctx.section()["leverage"], json!(3));
    assert!(text.contains("레버리지: 3x"));
    assert!(text.contains("다음 진입부터 적용"));
    press(&ctx, "lev:20").await;
    assert_eq!(ctx.section()["leverage"], json!(3));
}

#[tokio::test]
async fn tradfi_button_is_refused_on_testnet() {
    let tmp = common::temp_dir("tg_tradfi_refuse");
    let ctx = controller(&tmp);
    let (text, markup) = press(&ctx, "scope:tradfi").await;
    assert!(text.contains("메인넷에서만"));
    assert_ne!(ctx.section().get("scan_scope"), Some(&json!("tradfi")));
    assert!(labeled(&markup)
        .iter()
        .any(|(_, data)| data == "bm:scope:tradfi"));
}

#[tokio::test]
async fn alts_scope_still_selectable() {
    let tmp = common::temp_dir("tg_scope_alts");
    let ctx = controller(&tmp);
    let mut raw = ctx.read_cfg();
    if let Some(sec) = raw.get_mut("btc_ma_cross").and_then(|v| v.as_object_mut()) {
        sec.insert("scan_scope".into(), json!("alts"));
    }
    write_cfg(&ctx.cfg_path, &raw);
    press(&ctx, "scope:btc").await;
    assert_eq!(ctx.section()["scan_scope"], json!("btc"));
    press(&ctx, "scope:alts").await;
    assert_eq!(ctx.section()["scan_scope"], json!("alts"));
}

// ---------------------------------------------------------------- helpers used above

async fn load_state(ctx: &Ctx) -> Value {
    let engine = ctx.bot.engine();
    let network = engine
        .call("current_network", Some("btc_ma"), json!({}))
        .await
        .unwrap();
    let network = network.as_str().unwrap_or("testnet").to_string();
    let mode = engine
        .call("mode", Some("btc_ma"), json!({}))
        .await
        .unwrap();
    let mode = mode.as_str().unwrap_or("DRY_RUN").to_string();
    engine
        .call(
            "load_state",
            Some("btc_ma"),
            json!({"network": network, "mode": mode}),
        )
        .await
        .unwrap()
}

async fn close_profit_lock(ctx: &Ctx, _state: Value) {
    // Drive the service cycle directly: options/pullback controllers are not ported,
    // so a manual trade close is simulated through the engine state.
    let engine = ctx.bot.engine();
    let network = "testnet";
    let mode = "DRY_RUN";
    let mut state = engine
        .call(
            "load_state",
            Some("btc_ma"),
            json!({"network": network, "mode": mode}),
        )
        .await
        .unwrap();
    let count = state
        .get("profit_lock_exit_count")
        .and_then(|v| v.as_i64())
        .unwrap_or(0);
    state["profit_lock_exit_count"] = json!(count + 1);
    if count + 1 >= 3 {
        state["entry_mode_forced_cross"] = json!(true);
        state["pending_entry_mode_switch"] =
            json!({"entry_mode": "cross", "from_mode": "trend", "exit_count": 3});
        state["profit_lock_exit_count"] = json!(0);
    }
    engine
        .call(
            "save_state",
            Some("btc_ma"),
            json!({"network": network, "mode": mode, "state": state}),
        )
        .await
        .unwrap();
}

// silence unused when a helper is only used by some tests
#[allow(dead_code)]
fn _touch(_s: OrderState) {}

#[tokio::test]
async fn updates_from_other_chats_are_ignored() {
    let tmp = common::temp_dir("tg_owner");
    let ctx = controller(&tmp);
    let msg = json!({"update_id": 1, "message": {"chat": {"id": 999}, "text": "/stop"}});
    assert!(ctx.bot.handle_update(&msg).await.is_empty());
    let cb = json!({"update_id": 2, "callback_query": {"id": "q", "data": "bm:on",
        "from": {"id": 999}, "message": {"chat": {"id": 999}, "message_id": 5}}});
    assert!(ctx.bot.handle_update(&cb).await.is_empty());
    assert_ne!(ctx.section()["enabled"], json!(true));
}
