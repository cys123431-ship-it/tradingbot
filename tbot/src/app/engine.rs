//! `tbot-engine` process: run the two SmaCross strategies and serve the
//! local control API (IPC).
#![allow(clippy::type_complexity)] // service dependency closures

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};

use crate::app::config_file::{credentials, dump_indent4, main_network, ConfigFile};
use crate::app::ipc::{self, BoxFut, Handler, Request};
use crate::app::telegram_api::TelegramApi;
use crate::exchange::binance::{BinanceClient, Credentials, Network};
use crate::exchange::binance_futures::BinanceFutures;
use crate::store::clock::{Clock, SystemClock};
use crate::strategy::service::{ServiceDeps, SmaCrossService, Variant};

const VERSION: &str = env!("CARGO_PKG_VERSION");
const HEARTBEAT_FILE: &str = "tbot_engine_heartbeat.json";

pub struct Engine {
    cfg: Arc<ConfigFile>,
    btc_ma: Arc<SmaCrossService<BinanceFutures>>,
    ma_plus: Arc<SmaCrossService<BinanceFutures>>,
    #[allow(dead_code)] // notices go through the service notifier
    telegram: Option<TelegramApi>,
    started_at: i64,
    runtime_dir: PathBuf,
}

impl Engine {
    pub fn from_env() -> Result<Engine, String> {
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
        let ma_plus = build_service(&cfg, runtime_dir.clone(), Variant::MaPlus, notifier)?;

        let started_at = now_secs();
        Ok(Engine {
            cfg,
            btc_ma,
            ma_plus,
            telegram,
            started_at,
            runtime_dir,
        })
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
        let heartbeat = {
            let engine = Arc::clone(&self);
            tokio::spawn(async move { engine.heartbeat_loop().await })
        };

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
        self.write_heartbeat().await;
        Ok(())
    }

    async fn strategy_loop(&self, service: Arc<SmaCrossService<BinanceFutures>>) {
        tokio::time::sleep(std::time::Duration::from_secs(30)).await;
        loop {
            let mut interval: i64 = 10;
            let result: Result<(), String> = async {
                let cfg = service.config();
                interval = cfg_field_i64(&cfg, "loop_interval_seconds", 10);
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
            json!({
                "ts": now_secs(),
                "pid": std::process::id(),
                "strategies": {
                    "btc_ma": {"enabled": enabled, "mode": mode, "last_action": last_action},
                    "ma_plus": {"enabled": ma_enabled, "mode": ma_mode, "last_action": ma_last},
                }
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

    async fn last_action(
        &self,
        service: &Arc<SmaCrossService<BinanceFutures>>,
    ) -> Result<Value, String> {
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

    async fn handle(&self, req: Request) -> Result<Value, String> {
        match req.cmd.as_str() {
            "ping" => Ok(json!({
                "pid": std::process::id(),
                "started_at": self.started_at,
                "version": VERSION,
            })),
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
                    .resolve_coin(&network, &text)
                    .await
                    .map_err(|e| e.to_string())?;
                Ok(json!({"ccxt_symbol": symbol, "market_id": market}))
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
            other => Err(format!("ValueError: unknown cmd {other}")),
        }
    }

    fn service_for(&self, req: &Request) -> Result<Arc<SmaCrossService<BinanceFutures>>, String> {
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
            other => Err(format!("ValueError: unknown strategy {other}")),
        }
    }

    async fn emergency_stop(&self) -> Result<Value, String> {
        let mut lines: Vec<String> = Vec::new();
        for (label, service, section) in [
            ("3/200 SMA", &self.btc_ma, "btc_ma_cross"),
            ("SMA3/120 C", &self.ma_plus, "ma_cross"),
        ] {
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
        Ok(json!({"lines": lines}))
    }
}

fn section_for(strategy: &str) -> &'static str {
    match strategy {
        "ma_plus" => "ma_cross",
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
        store_clock: Arc::new(SystemClock) as Arc<dyn Clock>,
    };

    Ok(Arc::new(SmaCrossService::new(variant, deps)))
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
