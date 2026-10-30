//! Telegram front end for the two SmaCross strategies. It never holds the
//! services directly: every call and config change goes through the engine
//! IPC (`EngineApi`), so the menu stays responsive while the engine works.
#![allow(clippy::too_many_arguments)]

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use serde_json::{json, Map, Value};

use crate::app::ipc::EngineApi;
use crate::app::telegram_api::TelegramApi;
use crate::core::periods::{
    is_default_periods, parse_period_command, periods_label, required_candle_history,
    BTC_MA_PERIOD_COMMANDS, PERIOD_PRESETS,
};

const CONFIG_KEY: &str = "btc_ma_cross";
const STRATEGY: &str = "btc_ma";
const DEFAULT_PROFIT_LOCK_SWITCH_COUNT: i64 = 3;
const PICK_LIST_SIZE: usize = 12;

pub const TIMEFRAMES: &[&str] = &["3m", "5m", "15m", "30m", "1h", "2h", "4h"];
pub const LEVERAGE_CHOICES: &[i64] = &[2, 3, 4, 5];
pub const EMERGENCY_STOP_CHOICES: &[i64] = &[0, 10, 20, 30, 50];
pub const REENTRY_WAIT_CHOICES: &[i64] = &[0, 1, 2, 3, 5];
pub const SLOW_PERIOD_CHOICES: &[i64] = &[60, 120, 200];
const TRADFI_SCOPE: &str = "tradfi";

pub const NETWORK_LABELS: &[(&str, &str)] =
    &[("testnet", "테스트넷(데모)"), ("mainnet", "메인넷(실계좌)")];

pub fn network_label(network: &str) -> String {
    NETWORK_LABELS
        .iter()
        .find(|(k, _)| *k == network)
        .map(|(_, v)| v.to_string())
        .unwrap_or_else(|| network.to_string())
}

pub fn timeframe_labels(tf: &str) -> &'static str {
    match tf {
        "3m" => "3분",
        "5m" => "5분",
        "15m" => "15분",
        "30m" => "30분",
        "1h" => "1시간",
        "2h" => "2시간",
        "4h" => "4시간",
        _ => "-",
    }
}

pub fn tradfi_ko(reason: &str) -> &'static str {
    match reason {
        "weekend" => "주말 휴장",
        "holiday" => "미국 휴장일",
        "outside_regular_session" => "정규장 시간 아님",
        "regular_session_open" => "정규장 진행 중",
        _ => "",
    }
}

/// Python `_n`: float(value) or default.
pub fn py_n(value: Option<&Value>) -> f64 {
    fn inner(v: &Value) -> Option<f64> {
        match v {
            Value::Null => None,
            Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
            Value::Number(n) => n.as_f64(),
            Value::String(s) => s.trim().parse::<f64>().ok(),
            _ => None,
        }
    }
    match value {
        None => 0.0,
        Some(v) => inner(v).unwrap_or(0.0),
    }
}

pub fn py_int_of(value: Option<&Value>) -> i64 {
    fn inner(v: &Value) -> Option<i64> {
        match v {
            Value::Null => None,
            Value::Bool(b) => Some(if *b { 1 } else { 0 }),
            Value::Number(n) => n.as_i64().or_else(|| n.as_f64().map(|f| f.trunc() as i64)),
            Value::String(s) => s.trim().parse::<i64>().ok(),
            _ => None,
        }
    }
    match value {
        None => 0,
        Some(v) => inner(v).unwrap_or(0),
    }
}

/// Python `_kst`: ISO timestamp -> KST `%m-%d %H:%M`, else "-".
pub fn kst(iso_text: Option<&Value>) -> String {
    let Some(Value::String(text)) = iso_text else {
        return "-".to_string();
    };
    match chrono::DateTime::parse_from_rfc3339(text) {
        Ok(dt) => {
            use chrono::FixedOffset;
            let kst = FixedOffset::east_opt(9 * 3600)
                .unwrap_or_else(|| FixedOffset::east_opt(0).unwrap());
            dt.with_timezone(&kst).format("%m-%d %H:%M").to_string()
        }
        Err(_) => "-".to_string(),
    }
}

// ------------------------------------------------------------ python number format

/// Python `:g` formatting for a float.
pub fn format_g(v: f64) -> String {
    if v.is_nan() {
        return "nan".to_string();
    }
    if v.is_infinite() {
        return if v > 0.0 { "inf" } else { "-inf" }.to_string();
    }
    if v == 0.0 {
        return "0".to_string();
    }
    let abs = v.abs();
    if !(1e-4..1e16).contains(&abs) {
        return format!("{:e}", v);
    }
    let s = format!("{:.6}", v);
    let trimmed = s.trim_end_matches('0').trim_end_matches('.');
    if trimmed.is_empty() || trimmed == "-" {
        "0".to_string()
    } else {
        trimmed.to_string()
    }
}

/// Python `:g` for an integer value.
pub fn format_g_int(v: i64) -> String {
    format_g(v as f64)
}

/// Python `:.{prec}f` string.
pub fn format_f(v: f64, prec: usize) -> String {
    format!("{v:.prec$}")
}

/// Python `:.{prec}g` on a float.
pub fn format_g_prec(v: f64, prec: usize) -> String {
    if v.is_nan() {
        return "nan".to_string();
    }
    if v.is_infinite() {
        return if v > 0.0 { "inf" } else { "-inf" }.to_string();
    }
    if v == 0.0 {
        return "0".to_string();
    }
    let py = format!("{:.*e}", prec.saturating_sub(1), v);
    // py = "d.dddde±x"; python strips trailing zeros in the mantissa.
    let (mantissa, exponent) = match py.split_once('e') {
        Some((m, e)) => (m.to_string(), e.parse::<i32>().unwrap_or(0)),
        None => (py.clone(), 0),
    };
    if !(-4..prec as i32).contains(&exponent) {
        let mant = mantissa.trim_end_matches('0').trim_end_matches('.');
        let sign = if exponent >= 0 { "+" } else { "-" };
        return format!("{}e{}{:02}", mant, sign, exponent.abs());
    }
    let s = format!(
        "{v:.decimals$}",
        decimals = (prec as i32 - 1 - exponent).max(0) as usize
    );
    let trimmed = if s.contains('.') {
        s.trim_end_matches('0').trim_end_matches('.')
    } else {
        &s
    };
    if trimmed.is_empty() || trimmed == "-" {
        "0".to_string()
    } else {
        trimmed.to_string()
    }
}

/// Python `:+.4f`.
pub fn format_plus4(v: f64) -> String {
    if v >= 0.0 {
        format!("+{v:.4}")
    } else {
        format!("{v:.4}")
    }
}

/// Python `value.startswith("prefix")`.
fn starts_with_any(s: &str, prefixes: &[&str]) -> bool {
    prefixes.iter().any(|p| s.starts_with(p))
}

// ------------------------------------------------------------ helpers ported verbatim

fn py_truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().map(|f| f != 0.0).unwrap_or(false),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

fn cfg_get<'a>(cfg: &'a Value, key: &str) -> Option<&'a Value> {
    cfg.as_object().and_then(|m| m.get(key))
}

fn cfg_str<'a>(cfg: &'a Value, key: &str, default: &'a str) -> &'a str {
    cfg_get(cfg, key)
        .and_then(|v| v.as_str())
        .unwrap_or(default)
}

fn cfg_int(cfg: &Value, key: &str, default: i64) -> i64 {
    cfg_get(cfg, key)
        .map(|v| py_int_of(Some(v)))
        .unwrap_or(default)
}

fn markup_button(text: impl Into<String>, callback_data: impl Into<String>) -> Value {
    json!({"text": text.into(), "callback_data": callback_data.into()})
}

fn markup(rows: Vec<Vec<Value>>) -> Value {
    json!({"inline_keyboard": rows})
}

fn mark(selected: bool, label: &str) -> String {
    if selected {
        format!("✅ {label}")
    } else {
        label.to_string()
    }
}

fn btc_sma_button(cfg: &Value, fast: i64, slow: i64) -> Value {
    let current = (
        cfg_int(cfg, "fast_period", 0),
        cfg_int(cfg, "slow_period", 0),
    );
    let text = mark(current == (fast, slow), &format!("SMA{fast}/SMA{slow}"));
    markup_button(text, format!("bm:sma:{fast}:{slow}"))
}

fn rotation_text(cfg: &Value) -> String {
    if !py_truthy(cfg_get(cfg, "rotate_after_exit")) {
        return "OFF".to_string();
    }
    if cfg_str(cfg, "entry_mode", "") != "trend" {
        return "ON (방향 유지 중 진입 모드에서만 동작)".to_string();
    }
    if cfg_str(cfg, "scan_scope", "") == "single" {
        return "ON (코인 지정 중에는 동작 안 함 — 지정 코인만 거래)".to_string();
    }
    "ON — 익절·손절 직후 방금 코인을 뺀 거래대금 1위 코인에 바로 진입".to_string()
}

