//! `tbot-engine` process: run the two SmaCross strategies and serve the
//! local control API (IPC).
#![allow(clippy::type_complexity)] // service dependency closures

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};

use crate::app::config_file::{credentials, dump_indent4, main_network, ConfigFile};
use crate::app::ipc::{self, BoxFut, EngineApi, Handler, Request};
use crate::app::telegram_api::TelegramApi;
use crate::exchange::binance::{BinanceClient, Credentials, Network};
use crate::exchange::binance_futures::BinanceFutures;
use crate::exchange::binance_options::{BinanceOptionsClient, CredentialsGetter};
use crate::exchange::FuturesExchange;
use crate::store::clock::{Clock, SystemClock};
use crate::strategy::options_config::normalize_options_config;
use crate::strategy::options_service::{ConfigGetter, Notifier, OptionsService, Sleeper};
use crate::strategy::service::base::{BINANCE_MAINNET, BINANCE_TESTNET, UPBIT_MODE};
use crate::strategy::service::{ServiceDeps, SmaCrossService, Variant};
use crate::strategy::tradfi::market_in_tradfi_scope;

const VERSION: &str = env!("CARGO_PKG_VERSION");
const HEARTBEAT_FILE: &str = "tbot_engine_heartbeat.json";

/// config.json section and runtime state file of the options sleeve (shared with Python).
const OPTIONS_SECTION: &str = "options_trading";
const OPTIONS_STATE_FILE: &str = "options_trading_state.json";
/// Marker written once when tbot first runs the options sleeve. Until it is set the
/// sleeve is forced OFF, so a stale `enabled: true` left by the retired Python bot can
/// never start real option orders right after a deploy.
const OPTIONS_PORT_ACK: &str = "tbot_port_ack";
/// config.json section and display label of the TradFi alignment strategy.
const TRADFI_SECTION: &str = crate::strategy::config_tradfi::CONFIG_KEY;
const TRADFI_LABEL: &str = "TradFi 1/5/120";

/// Coin list size requested by the Telegram "pick a coin" keyboard.
const PICK_LIST_SIZE: usize = 12;

/// Python `exchange_switch_cooldown_seconds`.
const EXCHANGE_SWITCH_COOLDOWN_SECS: f64 = 30.0;

pub struct Engine<E: FuturesExchange + 'static> {
    cfg: Arc<ConfigFile>,
    btc_ma: Arc<SmaCrossService<E>>,
    ma_plus: Arc<SmaCrossService<E>>,
    /// TradFi-only 1/5/120 alignment strategy (`/tradfi`); None in hand-built test engines.
    tradfi: Option<Arc<SmaCrossService<E>>>,
    /// Binance European Options sleeve (None in tests that build the engine by hand).
    options: Option<Arc<OptionsService<BinanceOptionsClient>>>,
    #[allow(dead_code)] // notices go through the service notifier
    telegram: Option<TelegramApi>,
    started_at: i64,
    runtime_dir: PathBuf,
    /// Python `exchange_switch_lock` + `last_exchange_switch_ts`.
    exchange_switch: tokio::sync::Mutex<Option<f64>>,
}

impl Engine<BinanceFutures> {
    pub fn from_env() -> Result<Engine<BinanceFutures>, String> {
        let cfg_path =
            std::env::var("TBOT_CONFIG").unwrap_or_else(|_| "../config.json".to_string());
        let runtime_dir = ipc::runtime_dir();
        std::fs::create_dir_all(&runtime_dir).map_err(|e| format!("OSError: {e}"))?;
        let cfg = Arc::new(ConfigFile::new(PathBuf::from(cfg_path)));

        let (tg_token, tg_chat) = telegram_settings(&cfg.load());
        let telegram = tg_token.clone().map(TelegramApi::new);

        let notifier: Arc<dyn Fn(String) -> BoxFut<()> + Send + Sync> = {
            let api = telegram.clone();
            let chat = tg_chat.clone();
            Arc::new(move |text: String| -> BoxFut<()> {
                let api = api.clone();
                let chat = chat.clone();
                Box::pin(async move {
                    let (Some(api), Some(chat)) = (api, chat) else {
                        log_line("notifier: telegram not configured");
                        return;
                    };
                    if chat.trim().is_empty() {
                        log_line("notifier: telegram chat_id empty");
                        return;
                    }
                    match api.send_message(chat.trim(), &text, None).await {
                        Ok(_) => {}
                        Err(e) => log_line(&format!("notifier: send_message failed: {e}")),
                    }
                })
            })
        };

        let btc_ma = build_service(&cfg, runtime_dir.clone(), Variant::BtcMa, notifier.clone())?;
        let tradfi = Some(build_service(
            &cfg,
            runtime_dir.clone(),
            Variant::TradFi,
            notifier.clone(),
        )?);
        let options = Some(build_options_service(&cfg, &runtime_dir, notifier.clone()));
        let ma_plus = build_service(&cfg, runtime_dir.clone(), Variant::MaPlus, notifier)?;

        let started_at = now_secs();
        Ok(Engine {
            cfg,
            btc_ma,
            ma_plus,
            tradfi,
            options,
            telegram,
            started_at,
            runtime_dir,
            exchange_switch: tokio::sync::Mutex::new(None),
        })
    }
}

