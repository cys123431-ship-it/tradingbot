//! Telegram options menus: `/options` (callbacks `op:`) and `/btcoptions`
//! (callbacks `bo:`). Ported from `bot_runtime/controller_options.py`.
//! All user-facing strings must stay byte-for-byte identical to the Python
//! originals.

use chrono::{TimeZone, Utc};
use chrono_tz::Asia::Seoul;
use serde_json::{json, Map, Value};

use crate::app::ipc::EngineApi;
use crate::app::telegram_bot::{format_g, markup_button, TelegramBot};
use crate::strategy::options_config::{
    btc_dte_preset, btc_dte_preset_label, multi_underlying_restore_values,
    normalize_btc_spread_choice, options_budget_text, BTC_DTE_PRESETS, BTC_ONLY_UNDERLYING,
    BTC_SPREAD_CHOICES,
};

/// Python `float(value or 0.0)` with TypeError/ValueError -> 0.0.
/// Numeric strings parse; bools map to 1.0/0.0.
pub fn safe_number(v: Option<&Value>) -> f64 {
    let value = match v {
        Some(v) => v,
        None => return 0.0,
    };
    if value.is_null() {
        return 0.0;
    }
    if let Some(n) = value.as_f64() {
        return n;
    }
    if let Some(b) = value.as_bool() {
        return if b { 1.0 } else { 0.0 };
    }
    if let Some(s) = value.as_str() {
        let trimmed = s.trim();
        if trimmed.is_empty() {
            return 0.0;
        }
        if let Ok(n) = trimmed.parse::<f64>() {
            return n;
        }
        return 0.0;
    }
    if let Some(arr) = value.as_array() {
        // Python float([]) raises TypeError -> 0.0; list with one number works.
        if arr.is_empty() {
            return 0.0;
        }
        return 0.0;
    }
    0.0
}

/// Python `str(x or "")` for a JSON value (only used for symbols).
fn str_value(v: Option<&Value>) -> String {
    match v {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Null) | None => String::new(),
        Some(Value::Bool(b)) => {
            if *b {
                "True".to_string()
            } else {
                "False".to_string()
            }
        }
        Some(Value::Number(n)) => {
            if let Some(i) = n.as_i64() {
                i.to_string()
            } else if let Some(f) = n.as_f64() {
                format_g(f)
            } else {
                String::new()
            }
        }
        Some(Value::Array(_)) | Some(Value::Object(_)) => String::new(),
    }
}

/// Python truthiness of a JSON value.
fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().map(|f| f != 0.0).unwrap_or(false),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

/// Python `d.get(key) or {}` -> a reference to the object when truthy.
fn get_obj<'a>(v: &'a Value, key: &str) -> &'a Map<String, Value> {
    static EMPTY: std::sync::OnceLock<Map<String, Value>> = std::sync::OnceLock::new();
    if let Some(Value::Object(m)) = v.get(key) {
        if !m.is_empty() {
            return m;
        }
    }
    EMPTY.get_or_init(Map::new)
}

fn obj_is_empty(m: &Map<String, Value>) -> bool {
    m.is_empty()
}

/// Python `get(key) or default` for display: returns Some when the field is
/// truthy (non-null, non-empty string).
fn get_truthy_str(v: &Value, key: &str) -> Option<String> {
    match v.get(key) {
        Some(Value::String(s)) if !s.is_empty() => Some(s.clone()),
        _ => None,
    }
}

fn get_int_or_zero(v: &Value, key: &str) -> i64 {
    match v.get(key) {
        Some(Value::Number(n)) => n.as_i64().unwrap_or(0),
        Some(Value::Bool(b)) => {
            if *b {
                1
            } else {
                0
            }
        }
        Some(Value::String(s)) => {
            let t = s.trim();
            if t.is_empty() {
                0
            } else {
                t.parse::<f64>().map(|f| f as i64).unwrap_or(0)
            }
        }
        _ => 0,
    }
}

// ----- DTE preset helpers -----

fn preset_tuple(key: &str) -> (f64, f64, f64) {
    btc_dte_preset(key).unwrap_or((0.0, 0.0, 0.0))
}

fn preset_label(key: &str) -> String {
    btc_dte_preset_label(key).unwrap_or(key).to_string()
}

fn preset_key_from_cfg(cfg: &Value) -> Option<String> {
    match cfg.get("btc_dte_preset") {
        Some(Value::String(s)) => Some(s.clone()),
        _ => None,
    }
}

// ----- candidate helpers -----

/// `int(_safe_number(candidate.get("expiry_date_ms")))`.
pub fn candidate_expiry_ms(candidate: &Value) -> i64 {
    let direct = safe_number(candidate.get("expiry_date_ms")) as i64;
    if direct > 0 {
        return direct;
    }
    let symbol = str_value(candidate.get("symbol"));
    let parts: Vec<&str> = symbol.split('-').collect();
    if parts.len() >= 2 && parts[1].len() == 6 && parts[1].chars().all(|c| c.is_ascii_digit()) {
        let raw = parts[1];
        // Parse YYMMDD.
        let yy = match raw[0..2].parse::<i32>() {
            Ok(v) => v,
            Err(_) => return 0,
        };
        let mm = match raw[2..4].parse::<i32>() {
            Ok(v) => v,
            Err(_) => return 0,
        };
        let dd = match raw[4..6].parse::<i32>() {
            Ok(v) => v,
            Err(_) => return 0,
        };
        let year = 2000 + yy;
        let dt = match Utc.with_ymd_and_hms(year, mm as u32, dd as u32, 8, 0, 0) {
            chrono::LocalResult::Single(d) => d,
            _ => return 0,
        };
        return dt.timestamp_millis();
    }
    0
}

pub fn candidate_expired(candidate: &Value, now_ms: i64) -> bool {
    let expiry = candidate_expiry_ms(candidate);
    expiry != 0 && expiry <= now_ms
}

pub fn candidate_found_text(candidate: &Value) -> String {
    let found = safe_number(candidate.get("found_at_ms")) as i64;
    if found <= 0 {
        return "발견 시각 미기록".to_string();
    }
    let secs = found / 1000;
    let nanos = ((found % 1000) as u32) * 1_000_000;
    match Utc.timestamp_opt(secs, nanos) {
        chrono::LocalResult::Single(dt) => {
            let kst = dt.with_timezone(&Seoul);
            kst.format("%m-%d %H:%M KST 발견").to_string()
        }
        _ => "발견 시각 미기록".to_string(),
    }
}

