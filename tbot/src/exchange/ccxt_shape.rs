//! Converts raw Binance USDⓈ-M futures JSON into ccxt-shaped values.

use std::collections::HashMap;

use rust_decimal::prelude::{FromPrimitive, ToPrimitive};
use rust_decimal::Decimal;
use serde_json::{json, Map, Number, Value};

/// ccxt Binance default fees for linear (USDT-margined) contracts.
const LINEAR_TAKER: f64 = 0.0005;
const LINEAR_MAKER: f64 = 0.0002;

const PRECISE_DIV_PRECISION: u32 = 28;

#[derive(Debug, Clone, Default)]
pub struct Markets {
    by_symbol: Map<String, Value>,
    by_id: HashMap<String, Vec<String>>,
}

impl Markets {
    pub fn from_exchange_info(info: &Value) -> Markets {
        let mut by_symbol = Map::new();
        let mut by_id: HashMap<String, Vec<String>> = HashMap::new();
        if let Some(list) = info.get("symbols").and_then(|v| v.as_array()) {
            for raw in list {
                let parsed = parse_market(raw);
                let symbol = parsed
                    .get("symbol")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let id = parsed
                    .get("id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                by_id.entry(id).or_default().push(symbol.clone());
                by_symbol.insert(symbol, parsed);
            }
        }
        Markets { by_symbol, by_id }
    }

    pub fn market(&self, symbol: &str) -> Option<&Value> {
        self.by_symbol.get(symbol)
    }

    /// Returns the swap/linear market for a Binance id (e.g. "SOLUSDT").
    pub fn market_by_id(&self, id: &str) -> Option<&Value> {
        let symbols = self.by_id.get(id)?;
        for symbol in symbols {
            if let Some(market) = self.by_symbol.get(symbol) {
                let swap = market
                    .get("swap")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);
                let linear = market
                    .get("linear")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);
                if swap && linear {
                    return Some(market);
                }
            }
        }
        None
    }

    pub fn symbols(&self) -> impl Iterator<Item = &String> {
        self.by_symbol.keys()
    }

    pub fn as_map(&self) -> &Map<String, Value> {
        &self.by_symbol
    }
}

// ---------------------------------------------------------------------------
// Helpers mirroring ccxt safe_* primitives
// ---------------------------------------------------------------------------

fn get<'a>(obj: &'a Value, key: &str) -> Option<&'a Value> {
    match obj {
        Value::Object(map) => map.get(key),
        _ => None,
    }
}

fn safe_string(obj: &Value, key: &str) -> Option<String> {
    match get(obj, key)? {
        Value::String(s) => Some(s.clone()),
        Value::Number(n) => Some(n.to_string()),
        _ => None,
    }
}

fn safe_string_2(obj: &Value, key1: &str, key2: &str) -> Option<String> {
    safe_string(obj, key1).or_else(|| safe_string(obj, key2))
}

fn safe_string_n(obj: &Value, keys: &[&str]) -> Option<String> {
    for key in keys {
        if let Some(v) = safe_string(obj, key) {
            return Some(v);
        }
    }
    None
}

fn safe_string_lower(obj: &Value, key: &str) -> Option<String> {
    safe_string(obj, key).map(|s| s.to_lowercase())
}

fn safe_string_lower_2(obj: &Value, key1: &str, key2: &str) -> Option<String> {
    safe_string_lower(obj, key1).or_else(|| safe_string_lower(obj, key2))
}

fn safe_integer(obj: &Value, key: &str) -> Option<i64> {
    match get(obj, key)? {
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64)),
        Value::String(s) => s
            .parse::<i64>()
            .ok()
            .or_else(|| s.parse::<f64>().ok().map(|f| f as i64)),
        Value::Bool(b) => Some(if *b { 1 } else { 0 }),
        _ => None,
    }
}

fn safe_integer_2(obj: &Value, key1: &str, key2: &str) -> Option<i64> {
    safe_integer(obj, key1).or_else(|| safe_integer(obj, key2))
}

fn safe_integer_n(obj: &Value, keys: &[&str]) -> Option<i64> {
    for key in keys {
        if let Some(v) = safe_integer(obj, key) {
            return Some(v);
        }
    }
    None
}

fn safe_bool(obj: &Value, key: &str) -> Option<bool> {
    match get(obj, key)? {
        Value::Bool(b) => Some(*b),
        Value::Number(n) => n.as_f64().map(|f| f != 0.0),
        Value::String(s) => Some(!s.is_empty() && s != "0" && s != "false"),
        _ => None,
    }
}

fn safe_list(obj: &Value, key: &str) -> Option<Vec<Value>> {
    match get(obj, key)? {
        Value::Array(arr) => Some(arr.clone()),
        _ => None,
    }
}

fn safe_dict(obj: &Value, key: &str) -> Option<Map<String, Value>> {
    match get(obj, key)? {
        Value::Object(map) => Some(map.clone()),
        _ => None,
    }
}

fn safe_dict_2(obj: &Value, key1: &str, key2: &str) -> Map<String, Value> {
    safe_dict(obj, key1)
        .or_else(|| safe_dict(obj, key2))
        .unwrap_or_default()
}

fn parse_decimal(value: &str) -> Option<Decimal> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return None;
    }
    trimmed.parse::<Decimal>().ok()
}

fn safe_number(obj: &Value, key: &str) -> Option<f64> {
    let s = safe_string(obj, key)?;
    parse_decimal(&s).and_then(|d| d.to_f64())
}

fn safe_number_2(obj: &Value, key1: &str, key2: &str) -> Option<f64> {
    safe_number(obj, key1).or_else(|| safe_number(obj, key2))
}

fn num(f: f64) -> Value {
    match Number::from_f64(f) {
        Some(n) => Value::Number(n),
        None => Value::Null,
    }
}

fn opt_num(f: Option<f64>) -> Value {
    match f {
        Some(v) => num(v),
        None => Value::Null,
    }
}

fn dec_to_str(d: &Decimal) -> String {
    d.normalize().to_string()
}

/// ccxt `omit_zero`: None for missing, empty or numerically zero ("0.0000").
fn omit_zero(s: Option<String>) -> Option<String> {
    let v = s?;
    if v.is_empty() || v.trim().parse::<f64>().map(|f| f == 0.0).unwrap_or(false) {
        None
    } else {
        Some(v)
    }
}

fn number_to_string(v: Option<&Value>) -> Option<String> {
    match v? {
        Value::String(s) => Some(s.clone()),
        Value::Number(n) => Some(n.to_string()),
        _ => None,
    }
}

/// ccxt `parse_precision`: returns precision digits as a decimal string
/// (e.g. "2" -> "0.01", "8" -> "0.00000001").
/// ccxt `parse_precision`: decimal places ("8") -> tick ("0.00000001").
fn parse_precision(s: &str) -> String {
    let Ok(n) = s.trim().parse::<i64>() else {
        return String::new();
    };
    match n {
        0 => "1".to_string(),
        n if n > 0 => format!("0.{}1", "0".repeat((n - 1) as usize)),
        n => format!("1{}0", "0".repeat((-n - 1) as usize)),
    }
}

/// ccxt `precision_from_string`: decimal places of a tick ("0.01" -> 2, "1e-7" -> 7).
fn precision_from_string(s: &str) -> Option<u32> {
    if let Some(idx) = s.find(['e', 'E']) {
        let exp: i64 = s[idx + 1..].parse().ok()?;
        return u32::try_from(-exp).ok();
    }
    let Some(dot) = s.find('.') else {
        return Some(0);
    };
    Some(s[dot + 1..].trim_end_matches('0').len() as u32)
}

fn yymmdd(ms: i64) -> String {
    let (year, month, day) = civil_from_days(ms.div_euclid(86_400_000));
    format!("{:02}{:02}{:02}", year % 100, month, day)
}

/// Convert days since 1970-01-01 to (year, month, day). Howard Hinnant algorithm.
fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    (y, m, d)
}

fn split_datetime(ms: i64) -> (i64, i64, i64, i64, i64, i64, i64) {
    let days = ms.div_euclid(86_400_000);
    let rem = ms.rem_euclid(86_400_000);
    let (year, month, day) = civil_from_days(days);
    let hour = rem / 3_600_000;
    let minute = (rem % 3_600_000) / 60_000;
    let second = (rem % 60_000) / 1000;
    let milli = rem % 1000;
    (year, month, day, hour, minute, second, milli)
}

/// ccxt `iso8601`: "YYYY-MM-DDTHH:MM:SS.mmmZ" or null.
pub fn iso8601(ms: Option<i64>) -> Value {
    match ms {
        None => Value::Null,
        Some(ms) => {
            let (year, month, day, hour, minute, second, milli) = split_datetime(ms);
            Value::String(format!(
                "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:03}Z",
                year, month, day, hour, minute, second, milli
            ))
        }
    }
}

