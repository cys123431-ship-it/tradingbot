//! Telegram `/tradfi` menu — Binance TradFi-only SMA 1/5/120 alignment
//! strategy (gold XAUUSDT, oil CLUSDT, US equities...). Every service call and
//! config change goes through the engine IPC with strategy "tradfi".
#![allow(clippy::too_many_arguments)]

use serde_json::{json, Map, Value};

use crate::app::ipc::EngineApi;
use crate::app::telegram_bot::{cfg_get, format_g, markup_button, py_n, Outgoing, TelegramBot};

const CONFIG_KEY: &str = "tradfi_ma";
const STRATEGY: &str = "tradfi";
pub const EXIT_MA_CHOICES: &[i64] = &[20, 30, 50];

const MAINNET_LINE: &str =
    "⚠️ TradFi 종목은 바이낸스 메인넷에서만 거래됩니다. /exchange 에서 메인넷으로 바꾸세요.";

const HOLDING_BLOCK: &str = "⚠️ 포지션 보유 중에는 설정을 바꿀 수 없습니다. 먼저 정리하세요.";

fn mark(selected: bool, label: &str) -> String {
    if selected {
        format!("✅ {label}")
    } else {
        label.to_string()
    }
}

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

fn cfg_str<'a>(cfg: &'a Value, key: &str, default: &'a str) -> &'a str {
    cfg_get(cfg, key)
        .and_then(|v| v.as_str())
        .unwrap_or(default)
}

fn cfg_int_of(cfg: &Value, key: &str) -> i64 {
    cfg_get(cfg, key)
        .map(|v| crate::app::telegram_bot::py_int_of(Some(v)))
        .unwrap_or(0)
}

fn current_coin(cfg: &Value) -> String {
    cfg_get(cfg, "single_symbol")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .unwrap_or("XAUUSDT")
        .to_string()
}

fn entry_mode_label(mode: &str) -> String {
    if mode == "trend" {
        "배열 유지 중".to_string()
    } else {
        "새 배열만".to_string()
    }
}

fn exit_ma_of(cfg: &Value) -> i64 {
    match cfg_int_of(cfg, "exit_ma_period") {
        0 => 30,
        n => n,
    }
}

fn network_of(status: &Value) -> String {
    status
        .get("network")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string()
}

fn engine_error_text(e: &str) -> String {
    format!("⚠️ 엔진 응답 없음: {e}")
}

fn holding(status: &Value) -> bool {
    py_truthy(status.get("state").and_then(|s| s.get("trade")))
}

/// Lenient ledger event text: kind + message/detail.
fn event_text(v: &Value) -> Option<String> {
    if !py_truthy(Some(v)) {
        return None;
    }
    let mut out = String::new();
    if let Some(kind) = v.get("kind").and_then(|k| k.as_str()) {
        if !kind.is_empty() {
            out.push_str(kind);
        }
    }
    let mut found = false;
    for key in ["message", "detail"] {
        if let Some(seg) = v.get(key) {
            let text = value_display(seg);
            if !text.is_empty() && text != "None" {
                if !out.is_empty() {
                    out.push_str(": ");
                }
                out.push_str(&text);
                found = true;
                break;
            }
        }
    }
    if out.is_empty() && !found {
        return None;
    }
    for key in ["ts", "created_at"] {
        if let Some(seg) = v.get(key) {
            let text = value_display(seg);
            if !text.is_empty() && text != "None" {
                let short: String = text.chars().take(19).collect();
                return Some(format!("{out} ({short})"));
            }
        }
    }
    Some(out)
}

fn trade_text(trade: &Value) -> String {
    let side = trade
        .get("side")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let qty = format_g(py_n(trade.get("quantity")));
    let entry = format_g(py_n(trade.get("entry_price")));
    let market = trade
        .get("market_id")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    if market.is_empty() {
        format!("{side} {qty} @ {entry}")
    } else {
        format!("{side} {qty} @ {entry} ({market})")
    }
}

