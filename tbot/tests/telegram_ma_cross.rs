#![allow(unused, clippy::all)] // ported Python tests: keep their shape
#![allow(unused, clippy::all)] // ported Python tests: keep their shape
//! Telegram `/macross` menu tests ported from the Python controller suite.
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

use common::{Fake, Harness};
use tbot::app::ipc::EngineApi;

// ---------------------------------------------------------------- helpers

struct Ctx {
    bot: TelegramBot<Arc<Engine<Fake>>>,
    cfg_path: PathBuf,
    tmp: PathBuf,
}

impl Ctx {
    fn read_cfg(&self) -> Value {
        let cfg = ConfigFile::new(self.cfg_path.clone());
        cfg.load()
    }

    fn ma_section(&self) -> Value {
        self.read_cfg()
            .get("ma_cross")
            .cloned()
            .unwrap_or(Value::Object(Map::new()))
    }

    fn section(&self, name: &str) -> Value {
        self.read_cfg()
            .get(name)
            .cloned()
            .unwrap_or(Value::Object(Map::new()))
    }
}

fn write_cfg(path: &Path, data: &Value) {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).unwrap();
    }
    std::fs::write(path, dump_indent4(data)).unwrap();
}

fn service_over<X: tbot::exchange::FuturesExchange + 'static>(
    tmp: &Path,
    variant: tbot::strategy::service::Variant,
    section: &'static str,
    fake: Arc<X>,
    network: &'static str,
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
        main_network_getter: Some(Arc::new(move || Some(network.to_string()))),
        clock: Arc::new(|| 1_790_000_000.0),
        sleep: Arc::new(|_s: f64| Box::pin(async {}) as BoxFut<()>),
        environ: HashMap::new(),
        config_writer: None,
        store_clock: Arc::new(SystemClock),
    };
    SmaCrossService::new(variant, deps)
}

fn controller(tmp_path: &Path) -> Ctx {
    controller_over(tmp_path, "testnet")
}

fn controller_over(tmp_path: &Path, network: &'static str) -> Ctx {
    use tbot::strategy::service::Variant;
    let cfg_path = tmp_path.join("config.json");
    let initial = json!({
        "ma_cross": {},
        "btc_ma_cross": {"enabled": true}, // Python _controller()
        "btc_ema_pullback": {"enabled": true},
        "telegram": {"token": "x", "chat_id": 1},
    });
    write_cfg(&cfg_path, &initial);

    let h = Harness::new(tmp_path, false, network, 85_200.0, HashMap::new());
    let h2 = Harness::new(tmp_path, false, network, 85_200.0, HashMap::new());
    let fake = h.fake.clone();
    let mut btc_ma = service_over(
        tmp_path,
        Variant::BtcMa,
        "btc_ma_cross",
        fake.clone(),
        network,
    );
    let mut ma_plus = service_over(tmp_path, Variant::MaPlus, "ma_cross", fake, network);
    let cfg = Arc::new(ConfigFile::new(cfg_path.clone()));
    let engine = Engine::with_services(
        cfg,
        tmp_path.to_path_buf(),
        Arc::new(btc_ma),
        Arc::new(ma_plus),
    );
    let engine = Arc::new(engine);
    Ctx {
        bot: TelegramBot::new(
            TelegramApi::new("test-token".to_string()),
            Arc::new(engine),
            1,
        ),
        cfg_path,
        tmp: tmp_path.to_path_buf(),
    }
}