impl<E: FuturesExchange + 'static> Engine<E> {
    /// Build an engine around caller-provided services (used by tests).
    pub fn with_services(
        cfg: Arc<ConfigFile>,
        runtime_dir: PathBuf,
        btc_ma: Arc<SmaCrossService<E>>,
        ma_plus: Arc<SmaCrossService<E>>,
    ) -> Engine<E> {
        Engine {
            cfg,
            btc_ma,
            ma_plus,
            tradfi: None,
            options: None,
            telegram: None,
            started_at: now_secs(),
            runtime_dir,
            exchange_switch: tokio::sync::Mutex::new(None),
        }
    }

    pub async fn run(self: Arc<Self>) -> Result<(), String> {
        let token = ipc::ensure_token(&self.runtime_dir)?;
        let port = ipc::ipc_port();
        let addr = format!("127.0.0.1:{port}");
        let listener = tokio::net::TcpListener::bind(&addr)
            .await
            .map_err(|e| format!("OSError: bind {addr}: {e}"))?;
        log_line(&format!("engine: listening on {addr}"));

        let handler: Handler = {
            let engine = Arc::clone(&self);
            Arc::new(move |req: Request| -> BoxFut<Result<Value, String>> {
                let engine = Arc::clone(&engine);
                Box::pin(async move { engine.handle(req).await })
            })
        };

        let ipc_task = tokio::spawn(ipc::serve(listener, token, handler));

        let btc_loop = {
            let engine = Arc::clone(&self);
            let service = Arc::clone(&self.btc_ma);
            tokio::spawn(async move { engine.strategy_loop(service).await })
        };
        let ma_loop = {
            let engine = Arc::clone(&self);
            let service = Arc::clone(&self.ma_plus);
            tokio::spawn(async move { engine.strategy_loop(service).await })
        };
        let tradfi_loop = self.tradfi.clone().map(|service| {
            let engine = Arc::clone(&self);
            tokio::spawn(async move { engine.strategy_loop(service).await })
        });
        let heartbeat = {
            let engine = Arc::clone(&self);
            tokio::spawn(async move { engine.heartbeat_loop().await })
        };
        let options_loop = self.options.clone().map(|service| {
            let engine = Arc::clone(&self);
            tokio::spawn(async move { engine.options_loop(service).await })
        });

        let ctrl_c = tokio::signal::ctrl_c();
        #[cfg(unix)]
        {
            let mut sigterm =
                tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                    .map_err(|e| format!("OSError: sigterm: {e}"))?;
            tokio::select! {
                _ = ctrl_c => { log_line("engine: SIGINT received"); }
                _ = sigterm.recv() => { log_line("engine: SIGTERM received"); }
            }
        }
        #[cfg(not(unix))]
        {
            let _ = ctrl_c.await;
            log_line("engine: ctrl-c received");
        }

        ipc_task.abort();
        btc_loop.abort();
        ma_loop.abort();
        heartbeat.abort();
        if let Some(task) = tradfi_loop {
            task.abort();
        }
        if let Some(task) = options_loop {
            task.abort();
        }
        self.write_heartbeat().await;
        Ok(())
    }

    async fn strategy_loop(&self, service: Arc<SmaCrossService<E>>) {
        tokio::time::sleep(std::time::Duration::from_secs(30)).await;
        loop {
            let mut interval: i64 = 10;
            let result: Result<(), String> = async {
                let cfg = service.config();
                interval = cfg_field_i64(&cfg, "loop_interval_seconds", 10);
                if service.variant == Variant::TradFi {
                    self.tradfi_drawdown_guard(&service).await;
                }
                if service.needs_cycle() {
                    service.run_cycle().await.map_err(|e| e.to_string())?;
                }
                Ok(())
            }
            .await;
            if let Err(e) = result {
                log_line(&format!(
                    "engine: {} cycle failed: {e}",
                    service.variant.runtime_subdir()
                ));
            }
            let sleep_secs = interval.max(5) as u64;
            tokio::time::sleep(std::time::Duration::from_secs(sleep_secs)).await;
        }
    }

    /// /tradfi account guard (user rule 2026-11-21): when total equity drops 50%
    /// below the equity recorded at turn-on, close the position at market,
    /// switch the strategy off and refuse to run it for 30 days. While halted
    /// a still-open position is closed again on every loop.
    async fn tradfi_drawdown_guard(&self, service: &SmaCrossService<E>) {
        use crate::strategy::config_tradfi::{
            drawdown_decision, halt_equity, is_halted, DrawdownAction, BASE_EQUITY_KEY,
            DRAWDOWN_HALT_DAYS, DRAWDOWN_HALT_PCT, HALT_UNTIL_KEY,
        };
        let cfg = service.config();
        let now = service.now_ms();
        let enabled = cfg
            .get("enabled")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let mode = service.mode(Some(&cfg));
        let Some(network) = service.current_network(Some(&cfg)) else {
            return;
        };
        let holding = service
            .load_state(&network, &mode)
            .map(|s| s.get("trade").map(|v| !v.is_null()).unwrap_or(false))
            .unwrap_or(false);
        if is_halted(&cfg, now) {
            if enabled {
                let mut changes = Map::new();
                changes.insert("enabled".to_string(), Value::Bool(false));
                if let Err(e) = self.cfg.update_values(TRADFI_SECTION, changes).await {
                    log_line(&format!("engine: tradfi halt disable failed: {e}"));
                }
            }
            if holding {
                match service.close_position("drawdown_halt").await {
                    Ok(v) => log_line(&format!("engine: tradfi halt close retry: {v}")),
                    Err(e) => log_line(&format!("engine: tradfi halt close retry failed: {e}")),
                }
            }
            return;
        }
        if !enabled && !holding {
            return;
        }
        let equity = match service.balances(&network, &cfg, &mode).await {
            Ok((_, _, equity, _)) => equity,
            Err(e) => {
                log_line(&format!("engine: tradfi drawdown balance failed: {e}"));
                return;
            }
        };
        match drawdown_decision(&cfg, equity, now) {
            DrawdownAction::None => {}
            DrawdownAction::SetBase(base) => {
                let mut changes = Map::new();
                changes.insert(BASE_EQUITY_KEY.to_string(), json!(base));
                if let Err(e) = self.cfg.update_values(TRADFI_SECTION, changes).await {
                    log_line(&format!("engine: tradfi base equity write failed: {e}"));
                } else {
                    log_line(&format!("engine: tradfi base equity set to {base:.4} USDT"));
                }
            }
            DrawdownAction::Halt {
                base,
                equity,
                until_ms,
            } => {
                let mut changes = Map::new();
                changes.insert("enabled".to_string(), Value::Bool(false));
                changes.insert(HALT_UNTIL_KEY.to_string(), json!(until_ms));
                if let Err(e) = self.cfg.update_values(TRADFI_SECTION, changes).await {
                    log_line(&format!("engine: tradfi halt write failed: {e}"));
                }
                let close = if holding {
                    match service.close_position("drawdown_halt").await {
                        Ok(v) => {
                            let action = v.get("action").and_then(|a| a.as_str()).unwrap_or("");
                            let reason = v.get("reason").and_then(|a| a.as_str()).unwrap_or("");
                            format!("청산 요청: {action} {reason}").trim().to_string()
                        }
                        Err(e) => format!("⚠️ 청산 실패 (다음 주기에 다시 시도): {e}"),
                    }
                } else {
                    "보유 포지션 없음".to_string()
                };
                log_line(&format!(
                    "engine: tradfi drawdown halt equity={equity:.4} base={base:.4} until={until_ms} close={close}"
                ));
                let until = chrono::DateTime::from_timestamp_millis(until_ms)
                    .map(|t| {
                        t.with_timezone(&chrono::FixedOffset::east_opt(9 * 3600).unwrap())
                            .format("%Y-%m-%d %H:%M KST")
                            .to_string()
                    })
                    .unwrap_or_default();
                service
                    .notify(
                        format!(
                            "⛔ /tradfi 손실 한도 도달 — 매매 정지\n총자본 {equity:.2} USDT (기준 {base:.2} USDT의 -{pct}% 선 {line:.2} USDT 이하)\n· {close}\n· 전략 OFF, {days}일간 다시 켤 수 없음 ({until}까지)",
                            pct = DRAWDOWN_HALT_PCT,
                            line = halt_equity(base),
                            days = DRAWDOWN_HALT_DAYS,
                        ),
                        Some("tradfi_drawdown_halt"),
                    )
                    .await;
            }
        }
    }

    /// Python `_options_trading_loop`: one cycle, then sleep max(5, manage_interval).
    async fn options_loop(&self, service: Arc<OptionsService<BinanceOptionsClient>>) {
        self.options_port_gate().await;
        tokio::time::sleep(std::time::Duration::from_secs(20)).await;
        loop {
            let result = service.run_cycle(false, false).await;
            let action = result.get("action").and_then(|v| v.as_str()).unwrap_or("");
            if action == "blocked" {
                let reason = result.get("reason").and_then(|v| v.as_str()).unwrap_or("");
                log_line(&format!("engine: options cycle blocked: {reason}"));
            }
            let interval = cfg_field_i64(&service.config(), "manage_interval_seconds", 10);
            tokio::time::sleep(std::time::Duration::from_secs(interval.max(5) as u64)).await;
        }
    }

    /// User rule: option entries always start OFF. Every engine start switches
    /// them off; only the Telegram ON button (two checks) turns them on again.
    async fn options_port_gate(&self) {
        let section = self.cfg.section(OPTIONS_SECTION);
        let was_on = section
            .get("enabled")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let acked = section.get(OPTIONS_PORT_ACK).and_then(|v| v.as_bool()) == Some(true);
        if !was_on && acked {
            return;
        }
        let mut changes = Map::new();
        changes.insert("enabled".to_string(), Value::Bool(false));
        changes.insert(OPTIONS_PORT_ACK.to_string(), Value::Bool(true));
        if let Err(e) = self.cfg.update_values(OPTIONS_SECTION, changes).await {
            log_line(&format!("engine: options start-off write failed: {e}"));
            return;
        }
        log_line(&format!(
            "engine: options entries forced OFF at engine start (was_on={was_on})"
        ));
    }

    /// Option entries run only while every futures strategy is OFF.
    fn futures_block_reason(cfg: &ConfigFile) -> Option<String> {
        let on: Vec<&str> = [
            ("btc_ma_cross", "/btcmacross"),
            ("ma_cross", "/macross"),
            (TRADFI_SECTION, "/tradfi"),
        ]
        .into_iter()
        .filter(|(section, _)| {
            cfg.section(section)
                .get("enabled")
                .and_then(|v| v.as_bool())
                .unwrap_or(false)
        })
        .map(|(_, command)| command)
        .collect();
        if on.is_empty() {
            return None;
        }
        Some(format!(
            "선물 전략({})이 켜져 있어 옵션 신규 진입을 하지 않습니다. 옵션은 선물 전략이 모두 꺼져 있을 때만 작동합니다.",
            on.join(", ")
        ))
    }
    async fn heartbeat_loop(&self) {
        loop {
            self.write_heartbeat().await;
            tokio::time::sleep(std::time::Duration::from_secs(30)).await;
        }
    }

    async fn write_heartbeat(&self) {
        let payload = async {
            let btc_cfg = self.cfg.section("btc_ma_cross");
            let enabled = btc_cfg
                .get("enabled")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            let mode = self.btc_ma.mode(Some(&btc_cfg));
            let last_action = self.last_action(&self.btc_ma).await.unwrap_or(Value::Null);
            let ma_cfg = self.cfg.section("ma_cross");
            let ma_enabled = ma_cfg
                .get("enabled")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            let ma_mode = self.ma_plus.mode(Some(&ma_cfg));
            let ma_last = self.last_action(&self.ma_plus).await.unwrap_or(Value::Null);
            let mut strategies = json!({
                "btc_ma": {"enabled": enabled, "mode": mode, "last_action": last_action},
                "ma_plus": {"enabled": ma_enabled, "mode": ma_mode, "last_action": ma_last},
            });
            if let Some(tf) = self.tradfi.as_ref() {
                let tf_cfg = self.cfg.section(TRADFI_SECTION);
                let tf_enabled = tf_cfg
                    .get("enabled")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);
                let tf_mode = tf.mode(Some(&tf_cfg));
                let tf_last = self.last_action(tf).await.unwrap_or(Value::Null);
                strategies["tradfi"] =
                    json!({"enabled": tf_enabled, "mode": tf_mode, "last_action": tf_last});
            }
            json!({
                "ts": now_secs(),
                "pid": std::process::id(),
                "strategies": strategies,
            })
        }
        .await;
        let path = self.runtime_dir.join(HEARTBEAT_FILE);
        let tmp = path.with_extension("json.tmp");
        let serialized = dump_indent4(&payload);
        if let Err(e) = std::fs::write(&tmp, serialized) {
            log_line(&format!("engine: heartbeat write failed: {e}"));
            return;
        }
        if let Err(e) = std::fs::rename(&tmp, &path) {
            log_line(&format!("engine: heartbeat rename failed: {e}"));
        }
    }

    async fn last_action(&self, service: &Arc<SmaCrossService<E>>) -> Result<Value, String> {
        let network = service
            .current_network(None)
            .ok_or_else(|| "ValueError: unsupported exchange mode".to_string())?;
        let cfg = service.config();
        let mode = service.mode(Some(&cfg));
        let state = service
            .load_state(&network, &mode)
            .map_err(|e| e.to_string())?;
        Ok(state.get("last_action").cloned().unwrap_or(Value::Null))
    }

    pub async fn handle(&self, req: Request) -> Result<Value, String> {
        match req.cmd.as_str() {
            "ping" => Ok(json!({
                "pid": std::process::id(),
                "started_at": self.started_at,
                "version": VERSION,
            })),
            // Python controllers read service.config(): the normalized section.
            "config_get" if req.strategy.is_some() && req.arg_str("section").is_none() => {
                let service = self.service_for(&req)?;
                Ok(service.config())
            }
            "config_get" => {
                let section = req
                    .arg_str("section")
                    .or_else(|| req.arg_str("key"))
                    .or_else(|| req.strategy.as_ref().map(|s| section_for(s).to_string()))
                    .unwrap_or_else(|| "btc_ma_cross".to_string());
                Ok(self.cfg.section(&section))
            }
            "config_set" => {
                let section = req
                    .arg_str("section")
                    .or_else(|| req.strategy.as_ref().map(|s| section_for(s).to_string()))
                    .unwrap_or_else(|| "btc_ma_cross".to_string());
                let changes = req.arg_map("changes");
                self.cfg.update_values(&section, changes).await
            }
            "status" => {
                let service = self.service_for(&req)?;
                service.status().await.map_err(|e| e.to_string())
            }
            "load_state" => {
                let service = self.service_for(&req)?;
                let network = req
                    .arg_str("network")
                    .or_else(|| service.current_network(None))
                    .ok_or_else(|| "ValueError: missing network".to_string())?;
                let mode = req.arg_str("mode").unwrap_or_else(|| {
                    let cfg = service.config();
                    service.mode(Some(&cfg))
                });
                let state = service
                    .load_state(&network, &mode)
                    .map_err(|e| e.to_string())?;
                Ok(Value::Object(state))
            }
            "save_state" => {
                let service = self.service_for(&req)?;
                let network = req
                    .arg_str("network")
                    .ok_or_else(|| "ValueError: missing network".to_string())?;
                let mode = req
                    .arg_str("mode")
                    .ok_or_else(|| "ValueError: missing mode".to_string())?;
                let mut state = req.arg_map("state");
                service
                    .save_state(&network, &mode, &mut state)
                    .map_err(|e| e.to_string())?;
                Ok(Value::Object(state))
            }
            "exchange_mode" => {
                let api = self.cfg.section("api");
                let mode = crate::app::config_file::exchange_mode(&self.cfg.load());
                let use_testnet = api
                    .get("use_testnet")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(mode == BINANCE_TESTNET);
                Ok(json!({
                    "mode": mode,
                    "use_testnet": use_testnet,
                    "network": crate::app::config_file::main_network(&self.cfg.load()),
                    "cooldown_seconds": EXCHANGE_SWITCH_COOLDOWN_SECS as i64,
                }))
            }
            "set_exchange_mode" => {
                let target = req
                    .arg_str("mode")
                    .ok_or_else(|| "ValueError: missing mode".to_string())?
                    .trim()
                    .to_lowercase();
                let force = req.arg("force").and_then(|v| v.as_bool()).unwrap_or(false);
                let dry_run = req
                    .arg("dry_run")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);
                if dry_run {
                    self.exchange_switch_preview(&target).await
                } else {
                    self.set_exchange_mode(&target, force).await
                }
            }
            "positions_overview" => self.positions_overview(&req).await,
            "current_network" => {
                let service = self.service_for(&req)?;
                Ok(service
                    .current_network(None)
                    .map(Value::String)
                    .unwrap_or(Value::Null))
            }
            "mode" => {
                let service = self.service_for(&req)?;
                Ok(Value::String(service.mode(None)))
            }
            "needs_cycle" => {
                let service = self.service_for(&req)?;
                Ok(Value::Bool(service.needs_cycle()))
            }
            "run_cycle" => {
                let service = self.service_for(&req)?;
                service.run_cycle().await.map_err(|e| e.to_string())
            }
            "close_position" => {
                let service = self.service_for(&req)?;
                let reason = req
                    .arg_str("reason")
                    .unwrap_or_else(|| "manual".to_string());
                service
                    .close_position(&reason)
                    .await
                    .map_err(|e| e.to_string())
            }
            "reset_reconciliation" => {
                let service = self.service_for(&req)?;
                service.reset_reconciliation().map_err(|e| e.to_string())?;
                Ok(Value::Bool(true))
            }
            "resolve_coin" => {
                let service = self.service_for(&req)?;
                let network = req
                    .arg_str("network")
                    .or_else(|| service.current_network(None))
                    .ok_or_else(|| "ValueError: missing network".to_string())?;
                let text = req.arg("text").cloned().unwrap_or(Value::Null);
                let (symbol, market) = service
                    .resolve_single_coin(&network, &text)
                    .await
                    .map_err(|e| e.to_string())?;
                Ok(json!({"ccxt_symbol": symbol, "market_id": market}))
            }
            "resolve_tradfi_any" => {
                let service = self.service_for(&req)?;
                let network = req
                    .arg_str("network")
                    .or_else(|| service.current_network(None))
                    .ok_or_else(|| "ValueError: missing network".to_string())?;
                let text = req.arg("text").cloned().unwrap_or(Value::Null);
                let (symbol, market) = service
                    .resolve_tradfi_strategy_coin(&network, &text)
                    .await
                    .map_err(|e| e.to_string())?;
                Ok(json!({"ccxt_symbol": symbol, "market_id": market}))
            }
            "tradfi_symbols" => {
                let service = self.service_for(&req)?;
                let network = req
                    .arg_str("network")
                    .or_else(|| service.current_network(None))
                    .ok_or_else(|| "ValueError: missing network".to_string())?;
                let list = service
                    .tradfi_any_symbols(&network)
                    .await
                    .map_err(|e| e.to_string())?;
                Ok(Value::Array(list.into_iter().map(Value::String).collect()))
            }
            "resolve_tradfi_coin" => {
                let service = self.service_for(&req)?;
                let network = req
                    .arg_str("network")
                    .or_else(|| service.current_network(None))
                    .ok_or_else(|| "ValueError: missing network".to_string())?;
                let text = req.arg("text").cloned().unwrap_or(Value::Null);
                let (symbol, market) = service
                    .resolve_tradfi_coin(&network, &text)
                    .await
                    .map_err(|e| e.to_string())?;
                Ok(json!({"ccxt_symbol": symbol, "market_id": market}))
            }
            "volume_universe" => {
                let service = self.service_for(&req)?;
                let network = req
                    .arg_str("network")
                    .or_else(|| service.current_network(None))
                    .ok_or_else(|| "ValueError: missing network".to_string())?;
                let mut cfg = service.config();
                if let Some(map) = cfg.as_object_mut() {
                    map.insert("scan_scope".to_string(), Value::String("alts".to_string()));
                }
                let items = service
                    .scan_universe(&network, &cfg)
                    .await
                    .map_err(|e| e.to_string())?;
                let list: Vec<Value> = items
                    .into_iter()
                    .map(|(symbol, market, vol)| {
                        json!({
                            "ccxt_symbol": symbol,
                            "market_id": market,
                            "quote_volume": vol,
                        })
                    })
                    .collect();
                Ok(Value::Array(list))
            }
            "reentry_wait_lines" => {
                let service = self.service_for(&req)?;
                let cfg = service.config();
                let network = service
                    .current_network(Some(&cfg))
                    .ok_or_else(|| "ValueError: unsupported exchange mode".to_string())?;
                let mode = service.mode(Some(&cfg));
                let state = service
                    .load_state(&network, &mode)
                    .map_err(|e| e.to_string())?;
                let lines = service.reentry_wait_lines(&cfg, &state);
                Ok(Value::Array(lines.into_iter().map(Value::String).collect()))
            }
            "adopt_position" => {
                let service = self.service_for(&req)?;
                let apply = req.arg("apply").and_then(|v| v.as_bool()).unwrap_or(false);
                service
                    .adopt_position(apply)
                    .await
                    .map_err(|e| e.to_string())
            }
            "clear_reentry" => {
                let service = self.service_for(&req)?;
                let released = service
                    .clear_reentry_blocks()
                    .await
                    .map_err(|e| e.to_string())?;
                Ok(Value::Array(
                    released.into_iter().map(Value::String).collect(),
                ))
            }
            "tradfi_network_allowed" => {
                let service = self.service_for(&req)?;
                let network = req
                    .arg_str("network")
                    .ok_or_else(|| "ValueError: missing network".to_string())?;
                Ok(Value::Bool(
                    service.tradfi_network_allowed("tradfi", &network),
                ))
            }
            "tradifi_scope_state" => {
                let service = self.service_for(&req)?;
                let network = req
                    .arg_str("network")
                    .or_else(|| service.current_network(None))
                    .ok_or_else(|| "ValueError: missing network".to_string())?;
                Ok(service.tradifi_scope_state(&network))
            }
            "us_session_status" => {
                let service = self.service_for(&req)?;
                Ok(service.us_session_status())
            }
            "ledger_trades" => {
                let service = self.service_for(&req)?;
                let network = req
                    .arg_str("network")
                    .or_else(|| service.current_network(None))
                    .ok_or_else(|| "ValueError: missing network".to_string())?;
                let mode = req.arg_str("mode").unwrap_or_else(|| {
                    let cfg = service.config();
                    service.mode(Some(&cfg))
                });
                let status = req.arg_str("status");
                let ledger = service.ledger(&network).map_err(|e| e.to_string())?;
                let guard = ledger
                    .lock()
                    .map_err(|_| "RuntimeError: ledger lock".to_string())?;
                let trades = guard
                    .trades(Some(&mode), Some(&network), None, None, status.as_deref())
                    .map_err(|e| e.to_string())?;
                Ok(Value::Array(
                    trades.into_iter().map(Value::Object).collect(),
                ))
            }
            "recent_events" => {
                let service = self.service_for(&req)?;
                let network = req
                    .arg_str("network")
                    .or_else(|| service.current_network(None))
                    .ok_or_else(|| "ValueError: missing network".to_string())?;
                let limit = req.arg("limit").and_then(|v| v.as_u64()).unwrap_or(50) as usize;
                let kinds = req.arg("kinds").and_then(|v| v.as_array()).map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(|s| s.to_string()))
                        .collect::<std::collections::HashSet<_>>()
                });
                let ledger = service.ledger(&network).map_err(|e| e.to_string())?;
                let guard = ledger
                    .lock()
                    .map_err(|_| "RuntimeError: ledger lock".to_string())?;
                let events = guard.recent_events(limit, kinds.as_ref());
                Ok(Value::Array(events))
            }
            "emergency_stop" => self.emergency_stop().await,
            "options_status" => {
                let service = self.options_service()?;
                let refresh = req.arg("refresh").and_then(|v| v.as_bool()).unwrap_or(true);
                Ok(service.status_snapshot(refresh).await)
            }
            "options_config" => Ok(self.options_service()?.config()),
            "options_state" => Ok(Value::Object(self.options_service()?.state())),
            "options_set" => {
                self.options_service()?;
                let mut changes = req.arg_map("changes");
                if changes.contains_key("enabled") {
                    // An explicit ON/OFF from the user settles the first-start gate.
                    changes.insert(OPTIONS_PORT_ACK.to_string(), Value::Bool(true));
                }
                self.cfg.update_values(OPTIONS_SECTION, changes).await
            }
            "options_preflight" => Ok(self.options_service()?.preflight().await),
            "options_futures_block" => {
                self.options_service()?;
                Ok(Self::futures_block_reason(&self.cfg)
                    .map(Value::String)
                    .unwrap_or(Value::Null))
            }
            "options_run_cycle" => {
                let service = self.options_service()?;
                let force_scan = req
                    .arg("force_scan")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);
                let force_exit = req
                    .arg("force_exit")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);
                Ok(service.run_cycle(force_scan, force_exit).await)
            }
            "turn_off_others" => {
                let except = req.arg_str("except").unwrap_or_default();
                self.turn_off_others(&except).await
            }
            "reset_counters" => {
                let service = self.service_for(&req)?;
                let network = req
                    .arg_str("network")
                    .or_else(|| service.current_network(None))
                    .ok_or_else(|| "ValueError: missing network".to_string())?;
                let mut state = service
                    .load_state(&network, &service.mode(None))
                    .map_err(|e| e.to_string())?;
                state.insert("profit_lock_exit_count".into(), json!(0));
                state.insert("entry_mode_forced_cross".into(), json!(false));
                state.insert("pending_entry_mode_switch".into(), Value::Null);
                let mode = service.mode(None);
                service
                    .save_state(&network, &mode, &mut state)
                    .map_err(|e| e.to_string())?;
                Ok(Value::Object(state))
            }
            "pick_universe" => {
                let service = self.service_for(&req)?;
                let network = req
                    .arg_str("network")
                    .or_else(|| service.current_network(None))
                    .ok_or_else(|| "ValueError: missing network".to_string())?;
                let mut cfg = service.config();
                if let Some(map) = cfg.as_object_mut() {
                    map.insert("scan_scope".to_string(), Value::String("alts".to_string()));
                }
                let mut coins: Vec<String> = vec!["BTCUSDT".to_string()];
                match service.scan_universe(&network, &cfg).await {
                    Ok(items) => {
                        for (_symbol, market_id, _vol) in items {
                            if !coins.iter().any(|c| c == &market_id) {
                                coins.push(market_id);
                            }
                            if coins.len() >= PICK_LIST_SIZE {
                                break;
                            }
                        }
                    }
                    Err(e) => {
                        log_line(&format!("3/200 SMA coin list unavailable: {e}"));
                    }
                }
                Ok(Value::Array(coins.into_iter().map(Value::String).collect()))
            }
            other => Err(format!("ValueError: unknown cmd {other}")),
        }
    }

    fn service_for(&self, req: &Request) -> Result<Arc<SmaCrossService<E>>, String> {
        let variant = req
            .strategy
            .as_deref()
            .or_else(|| {
                req.arg_str("strategy").as_deref().map(|s| {
                    // Borrow escape hatch: we clone into a temp string first.
                    Box::leak(s.to_string().into_boxed_str()) as &str
                })
            })
            .unwrap_or("btc_ma");
        match variant {
            "btc_ma" => Ok(Arc::clone(&self.btc_ma)),
            "ma_plus" => Ok(Arc::clone(&self.ma_plus)),
            "tradfi" => self
                .tradfi
                .clone()
                .ok_or_else(|| "ValueError: tradfi strategy is not configured".to_string()),
            other => Err(format!("ValueError: unknown strategy {other}")),
        }
    }

    /// Another futures strategy took over: switch the others OFF.
    async fn turn_off_others(&self, except_key: &str) -> Result<Value, String> {
        let strategies = [
            ("btc_ma_cross", "/btcmacross", ""),
            ("ma_cross", "/macross", " C"),
            (TRADFI_SECTION, "/tradfi", ""),
        ];
        for (key, command, suffix) in strategies {
            if key == except_key {
                continue;
            }
            let label = self.sma_label(key, suffix);
            let enabled = self
                .cfg
                .section(key)
                .get("enabled")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            if !enabled {
                continue;
            }
            let mut changes = Map::new();
            changes.insert("enabled".to_string(), Value::Bool(false));
            if let Err(e) = self.cfg.update_values(key, changes).await {
                log_line(&format!("engine: could not switch {key} off: {e}"));
                continue;
            }
            let text = format!(
                "⏹ 다른 선물 전략이 켜져서 {label} 전략 신규 진입을 OFF 했습니다.\n\
                 이 전략이 보유한 포지션은 계속 관리합니다. 다시 켜려면 {command}"
            );
            self.notify(text).await;
        }
        Ok(Value::Null)
    }

    async fn notify(&self, text: String) {
        let Some(api) = self.telegram.clone() else {
            return;
        };
        let (_, chat) = telegram_settings(&self.cfg.load());
        let Some(chat) = chat else {
            return;
        };
        if chat.trim().is_empty() {
            return;
        }
        match api.send_message(chat.trim(), &text, None).await {
            Ok(_) => {}
            Err(e) => log_line(&format!("engine: notify failed: {e}")),
        }
    }

    /// "1/200 SMA" (+ suffix) from a strategy section's configured periods.
    fn sma_label(&self, section: &str, suffix: &str) -> String {
        if section == TRADFI_SECTION {
            return TRADFI_LABEL.to_string();
        }
        let cfg = self.cfg.section(section);
        let default_slow = if section == "ma_cross" { 120 } else { 200 };
        let period = |key: &str, default: i64| {
            cfg.get(key)
                .and_then(|v| {
                    v.as_i64()
                        .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
                })
                .unwrap_or(default)
        };
        format!(
            "{}/{} SMA{suffix}",
            period("fast_period", 3),
            period("slow_period", default_slow)
        )
    }

    /// Every open position on both Binance Futures networks for the two SMA
    /// strategies, in live mode (the mode that sends real orders).
    async fn open_positions(&self) -> Result<Vec<Value>, String> {
        let mut items = Vec::new();
        for (suffix, service, section) in self.sma_services() {
            let service = &service;
            let label = self.sma_label(section, suffix);
            for network in ["testnet", "mainnet"] {
                let rows = service
                    .fetch_account_positions(network)
                    .await
                    .map_err(|e| format!("{label} {network}: {e}"))?;
                for row in rows {
                    let contracts = row.get("contracts").and_then(|v| v.as_f64()).unwrap_or(0.0);
                    if contracts.abs() <= 0.0 {
                        continue;
                    }
                    let symbol = row
                        .get("symbol")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string();
                    let market = row.get("info").cloned();
                    items.push(json!({
                        "strategy": label,
                        "network": network,
                        "symbol": symbol,
                        "side": row.get("side").cloned().unwrap_or(Value::Null),
                        "contracts": contracts,
                        "tradfi": market_in_tradfi_scope(&symbol, market.as_ref()),
                    }));
                }
            }
        }
        Ok(items)
    }

    /// What a switch would do to the kept coins: the per-strategy single coin
    /// and the explicit watchlist, checked against the mode's rules.
    async fn exchange_switch_plan(&self, mode: &str) -> Value {
        let mut changes = Vec::new();
        for (suffix, section) in [("", "btc_ma_cross"), (" C", "ma_cross")] {
            let label = self.sma_label(section, suffix);
            let cfg = self.cfg.section(section);
            let single = cfg
                .get("single_symbol")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim()
                .to_uppercase();
            if !single.is_empty() && market_in_tradfi_scope(&single, None) && mode != "mainnet" {
                changes.push(json!({
                    "strategy": label,
                    "field": "단일 코인",
                    "from": single,
                    "to": "BTCUSDT",
                }));
            }
            let watchlist: Vec<String> = cfg
                .get("watchlist")
                .and_then(|v| v.as_array())
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str())
                        .filter(|s| !s.trim().is_empty())
                        .map(|s| s.to_string())
                        .collect()
                })
                .unwrap_or_default();
            if watchlist.is_empty() {
                continue;
            }
            let kept: Vec<&String> = watchlist
                .iter()
                .filter(|s| mode == "mainnet" || !market_in_tradfi_scope(s, None))
                .collect();
            if kept.len() != watchlist.len() {
                let to = if kept.is_empty() {
                    "BTCUSDT".to_string()
                } else {
                    kept.iter()
                        .map(|s| s.as_str())
                        .collect::<Vec<_>>()
                        .join(",")
                };
                changes.push(json!({
                    "strategy": label,
                    "field": "감시목록",
                    "from": watchlist.join(","),
                    "to": to,
                }));
            }
        }
        Value::Array(changes)
    }

    /// `set_exchange_mode` mode: what changing the exchange would do right now.
    async fn exchange_switch_preview(&self, mode: &str) -> Result<Value, String> {
        let current = crate::app::config_file::exchange_mode(&self.cfg.load());
        if mode == UPBIT_MODE {
            return Ok(json!({
                "ok": false,
                "blocked": "upbit_unsupported",
                "current": current,
                "target": mode,
                "message": "업비트는 현물 전용이라 선물 전략(SMA 크로스)을 돌릴 수 없습니다.\
                    바이낸스 테스트넷(데모) 또는 메인넷을 선택하세요.",
            }));
        }
        if mode != BINANCE_TESTNET && mode != BINANCE_MAINNET {
            return Err(format!("지원하지 않는 거래모드: {mode}"));
        }
        let after_cooldown = {
            let last = *(self.exchange_switch.lock().await);
            match last {
                Some(ts) => {
                    let remain = EXCHANGE_SWITCH_COOLDOWN_SECS - (now_secs_f64() - ts);
                    if remain > 0.0 {
                        format!("⚠️ 거래소 전환은 너무 자주 실행할 수 없습니다. {}초 후 다시 시도하세요.", remain.ceil() as i64)
                    } else {
                        String::new()
                    }
                }
                None => String::new(),
            }
        };
        let positions = self.open_positions().await?;
        let tradfi = positions
            .iter()
            .filter(|p| {
                p.get("tradfi").and_then(|v| v.as_bool()) == Some(true)
                    && p.get("network").and_then(|v| v.as_str()) != Some("mainnet")
            })
            .collect::<Vec<_>>();
        let mut plans = Vec::new();
        if mode != BINANCE_TESTNET {
            plans.push(self.exchange_switch_plan("mainnet").await);
        }
        plans.push(self.exchange_switch_plan("testnet").await);
        Ok(json!({
            "ok": true,
            "current": current,
            "target": mode,
            "same": current == mode,
            "cooldown_text": after_cooldown,
            "positions": positions,
            "tradfi_positions": tradfi,
            "plan": plans,
        }))
    }

    /// Python `reinit_exchange`: exclusive, 30s cooldown, refuse while a
    /// position is open, verify the new network resolves, roll back on failure.
    async fn set_exchange_mode(&self, mode: &str, force: bool) -> Result<Value, String> {
        let Ok(mut guard) = self.exchange_switch.try_lock() else {
            return Err(
                "이미 거래소/네트워크 전환 중입니다. 완료될 때까지 기다려주세요.".to_string(),
            );
        };
        if mode != BINANCE_TESTNET && mode != BINANCE_MAINNET && mode != UPBIT_MODE {
            return Err(format!("지원하지 않는 거래모드: {mode}"));
        }
        {
            let last = *guard;
            if let Some(ts) = last {
                let elapsed = now_secs_f64() - ts;
                if elapsed < EXCHANGE_SWITCH_COOLDOWN_SECS {
                    let remain = (EXCHANGE_SWITCH_COOLDOWN_SECS - elapsed).ceil().max(1.0) as i64;
                    return Err(format!(
                        "거래소 전환은 너무 자주 실행할 수 없습니다. {remain}초 후 다시 시도하세요."
                    ));
                }
            }
        }
        let current = crate::app::config_file::exchange_mode(&self.cfg.load());
        // A position only exists on the exchange the strategy trades now.
        // Switching under it would orphan its protective SL/TP orders.
        if !force {
            let held = self.open_positions().await?;
            if !held.is_empty() {
                let mut lines = vec!["보유 포지션이 있어 거래소를 바꾸지 않았습니다.\n\
                     · 거래소를 바꾸면 보유 포지션과 SL/TP 주문의 관리가 끊깁니다."
                    .to_string()];
                for p in held {
                    lines.push(format!(
                        "· {} {} {} {}개 ({})",
                        p.get("strategy").and_then(|v| v.as_str()).unwrap_or(""),
                        p.get("symbol").and_then(|v| v.as_str()).unwrap_or(""),
                        p.get("side").and_then(|v| v.as_str()).unwrap_or(""),
                        p.get("contracts").and_then(|v| v.as_f64()).unwrap_or(0.0),
                        p.get("network").and_then(|v| v.as_str()).unwrap_or(""),
                    ));
                }
                lines.push("· 먼저 포지션을 청산한 뒤 전환하세요.".to_string());
                return Err(lines.join("\n"));
            }
        }
        if mode == current {
            return Ok(json!({
                "ok": true, "same": true, "mode": mode, "network": crate::app::config_file::main_network(&self.cfg.load()),
            }));
        }
        let target = if mode == BINANCE_TESTNET {
            "testnet"
        } else {
            "mainnet"
        };
        let plan = self.exchange_switch_plan(target).await;
        let apply_plan = |cfg: &mut Map<String, Value>, plan: &Value| {
            for change in plan.as_array().into_iter().flatten() {
                let field = change.get("field").and_then(|v| v.as_str()).unwrap_or("");
                let key = if field == "단일 코인" {
                    "single_symbol"
                } else {
                    "watchlist"
                };
                let to = change.get("to").and_then(|v| v.as_str()).unwrap_or("");
                let value = if key == "watchlist" {
                    Value::Array(
                        to.split(',')
                            .map(|s| Value::String(s.trim().to_string()))
                            .collect(),
                    )
                } else {
                    Value::String(to.to_string())
                };
                cfg.insert(key.to_string(), value);
            }
        };
        let old = self.cfg.load();
        let mut rollback: Vec<(String, Map<String, Value>)> = vec![(
            "api".to_string(),
            old.get("api")
                .and_then(|v| v.as_object())
                .cloned()
                .unwrap_or_default(),
        )];
        for section in ["btc_ma_cross", "ma_cross"] {
            rollback.push((
                section.to_string(),
                old.get(section)
                    .and_then(|v| v.as_object())
                    .cloned()
                    .unwrap_or_default(),
            ));
        }
        let restore = |text: String| -> BoxFut<Result<Value, String>> {
            let cfg = Arc::clone(&self.cfg);
            Box::pin(async move {
                for (section, values) in rollback {
                    match cfg.update_values(&section, values).await {
                        Ok(_) => {}
                        Err(e) => {
                            log_line(&format!("engine: exchange rollback {section} failed: {e}"))
                        }
                    }
                }
                Err(format!("{text} (이전 거래소 설정으로 롤백했습니다)"))
            })
        };
        for section in ["btc_ma_cross", "ma_cross"] {
            let mut values = self
                .cfg
                .section(section)
                .as_object()
                .cloned()
                .unwrap_or_default();
            apply_plan(&mut values, &plan);
            let mut changes = values
                .into_iter()
                .filter(|(k, _)| matches!(k.as_str(), "single_symbol" | "watchlist"))
                .collect::<Map<String, Value>>();
            if changes.is_empty() {
                continue;
            }
            if let Err(e) = self
                .cfg
                .update_values(section, std::mem::take(&mut changes))
                .await
            {
                return restore(e).await;
            }
        }
        let mut errors = Vec::new();
        for (_suffix, service, _section) in self.sma_services() {
            if let Err(e) = service.set_exchange_mode(mode).await {
                errors.push(e);
            }
        }
        if !errors.is_empty() {
            return restore(errors.join(" / ")).await;
        }
        *guard = Some(now_secs_f64());
        Ok(json!({
            "ok": true,
            "mode": mode,
            "network": target,
            "previous": current,
            "plan": plan,
        }))
    }

    /// Open positions per network, or a read error (for the switch screen).
    async fn positions_overview(&self, _req: &Request) -> Result<Value, String> {
        match self.open_positions().await {
            Ok(items) => Ok(json!({"ok": true, "positions": items})),
            Err(e) => Ok(json!({"ok": false, "error": e})),
        }
    }

    async fn emergency_stop(&self) -> Result<Value, String> {
        let mut lines: Vec<String> = Vec::new();
        for (suffix, service, section) in self.sma_services() {
            let label = self.sma_label(section, suffix);
            let was_on = self
                .cfg
                .section(section)
                .get("enabled")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            let mut changes = Map::new();
            changes.insert("enabled".to_string(), Value::Bool(false));
            if let Err(e) = self.cfg.update_values(section, changes).await {
                lines.push(format!("⚠️ {label} 정지 처리 오류: {e}"));
                continue;
            }
            match service.close_position("emergency_stop").await {
                Ok(result) => {
                    let action = result
                        .get("action")
                        .and_then(|v| v.as_str())
                        .unwrap_or("none");
                    if action == "none" {
                        lines.push(format!(
                            "{label}: OFF{}",
                            if was_on {
                                ""
                            } else {
                                " (이미 꺼져 있었음)"
                            }
                        ));
                    } else {
                        let reason = result.get("reason").and_then(|v| v.as_str()).unwrap_or("");
                        lines.push(
                            format!("{label}: OFF · 포지션 청산 {action} {reason}")
                                .trim()
                                .to_string(),
                        );
                    }
                }
                Err(e) => {
                    log_line(&format!("engine: {label} emergency stop failed: {e}"));
                    lines.push(format!("⚠️ {label} 정지 처리 오류: {e}"));
                }
            }
        }
        lines.extend(self.stop_options().await);
        Ok(json!({"lines": lines}))
    }

    /// Every SMA-family futures strategy: (label suffix, service, config section).
    fn sma_services(&self) -> Vec<(&'static str, Arc<SmaCrossService<E>>, &'static str)> {
        let mut out = vec![
            ("", Arc::clone(&self.btc_ma), "btc_ma_cross"),
            (" C", Arc::clone(&self.ma_plus), "ma_cross"),
        ];
        if let Some(tf) = self.tradfi.as_ref() {
            out.push(("", Arc::clone(tf), TRADFI_SECTION));
        }
        out
    }

    fn options_service(&self) -> Result<&Arc<OptionsService<BinanceOptionsClient>>, String> {
        self.options
            .as_ref()
            .ok_or_else(|| "ValueError: options sleeve is not configured".to_string())
    }

    /// Python `_stop_all_auxiliary_trading` (options part): entries OFF, close bot options.
    async fn stop_options(&self) -> Vec<String> {
        let Some(service) = self.options.as_ref() else {
            return Vec::new();
        };
        let was_on = self
            .cfg
            .section(OPTIONS_SECTION)
            .get("enabled")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let mut changes = Map::new();
        changes.insert("enabled".to_string(), Value::Bool(false));
        if let Err(e) = self.cfg.update_values(OPTIONS_SECTION, changes).await {
            return vec![format!("⚠️ 옵션 정지 처리 오류: {e}")];
        }
        let state = service.state();
        let symbol = state
            .get("active_position")
            .and_then(|p| p.get("symbol"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        if symbol.is_empty() {
            return vec![format!(
                "옵션: 신규진입 OFF{}",
                if was_on {
                    ""
                } else {
                    " (이미 꺼져 있었음)"
                }
            )];
        }
        let result = service.run_cycle(false, true).await;
        let reason = result
            .get("reason")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .or_else(|| result.get("action").and_then(|v| v.as_str()))
            .unwrap_or("");
        vec![format!(
            "옵션: 신규진입 OFF · 봇 옵션 {symbol} 청산 요청 — {reason}"
        )]
    }
}

impl<E: FuturesExchange + 'static> EngineApi for Arc<Engine<E>> {
    fn call(
        &self,
        cmd: &str,
        strategy: Option<&str>,
        args: Value,
    ) -> BoxFut<Result<Value, String>> {
        let engine = Arc::clone(self);
        let req = Request {
            cmd: cmd.to_string(),
            strategy: strategy.map(|s| s.to_string()),
            args,
        };
        Box::pin(async move { engine.handle(req).await })
    }
}

