//! `tbot-telegram` process: long-poll Telegram updates and drive the engine
//! process over the local control API. Never prints the bot token.

use std::sync::Arc;

use serde_json::Value;

use tbot::app::config_file::ConfigFile;
use tbot::app::ipc::{ipc_port, runtime_dir, token_path, IpcClient};
use tbot::app::telegram_api::TelegramApi;
use tbot::app::telegram_bot::TelegramBot;
use tbot::core::periods::BTC_MA_PERIOD_COMMANDS;

#[tokio::main]
async fn main() -> Result<(), String> {
    let cfg_path = std::env::var("TBOT_CONFIG").unwrap_or_else(|_| "../config.json".to_string());
    let cfg = ConfigFile::new(std::path::PathBuf::from(cfg_path));
    let loaded = cfg.load();
    let (token, chat) = telegram_settings(&loaded);
    let token = token.ok_or_else(|| "ValueError: telegram.token missing".to_string())?;
    let chat_id = chat
        .and_then(|s| s.trim().parse::<i64>().ok())
        .ok_or_else(|| "ValueError: telegram.chat_id missing".to_string())?;

    let runtime = runtime_dir();
    let token_file = token_path(&runtime);
    let ipc_token = std::fs::read_to_string(&token_file)
        .map_err(|_| format!("OSError: cannot read IPC token at {}", token_file.display()))?
        .trim()
        .to_string();
    if ipc_token.is_empty() {
        return Err(format!(
            "OSError: empty IPC token at {}",
            token_file.display()
        ));
    }
    let client = IpcClient::localhost(ipc_port(), ipc_token);
    let engine = Arc::new(client);

    let api = TelegramApi::new(token);

    let mut commands = vec![
        (
            "btcmacross".to_string(),
            "3/200 SMA 크로스 전략 메뉴".to_string(),
        ),
        ("macoin".to_string(), "거래할 코인 1개 지정".to_string()),
        ("start".to_string(), "도움말".to_string()),
        ("help".to_string(), "도움말".to_string()),
        ("stop".to_string(), "모든 선물 전략 비상 정지".to_string()),
    ];
    for name in BTC_MA_PERIOD_COMMANDS {
        commands.push(((*name).to_string(), "SMA 기간 변경".to_string()));
    }
    if let Err(e) = api.set_my_commands(commands).await {
        log_line(&format!("telegram: set_my_commands failed: {e}"));
    }

    let mut bot = TelegramBot::new(api, engine, chat_id);
    bot.run().await
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

fn log_line(msg: &str) {
    use std::io::Write as _;
    let line = format!(
        "{} {msg}\n",
        chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, false)
    );
    let _ = std::io::stderr().write_all(line.as_bytes());
}
