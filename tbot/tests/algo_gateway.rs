use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use serde_json::{Map, Value};
use tbot::exchange::{ExchangeError, FuturesExchange};
use tbot::safety::algo_gateway::BinanceAlgoOrderGateway;

#[derive(Clone)]
enum AlgoResponseMode {
    Normal,
    CloseAll,
}

struct FakeBinance {
    inner: Mutex<FakeState>,
    mode: Mutex<AlgoResponseMode>,
}

struct FakeState {
    params: Option<Map<String, Value>>,
}

impl FakeBinance {
    fn new() -> Self {
        Self {
            inner: Mutex::new(FakeState { params: None }),
            mode: Mutex::new(AlgoResponseMode::Normal),
        }
    }

    fn params(&self) -> Map<String, Value> {
        self.inner
            .lock()
            .unwrap()
            .params
            .clone()
            .unwrap_or_default()
    }

    fn set_mode(&self, mode: AlgoResponseMode) {
        *self.mode.lock().unwrap() = mode;
    }
}

impl FuturesExchange for FakeBinance {
    fn markets(&self) -> Option<Map<String, Value>> {
        let mut markets = Map::new();
        let mut entry = Map::new();
        entry.insert("id".to_string(), Value::String("HMSTRUSDT".to_string()));
        markets.insert("HMSTR/USDT:USDT".to_string(), Value::Object(entry));
        Some(markets)
    }

    async fn algo_post_raw(&self, params: Map<String, Value>) -> Result<Value, ExchangeError> {
        self.inner.lock().unwrap().params = Some(params.clone());
        let mode = self.mode.lock().unwrap().clone();
        match mode {
            AlgoResponseMode::Normal => {
                let mut out = Map::new();
                out.insert("algoId".to_string(), Value::from(123));
                out.insert(
                    "clientAlgoId".to_string(),
                    params.get("clientAlgoId").cloned().unwrap_or(Value::Null),
                );
                out.insert(
                    "symbol".to_string(),
                    params.get("symbol").cloned().unwrap_or(Value::Null),
                );
                out.insert(
                    "side".to_string(),
                    params.get("side").cloned().unwrap_or(Value::Null),
                );
                out.insert(
                    "orderType".to_string(),
                    params.get("type").cloned().unwrap_or(Value::Null),
                );
                out.insert(
                    "quantity".to_string(),
                    params.get("quantity").cloned().unwrap_or(Value::Null),
                );
                out.insert(
                    "triggerPrice".to_string(),
                    params.get("triggerPrice").cloned().unwrap_or(Value::Null),
                );
                out.insert(
                    "workingType".to_string(),
                    params.get("workingType").cloned().unwrap_or(Value::Null),
                );
                out.insert(
                    "priceProtect".to_string(),
                    params.get("priceProtect").cloned().unwrap_or(Value::Null),
                );
                out.insert(
                    "reduceOnly".to_string(),
                    params.get("reduceOnly").cloned().unwrap_or(Value::Null),
                );
                out.insert("algoStatus".to_string(), Value::String("NEW".to_string()));
                Ok(Value::Object(out))
            }
            AlgoResponseMode::CloseAll => {
                let mut out = Map::new();
                out.insert("algoId".to_string(), Value::from(456));
                out.insert(
                    "clientAlgoId".to_string(),
                    params.get("clientAlgoId").cloned().unwrap_or(Value::Null),
                );
                out.insert(
                    "symbol".to_string(),
                    params.get("symbol").cloned().unwrap_or(Value::Null),
                );
                out.insert(
                    "side".to_string(),
                    params.get("side").cloned().unwrap_or(Value::Null),
                );
                out.insert(
                    "orderType".to_string(),
                    params.get("type").cloned().unwrap_or(Value::Null),
                );
                out.insert(
                    "triggerPrice".to_string(),
                    params.get("triggerPrice").cloned().unwrap_or(Value::Null),
                );
                out.insert(
                    "closePosition".to_string(),
                    params.get("closePosition").cloned().unwrap_or(Value::Null),
                );
                out.insert("algoStatus".to_string(), Value::String("NEW".to_string()));
                Ok(Value::Object(out))
            }
        }
    }
}

#[tokio::test]
async fn test_stop_market_uses_binance_algo_service_and_mark_price() {
    let exchange = Arc::new(FakeBinance::new());
    let gateway = BinanceAlgoOrderGateway::new(exchange.clone());
    let order = gateway
        .create_conditional_order(
            "HMSTR/USDT:USDT",
            "STOP_MARKET",
            "sell",
            Some(Value::String("1000".to_string())),
            Value::String("0.0002".to_string()),
            "utbslhmstr123",
            true,
            "MARK_PRICE",
            false,
            None,
            false,
            None,
        )
        .await
        .unwrap();

    let params = exchange.params();
    assert_eq!(
        params.get("algoType"),
        Some(&Value::String("CONDITIONAL".to_string()))
    );
    assert_eq!(
        params.get("workingType"),
        Some(&Value::String("MARK_PRICE".to_string()))
    );
    assert_eq!(
        params.get("priceProtect"),
        Some(&Value::String("false".to_string()))
    );
    assert_eq!(
        params.get("reduceOnly"),
        Some(&Value::String("true".to_string()))
    );
    assert_eq!(
        params.get("triggerPrice"),
        Some(&Value::String("0.0002".to_string()))
    );
    assert_eq!(
        order.get("_protection_source"),
        Some(&Value::String("binance_algo".to_string()))
    );
}