fn scope_text(cfg: &Value) -> String {
    let size = cfg_int(cfg, "alt_universe_size", 50);
    match cfg_str(cfg, "scan_scope", "") {
        TRADFI_SCOPE => format!(
            "🏦 TradFi만 — 바이낸스 메인넷 미국주식 + 금 (선물 상장 종목, 24h 거래대금 상위 {size}, 미국 정규장 시간에만 신규 진입)"
        ),
        "alts" => format!("알트 전용 24h 거래대금 상위 {size}개 (BTC 제외, 동시 신호 시 거래대금 1위)"),
        "single" => {
            let single = cfg_str(cfg, "single_symbol", "");
            let single = if single.is_empty() { "BTCUSDT" } else { single };
            format!("지정 코인 {single}만")
        }
        _ => "BTC만".to_string(),
    }
}

fn us_session_text(session: &Value) -> String {
    if !py_truthy(Some(session)) {
        return "확인 불가".to_string();
    }
    let reason = session.get("reason").and_then(|v| v.as_str()).unwrap_or("");
    let label = {
        let ko = tradfi_ko(reason);
        if ko.is_empty() {
            if reason.is_empty() {
                "-".to_string()
            } else {
                reason.to_string()
            }
        } else {
            ko.to_string()
        }
    };
    let slice19 = |key: &str| -> String {
        session
            .get(key)
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .chars()
            .take(19)
            .collect()
    };
    if py_truthy(session.get("open")) {
        format!("✅ {label} ({})", slice19("regular_close"))
    } else {
        format!("⏸ {label} (다음 정규장 {})", slice19("regular_open"))
    }
}

fn profit_lock_switch_text(cfg: &Value, state: Option<&Value>) -> String {
    let count = cfg_int(cfg, "profit_lock_switch_count", 0);
    if count == 0 {
        return "OFF (익절 횟수와 상관없이 선택한 진입 모드를 유지)".to_string();
    }
    let empty = Value::Object(Map::new());
    let state = state.unwrap_or(&empty);
    let done = state
        .get("profit_lock_exit_count")
        .map(|v| py_int_of(Some(v)))
        .unwrap_or(0);
    let mut text = format!(
        "수익 확보 익절 {count}회마다 다음 진입부터 '돌파 시 진입' 강제 (현재 {done}/{count})"
    );
    let forced = py_truthy(state.get("entry_mode_forced_cross"))
        || py_truthy(state.get("pending_entry_mode_switch"));
    if forced {
        text.push_str(" · 돌파 강제 적용됨");
    }
    text
}

fn profit_lock_switch_button(cfg: &Value, state: Option<&Value>) -> String {
    let count = cfg_int(cfg, "profit_lock_switch_count", 0);
    let empty = Value::Object(Map::new());
    let state = state.unwrap_or(&empty);
    if count == 0 {
        return "🔁 익절N회→돌파 강제: OFF".to_string();
    }
    let done = state
        .get("profit_lock_exit_count")
        .map(|v| py_int_of(Some(v)))
        .unwrap_or(0);
    let forced_flag = py_truthy(state.get("entry_mode_forced_cross"))
        || py_truthy(state.get("pending_entry_mode_switch"));
    let forced = if forced_flag { " ✅" } else { "" };
    format!("🔁 익절{count}회→돌파 강제: ON ({done}/{count}){forced}")
}

fn coin_error_text(reason: &str) -> String {
    let (code, symbol) = match reason.split_once(':') {
        Some((c, s)) => (c, s),
        None => (reason, ""),
    };
    match code {
        "SYMBOL_EMPTY" => "코인 이름을 입력하세요. 예: /macoin SOL".to_string(),
        "SYMBOL_NOT_FOUND" => {
            format!("{symbol} USDT 무기한 선물을 이 거래소에서 찾지 못했습니다.")
        }
        "NOT_TRADING" => format!("{symbol}은(는) 지금 거래가 중지된 상태입니다."),
        "STABLECOIN" => format!("{symbol}은(는) 스테이블코인이라 지정할 수 없습니다."),
        "TRADFI" => format!("{symbol}은(는) 주식·원자재(TradFi) 계약이라 지정할 수 없습니다."),
        _ => format!("코인을 확인하지 못했습니다: {reason}"),
    }
}

fn reentry_text(cfg: &Value) -> String {
    let wait = cfg_int(cfg, "reentry_wait_bars", 3);
    if wait == 0 {
        "반대 크로스까지 대기".to_string()
    } else {
        format!("{wait}봉 대기 후 같은 방향 재진입 (반대 크로스 시 즉시)")
    }
}

fn entry_mode_text(cfg: &Value) -> String {
    if cfg_str(cfg, "entry_mode", "") == "trend" {
        format!(
            "방향 유지 중 진입 (SMA3이 SMA200 위=롱 / 아래=숏, 수익확보·손절 청산 후 그 코인은 {})",
            reentry_text(cfg)
        )
    } else {
        "돌파 시 진입 (SMA3이 SMA200을 뚫는 봉)".to_string()
    }
}

fn lock_table(cfg: &Value) -> String {
    let start = py_n(cfg_get(cfg, "lock_start_roi_percent"));
    let step = py_n(cfg_get(cfg, "lock_step_percent"));
    let gap = py_n(cfg_get(cfg, "lock_gap_percent"));
    let steps: Vec<f64> = (0..4).map(|i| start + step * i as f64).collect();
    let body = steps
        .iter()
        .map(|s| format!("+{}%→+{}%", format_g(*s), format_g(s - gap)))
        .collect::<Vec<_>>()
        .join(" / ");
    format!("{body} …")
}

// ------------------------------------------------------------ outgoing

#[derive(Debug, Clone)]
pub enum Outgoing {
    Send {
        chat_id: i64,
        text: String,
        markup: Option<Value>,
    },
    Edit {
        chat_id: i64,
        message_id: i64,
        text: String,
        markup: Option<Value>,
    },
    Answer {
        callback_id: String,
    },
}

pub struct TelegramBot<A: EngineApi> {
    api: TelegramApi,
    engine: Arc<A>,
    owner_chat_id: i64,
    offset: i64,
    #[allow(dead_code)] // reserved for text-input prompts
    pending_input: Mutex<HashMap<i64, String>>,
}

impl<A: EngineApi> TelegramBot<A> {
    pub fn new(api: TelegramApi, engine: Arc<A>, owner_chat_id: i64) -> Self {
        Self {
            api,
            engine,
            owner_chat_id,
            offset: 0,
            pending_input: Mutex::new(HashMap::new()),
        }
    }

    /// The engine connection (tests drive the in-process engine directly).
    pub fn engine(&self) -> &Arc<A> {
        &self.engine
    }

    async fn engine_call(&self, cmd: &str, args: Value) -> Result<Value, String> {
        self.engine.call(cmd, Some(STRATEGY), args).await
    }

    fn engine_error_text(e: &str) -> String {
        format!("⚠️ 엔진 응답 없음: {e}")
    }

    // ------------------------------------------------------ run

    pub async fn run(&mut self) -> Result<(), String> {
        loop {
            let updates = match self.api.get_updates(Some(self.offset), 30).await {
                Ok(list) => list,
                Err(e) => {
                    log_line(&format!("telegram: get_updates failed: {e}"));
                    tokio::time::sleep(std::time::Duration::from_secs(5)).await;
                    continue;
                }
            };
            for update in updates {
                if let Some(id) = update.get("update_id").and_then(|v| v.as_i64()) {
                    self.offset = id + 1;
                }
                if !self.is_authorized(&update) {
                    continue;
                }
                let outgoing = self.handle_update(&update).await;
                self.dispatch(outgoing).await;
            }
        }
    }

    async fn dispatch(&self, outgoing: Vec<Outgoing>) {
        for item in outgoing {
            match item {
                Outgoing::Send {
                    chat_id,
                    text,
                    markup,
                } => {
                    let chat = chat_id.to_string();
                    match self.api.send_message(&chat, &text, markup).await {
                        Ok(_) => {}
                        Err(e) => log_line(&format!("telegram: send failed: {e}")),
                    }
                }
                Outgoing::Edit {
                    chat_id,
                    message_id,
                    text,
                    markup,
                } => {
                    let chat = chat_id.to_string();
                    match self
                        .api
                        .edit_message_text(&chat, message_id, &text, markup)
                        .await
                    {
                        Ok(_) => {}
                        Err(e) => {
                            if !e.to_lowercase().contains("message is not modified") {
                                log_line(&format!("telegram: edit failed: {e}"));
                            }
                        }
                    }
                }
                Outgoing::Answer { callback_id } => {
                    if let Err(e) = self.api.answer_callback_query(&callback_id).await {
                        // python: ignored on purpose (BadRequest is only logged)
                        log_line(&format!("telegram: answer failed: {e}"));
                    }
                }
            }
        }
    }

    /// Python `_require_authorized_telegram_update`: compare chat id.
    fn is_authorized(&self, update: &Value) -> bool {
        fn chat_id_of(v: &Value) -> Option<i64> {
            v.as_object()
                .and_then(|m| m.get("chat"))
                .and_then(|c| c.get("id"))
                .and_then(|id| id.as_i64())
        }
        if let Some(msg) = update.get("message") {
            return chat_id_of(msg) == Some(self.owner_chat_id);
        }
        if let Some(query) = update.get("callback_query") {
            if let Some(msg) = query.get("message") {
                if let Some(id) = chat_id_of(msg) {
                    return id == self.owner_chat_id;
                }
            }
            if let Some(from) = query.get("from") {
                return from.get("id").and_then(|v| v.as_i64()) == Some(self.owner_chat_id);
            }
        }
        false
    }

