//! Shared TradFi scope helpers (US equities + gold; classification only).

use std::collections::HashSet;

use serde_json::Value;

use crate::strategy::coinselect::market_is_tradifi_perpetual;

pub const TRADFI_SCOPE: &str = "tradfi";
pub const MAINNET_NETWORK: &str = "mainnet";
pub const TRADFI_MAINNET_ONLY_REASON: &str = "TRADFI_MAINNET_ONLY";
pub const TRADFI_SESSION_CLOSED_REASON: &str = "TRADFI_US_SESSION_CLOSED";

pub const TRADFI_GOLD_BASES: &[&str] = &[
    "XAU",
    "GOLD",
    "GC",
    "GLD",
    "IAU",
    "XAUT",
    "PAXG",
    "GOLDTRACKER",
];
pub const TRADFI_GOLD_MARKERS: &[&str] = &["GOLD", "GOLDSPOT", "GOLDTRACKER", "PRECIOUSMETALGOLD"];
pub const TRADFI_NON_GOLD_MARKERS: &[&str] = &[
    "SILVER",
    "XAG",
    "PLATINUM",
    "XPT",
    "PALLADIUM",
    "XPD",
    "COPPER",
    "HG",
    "OIL",
    "WTI",
    "BRENT",
    "CRUDE",
    "NATGAS",
    "NATURALGAS",
    "ENERGY",
    "ALUMINUM",
    "ALUMINIUM",
    "NICKEL",
    "ZINC",
    "LEAD",
    "TIN",
    "IRON",
    "LITHIUM",
    "COBALT",
    "URANIUM",
    "SILVERSPOT",
];
pub const TRADFI_EQUITY_MARKERS: &[&str] = &[
    "STOCK", "STOCKS", "EQUITY", "EQUITIES", "INDEX", "INDICES", "ETF", "SHARE", "SHARES",
];

fn as_object(value: &Value) -> Option<&serde_json::Map<String, Value>> {
    value.as_object()
}

fn market_info(market: &Value) -> Option<&serde_json::Map<String, Value>> {
    let map = as_object(market)?;
    map.get("info").and_then(as_object)
}

fn market_get_str(market: &Value, key: &str) -> String {
    let Some(map) = as_object(market) else {
        return String::new();
    };
    match map.get(key) {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        Some(Value::Bool(b)) => {
            if *b {
                "True".to_string()
            } else {
                "False".to_string()
            }
        }
        _ => String::new(),
    }
}

fn info_get_str(info: &serde_json::Map<String, Value>, key: &str) -> String {
    match info.get(key) {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        Some(Value::Bool(b)) => {
            if *b {
                "True".to_string()
            } else {
                "False".to_string()
            }
        }
        _ => String::new(),
    }
}

fn alnum_upper(text: &str) -> String {
    text.chars()
        .filter(|c| c.is_alphanumeric())
        .map(|c| c.to_ascii_uppercase())
        .collect()
}

fn market_base_tokens(symbol: &str, market: &Value) -> HashSet<String> {
    let fallback = Value::Object(serde_json::Map::new());
    let market_ref = if as_object(market).is_some() {
        market
    } else {
        &fallback
    };
    let info_map = market_info(market_ref);
    let mut raw: HashSet<String> = HashSet::new();
    let base = market_get_str(market_ref, "base");
    if !base.is_empty() {
        raw.insert(base);
    }
    if let Some(info) = info_map {
        let ba = info_get_str(info, "baseAsset");
        if !ba.is_empty() {
            raw.insert(ba);
        }
        let un = info_get_str(info, "underlying");
        if !un.is_empty() {
            raw.insert(un);
        }
    }
    let symbol_part = symbol.split(':').next().unwrap_or("");
    let symbol_base_part = symbol_part.split('/').next().unwrap_or("");
    if !symbol_base_part.is_empty() {
        raw.insert(symbol_base_part.to_string());
    }
    raw.iter()
        .filter(|v| !v.trim().is_empty())
        .map(|v| alnum_upper(v))
        .collect()
}

fn set_intersects(values: &HashSet<String>, markers: &[&str]) -> bool {
    markers.iter().any(|m| values.contains(*m))
}

