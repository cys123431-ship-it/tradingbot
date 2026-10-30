use std::str::FromStr;
use std::sync::Arc;

use rust_decimal::prelude::*;
use rust_decimal::Decimal;
use serde_json::{Map, Value};
use tokio::sync::RwLock;

use super::binance::{BinanceClient, NewAlgoOrder, NewOrder};
use super::ccxt_shape::{self, Markets};
use super::errors::ExchangeError;
use super::facade::FuturesExchange;
use crate::core::candles::Candle;

/// Real USD-M futures implementation backed by `BinanceClient`.
pub struct BinanceFutures {
    client: BinanceClient,
    markets: Arc<RwLock<Option<Markets>>>,
}

impl BinanceFutures {
    pub fn new(client: BinanceClient) -> Self {
        Self {
            client,
            markets: Arc::new(RwLock::new(None)),
        }
    }

    pub fn with_markets(client: BinanceClient, markets: Markets) -> Self {
        Self {
            client,
            markets: Arc::new(RwLock::new(Some(markets))),
        }
    }

    /// Ensures the market cache is populated and returns the cached markets.
    async fn ensure_markets(&self) -> Result<Markets, ExchangeError> {
        {
            let guard = self.markets.read().await;
            if let Some(m) = guard.as_ref() {
                return Ok(m.clone());
            }
        }
        let info = self.client.exchange_info().await?;
        let markets = Markets::from_exchange_info(&info);
        let mut guard = self.markets.write().await;
        *guard = Some(markets.clone());
        Ok(markets)
    }

    async fn market_id(&self, symbol: &str) -> Result<String, ExchangeError> {
        let markets = self.ensure_markets().await?;
        let market = markets.market(symbol).ok_or_else(|| {
            ExchangeError::new(
                "BadSymbol",
                format!("binance does not have market symbol {symbol}"),
            )
        })?;
        market
            .get("id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
            .ok_or_else(|| {
                ExchangeError::new(
                    "BadSymbol",
                    format!("binance does not have market symbol {symbol}"),
                )
            })
    }

    async fn market_value(&self, symbol: &str) -> Result<Value, ExchangeError> {
        let markets = self.ensure_markets().await?;
        markets.market(symbol).cloned().ok_or_else(|| {
            ExchangeError::new(
                "BadSymbol",
                format!("binance does not have market symbol {symbol}"),
            )
        })
    }
}

fn to_decimal(v: f64) -> Decimal {
    Decimal::from_f64(v).unwrap_or_else(|| Decimal::from_str(&v.to_string()).unwrap_or_default())
}

fn precision_step(market: &Value, key: &str, step_key: &str) -> Option<Decimal> {
    let precision = market.get("precision")?.get(key)?;
    // precision may be an integer (decimals) or a step string/number.
    if let Some(n) = precision.as_u64() {
        return Some(Decimal::new(1, n as u32));
    }
    if let Some(s) = precision.as_str() {
        return Decimal::from_str(s).ok();
    }
    if let Some(f) = precision.as_f64() {
        return Decimal::from_f64(f);
    }
    let _ = step_key;
    None
}

fn trim_zeros(s: String) -> String {
    if s.contains('.') {
        let trimmed = s.trim_end_matches('0').trim_end_matches('.').to_string();
        if trimmed.is_empty() {
            "0".to_string()
        } else {
            trimmed
        }
    } else {
        s
    }
}

/// Truncates (floor toward zero) `amount` to the market's amount precision.
pub fn amount_to_precision(market: &Value, amount: f64) -> String {
    let step = precision_step(market, "amount", "stepSize")
        .filter(|d| !d.is_zero())
        .unwrap_or(Decimal::new(1, 8));
    let dec = to_decimal(amount);
    let steps = (dec / step).floor();
    let result = steps * step;
    trim_zeros(result.normalize().to_string())
}

/// Rounds `price` toward nearest on the market's price precision (ccxt uses ROUND).
pub fn price_to_precision(market: &Value, price: f64) -> String {
    let step = precision_step(market, "price", "tickSize")
        .filter(|d| !d.is_zero())
        .unwrap_or(Decimal::new(1, 8));
    let dec = to_decimal(price);
    let steps = (dec / step).round();
    let result = steps * step;
    trim_zeros(result.normalize().to_string())
}

fn value_to_string(v: &Value) -> Option<String> {
    match v {
        Value::String(s) => Some(s.clone()),
        Value::Bool(b) => Some(if *b {
            "True".to_string()
        } else {
            "False".to_string()
        }),
        Value::Number(n) => Some(n.to_string()),
        Value::Null => None,
        other => Some(other.to_string()),
    }
}

fn value_to_bool(v: &Value) -> Option<bool> {
    match v {
        Value::Bool(b) => Some(*b),
        Value::String(s) => match s.as_str() {
            "true" | "True" | "1" => Some(true),
            "false" | "False" | "0" => Some(false),
            _ => None,
        },
        Value::Number(n) => n.as_i64().map(|i| i != 0),
        _ => None,
    }
}

impl FuturesExchange for BinanceFutures {
    async fn load_markets(&self) -> Result<Map<String, Value>, ExchangeError> {
        let markets = self.ensure_markets().await?;
        Ok(markets.as_map().clone())
    }

