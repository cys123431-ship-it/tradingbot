//! Strategy definitions: config normalization, market classification.

pub mod coinselect;
pub mod config_base;
pub mod config_btc_ma;
pub mod config_ma_plus;
pub mod config_tradfi;
pub mod market_session;
pub mod options_config;
pub mod options_manage;
pub mod options_service;
#[cfg(test)]
mod options_service_tests;
pub mod service;
pub mod tradfi;