    // ------------------------------------------------------ update dispatch

    pub async fn handle_update(&self, update: &Value) -> Vec<Outgoing> {
        // Only the configured owner chat may drive the bot (defence in depth: run() checks too).
        if !self.is_authorized(update) {
            return Vec::new();
        }
        if let Some(query) = update.get("callback_query") {
            return self.handle_callback(query).await;
        }
        if let Some(message) = update.get("message") {
            return self.handle_message(message).await;
        }
        Vec::new()
    }

    async fn handle_callback(&self, query: &Value) -> Vec<Outgoing> {
        let callback_id = query
            .get("id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let data = query.get("data").and_then(|v| v.as_str()).unwrap_or("");
        let mut out = vec![Outgoing::Answer { callback_id }];
        if !data.starts_with("bm:") {
            return out;
        }
        let action = data.split_once(':').map(|x| x.1).unwrap_or("");
        let (text, markup) = self.handle_btc_ma_cross_action(action).await;
        let (chat_id, message_id) = match query.get("message") {
            Some(msg) => (
                msg.get("chat")
                    .and_then(|c| c.get("id"))
                    .and_then(|v| v.as_i64())
                    .unwrap_or(self.owner_chat_id),
                msg.get("message_id").and_then(|v| v.as_i64()).unwrap_or(0),
            ),
            None => (self.owner_chat_id, 0),
        };
        out.push(Outgoing::Edit {
            chat_id,
            message_id,
            text,
            markup: Some(markup),
        });
        out
    }

    async fn handle_message(&self, message: &Value) -> Vec<Outgoing> {
        let chat_id = message
            .get("chat")
            .and_then(|c| c.get("id"))
            .and_then(|v| v.as_i64())
            .unwrap_or(self.owner_chat_id);
        let text = message.get("text").and_then(|v| v.as_str()).unwrap_or("");
        let (command, args) = parse_command(text);
        match command.as_str() {
            "btcmacross" => {
                let (status, markup) = self.status_screen().await;
                vec![Outgoing::Send {
                    chat_id,
                    text: status,
                    markup: Some(markup),
                }]
            }
            "macoin" => self.handle_macoin(chat_id, &args).await,
            "start" | "help" => self.handle_help(chat_id).await,
            "stop" => self.handle_stop(chat_id).await,
            other if BTC_MA_PERIOD_COMMANDS.contains(&other) => {
                self.handle_period_cmd(chat_id, &args).await
            }
            _ => Vec::new(),
        }
    }

    async fn handle_help(&self, chat_id: i64) -> Vec<Outgoing> {
        let text = "📖 사용 가능한 명령\n\
                    /btcmacross — 3/200 SMA 크로스 전략 메뉴\n\
                    /macoin <코인> — 거래할 코인 1개 지정 (예: /macoin SOL)\n\
                    /btcma <패스트> <슬로우> — SMA 기간 변경 (예: /btcma 1 200)\n\
                    /btcmaperiod — SMA 기간 도움말\n\
                    /stop — 모든 선물 전략 비상 정지"
            .to_string();
        vec![Outgoing::Send {
            chat_id,
            text,
            markup: None,
        }]
    }

    async fn handle_stop(&self, chat_id: i64) -> Vec<Outgoing> {
        let result = self.engine_call("emergency_stop", json!({})).await;
        let text = match result {
            Ok(value) => {
                let lines = value
                    .get("lines")
                    .and_then(|v| v.as_array())
                    .map(|a| {
                        a.iter()
                            .filter_map(|v| v.as_str().map(|s| s.to_string()))
                            .collect::<Vec<_>>()
                    })
                    .unwrap_or_default();
                let mut out = "🚨 비상 정지".to_string();
                for line in lines {
                    out.push('\n');
                    out.push_str(&line);
                }
                out
            }
            Err(e) => Self::engine_error_text(&e),
        };
        vec![Outgoing::Send {
            chat_id,
            text,
            markup: None,
        }]
    }

    async fn handle_macoin(&self, chat_id: i64, args: &[String]) -> Vec<Outgoing> {
        let network = match self.engine_call("current_network", json!({})).await {
            Ok(Value::String(n)) => n,
            Ok(_) => {
                let (status, _) = self.status_screen().await;
                return vec![Outgoing::Send {
                    chat_id,
                    text: status,
                    markup: None,
                }];
            }
            Err(e) => {
                return vec![Outgoing::Send {
                    chat_id,
                    text: Self::engine_error_text(&e),
                    markup: None,
                }]
            }
        };
        if args.is_empty() {
            let pick = self.pick_keyboard(&network).await;
            return vec![Outgoing::Send {
                chat_id,
                text: "🎯 거래할 코인 1개를 고르세요. 직접 입력: /macoin SOL".to_string(),
                markup: Some(pick),
            }];
        }
        let (ok, message) = self.set_coin(&network, &args[0]).await;
        let (status, markup) = self.status_screen().await;
        let body = if ok {
            format!("{message}\n\n{status}")
        } else {
            message
        };
        vec![Outgoing::Send {
            chat_id,
            text: body,
            markup: Some(markup),
        }]
    }

    async fn handle_period_cmd(&self, chat_id: i64, args: &[String]) -> Vec<Outgoing> {
        if args.is_empty() {
            let (help_text, _) = self.handle_btc_ma_cross_action("sma_help").await;
            let (_, markup) = self.status_screen().await;
            return vec![Outgoing::Send {
                chat_id,
                text: help_text,
                markup: Some(markup),
            }];
        }
        match parse_period_command(args) {
            Ok((fast, slow)) => {
                let (_, message) = self.set_periods(fast, slow).await;
                let (status, markup) = self.status_screen().await;
                vec![Outgoing::Send {
                    chat_id,
                    text: format!("{message}\n\n{status}"),
                    markup: Some(markup),
                }]
            }
            Err(e) => {
                let cfg = self.config().await;
                let current = periods_label(
                    cfg_int(&cfg, "fast_period", 3),
                    cfg_int(&cfg, "slow_period", 200),
                );
                let text = format!("⚠️ 기간 변경 실패: {e}\n예) /btcma 1 200\n지금: {current}");
                vec![Outgoing::Send {
                    chat_id,
                    text,
                    markup: None,
                }]
            }
        }
    }

    async fn status_screen(&self) -> (String, Value) {
        let text = self.format_status().await;
        let cfg = self.config().await;
        let state = self.load_state_or_empty().await;
        let markup = self.build_keyboard(&cfg, None, Some(&state));
        (text, markup)
    }

    // ------------------------------------------------------ config / state helpers

    async fn config(&self) -> Value {
        match self.engine_call("config_get", json!({})).await {
            Ok(v @ Value::Object(_)) => v,
            Ok(_) => Value::Object(Map::new()),
            Err(e) => {
                log_line(&format!("telegram: config_get failed: {e}"));
                Value::Object(Map::new())
            }
        }
    }

    async fn set_value(&self, key: &str, value: Value) -> Result<(), String> {
        let mut changes = Map::new();
        changes.insert(key.to_string(), value);
        self.engine_call("config_set", json!({ "changes": changes }))
            .await
            .map(|_| ())
    }

    async fn set_coin_value(&self, key: &str, value: Value) -> Result<(), String> {
        let arg = serde_json::to_value(value).map_err(|e| format!("ValueError: {e}"))?;
        self.set_value(key, arg).await
    }

    async fn load_state_or_empty(&self) -> Value {
        match self.engine_call("load_state", json!({})).await {
            Ok(v @ Value::Object(_)) => v,
            Ok(_) => Value::Object(Map::new()),
            Err(e) => {
                log_line(&format!("telegram: load_state failed: {e}"));
                Value::Object(Map::new())
            }
        }
    }

    // ------------------------------------------------------ rendering

    fn build_keyboard(
        &self,
        cfg: &Value,
        confirm: Option<(&str, &str)>,
        state: Option<&Value>,
    ) -> Value {
        let enabled = py_truthy(cfg_get(cfg, "enabled"));
        let trading_mode = cfg_str(cfg, "trading_mode", "");
        let scan_scope = cfg_str(cfg, "scan_scope", "");
        let entry_mode = cfg_str(cfg, "entry_mode", "");
        let timeframe = cfg_str(cfg, "timeframe", "");
        let alt_size = cfg_int(cfg, "alt_universe_size", 50);
        let single = cfg_str(cfg, "single_symbol", "");
        let single = if single.is_empty() { "BTCUSDT" } else { single };

        let mut rows: Vec<Vec<Value>> = Vec::new();
        rows.push(vec![
            markup_button(mark(enabled, "▶️ 전략 ON"), "bm:on"),
            markup_button(mark(!enabled, "⏹ 신규진입 OFF"), "bm:off"),
        ]);
        rows.push(vec![
            markup_button(mark(trading_mode == "DRY_RUN", "🧪 DRY_RUN"), "bm:mode:dry"),
            markup_button(mark(trading_mode == "LIVE", "💥 LIVE"), "bm:mode:live"),
        ]);
        let pick_label = if scan_scope == "single" {
            mark(true, &format!("🎯 {single}"))
        } else {
            "🎯 코인 지정".to_string()
        };
        rows.push(vec![
            markup_button(
                mark(
                    !matches!(scan_scope, "alts" | "single" | TRADFI_SCOPE),
                    "🪙 BTC만",
                ),
                "bm:scope:btc",
            ),
            markup_button(
                mark(scan_scope == "alts", &format!("🌐 알트 상위{alt_size}")),
                "bm:scope:alts",
            ),
            markup_button(pick_label, "bm:pick"),
        ]);
        rows.push(vec![markup_button(
            mark(scan_scope == TRADFI_SCOPE, "🏦 TradFi만 (메인넷)"),
            "bm:scope:tradfi",
        )]);
        rows.push(vec![
            markup_button(
                mark(entry_mode != "trend", "⚡ 돌파 시 진입"),
                "bm:entry:cross",
            ),
            markup_button(
                mark(entry_mode == "trend", "📈 방향 유지 중 진입"),
                "bm:entry:trend",
            ),
        ]);
        let pls_count = cfg_int(cfg, "profit_lock_switch_count", 0);
        let pls_cb = if pls_count == 0 {
            "bm:pls:on"
        } else {
            "bm:pls:off"
        };
        rows.push(vec![markup_button(
            profit_lock_switch_button(cfg, state),
            pls_cb,
        )]);
        let rot_on = py_truthy(cfg_get(cfg, "rotate_after_exit"));
        rows.push(vec![markup_button(
            mark(rot_on, "🔁 청산 후 다른 코인 바로 진입"),
            if rot_on { "bm:rot:off" } else { "bm:rot:on" },
        )]);
        let reentry = cfg_int(cfg, "reentry_wait_bars", 3);
        rows.push(
            REENTRY_WAIT_CHOICES
                .iter()
                .map(|choice| {
                    let label = if *choice == 0 {
                        "재진입 크로스까지".to_string()
                    } else {
                        format!("재진입 {choice}봉 후")
                    };
                    markup_button(mark(reentry == *choice, &label), format!("bm:rw:{choice}"))
                })
                .collect(),
        );
        let presets = PERIOD_PRESETS;
        let head: Vec<Value> = presets
            .iter()
            .take(2)
            .map(|(_l, f, s)| btc_sma_button(cfg, *f, *s))
            .collect();
        let tail: Vec<Value> = presets
            .iter()
            .skip(2)
            .map(|(_l, f, s)| btc_sma_button(cfg, *f, *s))
            .collect();
        rows.push(head);
        rows.push(tail);
        rows.push(vec![markup_button(
            "✏️ 직접 입력: /btcma 1 200",
            "bm:sma_help",
        )]);
        let tf_buttons = |slice: &[&'static str]| -> Vec<Value> {
            slice
                .iter()
                .map(|tf| {
                    markup_button(
                        mark(timeframe == *tf, timeframe_labels(tf)),
                        format!("bm:tf:{tf}"),
                    )
                })
                .collect()
        };
        rows.push(tf_buttons(&TIMEFRAMES[..4]));
        rows.push(tf_buttons(&TIMEFRAMES[4..]));
        let leverage = cfg_int(cfg, "leverage", 5);
        rows.push(
            LEVERAGE_CHOICES
                .iter()
                .map(|lev| {
                    markup_button(
                        mark(leverage == *lev, &format!("{lev}x")),
                        format!("bm:lev:{lev}"),
                    )
                })
                .collect(),
        );
        let stop = cfg_int(cfg, "emergency_stop_roi_percent", 0);
        rows.push(
            EMERGENCY_STOP_CHOICES
                .iter()
                .map(|choice| {
                    let label = if *choice == 0 {
                        "비상손절 OFF".to_string()
                    } else {
                        format!("-{choice}%")
                    };
                    markup_button(mark(stop == *choice, &label), format!("bm:es:{choice}"))
                })
                .collect(),
        );
        rows.push(vec![
            markup_button("📊 상태", "bm:status"),
            markup_button("📈 성과 통계", "bm:stats"),
        ]);
        rows.push(vec![
            markup_button("🧾 최근 판단", "bm:signals"),
            markup_button("🔄 거래소 재대조", "bm:reconcile"),
        ]);
        rows.push(vec![
            markup_button("📘 전략 규칙", "bm:rules"),
            markup_button("🔻 포지션 청산", "bm:close"),
        ]);
        if let Some((confirm_text, confirm_cb)) = confirm {
            rows.insert(
                0,
                vec![
                    markup_button(confirm_text, confirm_cb),
                    markup_button("취소", "bm:status"),
                ],
            );
        }
        markup(rows)
    }

    async fn pick_keyboard(&self, network: &str) -> Value {
        let cfg = self.config().await;
        let coins: Vec<String> = match self
            .engine_call("pick_universe", json!({ "network": network }))
            .await
        {
            Ok(Value::Array(list)) => list
                .iter()
                .filter_map(|v| v.as_str().map(|s| s.to_string()))
                .collect(),
            Ok(_) => vec!["BTCUSDT".to_string()],
            Err(e) => {
                log_line(&format!("3/200 SMA coin list unavailable: {e}"));
                vec!["BTCUSDT".to_string()]
            }
        };
        let current = if cfg_str(&cfg, "scan_scope", "") == "single" {
            cfg.get("single_symbol").cloned()
        } else {
            None
        };
        let buttons: Vec<Value> = coins
            .iter()
            .map(|coin| {
                let selected = current
                    .as_ref()
                    .and_then(|v| v.as_str())
                    .map(|s| s == coin)
                    .unwrap_or(false);
                let label = format!(
                    "{}{}",
                    if selected { "✅ " } else { "" },
                    coin.strip_suffix("USDT").unwrap_or(coin)
                );
                markup_button(label, format!("bm:coin:{coin}"))
            })
            .collect();
        let mut rows: Vec<Vec<Value>> = buttons.chunks(4).map(|c| c.to_vec()).collect();
        rows.push(vec![markup_button("↩️ 돌아가기", "bm:status")]);
        let _ = PICK_LIST_SIZE; // engine enforces the size
        markup(rows)
    }

    async fn format_status(&self) -> String {
        let status = match self.engine_call("status", json!({})).await {
            Ok(v @ Value::Object(_)) => v,
            Ok(_) => Value::Object(Map::new()),
            Err(e) => return Self::engine_error_text(&e),
        };
        if py_truthy(status.get("unsupported")) {
            return "📏 3/200 SMA 크로스 전략\n\
                    지금 /setup 거래소가 업비트라 사용할 수 없습니다. /setup에서 바이낸스(테스트넷 데모 또는 메인넷)를 선택하세요."
                .to_string();
        }
        let empty = Value::Object(Map::new());
        let cfg = status.get("config").unwrap_or(&empty);
        let state = status.get("state").unwrap_or(&empty);
        let mode = status.get("mode").and_then(|v| v.as_str()).unwrap_or("");
        let network = status.get("network").and_then(|v| v.as_str()).unwrap_or("");
        let has_credentials = py_truthy(status.get("has_credentials"));
        let trade = state
            .get("trade")
            .cloned()
            .unwrap_or(Value::Object(Map::new()));
        let trade = if trade.is_null() {
            Value::Object(Map::new())
        } else {
            trade
        };
        let trade_held = py_truthy(Some(&trade));
        let stop = cfg_int(cfg, "emergency_stop_roi_percent", 0);

        let fast = cfg_int(cfg, "fast_period", 3);
        let slow = cfg_int(cfg, "slow_period", 200);
        let mut lines: Vec<String> = vec![
            format!("📏 {fast}/{slow} SMA 크로스 전략 (반대 크로스 시 즉시 반대 진입)"),
            format!(
                "전략: {} · 모드: {mode}{}",
                if py_truthy(cfg_get(cfg, "enabled")) {
                    "ON"
                } else {
                    "OFF"
                },
                if cfg_str(cfg, "trading_mode", "") == "LIVE" && mode == "DRY_RUN" {
                    " (환경변수로 DRY_RUN 강제)"
                } else {
                    ""
                }
            ),
            format!(
                "거래소: {} (/setup 선택을 따름) · API 키: {}",
                network_label(network),
                if has_credentials { "있음" } else { "없음" }
            ),
            "메인 선물 자동매매: 동작 중 (선물 전략은 하나만 켜짐)".to_string(),
            format!("종목: {}", scope_text(cfg)),
            format!("진입: {}", entry_mode_text(cfg)),
            format!(
                "익절 자동 전환: {}",
                profit_lock_switch_text(cfg, Some(state))
            ),
            format!("청산 후 다른 코인 전환: {}", rotation_text(cfg)),
            format!(
                "봉: {} (마감봉 기준) · SMA {fast}/{slow}",
                timeframe_labels(cfg_str(cfg, "timeframe", ""))
            ),
            format!(
                "레버리지 {}x ISOLATED (2~5x 선택, 다음 진입부터) · 증거금: 가용 잔고의 {:.0}%",
                cfg_int(cfg, "leverage", 5),
                py_n(cfg_get(cfg, "margin_fraction")) * 100.0
            ),
            if stop == 0 {
                "비상손절: OFF".to_string()
            } else {
                format!("비상손절: 증거금 -{stop}% (Mark 기준)")
            },
            format!(
                "수익 확보 손절(증거금 기준, Last 기준 트리거): {}",
                lock_table(cfg)
            ),
        ];
        if cfg_str(cfg, "scan_scope", "") == TRADFI_SCOPE {
            let tradfi_state = match self
                .engine_call("tradifi_scope_state", json!({ "network": network }))
                .await
            {
                Ok(v) => v,
                Err(e) => return Self::engine_error_text(&e),
            };
            let allowed = py_truthy(tradfi_state.get("allowed"));
            let detail = tradfi_state
                .get("detail")
                .map(|v| match v {
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                })
                .unwrap_or_default();
            lines.push(if allowed {
                "🏦 TradFi 신규 진입: 가능".to_string()
            } else {
                format!("🏦 TradFi 신규 진입: 차단 — {detail}")
            });
            let session = match self.engine_call("us_session_status", json!({})).await {
                Ok(v) => v,
                Err(e) => return Self::engine_error_text(&e),
            };
            lines.push(format!("미국 정규장: {}", us_session_text(&session)));
        }
        if mode == "DRY_RUN" && !has_credentials {
            let balance = cfg
                .get("dry_run_balance_usdt")
                .map(|v| match v {
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                })
                .unwrap_or_default();
            lines.push(format!(
                "가상 잔고: {balance} USDT + 가상 손익 (API 키 없음)"
            ));
        }
        if trade_held {
            let market_id = trade
                .get("market_id")
                .and_then(|v| v.as_str())
                .unwrap_or("BTCUSDT");
            let side = trade.get("side").map(value_display).unwrap_or_default();
            let quantity = trade.get("quantity").map(value_display).unwrap_or_default();
            let entry_price = trade
                .get("entry_price")
                .map(value_display)
                .unwrap_or_default();
            let margin = py_n(trade.get("margin_used"));
            lines.push(String::new());
            lines.push(format!(
                "보유: {market_id} {side} {quantity} @ {entry_price} (증거금 {margin:.2} USDT)"
            ));
            let stop_price = trade
                .get("stop_price")
                .filter(|v| py_truthy(Some(v)))
                .map(value_display)
                .unwrap_or_else(|| "OFF".to_string());
            let lock_desc = match trade.get("lock_price") {
                Some(v) if py_truthy(Some(v)) => {
                    let lock_roi = py_n(trade.get("lock_roi"));
                    let lock_price = trade
                        .get("lock_price")
                        .map(value_display)
                        .unwrap_or_default();
                    format!("+{}% @ {lock_price}", format_g(lock_roi))
                }
                _ => "아직 없음 (+5% 도달 전)".to_string(),
            };
            lines.push(format!("비상손절 {stop_price} · 수익 확보 {lock_desc}"));
        } else {
            lines.push(String::new());
            lines.push("보유: 없음".to_string());
            let wait_lines = match self.engine_call("reentry_wait_lines", json!({})).await {
                Ok(Value::Array(list)) => list,
                Ok(_) => Vec::new(),
                Err(e) => return Self::engine_error_text(&e),
            };
            for wait_line in wait_lines {
                lines.push(format!("⏳ {}", value_display(&wait_line)));
            }
        }
        let last = status
            .get("last_skip")
            .cloned()
            .unwrap_or(Value::Object(Map::new()));
        if py_truthy(Some(&last)) {
            let detail = last
                .get("skip_reason")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string())
                .or_else(|| {
                    last.get("side")
                        .and_then(|v| v.as_str())
                        .map(|s| format!("{s} 크로스"))
                })
                .unwrap_or_else(|| "-".to_string());
            lines.push(format!("최근 판단 ({}): {detail}", kst(last.get("ts"))));
        }
        if let Some(err) = state.get("last_error") {
            if py_truthy(Some(err)) {
                lines.push(format!("최근 오류: {}", value_display(err)));
            }
        }
        lines.push("OFF는 신규 진입만 멈추며, 보유 포지션의 비상손절·수익 확보·반대 크로스 청산은 계속됩니다.".to_string());
        lines.join("\n")
    }

