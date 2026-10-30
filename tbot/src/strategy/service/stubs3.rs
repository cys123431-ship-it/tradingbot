//! Temporary NotImplemented bodies for S4 methods called from sma.rs.

use serde_json::{Map, Value};

use crate::exchange::FuturesExchange;
use crate::strategy::service::{ServiceError, SmaCrossService};

impl<E: FuturesExchange + 'static> SmaCrossService<E> {
    // STUBS: retry_reverse_entry(network, cfg, mode, state, signal) -> Value
    pub(crate) async fn retry_reverse_entry(
        &self,
        _network: &str,
        _cfg: &Value,
        _mode: &str,
        _state: &mut Map<String, Value>,
        _signal: &Value,
    ) -> Result<Value, ServiceError> {
        Err(ServiceError::new("NotImplemented", "retry_reverse_entry"))
    }
}