/// Status text for `/tradfi`, built from the engine `status` result.
pub fn tradfi_status_text(status: &Value) -> String {
    if py_truthy(status.get("unsupported")) {
        return "🏦 TradFi 정배열 전략 (/tradfi)\n지금 거래소가 업비트라 사용할 수 없습니다. /exchange 에서 바이낸스 메인넷을 선택하세요.".to_string();
    }
    let cfg = status
        .get("config")
        .cloned()
        .unwrap_or_else(|| Value::Object(Map::new()));
    let enabled = py_truthy(cfg_get(&cfg, "enabled"));
    let trading_mode = cfg_str(&cfg, "trading_mode", "DRY_RUN");
    let mode_label = if trading_mode == "LIVE" {
        "LIVE".to_string()
    } else {
        "DRY_RUN (DRY_RUN = 모의매매, 실제 주문 없음)".to_string()
    };
    let network = network_of(status);
    let network_label = if network == "mainnet" {
        "메인넷"
    } else if network == "testnet" {
        "테스트넷"
    } else {
        "업비트"
    };
    let has_credentials = py_truthy(status.get("has_credentials"));
    let coin = current_coin(&cfg);
    let entry = cfg_str(&cfg, "entry_mode", "cross");
    let exit_ma = exit_ma_of(&cfg);
    let leverage = match cfg_int_of(&cfg, "leverage") {
        0 => 5,
        n => n,
    };
    let margin_pct = py_n(cfg_get(&cfg, "margin_fraction")) * 100.0;
    let stop = py_n(cfg_get(&cfg, "emergency_stop_roi_percent"));
    let stop_text = if stop <= 0.0 {
        "끔".to_string()
    } else {
        format!("증거금 -{}%", format_g(stop))
    };

    let mut lines = vec![
        "🏦 TradFi 정배열 전략 (/tradfi)".to_string(),
        format!(
            "전략: {} · 모드: {mode_label}",
            if enabled { "ON" } else { "OFF" }
        ),
        format!(
            "거래소: {network_label} · API 키: {}",
            if has_credentials { "있음" } else { "없음" }
        ),
        format!("종목: {coin} (TradFi 전용 · 순수 코인 불가) · 4시간봉 마감 기준"),
        format!(
            "진입: SMA 1·5·120 정배열 롱 / 역배열 숏 — {}",
            entry_mode_label(entry)
        ),
        format!("청산: 종가가 {exit_ma}MA 반대편으로 마감하면 청산"),
        format!(
            "레버리지 {leverage}x ISOLATED 고정 · 증거금: 가용 잔고의 {:.0}% 고정 · 비상손절: {stop_text}",
            margin_pct
        ),
    ];
    let trade = status.get("state").and_then(|s| s.get("trade"));
    if py_truthy(trade) {
        if let Some(t) = trade {
            lines.push(format!("보유: {}", trade_text(t)));
        }
    } else {
        lines.push("보유: 없음".to_string());
    }
    match status.get("last_signal").and_then(event_text) {
        Some(t) => lines.push(format!("최근 신호: {t}")),
        None => lines.push("최근 신호: 없음".to_string()),
    }
    match status.get("last_skip").and_then(event_text) {
        Some(t) => lines.push(format!("최근 판단: {t}")),
        None => lines.push("최근 판단: 없음".to_string()),
    }
    let stats = status.get("stats");
    let total = stats
        .and_then(|s| s.get("total_trades"))
        .map(|v| crate::app::telegram_bot::py_int_of(Some(v)))
        .unwrap_or(0);
    let win_rate = stats
        .and_then(|s| s.get("win_rate"))
        .map(|v| py_n(Some(v)))
        .unwrap_or(0.0);
    let net = stats
        .and_then(|s| s.get("net_pnl"))
        .map(|v| py_n(Some(v)))
        .unwrap_or(0.0);
    let sign = if net > 0.0 { "+" } else { "" };
    lines.push(format!(
        "누적: 거래 {total}회 · 승률 {:.1}% · 손익 {sign}{:.2} USDT",
        win_rate * 100.0,
        net
    ));
    if network != "mainnet" {
        lines.push(MAINNET_LINE.to_string());
    }
    if enabled {
        lines.push(
            "이 전략이 켜져 있으면 /btcmacross·/macross 신규 진입과 옵션 신규 진입이 멈춥니다."
                .to_string(),
        );
    }
    lines.join("\n")
}

/// Status text for `/tradfi`, built from the engine `status` result.
async fn fetch_status<A: EngineApi + 'static>(bot: &TelegramBot<A>) -> Value {
    match bot.engine.call("status", Some(STRATEGY), json!({})).await {
        Ok(v @ Value::Object(_)) => v,
        Ok(_) => Value::Object(Map::new()),
        Err(e) => {
            log_line(&format!("telegram: tradfi status failed: {e}"));
            json!({"error": e})
        }
    }
}

async fn fetch_config<A: EngineApi + 'static>(bot: &TelegramBot<A>) -> Value {
    match bot
        .engine
        .call("config_get", Some(STRATEGY), json!({}))
        .await
    {
        Ok(v @ Value::Object(_)) => v,
        Ok(_) => Value::Object(Map::new()),
        Err(e) => {
            log_line(&format!("telegram: tradfi config_get failed: {e}"));
            Value::Object(Map::new())
        }
    }
}

