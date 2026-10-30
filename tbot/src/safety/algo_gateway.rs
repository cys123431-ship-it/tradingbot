//! Port of `trading_safety/binance_algo_gateway.py`.

use std::sync::Arc;

use serde_json::{Map, Value};

use crate::exchange::{ExchangeError, FuturesExchange};

pub const CONDITIONAL_TYPES: &[&str] = &[
    "STOP",
    "STOP_MARKET",
    "TAKE_PROFIT",
    "TAKE_PROFIT_MARKET",
    "TRAILING_STOP_MARKET",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AlgoLookupStatus {
    Found,
    NotFound,
    Unknown,
}

impl AlgoLookupStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            AlgoLookupStatus::Found => "FOUND",
            AlgoLookupStatus::NotFound => "NOT_FOUND",
            AlgoLookupStatus::Unknown => "UNKNOWN",
        }
    }
}

#[derive(Debug, Clone)]
pub struct AlgoLookupResult {
    pub status: AlgoLookupStatus,
    pub order: Option<Value>,
    pub error: Option<String>,
}

impl AlgoLookupResult {
    pub fn new(status: AlgoLookupStatus, order: Option<Value>, error: Option<String>) -> Self {
        Self {
            status,
            order,
            error,
        }
    }
}

#[derive(Debug, Clone)]
pub struct AlgoOrdersSnapshot {
    pub ok: bool,
    pub orders: Vec<Value>,
    pub error: Option<String>,
}

impl AlgoOrdersSnapshot {
    pub fn new(ok: bool, orders: Vec<Value>, error: Option<String>) -> Self {
        Self { ok, orders, error }
    }
}

#[derive(Debug, Clone)]
pub struct ProtectionOrderSnapshot {
    pub regular_orders_ok: bool,
    pub algo_orders_ok: bool,
    pub orders: Vec<Value>,
    pub errors: Vec<String>,
}

impl ProtectionOrderSnapshot {
    pub fn complete(&self) -> bool {
        self.regular_orders_ok && self.algo_orders_ok
    }
}

fn bool_text(value: &Value) -> String {
    if truthy(value) {
        "true".to_string()
    } else {
        "false".to_string()
    }
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i != 0
            } else if let Some(u) = n.as_u64() {
                u != 0
            } else if let Some(f) = n.as_f64() {
                f != 0.0
            } else {
                false
            }
        }
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Return Binance's compact market id for CCXT or exchange symbols.
pub fn normalize_futures_market_id(value: Option<&Value>) -> String {
    let text_raw = match value {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(other) => other.to_string(),
    };
    let mut text = text_raw.to_uppercase();
    text = text.trim().to_string();
    if let Some(idx) = text.find(':') {
        text = text[..idx].to_string();
    }
    text.chars().filter(|c| c.is_alphanumeric()).collect()
}

fn market_key_matches(key: &str, symbol: &str) -> bool {
    if key == symbol {
        return true;
    }
    let normalized_key = normalize_futures_market_id(Some(&Value::String(key.to_string())));
    let normalized_symbol = normalize_futures_market_id(Some(&Value::String(symbol.to_string())));
    !normalized_symbol.is_empty() && normalized_key == normalized_symbol
}

fn lookup_market_id<E: FuturesExchange>(exchange: &E, symbol: &str) -> Option<String> {
    let markets = exchange.markets()?;
    for (key, value) in markets.iter() {
        if market_key_matches(key, symbol) {
            if let Value::Object(obj) = value {
                if let Some(id) = obj.get("id") {
                    let s = match id {
                        Value::String(s) => s.clone(),
                        Value::Null => String::new(),
                        other => other.to_string(),
                    };
                    if truthy(id) {
                        return Some(s);
                    }
                }
            }
        }
    }
    None
}

pub struct BinanceAlgoOrderGateway<E: FuturesExchange> {
    exchange: Arc<E>,
}

impl<E: FuturesExchange> BinanceAlgoOrderGateway<E> {
    pub fn new(exchange: Arc<E>) -> Self {
        Self { exchange }
    }

    pub fn market_id(&self, symbol: &str) -> String {
        if let Some(id) = lookup_market_id(self.exchange.as_ref(), symbol) {
            return id;
        }
        normalize_futures_market_id(Some(&Value::String(symbol.to_string())))
    }