    async fn format_stats(&self) -> String {
        let status = match self.engine_call("status", json!({})).await {
            Ok(v @ Value::Object(_)) => v,
            Ok(_) => Value::Object(Map::new()),
            Err(e) => return Self::engine_error_text(&e),
        };
        if py_truthy(status.get("unsupported")) {
            return "/setup 거래소가 업비트라 사용할 수 없습니다.".to_string();
        }
        let empty = Value::Object(Map::new());
        let s = status.get("stats").unwrap_or(&empty);
        let mode = status.get("mode").and_then(|v| v.as_str()).unwrap_or("");
        let network = status.get("network").and_then(|v| v.as_str()).unwrap_or("");
        let pf = py_n(s.get("profit_factor"));
        let pf_text = if pf == f64::INFINITY {
            "∞".to_string()
        } else {
            format!("{pf:.2}")
        };
        let num = |key: &str| py_n(s.get(key));
        let int = |key: &str| s.get(key).map(|v| py_int_of(Some(v))).unwrap_or(0);
        [
            format!("📈 3/200 SMA 성과 ({mode} · {network})"),
            format!(
                "거래 {}회 · 승 {} / 패 {} · 승률 {:.1}%",
                int("total_trades"),
                int("wins"),
                int("losses"),
                num("win_rate") * 100.0
            ),
            format!(
                "총이익 {} · 총손실 {} · 순손익 {} USDT",
                format_plus4(num("gross_profit")),
                format_plus4(num("gross_loss")),
                format_plus4(num("net_pnl"))
            ),
            format!(
                "평균 이익 {} · 평균 손실 {}",
                format_plus4(num("average_win")),
                format_plus4(num("average_loss"))
            ),
            format!(
                "손익비(PF) {pf_text} · 기대값 {} USDT/회 · 평균 증거금 수익률 {:+.2}%",
                format_plus4(num("expectancy")),
                num("average_r") * 100.0
            ),
            format!(
                "최대 연승 {} · 최대 연패 {} · 최대 낙폭 {:.4} USDT",
                int("max_consecutive_wins"),
                int("max_consecutive_losses"),
                num("max_drawdown")
            ),
            "과거 성과가 미래 수익을 보장하지 않습니다.".to_string(),
        ]
        .join("\n")
    }

