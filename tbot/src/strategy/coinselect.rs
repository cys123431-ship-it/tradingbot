//! Pure helpers from `utbreakout/coinselector.py`.

use serde_json::Value;

pub fn normalize_symbol(symbol: &str) -> String {
    let mut text = symbol.trim().to_uppercase();
    if text.is_empty() {
        return String::new();
    }
    text = text.replace(":USDT", "");
    if text.contains('/') {
        let mut parts = text.splitn(2, '/');
        let base = parts.next().unwrap_or("");
        let quote = parts.next().unwrap_or("");
        let quote_clean = quote.split(':').next().unwrap_or("");
        return format!("{}/{}", base, quote_clean);
    }
    if text.ends_with("USDT") {
        let base = &text[..text.len() - 4];
        return format!("{}/USDT", base);
    }
    text
}

pub fn symbol_base(symbol: &str) -> String {
    let normalized = normalize_symbol(symbol);
    if normalized.contains('/') {
        normalized.split('/').next().unwrap_or("").to_string()
    } else {
        normalized
    }
}

fn info_value(container: Option<&Value>, key: &str, default: Option<&Value>) -> Option<Value> {
    let Some(map) = container.and_then(|v| v.as_object()) else {
        return default.cloned();
    };
    if let Some(v) = map.get(key) {
        return Some(v.clone());
    }
    if let Some(info) = map.get("info").and_then(|v| v.as_object()) {
        if let Some(v) = info.get(key) {
            return Some(v.clone());
        }
    }
    default.cloned()
}

fn info_str(map: &serde_json::Map<String, Value>, key: &str, default: &str) -> String {
    info_value(Some(&Value::Object(map.clone())), key, None)
        .as_ref()
        .map(|v| match v {
            Value::Null => default.to_string(),
            Value::String(s) => s.clone(),
            Value::Number(n) => n.to_string(),
            Value::Bool(b) => {
                if *b {
                    "True".to_string()
                } else {
                    "False".to_string()
                }
            }
            other => other.to_string(),
        })
        .unwrap_or_else(|| default.to_string())
}

pub fn market_is_usdt_perpetual(symbol: &str, market: Option<&Value>) -> bool {
    let normalized = normalize_symbol(symbol);
    if !normalized.contains("/USDT") {
        return false;
    }
    let Some(market) = market else {
        return true;
    };
    let Some(map) = market.as_object() else {
        return true;
    };

    let quote_raw = info_str(map, "quote", "");
    let quote_raw = if quote_raw.is_empty() {
        info_str(map, "quoteAsset", "")
    } else {
        quote_raw
    };
    let quote = quote_raw.to_uppercase();

    let settle_raw = info_str(map, "settle", "");
    let settle_raw = if settle_raw.is_empty() {
        info_str(map, "marginAsset", "USDT")
    } else {
        settle_raw
    };
    let settle = settle_raw.to_uppercase();

    let contract_type = info_str(map, "contractType", "PERPETUAL").to_uppercase();
    let market_type = match map.get("type") {
        Some(Value::String(s)) => s.to_lowercase(),
        Some(Value::Null) | None => String::new(),
        Some(v) => match v {
            Value::Number(n) => n.to_string().to_lowercase(),
            _ => String::new(),
        },
    };
    let active = map.get("active").cloned().unwrap_or(Value::Bool(true));
    let status = info_str(map, "status", "TRADING").to_uppercase();
    let swap_bool = map.get("swap").and_then(|v| v.as_bool()).unwrap_or(false);
    let swap_like = swap_bool || market_type == "swap" || market_type == "future";

    let active_ok = !matches!(active, Value::Bool(false));
    let contract_ok = contract_type == "PERPETUAL" || contract_type == "TRADIFI_PERPETUAL";

    quote == "USDT"
        && settle == "USDT"
        && contract_ok
        && status == "TRADING"
        && active_ok
        && swap_like
}

pub fn market_is_tradifi_perpetual(symbol: &str, market: Option<&Value>) -> bool {
    if !market_is_usdt_perpetual(symbol, market) {
        return false;
    }
    let Some(market) = market else {
        return false;
    };
    let Some(map) = market.as_object() else {
        return false;
    };
    let contract_type = info_str(map, "contractType", "").to_uppercase();
    contract_type == "TRADIFI_PERPETUAL"
}

pub fn market_tradifi_underlying_type(market: Option<&Value>) -> String {
    let Some(market) = market else {
        return String::new();
    };
    let Some(map) = market.as_object() else {
        return String::new();
    };
    let underlying = info_value(Some(&Value::Object(map.clone())), "underlyingType", None);
    let text = underlying
        .map(|v| match v {
            Value::Null => String::new(),
            Value::String(s) => s,
            Value::Number(n) => n.to_string(),
            Value::Bool(b) => {
                if b {
                    "True".to_string()
                } else {
                    "False".to_string()
                }
            }
            _ => String::new(),
        })
        .unwrap_or_default();
    let trimmed = text.trim();
    let value = if trimmed.is_empty() { "" } else { trimmed };
    value.to_uppercase()
}
