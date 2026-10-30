use std::future::Future;

use serde_json::{Map, Value};

use super::errors::ExchangeError;
use crate::core::candles::Candle;

/// Minimal ccxt-compatible USD-M futures exchange interface.
///
/// Every method has a NotSupported default so test doubles only implement what they use.
#[allow(unused_variables)] // default bodies ignore their arguments
pub trait FuturesExchange: Send + Sync {
    fn load_markets(
        &self,
    ) -> impl Future<Output = Result<Map<String, Value>, ExchangeError>> + Send {
        async move { Err(ExchangeError::not_supported("load_markets")) }
    }

    fn markets(&self) -> Option<Map<String, Value>> {
        None
    }

    fn exchange_info_raw(&self) -> impl Future<Output = Result<Value, ExchangeError>> + Send {
        async move { Err(ExchangeError::not_supported("exchange_info_raw")) }
    }

    fn fetch_ohlcv(
        &self,
        symbol: &str,
        timeframe: &str,
        since: Option<i64>,
        limit: Option<u32>,
    ) -> impl Future<Output = Result<Vec<Candle>, ExchangeError>> + Send {
        async move { Err(ExchangeError::not_supported("fetch_ohlcv")) }
    }

    fn premium_index_raw(
        &self,
        market_id: &str,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send {
        async move { Err(ExchangeError::not_supported("premium_index_raw")) }
    }

    fn commission_rate_raw(
        &self,
        market_id: &str,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send {
        async move { Err(ExchangeError::not_supported("commission_rate_raw")) }
    }

    fn fetch_balance(&self) -> impl Future<Output = Result<Value, ExchangeError>> + Send {
        async move { Err(ExchangeError::not_supported("fetch_balance")) }
    }

    fn position_side_dual_raw(&self) -> impl Future<Output = Result<Value, ExchangeError>> + Send {
        async move { Err(ExchangeError::not_supported("position_side_dual_raw")) }
    }

    fn set_margin_mode(
        &self,
        mode: &str,
        symbol: &str,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send {
        async move { Err(ExchangeError::not_supported("set_margin_mode")) }
    }

    fn set_leverage(
        &self,
        leverage: u32,
        symbol: &str,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send {
        async move { Err(ExchangeError::not_supported("set_leverage")) }
    }

    fn position_risk_v2_raw(
        &self,
        market_id: &str,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send {
        async move { Err(ExchangeError::not_supported("position_risk_v2_raw")) }
    }

    fn fetch_positions(
        &self,
        symbols: Option<Vec<String>>,
    ) -> impl Future<Output = Result<Vec<Value>, ExchangeError>> + Send {
        async move { Err(ExchangeError::not_supported("fetch_positions")) }
    }

    fn get_order_raw(
        &self,
        market_id: &str,
        orig_client_order_id: &str,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send {
        async move { Err(ExchangeError::not_supported("get_order_raw")) }
    }

    fn fetch_open_orders(
        &self,
        symbol: Option<&str>,
    ) -> impl Future<Output = Result<Vec<Value>, ExchangeError>> + Send {
        async move { Err(ExchangeError::not_supported("fetch_open_orders")) }
    }

    fn cancel_order(
        &self,
        order_id: &str,
        symbol: &str,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send {
        async move { Err(ExchangeError::not_supported("cancel_order")) }
    }

    fn create_order(
        &self,
        symbol: &str,
        order_type: &str,
        side: &str,
        amount: f64,
        price: Option<f64>,
        params: Map<String, Value>,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send {
        async move { Err(ExchangeError::not_supported("create_order")) }
    }

    fn algo_post_raw(
        &self,
        params: Map<String, Value>,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send {
        async move { Err(ExchangeError::not_supported("algo_post_raw")) }
    }

    fn algo_get_raw(
        &self,
        client_algo_id: &str,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send {
        async move { Err(ExchangeError::not_supported("algo_get_raw")) }
    }

    fn algo_open_raw(&self) -> impl Future<Output = Result<Value, ExchangeError>> + Send {
        async move { Err(ExchangeError::not_supported("algo_open_raw")) }
    }

    fn algo_delete_raw(
        &self,
        client_algo_id: &str,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send {
        async move { Err(ExchangeError::not_supported("algo_delete_raw")) }
    }

    fn user_trades_raw(
        &self,
        market_id: &str,
        start_time: Option<i64>,
        limit: u32,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send {
        async move { Err(ExchangeError::not_supported("user_trades_raw")) }
    }

    fn income_raw(
        &self,
        market_id: &str,
        income_type: Option<&str>,
        start_time: Option<i64>,
        limit: u32,
    ) -> impl Future<Output = Result<Value, ExchangeError>> + Send {
        async move { Err(ExchangeError::not_supported("income_raw")) }
    }

    fn fetch_tickers(
        &self,
    ) -> impl Future<Output = Result<Map<String, Value>, ExchangeError>> + Send {
        async move { Err(ExchangeError::not_supported("fetch_tickers")) }
    }
}