    async fn format_signals(&self) -> String {
        let network = match self.engine_call("current_network", json!({})).await {
            Ok(Value::String(n)) => n,
            Ok(_) => return "/setup 거래소가 업비트라 사용할 수 없습니다.".to_string(),
            Err(e) => return Self::engine_error_text(&e),
        };
        let kinds = json!(["SIGNAL", "SKIP", "ORDER", "EXIT", "FAILSAFE", "PROFIT_LOCK"]);
        let events = match self
            .engine_call(
                "recent_events",
                json!({ "network": network, "limit": 8, "kinds": kinds }),
            )
            .await
        {
            Ok(Value::Array(list)) => list,
            Ok(_) => Vec::new(),
            Err(e) => return Self::engine_error_text(&e),
        };
        if events.is_empty() {
            return "🧾 아직 기록된 판단이 없습니다. (선택한 봉이 마감될 때마다 기록됩니다)"
                .to_string();
        }
        let mut lines = vec!["🧾 최근 판단 (최신순)".to_string()];
        for event in &events {
            let kind = event.get("event").and_then(|v| v.as_str()).unwrap_or("");
            let detail = match kind {
                "SIGNAL" => {
                    let body = match event.get("side") {
                        Some(side) if py_truthy(Some(side)) => {
                            let market = event
                                .get("market_id")
                                .and_then(|v| v.as_str())
                                .unwrap_or("");
                            format!("{} {} 신호", market, value_display(side))
                                .trim()
                                .to_string()
                        }
                        _ => event
                            .get("skip_reason")
                            .map(value_display)
                            .unwrap_or_default(),
                    };
                    let mut detail = if body.is_empty() {
                        "-".to_string()
                    } else {
                        body
                    };
                    if py_truthy(event.get("rotation")) {
                        let exclude = event.get("exclude").map(value_display).unwrap_or_default();
                        let evaluated = event
                            .get("evaluated")
                            .map(value_display)
                            .unwrap_or_default();
                        detail.push_str(&format!(
                            " · 청산 후 다른 코인 전환 ({exclude} 제외 {evaluated}종목 평가)"
                        ));
                    } else if event.get("scope").and_then(|v| v.as_str()) == Some("alts") {
                        let evaluated = event
                            .get("evaluated")
                            .map(value_display)
                            .unwrap_or_default();
                        let crosses = event
                            .get("crosses")
                            .and_then(|v| v.as_array())
                            .map(|a| a.len())
                            .unwrap_or(0);
                        detail.push_str(&format!(" · {evaluated}종목 평가, 크로스 {crosses}건"));
                    } else {
                        let fast = format_g_prec(py_n(event.get("sma_fast")), 8);
                        let slow = format_g_prec(py_n(event.get("sma_slow")), 8);
                        detail.push_str(&format!(" · SMA3 {fast} / SMA200 {slow}"));
                    }
                    detail
                }
                "ORDER" => {
                    let symbol = event.get("symbol").and_then(|v| v.as_str()).unwrap_or("");
                    let side = event.get("side").map(value_display).unwrap_or_default();
                    let quantity = event.get("quantity").map(value_display).unwrap_or_default();
                    let entry = event
                        .get("actual_avg_entry")
                        .map(value_display)
                        .unwrap_or_default();
                    format!("{symbol} {side} {quantity} @ {entry}")
                }
                "PROFIT_LOCK" => {
                    let reached = format_g(py_n(event.get("reached_roi")));
                    let locked = format_g(py_n(event.get("locked_roi")));
                    let trigger = event.get("trigger").map(value_display).unwrap_or_default();
                    format!("+{reached}% 도달 → +{locked}% 확보 @ {trigger}")
                }
                _ => event
                    .get("skip_reason")
                    .or_else(|| event.get("exit_reason"))
                    .or_else(|| event.get("reason"))
                    .map(value_display)
                    .unwrap_or_else(|| "-".to_string()),
            };
            lines.push(format!("{} {kind}: {detail}", kst(event.get("ts"))));
        }
        lines.join("\n")
    }