fn safe_symbol(market_id: &str, market: Option<&Value>, fallback: &str) -> String {
    if let Some(m) = market {
        if let Some(s) = m.get("symbol").and_then(|v| v.as_str()) {
            return s.to_string();
        }
    }
    if !market_id.is_empty() {
        market_id.to_string()
    } else {
        fallback.to_string()
    }
}

// ---------------------------------------------------------------------------
// parse_market
// ---------------------------------------------------------------------------

pub fn parse_market(raw: &Value) -> Value {
    let id = safe_string(raw, "symbol");
    let id_str = id.clone().unwrap_or_default();
    let option_parts: Vec<&str> = id_str.split('-').collect();
    let option_base: Option<String> = option_parts.first().map(|s| s.to_string());
    let lowercase_id = safe_string_lower(raw, "symbol");
    let base_id = safe_string(raw, "baseAsset").or_else(|| option_base.clone());
    let quote_id = safe_string(raw, "quoteAsset");
    let base = base_id.clone();
    let quote = quote_id.clone();
    let contract_type = safe_string(raw, "contractType");
    let contract = get(raw, "contractType").is_some();
    let mut expiry = safe_integer_2(raw, "deliveryDate", "expiryDate");
    let mut settle_id = safe_string(raw, "marginAsset");
    let mut swap = false;
    let mut future = false;
    let option = false;

    if contract_type.as_deref() == Some("PERPETUAL") || expiry == Some(4_133_404_800_000) {
        expiry = None;
        swap = true;
    } else if safe_string(raw, "underlying").is_some() {
        settle_id = settle_id.or_else(|| Some("USDT".to_string()));
    } else if expiry.is_some() {
        future = true;
    }

    let settle = settle_id.clone();
    let spot = !contract;

    let filters = safe_list(raw, "filters").unwrap_or_default();
    let mut filters_by_type: HashMap<String, Value> = HashMap::new();
    for filter in &filters {
        if let Some(s) = filter.get("filterType").and_then(|v| v.as_str()) {
            filters_by_type.insert(s.to_string(), filter.clone());
        }
    }

    let status = safe_string_2(raw, "status", "contractStatus");

    let base_str = base.clone().unwrap_or_default();
    let quote_str = quote.clone().unwrap_or_default();
    let mut symbol = format!("{}/{}", base_str, quote_str);
    let strike: Option<String> = None;

    let mut contract_size: Option<f64> = None;
    let mut linear: Option<bool> = None;
    let mut inverse: Option<bool> = None;
    let mut taker = LINEAR_TAKER;
    let mut maker = LINEAR_MAKER;

    if contract {
        if swap {
            let settle_str = settle.clone().unwrap_or_default();
            symbol = format!("{}:{}", symbol, settle_str);
        } else if future {
            let settle_str = settle.clone().unwrap_or_default();
            let exp = expiry.unwrap_or(0);
            symbol = format!("{}:{}-{}", symbol, settle_str, yymmdd(exp));
        }
        contract_size = safe_number_2(raw, "contractSize", "unit").or(Some(1.0));
        let is_linear = settle == quote;
        let is_inverse = settle == base;
        linear = Some(is_linear);
        inverse = Some(is_inverse);
        if is_linear {
            taker = LINEAR_TAKER;
            maker = LINEAR_MAKER;
        } else if is_inverse {
            taker = 0.0;
            maker = 0.0;
        }
    }

    let mut active = Some(status.as_deref() == Some("TRADING"));
    if spot {
        let permissions = safe_list(raw, "permissions").unwrap_or_default();
        for perm in &permissions {
            if perm.as_str() == Some("TRD_GRP_003") {
                active = Some(false);
                break;
            }
        }
    }
    let is_margin_trading_allowed = safe_bool(raw, "isMarginTradingAllowed").unwrap_or(false);

    let margin_modes: Value = if spot {
        json!({
            "cross": false,
            "isolated": false,
        })
    } else if linear == Some(true) || inverse == Some(true) {
        json!({
            "cross": true,
            "isolated": true,
        })
    } else {
        Value::Null
    };

    let unified_type: Value = if spot {
        Value::String("spot".to_string())
    } else if swap {
        Value::String("swap".to_string())
    } else if future {
        Value::String("future".to_string())
    } else if option {
        Value::String("option".to_string())
    } else {
        Value::Null
    };

    let margin = spot && is_margin_trading_allowed;

    let amount_id = safe_string_2(raw, "quantityPrecision", "quantityScale").unwrap_or_default();
    let price_id = safe_string_2(raw, "pricePrecision", "priceScale").unwrap_or_default();
    let base_prec_id = safe_string(raw, "baseAssetPrecision").unwrap_or_default();
    let quote_prec_id = safe_string(raw, "quotePrecision").unwrap_or_default();

    let amount_precision = parse_decimal(&parse_precision(&amount_id)).and_then(|d| d.to_f64());
    let price_precision = parse_decimal(&parse_precision(&price_id)).and_then(|d| d.to_f64());
    let base_precision = parse_decimal(&parse_precision(&base_prec_id)).and_then(|d| d.to_f64());
    let quote_precision = parse_decimal(&parse_precision(&quote_prec_id)).and_then(|d| d.to_f64());

    let mut order = Map::new();
    order.insert("id".to_string(), Value::String(id_str.clone()));
    order.insert(
        "lowercaseId".to_string(),
        match lowercase_id {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    order.insert("symbol".to_string(), Value::String(symbol));
    order.insert(
        "base".to_string(),
        match base.clone() {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    order.insert(
        "quote".to_string(),
        match quote.clone() {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    order.insert(
        "settle".to_string(),
        match settle.clone() {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    order.insert(
        "baseId".to_string(),
        match base_id.clone() {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    order.insert(
        "quoteId".to_string(),
        match quote_id.clone() {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    order.insert(
        "settleId".to_string(),
        match settle_id.clone() {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    order.insert("type".to_string(), unified_type);
    order.insert("spot".to_string(), Value::Bool(spot));
    order.insert("margin".to_string(), Value::Bool(margin));
    order.insert("marginModes".to_string(), margin_modes);
    order.insert("swap".to_string(), Value::Bool(swap));
    order.insert("future".to_string(), Value::Bool(future));
    order.insert("option".to_string(), Value::Bool(option));
    order.insert(
        "active".to_string(),
        match active {
            Some(b) => Value::Bool(b),
            None => Value::Null,
        },
    );
    order.insert("contract".to_string(), Value::Bool(contract));
    order.insert(
        "linear".to_string(),
        match linear {
            Some(b) => Value::Bool(b),
            None => Value::Null,
        },
    );
    order.insert(
        "inverse".to_string(),
        match inverse {
            Some(b) => Value::Bool(b),
            None => Value::Null,
        },
    );
    order.insert("taker".to_string(), num(taker));
    order.insert("maker".to_string(), num(maker));
    order.insert("contractSize".to_string(), opt_num(contract_size));
    order.insert(
        "expiry".to_string(),
        match expiry {
            Some(v) => Value::Number(Number::from(v)),
            None => Value::Null,
        },
    );
    order.insert("expiryDatetime".to_string(), iso8601(expiry));
    order.insert(
        "strike".to_string(),
        match &strike {
            Some(s) => match parse_decimal(s).and_then(|d| d.to_f64()) {
                Some(f) => num(f),
                None => Value::Null,
            },
            None => Value::Null,
        },
    );
    order.insert(
        "optionType".to_string(),
        match safe_string_lower(raw, "side") {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );

    let mut precision = Map::new();
    precision.insert("amount".to_string(), opt_num(amount_precision));
    precision.insert("price".to_string(), opt_num(price_precision));
    precision.insert("base".to_string(), opt_num(base_precision));
    precision.insert("quote".to_string(), opt_num(quote_precision));
    order.insert("precision".to_string(), Value::Object(precision));

    let mut leverage_limits = Map::new();
    leverage_limits.insert("min".to_string(), Value::Null);
    leverage_limits.insert("max".to_string(), Value::Null);

    let mut amount_limits = Map::new();
    amount_limits.insert("min".to_string(), opt_num(safe_number(raw, "minQty")));
    amount_limits.insert("max".to_string(), opt_num(safe_number(raw, "maxQty")));

    let mut price_limits = Map::new();
    price_limits.insert("min".to_string(), Value::Null);
    price_limits.insert("max".to_string(), Value::Null);

    let mut cost_limits = Map::new();
    cost_limits.insert("min".to_string(), Value::Null);
    cost_limits.insert("max".to_string(), Value::Null);

    let mut limits = Map::new();
    limits.insert("leverage".to_string(), Value::Object(leverage_limits));
    limits.insert("amount".to_string(), Value::Object(amount_limits));
    limits.insert("price".to_string(), Value::Object(price_limits));
    limits.insert("cost".to_string(), Value::Object(cost_limits));
    order.insert("limits".to_string(), Value::Object(limits));

    order.insert("info".to_string(), raw.clone());
    order.insert(
        "created".to_string(),
        match safe_integer(raw, "onboardDate") {
            Some(v) => Value::Number(Number::from(v)),
            None => Value::Null,
        },
    );

    // Apply filter overrides.
    if let Some(filter) = filters_by_type.get("PRICE_FILTER") {
        let mut price_limits = Map::new();
        price_limits.insert("min".to_string(), opt_num(safe_number(filter, "minPrice")));
        price_limits.insert("max".to_string(), opt_num(safe_number(filter, "maxPrice")));
        if let Some(limits) = order.get_mut("limits").and_then(|v| v.as_object_mut()) {
            limits.insert("price".to_string(), Value::Object(price_limits));
        }
        if let Some(precision) = order.get_mut("precision").and_then(|v| v.as_object_mut()) {
            precision.insert(
                "price".to_string(),
                opt_num(safe_number(filter, "tickSize")),
            );
        }
    }
    if let Some(filter) = filters_by_type.get("LOT_SIZE") {
        if let Some(precision) = order.get_mut("precision").and_then(|v| v.as_object_mut()) {
            precision.insert(
                "amount".to_string(),
                opt_num(safe_number(filter, "stepSize")),
            );
        }
        let mut amount_limits = Map::new();
        amount_limits.insert("min".to_string(), opt_num(safe_number(filter, "minQty")));
        amount_limits.insert("max".to_string(), opt_num(safe_number(filter, "maxQty")));
        if let Some(limits) = order.get_mut("limits").and_then(|v| v.as_object_mut()) {
            limits.insert("amount".to_string(), Value::Object(amount_limits));
        }
    }
    if let Some(filter) = filters_by_type.get("MARKET_LOT_SIZE") {
        let mut market_limits = Map::new();
        market_limits.insert("min".to_string(), opt_num(safe_number(filter, "minQty")));
        market_limits.insert("max".to_string(), opt_num(safe_number(filter, "maxQty")));
        if let Some(limits) = order.get_mut("limits").and_then(|v| v.as_object_mut()) {
            limits.insert("market".to_string(), Value::Object(market_limits));
        }
    }
    if filters_by_type.contains_key("MIN_NOTIONAL") || filters_by_type.contains_key("NOTIONAL") {
        let filter = safe_dict_2(
            &Value::Object(filters_by_type.clone().into_iter().collect()),
            "MIN_NOTIONAL",
            "NOTIONAL",
        );
        let filter_value = Value::Object(filter);
        if let Some(limits) = order.get_mut("limits").and_then(|v| v.as_object_mut()) {
            if let Some(cost) = limits.get_mut("cost").and_then(|v| v.as_object_mut()) {
                cost.insert(
                    "min".to_string(),
                    opt_num(safe_number_2(&filter_value, "minNotional", "notional")),
                );
                cost.insert(
                    "max".to_string(),
                    opt_num(safe_number(&filter_value, "maxNotional")),
                );
            }
        }
    }

    Value::Object(order)
}

// ---------------------------------------------------------------------------
// parse_ticker
// ---------------------------------------------------------------------------

pub fn parse_ticker(raw: &Value, market: Option<&Value>) -> Value {
    let timestamp = safe_integer_2(raw, "closeTime", "time");
    let market_id = safe_string(raw, "symbol").unwrap_or_default();
    let symbol = safe_symbol(&market_id, market, &market_id);

    let last = safe_string(raw, "lastPrice");
    let w_avg = safe_string(raw, "weightedAvgPrice");
    let is_coinm = get(raw, "baseVolume").is_some();
    let base_volume: Option<String>;
    let quote_volume: Option<String>;
    if is_coinm {
        base_volume = safe_string(raw, "baseVolume");
        quote_volume = match (&base_volume, &w_avg) {
            (Some(b), Some(w)) => match (parse_decimal(b), parse_decimal(w)) {
                (Some(bd), Some(wd)) => Some(dec_to_str(&(bd * wd))),
                _ => None,
            },
            _ => None,
        };
    } else {
        base_volume = safe_string(raw, "volume");
        quote_volume = safe_string_2(raw, "quoteVolume", "amount");
    }

    let mut t = Map::new();
    t.insert("symbol".to_string(), Value::String(symbol));
    t.insert(
        "timestamp".to_string(),
        match timestamp {
            Some(v) => Value::Number(Number::from(v)),
            None => Value::Null,
        },
    );
    t.insert("datetime".to_string(), iso8601(timestamp));
    t.insert(
        "high".to_string(),
        str_or_null_opt(safe_string_2(raw, "highPrice", "high")),
    );
    t.insert(
        "low".to_string(),
        str_or_null_opt(safe_string_2(raw, "lowPrice", "low")),
    );
    t.insert(
        "bid".to_string(),
        str_or_null_opt(safe_string(raw, "bidPrice")),
    );
    t.insert(
        "bidVolume".to_string(),
        str_or_null_opt(safe_string(raw, "bidQty")),
    );
    t.insert(
        "ask".to_string(),
        str_or_null_opt(safe_string(raw, "askPrice")),
    );
    t.insert(
        "askVolume".to_string(),
        str_or_null_opt(safe_string(raw, "askQty")),
    );
    t.insert("vwap".to_string(), str_or_null_opt(w_avg.clone()));
    t.insert(
        "open".to_string(),
        str_or_null_opt(safe_string_2(raw, "openPrice", "open")),
    );
    t.insert("close".to_string(), str_or_null_opt(last.clone()));
    t.insert("last".to_string(), str_or_null_opt(last.clone()));
    t.insert(
        "previousClose".to_string(),
        str_or_null_opt(safe_string(raw, "prevClosePrice")),
    );
    t.insert(
        "change".to_string(),
        str_or_null_opt(safe_string(raw, "priceChange")),
    );
    t.insert(
        "percentage".to_string(),
        str_or_null_opt(safe_string(raw, "priceChangePercent")),
    );
    t.insert("average".to_string(), Value::Null);
    t.insert(
        "baseVolume".to_string(),
        str_or_null_opt(base_volume.clone()),
    );
    t.insert(
        "quoteVolume".to_string(),
        str_or_null_opt(quote_volume.clone()),
    );
    t.insert(
        "markPrice".to_string(),
        str_or_null_opt(safe_string(raw, "markPrice")),
    );
    t.insert(
        "indexPrice".to_string(),
        str_or_null_opt(safe_string(raw, "indexPrice")),
    );
    t.insert("info".to_string(), raw.clone());

    safe_ticker(t, market)
}

fn str_or_null_opt(s: Option<String>) -> Value {
    match s {
        Some(v) => Value::String(v),
        None => Value::Null,
    }
}

fn safe_ticker(mut ticker: Map<String, Value>, market: Option<&Value>) -> Value {
    let open = omit_zero(dict_safe_string(&ticker, "open"));
    let close = omit_zero(dict_safe_string_2(&ticker, "close", "last"));
    let change = omit_zero(dict_safe_string(&ticker, "change"));
    let percentage = omit_zero(dict_safe_string(&ticker, "percentage"));
    let average = omit_zero(dict_safe_string(&ticker, "average"));
    let vwap = dict_safe_string(&ticker, "vwap");
    let base_volume = dict_safe_string(&ticker, "baseVolume");
    let quote_volume = dict_safe_string(&ticker, "quoteVolume");
    let mut vwap = vwap;
    if vwap.is_none() {
        if let (Some(q), Some(b)) = (omit_zero(quote_volume.clone()), base_volume.clone()) {
            if let (Some(qd), Some(bd)) = (parse_decimal(&q), parse_decimal(&b)) {
                if !bd.is_zero() {
                    vwap = Some(dec_to_str(&(qd / bd)));
                }
            }
        }
    }
    let mut open = open;
    let mut close = close;
    let mut change = change;
    let mut percentage = percentage;
    let mut average = average;

    if change.is_some() {
        if close.is_none() && average.is_some() {
            if let (Some(a), Some(c)) = (&average, &change) {
                if let (Some(ad), Some(cd)) = (parse_decimal(a), parse_decimal(c)) {
                    let two = Decimal::from(2);
                    close = Some(dec_to_str(&(ad + (cd / two))));
                }
            }
        }
        if open.is_none() && close.is_some() {
            if let (Some(c), Some(ch)) = (&close, &change) {
                if let (Some(cd), Some(chd)) = (parse_decimal(c), parse_decimal(ch)) {
                    open = Some(dec_to_str(&(cd - chd)));
                }
            }
        }
    } else if percentage.is_some() {
        if close.is_none() && average.is_some() {
            if let (Some(a), Some(p)) = (&average, &percentage) {
                if let (Some(ad), Some(pd)) = (parse_decimal(a), parse_decimal(p)) {
                    let two = Decimal::from(2);
                    let hundred = Decimal::from(100);
                    let one = Decimal::from(1);
                    let open_add_close = ad * two;
                    let denominator = two + (pd / hundred);
                    let calc_open = open.as_ref().and_then(|o| parse_decimal(o)).or_else(|| {
                        if !denominator.is_zero() {
                            Some(open_add_close / denominator)
                        } else {
                            None
                        }
                    });
                    if let Some(co) = calc_open {
                        close = Some(dec_to_str(&(co * (one + (pd / hundred)))));
                    }
                }
            }
        }
        if open.is_none() && close.is_some() {
            if let (Some(c), Some(p)) = (&close, &percentage) {
                if let (Some(cd), Some(pd)) = (parse_decimal(c), parse_decimal(p)) {
                    let hundred = Decimal::from(100);
                    let one = Decimal::from(1);
                    let denom = one + (pd / hundred);
                    if !denom.is_zero() {
                        open = Some(dec_to_str(&(cd / denom)));
                    }
                }
            }
        }
    }

    if change.is_none() {
        if close.is_some() && open.is_some() {
            if let (Some(c), Some(o)) = (&close, &open) {
                if let (Some(cd), Some(od)) = (parse_decimal(c), parse_decimal(o)) {
                    change = Some(dec_to_str(&(cd - od)));
                }
            }
        } else if close.is_some() && percentage.is_some() {
            if let (Some(c), Some(p)) = (&close, &percentage) {
                if let (Some(cd), Some(pd)) = (parse_decimal(c), parse_decimal(p)) {
                    let hundred = Decimal::from(100);
                    change = Some(dec_to_str(&((pd / hundred) * (cd / hundred))));
                }
            }
        } else if open.is_some() && percentage.is_some() {
            if let (Some(o), Some(p)) = (&open, &percentage) {
                if let (Some(od), Some(pd)) = (parse_decimal(o), parse_decimal(p)) {
                    let hundred = Decimal::from(100);
                    change = Some(dec_to_str(&(od * (pd / hundred))));
                }
            }
        }
    }

    if open.is_some() {
        if percentage.is_none() && change.is_some() {
            if let (Some(o), Some(ch)) = (&open, &change) {
                if let (Some(od), Some(chd)) = (parse_decimal(o), parse_decimal(ch)) {
                    if !od.is_zero() {
                        let hundred = Decimal::from(100);
                        percentage = Some(dec_to_str(&((chd / od) * hundred)));
                    }
                }
            }
        }
        if close.is_none() && change.is_some() {
            if let (Some(o), Some(ch)) = (&open, &change) {
                if let (Some(od), Some(chd)) = (parse_decimal(o), parse_decimal(ch)) {
                    close = Some(dec_to_str(&(od + chd)));
                }
            }
        }
        if close.is_none() && average.is_some() {
            if let Some(a) = &average {
                if let Some(ad) = parse_decimal(a) {
                    close = Some(dec_to_str(&(ad * Decimal::from(2))));
                }
            }
        }
        if average.is_none() && close.is_some() {
            let precision;
            if let Some(m) = market {
                if let Some(mp) = m.get("precision").and_then(|v| v.as_object()) {
                    // ccxt safe_string: the tick may be stored as a number.
                    let tick = mp.get("price").and_then(|v| match v {
                        Value::String(s) => Some(s.clone()),
                        Value::Number(n) => Some(n.to_string()),
                        _ => None,
                    });
                    if let Some(pp) = tick {
                        precision = precision_from_string(&pp).unwrap_or(18);
                    } else {
                        precision = 18;
                    }
                } else {
                    precision = 18;
                }
            } else {
                precision = 18;
            }
            if let (Some(o), Some(c)) = (&open, &close) {
                if let (Some(od), Some(cd)) = (parse_decimal(o), parse_decimal(c)) {
                    let two = Decimal::from(2);
                    let mut avg_dec = (od + cd) / two;
                    avg_dec = round_dec(avg_dec, precision);
                    average = Some(dec_to_str(&avg_dec));
                }
            }
        }
    }

    let close_parsed =
        omit_zero(close.clone()).and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64()));

    ticker.insert(
        "bid".to_string(),
        opt_num(
            omit_zero(dict_safe_string(&ticker, "bid"))
                .and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64())),
        ),
    );
    ticker.insert(
        "bidVolume".to_string(),
        opt_num(dict_safe_number(&ticker, "bidVolume")),
    );
    ticker.insert(
        "ask".to_string(),
        opt_num(
            omit_zero(dict_safe_string(&ticker, "ask"))
                .and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64())),
        ),
    );
    ticker.insert(
        "askVolume".to_string(),
        opt_num(dict_safe_number(&ticker, "askVolume")),
    );
    ticker.insert(
        "high".to_string(),
        opt_num(
            omit_zero(dict_safe_string(&ticker, "high"))
                .and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64())),
        ),
    );
    ticker.insert(
        "low".to_string(),
        opt_num(
            omit_zero(dict_safe_string(&ticker, "low"))
                .and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64())),
        ),
    );
    ticker.insert(
        "open".to_string(),
        opt_num(omit_zero(open).and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64()))),
    );
    ticker.insert(
        "close".to_string(),
        match close_parsed {
            Some(f) => num(f),
            None => Value::Null,
        },
    );
    ticker.insert(
        "last".to_string(),
        match close_parsed {
            Some(f) => num(f),
            None => Value::Null,
        },
    );
    ticker.insert(
        "change".to_string(),
        opt_num(change.and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64()))),
    );
    ticker.insert(
        "percentage".to_string(),
        opt_num(percentage.and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64()))),
    );
    ticker.insert(
        "average".to_string(),
        opt_num(average.and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64()))),
    );
    ticker.insert(
        "vwap".to_string(),
        opt_num(vwap.and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64()))),
    );
    ticker.insert(
        "baseVolume".to_string(),
        opt_num(base_volume.and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64()))),
    );
    ticker.insert(
        "quoteVolume".to_string(),
        opt_num(quote_volume.and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64()))),
    );
    ticker.insert(
        "previousClose".to_string(),
        opt_num(dict_safe_number(&ticker, "previousClose")),
    );
    ticker.insert(
        "indexPrice".to_string(),
        opt_num(dict_safe_number(&ticker, "indexPrice")),
    );
    ticker.insert(
        "markPrice".to_string(),
        opt_num(dict_safe_number(&ticker, "markPrice")),
    );

    Value::Object(ticker)
}

