//! Temporary NotImplemented bodies for methods ported in later service steps.
//! Each step deletes the stubs it replaces.

use serde_json::{Map, Value};

use crate::exchange::FuturesExchange;
use crate::strategy::service::{ServiceError, SmaCrossService};

impl<E: FuturesExchange + 'static> SmaCrossService<E> {
    // STUBS: manage_paper(network,cfg,state) -> Result<Value, ServiceError>
    pub async fn manage_paper(
        &self,
        _network: &str,
        _cfg: &Value,
        _state: &mut Map<String, Value>,
    ) -> Result<Value, ServiceError> {
        Err(ServiceError::new("NotImplemented", "manage_paper"))
    }

    // STUBS: manage_live(network,cfg,state) -> Result<Value, ServiceError>
    pub async fn manage_live(
        &self,
        _network: &str,
        _cfg: &Value,
        _state: &mut Map<String, Value>,
    ) -> Result<Value, ServiceError> {
        Err(ServiceError::new("NotImplemented", "manage_live"))
    }

    // STUBS: verify_protection(network,cfg,state) -> Result<(), ServiceError>
    pub async fn verify_protection(
        &self,
        _network: &str,
        _cfg: &Value,
        _state: &mut Map<String, Value>,
    ) -> Result<(), ServiceError> {
        Err(ServiceError::new("NotImplemented", "verify_protection"))
    }

    // STUBS: attach_protection(network,cfg,state) -> Result<Value, ServiceError>
    pub async fn attach_protection(
        &self,
        _network: &str,
        _cfg: &Value,
        _state: &mut Map<String, Value>,
    ) -> Result<Value, ServiceError> {
        Err(ServiceError::new("NotImplemented", "attach_protection"))
    }
}