    fn rules_text(&self, cfg: &Value) -> String {
        let stop = cfg_int(cfg, "emergency_stop_roi_percent", 0);
        let fast = cfg_int(cfg, "fast_period", 3);
        let slow = cfg_int(cfg, "slow_period", 200);
        let leverage = cfg_int(cfg, "leverage", 5);
        let margin = py_n(cfg_get(cfg, "margin_fraction")) * 100.0;
        let gap = cfg
            .get("lock_gap_percent")
            .map(value_display)
            .unwrap_or_default();
        let stop_line = if stop == 0 {
            "OFF".to_string()
        } else {
            format!("증거금 -{stop}%")
        };
        format!(
            "📘 {fast}/{slow} SMA 크로스 규칙 (기간은 /btcma 또는 버튼으로 변경)\n\
             • 종목: {} (TradFi·스테이블코인 제외, 🎯 코인 지정 또는 /macoin SOL 로 1개 지정)\n\
             • 진입: {}\n\
             • 익절 자동 전환: {}\n\
             • 청산 후 다른 코인 전환: {}\n\
             • 봉: {} 마감봉 기준, 진입·청산(반대 크로스) 모두 같은 봉 (3분~4시간 선택)\n\
             • 보유 중 반대 크로스(또는 봉 마감 때 SMA3이 반대편): 시장가(reduce-only) 청산 후 즉시 반대 방향 진입\n\
             • 수익 확보(증거금 기준): {} (항상 도달 칸 {gap}% 아래, 한 번 올라간 손절은 내려가지 않음)\n\
             • 수익 확보 손절로 청산되면 다음 크로스까지 대기\n\
             • 비상손절: {stop_line} (메뉴에서 선택, 거래소에 Mark 기준으로 걸림)\n\
             • 레버리지 {leverage}배 ISOLATED 선택(2~5배, 다음 진입부터), 가용 증거금의 {margin:.0}% 사용, 계좌 전체 1포지션\n\
             • 🏦 TradFi만: 바이낸스 메인넷 전용 · 미국주식 TradFi + 금만 · 미국 정규장 시간에만 신규 진입\n\
             • 거래소는 /setup 선택을 따르고, 켜면 다른 선물 자동전략은 멈춤\n\
             • 수익을 보장하지 않는 전략입니다.",
            scope_text(cfg),
            entry_mode_text(cfg),
            profit_lock_switch_text(cfg, None),
            rotation_text(cfg),
            timeframe_labels(cfg_str(cfg, "timeframe", "")),
            lock_table(cfg),
        )
    }

    // ------------------------------------------------------ period / coin setters

    async fn set_periods(&self, fast: impl Into<i64>, slow: impl Into<i64>) -> (bool, String) {
        let fast_arg = fast.into();
        let slow_arg = slow.into();
        let parsed = parse_period_command(&[fast_arg.to_string(), slow_arg.to_string()]);
        let (fast_p, slow_p) = match parsed {
            Ok(pair) => pair,
            Err(e) => {
                return (false, format!("⚠️ 기간 변경 실패: {e}\n예) /btcma 1 200"));
            }
        };
        // Python reads cfg_before before writing the new pair.
        let cfg_before = self.config().await;
        if let Err(e) = self.set_coin_value("fast_period", json!(fast_p)).await {
            return (false, format!("⚠️ 기간 변경 실패: {e}\n예) /btcma 1 200"));
        }
        if let Err(e) = self.set_coin_value("slow_period", json!(slow_p)).await {
            return (false, format!("⚠️ 기간 변경 실패: {e}\n예) /btcma 1 200"));
        }
        let needed = required_candle_history(slow_p);
        if cfg_int(&cfg_before, "candle_history", 0) < needed {
            if let Err(e) = self.set_coin_value("candle_history", json!(needed)).await {
                return (false, format!("⚠️ 기간 변경 실패: {e}\n예) /btcma 1 200"));
            }
        }
        let cfg = self.config().await;
        let holding = match self.engine_call("current_network", json!({})).await {
            Ok(Value::String(_)) => {
                let state = self.load_state_or_empty().await;
                py_truthy(state.get("trade"))
            }
            _ => false,
        };
        let note = if holding {
            "\n(보유 중인 포지션은 진입 당시 기간 기준으로 관리, 다음 진입부터 새 기간 적용)"
        } else {
            ""
        };
        let timeframe = cfg_str(&cfg, "timeframe", "");
        let history = cfg_int(&cfg, "candle_history", 0);
        (
            true,
            format!(
                "✅ SMA 기간 변경: {} (패스트 {fast_p} · 슬로우 {slow_p})\n\
                 • 봉 {timeframe} 기준 · 신호 계산에 {history}봉 사용{note}",
                periods_label(fast_p, slow_p)
            ),
        )
    }