#[tokio::test]
async fn test_close_position_market_trigger_omits_quantity_and_reduce_only() {
    let exchange = Arc::new(FakeBinance::new());
    exchange.set_mode(AlgoResponseMode::CloseAll);
    let gateway = BinanceAlgoOrderGateway::new(exchange.clone());
    gateway
        .create_conditional_order(
            "HMSTR/USDT:USDT",
            "STOP_MARKET",
            "sell",
            None,
            Value::String("0.0002".to_string()),
            "closeallhmstr123",
            true,
            "MARK_PRICE",
            false,
            None,
            true,
            None,
        )
        .await
        .unwrap();
    let params = exchange.params();
    assert_eq!(
        params.get("closePosition"),
        Some(&Value::String("true".to_string()))
    );
    assert!(!params.contains_key("quantity"));
    assert!(!params.contains_key("reduceOnly"));
}

#[tokio::test]
async fn test_close_position_rejects_invalid_parameter_combinations() {
    let exchange = Arc::new(FakeBinance::new());
    let gateway = BinanceAlgoOrderGateway::new(exchange.clone());

    let res1 = gateway
        .create_conditional_order(
            "HMSTR/USDT:USDT",
            "STOP",
            "sell",
            None,
            Value::String("0.0002".to_string()),
            "badclose1",
            true,
            "MARK_PRICE",
            false,
            None,
            true,
            None,
        )
        .await;
    assert!(res1.is_err());
    let err1 = res1.err().unwrap();
    assert_eq!(err1.class, "ValueError");
    assert!(err1.message.contains("closePosition=true"));

    let res2 = gateway
        .create_conditional_order(
            "HMSTR/USDT:USDT",
            "STOP_MARKET",
            "sell",
            None,
            Value::String("0.0002".to_string()),
            "badclose2",
            true,
            "MARK_PRICE",
            false,
            None,
            true,
            Some(Value::String("0.0001".to_string())),
        )
        .await;
    assert!(res2.is_err());
    let err2 = res2.err().unwrap();
    assert_eq!(err2.class, "ValueError");
    assert!(err2.message.contains("must not include price"));
}

#[test]
fn test_normalize_preserves_manual_close_position_fields() {
    let mut raw = Map::new();
    raw.insert("algoId".to_string(), Value::from(77));
    raw.insert(
        "clientAlgoId".to_string(),
        Value::String("manual-stop".to_string()),
    );
    raw.insert("symbol".to_string(), Value::String("KORUUSDT".to_string()));
    raw.insert(
        "orderType".to_string(),
        Value::String("STOP_MARKET".to_string()),
    );
    raw.insert("side".to_string(), Value::String("BUY".to_string()));
    raw.insert(
        "triggerPrice".to_string(),
        Value::String("467.14".to_string()),
    );
    raw.insert(
        "closePosition".to_string(),
        Value::String("true".to_string()),
    );
    raw.insert(
        "positionSide".to_string(),
        Value::String("BOTH".to_string()),
    );
    raw.insert(
        "workingType".to_string(),
        Value::String("CONTRACT_PRICE".to_string()),
    );
    raw.insert("algoStatus".to_string(), Value::String("NEW".to_string()));
    let normalized = BinanceAlgoOrderGateway::<FakeBinance>::normalize(&raw);
    assert_eq!(normalized.get("closePosition"), Some(&Value::Bool(true)));
    assert_eq!(
        normalized.get("positionSide"),
        Some(&Value::String("BOTH".to_string()))
    );
    assert_eq!(
        normalized.get("triggerPrice"),
        Some(&Value::String("467.14".to_string()))
    );
}

struct MarketLookupUnavailable;

impl FuturesExchange for MarketLookupUnavailable {
    fn markets(&self) -> Option<Map<String, Value>> {
        None
    }
}

#[tokio::test]
async fn test_market_id_fallback_handles_usdc_settlement_without_duplicate_quote() {
    let gateway = BinanceAlgoOrderGateway::new(Arc::new(MarketLookupUnavailable));
    let ltc_id = gateway.market_id("LTC/USDC:USDC");
    assert_eq!(ltc_id, "LTCUSDC");
    let btc_id = gateway.market_id("BTC/USDT:USDT");
    assert_eq!(btc_id, "BTCUSDT");
}

#[allow(dead_code)]
fn _unused() -> HashMap<String, String> {
    HashMap::new()
}
