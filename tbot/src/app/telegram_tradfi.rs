//! Telegram `/tradfi` menu — Binance TradFi-only SMA 1/5/120 alignment
//! strategy (gold XAUUSDT, oil CLUSDT, US equities...). Every service call and
//! config change goes through the engine IPC with strategy "tradfi".
#![allow(clippy::too_many_arguments)]

use serde_json::{json, Map, Value};

use crate::app::ipc::EngineApi;
use crate::app::telegram_bot::{
    adopt_error_text, adopt_summary, cfg_get, format_g, lock_table, markup_button, py_n, Outgoing,
    TelegramBot,
};
use crate::strategy::config_tradfi;

const CONFIG_KEY: &str = "tradfi_ma";
const STRATEGY: &str = "tradfi";
pub use crate::strategy::config_tradfi::EXIT_MA_CHOICES;
use crate::strategy::config_tradfi::{exit_ma_allowed, EXIT_MA_MAX, EXIT_MA_MIN};
/// `pending_input` marker: the next plain text is the exit MA period.
pub const PENDING_EXIT_MA: &str = "tf_exit_ma";
pub use crate::strategy::config_tradfi::TIMEFRAMES;

/// "3m" -> "3분봉", "1h" -> "1시간봉", "1d" -> "1일봉".
pub fn timeframe_label(tf: &str) -> String {
    let (num, unit) = tf.split_at(tf.len().saturating_sub(1));
    match unit {
        "m" => format!("{num}분봉"),
        "h" => format!("{num}시간봉"),
        "d" => format!("{num}일봉"),
        _ => tf.to_string(),
    }
}

fn timeframe_of(cfg: &Value) -> String {
    let tf = cfg_str(cfg, "timeframe", "4h");
    if TIMEFRAMES.contains(&tf) {
        tf.to_string()
    } else {
        "4h".to_string()
    }
}

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

/// "1회 손실 2% (손절 2×ATR)" or None when the loss-limit sizing is off.
fn risk_text(cfg: &Value) -> Option<String> {
    if !cfg_get(cfg, "risk_sizing_enabled")
        .map(|v| py_truthy(Some(v)))
        .unwrap_or(true)
    {
        return None;
    }
    let pct = match cfg_int_of(cfg, "risk_per_trade_pct") {
        p if config_tradfi::RISK_CHOICES.contains(&p) => p,
        _ => config_tradfi::DEFAULT_RISK_PCT,
    };
    Some(format!("1회 손실 자본의 {pct}% (손절 2×ATR)"))
}

fn auto_on(cfg: &Value) -> bool {
    py_truthy(cfg_get(cfg, "auto_scan"))
}

const AUTO_UNIVERSE_TEXT: &str = "24h 거래대금 100M USDT 이상 코인·주식, 스테이블·원자재 제외";

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

fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

/// "청산MA 없음" chosen: no exit line, the opposite alignment closes.
fn exit_off(cfg: &Value) -> bool {
    py_truthy(cfg_get(cfg, "exit_ma_off"))
}

/// Short exit-rule label: "30MA" or "청산MA 없음".
fn exit_label(cfg: &Value) -> String {
    if exit_off(cfg) {
        "청산MA 없음".to_string()
    } else {
        format!("{}MA", exit_ma_of(cfg))
    }
}

fn exit_rule_text(cfg: &Value) -> String {
    if exit_off(cfg) {
        "청산: 청산MA 없음 — 반대 배열이 새로 생기면 청산 (반대 진입 없음), 그 외엔 수익잠금·비상손절".to_string()
    } else {
        format!(
            "청산: 종가가 {}MA 반대편으로 마감하면 청산",
            exit_ma_of(cfg)
        )
    }
}

/// "11-27 18:30" in KST.
fn kst_hm(ms: i64) -> String {
    chrono::DateTime::from_timestamp_millis(ms)
        .map(|t| {
            t.with_timezone(&chrono::FixedOffset::east_opt(9 * 3600).unwrap())
                .format("%m-%d %H:%M")
                .to_string()
        })
        .unwrap_or_default()
}

/// (fast, slow, long) alignment periods of the config.
fn ma_of(cfg: &Value) -> (i64, i64, i64) {
    config_tradfi::ma_periods_of(cfg)
}

/// "MA 1·5·120" / "MA 1·5 (긴MA 안 씀)".
fn ma_text(cfg: &Value) -> String {
    let (f, s, l) = ma_of(cfg);
    config_tradfi::ma_label(f, s, l, slow_on(cfg))
}

/// "종가" for period 1, else "SMA3".
fn fast_name(fast: i64) -> String {
    if fast <= 1 {
        "종가".to_string()
    } else {
        format!("SMA{fast}")
    }
}

pub const MA_PROMPT_HEAD: &str = "✏️ 진입 MA 직접 입력";
/// `pending_input` marker: the next plain text is "fast slow [long]".
pub const PENDING_MA: &str = "tf_ma";
pub const NEXT_MA_PROMPT_HEAD: &str = "✏️ 예약할 진입 MA 입력";
/// `pending_input` marker: the next plain text is the booked "fast slow [long]".
pub const PENDING_NEXT_MA: &str = "tf_next_ma";

fn ma_prompt_text(head: &str, now: &str) -> String {
    format!(
        "{head}\n지금: {now}\nfast slow 긴MA 순서로 숫자를 보내세요. 예: 1 5 120 (두 개만 보내면 긴MA는 그대로, 그만두려면 취소)\n규칙: fast < slow, 긴MA를 포함할 땐 slow < 긴MA, 최대 {}",
        config_tradfi::MA_PERIOD_MAX
    )
}

fn ma_error_text(text: &str) -> String {
    let shown: String = text.chars().take(20).collect();
    format!(
        "❌ 진입 MA를 바꾸지 못했습니다 (입력: {shown}). fast < slow, 긴MA 포함 시 slow < 긴MA, 1~{} 사이로 보내세요. 예: 1 5 120",
        config_tradfi::MA_PERIOD_MAX
    )
}

/// Current settings in the switch shape.
fn current_switch(cfg: &Value) -> Value {
    let (f, s, l) = ma_of(cfg);
    json!({
        "timeframe": timeframe_of(cfg),
        "exit_ma_period": exit_ma_of(cfg),
        "exit_ma_off": exit_off(cfg),
        "align_slow_enabled": slow_on(cfg),
        "ma_fast_period": f,
        "ma_slow_period": s,
        "ma_long_period": l,
    })
}

fn booked_switch(cfg: &Value) -> Option<Value> {
    cfg_get(cfg, config_tradfi::NEXT_SWITCH_KEY)
        .filter(|v| v.is_object())
        .and_then(|v| config_tradfi::clean_switch(v, true))
}

fn draft_switch(cfg: &Value) -> Value {
    let mut draft = cfg_get(cfg, config_tradfi::NEXT_DRAFT_KEY)
        .and_then(|v| config_tradfi::clean_switch(v, false))
        .unwrap_or_else(|| current_switch(cfg));
    if draft.get("ma_fast_period").is_none() {
        let (f, s, l) = ma_of(cfg);
        draft["ma_fast_period"] = json!(f);
        draft["ma_slow_period"] = json!(s);
        draft["ma_long_period"] = json!(l);
    }
    draft
}

fn booked_line(sw: &Value) -> String {
    let from = sw["from_timeframe"].as_str().unwrap_or("");
    format!(
        "⏭ 예약: {} {} 마감 뒤 → {}",
        kst_hm(sw["apply_at_ms"].as_i64().unwrap_or(0)),
        timeframe_label(from),
        config_tradfi::switch_summary(sw, None)
    )
}

pub const NEXT_EXIT_PROMPT_HEAD: &str = "✏️ 예약할 청산 MA 입력";
/// `pending_input` marker: the next plain text is the booked exit MA period.
pub const PENDING_NEXT_EXIT_MA: &str = "tf_next_exit_ma";

fn next_screen_text(cfg: &Value, head: &str) -> String {
    let now = current_switch(cfg);
    let draft = draft_switch(cfg);
    let tf = timeframe_of(cfg);
    let close = config_tradfi::current_bar_close_ms(&tf, now_ms());
    let mut lines = Vec::new();
    if !head.is_empty() {
        lines.push(head.to_string());
    }
    lines.push("⏭ 다음 봉부터 변경".to_string());
    lines.push(format!(
        "지금: {}",
        config_tradfi::switch_summary(&now, None)
    ));
    lines.push(format!(
        "바꿀 값: {}",
        config_tradfi::switch_summary(&draft, None)
    ));
    lines.push(format!(
        "✅ 예약하기를 누르면 지금 {} 봉이 마감되는 {} 뒤에 바뀝니다.",
        timeframe_label(&tf),
        kst_hm(close)
    ));
    lines.push(
        "보유 포지션은 그대로 두고, 바뀐 뒤 새 시간봉의 다음 마감부터 새 기준으로 판단합니다 (이미 마감된 봉으로 바로 청산하지 않음). 수익잠금·비상손절은 그대로."
            .to_string(),
    );
    if let Some(sw) = booked_switch(cfg) {
        lines.push(booked_line(&sw));
    }
    lines.join("\n")
}