    async fn set_coin(&self, network: &str, text: &str) -> (bool, String) {
        let resolved = self
            .engine_call("resolve_coin", json!({ "network": network, "text": text }))
            .await;
        let market_id = match resolved {
            Ok(v) => v
                .get("market_id")
                .and_then(|m| m.as_str())
                .unwrap_or("")
                .to_string(),
            Err(e) => {
                let code = e.split(':').next().unwrap_or("");
                if matches!(
                    code,
                    "SYMBOL_EMPTY" | "SYMBOL_NOT_FOUND" | "NOT_TRADING" | "STABLECOIN" | "TRADFI"
                ) {
                    return (false, format!("⚠️ {}", coin_error_text(&e)));
                }
                let name = e.split(':').next().unwrap_or("Exception");
                return (
                    false,
                    format!("⚠️ 거래소 종목 정보를 불러오지 못했습니다: {name}"),
                );
            }
        };
        if let Err(e) = self.set_coin_value("single_symbol", json!(market_id)).await {
            return (false, format!("⚠️ {e}"));
        }
        if let Err(e) = self.set_coin_value("scan_scope", json!("single")).await {
            return (false, format!("⚠️ {e}"));
        }
        let state = self.load_state_or_empty().await;
        let holding = state.get("trade").cloned().unwrap_or(Value::Null);
        let mut note = String::new();
        if py_truthy(Some(&holding)) {
            let held_market = holding
                .get("market_id")
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty())
                .unwrap_or("BTCUSDT")
                .to_string();
            if held_market != market_id {
                note = format!(
                    "\n(보유 중인 {held_market} 포지션은 그대로 관리, 청산 후부터 {market_id}만 진입)"
                );
            }
        }
        (
            true,
            format!("🎯 코인 지정: {market_id}만 거래합니다.{note}"),
        )
    }

    async fn reset_counters(&self, network: &str) -> Result<Value, String> {
        self.engine_call("reset_counters", json!({ "network": network }))
            .await
    }

    fn tradfi_scope_refusal(&self, allowed: bool, network: &str) -> String {
        if allowed {
            return String::new();
        }
        format!(
            "🚫 TradFi 전용은 바이낸스 메인넷에서만 사용할 수 있습니다.\n\
             지금 거래소: {} (/setup 선택을 따름)\n\
             테스트넷(데모)에는 미국주식 TradFi 계약이 거의 없어 모드를 켜지 않습니다.\n\
             /setup에서 바이낸스 메인넷으로 바꾼 뒤 다시 선택하세요.",
            network_label(network)
        )
    }

    // ------------------------------------------------------ action handler (ported verbatim)

    pub async fn handle_btc_ma_cross_action(&self, action: &str) -> (String, Value) {
        let cfg = self.config().await;
        let network = match self.engine_call("current_network", json!({})).await {
            Ok(Value::String(n)) => n,
            Ok(_) => {
                let status = self.format_status().await;
                let state = Value::Object(Map::new());
                return (status, self.build_keyboard(&cfg, None, Some(&state)));
            }
            Err(e) => {
                let text = Self::engine_error_text(&e);
                return (text, self.build_keyboard(&cfg, None, None));
            }
        };
        let state = self.load_state_or_empty().await;
        let holding = py_truthy(state.get("trade"));

        macro_rules! kb {
            () => {
                self.build_keyboard(&self.config().await, None, Some(&state))
            };
            ($confirm:expr) => {
                self.build_keyboard(&self.config().await, Some($confirm), Some(&state))
            };
        }
        macro_rules! kb_owned {
            () => {{
                let cfg_now = self.config().await;
                self.build_keyboard(&cfg_now, None, Some(&state))
            }};
            ($confirm:expr) => {{
                let cfg_now = self.config().await;
                self.build_keyboard(&cfg_now, Some($confirm), Some(&state))
            }};
        }

        match action {
            "on" => {
                let mode = cfg_str(&cfg, "trading_mode", "");
                let mode_now = self
                    .engine_call("mode", json!({}))
                    .await
                    .ok()
                    .and_then(|v| v.as_str().map(|s| s.to_string()))
                    .unwrap_or_else(|| mode.to_string());
                let entry = if cfg_str(&cfg, "entry_mode", "") == "trend" {
                    "방향 유지 중 진입"
                } else {
                    "돌파 시 진입"
                };
                let body = format!(
                    "⚠️ 3/200 SMA 크로스 전략을 켤까요?\n\
                     모드 {mode_now} · {} (/setup) · {}\n\
                     종목 {} · {entry}\n\
                     켜면 메인 선물 자동매매와 BTC 눌림목 전략의 신규 진입이 멈춥니다.\n{}",
                    network_label(&network),
                    timeframe_labels(cfg_str(&cfg, "timeframe", "")),
                    scope_text(&cfg),
                    if mode_now == "LIVE" {
                        "실제 주문이 나갑니다."
                    } else {
                        "DRY_RUN: 실제 주문 없이 가상 체결만 기록합니다."
                    }
                );
                (body, kb!(("✅ 전략 시작", "bm:confirm_on")))
            }
            "confirm_on" => {
                if let Err(e) = self.set_coin_value("enabled", json!(true)).await {
                    return (Self::engine_error_text(&e), kb!());
                }
                if let Err(e) = self
                    .engine_call("turn_off_others", json!({ "except": CONFIG_KEY }))
                    .await
                {
                    log_line(&format!("telegram: turn_off_others failed: {e}"));
                }
                let note =
                    "\n메인 선물 자동매매 신규 진입을 일시정지했습니다 (보유 포지션 관리는 계속).";
                let status = self.format_status().await;
                (format!("✅ 3/200 SMA 전략 ON{note}\n\n{status}"), kb!())
            }
            "off" => {
                if let Err(e) = self.set_coin_value("enabled", json!(false)).await {
                    return (Self::engine_error_text(&e), kb!());
                }
                let status = self.format_status().await;
                (
                    format!("⏹ 신규 진입 OFF (보유 포지션 관리는 계속)\n\n{status}"),
                    kb!(),
                )
            }
            a if starts_with_any(a, &["mode:", "tf:"]) && holding => (
                "⚠️ 포지션 보유 중에는 모드/봉을 바꿀 수 없습니다. 먼저 정리하세요.".to_string(),
                kb!(),
            ),
            "mode:dry" => {
                if let Err(e) = self.set_coin_value("trading_mode", json!("DRY_RUN")).await {
                    return (Self::engine_error_text(&e), kb!());
                }
                if let Err(e) = self.engine_call("reset_reconciliation", json!({})).await {
                    return (Self::engine_error_text(&e), kb!());
                }
                let status = self.format_status().await;
                (
                    format!("🧪 DRY_RUN으로 전환 (실제 주문 없음)\n\n{status}"),
                    kb!(),
                )
            }
            "mode:live" => {
                let body = format!(
                    "⚠️ LIVE로 전환할까요? 실제 주문이 나갑니다.\n\
                     거래소: {} (/setup)\n\
                     환경변수 TRADING_MODE=DRY_RUN 이 설정돼 있으면 계속 DRY_RUN으로 동작합니다.",
                    network_label(&network)
                );
                (body, kb!(("✅ LIVE 전환", "bm:confirm_live")))
            }
            "confirm_live" => {
                if let Err(e) = self.set_coin_value("trading_mode", json!("LIVE")).await {
                    return (Self::engine_error_text(&e), kb!());
                }
                if let Err(e) = self.engine_call("reset_reconciliation", json!({})).await {
                    return (Self::engine_error_text(&e), kb!());
                }
                let status = self.format_status().await;
                (
                    format!(
                        "💥 LIVE 전환 완료 (다음 주기에 거래소 상태를 먼저 대조합니다)\n\n{status}"
                    ),
                    kb!(),
                )
            }
            "pick" | "scope:single" => {
                let pick = self.pick_keyboard(&network).await;
                let body = format!(
                    "🎯 거래할 코인 1개를 고르세요 (거래대금 상위 순).\n\
                     목록에 없는 코인은 /macoin SOL 처럼 입력하면 됩니다.\n\
                     지금: {}",
                    scope_text(&cfg)
                );
                (body, pick)
            }
            a if a.starts_with("coin:") => {
                let coin = a.split_once(':').map(|x| x.1).unwrap_or("");
                let (_, message) = self.set_coin(&network, coin).await;
                let status = self.format_status().await;
                (format!("{message}\n\n{status}"), kb!())
            }
            a if a.starts_with("scope:") => {
                let scope = a.split_once(':').map(|x| x.1).unwrap_or("");
                if scope == TRADFI_SCOPE {
                    let allowed = match self
                        .engine_call("tradfi_network_allowed", json!({ "network": network }))
                        .await
                    {
                        Ok(v) => py_truthy(Some(&v)),
                        Err(e) => return (Self::engine_error_text(&e), kb!()),
                    };
                    let refusal = self.tradfi_scope_refusal(allowed, &network);
                    if !refusal.is_empty() {
                        return (refusal, kb!());
                    }
                    if let Err(e) = self.set_coin_value("scan_scope", json!(TRADFI_SCOPE)).await {
                        return (Self::engine_error_text(&e), kb!());
                    }
                    let note = if holding {
                        "\n(보유 중인 포지션은 그대로 관리, 다음 진입부터 적용)"
                    } else {
                        ""
                    };
                    let session = match self.engine_call("us_session_status", json!({})).await {
                        Ok(v) => v,
                        Err(e) => return (Self::engine_error_text(&e), kb!()),
                    };
                    let cfg_now = self.config().await;
                    let status = self.format_status().await;
                    let body = format!(
                        "🏦 TradFi만 선택: {}\n\
                         • 미국 정규장에서만 신규 진입합니다. 지금: {}\n\
                         • 장 마감·주말·휴장에는 새 진입을 하지 않고, 보유 포지션의 비상손절·수익 확보·\
                         반대 크로스 청산은 계속 동작합니다.{note}\n\n{status}",
                        scope_text(&cfg_now),
                        us_session_text(&session)
                    );
                    return (body, kb!());
                }
                if scope == "btc" || scope == "alts" {
                    if let Err(e) = self.set_coin_value("scan_scope", json!(scope)).await {
                        return (Self::engine_error_text(&e), kb!());
                    }
                }
                let note = if holding {
                    "\n(보유 중인 포지션은 그대로 관리, 다음 진입부터 적용)"
                } else {
                    ""
                };
                let cfg_now = self.config().await;
                let status = self.format_status().await;
                (
                    format!("🔎 종목: {}{note}\n\n{status}", scope_text(&cfg_now)),
                    kb!(),
                )
            }
            a if a.starts_with("lev:") => {
                let choice = a
                    .split_once(':')
                    .map(|x| x.1)
                    .and_then(|s| s.parse::<i64>().ok())
                    .unwrap_or(-1);
                if LEVERAGE_CHOICES.contains(&choice) {
                    if let Err(e) = self.set_coin_value("leverage", json!(choice)).await {
                        return (Self::engine_error_text(&e), kb!());
                    }
                }
                let cfg_now = self.config().await;
                let leverage = cfg_int(&cfg_now, "leverage", 5);
                let margin = py_n(cfg_get(&cfg_now, "margin_fraction")) * 100.0;
                let status = self.format_status().await;
                (
                    format!(
                        "⚙️ 레버리지: {leverage}x ISOLATED\n\
                         (보유 중인 포지션은 기존 레버리지 유지, 다음 진입부터 적용)\n\
                         증거금 비율은 가용 잔고의 {margin:.0}% 그대로이고, \
                         수익 확보·비상손절은 증거금 기준 %로 계산되므로 레버리지를 바꾸면 같은 %의 가격거리가 좁아집니다.\n\n{status}"
                    ),
                    kb!(),
                )
            }
            a if a.starts_with("entry:") => {
                let entry_mode = a.split_once(':').map(|x| x.1).unwrap_or("");
                if entry_mode == "cross" || entry_mode == "trend" {
                    if let Err(e) = self.set_coin_value("entry_mode", json!(entry_mode)).await {
                        return (Self::engine_error_text(&e), kb!());
                    }
                    if entry_mode == "trend" {
                        if let Err(e) = self.reset_counters(&network).await {
                            return (Self::engine_error_text(&e), kb!());
                        }
                    }
                }
                let cfg_now = self.config().await;
                let status = self.format_status().await;
                (
                    format!("🎯 진입: {}\n\n{status}", entry_mode_text(&cfg_now)),
                    kb_owned!(),
                )
            }
            "pls:on" => {
                if let Err(e) = self
                    .set_coin_value(
                        "profit_lock_switch_count",
                        json!(DEFAULT_PROFIT_LOCK_SWITCH_COUNT),
                    )
                    .await
                {
                    return (Self::engine_error_text(&e), kb!());
                }
                let status = self.format_status().await;
                (
                    format!(
                        "🔁 익절 자동 전환 ON: 수익 확보 익절 {DEFAULT_PROFIT_LOCK_SWITCH_COUNT}회마다 \
                         다음 진입부터 '돌파 시 진입'으로 강제합니다.\n\
                         (강제 전환 뒤 카운트는 0부터 다시 셉니다. '방향 유지 중 진입'을 직접 누르면 강제가 해제됩니다.)\n\n{status}"
                    ),
                    kb!(),
                )
            }
            "pls:off" => {
                if let Err(e) = self
                    .set_coin_value("profit_lock_switch_count", json!(0))
                    .await
                {
                    return (Self::engine_error_text(&e), kb!());
                }
                let status = self.format_status().await;
                (
                    format!(
                        "🔁 익절 자동 전환 OFF: 익절 횟수와 상관없이 선택한 진입 모드를 유지합니다.\n\n{status}"
                    ),
                    kb!(),
                )
            }
            a if a.starts_with("rot:") => {
                let on = a.split_once(':').map(|x| x.1).unwrap_or("") == "on";
                if let Err(e) = self.set_coin_value("rotate_after_exit", json!(on)).await {
                    return (Self::engine_error_text(&e), kb!());
                }
                let cfg_now = self.config().await;
                let status = self.format_status().await;
                (
                    format!(
                        "🔁 청산 후 다른 코인 전환: {}\n\n{status}",
                        rotation_text(&cfg_now)
                    ),
                    kb_owned!(),
                )
            }
            a if a.starts_with("rw:") => {
                let choice = a
                    .split_once(':')
                    .map(|x| x.1)
                    .and_then(|s| s.parse::<i64>().ok())
                    .unwrap_or(-1);
                if REENTRY_WAIT_CHOICES.contains(&choice) {
                    if let Err(e) = self
                        .set_coin_value("reentry_wait_bars", json!(choice))
                        .await
                    {
                        return (Self::engine_error_text(&e), kb!());
                    }
                }
                let cfg_now = self.config().await;
                let status = self.format_status().await;
                (
                    format!(
                        "⏳ 익절·손절 후 재진입: {}\n\n{status}",
                        reentry_text(&cfg_now)
                    ),
                    kb_owned!(),
                )
            }
            a if a.starts_with("slow:") => {
                let slow = a
                    .split_once(':')
                    .map(|x| x.1)
                    .and_then(|s| s.parse::<i64>().ok())
                    .unwrap_or(-1);
                if SLOW_PERIOD_CHOICES.contains(&slow) {
                    if let Err(e) = self.set_coin_value("slow_period", json!(slow)).await {
                        return (Self::engine_error_text(&e), kb!());
                    }
                }
                let cfg_now = self.config().await;
                let slow_now = cfg_int(&cfg_now, "slow_period", 200);
                let note = if holding {
                    format!(
                        "\n보유 중 포지션도 다음 봉 마감부터 SMA3/SMA{slow_now} 기준으로 관리합니다. \
                         그때 SMA3이 반대편에 있으면 반대 방향으로 전환합니다."
                    )
                } else {
                    String::new()
                };
                let status = self.format_status().await;
                (
                    format!("📐 기준선: SMA3 / SMA{slow_now}{note}\n\n{status}"),
                    kb!(),
                )
            }
            a if a.starts_with("tf:") => {
                let timeframe = a.split_once(':').map(|x| x.1).unwrap_or("");
                if TIMEFRAMES.contains(&timeframe) {
                    if let Err(e) = self.set_coin_value("timeframe", json!(timeframe)).await {
                        return (Self::engine_error_text(&e), kb!());
                    }
                }
                let label = timeframe_labels(timeframe);
                let status = self.format_status().await;
                (format!("🕒 봉: {label}\n\n{status}"), kb!())
            }
            a if a.starts_with("es:") => {
                let choice = a
                    .split_once(':')
                    .map(|x| x.1)
                    .and_then(|s| s.parse::<i64>().ok())
                    .unwrap_or(-1);
                if EMERGENCY_STOP_CHOICES.contains(&choice) {
                    if let Err(e) = self
                        .set_coin_value("emergency_stop_roi_percent", json!(choice))
                        .await
                    {
                        return (Self::engine_error_text(&e), kb!());
                    }
                }
                let note = if holding {
                    "\n(보유 중인 포지션은 기존 비상손절 유지, 다음 진입부터 적용)"
                } else {
                    ""
                };
                let label = if choice == 0 {
                    "OFF".to_string()
                } else {
                    format!("증거금 -{choice}%")
                };
                let status = self.format_status().await;
                (format!("🛑 비상손절: {label}{note}\n\n{status}"), kb!())
            }
            "stats" => (self.format_stats().await, kb!()),
            "sma_help" => {
                let current = periods_label(
                    cfg_int(&cfg, "fast_period", 3),
                    cfg_int(&cfg, "slow_period", 200),
                );
                let presets = PERIOD_PRESETS
                    .iter()
                    .map(|(label, _, _)| *label)
                    .collect::<Vec<_>>()
                    .join(" · ");
                let body = format!(
                    "✏️ SMA 기간 직접 입력\n지금: {current}\n\n\
                     명령: /btcma <패스트> <슬로우>\n\
                     예) /btcma 1 200 → 패스트 1, 슬로우 200\n\
                     예) /btcma 5 60 → 패스트 5, 슬로우 60\n\
                     예) /btcma 9/21 형식도 가능\n\n\
                     버튼 프리셋: {presets}\n\
                     • 패스트는 슬로우보다 작아야 합니다 (1 이상, 슬로우 500 이하).\n\
                     • 보유 중에 바꾸면 다음 진입부터 적용되고, \
                     보유 포지션은 진입 당시 기간으로 계속 관리됩니다."
                );
                (body, kb!())
            }
            a if a.starts_with("sma:") => {
                let parts: Vec<&str> = a.split(':').collect();
                if parts.len() != 3 {
                    return ("⚠️ 알 수 없는 SMA 설정입니다.".to_string(), kb!());
                }
                let fast: i64 = parts[1].parse().unwrap_or(-1);
                let slow: i64 = parts[2].parse().unwrap_or(-1);
                let (_, message) = self.set_periods(fast, slow).await;
                let status = self.format_status().await;
                (format!("{message}\n\n{status}"), kb!())
            }
            "signals" => (self.format_signals().await, kb!()),
            "rules" => (self.rules_text(&cfg), kb!()),
            "reconcile" => {
                if let Err(e) = self.engine_call("reset_reconciliation", json!({})).await {
                    return (Self::engine_error_text(&e), kb!());
                }
                let result = match self.engine_call("run_cycle", json!({})).await {
                    Ok(v) => v,
                    Err(e) => return (Self::engine_error_text(&e), kb!()),
                };
                let action = result.get("action").map(value_display).unwrap_or_default();
                let reason = result.get("reason").map(value_display).unwrap_or_default();
                let status = self.format_status().await;
                (format!("🔄 재대조: {action} {reason}\n\n{status}"), kb!())
            }
            "close" => {
                if !holding {
                    let status = self.format_status().await;
                    return (
                        format!("보유 중인 이 전략의 포지션이 없습니다.\n\n{status}"),
                        kb!(),
                    );
                }
                let state_now = self.load_state_or_empty().await;
                let held = state_now
                    .get("trade")
                    .cloned()
                    .unwrap_or(Value::Object(Map::new()));
                let market_id = held
                    .get("market_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("BTCUSDT");
                (
                    format!("⚠️ 이 전략의 {market_id} 포지션을 시장가(reduce-only)로 청산할까요?"),
                    kb!(("✅ 청산 확인", "bm:confirm_close")),
                )
            }
            "confirm_close" => {
                let result = match self
                    .engine_call("close_position", json!({ "reason": "manual" }))
                    .await
                {
                    Ok(v) => v,
                    Err(e) => return (Self::engine_error_text(&e), kb!()),
                };
                let action = result.get("action").map(value_display).unwrap_or_default();
                let reason = result.get("reason").map(value_display).unwrap_or_default();
                let status = self.format_status().await;
                (
                    format!("🔻 청산 요청: {action} {reason}\n\n{status}"),
                    kb!(),
                )
            }
            _ => {
                let status = self.format_status().await;
                (status, kb!())
            }
        }
    }
}