    fn markets(&self) -> Option<Map<String, Value>> {
        let guard = self.markets.try_read().ok()?;
        guard.as_ref().map(|m| m.as_map().clone())
    }

    async fn exchange_info_raw(&self) -> Result<Value, ExchangeError> {
        Ok(self.client.exchange_info().await?)
    }

    async fn fetch_ohlcv(
        &self,
        symbol: &str,
        timeframe: &str,
        since: Option<i64>,
        limit: Option<u32>,
    ) -> Result<Vec<Candle>, ExchangeError> {
        let id = self.market_id(symbol).await?;
        let limit = limit.unwrap_or(500);
        Ok(self
            .client
            .klines_since(&id, timeframe, since, limit)
            .await?)
    }

    async fn premium_index_raw(&self, market_id: &str) -> Result<Value, ExchangeError> {
        Ok(self.client.premium_index(market_id).await?)
    }

    async fn commission_rate_raw(&self, market_id: &str) -> Result<Value, ExchangeError> {
        Ok(self.client.commission_rate(market_id).await?)
    }

    async fn fetch_balance(&self) -> Result<Value, ExchangeError> {
        let account = self.client.account().await?;
        Ok(ccxt_shape::parse_balance_linear(&account))
    }

    async fn position_side_dual_raw(&self) -> Result<Value, ExchangeError> {
        Ok(self.client.position_side_dual().await?)
    }

    async fn set_margin_mode(&self, mode: &str, symbol: &str) -> Result<Value, ExchangeError> {
        let id = self.market_id(symbol).await?;
        let upper = mode.to_uppercase();
        let mode_str = if upper == "CROSS" {
            "CROSSED".to_string()
        } else {
            upper
        };
        Ok(self.client.set_margin_type(&id, &mode_str).await?)
    }

    async fn set_leverage(&self, leverage: u32, symbol: &str) -> Result<Value, ExchangeError> {
        let id = self.market_id(symbol).await?;
        Ok(self.client.set_leverage(&id, leverage).await?)
    }

    async fn position_risk_v2_raw(&self, market_id: &str) -> Result<Value, ExchangeError> {
        Ok(self.client.position_risk_v2(Some(market_id)).await?)
    }

    async fn fetch_positions(
        &self,
        symbols: Option<Vec<String>>,
    ) -> Result<Vec<Value>, ExchangeError> {
        let raw = self.client.position_risk(None).await?;
        let markets = self.ensure_markets().await?;
        let mut out = Vec::new();
        for row in raw.as_array().cloned().unwrap_or_default() {
            // ccxt fetch_positions_risk keeps rows whose raw entryPrice > 0.
            let entry = row
                .get("entryPrice")
                .and_then(|v| v.as_str())
                .and_then(|s| s.parse::<f64>().ok())
                .unwrap_or(0.0);
            if entry <= 0.0 {
                continue;
            }
            let market_id = row.get("symbol").and_then(|s| s.as_str()).unwrap_or("");
            let parsed = ccxt_shape::parse_position_risk(&row, markets.market_by_id(market_id));
            if let Some(wanted) = &symbols {
                let sym = parsed.get("symbol").and_then(|s| s.as_str()).unwrap_or("");
                if !wanted.iter().any(|w| w == sym) {
                    continue;
                }
            }
            out.push(parsed);
        }
        Ok(out)
    }

    async fn get_order_raw(
        &self,
        market_id: &str,
        orig_client_order_id: &str,
    ) -> Result<Value, ExchangeError> {
        Ok(self
            .client
            .query_order(market_id, orig_client_order_id)
            .await?)
    }

    async fn fetch_open_orders(&self, symbol: Option<&str>) -> Result<Vec<Value>, ExchangeError> {
        let Some(sym) = symbol else {
            return Ok(Vec::new());
        };
        let id = self.market_id(sym).await?;
        let market = self.market_value(sym).await.ok();
        let raw = self.client.open_orders(&id).await?;
        let rows = raw.as_array().cloned().unwrap_or_default();
        Ok(rows
            .iter()
            .map(|row| ccxt_shape::parse_order(row, market.as_ref()))
            .collect())
    }