/// Keyboard for `/tradfi` (callbacks use the `tf:` prefix).
pub fn tradfi_keyboard(cfg: &Value, confirm: Option<(&str, &str)>) -> Value {
    let enabled = py_truthy(cfg_get(cfg, "enabled"));
    let trading_mode = cfg_str(cfg, "trading_mode", "DRY_RUN");
    let entry_mode = cfg_str(cfg, "entry_mode", "cross");
    let exit_ma = exit_ma_of(cfg);

    let mut rows: Vec<Vec<Value>> = Vec::new();
    rows.push(vec![
        markup_button(mark(enabled, "▶️ 켜기"), "tf:on"),
        markup_button(mark(!enabled, "⏹ 신규진입 OFF"), "tf:off"),
    ]);
    rows.push(vec![
        markup_button(
            mark(trading_mode == "DRY_RUN", "🧪 모의매매"),
            "tf:mode:dry",
        ),
        markup_button(mark(trading_mode == "LIVE", "💵 실전"), "tf:mode:live"),
    ]);
    rows.push(vec![
        markup_button(mark(entry_mode == "cross", "새 배열만"), "tf:entry:cross"),
        markup_button(
            mark(entry_mode == "trend", "배열 유지 중"),
            "tf:entry:trend",
        ),
    ]);
    let exit_buttons: Vec<Value> = EXIT_MA_CHOICES
        .iter()
        .map(|n| {
            markup_button(
                mark(exit_ma == *n, &format!("청산 {n}MA")),
                format!("tf:exit:{n}"),
            )
        })
        .collect();
    rows.push(exit_buttons);
    rows.push(vec![
        markup_button("🪙 종목 변경", "tf:coin"),
        markup_button("📊 새로고침", "tf:status"),
    ]);
    rows.push(vec![
        markup_button("📘 규칙", "tf:rules"),
        markup_button("🛑 완전 끄기", "tf:full_off"),
    ]);
    if let Some((confirm_text, confirm_cb)) = confirm {
        rows.insert(
            0,
            vec![
                markup_button(confirm_text, confirm_cb),
                markup_button("취소", "tf:status"),
            ],
        );
    }
    json!({"inline_keyboard": rows})
}

/// Korean error text for a failed `resolve_tradfi_any` call.
pub fn tradfi_coin_error_text(error: &str, text: &str) -> String {
    if error.contains("NOT_TRADFI") {
        return format!(
            "❌ {text}은(는) TradFi 종목이 아닙니다. 이 전략은 TradFi(금·원유·미국주식 등)만 거래합니다."
        );
    }
    if error.contains("NOT_TRADING") {
        return format!("❌ {text}은(는) 지금 거래 중인 종목이 아닙니다.");
    }
    if error.contains("SYMBOL_EMPTY") {
        return "❌ 종목을 입력하세요. 예: /tfcoin XAU".to_string();
    }
    if error.contains("메인넷") || error.contains("MAINNET") {
        return "❌ TradFi 종목은 바이낸스 메인넷에서만 지정할 수 있습니다. /exchange 에서 메인넷으로 바꾸세요.".to_string();
    }
    if error.contains("Unknown") || error.contains("NOT_FOUND") || error.contains("not found") {
        return format!("❌ {text} 종목을 찾지 못했습니다.");
    }
    format!("❌ 종목 지정 실패: {error}")
}