pub fn underlyings_text(cfg: &Value) -> String {
    if truthy(cfg.get("btc_only")) {
        return format!("{} 전용 (/btcoptions)", BTC_ONLY_UNDERLYING);
    }
    match cfg.get("underlyings") {
        Some(Value::Array(items)) => {
            let joined: Vec<String> = items
                .iter()
                .filter_map(|v| v.as_str().map(|s| s.to_string()))
                .collect();
            let out = joined.join(", ");
            if out.is_empty() {
                "-".to_string()
            } else {
                out
            }
        }
        _ => "-".to_string(),
    }
}

// ----- status formatting -----

pub fn format_options_status(status: &Value, cfg: &Value, now_ms: i64) -> String {
    let active_map = get_obj(status, "active_position");
    let mut candidate = get_obj(status, "last_candidate").clone();
    let balance = get_obj(status, "balance");

    let enabled_on = truthy(status.get("enabled"));
    let api_ok = truthy(status.get("api_ok"));

    let can_trade_line = match status.get("can_trade") {
        None | Some(Value::Null) => "확인 불가".to_string(),
        Some(v) => {
            if truthy(Some(v)) {
                "허용".to_string()
            } else {
                "차단".to_string()
            }
        }
    };

    let budget_text = match get_truthy_str(status, "budget_text") {
        Some(s) => s,
        None => options_budget_text(cfg),
    };

    let realized = safe_number(status.get("realized_pnl_usdt"));
    let available = safe_number(balance.get("available"));
    let equity = safe_number(balance.get("equity"));
    let exchange_positions = get_int_or_zero(status, "exchange_positions");
    let exchange_orders = get_int_or_zero(status, "exchange_orders");
    let manage_err = get_int_or_zero(status, "manage_error_streak");

    let mut lines: Vec<String> = Vec::new();
    lines.push("🟣 Binance European Options".to_string());
    lines.push("네트워크: MAINNET (선물 테스트넷/메인넷 설정과 별도)".to_string());
    lines.push(format!(
        "자동매매: {}",
        if enabled_on { "ON" } else { "OFF" }
    ));
    lines.push(format!(
        "API 연결: {}",
        if api_ok { "정상" } else { "실패" }
    ));
    lines.push(format!("옵션 주문 권한: {}", can_trade_line));
    lines.push("운용 방식: 옵션 매수 전용 · 네이키드 매도 금지".to_string());
    lines.push(format!("대상 기초자산: {}", underlyings_text(cfg)));
    lines.push(format!("운용 예산: {}", budget_text));
    lines.push(format!("봇 옵션 누적 손익: {:+.4} USDT", realized));
    lines.push(format!(
        "옵션 계좌: 가용 {:.4} / 평가 {:.4} USDT",
        available, equity
    ));
    lines.push(format!(
        "거래소 포지션/주문: {} / {}",
        exchange_positions, exchange_orders
    ));
    lines.push(format!("관리 API 연속 오류: {}회", manage_err));

    if !obj_is_empty(active_map) {
        let symbol = str_value(active_map.get("symbol"));
        let side = str_value(active_map.get("side"));
        let quantity = safe_number(active_map.get("quantity"));
        let entry = safe_number(active_map.get("entry_price"));
        let last_mark = safe_number(active_map.get("last_mark"));
        let last_pnl_pct = safe_number(active_map.get("last_pnl_pct")) * 100.0;
        lines.push(String::new());
        lines.push("보유 중:".to_string());
        lines.push(format!("{} {} · 수량 {}", symbol, side, format_g(quantity)));
        lines.push(format!("진입 {:.4} · 최근 {:.4}", entry, last_mark));
        lines.push(format!("프리미엄 손익률 {:+.1}%", last_pnl_pct));
        if let Some(last_bid) = active_map.get("last_bid") {
            if !last_bid.is_null() {
                let bid = safe_number(Some(last_bid));
                let bid_pnl = safe_number(active_map.get("last_bid_pnl_pct")) * 100.0;
                lines.push(format!(
                    "Bid {:.4} · 즉시 청산 기준 손익률 {:+.1}%",
                    bid, bid_pnl
                ));
            }
        }
    } else {
        lines.push(String::new());
        lines.push("보유 중: 없음".to_string());
    }

    let cand_value = Value::Object(candidate.clone());
    if !candidate.is_empty() && candidate_expired(&cand_value, now_ms) {
        candidate = Map::new();
    }
    if !candidate.is_empty() {
        let cand = Value::Object(candidate.clone());
        let symbol = str_value(cand.get("symbol"));
        let strategy =
            get_truthy_str(&cand, "strategy").unwrap_or_else(|| "ADAPTIVE_TREND".to_string());
        let signal_score = safe_number(cand.get("signal_score"));
        let delta = safe_number(cand.get("delta"));
        let target_delta = safe_number(cand.get("target_delta"));
        let dte_days = safe_number(cand.get("dte_days"));
        let target_dte = safe_number(cand.get("target_dte_days"));
        let spread_pct = safe_number(cand.get("spread_pct")) * 100.0;
        let iv_to_realized = safe_number(cand.get("iv_to_realized"));
        let net_edge = safe_number(cand.get("net_expected_edge_pct")) * 100.0;
        let surface = safe_number(cand.get("surface_iv_premium_pct")) * 100.0;
        let flow = safe_number(cand.get("flow_score"));
        let entry_fraction = safe_number(cand.get("entry_fraction")) * 100.0;
        let planned_cost = safe_number(cand.get("planned_cost_usdt"));
        let found_text = candidate_found_text(&cand);
        lines.push(String::new());
        lines.push(format!("최근 후보 ({}):", found_text));
        lines.push(format!(
            "{} · {} · 신호 {:+.2}",
            symbol, strategy, signal_score
        ));
        lines.push(format!(
            "Delta {:.2}/{:.2} · DTE {:.1}/{:.1}일",
            delta, target_delta, dte_days, target_dte
        ));
        lines.push(format!(
            "Spread {:.1}% · IV/RV {:.2} · 순기대수익 {:+.1}%",
            spread_pct, iv_to_realized, net_edge
        ));
        lines.push(format!(
            "IV표면 프리미엄 {:+.1}% · 흐름 {:+.2} · 투입 {:.0}%/{:.2} USDT",
            surface, flow, entry_fraction, planned_cost
        ));
    }

    let stats = get_obj(status, "scan_rejection_stats");
    let labels = get_obj(status, "scan_outcome_labels");
    let window = get_int_or_zero(status, "scan_outcomes_window");
    if window != 0 {
        lines.push(String::new());
        lines.push(format!("최근 {}회 옵션 스캔 결과:", window));
        let order = [
            "DIRECTION_SIGNAL",
            "DTE",
            "DELTA",
            "IV",
            "EDGE",
            "FLOW",
            "SPREAD",
            "LIQUIDITY",
            "BUDGET",
            "EXISTING_POSITION",
            "OPEN_ORDER",
            "CAN_TRADE",
            "API",
            "OTHER",
            "ORDERABLE_CANDIDATE",
        ];
        for code in order.iter() {
            let count = match stats.get(*code) {
                Some(Value::Number(n)) => n.as_i64().unwrap_or(0),
                Some(Value::Bool(b)) => {
                    if *b {
                        1
                    } else {
                        0
                    }
                }
                Some(Value::String(s)) => {
                    let t = s.trim();
                    if t.is_empty() {
                        0
                    } else {
                        t.parse::<f64>().map(|f| f as i64).unwrap_or(0)
                    }
                }
                _ => 0,
            };
            if count != 0 {
                let label = match labels.get(*code) {
                    Some(Value::String(s)) if !s.is_empty() => s.clone(),
                    _ => (*code).to_string(),
                };
                lines.push(format!("- {}: {}", label, count));
            }
        }
    }

    let last_reason = get_truthy_str(status, "last_reason").unwrap_or_else(|| "없음".to_string());
    lines.push(String::new());
    lines.push(format!("최근 판단: {}", last_reason));

    let api_error = get_truthy_str(status, "api_error");
    if let Some(err) = &api_error {
        lines.push(format!("API 오류: {}", err));
    }
    if let Some(last_err) = get_truthy_str(status, "last_error") {
        let differs = match &api_error {
            Some(ae) => ae != &last_err,
            None => true,
        };
        if differs {
            lines.push(format!("운영 오류: {}", last_err));
        }
    }

    lines.push(
        "OFF는 신규 진입만 중단하며 이미 보유한 봇 옵션의 손절·익절 관리는 계속됩니다.".to_string(),
    );
    lines.join("\n")
}

