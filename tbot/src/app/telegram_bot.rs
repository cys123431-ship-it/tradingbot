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
    BTC_MA_PERIOD_COMMANDS, MA_CROSS_PERIOD_COMMANDS, PERIOD_PRESETS,
};

const CONFIG_KEY: &str = "btc_ma_cross";
const STRATEGY: &str = "btc_ma";
const DEFAULT_PROFIT_LOCK_SWITCH_COUNT: i64 = 3;

use crate::core::sma_cross::{scale_in_loss_roi, SCALE_IN_STEPS};
use crate::strategy::service::base::{BINANCE_MAINNET, BINANCE_TESTNET, UPBIT_MODE};
const PICK_LIST_SIZE: usize = 12;

pub const TIMEFRAMES: &[&str] = &["3m", "5m", "15m", "30m", "1h", "2h", "4h"];
pub const LEVERAGE_CHOICES: &[i64] = &[2, 3, 4, 5];
use crate::strategy::config_btc_ma::{
    DAILY_LOSS_LIMIT_CHOICES, EXIT_MA_DEFAULT_PERIOD, EXIT_MA_MAX_PERIOD, EXIT_MA_MIN_PERIOD,
    LOSS_STREAK_CHOICES, MARGIN_FRACTION_CHOICES, MIN_BALANCE_CHOICES, STOP_MA_PERIOD,
};
/// Buttons for the custom exit line (Python `EXIT_MA_PRESETS`).
pub const EXIT_MA_PRESETS: &[i64] = &[25, 69];
pub const EMERGENCY_STOP_CHOICES: &[i64] = &[0, 10, 20, 30, 50];
pub const REENTRY_WAIT_CHOICES: &[i64] = &[0, 1, 2, 3, 5];
pub const SLOW_PERIOD_CHOICES: &[i64] = &[60, 120, 200];
const TRADFI_SCOPE: &str = "tradfi";

pub const NETWORK_LABELS: &[(&str, &str)] =
    &[("testnet", "테스트넷(데모)"), ("mainnet", "메인넷(실계좌)")];

/// `/exchange` menu: which exchange/network the futures strategies trade on
/// (Python `/setup` -> "22. 거래소/네트워크 전환"). Upbit is spot-only, so it is
/// offered as a button that explains why the futures bot cannot use it.
pub const EXCHANGE_COMMANDS: &[&str] = &["exchange", "setup", "network"];
/// Python `get_network_status_label`.
pub const EXCHANGE_MODE_LABELS: &[(&str, &str)] = &[
    ("binance_testnet", "테스트넷(데모) 🧪"),
    ("binance_mainnet", "메인넷 💰"),
    ("upbit", "업비트 KRW 현물"),
];

pub fn exchange_mode_label(mode: &str) -> String {
    EXCHANGE_MODE_LABELS
        .iter()
        .find(|(k, _)| *k == mode)
        .map(|(_, v)| v.to_string())
        .unwrap_or_else(|| mode.to_string())
}

/// Engine network id (`testnet`/`mainnet`) or exchange mode id
/// (`binance_testnet`/`binance_mainnet`/`upbit`) as the short net name.
pub fn net_name_of(mode: &str) -> &'static str {
    match mode {
        BINANCE_MAINNET => "mainnet",
        BINANCE_TESTNET => "testnet",
        _ => "",
    }
}

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

pub(crate) fn cfg_get<'a>(cfg: &'a Value, key: &str) -> Option<&'a Value> {
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

pub(crate) fn markup_button(text: impl Into<String>, callback_data: impl Into<String>) -> Value {
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
    let size = cfg_int(cfg, "alt_universe_size", 50);
    let pool = match cfg_str(cfg, "scan_scope", "") {
        "single" => return "ON (코인 지정 중에는 동작 안 함 — 지정 코인만 거래)".to_string(),
        TRADFI_SCOPE => "TradFi 종목 중".to_string(),
        "alts" => format!("알트 상위{size} 중"),
        "all" => "전체 코인 중".to_string(),
        // BTC-only still rotates into the top alts until the next exit.
        _ => format!("BTC + 알트 상위{size} 중 (BTC만 선택이어도 이때는 알트도 진입)"),
    };
    format!(
        "ON — 익절·손절 직후(SMA25 손절 제외) 방금 코인을 뺀 {pool} 거래대금 순 첫 코인에 바로 진입"
    )
}

fn alt_exclude_btc(cfg: &Value) -> bool {
    cfg_get(cfg, "alt_exclude_btc")
        .and_then(|v| v.as_bool())
        .unwrap_or(true)
}

/// How one coin is chosen when the scope scans many.
fn scan_pick_text(cfg: &Value, first: &str) -> String {
    if cfg_str(cfg, "entry_mode", "") == "trend" {
        format!(
            "방향 유지 중 진입이라 모든 코인이 늘 신호 상태 → 거래대금 순 첫 코인(보통 {first})에 진입, \
             재진입 대기·SMA25 조건에 걸리면 다음 코인"
        )
    } else {
        "같은 봉에 여러 코인이 크로스하면 거래대금 1위".to_string()
    }
}