/// ccxt Precise.string_div(.., precision) truncates toward zero.
fn round_dec(d: Decimal, places: u32) -> Decimal {
    d.trunc_with_scale(places.min(28))
}

fn dict_safe_string(map: &Map<String, Value>, key: &str) -> Option<String> {
    match map.get(key)? {
        Value::String(s) => Some(s.clone()),
        Value::Number(n) => Some(n.to_string()),
        _ => None,
    }
}

fn dict_safe_string_2(map: &Map<String, Value>, key1: &str, key2: &str) -> Option<String> {
    dict_safe_string(map, key1).or_else(|| dict_safe_string(map, key2))
}

fn dict_safe_number(map: &Map<String, Value>, key: &str) -> Option<f64> {
    let s = dict_safe_string(map, key)?;
    parse_decimal(&s).and_then(|d| d.to_f64())
}

// ---------------------------------------------------------------------------
// parse_order
// ---------------------------------------------------------------------------

fn parse_order_status(status: Option<&str>) -> Option<String> {
    let s = status?;
    let mapped = match s {
        "NEW" => "open",
        "PARTIALLY_FILLED" => "open",
        "ACCEPTED" => "open",
        "TRIGGERING" => "open",
        "FILLED" => "closed",
        "TRIGGERED" => "closed",
        "FINISHED" => "closed",
        "CANCELED" => "canceled",
        "CANCELLED" => "canceled",
        "PENDING_CANCEL" => "canceling",
        "REJECTED" => "rejected",
        "EXPIRED" => "expired",
        "EXPIRED_IN_MATCH" => "expired",
        _ => s,
    };
    Some(mapped.to_string())
}