// ----- BTC header / rules -----

pub fn btc_options_header_lines(cfg: &Value, state: &Value) -> Vec<String> {
    let preset = preset_key_from_cfg(cfg);
    let preset_label = match &preset {
        Some(k) => preset_label(k),
        None => match cfg.get("btc_dte_preset") {
            Some(Value::String(s)) => s.clone(),
            _ => String::new(),
        },
    };
    let preset_target = match &preset {
        Some(k) => preset_tuple(k).1,
        None => 0.0,
    };
    let spread_pct = safe_number(cfg.get("btc_max_spread_pct")) * 100.0;
    let min_qv = safe_number(cfg.get("min_quote_volume_usdt"));

    let mut lines: Vec<String> = Vec::new();
    lines.push("₿ BTC 전용 옵션 메뉴".to_string());
    lines.push(format!(
        "BTC 전용 모드: {}",
        if truthy(cfg.get("btc_only")) {
            "ON"
        } else {
            "OFF (멀티코인 설정 사용 중)"
        }
    ));
    lines.push(format!(
        "옵션 자동 신규진입: {}",
        if truthy(cfg.get("enabled")) {
            "ON"
        } else {
            "OFF"
        }
    ));
    lines.push(format!(
        "만기 범위: {} · 목표 {:.0}일",
        preset_label, preset_target
    ));
    lines.push(format!(
        "최대 스프레드: {:.0}% · 최소 24h 거래대금: {:.0} USDT",
        spread_pct, min_qv
    ));
    if truthy(cfg.get("profit_trigger_on_bid")) {
        lines.push("익절·추적청산 판단: Bid 기준 (실제로 팔리는 가격)".to_string());
    } else {
        lines.push("익절·추적청산 판단: Mark 기준".to_string());
    }

    let active_map = get_obj(state, "active_position");
    let symbol = str_value(active_map.get("symbol"));
    if !symbol.is_empty() && !symbol.to_uppercase().starts_with("BTC-") {
        lines.push(format!(
            "⚠️ 기존 {} 포지션은 청산될 때까지 계속 관리합니다 (신규 진입만 BTC).",
            symbol
        ));
    }
    lines
}

pub fn btc_options_rules_text(cfg: &Value) -> String {
    let spread_pct = safe_number(cfg.get("btc_max_spread_pct")) * 100.0;
    let min_qv = safe_number(cfg.get("min_quote_volume_usdt"));
    let stop_loss = safe_number(cfg.get("stop_loss_pct")) * 100.0;
    let expiry_hours = safe_number(cfg.get("expiry_exit_hours"));
    let max_hold = safe_number(cfg.get("max_hold_hours"));
    let budget = options_budget_text(cfg);

    let mut out = String::new();
    out.push_str("📘 BTC 전용 옵션 운용 규칙\n");
    out.push_str("• 기초자산: BTC 옵션만 거래합니다. 알트 옵션은 호가가 얇아 익절 체결이 늦기 때문에 제외합니다.\n");
    out.push_str("• 진입: 기존 Adaptive Convexity v2 신호(1h·4h 추세, 저IV 압축 돌파)로 CALL/PUT 매수만 합니다.\n");
    out.push_str(&format!(
        "• 유동성 필터: 스프레드 {:.0}% 이하, 24h 거래대금 {:.0} USDT 이상, 양쪽 호가 수량이 있어야 합니다.\n",
        spread_pct, min_qv
    ));
    out.push_str("• 익절·추적청산: Mark가 아니라 Bid로 판단합니다. 신호가 나면 그 Bid에 IOC 매도가 바로 나갑니다.\n");
    out.push_str(&format!(
        "• 손절: 프리미엄 -{:.0}% (Mark 기준). 만기 {:.0}시간 전 정리, 최대 보유 {:.0}시간.\n",
        stop_loss, expiry_hours, max_hold
    ));
    out.push_str(&format!(
        "• 예산: {}. 바이낸스 옵션 지갑에 있는 USDT만 씁니다(선물 지갑과 별개). 네이키드 매도는 하지 않습니다.\n",
        budget
    ));
    out.push_str("• 만기 버튼: 주간은 회전이 빠르지만 시간가치 감소가 크고, 월간은 느리지만 감소가 완만합니다.\n");
    out.push_str("• OFF는 신규 진입만 멈춥니다. 보유 중인 옵션은 손절·익절 관리를 계속합니다.");
    out
}

// ----- keyboards -----

fn keyboard_rows(rows: Vec<Vec<Value>>) -> Value {
    json!({ "inline_keyboard": rows })
}

