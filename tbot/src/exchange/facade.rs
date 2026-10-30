use std::future::Future;

use serde_json::{Map, Value};

use super::errors::ExchangeError;
use crate::core::candles::Candle;

/// Minimal ccxt-compatible USD-M futures exchange interface.
pub trait FuturesExchange: Send + Sync {
    fn load_markets(
        &self,
    ) -> impl Future<Output = Result<Map<String, Value>, ExchangeError>> + Send;

    fn markets(&self) -> Option<Map<String, Value>>;

    fn exchange_info_raw(&self) -> impl Future<Output = Result<Value, ExchangeError>> + Send;

    fn fetch_ohlcv(
        &self,
        symbol: &str,
        timeframe: &str,
        since: Option<i64>,
        limit: Option<u32>,
    ) -> impl Future<Output = Result<Vec<Candle>, ExchangeError>> + Send;

    fn premium_index_raw(
        &self,
        market_id: &str,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send;

    fn commission_rate_raw(
        &self,
        market_id: &str,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send;

    fn fetch_balance(&self) -> impl Future<Output = Result<Value, ExchangeError>> + Send;

    fn position_side_dual_raw(&self) -> impl Future<Output = Result<Value, ExchangeError>> + Send;

    fn set_margin_mode(
        &self,
        mode: &str,
        symbol: &str,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send;

    fn set_leverage(
        &self,
        leverage: u32,
        symbol: &str,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send;

    fn position_risk_v2_raw(
        &self,
        market_id: &str,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send;

    fn fetch_positions(
        &self,
        symbols: Option<Vec<String>>,
    ) -> impl Future<Output = Result<Vec<Value>, ExchangeError>> + Send;

    fn get_order_raw(
        &self,
        market_id: &str,
        orig_client_order_id: &str,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send;

    fn fetch_open_orders(
        &self,
        symbol: Option<&str>,
    ) -> impl Future<Output = Result<Vec<Value>, ExchangeError>> + Send;

    fn cancel_order(
        &self,
        order_id: &str,
        symbol: &str,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send;

    fn create_order(
        &self,
        symbol: &str,
        order_type: &str,
        side: &str,
        amount: f64,
        price: Option<f64>,
        params: Map<String, Value>,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send;

    fn algo_post_raw(
        &self,
        params: Map<String, Value>,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send;

    fn algo_get_raw(
        &self,
        client_algo_id: &str,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send;

    fn algo_open_raw(&self) -> impl Future<Output = Result<Value, ExchangeError>> + Send;

    fn algo_delete_raw(
        &self,
        client_algo_id: &str,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send;

    fn user_trades_raw(
        &self,
        market_id: &str,
        start_time: Option<i64>,
        limit: u32,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send;

    fn income_raw(
        &self,
        market_id: &str,
        income_type: Option<&str>,
        start_time: Option<i64>,
        limit: u32,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send;

    fn fetch_tickers(
        &self,
    ) -> impl Future<Output = Result<Map<String, Value>, ExchangeError>> + Send;
}