fn parse_order_type_by_market(t: &str, market_type: &str) -> String {
    let mut t = t.to_lowercase();
    if t == "market" && market_type == "contract" {
        // no change; kept as-is
    }
    if let Some(rest) = t.strip_prefix("mm") {
        if rest == "arket" {
            t = "market".to_string();
        }
    }
    t
}

pub fn parse_order(raw: &Value, market: Option<&Value>) -> Value {
    let code = safe_string(raw, "code");
    if let Some(c) = &code {
        let msg = safe_string(raw, "msg");
        if c != "200"
            && !(msg.as_deref() == Some("success")
                || msg.as_deref() == Some("The operation of cancel all open order is done."))
        {
            let mut o = Map::new();
            o.insert("info".to_string(), raw.clone());
            o.insert("status".to_string(), Value::String("rejected".to_string()));
            return safe_order(o, market);
        }
    }

    let status = parse_order_status(
        safe_string_n(raw, &["status", "strategyStatus", "algoStatus"]).as_deref(),
    );
    let market_id = safe_string(raw, "symbol").unwrap_or_default();
    let is_contract = get(raw, "positionSide").is_some() || get(raw, "cumQuote").is_some();
    let market_type = if is_contract { "contract" } else { "spot" };
    let symbol = safe_symbol(&market_id, market, &market_id);

    let filled = safe_string(raw, "executedQty").unwrap_or_else(|| "0".to_string());
    let timestamp = safe_integer_n(
        raw,
        &[
            "time",
            "createTime",
            "workingTime",
            "transactTime",
            "updateTime",
        ],
    );
    let mut last_trade_timestamp: Option<i64> = None;
    if get(raw, "transactTime").is_some() || get(raw, "updateTime").is_some() {
        let timestamp_value = safe_integer_2(raw, "updateTime", "transactTime");
        if status.as_deref() == Some("open") {
            if let Some(fd) = parse_decimal(&filled) {
                if fd > Decimal::ZERO {
                    last_trade_timestamp = timestamp_value;
                }
            }
        } else if status.as_deref() == Some("closed") {
            last_trade_timestamp = timestamp_value;
        }
    }
    let last_update_timestamp = safe_integer_2(raw, "transactTime", "updateTime");
    let average = safe_string(raw, "avgPrice");
    let price = safe_string(raw, "price");
    let amount = safe_string_2(raw, "origQty", "quantity");
    let cost1 = safe_string_2(raw, "cummulativeQuoteQty", "cumQuote");
    let cost = safe_string(raw, "cumBase").or(cost1);
    let order_type = safe_string_lower_2(raw, "type", "orderType");
    let side = safe_string_lower(raw, "side");
    let fills = safe_list(raw, "fills").unwrap_or_default();
    let mut time_in_force = safe_string(raw, "timeInForce");
    if time_in_force.as_deref() == Some("GTX") {
        time_in_force = Some("PO".to_string());
    }
    let post_only =
        order_type.as_deref() == Some("limit_maker") || time_in_force.as_deref() == Some("PO");
    let stop_price_string = safe_string_2(raw, "stopPrice", "triggerPrice");
    let trigger_price =
        omit_zero(stop_price_string).and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64()));
    let fee_cost = safe_number(raw, "fee");
    let fee = match fee_cost {
        Some(c) => {
            let mut f = Map::new();
            f.insert(
                "currency".to_string(),
                match safe_string(raw, "quoteAsset") {
                    Some(s) => Value::String(s),
                    None => Value::Null,
                },
            );
            f.insert("cost".to_string(), num(c));
            f.insert("rate".to_string(), Value::Null);
            Some(Value::Object(f))
        }
        None => None,
    };

    let order_type_final = match &order_type {
        Some(t) => Value::String(parse_order_type_by_market(t, market_type)),
        None => Value::Null,
    };

    let mut order = Map::new();
    order.insert("info".to_string(), raw.clone());
    order.insert(
        "id".to_string(),
        match safe_string_n(raw, &["strategyId", "orderId", "algoId"]) {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    order.insert(
        "clientOrderId".to_string(),
        match safe_string_n(
            raw,
            &["clientOrderId", "newClientStrategyId", "clientAlgoId"],
        ) {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    order.insert(
        "timestamp".to_string(),
        match timestamp {
            Some(v) => Value::Number(Number::from(v)),
            None => Value::Null,
        },
    );
    order.insert("datetime".to_string(), iso8601(timestamp));
    order.insert(
        "lastTradeTimestamp".to_string(),
        match last_trade_timestamp {
            Some(v) => Value::Number(Number::from(v)),
            None => Value::Null,
        },
    );
    order.insert(
        "lastUpdateTimestamp".to_string(),
        match last_update_timestamp {
            Some(v) => Value::Number(Number::from(v)),
            None => Value::Null,
        },
    );
    order.insert("symbol".to_string(), Value::String(symbol));
    order.insert("type".to_string(), order_type_final);
    order.insert(
        "timeInForce".to_string(),
        match time_in_force {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    order.insert("postOnly".to_string(), Value::Bool(post_only));
    order.insert(
        "reduceOnly".to_string(),
        match safe_bool(raw, "reduceOnly") {
            Some(b) => Value::Bool(b),
            None => Value::Null,
        },
    );
    order.insert(
        "side".to_string(),
        match side {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    order.insert(
        "price".to_string(),
        match price {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    order.insert("triggerPrice".to_string(), opt_num(trigger_price));
    order.insert(
        "amount".to_string(),
        match amount {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    order.insert(
        "cost".to_string(),
        match cost {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    order.insert(
        "average".to_string(),
        match average {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    order.insert("filled".to_string(), Value::String(filled));
    order.insert("remaining".to_string(), Value::Null);
    order.insert(
        "status".to_string(),
        match status {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    order.insert(
        "fee".to_string(),
        match fee {
            Some(v) => v,
            None => Value::Null,
        },
    );
    order.insert("trades".to_string(), Value::Array(fills));

    safe_order(order, market)
}

#[allow(clippy::too_many_lines)]
fn safe_order(mut order: Map<String, Value>, market: Option<&Value>) -> Value {
    let amount = omit_zero(dict_safe_string(&order, "amount"));
    let remaining = dict_safe_string(&order, "remaining");
    let mut filled = dict_safe_string(&order, "filled");
    let mut cost = dict_safe_string(&order, "cost");
    let mut average = omit_zero(dict_safe_string(&order, "average"));
    let mut price = omit_zero(dict_safe_string(&order, "price"));
    let mut last_trade_time_timestamp = dict_safe_integer(&order, "lastTradeTimestamp");
    let mut symbol = dict_safe_string(&order, "symbol");
    let mut side = dict_safe_string(&order, "side");
    let status = dict_safe_string(&order, "status");
    let parse_filled = filled.is_none();
    let parse_cost = cost.is_none();
    let parse_last_trade_time_timestamp = last_trade_time_timestamp.is_none();
    let fee_value = order.get("fee").cloned();
    let parse_fee = fee_value.is_none() || fee_value == Some(Value::Null);
    let parse_fees = order.get("fees").is_none();
    let parse_symbol = symbol.is_none();
    let parse_side = side.is_none();
    let should_parse_fees = parse_fee || parse_fees;
    let mut fees: Vec<Value> = dict_safe_list(&order, "fees").unwrap_or_default();
    let mut trades: Vec<Value> = Vec::new();

    let trigger_opt = dict_safe_string(&order, "triggerPrice");
    let sl_opt = dict_safe_string(&order, "stopLossPrice");
    let tp_opt = dict_safe_string(&order, "takeProfitPrice");
    let is_trigger_or_sltp_order = trigger_opt.is_some() || sl_opt.is_some() || tp_opt.is_some();

    if parse_filled || parse_cost || should_parse_fees {
        let raw_trades = order
            .get("trades")
            .cloned()
            .unwrap_or_else(|| Value::Array(Vec::new()));
        let raw_trades_arr = match &raw_trades {
            Value::Array(a) => a.clone(),
            _ => Vec::new(),
        };
        let first_trade = raw_trades_arr.first();
        let trades_are_parsed = first_trade
            .map(|t| t.get("info").is_some() && t.get("id").is_some())
            .unwrap_or(false);
        if !trades_are_parsed {
            trades = parse_trades_simple(&raw_trades_arr, market);
        } else {
            trades = raw_trades_arr.clone();
        }
        if !trades.is_empty() {
            let first = &trades[0];
            if order.get("symbol").map(|v| v.is_null()).unwrap_or(true) {
                order.insert(
                    "symbol".to_string(),
                    first.get("symbol").cloned().unwrap_or(Value::Null),
                );
            }
            if order.get("side").map(|v| v.is_null()).unwrap_or(true) {
                order.insert(
                    "side".to_string(),
                    first.get("side").cloned().unwrap_or(Value::Null),
                );
            }
            if order.get("type").map(|v| v.is_null()).unwrap_or(true) {
                order.insert(
                    "type".to_string(),
                    first.get("type").cloned().unwrap_or(Value::Null),
                );
            }
            if order.get("id").map(|v| v.is_null()).unwrap_or(true) {
                order.insert(
                    "id".to_string(),
                    first.get("order").cloned().unwrap_or(Value::Null),
                );
            }
            if parse_filled {
                filled = Some("0".to_string());
            }
            if parse_cost {
                cost = Some("0".to_string());
            }
            for trade in &trades {
                let trade_amount = safe_string(trade, "amount");
                if parse_filled {
                    if let Some(ta) = &trade_amount {
                        let cur = filled.clone().unwrap_or_else(|| "0".to_string());
                        if let (Some(cd), Some(td)) = (parse_decimal(&cur), parse_decimal(ta)) {
                            filled = Some(dec_to_str(&(cd + td)));
                        }
                    }
                }
                let trade_cost = safe_string(trade, "cost");
                if parse_cost {
                    if let Some(tc) = &trade_cost {
                        let cur = cost.clone().unwrap_or_else(|| "0".to_string());
                        if let (Some(cd), Some(td)) = (parse_decimal(&cur), parse_decimal(tc)) {
                            cost = Some(dec_to_str(&(cd + td)));
                        }
                    }
                }
                if parse_symbol {
                    symbol = safe_string(trade, "symbol");
                }
                if parse_side {
                    side = safe_string(trade, "side");
                }
                let trade_timestamp = trade.get("timestamp").and_then(|v| v.as_i64());
                if parse_last_trade_time_timestamp {
                    if let Some(tt) = trade_timestamp {
                        last_trade_time_timestamp = match last_trade_time_timestamp {
                            None => Some(tt),
                            Some(cur) => Some(cur.max(tt)),
                        };
                    }
                }
                if should_parse_fees {
                    let trade_fees = trade.get("fees").and_then(|v| v.as_array());
                    if let Some(tfs) = trade_fees {
                        for tf in tfs {
                            fees.push(tf.clone());
                        }
                    } else if let Some(tf) = trade.get("fee") {
                        if !tf.is_null() {
                            fees.push(tf.clone());
                        }
                    }
                }
            }
        }
    }

    if should_parse_fees {
        let mut reduced_fees = fees.clone();
        let reduced_length = reduced_fees.len();
        for rf in reduced_fees.iter_mut() {
            if let Value::Object(map) = rf {
                let cost_conv = map.get("cost").and_then(|v| {
                    let s = match v {
                        Value::String(s) => Some(s.clone()),
                        Value::Number(n) => Some(n.to_string()),
                        _ => None,
                    }?;
                    parse_decimal(&s).and_then(|d| d.to_f64())
                });
                map.insert("cost".to_string(), opt_num(cost_conv));
                if map.contains_key("rate") {
                    let rate_conv = map.get("rate").and_then(|v| {
                        let s = match v {
                            Value::String(s) => Some(s.clone()),
                            Value::Number(n) => Some(n.to_string()),
                            _ => None,
                        }?;
                        parse_decimal(&s).and_then(|d| d.to_f64())
                    });
                    map.insert("rate".to_string(), opt_num(rate_conv));
                }
            }
        }
        if !parse_fee && reduced_length == 0 {
            if let Some(Value::Object(fee_map)) = &fee_value {
                let mut fee_copy = fee_map.clone();
                let cost_conv = fee_copy
                    .get("cost")
                    .and_then(|v| match v {
                        Value::String(s) => Some(s.clone()),
                        Value::Number(n) => Some(n.to_string()),
                        _ => None,
                    })
                    .and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64()));
                fee_copy.insert("cost".to_string(), opt_num(cost_conv));
                if fee_copy.contains_key("rate") {
                    let rate_conv = fee_copy
                        .get("rate")
                        .and_then(|v| match v {
                            Value::String(s) => Some(s.clone()),
                            Value::Number(n) => Some(n.to_string()),
                            _ => None,
                        })
                        .and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64()));
                    fee_copy.insert("rate".to_string(), opt_num(rate_conv));
                }
                reduced_fees.push(Value::Object(fee_copy));
            }
        }
        order.insert("fees".to_string(), Value::Array(reduced_fees.clone()));
        if parse_fee && reduced_fees.len() == 1 {
            order.insert("fee".to_string(), reduced_fees[0].clone());
        }
    }

    let mut amount = amount;
    if amount.is_none() {
        if let (Some(f), Some(r)) = (&filled, &remaining) {
            if let (Some(fd), Some(rd)) = (parse_decimal(f), parse_decimal(r)) {
                amount = Some(dec_to_str(&(fd + rd)));
            }
        } else if status.as_deref() == Some("closed") {
            amount = filled.clone();
        }
    }
    if filled.is_none() {
        if let (Some(a), Some(r)) = (&amount, &remaining) {
            if let (Some(ad), Some(rd)) = (parse_decimal(a), parse_decimal(r)) {
                filled = Some(dec_to_str(&(ad - rd)));
            }
        } else if status.as_deref() == Some("closed") {
            filled = amount.clone();
        }
    }
    let mut remaining = remaining;
    if remaining.is_none() {
        if let (Some(a), Some(f)) = (&amount, &filled) {
            if let (Some(ad), Some(fd)) = (parse_decimal(a), parse_decimal(f)) {
                remaining = Some(dec_to_str(&(ad - fd)));
            }
        } else if status.as_deref() == Some("closed") {
            remaining = Some("0".to_string());
        }
    }

    let inverse = market
        .and_then(|m| m.get("inverse"))
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let contract_size = market
        .and_then(|m| m.get("contractSize"))
        .and_then(|v| match v {
            Value::Number(n) => Some(n.to_string()),
            Value::String(s) => Some(s.clone()),
            _ => None,
        })
        .unwrap_or_else(|| "1".to_string());

    if average.is_none() {
        if let (Some(f), Some(c)) = (&filled, &cost) {
            if let (Some(fd), Some(cd), Some(csd)) = (
                parse_decimal(f),
                parse_decimal(c),
                parse_decimal(&contract_size),
            ) {
                if fd > Decimal::ZERO {
                    let filled_times_cs = fd * csd;
                    if inverse {
                        if !cd.is_zero() {
                            average = Some(dec_to_str(&(filled_times_cs / cd)));
                        }
                    } else if !filled_times_cs.is_zero() {
                        average = Some(dec_to_str(&(cd / filled_times_cs)));
                    }
                }
            }
        }
    }

    let cost_price_exists = average.is_some() || price.is_some();
    if parse_cost && filled.is_some() && cost_price_exists {
        let multiply_price = if average.is_none() {
            price.clone()
        } else {
            average.clone()
        };
        if let (Some(f), Some(mp)) = (&filled, &multiply_price) {
            if let (Some(fd), Some(mpd), Some(csd)) = (
                parse_decimal(f),
                parse_decimal(mp),
                parse_decimal(&contract_size),
            ) {
                let filled_times_cs = fd * csd;
                if inverse {
                    if !mpd.is_zero() {
                        cost = Some(dec_to_str(&(filled_times_cs / mpd)));
                    }
                } else {
                    cost = Some(dec_to_str(&(filled_times_cs * mpd)));
                }
            }
        }
    }

    let order_type = dict_safe_string(&order, "type");
    let empty_price = price.is_none() || price.as_deref() == Some("0");
    if empty_price && order_type.as_deref() == Some("market") {
        price = average.clone();
    }

    // Convert trades numeric fields.
    for trade in trades.iter_mut() {
        if let Value::Object(map) = trade {
            let amount_num = map.get("amount").and_then(to_number_from_value);
            map.insert("amount".to_string(), opt_num(amount_num));
            let price_num = map.get("price").and_then(to_number_from_value);
            map.insert("price".to_string(), opt_num(price_num));
            let cost_num = map.get("cost").and_then(to_number_from_value);
            map.insert("cost".to_string(), opt_num(cost_num));
            let mut trade_fee = map
                .get("fee")
                .and_then(|v| v.as_object().cloned())
                .unwrap_or_default();
            let fee_cost_num = trade_fee.get("cost").and_then(to_number_from_value);
            trade_fee.insert("cost".to_string(), opt_num(fee_cost_num));
            if trade_fee.contains_key("rate") {
                let fee_rate_num = trade_fee.get("rate").and_then(to_number_from_value);
                trade_fee.insert("rate".to_string(), opt_num(fee_rate_num));
            }
            let mut entry_fees = map
                .get("fees")
                .and_then(|v| v.as_array().cloned())
                .unwrap_or_default();
            for ef in entry_fees.iter_mut() {
                if let Value::Object(efm) = ef {
                    let n = efm.get("cost").and_then(to_number_from_value);
                    efm.insert("cost".to_string(), opt_num(n));
                }
            }
            map.insert("fees".to_string(), Value::Array(entry_fees));
            map.insert("fee".to_string(), Value::Object(trade_fee));
        }
    }

    let mut time_in_force = dict_safe_string(&order, "timeInForce");
    let mut post_only = order.get("postOnly").and_then(|v| v.as_bool());
    if time_in_force.is_none() {
        if !is_trigger_or_sltp_order && order_type.as_deref() == Some("market") {
            time_in_force = Some("IOC".to_string());
        }
        if let Some(Value::Bool(true)) = order.get("postOnly") {
            time_in_force = Some("PO".to_string());
        }
    } else if post_only.is_none() {
        post_only = Some(time_in_force.as_deref() == Some("PO"));
    }

    let timestamp = dict_safe_integer(&order, "timestamp");
    let last_update_timestamp = dict_safe_integer(&order, "lastUpdateTimestamp");
    let datetime = dict_safe_string(&order, "datetime").or_else(|| match iso8601(timestamp) {
        Value::String(s) => Some(s),
        _ => None,
    });

    let trigger_price = match dict_safe_string(&order, "triggerPrice")
        .or_else(|| dict_safe_string(&order, "stopPrice"))
    {
        Some(s) => parse_decimal(&s).and_then(|d| d.to_f64()),
        None => None,
    };
    let take_profit_price = dict_safe_string(&order, "takeProfitPrice")
        .and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64()));
    let stop_loss_price = dict_safe_string(&order, "stopLossPrice")
        .and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64()));

    let mut out = order.clone();
    out.insert(
        "id".to_string(),
        match dict_safe_string(&order, "id") {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    out.insert(
        "clientOrderId".to_string(),
        match dict_safe_string(&order, "clientOrderId") {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    out.insert(
        "timestamp".to_string(),
        match timestamp {
            Some(v) => Value::Number(Number::from(v)),
            None => Value::Null,
        },
    );
    out.insert(
        "datetime".to_string(),
        match datetime {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    out.insert(
        "symbol".to_string(),
        match &symbol {
            Some(s) => Value::String(s.clone()),
            None => Value::Null,
        },
    );
    out.insert(
        "type".to_string(),
        match dict_safe_string(&order, "type") {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    out.insert(
        "side".to_string(),
        match &side {
            Some(s) => Value::String(s.clone()),
            None => Value::Null,
        },
    );
    out.insert(
        "lastTradeTimestamp".to_string(),
        match last_trade_time_timestamp {
            Some(v) => Value::Number(Number::from(v)),
            None => Value::Null,
        },
    );
    out.insert(
        "lastUpdateTimestamp".to_string(),
        match last_update_timestamp {
            Some(v) => Value::Number(Number::from(v)),
            None => Value::Null,
        },
    );
    out.insert(
        "price".to_string(),
        opt_num(price.and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64()))),
    );
    out.insert(
        "amount".to_string(),
        opt_num(amount.and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64()))),
    );
    out.insert(
        "cost".to_string(),
        opt_num(cost.and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64()))),
    );
    out.insert(
        "average".to_string(),
        opt_num(average.and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64()))),
    );
    out.insert(
        "filled".to_string(),
        opt_num(filled.and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64()))),
    );
    out.insert(
        "remaining".to_string(),
        opt_num(remaining.and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64()))),
    );
    out.insert(
        "timeInForce".to_string(),
        match time_in_force {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    out.insert(
        "postOnly".to_string(),
        match post_only {
            Some(b) => Value::Bool(b),
            None => Value::Null,
        },
    );
    out.insert("trades".to_string(), Value::Array(trades));
    out.insert(
        "reduceOnly".to_string(),
        order.get("reduceOnly").cloned().unwrap_or(Value::Null),
    );
    out.insert("stopPrice".to_string(), opt_num(trigger_price));
    out.insert("triggerPrice".to_string(), opt_num(trigger_price));
    out.insert("takeProfitPrice".to_string(), opt_num(take_profit_price));
    out.insert("stopLossPrice".to_string(), opt_num(stop_loss_price));
    out.insert(
        "status".to_string(),
        match &status {
            Some(s) => Value::String(s.clone()),
            None => Value::Null,
        },
    );
    out.insert(
        "fee".to_string(),
        order.get("fee").cloned().unwrap_or(Value::Null),
    );

    Value::Object(out)
}

fn to_number_from_value(v: &Value) -> Option<f64> {
    match v {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => parse_decimal(s).and_then(|d| d.to_f64()),
        _ => None,
    }
}

fn dict_safe_integer(map: &Map<String, Value>, key: &str) -> Option<i64> {
    match map.get(key)? {
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64)),
        Value::String(s) => s.parse::<i64>().ok(),
        _ => None,
    }
}

fn dict_safe_list(map: &Map<String, Value>, key: &str) -> Option<Vec<Value>> {
    match map.get(key)? {
        Value::Array(a) => Some(a.clone()),
        _ => None,
    }
}

/// Minimal parse_trades: ccxt would build full trades from raw data; empty list here.
fn parse_trades_simple(_raw: &[Value], _market: Option<&Value>) -> Vec<Value> {
    Vec::new()
}

// ---------------------------------------------------------------------------
// parse_position_risk
// ---------------------------------------------------------------------------

pub fn parse_position_risk(raw: &Value, market: Option<&Value>) -> Value {
    let market_id = safe_string(raw, "symbol").unwrap_or_default();
    let symbol = match market {
        Some(m) => m
            .get("symbol")
            .and_then(|v| v.as_str())
            .unwrap_or(&market_id)
            .to_string(),
        None => market_id.clone(),
    };

    let isolated_margin_string = safe_string(raw, "isolatedMargin");
    let notional_string = safe_string_2(raw, "notional", "notionalValue");
    let notional_string_abs = match &notional_string {
        Some(s) => match parse_decimal(s) {
            Some(d) => dec_to_str(&d.abs()),
            None => "0".to_string(),
        },
        None => "0".to_string(),
    };
    let _maintenance_margin_percentage_string: Option<String> = None;

    let notional = parse_decimal(&notional_string_abs).and_then(|d| d.to_f64());
    let contracts_abs = safe_string(raw, "positionAmt")
        .and_then(|s| parse_decimal(&s))
        .map(|d| dec_to_str(&d.abs()));
    let contracts = contracts_abs
        .as_ref()
        .and_then(|s| parse_decimal(s))
        .and_then(|d| d.to_f64());
    let unrealized_pnl_string = safe_string(raw, "unRealizedProfit");
    let unrealized_pnl = unrealized_pnl_string
        .as_ref()
        .and_then(|s| parse_decimal(s))
        .and_then(|d| d.to_f64());
    let liquidation_price_string = omit_zero(safe_string(raw, "liquidationPrice"));
    let liquidation_price = liquidation_price_string
        .as_ref()
        .and_then(|s| parse_decimal(s))
        .and_then(|d| d.to_f64());
    let collateral_string: Option<String> = None;
    let mut margin_mode = safe_string(raw, "marginType");
    if margin_mode.is_none() && isolated_margin_string.is_some() {
        margin_mode = Some(if isolated_margin_string.as_deref() == Some("0") {
            "cross".to_string()
        } else {
            "isolated".to_string()
        });
    }
    let side = match &notional_string {
        Some(s) => match parse_decimal(s) {
            Some(d) => {
                if d > Decimal::ZERO {
                    Some("long".to_string())
                } else if d < Decimal::ZERO {
                    Some("short".to_string())
                } else {
                    None
                }
            }
            None => None,
        },
        None => None,
    };
    let entry_price_string = safe_string(raw, "entryPrice");
    let entry_price = entry_price_string
        .as_ref()
        .and_then(|s| parse_decimal(s))
        .and_then(|d| d.to_f64());
    let contract_size = market.and_then(|m| m.get("contractSize")).cloned();
    let _contract_size_string = number_to_string(contract_size.as_ref());

    let collateral_string_final = {
        // Prefer isolatedMargin across cross/isolated modes (linear API branch).
        collateral_string
            .or_else(|| isolated_margin_string.clone())
            .unwrap_or_else(|| "0".to_string())
    };
    let collateral = parse_decimal(&collateral_string_final).and_then(|d| d.to_f64());
    let mark_price = omit_zero(safe_string(raw, "markPrice"))
        .and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64()));
    let mut timestamp = safe_integer(raw, "updateTime");
    if timestamp == Some(0) {
        timestamp = None;
    }
    let maintenance_margin_percentage = None;
    let maintenance_margin_string = safe_string(raw, "maintMargin");
    let maintenance_margin = maintenance_margin_string
        .as_ref()
        .and_then(|s| parse_decimal(s))
        .and_then(|d| d.to_f64());
    let initial_margin_string = safe_string(raw, "initialMargin");
    let leverage_string = safe_string(raw, "leverage");
    let mut initial_margin_percentage_string: Option<String> = None;
    if leverage_string.is_none() {
        if let (Some(im), Some(n)) = (&initial_margin_string, Some(&notional_string_abs)) {
            if let (Some(imd), Some(nd)) = (parse_decimal(im), parse_decimal(n)) {
                if !nd.is_zero() {
                    let ratio = (imd * Decimal::ONE) / nd;
                    initial_margin_percentage_string = Some(dec_to_str(&round_to(ratio, 8)));
                }
            }
        }
    }
    let margin_ratio: Option<f64>;
    let mut percentage: Option<f64> = None;
    if collateral_string_final != "0" {
        if let (Some(mm), Some(c)) = (&maintenance_margin_string, Some(&collateral_string_final)) {
            if let (Some(mmd), Some(cd)) = (parse_decimal(mm), parse_decimal(c)) {
                if !cd.is_zero() {
                    let inner = (mmd / cd) + Decimal::from_f64(0.00005).unwrap_or(Decimal::ZERO);
                    margin_ratio = (inner / Decimal::ONE).trunc_with_scale(4).to_f64();
                    if let (Some(u), Some(im)) = (
                        unrealized_pnl_string.as_ref(),
                        initial_margin_string.as_ref(),
                    ) {
                        if let (Some(ud), Some(imd)) = (parse_decimal(u), parse_decimal(im)) {
                            if !imd.is_zero() {
                                let r = (ud / imd).trunc_with_scale(4) * Decimal::from(100);
                                percentage = r.to_f64();
                            }
                        }
                    }
                } else {
                    margin_ratio = None;
                }
            } else {
                margin_ratio = None;
            }
        } else {
            margin_ratio = None;
        }
    } else {
        margin_ratio = None;
    }
    // handle percentage when collateral exists but maintenance missing
    if percentage.is_none() && collateral_string_final != "0" {
        if let (Some(u), Some(im)) = (
            unrealized_pnl_string.as_ref(),
            initial_margin_string.as_ref(),
        ) {
            if let (Some(ud), Some(imd)) = (parse_decimal(u), parse_decimal(im)) {
                if !imd.is_zero() {
                    let r = (ud / imd).trunc_with_scale(4) * Decimal::from(100);
                    percentage = r.to_f64();
                }
            }
        }
    }

    let position_side = safe_string(raw, "positionSide");
    let hedged = position_side.as_deref() != Some("BOTH");

    let mut pos = Map::new();
    pos.insert("info".to_string(), raw.clone());
    pos.insert("id".to_string(), Value::Null);
    pos.insert("symbol".to_string(), Value::String(symbol));
    pos.insert("contracts".to_string(), opt_num(contracts));
    pos.insert(
        "contractSize".to_string(),
        contract_size.clone().unwrap_or(Value::Null),
    );
    pos.insert("unrealizedPnl".to_string(), opt_num(unrealized_pnl));
    pos.insert(
        "leverage".to_string(),
        opt_num(leverage_string.and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64()))),
    );
    pos.insert("liquidationPrice".to_string(), opt_num(liquidation_price));
    pos.insert("collateral".to_string(), opt_num(collateral));
    pos.insert("notional".to_string(), opt_num(notional));
    pos.insert("markPrice".to_string(), opt_num(mark_price));
    pos.insert("entryPrice".to_string(), opt_num(entry_price));
    pos.insert(
        "timestamp".to_string(),
        match timestamp {
            Some(v) => Value::Number(Number::from(v)),
            None => Value::Null,
        },
    );
    pos.insert(
        "initialMargin".to_string(),
        opt_num(initial_margin_string.and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64()))),
    );
    pos.insert(
        "initialMarginPercentage".to_string(),
        opt_num(
            initial_margin_percentage_string
                .and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64())),
        ),
    );
    pos.insert("maintenanceMargin".to_string(), opt_num(maintenance_margin));
    pos.insert(
        "maintenanceMarginPercentage".to_string(),
        opt_num(maintenance_margin_percentage),
    );
    pos.insert("marginRatio".to_string(), opt_num(margin_ratio));
    pos.insert("datetime".to_string(), iso8601(timestamp));
    pos.insert(
        "marginMode".to_string(),
        match margin_mode.clone() {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    pos.insert(
        "marginType".to_string(),
        match margin_mode {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    pos.insert(
        "side".to_string(),
        match side {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    pos.insert("hedged".to_string(), Value::Bool(hedged));
    pos.insert("percentage".to_string(), opt_num(percentage));
    pos.insert("stopLossPrice".to_string(), Value::Null);
    pos.insert("takeProfitPrice".to_string(), Value::Null);

    let mut value = Value::Object(pos);
    safe_position(&mut value);
    value
}

fn round_to(d: Decimal, places: u32) -> Decimal {
    // ccxt Precise.string_div truncates toward zero.
    d.trunc_with_scale(places)
}

fn safe_position(position: &mut Value) {
    if let Value::Object(map) = position {
        let percentage = map.get("percentage").cloned().unwrap_or(Value::Null);
        if percentage.is_null() {
            let unrealised = map.get("unrealisedPnl").and_then(|v| match v {
                Value::String(s) => Some(s.clone()),
                _ => None,
            });
            let initial_margin = map.get("initialMargin").and_then(|v| match v {
                Value::String(s) => Some(s.clone()),
                _ => None,
            });
            if let (Some(u), Some(im)) = (unrealised, initial_margin) {
                if let (Some(ud), Some(imd)) = (parse_decimal(&u), parse_decimal(&im)) {
                    if !imd.is_zero() {
                        let p = (ud / imd).trunc_with_scale(4) * Decimal::from(100);
                        if let Some(f) = p.to_f64() {
                            map.insert("percentage".to_string(), num(f));
                        }
                    }
                }
            }
        }
        let contract_size = map.get("contractSize").and_then(|v| v.as_f64());
        if contract_size.is_none() {
            // market lookup by symbol would fill it; nothing to do here without markets.
        }
    }
}

// ---------------------------------------------------------------------------
// parse_balance_linear
// ---------------------------------------------------------------------------

pub fn parse_balance_linear(account: &Value) -> Value {
    // parse_balance_custom(response, "linear"): uses "assets" list.
    let assets = account
        .get("assets")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();

    let mut balance = Map::new();
    for asset in &assets {
        let (code, filled) = parse_balance_entry(asset);
        if let Some(code) = code {
            balance.insert(code, filled);
        }
    }

    let mut out = Map::new();
    out.insert("info".to_string(), account.clone());
    out.insert("timestamp".to_string(), Value::Null);
    out.insert("datetime".to_string(), Value::Null);
    for (k, v) in &balance {
        out.insert(k.clone(), v.clone());
    }
    safe_balance(out)
}

fn parse_balance_entry(asset: &Value) -> (Option<String>, Value) {
    let code = safe_string(asset, "asset");
    let mut entry = Map::new();
    entry.insert(
        "free".to_string(),
        match safe_string(asset, "availableBalance") {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    entry.insert(
        "total".to_string(),
        // ccxt: safe_string_2(balance, 'marginBalance', 'balance')
        match safe_string(asset, "marginBalance").or_else(|| safe_string(asset, "balance")) {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    entry.insert(
        "used".to_string(),
        match safe_string(asset, "initialMargin") {
            Some(s) => Value::String(s),
            None => Value::Null,
        },
    );
    (code, Value::Object(entry))
}

fn safe_balance(mut balance: Map<String, Value>) -> Value {
    let reserved = ["info", "timestamp", "datetime", "free", "used", "total"];
    let codes: Vec<String> = balance
        .keys()
        .filter(|k| !reserved.contains(&k.as_str()))
        .cloned()
        .collect();

    balance.insert("free".to_string(), Value::Object(Map::new()));
    balance.insert("used".to_string(), Value::Object(Map::new()));
    balance.insert("total".to_string(), Value::Object(Map::new()));

    let mut debt_balance = Map::new();
    let mut free_map = Map::new();
    let mut used_map = Map::new();
    let mut total_map = Map::new();

    for code in &codes {
        let entry = balance.get(code).cloned().unwrap_or(Value::Null);
        let mut total = safe_string(&entry, "total");
        let mut free = safe_string(&entry, "free");
        let mut used = safe_string(&entry, "used");
        let debt = safe_string(&entry, "debt");

        if total.is_none() && free.is_some() && used.is_some() {
            if let (Some(f), Some(u)) = (&free, &used) {
                if let (Some(fd), Some(ud)) = (parse_decimal(f), parse_decimal(u)) {
                    total = Some(dec_to_str(&(fd + ud)));
                }
            }
        }
        if free.is_none() && total.is_some() && used.is_some() {
            if let (Some(t), Some(u)) = (&total, &used) {
                if let (Some(td), Some(ud)) = (parse_decimal(t), parse_decimal(u)) {
                    free = Some(dec_to_str(&(td - ud)));
                }
            }
        }
        if used.is_none() && total.is_some() && free.is_some() {
            if let (Some(t), Some(f)) = (&total, &free) {
                if let (Some(td), Some(fd)) = (parse_decimal(t), parse_decimal(f)) {
                    used = Some(dec_to_str(&(td - fd)));
                }
            }
        }

        let free_num = free.and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64()));
        let used_num = used.and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64()));
        let total_num = total.and_then(|s| parse_decimal(&s).and_then(|d| d.to_f64()));

        if let Some(Value::Object(map)) = balance.get_mut(code) {
            map.insert("free".to_string(), opt_num(free_num));
            map.insert("used".to_string(), opt_num(used_num));
            map.insert("total".to_string(), opt_num(total_num));
            if let Some(d) = &debt {
                if let Some(df) = parse_decimal(d).and_then(|x| x.to_f64()) {
                    map.insert("debt".to_string(), num(df));
                    debt_balance.insert(code.clone(), num(df));
                }
            }
        }

        free_map.insert(code.clone(), opt_num(free_num));
        used_map.insert(code.clone(), opt_num(used_num));
        total_map.insert(code.clone(), opt_num(total_num));
    }

    balance.insert("free".to_string(), Value::Object(free_map));
    balance.insert("used".to_string(), Value::Object(used_map));
    balance.insert("total".to_string(), Value::Object(total_map));

    if !debt_balance.is_empty() {
        balance.insert("debt".to_string(), Value::Object(debt_balance));
    }

    Value::Object(balance)
}

// parse_balance_linear currently has isolated note about PRECISE_DIV_PRECISION; reference it.
#[allow(dead_code)]
fn _precision_guard() -> u32 {
    PRECISE_DIV_PRECISION
}

#[allow(dead_code)]
fn _float_guard(d: Decimal) -> Option<Decimal> {
    Decimal::from_f64(d.to_f64().unwrap_or(0.0))
}