fn next_keyboard(cfg: &Value) -> Value {
    let draft = draft_switch(cfg);
    let d_tf = draft["timeframe"].as_str().unwrap_or("").to_string();
    let d_exit = draft["exit_ma_period"].as_i64().unwrap_or(0);
    let d_off = draft["exit_ma_off"].as_bool().unwrap_or(false);
    let d_slow = draft["align_slow_enabled"].as_bool().unwrap_or(true);
    let (_, _, d_long) = config_tradfi::ma_periods_of(&draft);
    let mut rows: Vec<Vec<Value>> = Vec::new();
    for chunk in TIMEFRAMES.chunks(4) {
        rows.push(
            chunk
                .iter()
                .map(|tf| {
                    markup_button(
                        mark(d_tf == *tf, &timeframe_label(tf)),
                        format!("tf:nx:bar:{tf}"),
                    )
                })
                .collect(),
        );
    }
    let mut exits: Vec<Value> = EXIT_MA_CHOICES
        .iter()
        .chain(std::iter::once(&EXIT_MA_MAX))
        .map(|n| {
            markup_button(
                mark(!d_off && d_exit == *n, &format!("청산 {n}MA")),
                format!("tf:nx:exit:{n}"),
            )
        })
        .collect();
    let custom = !d_off && !EXIT_MA_CHOICES.contains(&d_exit) && d_exit != EXIT_MA_MAX;
    let custom_label = if custom {
        format!("✏️ 직접 {d_exit}MA")
    } else {
        "✏️ 직접".to_string()
    };
    exits.push(markup_button(
        mark(custom, &custom_label),
        "tf:nx:exit:custom",
    ));
    rows.push(exits);
    rows.push(vec![
        markup_button(mark(d_off, "청산MA 없음"), "tf:nx:exit:none"),
        markup_button(mark(d_slow, &format!("{d_long}MA 포함")), "tf:nx:slow:on"),
        markup_button(mark(!d_slow, &format!("{d_long}MA 빼기")), "tf:nx:slow:off"),
    ]);
    {
        let (f, s, l) = config_tradfi::ma_periods_of(&draft);
        rows.push(vec![markup_button(
            format!("✏️ 진입 MA {f}·{s}·{l} 바꾸기"),
            "tf:nx:ma",
        )]);
    }
    let mut last = vec![markup_button("✅ 예약하기", "tf:nx:save")];
    if booked_switch(cfg).is_some() {
        last.push(markup_button("🗑 예약 취소", "tf:nx:cancel"));
    }
    last.push(markup_button("↩️ 돌아가기", "tf:status"));
    rows.push(last);
    json!({"inline_keyboard": rows})
}

fn guard_line() -> String {
    format!(
        "거래정지: 선물계좌 총자본(평가손익 포함)이 {} USDT 미만이면 신규 진입 안 함 · 보유 포지션은 그대로 관리",
        config_tradfi::MIN_EQUITY_USDT
    )
}

/// SMA120 in the entry alignment (default on).
fn slow_on(cfg: &Value) -> bool {
    cfg_get(cfg, "align_slow_enabled")
        .and_then(|v| v.as_bool())
        .unwrap_or(true)
}

fn align_text(cfg: &Value) -> String {
    let (f, s, l) = ma_of(cfg);
    if slow_on(cfg) {
        format!("SMA {f}·{s}·{l} 정배열 롱 / 역배열 숏")
    } else {
        format!("SMA {f}·{s} 정배열 롱 / 역배열 숏 (긴MA 안 씀)")
    }
}

/// "종가 > SMA5 > SMA120" (long side of the alignment).
fn align_rule(cfg: &Value, long_side: bool) -> String {
    let (f, s, l) = ma_of(cfg);
    let op = if long_side { ">" } else { "<" };
    if slow_on(cfg) {
        format!("{} {op} SMA{s} {op} SMA{l}", fast_name(f))
    } else {
        format!("{} {op} SMA{s}", fast_name(f))
    }
}

fn lock_on(cfg: &Value) -> bool {
    py_truthy(cfg_get(cfg, "profit_lock_enabled"))
}

