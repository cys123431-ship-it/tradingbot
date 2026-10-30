//! Temporary NotImplemented bodies for child (BtcMaCrossService) methods that
//! the ported parent methods in `trading.rs` call.  Later steps replace them.

use serde_json::{Map, Value};

use crate::exchange::FuturesExchange;
use crate::strategy::service::{ServiceError, SmaCrossService};

impl<E: FuturesExchange + 'static> SmaCrossService<E> {
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
