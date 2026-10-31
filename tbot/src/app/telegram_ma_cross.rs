//! Telegram `/macross` menu (SMA3/SMA120, feature C only). Ported verbatim
//! from `bot_runtime/controller_ma_cross.py`; every service call and config
//! change goes through the engine IPC.
#![allow(clippy::too_many_arguments)]

use serde_json::{json, Map, Value};

use crate::app::ipc::EngineApi;
use crate::app::telegram_bot::{cfg_get, format_g, markup_button, py_n, Outgoing, TelegramBot};
use crate::core::periods::{
    is_default_periods, parse_period_command, periods_label, required_candle_history,
    PERIOD_PRESETS,
};
use crate::strategy::config_ma_plus::LEVERAGE_CHOICES;
use crate::strategy::config_ma_plus::{feature_mode, FEATURE_FIELDS, FEATURE_LABELS};

const CONFIG_KEY: &str = "ma_cross";
const STRATEGY: &str = "ma_plus";
const TRADFI_SCOPE: &str = "tradfi";

pub const TIMEFRAME_LABELS: &[(&str, &str)] = &[
    ("15m", "15분"),
    ("30m", "30분"),
    ("1h", "1시간"),
    ("2h", "2시간"),
    ("4h", "4시간"),
];

pub const NO_FEATURE_TEXT: &str =
    "🚫 이 전략은 C(변동성 타겟 사이징)가 고정 선택이라 이 상태가 나오면 안 됩니다.\n\
설정이 손상되었을 수 있으니 /macross 를 다시 열어 주세요.";

pub const TRADFI_KO: &[(&str, &str)] = &[
    ("weekend", "주말 휴장"),
    ("holiday", "미국 휴장일"),
    ("outside_regular_session", "정규장 시간 아님"),
    ("regular_session_open", "정규장 진행 중"),
];

fn timeframe_label(tf: &str) -> &'static str {
    TIMEFRAME_LABELS
        .iter()
        .find(|(k, _)| *k == tf)
        .map(|(_, v)| *v)
        .unwrap_or("-")
}

fn tradfi_ko_local(reason: &str) -> &'static str {
    TRADFI_KO
        .iter()
        .find(|(k, _)| *k == reason)
        .map(|(_, v)| *v)
        .unwrap_or("")
}

/// Python `_scope_text`.
pub fn scope_text(cfg: &Value) -> String {
    let scan_scope = cfg_get(cfg, "scan_scope")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let alt_universe_size = cfg_get(cfg, "alt_universe_size")
        .map(|v| crate::app::telegram_bot::py_int_of(Some(v)))
        .unwrap_or(50);
    if scan_scope == TRADFI_SCOPE {
        return format!(
            "🏦 TradFi만 — 바이낸스 메인넷 미국주식 + 금 (24h 거래대금 상위 {alt_universe_size}, 미국 정규장에만 신규 진입)"
        );
    }
    if scan_scope == "alts" {
        return format!("🌐 알트 전용 24h 거래대금 상위 {alt_universe_size}개 (BTC 제외)");
    }
    if scan_scope == "single" {
        let symbol = cfg_get(cfg, "single_symbol")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .unwrap_or("BTCUSDT");
        return format!("🎯 지정 코인 {symbol}만");
    }
    "🪙 BTC만".to_string()
}