fn section_for(strategy: &str) -> &'static str {
    match strategy {
        "ma_plus" => "ma_cross",
        "tradfi" => TRADFI_SECTION,
        _ => "btc_ma_cross",
    }
}

fn build_service(
    cfg: &Arc<ConfigFile>,
    runtime_dir: PathBuf,
    variant: Variant,
    notifier: Arc<dyn Fn(String) -> BoxFut<()> + Send + Sync>,
) -> Result<Arc<SmaCrossService<BinanceFutures>>, String> {
    let section = match variant {
        Variant::BtcMa => "btc_ma_cross",
        Variant::MaPlus => "ma_cross",
        Variant::TradFi => TRADFI_SECTION,
    };

    let cfg_get = {
        let cfg = Arc::clone(cfg);
        let sec = section.to_string();
        Arc::new(move || cfg.section(&sec)) as Arc<dyn Fn() -> Value + Send + Sync>
    };
    let creds_get = {
        let cfg = Arc::clone(cfg);
        Arc::new(move |network: &str| credentials(&cfg.load(), network))
            as Arc<dyn Fn(&str) -> Value + Send + Sync>
    };
    let exchange_factory: Arc<
        dyn Fn(&str, &Value) -> Result<Arc<BinanceFutures>, crate::exchange::ExchangeError>
            + Send
            + Sync,
    > = Arc::new(|network: &str, creds: &Value| {
        let map = creds.as_object();
        let api_key = map
            .and_then(|m| m.get("api_key"))
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
            .unwrap_or_default();
        let secret_key = map
            .and_then(|m| m.get("secret_key"))
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
            .unwrap_or_default();
        let credentials = if api_key.trim().is_empty() && secret_key.trim().is_empty() {
            None
        } else {
            Some(Credentials {
                api_key,
                secret: secret_key,
            })
        };
        let net = if network == "mainnet" {
            Network::Mainnet
        } else {
            Network::Testnet
        };
        Ok(Arc::new(BinanceFutures::new(BinanceClient::new(
            net,
            credentials,
        ))))
    });
    let main_network_getter: Option<Arc<dyn Fn() -> Option<String> + Send + Sync>> = {
        let cfg = Arc::clone(cfg);
        Some(Arc::new(move || main_network(&cfg.load())))
    };
    let config_writer: Option<Arc<dyn Fn(String) -> BoxFut<bool> + Send + Sync>> = {
        let cfg = Arc::clone(cfg);
        let sec = section.to_string();
        Some(Arc::new(move |mode: String| -> BoxFut<bool> {
            let cfg = Arc::clone(&cfg);
            let sec = sec.clone();
            Box::pin(async move {
                if mode == "disable" {
                    // Strategy turns itself OFF (minimum balance guard).
                    let mut changes = Map::new();
                    changes.insert("enabled".to_string(), Value::Bool(false));
                    return match cfg.update_values(&sec, changes).await {
                        Ok(saved) => saved.get("enabled").and_then(|v| v.as_bool()) == Some(false),
                        Err(e) => {
                            log_line(&format!("engine: disable failed: {e}"));
                            false
                        }
                    };
                }
                if mode != "cross" && mode != "trend" {
                    return false;
                }
                let mut changes = Map::new();
                changes.insert("entry_mode".to_string(), Value::String(mode.clone()));
                match cfg.update_values(&sec, changes).await {
                    Ok(saved) => {
                        saved.get("entry_mode").and_then(|v| v.as_str()) == Some(mode.as_str())
                    }
                    Err(e) => {
                        log_line(&format!("engine: set_entry_mode failed: {e}"));
                        false
                    }
                }
            })
        }))
    };

    let config_file_writer: Option<
        Arc<dyn Fn(&str, Map<String, Value>) -> BoxFut<Result<Value, String>> + Send + Sync>,
    > = {
        let cfg = Arc::clone(cfg);
        Some(Arc::new(
            move |section: &str, changes: Map<String, Value>| -> BoxFut<Result<Value, String>> {
                let cfg = Arc::clone(&cfg);
                let section = section.to_string();
                Box::pin(async move { cfg.update_values(&section, changes).await })
            },
        ))
    };

    let clock: Arc<dyn Fn() -> f64 + Send + Sync> = Arc::new(now_secs_f64);
    let sleep: Arc<dyn Fn(f64) -> BoxFut<()> + Send + Sync> = Arc::new(|secs: f64| -> BoxFut<()> {
        Box::pin(async move {
            if secs > 0.0 {
                tokio::time::sleep(std::time::Duration::from_secs_f64(secs)).await;
            }
        })
    });

    let deps = ServiceDeps {
        config_getter: cfg_get,
        credentials_getter: creds_get,
        exchange_factory,
        runtime_dir,
        notifier: Some(notifier),
        main_network_getter,
        clock,
        sleep,
        environ: env_map(),
        config_writer,
        config_file_writer,
        store_clock: Arc::new(SystemClock) as Arc<dyn Clock>,
    };

    Ok(Arc::new(SmaCrossService::new(variant, deps)))
}

