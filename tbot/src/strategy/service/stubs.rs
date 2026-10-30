//! Temporary NotImplemented bodies for methods ported in later service steps.
//! Each step deletes the stubs it replaces.

use serde_json::{Map, Value};

use crate::exchange::FuturesExchange;
use crate::strategy::service::{ServiceError, SmaCrossService};

impl<E: FuturesExchange + 'static> SmaCrossService<E> {
    // STUBS: maybe_enter(network,cfg,mode,state) -> Result<Value, ServiceError>
    pub async fn maybe_enter(
        &self,
        _network: &str,
        _cfg: &Value,
        _mode: &str,
        _state: &mut Map<String, Value>,
    ) -> Result<Value, ServiceError> {
        Err(ServiceError::new("NotImplemented", "maybe_enter"))
    }

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

    // STUBS: reconcile_live(network,cfg,state) -> Result<(), ServiceError>
    pub async fn reconcile_live(
        &self,
        _network: &str,
        _cfg: &Value,
        _state: &mut Map<String, Value>,
    ) -> Result<(), ServiceError> {
        Err(ServiceError::new("NotImplemented", "reconcile_live"))
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

    // STUBS: pre_trade_validation(network,cfg,mode,signal) -> Result<(Option<Value>,String,Map),ServiceError>
    pub async fn pre_trade_validation(
        &self,
        _network: &str,
        _cfg: &Value,
        _mode: &str,
        _signal: &Value,
    ) -> Result<(Option<Value>, String, Map<String, Value>), ServiceError> {
        Err(ServiceError::new("NotImplemented", "pre_trade_validation"))
    }

    // STUBS: enter_paper(network,cfg,state,signal,decision,context) -> Result<Value,ServiceError>
    pub async fn enter_paper(
        &self,
        _network: &str,
        _cfg: &Value,
        _state: &mut Map<String, Value>,
        _signal: &Value,
        _decision: &Value,
        _context: &Map<String, Value>,
    ) -> Result<Value, ServiceError> {
        Err(ServiceError::new("NotImplemented", "enter_paper"))
    }

    // STUBS: enter_live(network,cfg,state,signal,decision,context) -> Result<Value,ServiceError>
    pub async fn enter_live(
        &self,
        _network: &str,
        _cfg: &Value,
        _state: &mut Map<String, Value>,
        _signal: &Value,
        _decision: &Value,
        _context: &Map<String, Value>,
    ) -> Result<Value, ServiceError> {
        Err(ServiceError::new("NotImplemented", "enter_live"))
    }

    // STUBS: finalize_paper(network,cfg,state,exit_price,exit_reason,exit_ms) -> Result<Value,ServiceError>
    pub async fn finalize_paper(
        &self,
        _network: &str,
        _cfg: &Value,
        _state: &mut Map<String, Value>,
        _exit_price: f64,
        _exit_reason: &str,
        _exit_ms: i64,
    ) -> Result<Value, ServiceError> {
        Err(ServiceError::new("NotImplemented", "finalize_paper"))
    }

    // STUBS: finalize_live(network,cfg,state,forced_reason: Option<&str>) -> Result<Value,ServiceError>
    pub async fn finalize_live(
        &self,
        _network: &str,
        _cfg: &Value,
        _state: &mut Map<String, Value>,
        _forced_reason: Option<&str>,
    ) -> Result<Value, ServiceError> {
        Err(ServiceError::new("NotImplemented", "finalize_live"))
    }

    // STUBS: close_position(reason: &str) -> Result<Value,ServiceError>
    pub async fn close_position(&self, _reason: &str) -> Result<Value, ServiceError> {
        Err(ServiceError::new("NotImplemented", "close_position"))
    }

    // STUBS: scan_universe(network,cfg) -> Result<Vec<(String,String,Option<f64>)>,ServiceError>
    pub async fn scan_universe(
        &self,
        _network: &str,
        _cfg: &Value,
    ) -> Result<Vec<(String, String, Option<f64>)>, ServiceError> {
        Err(ServiceError::new("NotImplemented", "scan_universe"))
    }

    // STUBS: run_pending_entry_mode_switch(network,mode,cfg,state) -> Result<Value,ServiceError>
    // Returns the (possibly forced) cfg to use for this cycle.
    pub async fn run_pending_entry_mode_switch(
        &self,
        _network: &str,
        _mode: &str,
        _cfg: &Value,
        _state: &mut Map<String, Value>,
    ) -> Result<Value, ServiceError> {
        Err(ServiceError::new(
            "NotImplemented",
            "run_pending_entry_mode_switch",
        ))
    }
}