/// Python `_us_session_text`.
pub fn us_session_text(session: &Value) -> String {
    if !py_truthy(Some(session)) {
        return "확인 불가".to_string();
    }
    let reason = session.get("reason").map(value_display).unwrap_or_default();
    let label = {
        let ko = tradfi_ko_local(&reason);
        if ko.is_empty() {
            if reason.is_empty() {
                "-".to_string()
            } else {
                reason.clone()
            }
        } else {
            ko.to_string()
        }
    };
    let slice19 = |key: &str| -> String {
        session
            .get(key)
            .map(|v| match v {
                Value::String(s) => s.clone(),
                Value::Null => String::new(),
                other => other.to_string(),
            })
            .unwrap_or_default()
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

/// Python `_feature_summary`.
pub fn feature_summary(cfg: &Value) -> String {
    let mode = feature_mode(cfg);
    if mode.is_empty() {
        return "선택 없음 (동작 안 함)".to_string();
    }
    mode.chars()
        .map(|c| {
            let name = c.to_string();
            let label = FEATURE_LABELS
                .iter()
                .find(|(k, _)| *k == name)
                .map(|(_, v)| *v)
                .unwrap_or("");
            format!("{name}({label})")
        })
        .collect::<Vec<_>>()
        .join(" + ")
}

/// Python `_sma_button`.
fn sma_button(cfg: &Value, fast: i64, slow: i64) -> Value {
    let current = (
        cfg_int_of(cfg, "fast_period"),
        cfg_int_of(cfg, "slow_period"),
    );
    let label = format!("SMA{fast}/SMA{slow}");
    let text = if current == (fast, slow) {
        format!("✅ {label}")
    } else {
        label
    };
    markup_button(text, format!("mc:sma:{fast}:{slow}"))
}

fn cfg_int_of(cfg: &Value, key: &str) -> i64 {
    cfg_get(cfg, key)
        .map(|v| crate::app::telegram_bot::py_int_of(Some(v)))
        .unwrap_or(0)
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

fn mark(selected: bool, label: &str) -> String {
    if selected {
        format!("✅ {label}")
    } else {
        label.to_string()
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

impl<A: EngineApi + 'static> TelegramBot<A> {
    async fn ma_call(&self, cmd: &str, args: Value) -> Result<Value, String> {
        self.engine.call(cmd, Some(STRATEGY), args).await
    }

    async fn ma_config(&self) -> Value {
        match self.ma_call("config_get", json!({})).await {
            Ok(v @ Value::Object(_)) => v,
            Ok(_) => Value::Object(Map::new()),
            Err(e) => {
                log_line(&format!("telegram: config_get failed: {e}"));
                Value::Object(Map::new())
            }
        }
    }

    async fn set_ma_value(&self, key: &str, value: Value) -> Result<(), String> {
        let mut changes = Map::new();
        changes.insert(key.to_string(), value);
        self.ma_call("config_set", json!({ "changes": changes }))
            .await
            .map(|_| ())
    }

    async fn set_ma_raw(&self, key: &str, value: Value) -> Result<(), String> {
        // Python writes ints/bools directly; keep the same JSON shape.
        self.set_ma_value(key, value).await
    }

    async fn ma_network(&self) -> Option<String> {
        match self.ma_call("current_network", json!({})).await {
            Ok(Value::String(n)) => Some(n),
            _ => None,
        }
    }

    async fn ma_tradfi_refusal(&self) -> String {
        let network = self.ma_network().await;
        let network_ref: Option<&str> = network.as_deref();
        let allowed = match network_ref {
            Some(n) => match self
                .ma_call("tradfi_network_allowed", json!({ "network": n }))
                .await
            {
                Ok(v) => py_truthy(Some(&v)),
                Err(e) => {
                    log_line(&format!("telegram: tradfi_network_allowed failed: {e}"));
                    false
                }
            },
            None => false,
        };
        if allowed {
            return String::new();
        }
        tradfi_refusal(network_ref)
    }

    fn ma_build_keyboard(&self, cfg: &Value, confirm: Option<(&str, &str)>) -> Value {
        let enabled = py_truthy(cfg_get(cfg, "enabled"));
        let trading_mode = cfg_str(cfg, "trading_mode", "");
        let timeframe = cfg_str(cfg, "timeframe", "");

        let feature_rows = vec![vec![markup_button(
            mark(true, &format!("C · {} (고정)", feature_label("C"))),
            "mc:status",
        )]];
        let mode = feature_mode(cfg);
        let mut rows: Vec<Vec<Value>> = Vec::new();
        rows.push(vec![
            markup_button(mark(enabled, "▶️ 전략 ON"), "mc:on"),
            markup_button(mark(!enabled, "⏹ 신규진입 OFF"), "mc:off"),
        ]);
        rows.push(vec![markup_button(
            "🛑 완전 끄기 (진입 OFF + 포지션 청산)",
            "mc:full_off",
        )]);
        rows.push(vec![
            markup_button(mark(trading_mode == "DRY_RUN", "🧪 DRY_RUN"), "mc:mode:dry"),
            markup_button(mark(trading_mode == "LIVE", "💥 LIVE"), "mc:mode:live"),
        ]);
        let head: Vec<Value> = PERIOD_PRESETS
            .iter()
            .take(3)
            .map(|(_l, f, s)| sma_button(cfg, *f, *s))
            .collect();
        let tail: Vec<Value> = PERIOD_PRESETS
            .iter()
            .skip(3)
            .map(|(_l, f, s)| sma_button(cfg, *f, *s))
            .collect();
        rows.push(head);
        rows.push(tail);
        rows.push(vec![markup_button(
            "✏️ 직접 입력: /map 1 200",
            "mc:sma_help",
        )]);
        rows.push(vec![markup_button(
            format!(
                "🧩 기능: {} · 청산: 반대 크로스만",
                if mode.is_empty() {
                    "없음".to_string()
                } else {
                    mode.clone()
                }
            ),
            "mc:status",
        )]);
        for row in feature_rows {
            rows.push(row);
        }
        let tf_buttons: Vec<Value> = ["1h", "2h", "4h"]
            .iter()
            .map(|tf| {
                markup_button(
                    mark(timeframe == *tf, timeframe_label(tf)),
                    format!("mc:tf:{tf}"),
                )
            })
            .collect();
        rows.push(tf_buttons);
        let leverage = cfg_get(cfg, "leverage")
            .filter(|v| !is_empty_value(v))
            .map(|v| crate::app::telegram_bot::py_int_of(Some(v)))
            .unwrap_or(5);
        let lev_buttons: Vec<Value> = LEVERAGE_CHOICES
            .iter()
            .map(|lev| {
                markup_button(
                    mark(leverage == *lev, &format!("{lev}x")),
                    format!("mc:lev:{lev}"),
                )
            })
            .collect();
        rows.push(lev_buttons);
        let scan_scope = cfg_str(cfg, "scan_scope", "");
        rows.push(vec![
            markup_button(mark(scan_scope == "btc", "🪙 BTC만"), "mc:scope:btc"),
            markup_button(
                mark(scan_scope == "alts", "🌐 알트 상위50"),
                "mc:scope:alts",
            ),
        ]);
        rows.push(vec![markup_button(
            mark(scan_scope == TRADFI_SCOPE, "🏦 TradFi만 (메인넷)"),
            "mc:scope:tradfi",
        )]);
        rows.push(vec![
            markup_button("📊 상태", "mc:status"),
            markup_button("📘 전략 규칙", "mc:rules"),
        ]);
        rows.push(vec![
            markup_button("🔄 거래소 재대조", "mc:reconcile"),
            markup_button("📈 최근 판단", "mc:signals"),
        ]);
        if let Some((confirm_text, confirm_cb)) = confirm {
            rows.insert(
                0,
                vec![
                    markup_button(confirm_text, confirm_cb),
                    markup_button("취소", "mc:status"),
                ],
            );
        }
        json!({"inline_keyboard": rows})
    }

    /// Full stop: disable new entries, close this strategy's position in the
    /// current mode, then read config and both modes' state back to prove it.
    async fn ma_full_off(&self) -> String {
        if let Err(e) = self.set_ma_raw("enabled", json!(false)).await {
            return engine_error_text(&e);
        }
        let mut lines = vec!["🛑 /macross 완전 끄기".to_string()];
        match self
            .ma_call("close_position", json!({ "reason": "manual" }))
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

        let cfg = self.ma_config().await;
        if cfg_get(&cfg, "enabled").is_none() {
            lines.push("· ⚠️ 설정을 다시 읽지 못했습니다 — 엔진 상태를 확인하세요".to_string());
        } else if py_truthy(cfg_get(&cfg, "enabled")) {
            lines.push("· ⚠️ 설정이 아직 ON 입니다 — 다시 눌러 주세요".to_string());
        } else {
            lines.push("· 신규 진입: OFF 확인".to_string());
        }
        if self.ma_network().await.is_some() {
            for mode in ["LIVE", "DRY_RUN"] {
                match self.ma_call("load_state", json!({ "mode": mode })).await {
                    Ok(v) if py_truthy(v.get("trade")) => lines.push(format!(
                        "· ⚠️ {mode} 포지션 남아 있음 — 모드를 {mode}로 바꾼 뒤 다시 완전 끄기"
                    )),
                    Ok(_) => lines.push(format!("· {mode} 포지션: 없음")),
                    Err(e) => lines.push(format!("· ⚠️ {mode} 상태 확인 실패: {e}")),
                }
            }
        }
        let status = self.ma_status_text().await;
        format!("{}\n\n{status}", lines.join("\n"))
    }

    async fn ma_status_text(&self) -> String {
        let cfg = self.ma_config().await;
        ma_status_text(&cfg, self).await
    }

    async fn ma_rules_text(cfg: &Value) -> String {
        let fast = cfg_int_of(cfg, "fast_period");
        let slow = cfg_int_of(cfg, "slow_period");
        let timeframe = cfg_str(cfg, "timeframe", "");
        let margin_pct = py_n(cfg_get(cfg, "margin_fraction")) * 100.0;
        let leverage = cfg_int_of(cfg, "leverage");
        let target_atr = py_n(cfg_get(cfg, "feature_c_target_atr_pct")) * 100.0;
        let min_scale = py_n(cfg_get(cfg, "feature_c_min_scale"));
        let max_scale = py_n(cfg_get(cfg, "feature_c_max_scale"));
        let lock_start = py_n(cfg_get(cfg, "lock_start_roi_percent"));
        let lock_gap = py_n(cfg_get(cfg, "lock_gap_percent"));
        let lock_step = py_n(cfg_get(cfg, "lock_step_percent"));
        [
            "📘 /macross 규칙 (C 전용)".to_string(),
            format!(
                "· 신호: SMA{fast} / SMA{slow} 크로스 (완료된 {}봉)",
                timeframe_label(timeframe)
            ),
            "· 상향 돌파 → LONG · 하향 돌파 → SHORT".to_string(),
            String::new(),
            "C · 변동성 타겟 사이징".to_string(),
            format!("  수량 = 잔고 × {margin_pct:.0}% × 변동성배율 × {leverage}x ÷ 가격"),
            format!(
                "  변동성배율 = 목표 {}% ÷ (ATR/가격), 범위 {}~{}",
                format_g(target_atr),
                format_g(min_scale),
                format_g(max_scale)
            ),
            "  → 조용한 장에서 크게, 요동치는 장에서 작게".to_string(),
            String::new(),
            "청산".to_string(),
            "· 반대 크로스 발생 시 포지션 종료 (시장가)".to_string(),
            format!(
                "· 수익잠금: 최고 증거금 ROI +{}% 도달 → +{}%에 Stop, 이후 +{}%마다 한 칸 상향",
                format_g(lock_start),
                format_g(lock_start - lock_gap),
                format_g(lock_step)
            ),
            "· 비상손절: OFF (반대 크로스 전까지 손실을 그대로 보유)".to_string(),
            String::new(),
            "참고: 이 전략은 다른 선물 전략과 동시에 켜지지 않습니다.".to_string(),
        ]
        .join("\n")
    }

    pub async fn handle_ma_cross_action(&self, action: &str) -> (String, Value) {
        let cfg = self.ma_config().await;
        macro_rules! keyboard {
            () => {
                self.ma_build_keyboard(&self.ma_config().await, None)
            };
            ($confirm:expr) => {
                self.ma_build_keyboard(&self.ma_config().await, Some($confirm))
            };
        }

        if action == "on" {
            if feature_mode(&cfg).is_empty() {
                return (
                    format!("{NO_FEATURE_TEXT}\n\nA~E 중 하나 이상을 먼저 선택하세요."),
                    keyboard!(),
                );
            }
            let body = format!(
                "⚠️ /macross 전략을 켤까요?\n모드: {} · {} · 기능 {}\n켜면 다른 선물 전략(3/200 SMA, BTC 눌림목)의 신규 진입이 OFF 됩니다.",
                cfg_str(&cfg, "trading_mode", ""),
                timeframe_label(cfg_str(&cfg, "timeframe", "")),
                feature_mode(&cfg)
            );
            return (body, keyboard!(("✅ 전략 시작", "mc:confirm_on")));
        }
        if action == "confirm_on" {
            if feature_mode(&cfg).is_empty() {
                return (NO_FEATURE_TEXT.to_string(), keyboard!());
            }
            if let Err(e) = self.set_ma_raw("enabled", json!(true)).await {
                return (engine_error_text(&e), keyboard!());
            }
            if let Err(e) = self
                .ma_call("turn_off_others", json!({ "except": CONFIG_KEY }))
                .await
            {
                // python: ignored on purpose (callable guard, errors bubble only if raised)
                log_line(&format!("telegram: turn_off_others failed: {e}"));
            }
            let status = self.ma_status_text().await;
            return (
                format!("✅ /macross ON — 다른 선물 전략은 OFF 했습니다.\n\n{status}"),
                keyboard!(),
            );
        }
        if action == "off" {
            if let Err(e) = self.set_ma_raw("enabled", json!(false)).await {
                return (engine_error_text(&e), keyboard!());
            }
            let status = self.ma_status_text().await;
            return (
                format!("⏹ /macross 신규 진입 OFF (보유 포지션은 계속 관리)\n\n{status}"),
                keyboard!(),
            );
        }
        if action == "full_off" {
            return (
                "⚠️ /macross를 완전히 끌까요?\n· 신규 진입 OFF\n· 지금 모드에 이 전략의 포지션이 있으면 시장가(reduce-only)로 청산\n· 끈 뒤 설정·포지션을 다시 읽어 확인".to_string(),
                keyboard!(("🛑 완전 끄기 확인", "mc:confirm_full_off")),
            );
        }
        if action == "confirm_full_off" {
            return (self.ma_full_off().await, keyboard!());
        }
        if action.starts_with("feat:") {
            let status = self.ma_status_text().await;
            return (
                format!(
                    "🧩 이 전략은 C(변동성 타겟 사이징) 전용입니다.\n기능 선택은 고정되어 있고, A·B·D·E는 사용하지 않습니다.\n\n{status}"
                ),
                keyboard!(),
            );
        }
        if let Some(rest) = action.strip_prefix("tf:") {
            let tf = rest.split_once(':').map(|x| x.0).unwrap_or(rest);
            let known = TIMEFRAME_LABELS.iter().any(|(k, _)| *k == tf);
            if known {
                if let Err(e) = self.set_ma_raw("timeframe", json!(tf)).await {
                    return (engine_error_text(&e), keyboard!());
                }
            }
            let cfg_now = self.ma_config().await;
            let label = timeframe_label(cfg_str(&cfg_now, "timeframe", ""));
            let status = self.ma_status_text().await;
            return (format!("🕒 봉: {label}\n\n{status}"), keyboard!());
        }
        if let Some(rest) = action.strip_prefix("lev:") {
            let lev: i64 = rest
                .split_once(':')
                .map(|x| x.0)
                .unwrap_or(rest)
                .trim()
                .parse::<i64>()
                .unwrap_or(-1);
            if LEVERAGE_CHOICES.contains(&lev) {
                if let Err(e) = self.set_ma_raw("leverage", json!(lev)).await {
                    return (engine_error_text(&e), keyboard!());
                }
            }
            let cfg_now = self.ma_config().await;
            let value = cfg_int_of(&cfg_now, "leverage");
            let status = self.ma_status_text().await;
            return (format!("⚙️ 레버리지: {value}x\n\n{status}"), keyboard!());
        }
        if let Some(rest) = action.strip_prefix("scope:") {
            let scope = rest.split_once(':').map(|x| x.0).unwrap_or(rest);
            if scope == TRADFI_SCOPE {
                let refusal = self.ma_tradfi_refusal().await;
                if !refusal.is_empty() {
                    return (refusal, keyboard!());
                }
                if let Err(e) = self.set_ma_raw("scan_scope", json!(TRADFI_SCOPE)).await {
                    return (engine_error_text(&e), keyboard!());
                }
                let session = match self.ma_call("us_session_status", json!({})).await {
                    Ok(v) => v,
                    Err(e) => {
                        log_line(&format!("telegram: us_session_status failed: {e}"));
                        Value::Object(Map::new())
                    }
                };
                let cfg_now = self.ma_config().await;
                let status = self.ma_status_text().await;
                let body = format!(
                    "🏦 TradFi만 선택: {}\n• 미국 정규장에서만 신규 진입 · 지금: {}\n• 장 마감·주말·휴장에는 새 진입을 하지 않고, 보유 포지션의 반대 크로스 청산과 수익잠금은 계속 동작합니다.\n\n{status}",
                    scope_text(&cfg_now),
                    us_session_text(&session)
                );
                return (body, keyboard!());
            }
            if scope == "btc" || scope == "alts" {
                if let Err(e) = self.set_ma_raw("scan_scope", json!(scope)).await {
                    return (engine_error_text(&e), keyboard!());
                }
            }
            let cfg_now = self.ma_config().await;
            let status = self.ma_status_text().await;
            return (
                format!("🔎 종목: {}\n\n{status}", scope_text(&cfg_now)),
                keyboard!(),
            );
        }
        if action == "mode:live" {
            return (
                "⚠️ LIVE로 전환할까요? 실제 주문이 나갑니다.\n환경변수 TRADING_MODE=DRY_RUN 이면 계속 DRY_RUN으로 동작합니다."
                    .to_string(),
                keyboard!(("✅ LIVE 전환", "mc:confirm_live")),
            );
        }
        if action == "mode:dry" {
            if let Err(e) = self.set_ma_raw("trading_mode", json!("DRY_RUN")).await {
                return (engine_error_text(&e), keyboard!());
            }
            let status = self.ma_status_text().await;
            return (format!("🧪 DRY_RUN\n\n{status}"), keyboard!());
        }
        if action == "confirm_live" {
            if let Err(e) = self.set_ma_raw("trading_mode", json!("LIVE")).await {
                return (engine_error_text(&e), keyboard!());
            }
            let status = self.ma_status_text().await;
            return (format!("💥 LIVE 전환 완료\n\n{status}"), keyboard!());
        }
        if action == "rules" {
            let text = Self::ma_rules_text(&cfg).await;
            return (text, keyboard!());
        }
        if action == "sma_help" {
            let presets = PERIOD_PRESETS
                .iter()
                .map(|(label, _, _)| *label)
                .collect::<Vec<_>>()
                .join(" · ");
            let fast = cfg_int_of(&cfg, "fast_period");
            let slow = cfg_int_of(&cfg, "slow_period");
            let body = format!(
                "✏️ SMA 기간 직접 입력\n지금: {}\n\n명령: /map <패스트> <슬로우>  (또는 /maperiod)\n예) /map 1 200 → 패스트 1, 슬로우 200\n예) /map 5 60 → 패스트 5, 슬로우 60\n\n버튼 프리셋: {presets}\n• 패스트는 슬로우보다 작아야 합니다 (1 이상, 슬로우 500 이하).\n• 보유 중에 바꾸면 다음 진입부터 적용되고, 보유 포지션은 진입 당시 기간으로 계속 관리됩니다.\n• 이 전략의 백테스트 최적 조합은 3/120입니다.",
                periods_label(fast, slow)
            );
            return (body, keyboard!());
        }
        if let Some(rest) = action.strip_prefix("sma:") {
            let parts: Vec<&str> = rest.split(':').collect();
            if parts.len() != 2 {
                return ("⚠️ 알 수 없는 SMA 설정입니다.".to_string(), keyboard!());
            }
            let (_, message) = self.set_ma_cross_periods(parts[0], parts[1]).await;
            let status = self.ma_status_text().await;
            return (format!("{message}\n\n{status}"), keyboard!());
        }
        let status = self.ma_status_text().await;
        (status, keyboard!())
    }

    pub async fn ma_cross_status_screen(&self) -> (String, Value) {
        let text = self.ma_status_text().await;
        let cfg = self.ma_config().await;
        let markup = self.ma_build_keyboard(&cfg, None);
        (text, markup)
    }

    pub async fn handle_ma_period_cmd(&self, chat_id: i64, args: &[String]) -> Vec<Outgoing> {
        if args.is_empty() {
            let (help_text, _) = self.handle_ma_cross_action("sma_help").await;
            let cfg = self.ma_config().await;
            let markup = self.ma_build_keyboard(&cfg, None);
            return vec![Outgoing::Send {
                chat_id,
                text: help_text,
                markup: Some(markup),
            }];
        }
        match parse_period_command(args) {
            Ok((fast, slow)) => {
                let (_, message) = self
                    .set_ma_cross_periods(&fast.to_string(), &slow.to_string())
                    .await;
                let status = self.ma_status_text().await;
                let cfg = self.ma_config().await;
                let markup = self.ma_build_keyboard(&cfg, None);
                vec![Outgoing::Send {
                    chat_id,
                    text: format!("{message}\n\n{status}"),
                    markup: Some(markup),
                }]
            }
            Err(e) => {
                let cfg = self.ma_config().await;
                let fast = cfg_int_of(&cfg, "fast_period");
                let slow = cfg_int_of(&cfg, "slow_period");
                let text = format!(
                    "⚠️ 기간 변경 실패: {e}\n예) /map 1 200\n지금: {}",
                    periods_label(fast, slow)
                );
                vec![Outgoing::Send {
                    chat_id,
                    text,
                    markup: None,
                }]
            }
        }
    }

    async fn set_ma_cross_periods(&self, fast: &str, slow: &str) -> (bool, String) {
        let parsed = parse_period_command(&[fast.to_string(), slow.to_string()]);
        let (fast_p, slow_p) = match parsed {
            Ok(pair) => pair,
            Err(e) => {
                return (false, format!("⚠️ 기간 변경 실패: {e}\n예) /map 1 200"));
            }
        };
        let cfg_before = self.ma_config().await;
        if let Err(e) = self.set_ma_raw("fast_period", json!(fast_p)).await {
            return (false, format!("⚠️ 기간 변경 실패: {e}\n예) /map 1 200"));
        }
        if let Err(e) = self.set_ma_raw("slow_period", json!(slow_p)).await {
            return (false, format!("⚠️ 기간 변경 실패: {e}\n예) /map 1 200"));
        }
        let needed = required_candle_history(slow_p);
        if cfg_int_of(&cfg_before, "candle_history") < needed {
            if let Err(e) = self.set_ma_raw("candle_history", json!(needed)).await {
                return (false, format!("⚠️ 기간 변경 실패: {e}\n예) /map 1 200"));
            }
        }
        let cfg = self.ma_config().await;
        let network = self.ma_network().await;
        let mut holding = false;
        if network.is_some() {
            match self.ma_call("load_state", json!({})).await {
                Ok(v @ Value::Object(_)) => {
                    holding = py_truthy(v.get("trade"));
                }
                Ok(_) => {}
                Err(e) => {
                    log_line(&format!("telegram: macross holding lookup failed: {e}"));
                }
            }
        }
        let mut note = if holding {
            "\n(보유 중인 포지션은 진입 당시 기간 기준으로 관리, 다음 진입부터 새 기간 적용)"
                .to_string()
        } else {
            String::new()
        };
        if !is_default_periods(fast_p, slow_p, 3, 120) {
            note.push_str(
                "\n(참고: 이 전략의 백테스트 최적 조합은 3/120입니다 — 실제 성과 검증 없이 바꾸지 마세요)",
            );
        }
        let timeframe = cfg_str(&cfg, "timeframe", "");
        let history = cfg_int_of(&cfg, "candle_history");
        (
            true,
            format!(
                "✅ SMA 기간 변경: {} (패스트 {fast_p} · 슬로우 {slow_p})\n• 봉 {timeframe} 기준 · 신호 계산에 {history}봉 사용{note}",
                periods_label(fast_p, slow_p)
            ),
        )
    }
}

fn feature_label(name: &str) -> &'static str {
    FEATURE_LABELS
        .iter()
        .find(|(k, _)| *k == name)
        .map(|(_, v)| *v)
        .unwrap_or("")
}

fn is_empty_value(v: &Value) -> bool {
    match v {
        Value::Null => true,
        Value::String(s) => s.is_empty(),
        _ => false,
    }
}

fn engine_error_text(e: &str) -> String {
    format!("⚠️ 엔진 응답 없음: {e}")
}

fn tradfi_refusal(network: Option<&str>) -> String {
    let network_label_text = match network {
        Some(n) => crate::app::telegram_bot::network_label(n),
        None => "업비트".to_string(),
    };
    format!(
        "🚫 TradFi 전용은 바이낸스 메인넷에서만 사용할 수 있습니다.\n지금 거래소: {network_label_text} (/setup 선택을 따름)\n테스트넷(데모)에는 미국주식 TradFi 계약이 거의 없어 모드를 켜지 않습니다.\n/setup에서 바이낸스 메인넷으로 바꾼 뒤 다시 선택하세요."
    )
}

async fn ma_status_text<A: EngineApi + 'static>(cfg: &Value, bot: &TelegramBot<A>) -> String {
    let mode = feature_mode(cfg);
    let enabled = py_truthy(cfg_get(cfg, "enabled"));
    let fast = cfg_int_of(cfg, "fast_period");
    let slow = cfg_int_of(cfg, "slow_period");
    let timeframe = cfg_str(cfg, "timeframe", "");
    let leverage = cfg_int_of(cfg, "leverage");
    let margin_pct = py_n(cfg_get(cfg, "margin_fraction")) * 100.0;
    let mut lines = vec![
        format!("🧩 /macross — SMA{fast} / SMA{slow} + 변동성 타겟 사이징 (C 전용)"),
        format!(
            "전략: {} · 모드: {} · 봉: {}",
            if enabled { "ON" } else { "OFF" },
            cfg_get(cfg, "trading_mode")
                .map(value_display)
                .unwrap_or_else(|| "None".to_string()),
            timeframe_label(timeframe)
        ),
        format!("기능: {}", feature_summary(cfg)),
        "청산: 반대 크로스 시 종료 · 수익잠금 +5%→+4% 계단 · 비상손절 OFF".to_string(),
        format!("레버리지: {leverage}x ISOLATED · 증거금: 가용 잔고 {margin_pct:.0}%"),
        format!("종목: {}", scope_text(cfg)),
    ];
    if cfg_str(cfg, "scan_scope", "") == TRADFI_SCOPE {
        let network = bot.ma_network().await;
        let mut scope_state =
            json!({"allowed": false, "detail": "업비트에서는 사용할 수 없습니다."});
        if let Some(n) = &network {
            match bot
                .ma_call("tradifi_scope_state", json!({ "network": n }))
                .await
            {
                Ok(v) => scope_state = v,
                Err(e) => {
                    log_line(&format!("telegram: tradifi_scope_state failed: {e}"));
                    scope_state = json!({"allowed": false, "detail": "상태 확인 실패"});
                }
            }
        }
        let allowed = py_truthy(scope_state.get("allowed"));
        let detail = scope_state
            .get("detail")
            .map(value_display)
            .unwrap_or_default();
        lines.push(if allowed {
            "🏦 TradFi 신규 진입: 가능".to_string()
        } else {
            format!("🏦 TradFi 신규 진입: 차단 — {detail}")
        });
        if network.is_some() {
            match bot.ma_call("us_session_status", json!({})).await {
                Ok(v) => lines.push(format!("미국 정규장: {}", us_session_text(&v))),
                Err(e) => {
                    log_line(&format!("telegram: us_session_status failed: {e}"));
                }
            }
        }
    }
    if mode.is_empty() {
        lines.push(String::new());
        lines.push(NO_FEATURE_TEXT.to_string());
    } else if enabled {
        lines.push("이 전략이 켜져 있으면 다른 선물 전략은 자동 OFF 됩니다.".to_string());
    }
    lines.join("\n")
}

/// Kept for parity with the Python `_toggle_ma_cross_feature` shape; the menu
/// never calls it (C is fixed), but tests reference it.
pub async fn toggle_ma_cross_feature<A: EngineApi + 'static>(
    bot: &TelegramBot<A>,
    name: &str,
) -> (bool, String) {
    let cfg = bot.ma_config().await;
    let Some(field) = FEATURE_FIELDS
        .iter()
        .find(|(k, _)| *k == name)
        .map(|(_, v)| *v)
    else {
        return (false, String::new());
    };
    let current = py_truthy(cfg_get(&cfg, field));
    if current && feature_mode(&cfg) == name {
        return (false, "LAST_FEATURE".to_string());
    }
    let value = if current { 0 } else { 1 };
    if let Err(e) = bot.set_ma_raw(field, json!(value)).await {
        return (false, e);
    }
    (true, String::new())
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