    async fn cancel_order(&self, order_id: &str, symbol: &str) -> Result<Value, ExchangeError> {
        let id = self.market_id(symbol).await?;
        let market = self.market_value(symbol).await.ok();
        let raw = self.client.cancel_order_by_id(&id, order_id).await?;
        Ok(ccxt_shape::parse_order(&raw, market.as_ref()))
    }

    async fn create_order(
        &self,
        symbol: &str,
        order_type: &str,
        side: &str,
        amount: f64,
        price: Option<f64>,
        params: Map<String, Value>,
    ) -> Result<Value, ExchangeError> {
        let id = self.market_id(symbol).await?;
        let market = self.market_value(symbol).await?;
        let quantity = amount_to_precision(&market, amount);
        let type_upper = order_type.to_uppercase();
        let is_limit = type_upper == "LIMIT";

        let new_client_order_id = params
            .get("newClientOrderId")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
            .ok_or_else(|| {
                ExchangeError::new("ArgumentsRequired", "newClientOrderId is required")
            })?;

        let mut request = NewOrder {
            symbol: id.clone(),
            side: side.to_uppercase(),
            order_type: type_upper.clone(),
            ..NewOrder::default()
        };
        request.quantity = Some(quantity);
        if is_limit {
            let p = price.unwrap_or(0.0);
            request.price = Some(price_to_precision(&market, p));
            let tif = params
                .get("timeInForce")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string())
                .unwrap_or_else(|| "GTC".to_string());
            request.time_in_force = Some(tif);
        }
        request.reduce_only = params
            .get("reduceOnly")
            .and_then(value_to_bool)
            .unwrap_or(false);
        request.new_client_order_id = new_client_order_id;
        request.new_order_resp_type = Some("RESULT".to_string());
        request.position_side = params
            .get("positionSide")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        let resp = self.client.new_order(&request).await?;
        Ok(ccxt_shape::parse_order(&resp, Some(&market)))
    }

    async fn algo_post_raw(&self, params: Map<String, Value>) -> Result<Value, ExchangeError> {
        let text = |key: &str| params.get(key).and_then(value_to_string);
        let flag = |key: &str| params.get(key).and_then(value_to_bool).unwrap_or(false);
        let req = NewAlgoOrder {
            symbol: text("symbol").unwrap_or_default(),
            side: text("side").unwrap_or_default(),
            order_type: text("type").unwrap_or_default(),
            quantity: text("quantity"),
            trigger_price: text("triggerPrice").unwrap_or_default(),
            client_algo_id: text("clientAlgoId").unwrap_or_default(),
            reduce_only: flag("reduceOnly"),
            working_type: text("workingType").unwrap_or_else(|| "MARK_PRICE".to_string()),
            price_protect: flag("priceProtect"),
            position_side: text("positionSide"),
            close_position: flag("closePosition"),
            price: text("price"),
        };
        Ok(self.client.new_algo_order(&req).await?)
    }

    async fn algo_get_raw(&self, client_algo_id: &str) -> Result<Value, ExchangeError> {
        Ok(self.client.query_algo_order(client_algo_id).await?)
    }

    async fn algo_open_raw(&self) -> Result<Value, ExchangeError> {
        Ok(self.client.open_algo_orders(None).await?)
    }

    async fn algo_delete_raw(&self, client_algo_id: &str) -> Result<Value, ExchangeError> {
        Ok(self.client.cancel_algo_order(client_algo_id).await?)
    }

    async fn user_trades_raw(
        &self,
        market_id: &str,
        start_time: Option<i64>,
        limit: u32,
    ) -> Result<Value, ExchangeError> {
        Ok(self
            .client
            .user_trades(market_id, start_time, limit)
            .await?)
    }

    async fn income_raw(
        &self,
        market_id: &str,
        income_type: Option<&str>,
        start_time: Option<i64>,
        limit: u32,
    ) -> Result<Value, ExchangeError> {
        Ok(self
            .client
            .income(Some(market_id), income_type, start_time, limit)
            .await?)
    }

    async fn fetch_tickers(&self) -> Result<Map<String, Value>, ExchangeError> {
        let markets = self.ensure_markets().await?;
        let raw = self.client.ticker_24h().await?;
        let rows = raw.as_array().cloned().unwrap_or_default();
        let mut out: Map<String, Value> = Map::new();
        for row in rows {
            let id = row.get("symbol").and_then(|s| s.as_str()).unwrap_or("");
            if let Some(market) = markets.market_by_id(id) {
                let parsed = ccxt_shape::parse_ticker(&row, None);
                let symbol = market
                    .get("symbol")
                    .and_then(|s| s.as_str())
                    .unwrap_or(id)
                    .to_string();
                out.insert(symbol, parsed);
            }
        }
        Ok(out)
    }
}