    pub fn normalize(raw: &Map<String, Value>) -> Map<String, Value> {
        let info = raw.clone();
        let order_type_value = info
            .get("orderType")
            .filter(|v| truthy(v))
            .or_else(|| info.get("type").filter(|v| truthy(v)))
            .cloned();
        let order_type_str = order_type_value
            .as_ref()
            .map(value_to_string_or_empty)
            .unwrap_or_default();
        let trigger = info
            .get("triggerPrice")
            .filter(|v| truthy(v))
            .or_else(|| info.get("stopPrice"))
            .cloned();

        // Python: str(info.get("algoId") or "") or None  (0 / "" / null -> None)
        let id_string = info
            .get("algoId")
            .filter(|v| truthy(v))
            .map(value_to_string_or_empty)
            .filter(|s| !s.is_empty());

        let mut out: Map<String, Value> = Map::new();
        out.insert(
            "id".to_string(),
            match id_string {
                Some(s) => Value::String(s),
                None => Value::Null,
            },
        );
        out.insert(
            "algoId".to_string(),
            info.get("algoId").cloned().unwrap_or(Value::Null),
        );

        let client_algo_id = info.get("clientAlgoId").cloned().unwrap_or(Value::Null);
        out.insert("clientOrderId".to_string(), client_algo_id.clone());
        out.insert("clientAlgoId".to_string(), client_algo_id);

        out.insert(
            "symbol".to_string(),
            info.get("symbol").cloned().unwrap_or(Value::Null),
        );
        out.insert(
            "type".to_string(),
            Value::String(order_type_str.to_lowercase()),
        );
        out.insert(
            "orderType".to_string(),
            order_type_value.unwrap_or(Value::String(String::new())),
        );
        out.insert(
            "side".to_string(),
            Value::String(
                info.get("side")
                    .map(value_to_string_or_empty)
                    .unwrap_or_default()
                    .to_lowercase(),
            ),
        );

        let amount = info
            .get("quantity")
            .filter(|v| truthy(v))
            .or_else(|| info.get("origQty"))
            .cloned()
            .unwrap_or(Value::Number(0.into()));
        let amount_for_both = if truthy(&amount) {
            Some(amount.clone())
        } else {
            match info.get("quantity").or_else(|| info.get("origQty")) {
                Some(v) => Some(v.clone()),
                None => Some(Value::Number(0.into())),
            }
        };
        let _ = amount;
        out.insert(
            "amount".to_string(),
            amount_for_both.clone().unwrap_or(Value::Null),
        );
        out.insert(
            "quantity".to_string(),
            amount_for_both.unwrap_or(Value::Null),
        );

        out.insert(
            "stopPrice".to_string(),
            trigger.clone().unwrap_or(Value::Null),
        );
        out.insert("triggerPrice".to_string(), trigger.unwrap_or(Value::Null));

        out.insert(
            "reduceOnly".to_string(),
            Value::Bool(bool_field(info.get("reduceOnly"), "false")),
        );
        out.insert(
            "closePosition".to_string(),
            Value::Bool(bool_field(info.get("closePosition"), "false")),
        );
        out.insert(
            "positionSide".to_string(),
            info.get("positionSide").cloned().unwrap_or(Value::Null),
        );
        out.insert(
            "workingType".to_string(),
            info.get("workingType").cloned().unwrap_or(Value::Null),
        );
        out.insert(
            "priceProtect".to_string(),
            info.get("priceProtect").cloned().unwrap_or(Value::Null),
        );

        let status = info
            .get("algoStatus")
            .filter(|v| truthy(v))
            .or_else(|| info.get("status"))
            .cloned()
            .unwrap_or(Value::Null);
        out.insert("status".to_string(), status);

        let timestamp = info
            .get("updateTime")
            .filter(|v| truthy(v))
            .or_else(|| info.get("createTime"))
            .cloned()
            .unwrap_or(Value::Null);
        out.insert("timestamp".to_string(), timestamp);

        out.insert("info".to_string(), Value::Object(info));
        out.insert(
            "_protection_source".to_string(),
            Value::String("binance_algo".to_string()),
        );
        out
    }