impl<A: EngineApi + 'static> TelegramBot<A> {
    async fn tf_call(&self, cmd: &str, args: Value) -> Result<Value, String> {
        self.engine.call(cmd, Some(STRATEGY), args).await
    }

    async fn tf_set(&self, key: &str, value: Value) -> Result<(), String> {
        let mut changes = Map::new();
        changes.insert(key.to_string(), value);
        self.tf_call("config_set", json!({ "changes": changes }))
            .await
            .map(|_| ())
    }

    async fn tf_network(&self) -> Option<String> {
        match self.tf_call("current_network", json!({})).await {
            Ok(Value::String(n)) => Some(n),
            _ => None,
        }
    }

    async fn tf_mainnet_check(&self) -> bool {
        self.tf_network().await.as_deref() == Some("mainnet")
    }

    async fn tf_reset_reconciliation(&self) {
        if let Err(e) = self.tf_call("reset_reconciliation", json!({})).await {
            log_line(&format!("telegram: reset_reconciliation failed: {e}"));
        }
    }

    async fn tf_status_text(&self) -> String {
        let status = fetch_status(self).await;
        if let Some(err) = status.get("error").and_then(|v| v.as_str()) {
            return engine_error_text(err);
        }
        tradfi_status_text(&status)
    }

    async fn tf_keyboard(&self) -> Value {
        let cfg = fetch_config(self).await;
        tradfi_keyboard(&cfg, None)
    }

    /// Full stop: disable new entries then close this strategy's position.
    async fn tf_full_off(&self) -> String {
        if let Err(e) = self.tf_set("enabled", json!(false)).await {
            return engine_error_text(&e);
        }
        let mut lines = vec!["🛑 /tradfi 완전 끄기".to_string()];
        match self
            .tf_call("close_position", json!({ "reason": "manual" }))
            .await
        {
            Ok(v) => {
                let action = v.get("action").map(value_display).unwrap_or_default();
                let reason = v.get("reason").map(value_display).unwrap_or_default();
                if reason == "NO_POSITION" {
                    lines.push("· 청산: 보유 포지션 없음".to_string());
                } else {
                    lines.push(format!("· 청산 요청: {action} {reason}"));
                }
            }
            Err(e) => lines.push(format!("· ⚠️ 청산 실패: {e}")),
        }
        let status = self.tf_status_text().await;
        format!("{}\n\n{status}", lines.join("\n"))
    }

    /// Coin selection screen (`tf:coin` / `/tfcoin` without args).
    async fn tf_coin_screen(&self) -> (String, Value) {
        let cfg = fetch_config(self).await;
        let coin = current_coin(&cfg);
        let head = format!(
            "🪙 TradFi 종목 선택 (순수 코인은 불가)\n지금: {coin}\n아래 버튼을 누르거나 /tfcoin <종목> 으로 지정하세요. 예: /tfcoin CL"
        );
        match self.tf_call("tradfi_symbols", json!({})).await {
            Ok(Value::Array(items)) => {
                let symbols: Vec<String> = items
                    .iter()
                    .filter_map(|v| v.as_str().map(|s| s.to_string()))
                    .take(24)
                    .collect();
                let mut rows: Vec<Vec<Value>> = Vec::new();
                for chunk in symbols.chunks(4) {
                    let mut row: Vec<Value> = Vec::new();
                    for sym in chunk {
                        let label = sym.strip_suffix("USDT").unwrap_or(sym).to_string();
                        let selected = coin == *sym;
                        row.push(markup_button(
                            mark(selected, &label),
                            format!("tf:pick:{sym}"),
                        ));
                    }
                    rows.push(row);
                }
                rows.push(vec![markup_button("↩️ 돌아가기", "tf:status")]);
                (head, json!({"inline_keyboard": rows}))
            }
            Ok(_) => (head, tradfi_keyboard(&cfg, None)),
            Err(e) => {
                let text = format!("{head}\n\n{}", engine_error_text(&e));
                (text, tradfi_keyboard(&cfg, None))
            }
        }
    }

    /// Resolve a symbol text through the engine and, on success, store it.
    async fn tf_apply_symbol(&self, text: &str) -> (String, Value) {
        let resolved = self
            .tf_call("resolve_tradfi_any", json!({ "text": text }))
            .await;
        match resolved {
            Ok(v) => {
                let market_id = v
                    .get("market_id")
                    .and_then(|m| m.as_str())
                    .unwrap_or("")
                    .to_string();
                if let Err(e) = self.tf_set("single_symbol", json!(market_id.clone())).await {
                    return (engine_error_text(&e), self.tf_keyboard().await);
                }
                let status = self.tf_status_text().await;
                (
                    format!("🪙 종목: {market_id}\n\n{status}"),
                    self.tf_keyboard().await,
                )
            }
            Err(e) => (tradfi_coin_error_text(&e, text), self.tf_keyboard().await),
        }
    }

    /// `/tradfi` command: (status text, keyboard)
    pub async fn tradfi_screen(&self) -> (String, Value) {
        let status = fetch_status(self).await;
        if let Some(err) = status.get("error").and_then(|v| v.as_str()) {
            let cfg = fetch_config(self).await;
            return (engine_error_text(err), tradfi_keyboard(&cfg, None));
        }
        let text = tradfi_status_text(&status);
        let cfg = status
            .get("config")
            .cloned()
            .unwrap_or_else(|| Value::Object(Map::new()));
        let markup = tradfi_keyboard(&cfg, None);
        (text, markup)
    }

    /// `tf:` callback (data without the prefix) -> (text, keyboard)
    pub async fn handle_tradfi_action(&self, action: &str) -> (String, Value) {
        let cfg = fetch_config(self).await;

        if action == "on" {
            if !self.tf_mainnet_check().await {
                return (MAINNET_LINE.to_string(), tradfi_keyboard(&cfg, None));
            }
            let coin = current_coin(&cfg);
            let mode = cfg_str(&cfg, "trading_mode", "DRY_RUN").to_string();
            let entry = entry_mode_label(cfg_str(&cfg, "entry_mode", "cross"));
            let exit_ma = exit_ma_of(&cfg);
            let body = format!(
                "⚠️ TradFi 정배열 전략을 켤까요?\n종목: {coin} · 모드: {mode} · 진입: {entry} · 청산 {exit_ma}MA\n켜면 /btcmacross·/macross 신규 진입이 OFF 되고 옵션 신규 진입도 멈춥니다 (보유 포지션 관리는 계속)."
            );
            return (
                body,
                tradfi_keyboard(&cfg, Some(("✅ 전략 시작", "tf:confirm_on"))),
            );
        }
        if action == "confirm_on" {
            if !self.tf_mainnet_check().await {
                return (MAINNET_LINE.to_string(), tradfi_keyboard(&cfg, None));
            }
            if let Err(e) = self.tf_set("enabled", json!(true)).await {
                return (engine_error_text(&e), tradfi_keyboard(&cfg, None));
            }
            if let Err(e) = self
                .tf_call("turn_off_others", json!({ "except": CONFIG_KEY }))
                .await
            {
                log_line(&format!("telegram: turn_off_others failed: {e}"));
            }
            let status = self.tf_status_text().await;
            return (
                format!("✅ /tradfi ON\n\n{status}"),
                self.tf_keyboard().await,
            );
        }
        if action == "off" {
            if let Err(e) = self.tf_set("enabled", json!(false)).await {
                return (engine_error_text(&e), tradfi_keyboard(&cfg, None));
            }
            let status = self.tf_status_text().await;
            return (
                format!("⏹ /tradfi 신규 진입 OFF (보유 포지션은 계속 관리)\n\n{status}"),
                self.tf_keyboard().await,
            );
        }
        if action == "full_off" {
            let body = "⚠️ /tradfi를 완전히 끌까요?\n· 신규 진입 OFF\n· 보유 포지션이 있으면 시장가(reduce-only)로 청산"
                .to_string();
            return (
                body,
                tradfi_keyboard(&cfg, Some(("🛑 완전 끄기 확인", "tf:confirm_full_off"))),
            );
        }
        if action == "confirm_full_off" {
            let text = self.tf_full_off().await;
            return (text, self.tf_keyboard().await);
        }
        if action == "mode:dry" {
            let status = fetch_status(self).await;
            if holding(&status) {
                return (HOLDING_BLOCK.to_string(), tradfi_keyboard(&cfg, None));
            }
            if let Err(e) = self.tf_set("trading_mode", json!("DRY_RUN")).await {
                return (engine_error_text(&e), tradfi_keyboard(&cfg, None));
            }
            self.tf_reset_reconciliation().await;
            let status_text = self.tf_status_text().await;
            return (
                format!("🧪 모의매매(DRY_RUN)로 전환 — 실제 주문 없음\n\n{status_text}"),
                self.tf_keyboard().await,
            );
        }
        if action == "mode:live" {
            let status = fetch_status(self).await;
            if holding(&status) {
                return (HOLDING_BLOCK.to_string(), tradfi_keyboard(&cfg, None));
            }
            let body = "⚠️ 실전(LIVE)으로 전환할까요? 실제 주문이 나갑니다.\n레버리지 5x · 증거금 가용 잔고의 50%."
                .to_string();
            return (
                body,
                tradfi_keyboard(&cfg, Some(("✅ LIVE 전환", "tf:confirm_live"))),
            );
        }
        if action == "confirm_live" {
            if holding(&fetch_status(self).await) {
                return (HOLDING_BLOCK.to_string(), tradfi_keyboard(&cfg, None));
            }
            if let Err(e) = self.tf_set("trading_mode", json!("LIVE")).await {
                return (engine_error_text(&e), tradfi_keyboard(&cfg, None));
            }
            self.tf_reset_reconciliation().await;
            let status = self.tf_status_text().await;
            return (
                format!(
                    "💥 LIVE 전환 완료 (다음 주기에 거래소 상태를 먼저 대조합니다)\n\n{status}"
                ),
                self.tf_keyboard().await,
            );
        }
        if let Some(rest) = action.strip_prefix("entry:") {
            let value = rest.split_once(':').map(|x| x.0).unwrap_or(rest);
            if value == "cross" || value == "trend" {
                let status = fetch_status(self).await;
                if holding(&status) {
                    return (HOLDING_BLOCK.to_string(), tradfi_keyboard(&cfg, None));
                }
                if let Err(e) = self.tf_set("entry_mode", json!(value)).await {
                    return (engine_error_text(&e), tradfi_keyboard(&cfg, None));
                }
                let label = entry_mode_label(value);
                let status_text = self.tf_status_text().await;
                return (
                    format!("🎯 진입: {label}\n\n{status_text}"),
                    self.tf_keyboard().await,
                );
            }
            return (self.tf_status_text().await, self.tf_keyboard().await);
        }
        if let Some(rest) = action.strip_prefix("exit:") {
            let period: i64 = rest
                .split_once(':')
                .map(|x| x.0)
                .unwrap_or(rest)
                .trim()
                .parse::<i64>()
                .unwrap_or(-1);
            if EXIT_MA_CHOICES.contains(&period) {
                let status = fetch_status(self).await;
                if holding(&status) {
                    return (HOLDING_BLOCK.to_string(), tradfi_keyboard(&cfg, None));
                }
                if let Err(e) = self.tf_set("exit_ma_period", json!(period)).await {
                    return (engine_error_text(&e), tradfi_keyboard(&cfg, None));
                }
                let status_text = self.tf_status_text().await;
                return (
                    format!("🚪 청산선: {period}MA\n\n{status_text}"),
                    self.tf_keyboard().await,
                );
            }
            return (self.tf_status_text().await, self.tf_keyboard().await);
        }
        if action == "coin" {
            let status = fetch_status(self).await;
            if holding(&status) {
                return (HOLDING_BLOCK.to_string(), tradfi_keyboard(&cfg, None));
            }
            return self.tf_coin_screen().await;
        }
        if let Some(rest) = action.strip_prefix("pick:") {
            let id = rest.split_once(':').map(|x| x.0).unwrap_or(rest);
            let status = fetch_status(self).await;
            if holding(&status) {
                return (HOLDING_BLOCK.to_string(), tradfi_keyboard(&cfg, None));
            }
            return self.tf_apply_symbol(id).await;
        }
        if action == "rules" {
            let text = tradfi_rules_text(&cfg);
            return (text, tradfi_keyboard(&cfg, None));
        }
        (self.tf_status_text().await, self.tf_keyboard().await)
    }

    /// `/tfcoin <종목>` command
    pub async fn handle_tfcoin(&self, chat_id: i64, args: &[String]) -> Vec<Outgoing> {
        if args.is_empty() {
            let (text, markup) = self.tf_coin_screen().await;
            return vec![Outgoing::Send {
                chat_id,
                text,
                markup: Some(markup),
            }];
        }
        let cfg = fetch_config(self).await;
        let joined = args.join("");
        let status = fetch_status(self).await;
        if holding(&status) {
            return vec![Outgoing::Send {
                chat_id,
                text: HOLDING_BLOCK.to_string(),
                markup: Some(tradfi_keyboard(&cfg, None)),
            }];
        }
        let resolved = self
            .tf_call("resolve_tradfi_any", json!({ "text": joined }))
            .await;
        match resolved {
            Ok(v) => {
                let market_id = v
                    .get("market_id")
                    .and_then(|m| m.as_str())
                    .unwrap_or("")
                    .to_string();
                let text =
                    if let Err(e) = self.tf_set("single_symbol", json!(market_id.clone())).await {
                        engine_error_text(&e)
                    } else {
                        let status_text = tradfi_status_text(&fetch_status(self).await);
                        format!("🪙 종목: {market_id}\n\n{status_text}")
                    };
                let cfg_now = fetch_config(self).await;
                vec![Outgoing::Send {
                    chat_id,
                    text,
                    markup: Some(tradfi_keyboard(&cfg_now, None)),
                }]
            }
            Err(e) => vec![Outgoing::Send {
                chat_id,
                text: tradfi_coin_error_text(&e, &joined),
                markup: Some(tradfi_keyboard(&cfg, None)),
            }],
        }
    }
}

