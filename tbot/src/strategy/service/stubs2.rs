//! Temporary NotImplemented bodies for child (BtcMaCrossService) methods that
//! the ported parent methods in `trading.rs` call.  Later steps replace them.

use rust_decimal::Decimal;
use serde_json::{Map, Value};

use crate::exchange::FuturesExchange;
use crate::strategy::service::{ServiceError, SmaCrossService};

impl<E: FuturesExchange + 'static> SmaCrossService<E> {
    // STUBS: new_trade(network,mode,signal,decision,context,trade_id,entry_price,quantity,entry_ms,rules,cfg)
    //        -> Result<Map,String ServiceError>
    #[allow(clippy::too_many_arguments)]
    pub(crate) async fn new_trade(
        &self,
        _network: &str,
        _mode: &str,
        _signal: &Value,
        _decision: &Value,
        _context: &Map<String, Value>,
        _trade_id: &str,
        _entry_price: &str,
        _quantity: Decimal,
        _entry_ms: i64,
        _rules: &crate::core::rules::TradingRules,
        _cfg: &Value,
    ) -> Result<Map<String, Value>, ServiceError> {
        Err(ServiceError::new("NotImplemented", "new_trade"))
    }

    // STUBS: on_trade_closed(network,mode,state,trade)
    pub(crate) fn on_trade_closed(
        &self,
        _network: &str,
        _mode: &str,
        _state: &mut Map<String, Value>,
        _trade: &Map<String, Value>,
    ) {
    }

    // STUBS: protection_spec(cfg,trade,kind) -> Result<Map,ServiceError>
    pub(crate) fn protection_spec(
        &self,
        _cfg: &Value,
        _trade: &Map<String, Value>,
        _kind: &str,
    ) -> Result<Map<String, Value>, ServiceError> {
        Err(ServiceError::new("NotImplemented", "protection_spec"))
    }

    // STUBS: entry_text(trade,mode) -> Result<String,ServiceError>
    pub(crate) fn entry_text(
        &self,
        _trade: &Map<String, Value>,
        _mode: &str,
    ) -> Result<String, ServiceError> {
        Err(ServiceError::new("NotImplemented", "entry_text"))
    }

    // STUBS: exit_text(trade,mode) -> Result<String,ServiceError>
    pub(crate) fn exit_text(
        &self,
        _trade: &Map<String, Value>,
        _mode: &str,
    ) -> Result<String, ServiceError> {
        Err(ServiceError::new("NotImplemented", "exit_text"))
    }
}