/// Print a JSON value the way Python `str()`/embedded `f"{x}"` would.
fn value_display(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".to_string(),
        Value::Bool(b) => {
            if *b {
                "True".to_string()
            } else {
                "False".to_string()
            }
        }
        other => other.to_string(),
    }
}

/// Parse `text` into (command, args) the way `CommandHandler` would.
fn parse_command(text: &str) -> (String, Vec<String>) {
    let trimmed = text.trim();
    if !trimmed.starts_with('/') {
        return (String::new(), Vec::new());
    }
    let mut parts = trimmed[1..].split_whitespace();
    let head = parts.next().unwrap_or("");
    let command = head.split('@').next().unwrap_or("").to_lowercase();
    let args = parts.map(|s| s.to_string()).collect();
    (command, args)
}

fn log_line(msg: &str) {
    use std::io::Write as _;
    let line = format!(
        "{} {msg}\n",
        chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, false)
    );
    // logging must never crash the bot
    let _ = std::io::stderr().write_all(line.as_bytes());
}

#[allow(dead_code)]
fn _unused_default_period_check(cfg: &Value) -> bool {
    // Kept for parity with `is_default_periods` import; no behaviour.
    is_default_periods(
        cfg_int(cfg, "fast_period", 3),
        cfg_int(cfg, "slow_period", 200),
        3,
        200,
    )
}