fn scope_text(cfg: &Value) -> String {
    let size = cfg_int(cfg, "alt_universe_size", 50);
    match cfg_str(cfg, "scan_scope", "") {
        TRADFI_SCOPE => format!(
            "🏦 TradFi만 — 바이낸스 메인넷 미국주식 + 금 (선물 상장 종목, 24h 거래대금 상위 {size}, 미국 정규장 시간에만 신규 진입)"
        ),
        "alts" => {
            let (btc, first) = if alt_exclude_btc(cfg) {
                ("BTC 제외", "ETH")
            } else {
                ("BTC 포함", "BTC")
            };
            format!(
                "알트 24h 거래대금 상위 {size}개 ({btc}, 스테이블·TradFi 제외, {})",
                scan_pick_text(cfg, first)
            )
        }
        "all" => format!(
            "바이낸스 USDT 선물 전체 (BTC 포함, 스테이블·TradFi 제외, {})",
            scan_pick_text(cfg, "BTC")
        ),
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

/// True when `resolve_tradfi_coin` accepted `market_id` (a TradFi contract).
fn resolved_tradfi(market_id: &str, resolved: &Result<Value, String>) -> bool {
    matches!(
        resolved,
        Ok(v) if v.get("market_id").and_then(|m| m.as_str()) == Some(market_id)
    )
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
        "NOT_TRADFI" => {
            format!("{symbol}은(는) 지정할 수 있는 TradFi 종목이 아닙니다 (미국주식·금만 가능).")
        }
        "TRADFI_MAINNET_ONLY" => {
            "TradFi 종목은 바이낸스 메인넷에서만 지정할 수 있습니다 (/setup).".to_string()
        }
        _ => format!("코인을 확인하지 못했습니다: {reason}"),
    }
}

/// No position and at least one coin waiting for re-entry.
fn has_reentry_wait(state: Option<&Value>) -> bool {
    let Some(state) = state else {
        return false;
    };
    !py_truthy(state.get("trade"))
        && state
            .get("reentry_block")
            .and_then(|v| v.as_object())
            .is_some_and(|m| !m.is_empty())
}

/// Telegram text for an `adopt_position` refusal.
fn adopt_error_text(result: &Value) -> String {
    let detail = result.get("detail").and_then(|v| v.as_str()).unwrap_or("");
    match result.get("reason").and_then(|v| v.as_str()).unwrap_or("") {
        "NOT_LIVE" => "LIVE 모드에서만 맡길 수 있습니다.".to_string(),
        "STRATEGY_POSITION_EXISTS" => "봇이 이미 포지션을 관리 중입니다.".to_string(),
        "BUSY" => format!("봇이 주문을 처리 중입니다 ({detail}). 잠시 후 다시 눌러 주세요."),
        "NO_POSITION" => "거래소에 열린 포지션이 없습니다.".to_string(),
        "MULTIPLE_POSITIONS" => {
            format!(
                "포지션이 여러 개라 맡길 수 없습니다: {detail}\n하나만 남긴 뒤 다시 눌러 주세요."
            )
        }
        other => format!("포지션을 확인하지 못했습니다: {other} {detail}"),
    }
}

/// Position summary + warnings shown before and after adoption.
fn adopt_summary(result: &Value) -> String {
    let side = result.get("side").and_then(|v| v.as_str()).unwrap_or("");
    let side_text = if side == "LONG" { "롱" } else { "숏" };
    let mut text = format!(
        "{} {side_text} {} @ {} · {}x · 증거금 {:.2} USDT",
        result
            .get("market_id")
            .map(value_display)
            .unwrap_or_default(),
        result
            .get("quantity")
            .map(value_display)
            .unwrap_or_default(),
        result
            .get("entry_price")
            .map(value_display)
            .unwrap_or_default(),
        result
            .get("leverage")
            .map(value_display)
            .unwrap_or_default(),
        result.get("margin").and_then(|v| v.as_f64()).unwrap_or(0.0),
    );
    for w in result
        .get("warnings")
        .and_then(|v| v.as_array())
        .into_iter()
        .flatten()
    {
        text.push_str(&format!("\n⚠️ {}", value_display(w)));
    }
    text
}

fn reentry_text(cfg: &Value) -> String {
    let wait = cfg_int(cfg, "reentry_wait_bars", 3);
    if wait == 0 {
        "반대 크로스까지 대기".to_string()
    } else {
        format!("{wait}봉 대기 후 같은 방향 재진입 (반대 크로스 시 즉시)")
    }
}

fn loss_guard_label(cfg: &Value) -> String {
    let daily = match cfg_int(cfg, "daily_loss_limit_pct", 0) {
        0 => "하루 끔".to_string(),
        n => format!("하루 -{n}%"),
    };
    let streak = match cfg_int(cfg, "max_loss_streak", 0) {
        0 => "연패 끔".to_string(),
        n => format!("{n}연패"),
    };
    format!("{daily} · {streak}")
}

fn loss_guard_text(cfg: &Value) -> String {
    let daily = cfg_int(cfg, "daily_loss_limit_pct", 0);
    let streak = cfg_int(cfg, "max_loss_streak", 0);
    if daily == 0 && streak == 0 {
        return "끔".to_string();
    }
    let mut parts = Vec::new();
    if daily > 0 {
        parts.push(format!("오늘 손실이 시작 잔고의 -{daily}% 이상"));
    }
    if streak > 0 {
        parts.push(format!("오늘 {streak}연패"));
    }
    format!(
        "{}이면 그날 신규 진입 중지, 내일 0시(한국시간) 자동 재개",
        parts.join(" 또는 ")
    )
}

fn min_balance_label(cfg: &Value) -> String {
    match cfg_int(cfg, "min_balance_usdt", 0) {
        0 => "끔".to_string(),
        n => format!("{n} USDT"),
    }
}

fn min_balance_text(cfg: &Value) -> String {
    match cfg_int(cfg, "min_balance_usdt", 0) {
        0 => "끔".to_string(),
        n => format!("LIVE 잔고 {n} USDT 이하면 (포지션 없을 때) 진입 전에 전략 자동 OFF"),
    }
}

fn lookback_text(cfg: &Value) -> String {
    if cfg_int(cfg, "cross_lookback_bars", 1) >= 2 {
        "직전 봉 또는 2봉 전 크로스 (2봉 전은 크로스 방향이 유지될 때만, 직전 봉 크로스 우선)"
            .to_string()
    } else {
        "직전 봉 크로스만".to_string()
    }
}

fn entry_mode_text(cfg: &Value) -> String {
    let (fast, slow) = sma_names(cfg);
    if cfg_str(cfg, "entry_mode", "") == "trend" {
        format!(
            "방향 유지 중 진입 ({fast}이 {slow} 위=롱 / 아래=숏, 수익확보·손절·수동 청산 후 그 코인은 {})",
            reentry_text(cfg)
        )
    } else {
        format!("돌파 시 진입 ({fast}이 {slow}을 뚫는 봉)")
    }
}

/// ("SMA1", "SMA200") from the configured periods.
fn sma_names(cfg: &Value) -> (String, String) {
    (
        format!("SMA{}", cfg_int(cfg, "fast_period", 3)),
        format!("SMA{}", cfg_int(cfg, "slow_period", 200)),
    )
}

/// "1/200 SMA" from the configured periods.
fn strategy_name(cfg: &Value) -> String {
    format!(
        "{}/{} SMA",
        cfg_int(cfg, "fast_period", 3),
        cfg_int(cfg, "slow_period", 200)
    )
}

fn lock_start_text(cfg: &Value) -> String {
    format_g(py_n(cfg_get(cfg, "lock_start_roi_percent")))
}

fn stop_ma_on(cfg: &Value) -> bool {
    cfg_int(cfg, "stop_ma_period", STOP_MA_PERIOD) > 0
}

/// `(enabled, period)` of the custom exit line from the normalized config.
fn exit_ma_setting(cfg: &Value) -> (bool, i64) {
    let enabled = py_truthy(cfg_get(cfg, "exit_ma_enabled"));
    let period = cfg_int(cfg, "exit_ma_period", EXIT_MA_DEFAULT_PERIOD);
    (enabled, period)
}

/// One-line status description (only when on does it explain the behaviour).
fn exit_ma_text(cfg: &Value) -> String {
    let (enabled, period) = exit_ma_setting(cfg);
    let fast = cfg_int(cfg, "fast_period", 3);
    let slow = cfg_int(cfg, "slow_period", 200);
    if !enabled {
        return "OFF (반대 크로스에서만 청산 · 반대 방향 즉시 진입)".to_string();
    }
    format!(
        "ON · SMA{fast}가 SMA{period}을(를) 반대로 돌파하면 청산 \
         · 청산 후 반대 진입 없이 다음 SMA{fast}/SMA{slow} 크로스 마감까지 현금 대기"
    )
}

fn exit_ma_button(cfg: &Value) -> String {
    let (enabled, period) = exit_ma_setting(cfg);
    if enabled {
        format!("🔻 청산 MA: SMA{period}")
    } else {
        "🔻 청산 MA: OFF".to_string()
    }
}

/// Entry description that reflects the custom-exit rule when it is on.
fn entry_mode_text_with_exit_ma(cfg: &Value) -> String {
    let (enabled, _) = exit_ma_setting(cfg);
    if !enabled {
        return entry_mode_text(cfg);
    }
    let (fast, slow) = sma_names(cfg);
    format!(
        "크로스 마감 진입만 ({fast}이 {slow}을 상향 돌파한 봉 마감=롱 / \
         하향 돌파한 봉 마감=숏) — 청산 MA ON 중에는 방향 유지 진입·즉시 반대 진입 없음"
    )
}

fn rotation_text_with_exit_ma(cfg: &Value) -> String {
    if py_truthy(cfg_get(cfg, "exit_ma_enabled")) {
        return "OFF (청산 MA ON 중에는 청산 직후 다른 코인으로 바로 진입하지 않음)".to_string();
    }
    rotation_text(cfg)
}

fn profit_lock_on(cfg: &Value) -> bool {
    cfg_get(cfg, "profit_lock_enabled")
        .and_then(|v| v.as_bool())
        .unwrap_or(true)
}

fn scale_in_text(cfg: &Value) -> String {
    let on = cfg_get(cfg, "scale_in_enabled")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    if !on {
        return "끔".to_string();
    }
    let steps = SCALE_IN_STEPS
        .iter()
        .map(|(roi, _)| format!("-{}%", *roi as i64))
        .collect::<Vec<_>>()
        .join("·");
    let frac = format_g(SCALE_IN_STEPS[0].1);
    let loss = scale_in_loss_roi(cfg_int(cfg, "emergency_stop_roi_percent", 0) as f64) as i64;
    format!(
        "켬 (봉 마감 때 첫 진입가 대비 증거금 {steps} 손실 + 추세 유지면 첫 수량의 {frac}배씩 추가, 손절은 첫 증거금 -{loss}%)",
    )
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
    pub(crate) engine: Arc<A>,
    owner_chat_id: i64,
    offset: i64,
    #[allow(dead_code)] // reserved for text-input prompts
    pending_input: Mutex<HashMap<i64, String>>,
}

impl<A: EngineApi + 'static> TelegramBot<A> {
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
        let (text, markup) = if let Some(rest) = data.strip_prefix("bm:") {
            self.handle_btc_ma_cross_action(rest).await
        } else if let Some(rest) = data.strip_prefix("mc:") {
            self.handle_ma_cross_action(rest).await
        } else {
            return out;
        };
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
            "macross" => {
                let (status, markup) = self.ma_cross_status_screen().await;
                vec![Outgoing::Send {
                    chat_id,
                    text: status,
                    markup: Some(markup),
                }]
            }
            other if EXCHANGE_COMMANDS.contains(&other) => {
                let (text, markup) = self.exchange_menu().await;
                vec![Outgoing::Send {
                    chat_id,
                    text,
                    markup: Some(markup),
                }]
            }
            "start" | "help" => self.handle_help(chat_id).await,
            "stop" => self.handle_stop(chat_id).await,
            other if BTC_MA_PERIOD_COMMANDS.contains(&other) => {
                self.handle_period_cmd(chat_id, &args).await
            }
            other if MA_CROSS_PERIOD_COMMANDS.contains(&other) => {
                self.handle_ma_period_cmd(chat_id, &args).await
            }
            // A command we do not serve must never be answered with silence:
            // the user cannot tell a dead bot from an unimplemented command.
            other if !other.is_empty() => {
                let text = match unknown_command_note(other) {
                    Some(note) => format!("⚠️ /{other} 명령은 이 봇(tbot)에 없습니다.\n{note}"),
                    None => format!(
                        "⚠️ 알 수 없는 명령입니다: /{other}\n/help 로 사용 가능한 명령을 확인하세요."
                    ),
                };
                vec![Outgoing::Send {
                    chat_id,
                    text,
                    markup: None,
                }]
            }
            _ => Vec::new(),
        }
    }

    async fn handle_help(&self, chat_id: i64) -> Vec<Outgoing> {
        let cfg = self.config().await;
        let ma_cfg = self.ma_config().await;
        let text = format!(
            "📖 사용 가능한 명령\n\
             /btcmacross — {} 크로스 전략 메뉴\n\
             /macross — {} 크로스 + 변동성 타겟 사이징 전략 메뉴\n\
             /macoin <코인> — /btcmacross 거래 코인 1개 지정 (예: /macoin SOL)\n\
             /btcma <패스트> <슬로우> — /btcmacross SMA 기간 변경 (예: /btcma 1 200)\n\
             /btcma exit <기간> — /btcmacross 청산 MA 변경, 끄기는 /btcma exit off (예: /btcma exit 69)\n\
             /btcmaperiod — SMA 기간 도움말\
             /exchange — 거래소/네트워크 전환 (바이낸스 테스트넷 · 메인넷, 구 /setup)\n\
             /map <패스트> <슬로우> — /macross SMA 기간 변경 (예: /map 1 200)\n\
             /stop — 두 전략 모두 신규 진입 OFF + 보유 포지션 청산\n\
             /help — 이 목록 (없는 명령은 이 봇이 모른다고 답합니다)",
            strategy_name(&cfg),
            strategy_name(&ma_cfg),
        );
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
        let exit_keyword = args
            .first()
            .map(|a| a.trim().to_lowercase())
            .is_some_and(|a| matches!(a.as_str(), "exit" | "xma" | "exitma" | "청산"));
        if exit_keyword {
            let text = match parse_exit_ma_command(args) {
                Ok((enabled, period)) => {
                    self.set_exit_ma(enabled, period).await;
                    let cfg = self.config().await;
                    let (status, _) = self.status_screen().await;
                    format!("🔻 청산 MA: {}\n\n{status}", exit_ma_text(&cfg))
                }
                Err(e) => format!("⚠️ 청산 MA 설정 실패: {e}\n예) /btcma exit 25 · /btcma exit off"),
            };
            let (_, markup) = self.status_screen().await;
            return vec![Outgoing::Send {
                chat_id,
                text,
                markup: Some(markup),
            }];
        }
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

    // ------------------------------------------------------ exchange switch

    /// Current `api.exchange_mode` from the config file ("" if unreadable).
    async fn exchange_mode_now(&self) -> String {
        self.engine_call("exchange_mode", json!({}))
            .await
            .ok()
            .and_then(|v| v.get("mode").and_then(|m| m.as_str()).map(|s| s.to_string()))
            .unwrap_or_default()
    }

    /// One line for the switch screen: positions that stop the switch.
    fn held_positions_lines(preview: &Value) -> (Vec<String>, bool) {
        let mut positions: Vec<&Value> = preview
            .get("positions")
            .and_then(|v| v.as_array())
            .map(|a| a.iter().collect())
            .unwrap_or_default();
        let tradfi: Vec<&Value> = preview
            .get("tradfi_positions")
            .and_then(|v| v.as_array())
            .map(|a| a.iter().collect())
            .unwrap_or_default();
        for p in &tradfi {
            if !positions.iter().any(|q| std::ptr::eq(*q, *p)) {
                positions.push(p);
            }
        }
        if positions.is_empty() {
            return (vec!["· 보유 포지션: 없음".to_string()], false);
        }
        let mut lines = vec!["· ⚠️ 보유 포지션이 있어 전환할 수 없습니다. 먼저 청산하세요:".to_string()];
        for p in &positions {
            lines.push(format!(
                "  - {} {} {} {}개 ({})",
                p.get("strategy").and_then(|v| v.as_str()).unwrap_or(""),
                p.get("symbol").and_then(|v| v.as_str()).unwrap_or(""),
                p.get("side").and_then(|v| v.as_str()).unwrap_or(""),
                p.get("contracts").and_then(|v| v.as_f64()).unwrap_or(0.0),
                p.get("network").and_then(|v| v.as_str()).unwrap_or(""),
            ));
        }
        (lines, true)
    }

    /// What the switch keeps/changes (single coin + watchlist) for one mode.
    fn exchange_plan_lines(plan: Option<&Value>) -> Vec<String> {
        let mut lines = Vec::new();
        for changes in plan.and_then(|p| p.as_array()).into_iter().flatten() {
            for change in changes.as_array().into_iter().flatten() {
                lines.push(format!(
                    "· ⚠️ {}: {} — {} → {}",
                    change.get("strategy").and_then(|v| v.as_str()).unwrap_or(""),
                    change.get("field").and_then(|v| v.as_str()).unwrap_or(""),
                    change.get("from").and_then(|v| v.as_str()).unwrap_or(""),
                    change.get("to").and_then(|v| v.as_str()).unwrap_or(""),
                ));
            }
        }
        lines
    }

    fn exchange_menu_keyboard(network: &str) -> Value {
        markup(vec![
            vec![
                markup_button(mark(network == "testnet", "🧪 바이낸스 테스트넷"), "bm:exch:testnet"),
                markup_button(mark(network == "mainnet", "💰 바이낸스 메인넷"), "bm:exch:mainnet"),
            ],
            vec![markup_button("🏦 업비트 KRW 현물", "bm:exch:upbit")],
            vec![markup_button("📊 상태로", "bm:status")],
        ])
    }

    /// `/exchange` (alias `/setup`): which exchange/network the bot trades on.
    pub async fn exchange_menu(&self) -> (String, Value) {
        let mode = self.exchange_mode_now().await;
        let network = match mode.as_str() {
            BINANCE_TESTNET => "testnet",
            BINANCE_MAINNET => "mainnet",
            _ => "",
        };
        let cfg = self.config().await;
        let label = if mode.is_empty() {
            "읽을 수 없음".to_string()
        } else {
            exchange_mode_label(&mode)
        };
        let text = format!(
            "🔄 거래소/네트워크 전환 (구 /setup)\n\
             지금: {label}\n\
             \n\
             · 🧪 테스트넷(데모): 바이낸스 선물 테스트넷. 실제 주문 없음, /btcmacross에서 LIVE를 켜도 모의 체결만.\n\
             · 💰 메인넷: 바이낸스 선물 실계좌. LIVE면 실제 주문이 나갑니다.\n\
             · 🏦 업비트: 현물만 지원 — 선물 전략(SMA 크로스)은 사용할 수 없습니다.\n\
             \n\
             바꾸면 두 전략(/btcmacross·/macross) 모두 다음 판단부터 새 거래소를 씁니다.\n\
             모드는 {} 그대로입니다.\n\
             보유 포지션이 있으면 전환을 막습니다 (SL/TP 관리가 끊기지 않도록).",
            cfg_str(&cfg, "trading_mode", "")
        );
        (text, Self::exchange_menu_keyboard(network))
    }

    /// `bm:exch:*`: the switch screen, a step before anything is written.
    async fn exchange_action(&self, action: &str) -> (String, Value) {
        let heard = action.trim().to_lowercase();
        // Buttons speak network names (`testnet`); the engine speaks modes.
        let plan = match heard.as_str() {
            "testnet" => BINANCE_TESTNET,
            "mainnet" => BINANCE_MAINNET,
            other => other,
        };
        if plan == UPBIT_MODE {
            let mode = self.exchange_mode_now().await;
            let network = match mode.as_str() {
                BINANCE_TESTNET => "testnet",
                BINANCE_MAINNET => "mainnet",
                _ => "",
            };
            return (
                "🏦 업비트 KRW 현물\n\n\
                 이 봇의 전략(/btcmacross·/macross)은 바이낸스 선물 전용입니다.\n\
                 업비트는 현물 거래소라 레버리지·숏·선물 주문이 없어 사용할 수 없습니다.\n\
                 (파이썬 봇도 같은 이유로 선물 전략에서 업비트를 막았습니다.)\n\n\
                 거래소: 바이낸스 테스트넷(데모) 또는 메인넷을 선택하세요."
                    .to_string(),
                Self::exchange_menu_keyboard(network),
            );
        }
        if plan != BINANCE_TESTNET && plan != BINANCE_MAINNET {
            return self.exchange_menu().await;
        }
        let preview = match self
            .engine_call("set_exchange_mode", json!({"mode": plan, "dry_run": true}))
            .await
        {
            Ok(v) => v,
            Err(e) => {
                let mode = self.exchange_mode_now().await;
                let network = if mode == BINANCE_MAINNET { "mainnet" } else { "testnet" };
                return (
                    format!("⚠️ 거래소 전환 확인 실패: {e}"),
                    Self::exchange_menu_keyboard(network),
                );
            }
        };
        if preview.get("ok").and_then(|v| v.as_bool()) != Some(true) {
            let message = preview
                .get("message")
                .and_then(|v| v.as_str())
                .unwrap_or("거래소를 전환할 수 없습니다.");
            return (message.to_string(), Self::exchange_menu_keyboard(""));
        }
        let mode = preview.get("current").and_then(|v| v.as_str()).unwrap_or("");
        let (mut lines, _) = Self::held_positions_lines(&preview);
        if preview.get("same").and_then(|v| v.as_bool()) == Some(true) {
            lines.insert(0, "ℹ️ 이미 이 거래소를 사용 중입니다.".to_string());
        }
        let cooldown = preview
            .get("cooldown_text")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if !cooldown.is_empty() {
            lines.push(format!("· {cooldown}"));
        }
        let cfg = self.config().await;
        let mode_now = cfg_str(&cfg, "trading_mode", "");
        let mut body = format!(
            "🔄 {}로 전환할까요?\n\
             지금: {} · 모드 {} ({})\n\
             {}\n\
             · 바꾸면 이 봇의 모든 전략이 다음 판단부터 새 거래소를 씁니다.\n\
             · LIVE에서 메인넷이면 실제 주문이 나갑니다.",
            network_label(net_name_of(plan)),
            exchange_mode_label(mode),
            mode_now,
            if mode_now == "LIVE" { "실주문" } else { "가상" },
            lines.join("\n")
        );
        let network = net_name_of(plan);
        let network = if network.is_empty() {
            net_name_of(&self.exchange_mode_now().await)
        } else {
            network
        };
        let plan_lines = Self::exchange_plan_lines(preview.get("plan"));
        if !plan_lines.is_empty() {
            body.push_str(&format!("\n\n전환 시 조정:\n{}", plan_lines.join("\n")));
        }
        let held = lines.iter().any(|l| l.contains("보유 포지션이 있어"));
        let keyboard = if held || preview.get("same").and_then(|v| v.as_bool()) == Some(true) {
            Self::exchange_menu_keyboard(network)
        } else {
            markup(vec![
                vec![
                    markup_button(
                        format!("✅ {}로 전환", network_label(network)),
                        format!("bm:exch:confirm:{plan}"),
                    ),
                    markup_button("취소", "bm:exchange"),
                ],
            ])
        };
        (body, keyboard)
    }

    /// `bm:exch:confirm:<net>`: write `api.exchange_mode`, verify, else roll back.
    async fn exchange_switch(&self, target: &str) -> (String, Value) {
        let target = match target.trim().to_lowercase().as_str() {
            "testnet" => BINANCE_TESTNET,
            "mainnet" => BINANCE_MAINNET,
            other => other,
        }
        .to_string();
        let result = self
            .engine_call("set_exchange_mode", json!({"mode": target}))
            .await;
        let (cfg_now, network, text) = match result {
            Ok(v) => {
                let mode = v.get("mode").and_then(|m| m.as_str()).unwrap_or(target.as_str());
                let network = v.get("network").and_then(|n| n.as_str()).unwrap_or("");
                let mut lines = vec![
                    format!("✅ 거래소/네트워크 전환 완료: {}", exchange_mode_label(mode)),
                    String::new(),
                ];
                let plan_lines = Self::exchange_plan_lines(v.get("plan"));
                if plan_lines.is_empty() {
                    lines.push("· 감시목록·단일 코인은 그대로입니다.".to_string());
                } else {
                    lines.push("감시목록·단일 코인을 현재 거래소 기준으로 정리했습니다.".to_string());
                    lines.extend(plan_lines);
                }
                lines.push(String::new());
                lines.push(format!(
                    "모드: {} (그대로) · API 키: {}",
                    cfg_str(&self.config().await, "trading_mode", ""),
                    self.exchange_badge().await
                ));
                (self.config().await, network.to_string(), lines.join("\n"))
            }
            Err(e) => {
                let mode = self.exchange_mode_now().await;
                let network = if mode == BINANCE_MAINNET { "mainnet" } else { "testnet" };
                (
                    self.config().await,
                    network.to_string(),
                    format!("❌ 거래소 전환 실패: {e}"),
                )
            }
        };
        let state = self.load_state_or_empty().await;
        let markup = if network.is_empty() {
            Self::exchange_menu_keyboard("")
        } else {
            self.build_keyboard(&cfg_now, None, Some(&state))
        };
        (text, markup)
    }

    async fn exchange_badge(&self) -> String {
        match self.engine_call("status", json!({})).await {
            Ok(v) => {
                if py_truthy(v.get("has_credentials")) {
                    "있음".to_string()
                } else {
                    "없음".to_string()
                }
            }
            Err(_) => "확인 실패".to_string(),
        }
    }

    /// Full stop: disable new entries, close this strategy's position in the
    /// current mode, then read config and both modes' state back to prove it.
    async fn full_off(&self) -> String {
        if let Err(e) = self.set_coin_value("enabled", json!(false)).await {
            return Self::engine_error_text(&e);
        }
        let name = strategy_name(&self.config().await);
        let mut lines = vec![format!("🛑 {name} 크로스 완전 끄기")];
        match self
            .engine_call("close_position", json!({ "reason": "manual" }))
            .await
        {
            Ok(v) => {
                let action = v.get("action").map(value_display).unwrap_or_default();
                let reason = v.get("reason").map(value_display).unwrap_or_default();
                if reason == "NO_POSITION" {
                    lines.push("· 청산: 지금 모드에 보유 포지션 없음".to_string());
                } else {
                    lines.push(format!("· 청산 요청: {action} {reason}"));
                }
            }
            Err(e) => lines.push(format!("· ⚠️ 청산 실패: {e}")),
        }

        let cfg = self.config().await;
        if cfg_get(&cfg, "enabled").is_none() {
            lines.push("· ⚠️ 설정을 다시 읽지 못했습니다 — 엔진 상태를 확인하세요".to_string());
        } else if py_truthy(cfg_get(&cfg, "enabled")) {
            lines.push("· ⚠️ 설정이 아직 ON 입니다 — 다시 눌러 주세요".to_string());
        } else {
            lines.push("· 신규 진입: OFF 확인".to_string());
        }
        let network = self.engine_call("current_network", json!({})).await;
        if matches!(network, Ok(Value::String(_))) {
            for mode in ["LIVE", "DRY_RUN"] {
                match self
                    .engine_call("load_state", json!({ "mode": mode }))
                    .await
                {
                    Ok(v) if py_truthy(v.get("trade")) => lines.push(format!(
                        "· ⚠️ {mode} 포지션 남아 있음 — 모드를 {mode}로 바꾼 뒤 다시 완전 끄기"
                    )),
                    Ok(_) => lines.push(format!("· {mode} 포지션: 없음")),
                    Err(e) => lines.push(format!("· ⚠️ {mode} 상태 확인 실패: {e}")),
                }
            }
        }
        let status = self.format_status().await;
        format!(
            "{}

{status}",
            lines.join(
                "
"
            )
        )
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
        let network = cfg_str(cfg, "network", "");
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
        rows.push(vec![markup_button(
            "🛑 완전 끄기 (진입 OFF + 포지션 청산)",
            "bm:full_off",
        )]);
        let holding = state.is_some_and(|s| py_truthy(s.get("trade")));
        if trading_mode == "LIVE" && !holding {
            rows.push(vec![markup_button(
                "🤝 수동 포지션 관리 맡기기",
                "bm:adopt",
            )]);
        }
        if has_reentry_wait(state) {
            rows.push(vec![markup_button(
                "🔓 재진입 대기 해제",
                "bm:reentry_clear",
            )]);
        }
        rows.push(vec![
            markup_button(mark(trading_mode == "DRY_RUN", "🧪 DRY_RUN"), "bm:mode:dry"),
            markup_button(mark(trading_mode == "LIVE", "💥 LIVE"), "bm:mode:live"),
        ]);
        // Exchange/network line (network == "" means an unreadable mode).
        if !network.is_empty() {
            let after = if network == "mainnet" { "testnet" } else { "mainnet" };
            rows.push(vec![
                markup_button(
                    mark(network == "testnet", "🧪 바이낸스 테스트넷"),
                    "bm:exch:testnet",
                ),
                markup_button(
                    mark(network == "mainnet", "💰 바이낸스 메인넷"),
                    "bm:exch:mainnet",
                ),
                markup_button(format!("🔄 {after}로 전환"), format!("bm:exch:confirm:{after}")),
            ]);
        } else {
            rows.push(vec![markup_button("🔄 거래소/네트워크 전환", "bm:exchange")]);
        }
        let pick_label = if scan_scope == "single" {
            mark(true, &format!("🎯 {single}"))
        } else {
            "🎯 코인 지정".to_string()
        };
        rows.push(vec![
            markup_button(
                mark(
                    !matches!(scan_scope, "alts" | "all" | "single" | TRADFI_SCOPE),
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
            mark(scan_scope == "all", "🛰 전체 코인 스캔 (BTC 포함)"),
            "bm:scope:all",
        )]);
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
        let (exit_on, exit_period) = exit_ma_setting(cfg);
        rows.push(vec![markup_button(
            exit_ma_button(cfg),
            if exit_on { "bm:xma:off" } else { "bm:xma:on" },
        )]);
        let mut exit_row: Vec<Value> = EXIT_MA_PRESETS
            .iter()
            .map(|period| {
                markup_button(
                    mark(exit_on && exit_period == *period, &format!("청산 MA {period}")),
                    format!("bm:xmap:{period}"),
                )
            })
            .collect();
        exit_row.push(markup_button("✏️ 청산 MA 직접 입력", "bm:xma_help"));
        rows.push(exit_row);
        rows.push(vec![markup_button(
            format!("💰 최소 잔고: {}", min_balance_label(cfg)),
            "bm:minbal",
        )]);
        rows.push(vec![markup_button(
            format!("🛡 손실 한도: {}", loss_guard_label(cfg)),
            "bm:guard",
        )]);
        let lookback_on = cfg_int(cfg, "cross_lookback_bars", 1) >= 2;
        rows.push(vec![markup_button(
            mark(lookback_on, "⏮ 2봉 전 크로스도 진입"),
            if lookback_on { "bm:lb:1" } else { "bm:lb:2" },
        )]);
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
            markup_button(
                mark(stop_ma_on(cfg), "🧱 SMA25 손절"),
                if stop_ma_on(cfg) {
                    "bm:stopma:off"
                } else {
                    "bm:stopma:on"
                },
            ),
            markup_button(
                mark(profit_lock_on(cfg), "🔒 수익 잠금"),
                if profit_lock_on(cfg) {
                    "bm:plock:off"
                } else {
                    "bm:plock:on"
                },
            ),
        ]);
        {
            let on = cfg_get(cfg, "scale_in_enabled")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            rows.push(vec![markup_button(
                mark(on, "➕ 물타기 (추세 유지 시)"),
                if on {
                    "bm:scalein:off"
                } else {
                    "bm:scalein:on"
                },
            )]);
        }
        let current = (py_n(cfg_get(cfg, "margin_fraction")) * 100.0).round() as i64;
        rows.push(
            MARGIN_FRACTION_CHOICES
                .iter()
                .map(|n| {
                    markup_button(
                        mark(current == *n, &format!("증거금 {n}%")),
                        format!("bm:mf:{n}"),
                    )
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
                log_line(&format!("btc_ma coin list unavailable: {e}"));
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
            return "📏 SMA 크로스 전략 (/btcmacross)\n\
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
                "거래소: {} (/exchange 변경) · API 키: {}",
                network_label(network),
                if has_credentials { "있음" } else { "없음" }
            ),
            "다른 전략: /macross와 동시에 켜지지 않음 (켜면 /macross 신규 진입 OFF)".to_string(),
            format!("종목: {}", scope_text(cfg)),
            format!("진입: {}", entry_mode_text_with_exit_ma(cfg)),
            format!("청산 MA: {}", exit_ma_text(cfg)),
            format!(
                "익절 자동 전환: {}",
                profit_lock_switch_text(cfg, Some(state))
            ),
            format!("청산 후 다른 코인 전환: {}", rotation_text_with_exit_ma(cfg)),
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
            if profit_lock_on(cfg) {
                format!(
                    "수익 확보 손절(증거금 기준, Last 기준 트리거): {}",
                    lock_table(cfg)
                )
            } else {
                "수익 확보 손절: 끔".to_string()
            },
            format!("SMA25 손절: {}", if stop_ma_on(cfg) { "켬" } else { "끔" }),
            format!("물타기: {}", scale_in_text(cfg)),
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
                _ if !profit_lock_on(cfg) => "끔".to_string(),
                _ => format!("아직 없음 (+{}% 도달 전)", lock_start_text(cfg)),
            };
            lines.push(format!("비상손절 {stop_price} · 수익 확보 {lock_desc}"));
            let trade_fast = py_int_of(trade.get("fast_period"));
            let basis = if py_truthy(trade.get("exit_ma_enabled")) {
                let exit_period = match py_int_of(trade.get("exit_ma_period")) {
                    0 => EXIT_MA_DEFAULT_PERIOD,
                    p => p,
                };
                format!("청산 기준 SMA{trade_fast}/SMA{exit_period} (청산 MA)")
            } else {
                let trade_slow = match py_int_of(trade.get("slow_period")) {
                    0 => 200,
                    p => p,
                };
                format!("청산 기준 SMA{trade_fast}/SMA{trade_slow} (반대 크로스)")
            };
            lines.push(basis);
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
            format!(
                "📈 {} 성과 ({mode} · {network})",
                strategy_name(status.get("config").unwrap_or(&empty))
            ),
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
        let (fast_name, slow_name) = sma_names(&self.config().await);
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
                    } else if matches!(
                        event.get("scope").and_then(|v| v.as_str()),
                        Some("alts" | "all")
                    ) {
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
                        detail.push_str(&format!(" · {fast_name} {fast} / {slow_name} {slow}"));
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
        let scale_line = scale_in_text(cfg);
        let sma25_line = if stop_ma_on(cfg) {
            "• 진입 조건: 종가가 SMA25 기준 진입 방향 쪽에 있을 때만 (반대편이면 그 신호는 건너뜀)\n\
             • SMA25 손절: 봉 마감 종가가 SMA25를 반대로 이탈하면 시장가 청산, 이후 새 크로스까지 재진입 안 함 (같은 봉의 반대 크로스가 우선)\n"
                .to_string()
        } else {
            "• SMA25 손절: 끔 (SMA25 진입 조건도 없음)\n".to_string()
        };
        let lock_lines = if profit_lock_on(cfg) {
            let after = if cfg_str(cfg, "entry_mode", "") == "trend" {
                format!("그 코인은 {}", reentry_text(cfg))
            } else {
                "다음 크로스까지 대기".to_string()
            };
            format!(
                "• 수익 확보(증거금 기준): {} (항상 도달 칸 {gap}% 아래, 한 번 올라간 손절은 내려가지 않음)\n\
                 • 수익 확보 손절로 청산되면 {after}\n",
                lock_table(cfg),
            )
        } else {
            "• 수익 확보: 끔\n".to_string()
        };
        let exit_ma_on = py_truthy(cfg_get(cfg, "exit_ma_enabled"));
        let cross_line = if exit_ma_on {
            format!(
                "• 청산: 반대 크로스(SMA{fast} ↓/↑ SMA{slow})에서 시장가 청산 (반대 진입 없음, 다음 크로스 마감까지 대기)\n"
            )
        } else {
            format!(
                "• 청산: 반대 크로스(SMA{fast} ↓/↑ SMA{slow})에서 시장가 청산 후 즉시 반대 방향 진입\n"
            )
        };
        let hold_line = if exit_ma_on {
            "• 청산 MA ON: 청산 MA 크로스와 반대 크로스 중 먼저 닿는 쪽에서 시장가(reduce-only) 청산, \
             반대 진입은 하지 않고 다음 크로스 마감까지 현금 대기\n"
        } else {
            "• 보유 중 반대 크로스(또는 봉 마감 때 SMA가 반대편): 시장가(reduce-only) 청산 후 즉시 반대 방향 진입\n"
        };
        format!(
            "📘 {fast}/{slow} SMA 크로스 규칙 (기간은 /btcma 또는 버튼으로 변경)\n\
             • 종목: {} (🎯 코인 지정 또는 /macoin SOL 로 1개 지정)\n\
             • 진입: {}\n\
             {cross_line}\
             • 추가 청산 MA: {}\n\
             • 익절 자동 전환: {}\n\
             • 청산 후 다른 코인 전환: {}\n\
             • 봉: {} 마감봉 기준, 진입·청산(반대 크로스) 모두 같은 봉 (3분~4시간 선택)\n\
             {hold_line}\
             {sma25_line}\
             • 크로스 진입 범위: {}\n\
             • 최소 잔고: {}\n\
             • 손실 한도: {}\n\
             {lock_lines}\
             • 물타기: {scale_line}\n\
             • 비상손절: {stop_line} (메뉴에서 선택, 거래소에 Mark 기준으로 걸림)\n\
             • 레버리지 {leverage}배 ISOLATED 선택(2~5배, 다음 진입부터), 가용 증거금의 {margin:.0}% 사용, 계좌 전체 1포지션\n\
             • 🏦 TradFi만: 바이낸스 메인넷 전용 · 미국주식 TradFi + 금만 · 미국 정규장 시간에만 신규 진입\n\
             • 거래소는 /setup 선택을 따르고, 켜면 /macross 전략은 신규 진입 OFF\n\
             • 수익을 보장하지 않는 전략입니다.",
            scope_text(cfg),
            entry_mode_text_with_exit_ma(cfg),
            exit_ma_text(cfg),
            profit_lock_switch_text(cfg, None),
            rotation_text_with_exit_ma(cfg),
            timeframe_labels(cfg_str(cfg, "timeframe", "")),
            lookback_text(cfg),
            min_balance_text(cfg),
            loss_guard_text(cfg),
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
            "\n(보유 중인 포지션도 다음 봉 마감부터 새 기간으로 반대 크로스를 판단 — 그때 패스트가 반대편이면 청산 후 반대 진입)"
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
                    "SYMBOL_EMPTY"
                        | "SYMBOL_NOT_FOUND"
                        | "NOT_TRADING"
                        | "STABLECOIN"
                        | "TRADFI"
                        | "NOT_TRADFI"
                        | "TRADFI_MAINNET_ONLY"
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
        let tradfi = resolved_tradfi(
            &market_id,
            &self
                .engine_call(
                    "resolve_tradfi_coin",
                    json!({ "network": network, "text": market_id }),
                )
                .await,
        );
        if tradfi {
            note.push_str("\n🏦 TradFi 종목: 미국 정규장이 닫혀 있어도 신호가 나면 진입합니다.");
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

    /// Store the custom exit line; clears stale blocks when switching off and
    /// re-arms the newest closed candle when switching on in LIVE. Python
    /// `_set_btc_ma_cross_exit_ma`, but the writes go out as one config change
    /// so the engine never sees `enabled` without its period.
    async fn set_exit_ma(&self, enabled: bool, period: i64) {
        let mut changes = Map::new();
        changes.insert("exit_ma_period".to_string(), json!(period));
        changes.insert("exit_ma_enabled".to_string(), json!(enabled));
        if let Err(e) = self
            .engine_call("config_set", json!({ "changes": changes }))
            .await
        {
            log_line(&format!("telegram: exit MA config_set failed: {e}"));
            return;
        }
        let mode = match self.engine_call("mode", json!({})).await {
            Ok(Value::String(m)) => m,
            _ => return,
        };
        let mut state = match self.engine_call("load_state", json!({ "mode": mode })).await {
            Ok(Value::Object(m)) => m,
            _ => return,
        };
        let mut changed = false;
        if !enabled && py_truthy(state.get("reentry_block")) {
            state.insert("reentry_block".to_string(), Value::Object(Map::new()));
            changed = true;
        }
        if enabled
            && mode == "LIVE"
            && state
                .get("last_evaluated_bar")
                .map(|v| py_int_of(Some(v)) != 0)
                .unwrap_or(false)
        {
            // Re-evaluate the newest closed candle against the new line.
            state.insert("last_evaluated_bar".to_string(), json!(0));
            changed = true;
        }
        if changed {
            let network = match self.engine_call("current_network", json!({})).await {
                Ok(Value::String(n)) => n,
                Ok(_) => return,
                Err(e) => {
                    log_line(&format!("telegram: exit MA network lookup failed: {e}"));
                    return;
                }
            };
            if let Err(e) = self
                .engine_call(
                    "save_state",
                    json!({ "network": network, "mode": mode, "state": state }),
                )
                .await
            {
                log_line(&format!("telegram: exit MA save_state failed: {e}"));
            }
        }
    }

    /// Drop any waiting re-entry before a manual close, mirroring Python's
    /// `confirm_close` branch.
    async fn clear_reentry_if_blocked(&self) -> Result<(), String> {
        let mode = match self.engine_call("mode", json!({})).await? {
            Value::String(m) => m,
            _ => return Ok(()),
        };
        let mut state = match self.engine_call("load_state", json!({ "mode": mode })).await? {
            Value::Object(m) => m,
            _ => return Ok(()),
        };
        if !py_truthy(state.get("reentry_block")) {
            return Ok(());
        }
        state.insert("reentry_block".to_string(), Value::Object(Map::new()));
        let network = match self.engine_call("current_network", json!({})).await? {
            Value::String(n) => n,
            _ => return Ok(()),
        };
        self.engine_call(
            "save_state",
            json!({ "network": network, "mode": mode, "state": state }),
        )
        .await
        .map(|_| ())
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
        if let Some(rest) = action.strip_prefix("exch:") {
            return match rest.strip_prefix("confirm:") {
                Some(target) => self.exchange_switch(target).await,
                None => self.exchange_action(rest).await,
            };
        }
        if action == "exchange" {
            return self.exchange_menu().await;
        }
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
                    "⚠️ {} 크로스 전략을 켤까요?\n\
                     모드 {mode_now} · {} (/setup) · {}\n\
                     종목 {} · {entry}\n\
                     켜면 /macross 전략의 신규 진입이 OFF 됩니다 (그쪽 보유 포지션 관리는 계속).\n{}",
                    strategy_name(&cfg),
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
                let note = "\n/macross가 켜져 있었다면 신규 진입을 OFF 했습니다 (보유 포지션 관리는 계속).";
                let status = self.format_status().await;
                (
                    format!("✅ {} 전략 ON{note}\n\n{status}", strategy_name(&cfg)),
                    kb!(),
                )
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
            "full_off" => (
                format!(
                    "⚠️ {} 크로스를 완전히 끌까요?
· 신규 진입 OFF
· 지금 모드에 이 전략의 포지션이 있으면 시장가(reduce-only)로 청산
· 끈 뒤 설정·포지션을 다시 읽어 확인",
                    strategy_name(&cfg)
                ),
                kb!(("🛑 완전 끄기 확인", "bm:confirm_full_off")),
            ),
            "adopt" => {
                let preview = match self
                    .engine_call("adopt_position", json!({"apply": false}))
                    .await
                {
                    Ok(v) => v,
                    Err(e) => return (Self::engine_error_text(&e), kb!()),
                };
                if preview.get("ok").and_then(|v| v.as_bool()) != Some(true) {
                    return (format!("🤝 {}", adopt_error_text(&preview)), kb!());
                }
                let cfg = self.config().await;
                let sma25_line = if stop_ma_on(&cfg) {
                    "· 봉 마감이 SMA25 반대편 → 손절\n"
                } else {
                    ""
                };
                let plock_line = if profit_lock_on(&cfg) {
                    format!("· 수익 잠금 (+{}%부터 계단식)\n", lock_start_text(&cfg))
                } else {
                    String::new()
                };
                let stop = cfg_int(&cfg, "emergency_stop_roi_percent", 0);
                let stop_line =
                    if cfg_get(&cfg, "scale_in_enabled").and_then(|v| v.as_bool()) == Some(true) {
                        format!("· 물타기: {}\n", scale_in_text(&cfg))
                    } else if stop > 0 {
                        format!("· 비상손절: 증거금 -{stop}% 주문을 거래소에 걸기\n")
                    } else {
                        String::new()
                    };
                let (fast_name, slow_name) = sma_names(&cfg);
                let (exit_on, exit_period) = exit_ma_setting(&cfg);
                let exit_basis = if exit_on {
                    format!("· {fast_name}/SMA{exit_period} 청산 MA 크로스 → 청산 (반대 진입 없음)\n")
                } else {
                    format!("· {fast_name}/{slow_name} 반대 크로스 → 청산 후 반대 진입\n")
                };
                (
                    format!(
                        "🤝 이 포지션을 봇에게 맡길까요?\n{}\n\n\
                         맡기면 봇이 연 포지션처럼 관리합니다:\n\
                         {exit_basis}{sma25_line}{plock_line}{stop_line}\
                         · 오늘 손익·손실 한도 계산에 포함\n\
                         직접 걸어 둔 TP/SL 주문은 그대로 남습니다.",
                        adopt_summary(&preview)
                    ),
                    kb!(("✅ 관리 맡기기", "bm:confirm_adopt")),
                )
            }
            "confirm_adopt" => {
                let result = match self
                    .engine_call("adopt_position", json!({"apply": true}))
                    .await
                {
                    Ok(v) => v,
                    Err(e) => return (Self::engine_error_text(&e), kb!()),
                };
                let text = if result.get("ok").and_then(|v| v.as_bool()) == Some(true) {
                    format!("🤝 관리 시작: {}", adopt_summary(&result))
                } else {
                    format!("🤝 {}", adopt_error_text(&result))
                };
                let status = self.format_status().await;
                let state_now = self.load_state_or_empty().await;
                let cfg_now = self.config().await;
                (
                    format!("{text}\n\n{status}"),
                    self.build_keyboard(&cfg_now, None, Some(&state_now)),
                )
            }
            "reentry_clear" => {
                let released = match self.engine_call("clear_reentry", json!({})).await {
                    Ok(Value::Array(list)) => list
                        .iter()
                        .map(value_display)
                        .collect::<Vec<_>>()
                        .join(", "),
                    Ok(_) => String::new(),
                    Err(e) => return (Self::engine_error_text(&e), kb!()),
                };
                let text = if released.is_empty() {
                    "재진입 대기 중인 코인이 없습니다.".to_string()
                } else {
                    format!(
                        "🔓 재진입 대기 해제: {released}\n\
                         다음 봉 마감부터 진입 조건이 맞으면 바로 진입합니다."
                    )
                };
                let status = self.format_status().await;
                let state_now = self.load_state_or_empty().await;
                let cfg_now = self.config().await;
                (
                    format!("{text}\n\n{status}"),
                    self.build_keyboard(&cfg_now, None, Some(&state_now)),
                )
            }
            "confirm_full_off" => {
                let text = self.full_off().await;
                let state_now = self.load_state_or_empty().await;
                let cfg_now = self.config().await;
                (text, self.build_keyboard(&cfg_now, None, Some(&state_now)))
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
            "guard" => {
                let daily = cfg_int(&cfg, "daily_loss_limit_pct", 0);
                let streak = cfg_int(&cfg, "max_loss_streak", 0);
                let daily_row: Vec<Value> = DAILY_LOSS_LIMIT_CHOICES
                    .iter()
                    .map(|n| {
                        let label = if *n == 0 {
                            "하루 끔".to_string()
                        } else {
                            format!("하루 -{n}%")
                        };
                        markup_button(mark(daily == *n, &label), format!("bm:dll:{n}"))
                    })
                    .collect();
                let streak_row: Vec<Value> = LOSS_STREAK_CHOICES
                    .iter()
                    .map(|n| {
                        let label = if *n == 0 {
                            "연패 끔".to_string()
                        } else {
                            format!("{n}연패")
                        };
                        markup_button(mark(streak == *n, &label), format!("bm:mls:{n}"))
                    })
                    .collect();
                let rows = vec![
                    daily_row,
                    streak_row,
                    vec![markup_button("↩️ 돌아가기", "bm:status")],
                ];
                (
                    format!(
                        "🛡 손실 한도를 고르세요.\n\
                         • 하루 손실: 오늘(한국시간) 청산된 거래의 손익 합이 시작 잔고의 -N% 이상이면 중지\n\
                         • 연패: 오늘 N번 연속 손실이면 중지\n\
                         중지되면 그날은 신규 진입만 멈추고, 내일 0시에 자동으로 다시 진입합니다.\n\
                         지금: {}",
                        loss_guard_text(&cfg)
                    ),
                    markup(rows),
                )
            }
            a if a.starts_with("dll:") || a.starts_with("mls:") => {
                let (key, choices) = if a.starts_with("dll:") {
                    ("daily_loss_limit_pct", DAILY_LOSS_LIMIT_CHOICES)
                } else {
                    ("max_loss_streak", LOSS_STREAK_CHOICES)
                };
                let choice = a
                    .split_once(':')
                    .map(|x| x.1)
                    .and_then(|s| s.parse::<i64>().ok())
                    .unwrap_or(-1);
                if choices.contains(&choice) {
                    if let Err(e) = self.set_coin_value(key, json!(choice)).await {
                        return (Self::engine_error_text(&e), kb!());
                    }
                }
                let cfg_now = self.config().await;
                let status = self.format_status().await;
                (
                    format!("🛡 손실 한도: {}\n\n{status}", loss_guard_text(&cfg_now)),
                    kb_owned!(),
                )
            }
            "minbal" => {
                let current = cfg_int(&cfg, "min_balance_usdt", 0);
                let buttons: Vec<Value> = MIN_BALANCE_CHOICES
                    .iter()
                    .map(|n| {
                        let label = if *n == 0 {
                            "끄기".to_string()
                        } else {
                            format!("{n} USDT")
                        };
                        markup_button(mark(current == *n, &label), format!("bm:minbal:{n}"))
                    })
                    .collect();
                let mut rows: Vec<Vec<Value>> = buttons.chunks(3).map(|c| c.to_vec()).collect();
                rows.push(vec![markup_button("↩️ 돌아가기", "bm:status")]);
                (
                    format!(
                        "💰 최소 잔고를 고르세요.\n\
                         LIVE에서 포지션이 없을 때 잔고가 이 값 이하이면 새 진입 전에 전략을 OFF 합니다.\n\
                         지금: {}",
                        min_balance_text(&cfg)
                    ),
                    markup(rows),
                )
            }
            a if a.starts_with("minbal:") => {
                let choice = a
                    .split_once(':')
                    .map(|x| x.1)
                    .and_then(|s| s.parse::<i64>().ok())
                    .unwrap_or(-1);
                if MIN_BALANCE_CHOICES.contains(&choice) {
                    if let Err(e) = self.set_coin_value("min_balance_usdt", json!(choice)).await {
                        return (Self::engine_error_text(&e), kb!());
                    }
                }
                let cfg_now = self.config().await;
                let status = self.format_status().await;
                (
                    format!("💰 최소 잔고: {}\n\n{status}", min_balance_text(&cfg_now)),
                    kb_owned!(),
                )
            }
            "pick" | "scope:single" => {
                let pick = self.pick_keyboard(&network).await;
                let body = format!(
                    "🎯 거래할 코인 1개를 고르세요 (거래대금 상위 순).\n\
                     목록에 없는 코인은 /macoin SOL 처럼 입력하면 됩니다.\n\
                     TradFi(미국주식·금)도 /macoin NVDA 처럼 지정할 수 있습니다 (메인넷, 장 시간 제한 없음).\n\
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
                if scope == "btc" || scope == "alts" || scope == "all" {
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
            a @ ("stopma:on" | "stopma:off") => {
                let on = a == "stopma:on";
                let value = if on { json!(STOP_MA_PERIOD) } else { json!(0) };
                if let Err(e) = self.set_coin_value("stop_ma_period", value).await {
                    return (Self::engine_error_text(&e), kb!());
                }
                let status = self.format_status().await;
                let text = if on {
                    format!(
                        "🧱 SMA25 손절: 켬\n봉 마감 종가가 SMA25 반대편이면 청산하고, 진입도 종가가 SMA25 진입 방향 쪽일 때만 합니다.\n(진입할 때 SMA25가 꺼져 있던 보유 포지션에는 걸리지 않음, 다음 진입부터 적용)\n\n{status}"
                    )
                } else {
                    format!(
                        "🧱 SMA25 손절: 끔\nSMA25 진입 조건과 SMA25 손절을 쓰지 않습니다. 청산은 SMA 반대 크로스·수익 잠금·비상손절로만 합니다.\n(보유 중인 포지션도 바로 SMA25 손절이 풀림)\n\n{status}"
                    )
                };
                (text, kb!())
            }
            a @ ("scalein:on" | "scalein:off") => {
                let on = a == "scalein:on";
                if let Err(e) = self.set_coin_value("scale_in_enabled", json!(on)).await {
                    return (Self::engine_error_text(&e), kb!());
                }
                let status = self.format_status().await;
                let text = if on {
                    let loss = scale_in_loss_roi(cfg_int(
                        &self.config().await,
                        "emergency_stop_roi_percent",
                        0,
                    ) as f64) as i64;
                    format!(
                        "➕ 물타기: 켬\n· 봉 마감 때 첫 진입가 대비 증거금 손실이 -10%, -20%가 되고 종가가 아직 진입 방향 쪽(추세 유지)이면 첫 수량의 0.5배씩 추가 (최대 2번)\n· 수익 잠금이 걸린 뒤에는 추가하지 않음\n· 손절은 첫 증거금의 -{loss}%가 되는 가격으로 거래소에 걸고, 추가할 때마다 다시 계산\n(다음 진입부터 적용)\n\n{status}"
                    )
                } else {
                    format!(
                        "➕ 물타기: 끔\n남은 물타기는 하지 않습니다. 이미 걸린 손절 위치는 그대로입니다.\n\n{status}"
                    )
                };
                (text, kb!())
            }
            a @ ("plock:on" | "plock:off") => {
                let on = a == "plock:on";
                if let Err(e) = self.set_coin_value("profit_lock_enabled", json!(on)).await {
                    return (Self::engine_error_text(&e), kb!());
                }
                let cfg_now = self.config().await;
                let status = self.format_status().await;
                let text = if on {
                    let lock_table = lock_table(&cfg_now);
                    format!(
                        "🔒 수익 잠금: 켬 ({lock_table})\n보유 중인 포지션에도 바로 적용됩니다.\n\n{status}"
                    )
                } else {
                    format!(
                        "🔒 수익 잠금: 끔\n새 수익 잠금 주문을 걸지 않습니다. 이미 걸려 있는 잠금 주문은 그대로 둡니다.\n\n{status}"
                    )
                };
                (text, kb!())
            }
            a if a.starts_with("mf:") => {
                let choice = a
                    .split_once(':')
                    .map(|x| x.1)
                    .and_then(|s| s.parse::<i64>().ok())
                    .unwrap_or(-1);
                if MARGIN_FRACTION_CHOICES.contains(&choice) {
                    let value = json!(format!("{}", choice as f64 / 100.0));
                    if let Err(e) = self.set_coin_value("margin_fraction", value).await {
                        return (Self::engine_error_text(&e), kb!());
                    }
                }
                let cfg_now = self.config().await;
                let leverage = cfg_int(&cfg_now, "leverage", 5);
                let margin = py_n(cfg_get(&cfg_now, "margin_fraction")) * 100.0;
                let exposure = leverage as f64 * margin;
                let status = self.format_status().await;
                (
                    format!(
                        "💵 증거금: 가용 잔고의 {margin:.0}% (다음 진입부터 적용)\n포지션 크기 = 레버리지 {leverage}x × {margin:.0}% = 잔고의 {exposure:.0}%\n\n{status}"
                    ),
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
            a if a.starts_with("xma:") => {
                let enable = a.split_once(':').map(|x| x.1).unwrap_or("") == "on";
                let (_, period) = exit_ma_setting(&cfg);
                self.set_exit_ma(enable, period).await;
                let cfg_now = self.config().await;
                let note = if holding {
                    let (enabled_now, period_now) = exit_ma_setting(&cfg_now);
                    if enabled_now {
                        format!(
                            "\n보유 중 포지션에도 다음 봉 마감부터 SMA{period_now} 청산 기준을 적용합니다 \
                             — 조건이 먼저 닿으면 청산하고, 다음 크로스 마감까지 재진입하지 않습니다."
                        )
                    } else {
                        "\n보유 중 포지션은 다시 반대 크로스 청산 + 즉시 반대 진입 방식으로 관리합니다."
                            .to_string()
                    }
                } else {
                    String::new()
                };
                let status = self.format_status().await;
                (
                    format!("🔻 청산 MA: {}{note}\n\n{status}", exit_ma_text(&cfg_now)),
                    kb_owned!(),
                )
            }
            a if a.starts_with("xmap:") => {
                let period = a
                    .split_once(':')
                    .map(|x| x.1)
                    .and_then(|s| s.parse::<i64>().ok())
                    .unwrap_or(-1);
                if !(EXIT_MA_MIN_PERIOD..=EXIT_MA_MAX_PERIOD).contains(&period) {
                    return ("⚠️ 청산 MA 값이 올바르지 않습니다.".to_string(), kb!());
                }
                self.set_exit_ma(true, period).await;
                let cfg_now = self.config().await;
                let note = if holding {
                    "\n(보유 중 포지션은 다음 봉 마감부터 적용)"
                } else {
                    ""
                };
                let status = self.format_status().await;
                (
                    format!(
                        "🔻 청산 MA {period} ON: {}{note}\n\n{status}",
                        exit_ma_text(&cfg_now)
                    ),
                    kb_owned!(),
                )
            }
            "xma_help" => {
                let (enabled_now, period_now) = exit_ma_setting(&cfg);
                let now = if enabled_now {
                    format!("SMA{period_now} (ON)")
                } else {
                    "OFF".to_string()
                };
                (
                    format!(
                        "✏️ 청산 MA 직접 입력\n지금: {now}\n\n\
                         명령: /btcma exit <기간>\n\
                         예) /btcma exit 25 → SMA3 / SMA25 크로스에서 청산\n\
                         예) /btcma exit 69 → SMA3 / SMA69 크로스에서 청산\n\
                         끄기: /btcma exit off (기본값)\n\n\
                         • {EXIT_MA_MIN_PERIOD}~{EXIT_MA_MAX_PERIOD} 사이, 기본 {EXIT_MA_DEFAULT_PERIOD}\n\
                         반대 진입을 하지 않고 다음 크로스 마감까지 대기합니다."
                    ),
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
            a if a.starts_with("lb:") => {
                let bars = if a == "lb:2" { 2 } else { 1 };
                if let Err(e) = self
                    .set_coin_value("cross_lookback_bars", json!(bars))
                    .await
                {
                    return (Self::engine_error_text(&e), kb!());
                }
                let cfg_now = self.config().await;
                let status = self.format_status().await;
                (
                    format!(
                        "⏮ 크로스 진입 범위: {}\n\n{status}",
                        lookback_text(&cfg_now)
                    ),
                    kb_owned!(),
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
                let (fast_name, slow_name) = sma_names(&cfg_now);
                let note = if holding {
                    "\n(보유 중인 포지션도 다음 봉 마감부터 새 기간으로 반대 크로스를 판단 — 그때 패스트가 반대편이면 청산 후 반대 진입)"
                } else {
                    ""
                };
                let status = self.format_status().await;
                (
                    format!("📐 기준선: {fast_name} / {slow_name}{note}\n\n{status}"),
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
                     보유 포지션도 다음 봉 마감부터 새 기간으로 판단합니다 (그때 패스트가 반대편이면 청산 후 반대 진입)."
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
                // Python clears a waiting re-entry before asking for the close.
                if let Err(e) = self.clear_reentry_if_blocked().await {
                    log_line(&format!("telegram: confirm_close reentry clear failed: {e}"));
                }
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
pub(crate) fn parse_command(text: &str) -> (String, Vec<String>) {
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

/// Explain a command that the Python bot used to own but tbot does not serve.
/// Rendered under the "unknown command" notice so the user knows where the
/// feature went instead of assuming the bot died.
pub(crate) fn unknown_command_note(command: &str) -> Option<&'static str> {
    match command {
        // Python-only menus: /setup was the numbered settings menu (it only
        // opened the exchange/network switch at the end), and the rest were
        // its entries. tbot has no settings menu; everything is a button.
        "status" | "state" => Some("상태는 /btcmacross (또는 /macross) 화면에서 확인하세요."),
        "resume" | "pause" | "unpause" => Some(
            "정지/재개는 /stop 다음에 /btcmacross 메뉴의 버튼으로 하세요.",
        ),
        _ => None,
    }
}

/// Parse `/btcma exit 25` / `off` / `on 69` into `(enabled, period)`.
/// Ported from `parse_exit_ma_command`; the error is a Telegram-ready reason.
pub fn parse_exit_ma_command(args: &[String]) -> Result<(bool, i64), String> {
    let mut tokens: Vec<String> = args
        .iter()
        .map(|a| a.trim().to_lowercase())
        .filter(|a| !a.is_empty())
        .collect();
    // "/btcma exit 25" passes the leading keyword through; it is not a value.
    while matches!(tokens.first().map(|s| s.as_str()), Some("exit" | "xma" | "exitma" | "청산"))
    {
        tokens.remove(0);
    }
    let Some(head) = tokens.first().cloned() else {
        return Err("값이 없습니다. 예) /btcma exit 25 · /btcma exit off".to_string());
    };
    if head.starts_with(|c: char| c.is_numeric()) && tokens.len() > 1 {
        return Err("값이 너무 많습니다. 예) /btcma exit 25".to_string());
    }
    if head == "off" || head == "0" {
        return Ok((false, EXIT_MA_DEFAULT_PERIOD));
    }
    let value = if head == "on" {
        if tokens.len() > 2 {
            return Err("값이 너무 많습니다. 예) /btcma exit on 25".to_string());
        }
        match tokens.get(1) {
            Some(v) => v.clone(),
            None => return Ok((true, EXIT_MA_DEFAULT_PERIOD)),
        }
    } else {
        head
    };
    let period = match value.parse::<f64>() {
        Ok(f) if f.is_finite() => f as i64,
        _ => {
            return Err(format!("숫자가 아닙니다: {value} — 예) /btcma exit 25"));
        }
    };
    if !(EXIT_MA_MIN_PERIOD..=EXIT_MA_MAX_PERIOD).contains(&period) {
        return Err(format!(
            "청산 MA는 {EXIT_MA_MIN_PERIOD}~{EXIT_MA_MAX_PERIOD} 사이여야 합니다 (입력: {period})."
        ));
    }
    Ok((true, period))
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