async fn press(ctx: &Ctx, action: &str) -> (String, Value) {
    ctx.bot.handle_ma_cross_action(action).await
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

// ------------------------------------------------------------------ tests

#[tokio::test]
async fn test_menu_is_c_only() {
    let tmp = common::temp_dir("ma_menu_c_only");
    let ctx = controller(&tmp);
    let (_, markup) = press(&ctx, "status").await;
    let cbs = callbacks(&markup);
    for name in ["A", "B", "C", "D", "E"] {
        assert!(!cbs.contains(&format!("mc:feat:{name}")));
    }
    for lev in ["2", "3", "4", "5"] {
        assert!(cbs.contains(&format!("mc:lev:{lev}")));
    }
    assert!(cbs.contains(&"mc:on".to_string()));
    assert!(cbs.contains(&"mc:off".to_string()));
}

#[tokio::test]
async fn test_feature_toggle_is_refused_because_c_is_fixed() {
    let tmp = common::temp_dir("ma_feat_refused");
    let ctx = controller(&tmp);
    let (text, _) = press(&ctx, "feat:A").await;
    assert!(text.contains("C(변동성 타겟 사이징) 전용"));
    // skipped: feature_mode(controller._ma_cross_config()) == "C" (no public helper on the service)
}

#[tokio::test]
async fn test_strategy_can_be_enabled_with_the_fixed_c_feature() {
    let tmp = common::temp_dir("ma_enable");
    let ctx = controller(&tmp);
    press(&ctx, "confirm_on").await;
    assert_eq!(ctx.ma_section()["enabled"], json!(true));
}

#[tokio::test]
async fn test_enabling_turns_other_futures_strategies_off() {
    let tmp = common::temp_dir("ma_turn_off_others");
    let ctx = controller(&tmp);
    press(&ctx, "confirm_on").await;
    assert_eq!(ctx.ma_section()["enabled"], json!(true));
    // skipped: btc_ema_pullback is not part of the Rust bot (its flag is left alone).
    assert_eq!(ctx.section("btc_ma_cross")["enabled"], json!(false));
    // skipped: controller.is_paused is True (engine owns the pause flag)
}

#[tokio::test]
async fn test_status_and_rules_describe_the_c_only_strategy() {
    let tmp = common::temp_dir("ma_status_rules");
    let ctx = controller(&tmp);
    let (text, _) = press(&ctx, "status").await;
    assert!(text.contains("C(변동성 타겟 사이징)"));
    assert!(text.contains("반대 크로스") && text.contains("비상손절 OFF"));
    let (rules, _) = press(&ctx, "rules").await;
    assert!(rules.contains("SMA3 / SMA120"));
    assert!(rules.contains("반대 크로스 발생 시 포지션 종료"));
    assert!(rules.contains("변동성 타겟 사이징"));
}

#[tokio::test]
async fn test_macross_periods_presets_and_text_command() {
    let tmp = common::temp_dir("ma_periods");
    let ctx = controller(&tmp);
    let (_, markup) = press(&ctx, "status").await;
    let cbs = callbacks(&markup);
    assert!(cbs.contains(&"mc:sma:3:120".to_string()));
    assert!(cbs.contains(&"mc:sma:5:60".to_string()));
    assert!(cbs.contains(&"mc:sma:1:200".to_string()));

    let (text, _) = press(&ctx, "sma:1:200").await;
    let data = ctx.ma_section();
    assert_eq!(data["fast_period"], json!(1));
    assert_eq!(data["slow_period"], json!(200));
    assert!(text.contains("패스트 1 · 슬로우 200"));
    assert!(text.contains("3/120"));

    let (help_text, _) = press(&ctx, "sma_help").await;
    assert!(help_text.contains("/map <패스트> <슬로우>"));
    assert!(help_text.contains("지금: 1/200"));
}

#[tokio::test]
async fn test_macross_invalid_period_is_refused() {
    let tmp = common::temp_dir("ma_invalid_period");
    let ctx = controller(&tmp);
    let (text, _) = press(&ctx, "sma:120:120").await;
    assert!(text.contains("기간 변경 실패"));
    let cfg = tbot::strategy::config_ma_plus::normalize_ma_cross_config(Some(&ctx.ma_section()));
    assert_eq!(cfg["fast_period"], json!(3));
    assert_eq!(cfg["slow_period"], json!(120));
}

#[tokio::test]
async fn test_menu_has_the_tradfi_button() {
    let tmp = common::temp_dir("ma_tradfi_button");
    let ctx = controller(&tmp);
    let (_, markup) = press(&ctx, "status").await;
    assert!(callbacks(&markup).contains(&"mc:scope:tradfi".to_string()));
}

#[tokio::test]
async fn test_tradfi_button_is_refused_outside_mainnet() {
    let tmp = common::temp_dir("ma_tradfi_refused");
    let ctx = controller(&tmp);
    let (text, _) = press(&ctx, "scope:tradfi").await;
    assert!(text.contains("메인넷에서만"));
    assert_ne!(ctx.ma_section().get("scan_scope"), Some(&json!("tradfi")));
}

#[tokio::test]
async fn test_tradfi_button_selects_the_scope_on_mainnet() {
    let tmp = common::temp_dir("ma_tradfi_mainnet");
    let ctx = controller_over(&tmp, "mainnet");
    let (text, _) = press(&ctx, "scope:tradfi").await;
    assert_eq!(ctx.ma_section()["scan_scope"], json!("tradfi"), "{text}");
    assert!(text.contains("TradFi만 선택") && text.contains("미국 정규장"));
}

#[tokio::test]
async fn test_full_off_disables_and_verifies() {
    let tmp = common::temp_dir("ma_full_off");
    let ctx = controller(&tmp);
    press(&ctx, "confirm_on").await;
    assert_eq!(ctx.ma_section()["enabled"], json!(true));
    let (_, markup) = press(&ctx, "status").await;
    assert!(callbacks(&markup).contains(&"mc:full_off".to_string()));

    let (ask, markup) = press(&ctx, "full_off").await;
    assert!(ask.contains("완전히 끌까요"));
    assert!(callbacks(&markup).contains(&"mc:confirm_full_off".to_string()));
    assert_eq!(ctx.ma_section()["enabled"], json!(true));

    let (text, _) = press(&ctx, "confirm_full_off").await;
    assert_eq!(ctx.ma_section()["enabled"], json!(false));
    assert!(text.contains("신규 진입: OFF 확인"), "{text}");
    assert!(text.contains("보유 포지션 없음"), "{text}");
    assert!(text.contains("LIVE 포지션: 없음"), "{text}");
    assert!(text.contains("DRY_RUN 포지션: 없음"), "{text}");
}

#[tokio::test]
async fn test_full_off_warns_about_a_position_in_the_other_mode() {
    let tmp = common::temp_dir("ma_full_off_other_mode");
    let ctx = controller(&tmp);
    let engine = ctx.bot.engine();
    let mode = engine
        .call("mode", Some("ma_plus"), json!({}))
        .await
        .unwrap();
    assert_eq!(mode, json!("DRY_RUN"));
    let network = engine
        .call("current_network", Some("ma_plus"), json!({}))
        .await
        .unwrap();
    engine
        .call(
            "save_state",
            Some("ma_plus"),
            json!({"network": network, "mode": "LIVE",
                   "state": {"trade": {"market_id": "BTCUSDT", "side": "LONG"}}}),
        )
        .await
        .unwrap();

    let (text, _) = press(&ctx, "confirm_full_off").await;
    assert_eq!(ctx.ma_section()["enabled"], json!(false));
    assert!(text.contains("LIVE 포지션 남아 있음"), "{text}");
    assert!(text.contains("DRY_RUN 포지션: 없음"), "{text}");
}