fn lock_text(cfg: &Value) -> String {
    if lock_on(cfg) {
        format!("켬 (증거금 수익 {})", lock_table(cfg))
    } else {
        "끔".to_string()
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
    let bar = timeframe_label(&timeframe_of(&cfg));
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
        if auto_on(&cfg) {
            format!("종목: 🔎 자동 탐색 ({AUTO_UNIVERSE_TEXT}, 여럿이면 돌파 폭 큰 종목) · {bar} 마감 기준")
        } else {
            format!("종목: {coin} (TradFi·코인 모두 가능) · {bar} 마감 기준")
        },
        format!("진입: {} — {}", align_text(&cfg), entry_mode_label(entry)),
        exit_rule_text(&cfg),
        format!("수익잠금: {}", lock_text(&cfg)),
        guard_line(),
        format!(
            "레버리지 {leverage}x ISOLATED 고정 · {} · 비상손절: {stop_text}",
            match risk_text(&cfg) {
                Some(r) => format!("크기: {r}, 최대 가용 {margin_pct:.0}%"),
                None => format!("증거금: 가용 잔고의 {margin_pct:.0}% 고정"),
            }
        ),
    ];
    if let Some(sw) = booked_switch(&cfg) {
        lines.push(booked_line(&sw));
    }
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
    let no_exit = exit_off(cfg);

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
    let slow = slow_on(cfg);
    let (ma_f, ma_s, ma_l) = ma_of(cfg);
    rows.push(vec![
        markup_button(mark(slow, &format!("{ma_l}MA 포함")), "tf:slow:on"),
        markup_button(mark(!slow, &format!("{ma_l}MA 빼기")), "tf:slow:off"),
        markup_button(format!("✏️ MA {ma_f}·{ma_s}·{ma_l}"), "tf:ma"),
    ]);
    let mut exit_buttons: Vec<Value> = EXIT_MA_CHOICES
        .iter()
        .map(|n| {
            markup_button(
                mark(!no_exit && exit_ma == *n, &format!("청산 {n}MA")),
                format!("tf:exit:{n}"),
            )
        })
        .collect();
    let custom = !no_exit && !EXIT_MA_CHOICES.contains(&exit_ma);
    let custom_label = if custom {
        format!("✏️ 직접 {exit_ma}MA")
    } else {
        "✏️ 직접 입력".to_string()
    };
    exit_buttons.push(markup_button(mark(custom, &custom_label), "tf:exit:custom"));
    exit_buttons.push(markup_button(mark(no_exit, "없음"), "tf:exit:none"));
    rows.push(exit_buttons);
    let current_tf = timeframe_of(cfg);
    for chunk in TIMEFRAMES.chunks(4) {
        rows.push(
            chunk
                .iter()
                .map(|tf| {
                    markup_button(
                        mark(current_tf == *tf, &timeframe_label(tf)),
                        format!("tf:bar:{tf}"),
                    )
                })
                .collect(),
        );
    }
    let risk_on = cfg_get(cfg, "risk_sizing_enabled")
        .map(|v| py_truthy(Some(v)))
        .unwrap_or(true);
    let risk_pct = cfg_int_of(cfg, "risk_per_trade_pct");
    rows.push(vec![
        markup_button(mark(risk_on, "🛡 손실한도 켬"), "tf:risk:on"),
        markup_button(mark(!risk_on, "손실한도 끔 (고정 크기)"), "tf:risk:off"),
    ]);
    let risk_row: Vec<Value> = config_tradfi::RISK_CHOICES
        .iter()
        .map(|p| {
            let selected = risk_on
                && (risk_pct == *p || (risk_pct == 0 && *p == config_tradfi::DEFAULT_RISK_PCT));
            markup_button(
                mark(selected, &format!("1회 손실 {p}%")),
                format!("tf:risk:{p}"),
            )
        })
        .collect();
    rows.push(risk_row);
    let plock = lock_on(cfg);
    rows.push(vec![
        markup_button(mark(plock, "🔒 수익잠금 켬"), "tf:plock:on"),
        markup_button(mark(!plock, "수익잠금 끔"), "tf:plock:off"),
    ]);
    let next_label = if booked_switch(cfg).is_some() {
        "⏭ 다음 봉 변경 (예약됨)"
    } else {
        "⏭ 다음 봉부터 변경"
    };
    rows.push(vec![
        markup_button(next_label, "tf:nx"),
        markup_button("🤝 수동 포지션 맡기기", "tf:adopt"),
    ]);
    rows.push(vec![
        markup_button(
            if auto_on(cfg) {
                "✅ 🔎 자동 탐색 (끄기)"
            } else {
                "🔎 자동 탐색 켜기"
            },
            if auto_on(cfg) {
                "tf:auto:off"
            } else {
                "tf:auto:on"
            },
        ),
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
    if error.contains("STABLECOIN") {
        return format!("❌ {text}은(는) 스테이블코인이라 거래할 수 없습니다.");
    }
    if error.contains("NOT_TRADING") {
        return format!("❌ {text}은(는) 지금 거래 중인 종목이 아닙니다.");
    }
    if error.contains("SYMBOL_EMPTY") {
        return "❌ 종목을 입력하세요. 예: /tfcoin XAU 또는 /tfcoin BTC".to_string();
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
            "🪙 종목 선택 (TradFi·코인 모두 가능)\n지금: {coin}\n아래 TradFi 버튼을 누르거나 /tfcoin <종목> 으로 지정하세요. 예: /tfcoin CL, /tfcoin BTC"
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

    /// Store a validated exit MA period (refused while a position is open).
    async fn tf_apply_exit_ma(&self, period: i64) -> (String, Value) {
        let cfg = fetch_config(self).await;
        if !exit_ma_allowed(period) {
            return (
                exit_ma_range_error(&period.to_string()),
                tradfi_keyboard(&cfg, None),
            );
        }
        if holding(&fetch_status(self).await) {
            return (HOLDING_BLOCK.to_string(), tradfi_keyboard(&cfg, None));
        }
        let mut changes = Map::new();
        changes.insert("exit_ma_period".to_string(), json!(period));
        if exit_off(&cfg) {
            changes.insert("exit_ma_off".to_string(), json!(false));
        }
        if let Err(e) = self
            .tf_call("config_set", json!({ "changes": changes }))
            .await
        {
            return (engine_error_text(&e), tradfi_keyboard(&cfg, None));
        }
        let status_text = self.tf_status_text().await;
        (
            format!("🚪 청산선: {period}MA\n\n{status_text}"),
            self.tf_keyboard().await,
        )
    }

    /// Exit MA typed as text (`/tfexit 45` or a reply to the ✏️ prompt).
    pub async fn tf_exit_ma_from_text(&self, text: &str) -> (String, Value) {
        match parse_exit_ma(text) {
            Some(period) => self.tf_apply_exit_ma(period).await,
            None => {
                let cfg = fetch_config(self).await;
                (
                    exit_ma_range_error(text.trim()),
                    tradfi_keyboard(&cfg, None),
                )
            }
        }
    }

    /// `/tfexit [기간]` command; without a number it shows how to set one.
    pub async fn handle_tfexit(&self, chat_id: i64, args: &[String]) -> Vec<Outgoing> {
        let (text, markup) = if args.is_empty() {
            let cfg = fetch_config(self).await;
            (exit_ma_prompt_text(&cfg), tradfi_keyboard(&cfg, None))
        } else {
            self.tf_exit_ma_from_text(&args.join(" ")).await
        };
        vec![Outgoing::Send {
            chat_id,
            text,
            markup: Some(markup),
        }]
    }

    /// Store the menu draft of the next-bar switch.
    async fn tf_save_draft(&self, draft: &Value) -> Result<Value, String> {
        let mut changes = Map::new();
        changes.insert(config_tradfi::NEXT_DRAFT_KEY.to_string(), draft.clone());
        self.tf_call("config_set", json!({ "changes": changes }))
            .await
    }

    async fn tf_next_screen(&self, head: &str) -> (String, Value) {
        let cfg = fetch_config(self).await;
        (next_screen_text(&cfg, head), next_keyboard(&cfg))
    }

    /// `tf:nx...` -- the "⏭ 다음 봉부터 변경" screen.
    async fn tf_next_action(&self, cfg: &Value, action: &str) -> (String, Value) {
        let rest = action
            .strip_prefix("nx")
            .unwrap_or("")
            .trim_start_matches(':');
        let mut draft = draft_switch(cfg);
        let mut head = String::new();
        match rest {
            "" => {}
            "cancel" => {
                let mut changes = Map::new();
                changes.insert(config_tradfi::NEXT_SWITCH_KEY.to_string(), Value::Null);
                changes.insert(config_tradfi::NEXT_DRAFT_KEY.to_string(), Value::Null);
                if let Err(e) = self
                    .tf_call("config_set", json!({ "changes": changes }))
                    .await
                {
                    return (engine_error_text(&e), next_keyboard(cfg));
                }
                let status_text = self.tf_status_text().await;
                return (
                    format!("🗑 다음 봉 변경 예약을 취소했습니다.\n\n{status_text}"),
                    self.tf_keyboard().await,
                );
            }
            "save" => {
                if draft == current_switch(cfg) {
                    return (
                        next_screen_text(
                            cfg,
                            "ℹ️ 지금 설정과 같습니다. 바꿀 값을 고른 뒤 예약하세요.",
                        ),
                        next_keyboard(cfg),
                    );
                }
                let from = timeframe_of(cfg);
                let at = config_tradfi::current_bar_close_ms(&from, now_ms());
                let mut booked = draft.clone();
                booked["from_timeframe"] = json!(from);
                booked["apply_at_ms"] = json!(at);
                let mut changes = Map::new();
                changes.insert(config_tradfi::NEXT_SWITCH_KEY.to_string(), booked.clone());
                changes.insert(config_tradfi::NEXT_DRAFT_KEY.to_string(), Value::Null);
                if let Err(e) = self
                    .tf_call("config_set", json!({ "changes": changes }))
                    .await
                {
                    return (engine_error_text(&e), next_keyboard(cfg));
                }
                let status_text = self.tf_status_text().await;
                return (
                    format!(
                        "⏭ 예약했습니다: {}\n{} {} 마감 뒤 적용되고, 새 시간봉의 다음 마감부터 새 기준으로 판단합니다.\n\n{status_text}",
                        config_tradfi::switch_summary(&booked, Some(&from)),
                        kst_hm(at),
                        timeframe_label(&from)
                    ),
                    self.tf_keyboard().await,
                );
            }
            "exit:custom" => {
                return (
                    format!(
                        "{NEXT_EXIT_PROMPT_HEAD}\n{EXIT_MA_MIN}~{EXIT_MA_MAX} 사이 숫자를 이 채팅에 보내세요. 예: 45 (그만두려면 취소)"
                    ),
                    next_keyboard(cfg),
                );
            }
            "ma" => {
                let (f, s, l) = config_tradfi::ma_periods_of(&draft);
                let now = config_tradfi::ma_label(
                    f,
                    s,
                    l,
                    draft["align_slow_enabled"].as_bool().unwrap_or(true),
                );
                return (
                    ma_prompt_text(NEXT_MA_PROMPT_HEAD, &now),
                    next_keyboard(cfg),
                );
            }
            "exit:none" => draft["exit_ma_off"] = json!(true),
            "slow:on" => {
                let (f, s, l) = config_tradfi::ma_periods_of(&draft);
                if config_tradfi::ma_periods_ok(f, s, l, true) {
                    draft["align_slow_enabled"] = json!(true);
                } else {
                    head = format!("❌ slow MA({s})가 긴 MA({l})보다 짧아야 {l}MA를 넣을 수 있습니다. ✏️ 진입 MA를 먼저 바꾸세요.");
                }
            }
            "slow:off" => draft["align_slow_enabled"] = json!(false),
            other => {
                if let Some(tf) = other.strip_prefix("bar:") {
                    if TIMEFRAMES.contains(&tf) {
                        draft["timeframe"] = json!(tf);
                    }
                } else if let Some(p) = other.strip_prefix("exit:") {
                    match p.trim().parse::<i64>() {
                        Ok(n) if exit_ma_allowed(n) => {
                            draft["exit_ma_period"] = json!(n);
                            draft["exit_ma_off"] = json!(false);
                        }
                        _ => head = exit_ma_range_error(p),
                    }
                }
            }
        }
        if rest.is_empty()
            || draft != draft_switch(cfg)
            || cfg_get(cfg, config_tradfi::NEXT_DRAFT_KEY)
                .map(|v| v.is_null())
                .unwrap_or(true)
        {
            if let Err(e) = self.tf_save_draft(&draft).await {
                return (engine_error_text(&e), next_keyboard(cfg));
            }
        }
        self.tf_next_screen(&head).await
    }

    /// Store "fast slow [long]" typed after ✏️ MA or `/tfma` (refused while holding).
    pub async fn tf_ma_from_text(&self, text: &str) -> (String, Value) {
        let cfg = fetch_config(self).await;
        let (_, _, long) = ma_of(&cfg);
        let Some((f, s, l)) = config_tradfi::parse_ma_periods(text, long)
            .filter(|(f, s, l)| config_tradfi::ma_periods_ok(*f, *s, *l, slow_on(&cfg)))
        else {
            return (ma_error_text(text.trim()), tradfi_keyboard(&cfg, None));
        };
        if holding(&fetch_status(self).await) {
            return (HOLDING_BLOCK.to_string(), tradfi_keyboard(&cfg, None));
        }
        let mut changes = Map::new();
        changes.insert("ma_fast_period".to_string(), json!(f));
        changes.insert("ma_slow_period".to_string(), json!(s));
        changes.insert("ma_long_period".to_string(), json!(l));
        if let Err(e) = self
            .tf_call("config_set", json!({ "changes": changes }))
            .await
        {
            return (engine_error_text(&e), tradfi_keyboard(&cfg, None));
        }
        let status_text = self.tf_status_text().await;
        (
            format!(
                "📐 진입 MA: {}\n다음 봉 마감부터 적용됩니다.\n\n{status_text}",
                config_tradfi::ma_label(f, s, l, slow_on(&cfg))
            ),
            self.tf_keyboard().await,
        )
    }

    /// `/tfma [fast slow [long]]`; without numbers it shows how to set them.
    pub async fn handle_tfma(&self, chat_id: i64, args: &[String]) -> Vec<Outgoing> {
        let (text, markup) = if args.is_empty() {
            let cfg = fetch_config(self).await;
            (
                ma_prompt_text(MA_PROMPT_HEAD, &ma_text(&cfg)),
                tradfi_keyboard(&cfg, None),
            )
        } else {
            self.tf_ma_from_text(&args.join(" ")).await
        };
        vec![Outgoing::Send {
            chat_id,
            text,
            markup: Some(markup),
        }]
    }

    /// "fast slow [long]" typed after ✏️ on the next-bar screen.
    pub async fn tf_next_ma_from_text(&self, text: &str) -> (String, Value) {
        let cfg = fetch_config(self).await;
        let mut draft = draft_switch(&cfg);
        let (_, _, long) = config_tradfi::ma_periods_of(&draft);
        let use_long = draft["align_slow_enabled"].as_bool().unwrap_or(true);
        let Some((f, s, l)) = config_tradfi::parse_ma_periods(text, long)
            .filter(|(f, s, l)| config_tradfi::ma_periods_ok(*f, *s, *l, use_long))
        else {
            return (ma_error_text(text.trim()), next_keyboard(&cfg));
        };
        draft["ma_fast_period"] = json!(f);
        draft["ma_slow_period"] = json!(s);
        draft["ma_long_period"] = json!(l);
        if let Err(e) = self.tf_save_draft(&draft).await {
            return (engine_error_text(&e), next_keyboard(&cfg));
        }
        self.tf_next_screen(&format!(
            "✏️ 예약할 진입 MA: {}",
            config_tradfi::ma_label(f, s, l, use_long)
        ))
        .await
    }

    /// Exit MA typed after the "✏️ 직접" button of the next-bar screen.
    pub async fn tf_next_exit_from_text(&self, text: &str) -> (String, Value) {
        let cfg = fetch_config(self).await;
        match parse_exit_ma(text) {
            Some(n) => {
                let mut draft = draft_switch(&cfg);
                draft["exit_ma_period"] = json!(n);
                draft["exit_ma_off"] = json!(false);
                if let Err(e) = self.tf_save_draft(&draft).await {
                    return (engine_error_text(&e), next_keyboard(&cfg));
                }
                self.tf_next_screen(&format!("✏️ 예약할 청산선: {n}MA"))
                    .await
            }
            None => (exit_ma_range_error(text.trim()), next_keyboard(&cfg)),
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
            let bar = timeframe_label(&timeframe_of(&cfg));
            let exit_ma = exit_label(&cfg);
            let body = format!(
                "⚠️ TradFi 정배열 전략을 켤까요?\n종목: {coin} · {bar} · 모드: {mode} · 진입: {entry} · 청산 {exit_ma}\n켜면 /btcmacross·/macross 신규 진입이 OFF 되고 옵션 신규 진입도 멈춥니다 (보유 포지션 관리는 계속).\n거래정지: 선물계좌 총자본(평가손익 포함)이 {min} USDT 미만이면 신규 진입을 하지 않습니다 (보유 포지션은 그대로 관리, {min} 이상이 되면 자동 재개).",
                min = config_tradfi::MIN_EQUITY_USDT
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
            let changes = json!({ "enabled": true });
            if let Err(e) = self
                .tf_call("config_set", json!({ "changes": changes }))
                .await
            {
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
        if action == "ma" {
            if holding(&fetch_status(self).await) {
                return (HOLDING_BLOCK.to_string(), tradfi_keyboard(&cfg, None));
            }
            return (
                ma_prompt_text(MA_PROMPT_HEAD, &ma_text(&cfg)),
                tradfi_keyboard(&cfg, None),
            );
        }
        if action == "exit:none" {
            if holding(&fetch_status(self).await) {
                return (HOLDING_BLOCK.to_string(), tradfi_keyboard(&cfg, None));
            }
            if let Err(e) = self.tf_set("exit_ma_off", json!(true)).await {
                return (engine_error_text(&e), tradfi_keyboard(&cfg, None));
            }
            let status_text = self.tf_status_text().await;
            return (
                format!("🚪 청산MA 없음: 반대 배열이 새로 생기면 청산합니다 (반대 진입 없음).\n\n{status_text}"),
                self.tf_keyboard().await,
            );
        }
        if action == "nx" || action.starts_with("nx:") {
            return self.tf_next_action(&cfg, action).await;
        }
        if action == "exit:custom" {
            if holding(&fetch_status(self).await) {
                return (HOLDING_BLOCK.to_string(), tradfi_keyboard(&cfg, None));
            }
            return (exit_ma_prompt_text(&cfg), tradfi_keyboard(&cfg, None));
        }
        if let Some(rest) = action.strip_prefix("exit:") {
            let period: i64 = rest
                .split_once(':')
                .map(|x| x.0)
                .unwrap_or(rest)
                .trim()
                .parse::<i64>()
                .unwrap_or(-1);
            if exit_ma_allowed(period) {
                return self.tf_apply_exit_ma(period).await;
            }
            return (self.tf_status_text().await, self.tf_keyboard().await);
        }
        if action == "slow:on" || action == "slow:off" {
            let on = action == "slow:on";
            let (f, s, l) = ma_of(&cfg);
            if !config_tradfi::ma_periods_ok(f, s, l, on) {
                return (
                    format!("❌ slow MA({s})가 긴 MA({l})보다 짧아야 {l}MA를 넣을 수 있습니다. ✏️ MA 버튼이나 /tfma 로 먼저 바꾸세요."),
                    tradfi_keyboard(&cfg, None),
                );
            }
            if let Err(e) = self.tf_set("align_slow_enabled", json!(on)).await {
                return (engine_error_text(&e), tradfi_keyboard(&cfg, None));
            }
            let mut after = cfg.clone();
            if let Some(m) = after.as_object_mut() {
                m.insert("align_slow_enabled".to_string(), json!(on));
            }
            let head = format!(
                "📐 진입 기준: {} ({})",
                ma_text(&after),
                align_rule(&after, true)
            );
            let status_text = self.tf_status_text().await;
            return (
                format!("{head}\n다음 봉 마감부터 적용됩니다. 청산 규칙은 그대로입니다.\n\n{status_text}"),
                self.tf_keyboard().await,
            );
        }
        if let Some(rest) = action.strip_prefix("risk:") {
            let mut changes = Map::new();
            let rest = if rest == "on" {
                let p = cfg_int_of(&cfg, "risk_per_trade_pct");
                if config_tradfi::RISK_CHOICES.contains(&p) {
                    p.to_string()
                } else {
                    config_tradfi::DEFAULT_RISK_PCT.to_string()
                }
            } else {
                rest.to_string()
            };
            let rest = rest.as_str();
            let head = if rest == "off" {
                changes.insert("risk_sizing_enabled".to_string(), json!(false));
                "🛡 손실한도: 끔 — 크기: 고정 (가용 잔고 50% × 5배)\n손절은 비상손절(증거금 -50%)만 걸립니다."
                    .to_string()
            } else {
                let Ok(p) = rest.parse::<i64>() else {
                    return (self.tf_status_text().await, self.tf_keyboard().await);
                };
                if !config_tradfi::RISK_CHOICES.contains(&p) {
                    return (self.tf_status_text().await, self.tf_keyboard().await);
                }
                changes.insert("risk_sizing_enabled".to_string(), json!(true));
                changes.insert("risk_per_trade_pct".to_string(), json!(p));
                format!(
                    "🛡 손실한도: 켬 — 1회 손실 자본의 {p}%\n진입 때 손절을 2×ATR 거리에 걸고, 그 손절에 맞으면 가용 잔고의 {p}%만 잃도록 크기를 정합니다 (최대 가용 50% × 5배). 계산한 크기가 거래소 최소 주문보다 작으면 진입하지 않습니다."
                )
            };
            if let Err(e) = self
                .tf_call("config_set", json!({ "changes": changes }))
                .await
            {
                return (engine_error_text(&e), tradfi_keyboard(&cfg, None));
            }
            let tail = if holding(&fetch_status(self).await) {
                "\n보유 중인 포지션은 그대로 두고 다음 진입부터 적용됩니다."
            } else {
                "\n다음 진입부터 적용됩니다."
            };
            let status_text = self.tf_status_text().await;
            return (
                format!("{head}{tail}\n\n{status_text}"),
                self.tf_keyboard().await,
            );
        }
        if action == "auto:on" || action == "auto:off" {
            let on = action == "auto:on";
            if let Err(e) = self.tf_set("auto_scan", json!(on)).await {
                return (engine_error_text(&e), tradfi_keyboard(&cfg, None));
            }
            let status = fetch_status(self).await;
            let tail = if holding(&status) {
                "\n보유 중인 포지션은 강제로 청산하지 않고 지금 규칙대로 관리합니다. 그 포지션이 청산된 뒤 다음 진입부터 적용됩니다."
            } else {
                "\n다음 봉 마감부터 적용됩니다."
            };
            let head = if on {
                format!(
                    "🔎 자동 종목 탐색: 켬\n{AUTO_UNIVERSE_TEXT} 종목 중 {} 마감봉에 진입 조건(롱·숏)을 맞춘 종목에 진입합니다. 여럿이면 종가가 slow MA를 가장 크게 넘은 종목. 지정 종목({})은 무시합니다.{tail}",
                    timeframe_label(&timeframe_of(&cfg)),
                    current_coin(&cfg)
                )
            } else {
                format!(
                    "🔎 자동 종목 탐색: 끔\n다시 지정 종목 {}만 거래합니다.{tail}",
                    current_coin(&cfg)
                )
            };
            let status_text = self.tf_status_text().await;
            return (format!("{head}\n\n{status_text}"), self.tf_keyboard().await);
        }
        if action == "plock:on" || action == "plock:off" {
            let on = action == "plock:on";
            if let Err(e) = self.tf_set("profit_lock_enabled", json!(on)).await {
                return (engine_error_text(&e), tradfi_keyboard(&cfg, None));
            }
            let status_text = self.tf_status_text().await;
            let head = if on {
                "🔒 수익잠금: 켬
보유 중인 포지션에도 바로 적용됩니다."
            } else {
                "🔒 수익잠금: 끔
새 잠금 주문을 걸지 않습니다. 이미 걸린 잠금 주문은 그대로 둡니다."
            };
            return (
                format!(
                    "{head}

{status_text}"
                ),
                self.tf_keyboard().await,
            );
        }
        if let Some(rest) = action.strip_prefix("bar:") {
            let tf = rest.split_once(':').map(|x| x.0).unwrap_or(rest).trim();
            if TIMEFRAMES.contains(&tf) {
                let status = fetch_status(self).await;
                if holding(&status) {
                    return (HOLDING_BLOCK.to_string(), tradfi_keyboard(&cfg, None));
                }
                if let Err(e) = self.tf_set("timeframe", json!(tf)).await {
                    return (engine_error_text(&e), tradfi_keyboard(&cfg, None));
                }
                let status_text = self.tf_status_text().await;
                return (
                    format!(
                        "🕯 진입 봉: {}

{status_text}",
                        timeframe_label(tf)
                    ),
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
        if action == "adopt" {
            let preview = match self
                .tf_call("adopt_position", json!({ "apply": false }))
                .await
            {
                Ok(v) => v,
                Err(e) => return (engine_error_text(&e), tradfi_keyboard(&cfg, None)),
            };
            if preview.get("ok").and_then(|v| v.as_bool()) != Some(true) {
                return (
                    format!("🤝 {}", adopt_error_text(&preview)),
                    tradfi_keyboard(&cfg, None),
                );
            }
            let stop = py_n(cfg_get(&cfg, "emergency_stop_roi_percent"));
            let stop_line = if stop > 0.0 {
                format!(
                    "· 비상손절: 증거금 -{}% 주문을 거래소에 걸기\n",
                    format_g(stop)
                )
            } else {
                String::new()
            };
            let exit_rule = if exit_off(&cfg) {
                format!(
                    "{} 마감 때 반대 배열이 새로 생기면 청산 (청산MA 없음, 반대 진입 없음)",
                    timeframe_label(&timeframe_of(&cfg))
                )
            } else {
                format!(
                    "{} 종가가 청산선 SMA{} 반대편으로 마감하면 청산 (반대 진입 없음)",
                    timeframe_label(&timeframe_of(&cfg)),
                    exit_ma_of(&cfg)
                )
            };
            let text = format!(
                "🤝 이 포지션을 /tradfi에 맡길까요?\n{}\n\n\
                 맡기면 /tradfi 규칙대로 관리합니다:\n\
                 · {exit_rule}\n\
                 · 수익잠금: {}\n\
                 {stop_line}\
                 직접 걸어 둔 TP/SL 주문은 그대로 남습니다.\n\
                 청산 뒤 새 진입은 /tradfi 지정 종목({})으로 합니다.",
                adopt_summary(&preview),
                lock_text(&cfg),
                current_coin(&cfg),
            );
            return (
                text,
                tradfi_keyboard(&cfg, Some(("✅ 관리 맡기기", "tf:confirm_adopt"))),
            );
        }
        if action == "confirm_adopt" {
            let result = match self
                .tf_call("adopt_position", json!({ "apply": true }))
                .await
            {
                Ok(v) => v,
                Err(e) => return (engine_error_text(&e), tradfi_keyboard(&cfg, None)),
            };
            let head = if result.get("ok").and_then(|v| v.as_bool()) == Some(true) {
                format!("🤝 /tradfi 관리 시작: {}", adopt_summary(&result))
            } else {
                format!("🤝 {}", adopt_error_text(&result))
            };
            let status = self.tf_status_text().await;
            return (format!("{head}\n\n{status}"), self.tf_keyboard().await);
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

/// "45" / "45ma" / "SMA45" / " 45 MA " -> 45; anything else (or out of range) -> None.
pub fn parse_exit_ma(text: &str) -> Option<i64> {
    let t = text.trim().to_lowercase();
    let t = t.strip_prefix("sma").unwrap_or(&t).trim();
    let t = t.strip_suffix("ma").unwrap_or(t).trim();
    if t.is_empty() || !t.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    t.parse::<i64>().ok().filter(|p| exit_ma_allowed(*p))
}

/// First line of the ✏️ prompt; the bot waits for a number after sending it.
pub const EXIT_MA_PROMPT_HEAD: &str = "✏️ 청산 MA 직접 입력";

fn exit_ma_prompt_text(cfg: &Value) -> String {
    format!(
        "{EXIT_MA_PROMPT_HEAD}\n지금: {}MA\n{EXIT_MA_MIN}~{EXIT_MA_MAX} 사이 숫자를 이 채팅에 보내세요. 예: 45 (그만두려면 취소)\n(/tfexit 45 로도 바꿀 수 있습니다. 진입선 SMA120보다 길게는 설정할 수 없습니다.)",
        exit_ma_of(cfg)
    )
}

fn exit_ma_range_error(text: &str) -> String {
    let shown: String = text.chars().take(20).collect();
    format!(
        "❌ 청산 MA는 {EXIT_MA_MIN}~{EXIT_MA_MAX} 사이 정수로 입력하세요 (입력: {shown}). 예: /tfexit 45"
    )
}

fn tradfi_rules_text(cfg: &Value) -> String {
    let coin = current_coin(cfg);
    let exit_ma = exit_ma_of(cfg);
    let bar = timeframe_label(&timeframe_of(cfg));
    let margin_pct = py_n(cfg_get(cfg, "margin_fraction")) * 100.0;
    let margin_text = if margin_pct == 0.0 {
        "50".to_string()
    } else {
        format_g(margin_pct)
    };
    [
        "📘 /tradfi 규칙 (TradFi 정배열)".to_string(),
        format!("· 종목: {coin} · 바이낸스 TradFi 무기한(금·원유·미국주식 등)과 일반 코인 무기한 모두 가능 (/tfcoin BTC)"),
        format!("· 🔎 자동 탐색: 켜면 지정 종목 대신 {AUTO_UNIVERSE_TEXT} 종목 전체에서 마감봉 진입 조건을 맞춘 종목에 진입 (한 번에 1개, 여럿이면 돌파 폭 큰 종목). 보유 중에 켜고 끄면 그 포지션이 청산된 뒤부터 적용"),
        "· 거래소: 바이낸스 메인넷 전용 (테스트넷·업비트 불가)".to_string(),
        format!("· 봉: {bar} 마감 기준 (메뉴에서 3분봉~1일봉 중 하나 선택)"),
        String::new(),
        "진입".to_string(),
        format!("· 롱: {}이고 종가가 청산선 위", align_rule(cfg, true)),
        format!("· 숏: {}이고 종가가 청산선 아래", align_rule(cfg, false)),
        format!(
            "· 진입 MA: 지금 {} — ✏️ MA 버튼이나 /tfma 1 5 120 으로 fast·slow·긴MA를 직접 지정 (fast < slow, 긴MA 포함 시 slow < 긴MA, 최대 {})",
            ma_text(cfg),
            config_tradfi::MA_PERIOD_MAX
        ),
        "· 긴MA 포함/빼기 버튼으로 진입 기준에서 긴MA를 넣거나 뺄 수 있음".to_string(),
        "· 진입 방식: 새 배열만(배열이 새로 만들어진 봉에서만) 또는 배열 유지 중(배열이 유지되는 동안 언제든, 청산 후 1봉 대기)".to_string(),
        String::new(),
        "청산".to_string(),
        if exit_off(cfg) {
            "· 청산MA 없음 선택 중: 반대 배열이 새로 생기면 청산 (반대 진입 없음)".to_string()
        } else {
            format!("· 종가가 {exit_ma}MA 반대편으로 마감하면 청산 (청산 MA는 {EXIT_MA_MIN}~{EXIT_MA_MAX} 중 자유 지정: ✏️ 직접 입력 또는 /tfexit 45, '없음'이면 반대 배열에 청산)")
        },
        "· ⏭ 다음 봉부터 변경: 시간봉·진입MA·청산MA·긴MA 포함 여부를 예약하면 지금 봉 마감 뒤 바뀌고, 보유 포지션은 새 시간봉의 다음 마감부터 새 기준으로 관리".to_string(),
        format!("· 수익잠금: {} (켜면 증거금 수익이 단계에 닿을 때마다 그 아래에 거래소 손절을 올림)", lock_text(cfg)),
        String::new(),
        "고정 설정".to_string(),
        format!("· 레버리지 5x ISOLATED · 증거금 가용 잔고의 {margin_text}%"),
        match risk_text(cfg) {
            Some(r) => format!("· 크기: {r} — 진입 때 2×ATR(시간봉 ATR14) 거리에 손절 주문, 그 손절에 맞으면 가용 잔고의 그 %만 잃도록 수량을 정함 (최대 가용 {margin_text}% × 5배, 거래소 최소 주문 미만이면 진입 안 함). 손실한도 1·2·3% 또는 고정 크기 버튼"),
            None => "· 크기: 고정 (손실한도 버튼으로 1회 손실 한도 방식으로 바꿀 수 있음)".to_string(),
        },
        "· 비상손절: 증거금 -50% (Mark 기준, 거래소 주문)".to_string(),
        format!(
            "· 거래정지: 선물계좌 총자본(지갑 + 평가손익)이 {min} USDT 미만이면 신규 진입 정지. 보유 포지션은 청산선·비상손절대로 계속 관리하고, 전략은 ON 유지, {min} USDT 이상으로 돌아오면 자동 재개 (LIVE 모드)",
            min = config_tradfi::MIN_EQUITY_USDT
        ),
        "· SMA25 손절·물타기·일일손실/연패 가드 없음".to_string(),
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
                "adopt_position" => Ok(json!({
                    "ok": true, "market_id": "SKHYUSDT", "side": "SHORT",
                    "quantity": 0.8, "entry_price": 105.76, "leverage": 5, "margin": 16.92,
                    "warnings": ["종가가 청산선 반대편"],
                    "applied": args["apply"].clone(),
                })),
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
    async fn crypto_coin_is_accepted() {
        let fake = Fake::new();
        *fake.resolve.lock().unwrap() =
            Ok(json!({"market_id": "BTCUSDT", "ccxt_symbol": "BTC/USDT:USDT"}));
        let b = bot(&fake);
        let out = b.handle_tfcoin(1, &["BTC".to_string()]).await;
        match &out[0] {
            Outgoing::Send { text, .. } => assert!(text.contains("BTCUSDT"), "{text}"),
            other => panic!("{other:?}"),
        }
        assert_eq!(fake.sets(), vec![json!({"single_symbol": "BTCUSDT"})]);
    }

    #[tokio::test]
    async fn screens_describe_the_18_usdt_entry_stop() {
        let fake = Fake::new();
        let b = bot(&fake);
        let (text, _) = b.handle_tradfi_action("on").await;
        assert!(text.contains("18 USDT 미만이면 신규 진입"), "{text}");
        assert!(!text.contains("-50%가 되면"), "{text}");
        assert!(guard_line().contains("18 USDT 미만"));
        let cfg = fake.config.lock().unwrap().clone();
        let rules = tradfi_rules_text(&cfg);
        assert!(rules.contains("18 USDT 미만이면 신규 진입 정지"), "{rules}");
        assert!(!rules.contains("30일"), "{rules}");
        // an old 30-day halt left in the stored config no longer blocks ON
        fake.config.lock().unwrap()["drawdown_halt_until_ms"] = json!(now_ms() + 86_400_000);
        let (text, kb) = b.handle_tradfi_action("on").await;
        assert!(text.contains("켤까요"), "{text}");
        assert!(callbacks(&kb).iter().any(|(_, d)| d == "tf:confirm_on"));
    }

    #[tokio::test]
    async fn tradfi_coin_is_saved_and_listed() {
        let fake = Fake::new();
        *fake.resolve.lock().unwrap() =
            Ok(json!({"market_id": "CLUSDT", "ccxt_symbol": "CL/USDT:USDT"}));
        let b = bot(&fake);
        let (text, kb) = b.handle_tradfi_action("coin").await;
        assert!(text.contains("코인 모두 가능"), "{text}");
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
        b.handle_tradfi_action("exit:121").await;
        b.handle_tradfi_action("exit:1").await;
        b.handle_tradfi_action("entry:trend").await;
        b.handle_tradfi_action("entry:bogus").await;
        b.handle_tradfi_action("bar:1h").await;
        b.handle_tradfi_action("bar:7h").await;
        b.handle_tradfi_action("plock:on").await;
        assert_eq!(
            fake.sets(),
            vec![
                json!({"exit_ma_period": 50}),
                json!({"entry_mode": "trend"}),
                json!({"timeframe": "1h"}),
                json!({"profit_lock_enabled": true})
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
            .any(|(t, d)| t.starts_with("✅") && d == "tf:slow:on"));
        assert!(cbs
            .iter()
            .any(|(t, d)| t.starts_with("✅") && d == "tf:mode:dry"));
        assert!(cbs.iter().any(|(t, d)| t == "3분봉" && d == "tf:bar:3m"));
        assert!(cbs.iter().any(|(t, d)| t == "1일봉" && d == "tf:bar:1d"));
        assert!(cbs
            .iter()
            .any(|(t, d)| t.starts_with("✅") && d == "tf:plock:on"));
    }

    #[test]
    fn timeframe_labels() {
        assert_eq!(timeframe_label("3m"), "3분봉");
        assert_eq!(timeframe_label("12h"), "12시간봉");
        assert_eq!(timeframe_label("1d"), "1일봉");
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
            "exit:45",
            "exit:custom",
            "mode:live",
            "confirm_live",
            "coin",
            "pick:CLUSDT",
            "bar:1d",
            "exit:none",
        ] {
            let (text, _) = b.handle_tradfi_action(action).await;
            assert!(text.contains("포지션 보유 중"), "{action}: {text}");
        }
        assert!(fake.sets().is_empty());
        let out = b.handle_tfexit(1, &["45".to_string()]).await;
        match &out[0] {
            Outgoing::Send { text, .. } => assert!(text.contains("포지션 보유 중"), "{text}"),
            other => panic!("{other:?}"),
        }
        assert!(fake.sets().is_empty());
    }

    #[tokio::test]
    async fn typed_number_after_pencil_button_sets_exit_ma() {
        let fake = Fake::new();
        let b = bot(&fake);
        let press = json!({"callback_query": {
            "id": "q", "data": "tf:exit:custom",
            "message": {"chat": {"id": 1}, "message_id": 5}
        }});
        let say = |t: &str| json!({"message": {"chat": {"id": 1}, "text": t}});
        let last_text = |out: &Vec<Outgoing>| match out.last() {
            Some(Outgoing::Send { text, .. }) | Some(Outgoing::Edit { text, .. }) => text.clone(),
            other => panic!("{other:?}"),
        };
        // without the prompt, a bare number is not an exit MA
        b.handle_update(&say("45")).await;
        assert!(fake.sets().is_empty());
        // prompt -> bad text keeps waiting -> valid number is stored
        b.handle_update(&press).await;
        let out = b.handle_update(&say("300")).await;
        assert!(last_text(&out).contains("2~120"));
        let out = b.handle_update(&say("45")).await;
        assert!(
            last_text(&out).contains("청산선: 45MA"),
            "{}",
            last_text(&out)
        );
        assert_eq!(fake.sets(), vec![json!({"exit_ma_period": 45})]);
        // the prompt is over: the next number is ordinary text again
        b.handle_update(&say("60")).await;
        assert_eq!(fake.sets().len(), 1);
        // 취소 ends the prompt
        b.handle_update(&press).await;
        let out = b.handle_update(&say("취소")).await;
        assert!(last_text(&out).contains("취소"));
        b.handle_update(&say("60")).await;
        assert_eq!(fake.sets().len(), 1);
        // a command or another button ends it as well
        b.handle_update(&press).await;
        b.handle_update(&say("/tradfi")).await;
        b.handle_update(&say("60")).await;
        b.handle_update(&press).await;
        b.handle_update(&json!({"callback_query": {
            "id": "q2", "data": "tf:status",
            "message": {"chat": {"id": 1}, "message_id": 5}
        }}))
        .await;
        b.handle_update(&say("60")).await;
        assert_eq!(fake.sets().len(), 1);
        // bare /tfexit also waits for the number
        b.handle_update(&say("/tfexit")).await;
        b.handle_update(&say("88")).await;
        assert_eq!(fake.sets().last(), Some(&json!({"exit_ma_period": 88})));
    }

    #[test]
    fn exit_ma_text_parsing() {
        assert_eq!(parse_exit_ma("45"), Some(45));
        assert_eq!(parse_exit_ma(" 45ma "), Some(45));
        assert_eq!(parse_exit_ma("SMA 99"), Some(99));
        assert_eq!(parse_exit_ma("45 MA"), Some(45));
        assert_eq!(parse_exit_ma("2"), Some(2));
        assert_eq!(parse_exit_ma("120"), Some(120));
        for bad in ["", "1", "121", "500", "-5", "4.5", "abc", "45일"] {
            assert_eq!(parse_exit_ma(bad), None, "{bad}");
        }
    }

    #[tokio::test]
    async fn custom_exit_ma_button_and_command() {
        let fake = Fake::new();
        let b = bot(&fake);
        let cfg = fake.config.lock().unwrap().clone();
        let cbs = callbacks(&tradfi_keyboard(&cfg, None));
        assert!(
            cbs.iter()
                .any(|(t, d)| t == "✏️ 직접 입력" && d == "tf:exit:custom"),
            "{cbs:?}"
        );
        // ✏️ only shows the prompt; nothing is stored yet
        let (text, _) = b.handle_tradfi_action("exit:custom").await;
        assert!(text.starts_with(EXIT_MA_PROMPT_HEAD), "{text}");
        assert!(fake.sets().is_empty());
        // any in-range period via the button path or the command
        b.handle_tradfi_action("exit:45").await;
        let out = b.handle_tfexit(1, &["77".to_string()]).await;
        match &out[0] {
            Outgoing::Send { text, .. } => assert!(text.contains("청산선: 77MA"), "{text}"),
            other => panic!("{other:?}"),
        }
        let out = b.handle_tfexit(1, &["300".to_string()]).await;
        match &out[0] {
            Outgoing::Send { text, .. } => assert!(text.contains("2~120"), "{text}"),
            other => panic!("{other:?}"),
        }
        assert_eq!(
            fake.sets(),
            vec![json!({"exit_ma_period": 45}), json!({"exit_ma_period": 77})]
        );
        // a custom period is marked on the ✏️ button, not on a preset
        let cfg = fake.config.lock().unwrap().clone();
        let cbs = callbacks(&tradfi_keyboard(&cfg, None));
        assert!(
            cbs.iter()
                .any(|(t, d)| t == "✅ ✏️ 직접 77MA" && d == "tf:exit:custom"),
            "{cbs:?}"
        );
        assert!(!cbs.iter().any(|(t, d)| t.starts_with("✅")
            && d.starts_with("tf:exit:")
            && d != "tf:exit:custom"));
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

    #[tokio::test]
    async fn manual_position_is_adopted_after_confirmation() {
        let fake = Fake::new();
        let b = bot(&fake);
        let (text, kb) = b.handle_tradfi_action("adopt").await;
        assert!(text.contains("/tradfi에 맡길까요"), "{text}");
        assert!(text.contains("SKHYUSDT 숏"), "{text}");
        assert!(text.contains("청산선 SMA30"), "{text}");
        assert!(text.contains("⚠️ 종가가 청산선 반대편"), "{text}");
        assert!(callbacks(&kb).iter().any(|(_, d)| d == "tf:confirm_adopt"));
        let applied = |fake: &Fake| -> Vec<Value> {
            fake.calls
                .lock()
                .unwrap()
                .iter()
                .filter(|(c, _)| c == "adopt_position")
                .map(|(_, a)| a["apply"].clone())
                .collect()
        };
        assert_eq!(applied(&fake), vec![json!(false)]);
        let (text, _) = b.handle_tradfi_action("confirm_adopt").await;
        assert!(text.starts_with("🤝 /tradfi 관리 시작"), "{text}");
        assert_eq!(applied(&fake), vec![json!(false), json!(true)]);
        assert!(fake.sets().is_empty());
    }

    #[tokio::test]
    async fn sma120_toggle_is_saved() {
        let fake = Fake::new();
        let b = bot(&fake);
        let (text, kb) = b.handle_tradfi_action("slow:off").await;
        assert!(text.contains("긴MA 안 씀"), "{text}");
        assert_eq!(fake.sets(), vec![json!({"align_slow_enabled": false})]);
        assert!(callbacks(&kb)
            .iter()
            .any(|(t, d)| t.starts_with("✅") && d == "tf:slow:off"));
    }

    #[tokio::test]
    async fn next_bar_switch_is_drafted_then_booked() {
        let fake = Fake::new();
        *fake.trade.lock().unwrap() =
            json!({"side": "LONG", "entry_price": 2650.5, "quantity": 0.1});
        fake.config.lock().unwrap()["timeframe"] = json!("30m");
        let b = bot(&fake);
        let (text, kb) = b.handle_tradfi_action("nx").await;
        assert!(text.contains("다음 봉부터 변경"), "{text}");
        assert!(text.contains("30분봉"), "{text}");
        assert!(callbacks(&kb).iter().any(|(_, d)| d == "tf:nx:exit:none"));
        // unchanged draft cannot be booked
        let (text, _) = b.handle_tradfi_action("nx:save").await;
        assert!(text.contains("지금 설정과 같습니다"), "{text}");
        b.handle_tradfi_action("nx:bar:4h").await;
        b.handle_tradfi_action("nx:exit:120").await;
        let (text, kb) = b.handle_tradfi_action("nx:exit:none").await;
        assert!(
            text.contains("바꿀 값: 4시간봉 · MA 1·5·120 · 청산MA 없음"),
            "{text}"
        );
        assert!(callbacks(&kb)
            .iter()
            .any(|(t, d)| t.starts_with("✅") && d == "tf:nx:exit:none"));
        let (text, kb) = b.handle_tradfi_action("nx:save").await;
        assert!(
            text.starts_with("⏭ 예약했습니다: 30분봉 → 4시간봉"),
            "{text}"
        );
        assert!(text.contains("⏭ 예약:"), "{text}");
        assert!(callbacks(&kb)
            .iter()
            .any(|(t, d)| t.contains("예약됨") && d == "tf:nx"));
        let cfg = fake.config.lock().unwrap().clone();
        let sw = &cfg[config_tradfi::NEXT_SWITCH_KEY];
        assert_eq!(sw["timeframe"], json!("4h"));
        assert_eq!(sw["exit_ma_off"], json!(true));
        assert_eq!(sw["from_timeframe"], json!("30m"));
        let at = sw["apply_at_ms"].as_i64().unwrap();
        assert_eq!(at % 1_800_000, 0);
        assert!(at > now_ms() && at <= now_ms() + 1_800_000);
        assert_eq!(cfg[config_tradfi::NEXT_DRAFT_KEY], Value::Null);
        // the live settings are untouched until the engine applies it
        assert_eq!(cfg["timeframe"], json!("30m"));
        let (text, _) = b.handle_tradfi_action("nx:cancel").await;
        assert!(text.starts_with("🗑"), "{text}");
        assert_eq!(
            fake.config.lock().unwrap()[config_tradfi::NEXT_SWITCH_KEY],
            Value::Null
        );
    }

    #[tokio::test]
    async fn entry_ma_periods_are_typed() {
        let fake = Fake::new();
        let b = bot(&fake);
        let (text, kb) = b.handle_tradfi_action("ma").await;
        assert!(text.starts_with(MA_PROMPT_HEAD), "{text}");
        assert!(callbacks(&kb)
            .iter()
            .any(|(t, d)| t.contains("MA 1·5·120") && d == "tf:ma"));
        let (text, _) = b.tf_ma_from_text("5 5").await;
        assert!(text.starts_with("❌"), "{text}");
        let (text, _) = b.tf_ma_from_text("1 120 120").await;
        assert!(text.starts_with("❌"), "{text}");
        let (text, kb) = b.tf_ma_from_text("2 20 200").await;
        assert!(text.contains("MA 2·20·200"), "{text}");
        assert!(callbacks(&kb)
            .iter()
            .any(|(t, d)| t.contains("200MA 포함") && d == "tf:slow:on"));
        // two numbers keep the long MA
        b.tf_ma_from_text("1/10").await;
        assert_eq!(
            fake.sets(),
            vec![
                json!({"ma_fast_period": 2, "ma_slow_period": 20, "ma_long_period": 200}),
                json!({"ma_fast_period": 1, "ma_slow_period": 10, "ma_long_period": 200}),
            ]
        );
        // with the long MA excluded, slow may reach it
        fake.config.lock().unwrap()["align_slow_enabled"] = json!(false);
        let (text, _) = b.tf_ma_from_text("1 200").await;
        assert!(text.contains("MA 1·200 (긴MA 안 씀)"), "{text}");
        // ...but it cannot be switched back on until slow < long
        let (text, _) = b.handle_tradfi_action("slow:on").await;
        assert!(text.starts_with("❌"), "{text}");
        // locked while holding
        *fake.trade.lock().unwrap() = json!({"side": "LONG", "entry_price": 1.0, "quantity": 1.0});
        let (text, _) = b.tf_ma_from_text("1 5 120").await;
        assert!(text.contains("포지션 보유 중"), "{text}");
        let (text, _) = b.handle_tradfi_action("ma").await;
        assert!(text.contains("포지션 보유 중"), "{text}");
    }

    #[tokio::test]
    async fn next_bar_switch_can_change_entry_ma() {
        let fake = Fake::new();
        *fake.trade.lock().unwrap() = json!({"side": "LONG", "entry_price": 1.0, "quantity": 1.0});
        fake.config.lock().unwrap()["timeframe"] = json!("30m");
        let b = bot(&fake);
        let (text, _) = b.handle_tradfi_action("nx:ma").await;
        assert!(text.starts_with(NEXT_MA_PROMPT_HEAD), "{text}");
        let (text, _) = b.tf_next_ma_from_text("1 120 200").await;
        assert!(text.contains("바꿀 값: 30분봉 · MA 1·120·200"), "{text}");
        b.handle_tradfi_action("nx:bar:4h").await;
        let (text, _) = b.handle_tradfi_action("nx:save").await;
        assert!(
            text.starts_with("⏭ 예약했습니다: 30분봉 → 4시간봉 · MA 1·120·200"),
            "{text}"
        );
        let sw = fake.config.lock().unwrap()[config_tradfi::NEXT_SWITCH_KEY].clone();
        assert_eq!(sw["ma_slow_period"], json!(120));
        assert_eq!(sw["ma_long_period"], json!(200));
    }

    #[tokio::test]
    async fn loss_limit_buttons() {
        let fake = Fake::new();
        let b = bot(&fake);
        let (text, kb) = b.handle_tradfi_action("risk:3").await;
        assert!(
            text.starts_with("🛡 손실한도: 켬 — 1회 손실 자본의 3%"),
            "{text}"
        );
        assert!(callbacks(&kb)
            .iter()
            .any(|(t, d)| t.starts_with("✅") && d == "tf:risk:3"));
        let (text, kb) = b.handle_tradfi_action("risk:off").await;
        assert!(text.starts_with("🛡 손실한도: 끔"), "{text}");
        assert!(callbacks(&kb)
            .iter()
            .any(|(t, d)| t.starts_with("✅") && d == "tf:risk:off"));
        // turning it back on keeps the chosen %
        let (text, kb) = b.handle_tradfi_action("risk:on").await;
        assert!(text.contains("자본의 3%"), "{text}");
        assert!(callbacks(&kb)
            .iter()
            .any(|(t, d)| t.starts_with("✅") && d == "tf:risk:on"));
        b.handle_tradfi_action("risk:7").await;
        assert_eq!(
            fake.sets(),
            vec![
                json!({"risk_sizing_enabled": true, "risk_per_trade_pct": 3}),
                json!({"risk_sizing_enabled": false}),
                json!({"risk_sizing_enabled": true, "risk_per_trade_pct": 3}),
            ]
        );
    }

    #[tokio::test]
    async fn auto_scan_toggle() {
        let fake = Fake::new();
        let b = bot(&fake);
        let (text, kb) = b.handle_tradfi_action("auto:on").await;
        assert!(text.starts_with("🔎 자동 종목 탐색: 켬"), "{text}");
        assert!(text.contains("다음 봉 마감부터"), "{text}");
        assert!(text.contains("종목: 🔎 자동 탐색"), "{text}");
        assert!(callbacks(&kb).iter().any(|(_, d)| d == "tf:auto:off"));
        *fake.trade.lock().unwrap() = json!({"side": "LONG", "entry_price": 1.0, "quantity": 1.0});
        let (text, _) = b.handle_tradfi_action("auto:off").await;
        assert!(text.contains("청산된 뒤 다음 진입부터"), "{text}");
        assert_eq!(
            fake.sets(),
            vec![json!({"auto_scan": true}), json!({"auto_scan": false})]
        );
    }

    #[tokio::test]
    async fn exit_line_none_button() {
        let fake = Fake::new();
        let b = bot(&fake);
        let (text, kb) = b.handle_tradfi_action("exit:none").await;
        assert!(text.contains("청산MA 없음"), "{text}");
        assert!(callbacks(&kb)
            .iter()
            .any(|(t, d)| t.starts_with("✅") && d == "tf:exit:none"));
        b.handle_tradfi_action("exit:50").await;
        assert_eq!(
            fake.sets(),
            vec![
                json!({"exit_ma_off": true}),
                json!({"exit_ma_period": 50, "exit_ma_off": false})
            ]
        );
    }

    #[test]
    fn coin_error_texts() {
        assert!(tradfi_coin_error_text("STABLECOIN:USDCUSDT", "USDC").contains("스테이블"));
        assert!(tradfi_coin_error_text("SYMBOL_EMPTY", "").contains("종목을 입력하세요"));
        assert!(
            tradfi_coin_error_text("NOT_TRADING:XUSDT", "X").contains("거래 중인 종목이 아닙니다")
        );
    }
}