fn tradfi_rules_text(cfg: &Value) -> String {
    let coin = current_coin(cfg);
    let exit_ma = exit_ma_of(cfg);
    let margin_pct = py_n(cfg_get(cfg, "margin_fraction")) * 100.0;
    let margin_text = if margin_pct == 0.0 {
        "50".to_string()
    } else {
        format_g(margin_pct)
    };
    [
        "📘 /tradfi 규칙 (TradFi 정배열)".to_string(),
        format!("· 종목: {coin} 외 바이낸스 TradFi 무기한 (금·원유·미국주식 등) · 순수 코인은 입력해도 거부됩니다"),
        "· 거래소: 바이낸스 메인넷 전용 (테스트넷·업비트 불가)".to_string(),
        "· 봉: 4시간봉 마감 기준".to_string(),
        String::new(),
        "진입".to_string(),
        "· 롱: 종가(=SMA1) > SMA5 > SMA120 정배열이고 종가가 청산선 위".to_string(),
        "· 숏: 종가(=SMA1) < SMA5 < SMA120 역배열이고 종가가 청산선 아래".to_string(),
        "· 진입 방식: 새 배열만(배열이 새로 만들어진 봉에서만) 또는 배열 유지 중(배열이 유지되는 동안 언제든, 청산 후 1봉 대기)".to_string(),
        String::new(),
        "청산".to_string(),
        format!("· 종가가 {exit_ma}MA 반대편으로 마감하면 청산"),
        String::new(),
        "고정 설정".to_string(),
        format!("· 레버리지 5x ISOLATED · 증거금 가용 잔고의 {margin_text}%"),
        "· 비상손절: 증거금 -50% (Mark 기준, 거래소 주문)".to_string(),
        "· 수익잠금·SMA25 손절·물타기·일일손실/연패 가드 없음".to_string(),
        String::new(),
        "참고: 이 전략은 다른 선물 전략과 동시에 켜지지 않습니다. 켜면 /btcmacross·/macross 신규 진입이 OFF 되고 옵션 신규 진입도 멈춥니다.".to_string(),
    ]
    .join("\n")
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    use crate::app::ipc::BoxFut;
    use crate::app::telegram_api::TelegramApi;

    struct Fake {
        calls: Mutex<Vec<(String, Value)>>,
        config: Mutex<Value>,
        network: Mutex<Value>,
        trade: Mutex<Value>,
        resolve: Mutex<Result<Value, String>>,
    }

    struct FakeEngine(Arc<Fake>);

    impl Fake {
        fn new() -> Arc<Self> {
            Arc::new(Fake {
                calls: Mutex::new(Vec::new()),
                config: Mutex::new(json!({
                    "enabled": false, "trading_mode": "DRY_RUN", "single_symbol": "XAUUSDT",
                    "entry_mode": "cross", "exit_ma_period": 30, "leverage": 5,
                    "margin_fraction": "0.5", "timeframe": "4h",
                })),
                network: Mutex::new(json!("mainnet")),
                trade: Mutex::new(Value::Null),
                resolve: Mutex::new(Ok(json!({"market_id": "XAUUSDT"}))),
            })
        }
        fn sets(&self) -> Vec<Value> {
            self.calls
                .lock()
                .unwrap()
                .iter()
                .filter(|(c, _)| c == "config_set")
                .map(|(_, a)| a["changes"].clone())
                .collect()
        }
        fn count(&self, cmd: &str) -> usize {
            self.calls
                .lock()
                .unwrap()
                .iter()
                .filter(|(c, _)| c == cmd)
                .count()
        }
    }

    impl EngineApi for FakeEngine {
        fn call(
            &self,
            cmd: &str,
            strategy: Option<&str>,
            args: Value,
        ) -> BoxFut<Result<Value, String>> {
            let f = &self.0;
            assert_eq!(strategy, Some("tradfi"));
            f.calls
                .lock()
                .unwrap()
                .push((cmd.to_string(), args.clone()));
            let cfg = f.config.lock().unwrap().clone();
            let result = match cmd {
                "config_get" => Ok(cfg.clone()),
                "config_set" => {
                    let mut c = f.config.lock().unwrap();
                    if let (Some(map), Some(ch)) = (c.as_object_mut(), args["changes"].as_object())
                    {
                        for (k, v) in ch {
                            map.insert(k.clone(), v.clone());
                        }
                    }
                    Ok(c.clone())
                }
                "current_network" => Ok(f.network.lock().unwrap().clone()),
                "status" => Ok(json!({
                    "config": cfg, "mode": cfg["trading_mode"],
                    "network": f.network.lock().unwrap().clone(), "has_credentials": true,
                    "state": {"trade": f.trade.lock().unwrap().clone()},
                    "trades_today": [], "last_signal": null, "last_skip": null,
                    "stats": {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0},
                })),
                "resolve_tradfi_any" => f.resolve.lock().unwrap().clone(),
                "tradfi_symbols" => Ok(json!(["CLUSDT", "QQQUSDT", "XAUUSDT"])),
                _ => Ok(Value::Null),
            };
            Box::pin(async move { result })
        }
    }

    fn bot(fake: &Arc<Fake>) -> TelegramBot<FakeEngine> {
        TelegramBot::new(
            TelegramApi::new("x".into()),
            Arc::new(FakeEngine(fake.clone())),
            1,
        )
    }

    fn callbacks(kb: &Value) -> Vec<(String, String)> {
        kb["inline_keyboard"]
            .as_array()
            .into_iter()
            .flatten()
            .flat_map(|row| row.as_array().cloned().unwrap_or_default())
            .map(|b| {
                (
                    b["text"].as_str().unwrap_or("").to_string(),
                    b["callback_data"].as_str().unwrap_or("").to_string(),
                )
            })
            .collect()
    }

    #[tokio::test]
    async fn on_needs_confirmation_then_turns_others_off() {
        let fake = Fake::new();
        let b = bot(&fake);
        let (text, kb) = b.handle_tradfi_action("on").await;
        assert!(text.contains("TradFi 정배열 전략을 켤까요"), "{text}");
        assert!(callbacks(&kb).iter().any(|(_, d)| d == "tf:confirm_on"));
        assert!(fake.sets().is_empty());
        let (text, _) = b.handle_tradfi_action("confirm_on").await;
        assert!(text.starts_with("✅ /tradfi ON"), "{text}");
        assert_eq!(fake.sets(), vec![json!({"enabled": true})]);
        let others: Vec<Value> = fake
            .calls
            .lock()
            .unwrap()
            .iter()
            .filter(|(c, _)| c == "turn_off_others")
            .map(|(_, a)| a.clone())
            .collect();
        assert_eq!(others, vec![json!({"except": "tradfi_ma"})]);
    }

    #[tokio::test]
    async fn on_is_refused_off_mainnet() {
        let fake = Fake::new();
        *fake.network.lock().unwrap() = json!("testnet");
        let b = bot(&fake);
        let (text, kb) = b.handle_tradfi_action("on").await;
        assert!(text.contains("메인넷"), "{text}");
        assert!(!callbacks(&kb).iter().any(|(_, d)| d == "tf:confirm_on"));
        let _ = b.handle_tradfi_action("confirm_on").await;
        assert!(fake.sets().is_empty());
    }

    #[tokio::test]
    async fn crypto_coin_is_refused() {
        let fake = Fake::new();
        *fake.resolve.lock().unwrap() = Err("ValueError: NOT_TRADFI:BTCUSDT".to_string());
        let b = bot(&fake);
        let (text, _) = b.handle_tradfi_action("pick:BTCUSDT").await;
        assert!(text.contains("TradFi 종목이 아닙니다"), "{text}");
        let out = b.handle_tfcoin(1, &["BTC".to_string()]).await;
        match &out[0] {
            Outgoing::Send { text, .. } => {
                assert!(text.contains("TradFi 종목이 아닙니다"), "{text}")
            }
            other => panic!("{other:?}"),
        }
        assert!(fake.sets().is_empty());
    }

    #[tokio::test]
    async fn tradfi_coin_is_saved_and_listed() {
        let fake = Fake::new();
        *fake.resolve.lock().unwrap() =
            Ok(json!({"market_id": "CLUSDT", "ccxt_symbol": "CL/USDT:USDT"}));
        let b = bot(&fake);
        let (text, kb) = b.handle_tradfi_action("coin").await;
        assert!(text.contains("순수 코인은 불가"), "{text}");
        let cbs = callbacks(&kb);
        assert!(
            cbs.iter()
                .any(|(t, d)| t == "✅ XAU" && d == "tf:pick:XAUUSDT"),
            "{cbs:?}"
        );
        assert!(cbs.iter().any(|(t, d)| t == "CL" && d == "tf:pick:CLUSDT"));
        let (text, _) = b.handle_tradfi_action("pick:CLUSDT").await;
        assert!(text.starts_with("🪙 종목: CLUSDT"), "{text}");
        assert_eq!(fake.sets(), vec![json!({"single_symbol": "CLUSDT"})]);
    }

    #[tokio::test]
    async fn exit_and_entry_choices() {
        let fake = Fake::new();
        let b = bot(&fake);
        b.handle_tradfi_action("exit:50").await;
        b.handle_tradfi_action("exit:25").await;
        b.handle_tradfi_action("entry:trend").await;
        b.handle_tradfi_action("entry:bogus").await;
        assert_eq!(
            fake.sets(),
            vec![
                json!({"exit_ma_period": 50}),
                json!({"entry_mode": "trend"})
            ]
        );
        let kb = tradfi_keyboard(&fake.config.lock().unwrap().clone(), None);
        let cbs = callbacks(&kb);
        assert!(
            cbs.iter()
                .any(|(t, d)| t.starts_with("✅") && d == "tf:exit:50"),
            "{cbs:?}"
        );
        assert!(cbs
            .iter()
            .any(|(t, d)| t.starts_with("✅") && d == "tf:entry:trend"));
        assert!(cbs
            .iter()
            .any(|(t, d)| t.starts_with("✅") && d == "tf:mode:dry"));
    }

    #[tokio::test]
    async fn settings_are_locked_while_holding() {
        let fake = Fake::new();
        *fake.trade.lock().unwrap() =
            json!({"side": "LONG", "entry_price": 2650.5, "quantity": 0.1});
        let b = bot(&fake);
        for action in [
            "entry:trend",
            "exit:50",
            "mode:live",
            "confirm_live",
            "coin",
            "pick:CLUSDT",
        ] {
            let (text, _) = b.handle_tradfi_action(action).await;
            assert!(text.contains("포지션 보유 중"), "{action}: {text}");
        }
        assert!(fake.sets().is_empty());
    }

    #[tokio::test]
    async fn live_switch_confirms_and_resets_reconciliation() {
        let fake = Fake::new();
        let b = bot(&fake);
        let (text, kb) = b.handle_tradfi_action("mode:live").await;
        assert!(text.contains("실제 주문이 나갑니다"), "{text}");
        assert!(callbacks(&kb).iter().any(|(_, d)| d == "tf:confirm_live"));
        assert!(fake.sets().is_empty());
        b.handle_tradfi_action("confirm_live").await;
        assert_eq!(fake.sets(), vec![json!({"trading_mode": "LIVE"})]);
        assert_eq!(fake.count("reset_reconciliation"), 1);
    }

    #[test]
    fn coin_error_texts() {
        assert!(
            tradfi_coin_error_text("NOT_TRADFI:ETHUSDT", "ETH").contains("TradFi 종목이 아닙니다")
        );
        assert!(tradfi_coin_error_text("SYMBOL_EMPTY", "").contains("종목을 입력하세요"));
        assert!(
            tradfi_coin_error_text("NOT_TRADING:XUSDT", "X").contains("거래 중인 종목이 아닙니다")
        );
    }
}