    #[allow(clippy::too_many_arguments)]
    pub async fn create_conditional_order(
        &self,
        symbol: &str,
        order_type: &str,
        side: &str,
        quantity: Option<Value>,
        trigger_price: Value,
        client_algo_id: &str,
        reduce_only: bool,
        working_type: &str,
        price_protect: bool,
        position_side: Option<&str>,
        close_position: bool,
        price: Option<Value>,
    ) -> Result<Map<String, Value>, ExchangeError> {
        let order_type_value = order_type.trim().to_uppercase();
        if !CONDITIONAL_TYPES.contains(&order_type_value.as_str()) {
            return Err(ExchangeError::new(
                "ValueError",
                format!("unsupported Binance conditional order type: {order_type}"),
            ));
        }
        if close_position
            && order_type_value != "STOP_MARKET"
            && order_type_value != "TAKE_PROFIT_MARKET"
        {
            return Err(ExchangeError::new(
                "ValueError",
                "closePosition=true is only valid for STOP_MARKET or TAKE_PROFIT_MARKET"
                    .to_string(),
            ));
        }
        if close_position && price.is_some() {
            return Err(ExchangeError::new(
                "ValueError",
                "closePosition=true market triggers must not include price".to_string(),
            ));
        }
        if !close_position && quantity.is_none() {
            return Err(ExchangeError::new(
                "ValueError",
                "quantity is required when closePosition is false".to_string(),
            ));
        }

        let mut params: Map<String, Value> = Map::new();
        params.insert(
            "algoType".to_string(),
            Value::String("CONDITIONAL".to_string()),
        );
        params.insert("symbol".to_string(), Value::String(self.market_id(symbol)));
        params.insert("side".to_string(), Value::String(side.to_uppercase()));
        params.insert("type".to_string(), Value::String(order_type_value));
        params.insert("triggerPrice".to_string(), trigger_price);
        let truncated: String = client_algo_id.chars().take(36).collect();
        params.insert("clientAlgoId".to_string(), Value::String(truncated));
        params.insert(
            "workingType".to_string(),
            Value::String(working_type.to_uppercase()),
        );
        params.insert(
            "priceProtect".to_string(),
            Value::String(if price_protect { "true" } else { "false" }.to_string()),
        );
        if close_position {
            params.insert(
                "closePosition".to_string(),
                Value::String("true".to_string()),
            );
        } else {
            if let Some(q) = quantity {
                params.insert("quantity".to_string(), q);
            }
            params.insert(
                "reduceOnly".to_string(),
                Value::String(if reduce_only { "true" } else { "false" }.to_string()),
            );
        }
        if let Some(ps) = position_side {
            if !ps.is_empty() {
                params.insert("positionSide".to_string(), Value::String(ps.to_uppercase()));
            }
        }
        if let Some(p) = price {
            params.insert("price".to_string(), p);
        }

        let raw = self
            .exchange
            .algo_post_raw(params)
            .await
            .map_err(map_not_supported_post)?;
        match raw {
            Value::Object(map) => Ok(Self::normalize(&map)),
            _ => Err(ExchangeError::new(
                "RuntimeError",
                "Binance Algo Order returned an invalid response".to_string(),
            )),
        }
    }

    pub async fn fetch_by_client_id(&self, client_algo_id: &str) -> AlgoLookupResult {
        let truncated: String = client_algo_id.chars().take(36).collect();
        let raw = match self.exchange.algo_get_raw(&truncated).await {
            Ok(v) => v,
            Err(err) => {
                if err.class == "NotSupported" {
                    return AlgoLookupResult::new(
                        AlgoLookupStatus::Unknown,
                        None,
                        Some("Binance Algo lookup endpoint unavailable".to_string()),
                    );
                }
                let text = err.message.to_lowercase();
                if ["unknown order", "does not exist", "-2013", "-4139"]
                    .iter()
                    .any(|token| text.contains(token))
                {
                    return AlgoLookupResult::new(AlgoLookupStatus::NotFound, None, None);
                }
                return AlgoLookupResult::new(
                    AlgoLookupStatus::Unknown,
                    None,
                    Some(format!("{}: {}", err.class, err.message)),
                );
            }
        };
        let map = match raw {
            Value::Object(m) => m,
            _ => {
                return AlgoLookupResult::new(
                    AlgoLookupStatus::Unknown,
                    None,
                    Some("Binance Algo lookup returned invalid response".to_string()),
                );
            }
        };
        let algo_id_value = map.get("algoId").cloned().unwrap_or(Value::Null);
        if matches!(algo_id_value, Value::Null) || map.get("algoId").is_none() {
            let code = map
                .get("code")
                .map(value_to_string_or_empty)
                .unwrap_or_default();
            let message = map
                .get("msg")
                .map(value_to_string_or_empty)
                .unwrap_or_default()
                .to_lowercase();
            if code == "-2013" || code == "-4139" || message.contains("does not exist") {
                return AlgoLookupResult::new(AlgoLookupStatus::NotFound, None, None);
            }
            return AlgoLookupResult::new(
                AlgoLookupStatus::Unknown,
                None,
                Some("Binance Algo lookup response had no algoId".to_string()),
            );
        }
        let order = Self::normalize(&map);
        AlgoLookupResult::new(AlgoLookupStatus::Found, Some(Value::Object(order)), None)
    }