pub fn build_options_keyboard(confirming_on: bool, confirming_close: bool) -> Value {
    let mut rows: Vec<Vec<Value>> = vec![vec![
        markup_button("▶️ 옵션 ON", "op:on"),
        markup_button("⏹ 옵션 OFF", "op:off"),
    ]];
    rows.push(vec![
        markup_button("📊 상태", "op:status"),
        markup_button("🔎 지금 스캔", "op:scan"),
    ]);
    rows.push(vec![
        markup_button("📈 전략 설명", "op:strategy"),
        markup_button("💰 예산", "op:budget"),
    ]);
    rows.push(vec![markup_button("🔻 봇 옵션 포지션 청산", "op:close")]);
    if confirming_on {
        rows.insert(
            0,
            vec![
                markup_button("✅ 실주문 시작 확인", "op:confirm_on"),
                markup_button("취소", "op:status"),
            ],
        );
    }
    if confirming_close {
        rows.insert(
            0,
            vec![
                markup_button("✅ 청산 확인", "op:confirm_close"),
                markup_button("취소", "op:status"),
            ],
        );
    }
    keyboard_rows(rows)
}

fn mark_label(selected: bool, label: &str) -> String {
    if selected {
        format!("✅ {}", label)
    } else {
        label.to_string()
    }
}

pub fn build_btc_options_keyboard(
    cfg: &Value,
    confirming_on: bool,
    confirming_close: bool,
    confirming_multi: bool,
) -> Value {
    let preset = preset_key_from_cfg(cfg);
    let spread = safe_number(cfg.get("btc_max_spread_pct"));

    let mut rows: Vec<Vec<Value>> = Vec::new();
    rows.push(vec![
        markup_button("▶️ BTC 옵션 ON", "bo:on"),
        markup_button("⏹ 신규진입 OFF", "bo:off"),
    ]);
    rows.push(vec![
        markup_button("📊 상태", "bo:status"),
        markup_button("🔎 지금 스캔", "bo:scan"),
    ]);

    let dte_row: Vec<Value> = BTC_DTE_PRESETS
        .iter()
        .map(|(key, _range, label)| {
            let selected = match &preset {
                Some(p) => p == key,
                None => false,
            };
            let first_word = label.split(' ').next().unwrap_or("");
            let text = mark_label(selected, first_word);
            markup_button(&text, format!("bo:dte:{}", key))
        })
        .collect();
    rows.push(dte_row);

    let spr_row: Vec<Value> = BTC_SPREAD_CHOICES
        .iter()
        .map(|choice| {
            let selected = (spread - *choice).abs() < 1e-9;
            let text = mark_label(selected, &format!("스프레드 {:.0}%", *choice * 100.0));
            markup_button(&text, format!("bo:spr:{}", format_g(*choice)))
        })
        .collect();
    rows.push(spr_row);

    rows.push(vec![
        markup_button("📘 운용 규칙", "bo:help"),
        markup_button("🔁 멀티코인 복귀", "bo:multi"),
    ]);
    rows.push(vec![markup_button("🔻 봇 옵션 포지션 청산", "bo:close")]);

    let confirm: Option<(&str, &str)> = if confirming_on {
        Some(("✅ BTC 옵션 실주문 시작", "bo:confirm_on"))
    } else if confirming_close {
        Some(("✅ 청산 확인", "bo:confirm_close"))
    } else if confirming_multi {
        Some(("✅ BTC 전용 해제", "bo:confirm_multi"))
    } else {
        None
    };
    if let Some((label, cb)) = confirm {
        rows.insert(
            0,
            vec![markup_button(label, cb), markup_button("취소", "bo:status")],
        );
    }
    keyboard_rows(rows)
}

// ----- run_cycle result formatting -----

fn result_reason(result: &Value) -> String {
    let reason = match result.get("reason") {
        Some(Value::String(s)) if !s.is_empty() => s.clone(),
        _ => match result.get("action") {
            Some(Value::String(s)) if !s.is_empty() => s.clone(),
            _ => String::new(),
        },
    };
    reason
}

fn engine_error_text(e: &str) -> String {
    format!("⚠️ 엔진 응답 없음: {}", e)
}

// ----- bot impl -----