pub fn market_is_tradfi_gold(symbol: &str, market: Option<&Value>) -> bool {
    if !market_is_tradifi_perpetual(symbol, market) {
        return false;
    }
    let fallback = Value::Object(serde_json::Map::new());
    let market_ref = match market {
        Some(m) if as_object(m).is_some() => m,
        _ => &fallback,
    };
    let info_map = market_info(market_ref);
    let base_tokens = market_base_tokens(symbol, market_ref);
    let gold_bases = set_intersects(&base_tokens, TRADFI_GOLD_BASES);
    if set_intersects(&base_tokens, TRADFI_NON_GOLD_MARKERS) {
        return false;
    }
    if gold_bases {
        return true;
    }
    let mut metadata: HashSet<String> = HashSet::new();
    let keys = [
        "underlyingType",
        "underlyingSubType",
        "assetType",
        "category",
        "sector",
        "subType",
        "productType",
        "name",
        "displayName",
    ];
    if let Some(map) = as_object(market_ref) {
        for key in keys {
            let token = alnum_upper(&market_get_str(&Value::Object(map.clone()), key));
            if token.is_empty() {
                continue;
            }
            metadata.insert(token);
            if TRADFI_NON_GOLD_MARKERS
                .iter()
                .any(|m| metadata.iter().any(|t| t.contains(m)))
            {
                return false;
            }
        }
    }
    if let Some(info) = info_map {
        for key in keys {
            let token = alnum_upper(&info_get_str(info, key));
            if token.is_empty() {
                continue;
            }
            metadata.insert(token.clone());
            if TRADFI_NON_GOLD_MARKERS.iter().any(|m| token.contains(m)) {
                return false;
            }
        }
    }
    if metadata
        .iter()
        .any(|t| TRADFI_GOLD_MARKERS.contains(&t.as_str()))
        || metadata.iter().any(|t| t.contains("GOLD"))
    {
        return true;
    }
    gold_bases
}

pub fn market_is_tradfi_us_equity(symbol: &str, market: Option<&Value>) -> bool {
    if !market_is_tradifi_perpetual(symbol, market) {
        return false;
    }
    if market_is_tradfi_gold(symbol, market) {
        return false;
    }
    let fallback = Value::Object(serde_json::Map::new());
    let market_ref = match market {
        Some(m) if as_object(m).is_some() => m,
        _ => &fallback,
    };
    let info_map = market_info(market_ref);
    let mut metadata: HashSet<String> = HashSet::new();
    let keys = [
        "underlyingType",
        "underlyingSubType",
        "assetType",
        "category",
        "sector",
        "subType",
        "productType",
    ];
    if let Some(map) = as_object(market_ref) {
        for key in keys {
            let token = alnum_upper(&market_get_str(&Value::Object(map.clone()), key));
            if !token.is_empty() {
                metadata.insert(token);
            }
        }
    }
    if let Some(info) = info_map {
        for key in keys {
            let token = alnum_upper(&info_get_str(info, key));
            if !token.is_empty() {
                metadata.insert(token);
            }
        }
    }
    if set_intersects(&metadata, TRADFI_EQUITY_MARKERS) {
        return true;
    }
    let base_tokens = market_base_tokens(symbol, market_ref);
    !set_intersects(&base_tokens, TRADFI_NON_GOLD_MARKERS)
}

pub fn market_in_tradfi_scope(symbol: &str, market: Option<&Value>) -> bool {
    market_is_tradfi_us_equity(symbol, market) || market_is_tradfi_gold(symbol, market)
}

// Duplicated constants from btc_ma_cross/service.py — values match tradfi.py,
// but kept separately under the BTC_MA_ prefix to mirror the original layout.
pub const BTC_MA_TRADFI_GOLD_BASES: &[&str] = &[
    "XAU",
    "GOLD",
    "GC",
    "GLD",
    "IAU",
    "XAUT",
    "PAXG",
    "GOLDTRACKER",
];
pub const BTC_MA_TRADFI_GOLD_MARKERS: &[&str] =
    &["GOLD", "GOLDSPOT", "GOLDTRACKER", "PRECIOUSMETALGOLD"];
pub const BTC_MA_TRADFI_NON_GOLD_MARKERS: &[&str] = &[
    "SILVER",
    "XAG",
    "PLATINUM",
    "XPT",
    "PALLADIUM",
    "XPD",
    "COPPER",
    "HG",
    "OIL",
    "WTI",
    "BRENT",
    "CRUDE",
    "NATGAS",
    "NATURALGAS",
    "ENERGY",
    "ALUMINUM",
    "ALUMINIUM",
    "NICKEL",
    "ZINC",
    "LEAD",
    "TIN",
    "IRON",
    "LITHIUM",
    "COBALT",
    "URANIUM",
    "SILVERSPOT",
];
pub const BTC_MA_TRADFI_EQUITY_MARKERS: &[&str] = &[
    "STOCK", "STOCKS", "EQUITY", "EQUITIES", "INDEX", "INDICES", "ETF", "SHARE", "SHARES",
];
pub const BTC_MA_MAINNET_NETWORK: &str = "mainnet";
pub const BTC_MA_TRADFI_MAINNET_ONLY_REASON: &str = "TRADFI_MAINNET_ONLY";
pub const BTC_MA_TRADFI_SESSION_CLOSED_REASON: &str = "TRADFI_US_SESSION_CLOSED";
pub const BTC_MA_PROFIT_LOCK_EXIT_REASON: &str = "PROFIT_LOCK";