    pub async fn fetch_open_orders(&self) -> AlgoOrdersSnapshot {
        let raw = match self.exchange.algo_open_raw().await {
            Ok(v) => v,
            Err(err) => {
                if err.class == "NotSupported" {
                    return AlgoOrdersSnapshot::new(
                        false,
                        Vec::new(),
                        Some("Binance open Algo endpoint unavailable".to_string()),
                    );
                }
                return AlgoOrdersSnapshot::new(
                    false,
                    Vec::new(),
                    Some(format!("{}: {}", err.class, err.message)),
                );
            }
        };
        let rows: Vec<Value> = match raw {
            Value::Object(map) => {
                let mut found: Option<Value> = None;
                for key in ["orders", "algoOrders", "rows", "data"] {
                    if let Some(v) = map.get(key) {
                        found = Some(v.clone());
                        break;
                    }
                }
                match found {
                    None => {
                        return AlgoOrdersSnapshot::new(
                            false,
                            Vec::new(),
                            Some("Binance open Algo response was incomplete".to_string()),
                        );
                    }
                    Some(Value::Array(arr)) => arr,
                    Some(_) => {
                        return AlgoOrdersSnapshot::new(
                            false,
                            Vec::new(),
                            Some("Binance open Algo rows were invalid".to_string()),
                        );
                    }
                }
            }
            Value::Array(arr) => arr,
            _ => {
                return AlgoOrdersSnapshot::new(
                    false,
                    Vec::new(),
                    Some("Binance open Algo response was invalid".to_string()),
                );
            }
        };
        let orders: Vec<Value> = rows
            .into_iter()
            .filter_map(|row| match row {
                Value::Object(map) => Some(Value::Object(Self::normalize(&map))),
                _ => None,
            })
            .collect();
        AlgoOrdersSnapshot::new(true, orders, None)
    }
}

fn map_not_supported_post(err: ExchangeError) -> ExchangeError {
    if err.class == "NotSupported" {
        ExchangeError::new(
            "RuntimeError",
            "Binance Algo Order endpoint is unavailable in the installed exchange adapter"
                .to_string(),
        )
    } else {
        err
    }
}

fn value_to_string_or_empty(value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::String(s) => s.clone(),
        Value::Bool(b) => {
            if *b {
                "True".to_string()
            } else {
                "False".to_string()
            }
        }
        Value::Number(n) => n.to_string(),
        Value::Array(_) | Value::Object(_) => value.to_string(),
    }
}

fn bool_field(value: Option<&Value>, default: &str) -> bool {
    match value {
        None | Some(Value::Null) => default.to_lowercase() == "true",
        Some(Value::Bool(b)) => *b,
        Some(other) => {
            let s = value_to_string_or_empty(other).to_lowercase();
            s == "true"
        }
    }
}

/// Helper used by other modules that want the compact id without a gateway.
pub fn market_id_from_exchange<E: FuturesExchange>(exchange: &E, symbol: &str) -> String {
    if let Some(id) = lookup_market_id(exchange, symbol) {
        return id;
    }
    normalize_futures_market_id(Some(&Value::String(symbol.to_string())))
}

#[allow(dead_code)]
fn _bool_text_unused() -> String {
    bool_text(&Value::Null)
}