impl<A: EngineApi + 'static> TelegramBot<A> {
    async fn opt_call(&self, cmd: &str, args: Value) -> Result<Value, String> {
        self.engine.call(cmd, None, args).await
    }

    async fn opt_config(&self) -> Value {
        match self.opt_call("options_config", json!({})).await {
            Ok(v) if v.is_object() => v,
            _ => json!({}),
        }
    }

    async fn opt_state(&self) -> Value {
        match self.opt_call("options_state", json!({})).await {
            Ok(v) if v.is_object() => v,
            _ => json!({}),
        }
    }

    async fn opt_status(&self, refresh: bool) -> Value {
        match self
            .opt_call("options_status", json!({ "refresh": refresh }))
            .await
        {
            Ok(v) if v.is_object() => v,
            _ => json!({}),
        }
    }

    async fn opt_preflight(&self) -> Value {
        match self.opt_call("options_preflight", json!({})).await {
            Ok(v) if v.is_object() => v,
            _ => json!({}),
        }
    }

    async fn opt_set(&self, changes: Value) -> Result<(), String> {
        self.opt_call("options_set", json!({ "changes": changes }))
            .await
            .map(|_| ())
    }

    async fn opt_run_cycle(&self, force_scan: bool, force_exit: bool) -> Value {
        match self
            .opt_call(
                "options_run_cycle",
                json!({ "force_scan": force_scan, "force_exit": force_exit }),
            )
            .await
        {
            Ok(v) if v.is_object() => v,
            _ => json!({}),
        }
    }

    /// Fetch status (refresh) and config, then format the status text.
    async fn format_options_status_now(&self, refresh: bool) -> String {
        let status = self.opt_status(refresh).await;
        let cfg = self.opt_config().await;
        let now_ms = Utc::now().timestamp_millis();
        format_options_status(&status, &cfg, now_ms)
    }

    async fn format_btc_options_status_now(&self) -> String {
        let cfg = self.opt_config().await;
        let state = self.opt_state().await;
        let header = btc_options_header_lines(&cfg, &state).join("\n");
        let body = self.format_options_status_now(true).await;
        format!("{}\n\n{}", header, body)
    }

    /// Settings confirmation from config only (no exchange round-trip).
    async fn btc_options_settings_text_now(&self) -> String {
        let cfg = self.opt_config().await;
        let state = self.opt_state().await;
        let mut text = btc_options_header_lines(&cfg, &state).join("\n");
        text.push_str("\n\n✅ 저장됨 · 계좌·후보 현황은 📊 상태 버튼으로 확인하세요.");
        text
    }

    /// `/options` command: (status text, keyboard).
    pub async fn options_screen(&self) -> (String, Value) {
        let text = self.format_options_status_now(true).await;
        (text, build_options_keyboard(false, false))
    }

    /// `/btcoptions` command: (status text, keyboard).
    pub async fn btc_options_screen(&self) -> (String, Value) {
        let text = self.format_btc_options_status_now().await;
        let cfg = self.opt_config().await;
        (text, build_btc_options_keyboard(&cfg, false, false, false))
    }

    /// `op:` callback (data without the prefix) -> (text, keyboard).
    pub async fn handle_options_action(&self, action: &str) -> (String, Value) {
        if action == "on" {
            let preflight = self.opt_preflight().await;
            let ok = truthy(preflight.get("ok"));
            let can_trade_false = matches!(preflight.get("can_trade"), Some(Value::Bool(false)));
            if !ok || can_trade_false {
                let err = get_truthy_str(&preflight, "error").unwrap_or_else(|| {
                    "European Options 주문 권한이 비활성 상태입니다.".to_string()
                });
                let text = format!(
                    "❌ 옵션 API 사전점검 실패\n{}\n\nReading·European Options 권한과 서버 IP 제한을 확인하세요.",
                    err
                );
                return (text, build_options_keyboard(false, false));
            }
            let text = "⚠️ 옵션 실주문을 시작하시겠습니까?\n\
                        매수 프리미엄·예상 수수료 합계는 옵션 지갑 가용 잔고 안에서만 쓰며 \
                        네이키드 매도는 하지 않습니다.";
            return (text.to_string(), build_options_keyboard(true, false));
        }
        if action == "confirm_on" {
            let changes = json!({ "enabled": true });
            if let Err(e) = self.opt_set(changes).await {
                return (engine_error_text(&e), build_options_keyboard(false, false));
            }
            let result = self.opt_run_cycle(true, false).await;
            let reason = result_reason(&result);
            let body = self.format_options_status_now(true).await;
            let text = format!("✅ 옵션 자동매매 ON\n첫 판단: {}\n\n{}", reason, body);
            return (text, build_options_keyboard(false, false));
        }
        if action == "off" {
            let changes = json!({ "enabled": false });
            if let Err(e) = self.opt_set(changes).await {
                return (engine_error_text(&e), build_options_keyboard(false, false));
            }
            let body = self.format_options_status_now(true).await;
            let text = format!(
                "⏹ 옵션 신규 진입 OFF\n보유 중인 봇 옵션은 기존 손절·익절 규칙으로 계속 관리합니다.\n\n{}",
                body
            );
            return (text, build_options_keyboard(false, false));
        }
        if action == "status" {
            let text = self.format_options_status_now(true).await;
            return (text, build_options_keyboard(false, false));
        }
        if action == "scan" {
            let result = self.opt_run_cycle(true, false).await;
            let reason = result_reason(&result);
            let body = self.format_options_status_now(true).await;
            let text = format!("🔎 즉시 점검: {}\n\n{}", reason, body);
            return (text, build_options_keyboard(false, false));
        }
        if action == "strategy" {
            let text = "📈 Adaptive Convexity Trend v2\n\
                        1시간·4시간 다중속도 추세와 Low-IV Squeeze를 결합하고, HAR식 다중기간 실현변동성으로 신호강도에 맞는 DTE·Delta를 고릅니다.\n\
                        Low-IV Squeeze는 ATR/실현변동성 압축 뒤 거래량·모멘텀을 동반한 상·하방 돌파만 CALL/PUT 후보로 봅니다.\n\
                        옵션 지갑 잔고·최소수량·수수료를 먼저 통과한 계약만 순기대수익·Delta·DTE·IV/RV·IV표면·skew·Spread·최근 체결흐름·유동성·Greeks로 비교합니다.\n\
                        메이커 우선 체결 뒤 순기대수익이 남을 때만 IOC로 전환합니다. 고정 +80% 익절 대신 단계형 추적청산으로 큰 수익을 열어 두며 -55% 프리미엄 손절과 만기·시간 제한은 유지합니다.\n\
                        옵션 매수만 허용하므로 신규 진입용 SELL/네이키드 매도는 만들지 않습니다.";
            return (text.to_string(), build_options_keyboard(false, false));
        }
        if action == "budget" {
            let status = self.opt_status(true).await;
            let budget_text = match get_truthy_str(&status, "budget_text") {
                Some(s) => s,
                None => "-".to_string(),
            };
            let balance = get_obj(&status, "balance");
            let available = safe_number(balance.get("available"));
            let realized = safe_number(status.get("realized_pnl_usdt"));
            let text = format!(
                "💰 옵션 예산: {}\n옵션 지갑 가용: {:.4} USDT\n봇 옵션 누적 손익: {:+.4} USDT\n한 번의 진입은 진입 시점의 옵션 지갑 가용 잔고 안에서 프리미엄과 예상 수수료를 합쳐 계산합니다.\n입금하면 바로 예산이 늘고, 손실이 나면 줄어든 잔고만큼만 씁니다.\n선물 지갑·선물 리스크·일일손실 한도와는 완전히 별개입니다 (옵션 지갑으로 이체 필요).",
                budget_text, available, realized
            );
            return (text, build_options_keyboard(false, false));
        }
        if action == "close" {
            let text = "⚠️ 봇이 보유한 옵션 포지션을 IOC 지정가로 청산하시겠습니까?\n\
                        수동 옵션 포지션은 건드리지 않습니다.";
            return (text.to_string(), build_options_keyboard(false, true));
        }
        if action == "confirm_close" {
            let result = self.opt_run_cycle(false, true).await;
            let reason = result_reason(&result);
            let body = self.format_options_status_now(true).await;
            let text = format!("🔻 옵션 청산 요청: {}\n\n{}", reason, body);
            return (text, build_options_keyboard(false, false));
        }
        let text = self.format_options_status_now(true).await;
        (text, build_options_keyboard(false, false))
    }

    /// `bo:` callback (data without the prefix) -> (text, keyboard).
    pub async fn handle_btc_options_action(&self, action: &str) -> (String, Value) {
        let cfg = self.opt_config().await;

        if action == "on" {
            let preflight = self.opt_preflight().await;
            let ok = truthy(preflight.get("ok"));
            let can_trade_false = matches!(preflight.get("can_trade"), Some(Value::Bool(false)));
            if !ok || can_trade_false {
                let err = get_truthy_str(&preflight, "error").unwrap_or_else(|| {
                    "European Options 주문 권한이 비활성 상태입니다.".to_string()
                });
                let text = format!(
                    "❌ 옵션 API 사전점검 실패\n{}\n\nReading·European Options 권한과 서버 IP 제한을 확인하세요.",
                    err
                );
                let fresh = self.opt_config().await;
                return (
                    text,
                    build_btc_options_keyboard(&fresh, false, false, false),
                );
            }
            let text = format!(
                "⚠️ BTC 옵션 실주문을 시작하시겠습니까?\n대상: {} 옵션만 · 예산: {} (프리미엄+예상 수수료 기준) · 네이키드 매도 없음.\n옵션은 만기까지 시간가치가 줄어 프리미엄 전액을 잃을 수 있습니다.",
                BTC_ONLY_UNDERLYING,
                options_budget_text(&cfg)
            );
            return (text, build_btc_options_keyboard(&cfg, true, false, false));
        }
        if action == "confirm_on" {
            let changes = json!({ "btc_only": true, "enabled": true });
            if let Err(e) = self.opt_set(changes).await {
                let fresh = self.opt_config().await;
                return (
                    engine_error_text(&e),
                    build_btc_options_keyboard(&fresh, false, false, false),
                );
            }
            let result = self.opt_run_cycle(true, false).await;
            let reason = result_reason(&result);
            let body = self.format_btc_options_status_now().await;
            let text = format!("✅ BTC 옵션 자동매매 ON\n첫 판단: {}\n\n{}", reason, body);
            let fresh = self.opt_config().await;
            return (
                text,
                build_btc_options_keyboard(&fresh, false, false, false),
            );
        }
        if action == "off" {
            let changes = json!({ "enabled": false });
            if let Err(e) = self.opt_set(changes).await {
                let fresh = self.opt_config().await;
                return (
                    engine_error_text(&e),
                    build_btc_options_keyboard(&fresh, false, false, false),
                );
            }
            let body = self.format_btc_options_status_now().await;
            let text = format!(
                "⏹ 옵션 신규 진입 OFF\n보유 중인 봇 옵션은 기존 손절·익절 규칙으로 계속 관리합니다.\n\n{}",
                body
            );
            let fresh = self.opt_config().await;
            return (
                text,
                build_btc_options_keyboard(&fresh, false, false, false),
            );
        }
        if action == "scan" {
            let result = self.opt_run_cycle(true, false).await;
            let reason = result_reason(&result);
            let body = self.format_btc_options_status_now().await;
            let text = format!("🔎 즉시 점검: {}\n\n{}", reason, body);
            let fresh = self.opt_config().await;
            return (
                text,
                build_btc_options_keyboard(&fresh, false, false, false),
            );
        }
        if let Some(rest) = action.strip_prefix("dte:") {
            let preset = rest.to_string();
            let prefix = if btc_dte_preset(&preset).is_some() {
                let changes = json!({ "btc_dte_preset": preset });
                if let Err(e) = self.opt_set(changes).await {
                    let fresh = self.opt_config().await;
                    return (
                        engine_error_text(&e),
                        build_btc_options_keyboard(&fresh, false, false, false),
                    );
                }
                format!("🗓 만기 범위: {}", preset_label(&preset))
            } else {
                "알 수 없는 만기 설정입니다.".to_string()
            };
            let settings = self.btc_options_settings_text_now().await;
            let text = format!("{}\n\n{}", prefix, settings);
            let fresh = self.opt_config().await;
            return (
                text,
                build_btc_options_keyboard(&fresh, false, false, false),
            );
        }
        if let Some(rest) = action.strip_prefix("spr:") {
            let choice = normalize_btc_spread_choice(&json!(rest));
            let changes = json!({ "btc_max_spread_pct": choice });
            if let Err(e) = self.opt_set(changes).await {
                let fresh = self.opt_config().await;
                return (
                    engine_error_text(&e),
                    build_btc_options_keyboard(&fresh, false, false, false),
                );
            }
            let settings = self.btc_options_settings_text_now().await;
            let text = format!("↔️ 최대 스프레드: {:.0}%\n\n{}", choice * 100.0, settings);
            let fresh = self.opt_config().await;
            return (
                text,
                build_btc_options_keyboard(&fresh, false, false, false),
            );
        }
        if action == "help" {
            let text = btc_options_rules_text(&cfg);
            return (text, build_btc_options_keyboard(&cfg, false, false, false));
        }
        if action == "multi" {
            let text = "⚠️ BTC 전용 모드를 해제하시겠습니까?\n\
                        안전을 위해 옵션 신규 진입도 함께 OFF 됩니다. 멀티코인으로 다시 켜려면 /options 메뉴를 쓰세요.";
            return (
                text.to_string(),
                build_btc_options_keyboard(&cfg, false, false, true),
            );
        }
        if action == "confirm_multi" {
            let mut changes = Map::new();
            changes.insert("enabled".to_string(), Value::Bool(false));
            changes.insert("btc_only".to_string(), Value::Bool(false));
            for (k, v) in multi_underlying_restore_values().into_iter() {
                changes.insert(k, v);
            }
            if let Err(e) = self.opt_set(Value::Object(changes)).await {
                let fresh = self.opt_config().await;
                return (
                    engine_error_text(&e),
                    build_btc_options_keyboard(&fresh, false, false, false),
                );
            }
            let body = self.format_btc_options_status_now().await;
            let text = format!("🔁 BTC 전용 해제 · 옵션 신규 진입 OFF\n\n{}", body);
            let fresh = self.opt_config().await;
            return (
                text,
                build_btc_options_keyboard(&fresh, false, false, false),
            );
        }
        if action == "close" {
            let text = "⚠️ 봇이 보유한 옵션 포지션을 IOC 지정가로 청산하시겠습니까?\n\
                        수동 옵션 포지션은 건드리지 않습니다.";
            return (
                text.to_string(),
                build_btc_options_keyboard(&cfg, false, true, false),
            );
        }
        if action == "confirm_close" {
            let result = self.opt_run_cycle(false, true).await;
            let reason = result_reason(&result);
            let body = self.format_btc_options_status_now().await;
            let text = format!("🔻 옵션 청산 요청: {}\n\n{}", reason, body);
            let fresh = self.opt_config().await;
            return (
                text,
                build_btc_options_keyboard(&fresh, false, false, false),
            );
        }
        let text = self.format_btc_options_status_now().await;
        let fresh = self.opt_config().await;
        (
            text,
            build_btc_options_keyboard(&fresh, false, false, false),
        )
    }
}