fn build_options_service(
    cfg: &Arc<ConfigFile>,
    runtime_dir: &std::path::Path,
    notifier: Notifier,
) -> Arc<OptionsService<BinanceOptionsClient>> {
    let section_cfg = Arc::clone(cfg);
    let cfg_getter: ConfigGetter = Arc::new(move || section_cfg.section(OPTIONS_SECTION));
    // Python used the futures mainnet key pair (`api.mainnet`) for EAPI as well.
    let creds_cfg = Arc::clone(cfg);
    let creds: CredentialsGetter = Arc::new(move || {
        let pair = credentials(
            &creds_cfg.load(),
            crate::strategy::config_base::NETWORK_MAINNET,
        );
        let field = |key: &str| {
            pair.get(key)
                .and_then(|v| v.as_str())
                .map(|s| s.trim().to_string())
                .unwrap_or_default()
        };
        let (api_key, secret) = (field("api_key"), field("secret_key"));
        if api_key.is_empty() || secret.is_empty() {
            None
        } else {
            Some(Credentials { api_key, secret })
        }
    });
    let timeout = normalize_options_config(&cfg.section(OPTIONS_SECTION))
        .get("request_timeout_seconds")
        .and_then(|v| v.as_u64())
        .unwrap_or(10);
    let client = Arc::new(BinanceOptionsClient::new(creds, timeout));
    let sleeper: Sleeper = Arc::new(|secs: f64| -> BoxFut<()> {
        Box::pin(async move {
            if secs.is_finite() && secs > 0.0 {
                tokio::time::sleep(std::time::Duration::from_secs_f64(secs)).await;
            }
        })
    });
    let service = Arc::new(OptionsService::new(
        cfg_getter,
        client,
        runtime_dir.join(OPTIONS_STATE_FILE),
        Some(notifier),
        Arc::new(SystemClock),
        sleeper,
    ));
    let guard_cfg = Arc::clone(cfg);
    service.set_entry_guard(Arc::new(move || {
        Engine::<BinanceFutures>::futures_block_reason(&guard_cfg)
    }));
    service
}

fn env_map() -> HashMap<String, String> {
    std::env::vars().collect()
}

fn telegram_settings(cfg: &Value) -> (Option<String>, Option<String>) {
    let tg = cfg.get("telegram").and_then(|v| v.as_object());
    let token = tg
        .and_then(|m| m.get("token"))
        .and_then(|v| v.as_str())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());
    let chat = tg
        .and_then(|m| m.get("chat_id"))
        .and_then(|v| match v {
            Value::String(s) => Some(s.trim().to_string()),
            Value::Number(n) => Some(n.to_string()),
            _ => None,
        })
        .filter(|s| !s.is_empty());
    (token, chat)
}

fn cfg_field_i64(cfg: &Value, key: &str, default: i64) -> i64 {
    cfg.get(key)
        .and_then(|v| match v {
            Value::Number(n) => n.as_i64(),
            Value::String(s) => s.trim().parse::<i64>().ok(),
            _ => None,
        })
        .unwrap_or(default)
}

fn now_secs() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

fn now_secs_f64() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn log_line(msg: &str) {
    use std::io::Write as _;
    let line = format!(
        "{} {msg}\n",
        chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, false)
    );
    let _ = std::io::stderr().write_all(line.as_bytes());
}