#[cfg(test)]
mod tests {
    #![allow(
        dead_code,
        unused_variables,
        clippy::manual_async_fn,
        clippy::derivable_impls,
        clippy::manual_range_contains
    )]
    use super::*;
    use std::sync::{Arc, Mutex};

    use crate::app::ipc::BoxFut;
    use crate::app::telegram_api::TelegramApi;
    use crate::app::telegram_bot::TelegramBot;

    // ----- fake engine -----

    struct FakeEngine {
        recorded: Mutex<Vec<(String, Value)>>,
        config: Mutex<Value>,
        state: Mutex<Value>,
        status: Mutex<Value>,
        preflight: Mutex<Value>,
        run_cycle: Mutex<Value>,
    }

    impl FakeEngine {
        fn new(config: Value, state: Value, status: Value) -> Self {
            FakeEngine {
                recorded: Mutex::new(Vec::new()),
                config: Mutex::new(config.clone()),
                state: Mutex::new(state.clone()),
                status: Mutex::new(status),
                preflight: Mutex::new(json!({"ok": true, "can_trade": true})),
                run_cycle: Mutex::new(json!({"action": "waiting", "reason": "테스트 판단"})),
            }
        }

        fn calls(&self) -> Vec<(String, Value)> {
            self.recorded.lock().map(|g| g.clone()).unwrap_or_default()
        }

        fn set_preflight(&self, v: Value) {
            if let Ok(mut p) = self.preflight.lock() {
                *p = v;
            }
        }

        fn set_run_cycle(&self, v: Value) {
            if let Ok(mut r) = self.run_cycle.lock() {
                *r = v;
            }
        }

        fn set_state(&self, v: Value) {
            if let Ok(mut s) = self.state.lock() {
                *s = v;
            }
        }

        fn set_status(&self, v: Value) {
            if let Ok(mut s) = self.status.lock() {
                *s = v;
            }
        }

        fn set_config(&self, v: Value) {
            if let Ok(mut c) = self.config.lock() {
                *c = v;
            }
        }
    }

    impl EngineApi for FakeEngine {
        fn call(
            &self,
            cmd: &str,
            _strategy: Option<&str>,
            args: Value,
        ) -> BoxFut<Result<Value, String>> {
            let cmd = cmd.to_string();
            if let Ok(mut rec) = self.recorded.lock() {
                rec.push((cmd.clone(), args.clone()));
            }
            let result: Result<Value, String> = match cmd.as_str() {
                "options_config" => match self.config.lock() {
                    Ok(g) => Ok(g.clone()),
                    Err(_) => Ok(json!({})),
                },
                "options_state" => match self.state.lock() {
                    Ok(g) => Ok(g.clone()),
                    Err(_) => Ok(json!({})),
                },
                "options_status" => match self.status.lock() {
                    Ok(g) => Ok(g.clone()),
                    Err(_) => Ok(json!({})),
                },
                "options_preflight" => match self.preflight.lock() {
                    Ok(g) => Ok(g.clone()),
                    Err(_) => Ok(json!({})),
                },
                "options_run_cycle" => match self.run_cycle.lock() {
                    Ok(g) => Ok(g.clone()),
                    Err(_) => Ok(json!({})),
                },
                "options_set" => {
                    // Return the (unchanged by us) config as the new raw section.
                    match self.config.lock() {
                        Ok(g) => Ok(g.clone()),
                        Err(_) => Ok(json!({})),
                    }
                }
                _ => Ok(json!({})),
            };
            Box::pin(async move { result })
        }
    }

    fn empty_state() -> Value {
        json!({"active_position": null})
    }

    fn base_status() -> Value {
        json!({"enabled": false, "api_ok": true, "can_trade": true})
    }

    fn make_bot(fake: Arc<FakeEngine>) -> TelegramBot<FakeEngine> {
        let api = TelegramApi::new("x".into());
        TelegramBot::new(api, fake, 1)
    }

    fn callbacks(kb: &Value) -> Vec<String> {
        let mut out = Vec::new();
        if let Some(rows) = kb.get("inline_keyboard").and_then(|v| v.as_array()) {
            for row in rows {
                if let Some(items) = row.as_array() {
                    for b in items {
                        if let Some(cb) = b.get("callback_data").and_then(|v| v.as_str()) {
                            out.push(cb.to_string());
                        }
                    }
                }
            }
        }
        out
    }

    fn labels(kb: &Value) -> Vec<String> {
        let mut out = Vec::new();
        if let Some(rows) = kb.get("inline_keyboard").and_then(|v| v.as_array()) {
            for row in rows {
                if let Some(items) = row.as_array() {
                    for b in items {
                        if let Some(t) = b.get("text").and_then(|v| v.as_str()) {
                            out.push(t.to_string());
                        }
                    }
                }
            }
        }
        out
    }

    fn set_calls(fake: &FakeEngine) -> Vec<Value> {
        fake.calls()
            .into_iter()
            .filter(|(cmd, _)| cmd == "options_set")
            .map(|(_, args)| args)
            .collect()
    }

    fn channel_calls(fake: &FakeEngine, cmd: &str) -> usize {
        fake.calls().iter().filter(|(c, _)| c == cmd).count()
    }

    #[tokio::test]
    async fn btc_menu_on_requires_confirmation_then_enables_btc_only() {
        let fake = Arc::new(FakeEngine::new(
            json!({"enabled": false, "btc_only": false}),
            empty_state(),
            base_status(),
        ));
        let bot = make_bot(fake.clone());
        let (text, kb) = bot.handle_btc_options_action("on").await;
        assert!(text.contains("BTC 옵션 실주문을 시작"));
        assert!(callbacks(&kb).iter().any(|c| c == "bo:confirm_on"));
        assert!(set_calls(&fake).is_empty());

        let (text2, _) = bot.handle_btc_options_action("confirm_on").await;
        let sets = set_calls(&fake);
        assert_eq!(sets.len(), 1);
        let changes = &sets[0]["changes"];
        assert_eq!(changes.get("btc_only"), Some(&Value::Bool(true)));
        assert_eq!(changes.get("enabled"), Some(&Value::Bool(true)));
        assert!(text2.contains("BTC 옵션 자동매매 ON"));
    }

    #[tokio::test]
    async fn btc_menu_preflight_failure_does_not_enable() {
        let fake = Arc::new(FakeEngine::new(
            json!({"enabled": false, "btc_only": false}),
            empty_state(),
            base_status(),
        ));
        fake.set_preflight(json!({"ok": false, "error": "API-key format invalid."}));
        let bot = make_bot(fake.clone());
        let (text, kb) = bot.handle_btc_options_action("on").await;
        assert!(text.contains("사전점검 실패"));
        assert!(text.contains("API-key format invalid"));
        assert!(!callbacks(&kb).iter().any(|c| c == "bo:confirm_on"));
        assert!(set_calls(&fake).is_empty());
    }

    #[tokio::test]
    async fn btc_menu_dte_and_spread_buttons_update_config() {
        let fake = Arc::new(FakeEngine::new(
            json!({"enabled": true, "btc_only": true, "btc_dte_preset": "weekly", "btc_max_spread_pct": 0.04}),
            empty_state(),
            base_status(),
        ));
        let bot = make_bot(fake.clone());

        let (text, kb) = bot.handle_btc_options_action("dte:monthly").await;
        let sets = set_calls(&fake);
        assert_eq!(sets.len(), 1);
        assert_eq!(sets[0]["changes"]["btc_dte_preset"], json!("monthly"));
        assert!(text.contains("월간 (7~35일)"));
        assert!(callbacks(&kb).iter().any(|c| c == "bo:dte:weekly"));

        let (text, _) = bot.handle_btc_options_action("spr:0.1").await;
        let sets = set_calls(&fake);
        assert_eq!(sets.len(), 2);
        let stored = sets[1]["changes"]["btc_max_spread_pct"]
            .as_f64()
            .unwrap_or(0.0);
        assert!((stored - 0.10).abs() < 1e-9);
        assert!(text.contains("최대 스프레드: 10%"));

        let before = set_calls(&fake).len();
        let (text, _) = bot.handle_btc_options_action("dte:bogus").await;
        assert_eq!(set_calls(&fake).len(), before);
        assert!(text.contains("알 수 없는 만기 설정입니다."));
    }

    #[tokio::test]
    async fn btc_menu_multi_restore_turns_entries_off() {
        let fake = Arc::new(FakeEngine::new(
            json!({"enabled": true, "btc_only": true}),
            empty_state(),
            base_status(),
        ));
        let bot = make_bot(fake.clone());

        let (_, kb) = bot.handle_btc_options_action("multi").await;
        assert!(callbacks(&kb).iter().any(|c| c == "bo:confirm_multi"));
        assert!(set_calls(&fake).is_empty());

        let (text, _) = bot.handle_btc_options_action("confirm_multi").await;
        let sets = set_calls(&fake);
        assert_eq!(sets.len(), 1);
        let changes = &sets[0]["changes"];
        assert_eq!(changes.get("enabled"), Some(&Value::Bool(false)));
        assert_eq!(changes.get("btc_only"), Some(&Value::Bool(false)));
        let restore = multi_underlying_restore_values();
        for (k, v) in restore.iter() {
            assert_eq!(changes.get(k), Some(v));
        }
        assert!(text.contains("BTC 전용 해제"));
    }

    #[tokio::test]
    async fn btc_menu_close_requires_confirmation() {
        let fake = Arc::new(FakeEngine::new(
            json!({"enabled": true, "btc_only": true}),
            empty_state(),
            base_status(),
        ));
        let bot = make_bot(fake.clone());

        let (_, kb) = bot.handle_btc_options_action("close").await;
        assert!(callbacks(&kb).iter().any(|c| c == "bo:confirm_close"));
        assert_eq!(channel_calls(&fake, "options_run_cycle"), 0);

        let _ = bot.handle_btc_options_action("confirm_close").await;
        let cycles: Vec<Value> = fake
            .calls()
            .into_iter()
            .filter(|(c, _)| c == "options_run_cycle")
            .map(|(_, a)| a)
            .collect();
        assert_eq!(cycles.len(), 1);
        assert_eq!(cycles[0]["force_exit"], json!(true));
        assert_eq!(cycles[0]["force_scan"], json!(false));
    }

    #[tokio::test]
    async fn btc_menu_warns_about_a_legacy_alt_position() {
        let fake = Arc::new(FakeEngine::new(
            json!({"enabled": true, "btc_only": true}),
            json!({"active_position": {"symbol": "SOL-260828-88-C"}}),
            base_status(),
        ));
        let bot = make_bot(fake.clone());
        let (text, _) = bot.handle_btc_options_action("status").await;
        assert!(text.contains("기존 SOL-260828-88-C 포지션은 청산될 때까지 계속 관리합니다"));
    }

    #[test]
    fn status_hides_expired_candidate_and_shows_found_time() {
        // Expiry from symbol suffix; now well past 2026-08-28 08:00 UTC.
        let old = json!({"symbol": "SOL-260828-88-C"});
        let now_after = 1_790_000_000_000i64; // after 2026-08-28
        assert!(candidate_expired(&old, now_after));

        let fresh = json!({
            "symbol": "BTC-991231-60000-C",
            "expiry_date_ms": 4_102_444_800_000i64,
            "found_at_ms": 1_791_000_000_000i64,
        });
        assert!(!candidate_expired(&fresh, now_after));
        assert!(candidate_found_text(&fresh).ends_with("KST 발견"));
        assert_eq!(candidate_found_text(&json!({})), "발견 시각 미기록");

        // Expired candidate dropped from the formatted status.
        let cfg = json!({"btc_only": true, "enabled": true});
        let status = json!({
            "enabled": true,
            "last_candidate": {"symbol": "SOL-260828-88-C", "signal_score": 0.81},
        });
        let text = format_options_status(&status, &cfg, now_after);
        assert!(!text.contains("SOL-260828-88-C"));
        assert!(!text.contains("최근 후보"));
    }

    #[tokio::test]
    async fn setting_buttons_do_not_touch_the_exchange() {
        let fake = Arc::new(FakeEngine::new(
            json!({"enabled": true, "btc_only": true, "btc_max_spread_pct": 0.1, "btc_dte_preset": "weekly"}),
            empty_state(),
            base_status(),
        ));
        let bot = make_bot(fake.clone());

        let _ = bot.handle_btc_options_action("dte:weekly").await;
        let _ = bot.handle_btc_options_action("spr:0.06").await;

        assert_eq!(channel_calls(&fake, "options_status"), 0);
        assert_eq!(channel_calls(&fake, "options_preflight"), 0);
        assert_eq!(channel_calls(&fake, "options_run_cycle"), 0);
    }
}
