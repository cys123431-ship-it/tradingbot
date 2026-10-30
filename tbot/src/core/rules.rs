//! Binance trading-rule parsing, Decimal rounding and pre-trade validation,
//! ported from `btc_ema_pullback/rules.py`.

use std::str::FromStr;

use rust_decimal::{Decimal, RoundingStrategy};
use serde::Serialize;
use serde_json::Value;
use thiserror::Error;

/// Exchange rules are missing or malformed: trading must not proceed.
#[derive(Debug, Error)]
pub enum TradingRulesError {
    #[error("not a number: {0}")]
    NotANumber(String),
    #[error("not a finite number: {0}")]
    NotFinite(String),
    #[error("exchangeInfo symbol entry missing")]
    EntryMissing,
    #[error("PRICE_FILTER/LOT_SIZE filters missing")]
    FiltersMissing,
    #[error("non-positive tickSize/stepSize/minQty")]
    NonPositiveSizes,
    #[error("{0} not found in exchangeInfo")]
    SymbolNotFound(String),
    #[error("step must be positive")]
    NonPositiveStep,
    #[error("tickSize must be positive")]
    NonPositiveTick,
    #[error("average entry price must be positive")]
    NonPositiveEntry,
}

/// Python repr-like string used in error messages (best-effort).
fn py_repr(v: Option<&Value>) -> String {
    match v {
        None => "None".to_string(),
        Some(Value::Null) => "None".to_string(),
        Some(Value::Bool(b)) => {
            if *b {
                "True".to_string()
            } else {
                "False".to_string()
            }
        }
        Some(Value::Number(n)) => n.to_string(),
        Some(Value::String(s)) => format!("'{}'", s),
        Some(other) => other.to_string(),
    }
}

fn parse_decimal_text(text: &str) -> Option<Decimal> {
    let text = text.trim();
    Decimal::from_str(text)
        .or_else(|_| Decimal::from_scientific(text))
        .ok()
}

/// Python's Decimal parses NaN/Infinity but they are not finite.
fn is_non_finite_text(text: &str) -> bool {
    let t = text
        .trim()
        .trim_start_matches(['+', '-'])
        .to_ascii_lowercase();
    matches!(t.as_str(), "nan" | "snan" | "inf" | "infinity")
}

/// `D(value, default)`: parse to a finite Decimal, else the supplied default.
pub fn d(value: Option<&Value>, default: Option<&str>) -> Result<Decimal, TradingRulesError> {
    let text = match value {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) => Some(s.clone()),
        Some(Value::Number(n)) => Some(n.to_string()),
        Some(Value::Bool(b)) => Some(if *b {
            "True".to_string()
        } else {
            "False".to_string()
        }),
        Some(other) => Some(other.to_string()),
    };
    if let Some(text) = text {
        if let Some(result) = parse_decimal_text(&text) {
            return Ok(result);
        }
        return match default {
            Some(default) => Ok(Decimal::from_str(default).unwrap()),
            None if is_non_finite_text(&text) => Err(TradingRulesError::NotFinite(py_repr(value))),
            None => Err(TradingRulesError::NotANumber(py_repr(value))),
        };
    }
    match default {
        Some(default) => Ok(Decimal::from_str(default).unwrap()),
        None => Err(TradingRulesError::NotANumber(py_repr(value))),
    }
}

fn d_str(s: &str, default: Option<&str>) -> Result<Decimal, TradingRulesError> {
    let v = Value::String(s.to_string());
    d(Some(&v), default)
}

/// Parsed Binance trading rules for one symbol.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct TradingRules {
    pub symbol: String,
    pub tick_size: Decimal,
    pub min_price: Decimal,
    pub max_price: Decimal,
    pub min_qty: Decimal,
    pub step_size: Decimal,
    pub max_qty: Decimal,
    pub market_min_qty: Decimal,
    pub market_step_size: Decimal,
    pub market_max_qty: Decimal,
    pub min_notional: Decimal,
    pub quantity_precision: i64,
    pub price_precision: i64,
    pub status: String,
    pub contract_type: String,
}

impl TradingRules {
    pub fn effective_min_qty(&self) -> Decimal {
        self.min_qty.max(self.market_min_qty)
    }
    pub fn effective_step(&self) -> Decimal {
        self.step_size.max(self.market_step_size)
    }
    pub fn effective_max_qty(&self) -> Decimal {
        self.max_qty.min(self.market_max_qty)
    }
    /// `as_log()`: every field as a string, keyed by the dataclass field names.
    pub fn as_log(&self) -> serde_json::Map<String, Value> {
        let mut m = serde_json::Map::new();
        let mut put = |k: &str, v: String| {
            m.insert(k.to_string(), Value::String(v));
        };
        put("symbol", self.symbol.clone());
        put("tick_size", self.tick_size.to_string());
        put("min_price", self.min_price.to_string());
        put("max_price", self.max_price.to_string());
        put("min_qty", self.min_qty.to_string());
        put("step_size", self.step_size.to_string());
        put("max_qty", self.max_qty.to_string());
        put("market_min_qty", self.market_min_qty.to_string());
        put("market_step_size", self.market_step_size.to_string());
        put("market_max_qty", self.market_max_qty.to_string());
        put("min_notional", self.min_notional.to_string());
        put("quantity_precision", self.quantity_precision.to_string());
        put("price_precision", self.price_precision.to_string());
        put("status", self.status.clone());
        put("contract_type", self.contract_type.clone());
        m
    }
}

fn get_str<'a>(v: &'a Value, key: &str) -> Option<&'a Value> {
    v.get(key)
}

/// Parse one `exchangeInfo.symbols[]` entry.
pub fn parse_trading_rules(symbol_info: &Value) -> Result<TradingRules, TradingRulesError> {
    let obj = match symbol_info {
        Value::Object(_) => symbol_info,
        _ => return Err(TradingRulesError::EntryMissing),
    };
    let sym = obj.get("symbol");
    let sym_ok = matches!(sym, Some(Value::String(s)) if !s.is_empty())
        || matches!(sym, Some(Value::Number(_)));
    if !sym_ok {
        return Err(TradingRulesError::EntryMissing);
    }
    let mut filters: std::collections::HashMap<String, &Value> = std::collections::HashMap::new();
    if let Some(Value::Array(rows)) = obj.get("filters") {
        for row in rows {
            if let Some(ft) = row.get("filterType") {
                if let Some(s) = ft.as_str() {
                    if !s.is_empty() {
                        filters.insert(s.to_string(), row);
                    }
                }
            }
        }
    }
    let price = filters.get("PRICE_FILTER").copied();
    let lot = filters.get("LOT_SIZE").copied();
    let (Some(price), Some(lot)) = (price, lot) else {
        return Err(TradingRulesError::FiltersMissing);
    };
    let market_lot = filters.get("MARKET_LOT_SIZE").copied().unwrap_or(lot);
    let notional = filters
        .get("MIN_NOTIONAL")
        .copied()
        .or_else(|| filters.get("NOTIONAL").copied())
        .unwrap_or(&Value::Null);

    let symbol_value = sym.unwrap();
    let symbol_str = match symbol_value {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    };
    let quantity_precision = obj
        .get("quantityPrecision")
        .and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|f| f as i64)))
        .unwrap_or(0);
    let price_precision = obj
        .get("pricePrecision")
        .and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|f| f as i64)))
        .unwrap_or(0);
    let status = obj
        .get("status")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let contract_type = obj
        .get("contractType")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();

    let rules = TradingRules {
        symbol: symbol_str,
        tick_size: d(get_str(price, "tickSize"), None)?,
        min_price: d(get_str(price, "minPrice"), Some("0"))?,
        max_price: d(get_str(price, "maxPrice"), Some("0"))?,
        min_qty: d(get_str(lot, "minQty"), None)?,
        step_size: d(get_str(lot, "stepSize"), None)?,
        max_qty: d(get_str(lot, "maxQty"), Some("1e18"))?,
        market_min_qty: d(get_str(market_lot, "minQty"), Some("0"))
            .ok()
            .map(|_| ())
            .map_or_else(
                || d(get_str(lot, "minQty"), None),
                |_| {
                    d(
                        get_str(market_lot, "minQty"),
                        get_str(lot, "minQty").and_then(|v| v.as_str()),
                    )
                },
            )?,
        market_step_size: d_get_market(market_lot, lot, "stepSize")?,
        market_max_qty: d_get_market(market_lot, lot, "maxQty")?,
        min_notional: d(
            notional
                .get("notional")
                .or_else(|| notional.get("minNotional"))
                .or(Some(&Value::Null)),
            Some("0"),
        )?,
        quantity_precision,
        price_precision,
        status,
        contract_type,
    };
    if rules.tick_size <= Decimal::ZERO
        || rules.step_size <= Decimal::ZERO
        || rules.min_qty <= Decimal::ZERO
    {
        return Err(TradingRulesError::NonPositiveSizes);
    }
    Ok(rules)
}

fn d_get_market(market_lot: &Value, lot: &Value, key: &str) -> Result<Decimal, TradingRulesError> {
    let fallback = get_str(lot, key).and_then(|v| v.as_str());
    d(get_str(market_lot, key), fallback)
}

/// Find the symbol in a full exchangeInfo payload and parse it.
pub fn rules_from_exchange_info(
    payload: &Value,
    symbol: &str,
) -> Result<TradingRules, TradingRulesError> {
    if let Some(Value::Array(rows)) = payload.get("symbols") {
        for row in rows {
            let row_sym = row.get("symbol").map(|v| match v {
                Value::String(s) => s.clone(),
                other => other.to_string(),
            });
            if row_sym.as_deref() == Some(symbol) {
                return parse_trading_rules(row);
            }
        }
    }
    Err(TradingRulesError::SymbolNotFound(symbol.to_string()))
}

/// Python keeps the step's exponent (0 * "0.001" -> "0.000"); rust_decimal may drop it.
fn with_step_scale(mut value: Decimal, step: Decimal) -> Decimal {
    if value.scale() < step.scale() {
        value.rescale(step.scale());
    }
    value
}

/// Floor value to a multiple of `step`.
pub fn floor_to_step(value: &str, step: &str) -> Result<Decimal, TradingRulesError> {
    let v = d_str(value, None)?;
    let s = d_str(step, None)?;
    if s <= Decimal::ZERO {
        return Err(TradingRulesError::NonPositiveStep);
    }
    let units = v / s;
    let floored = units.floor();
    Ok(with_step_scale(floored * s, s))
}

/// Ceil value to a multiple of `step`.
pub fn ceil_to_step(value: &str, step: &str) -> Result<Decimal, TradingRulesError> {
    let v = d_str(value, None)?;
    let s = d_str(step, None)?;
    if s <= Decimal::ZERO {
        return Err(TradingRulesError::NonPositiveStep);
    }
    let units = v / s;
    let ceiled = units.ceil();
    Ok(with_step_scale(ceiled * s, s))
}

/// Round a price to the tick size in the given direction (`down`/`up`/nearest).
pub fn round_price_to_tick(
    price: &str,
    tick: &str,
    direction: &str,
) -> Result<Decimal, TradingRulesError> {
    let p = d_str(price, None)?;
    let t = d_str(tick, None)?;
    if t <= Decimal::ZERO {
        return Err(TradingRulesError::NonPositiveTick);
    }
    let mut units = p / t;
    if direction == "down" {
        units = units.floor();
    } else if direction == "up" {
        units = units.ceil();
    } else {
        units = units.round_dp_with_strategy(0, RoundingStrategy::MidpointNearestEven);
    }
    // Keep only the tick's real decimals (tick "0.10" -> 1 place, "10" -> 0).
    let places = t.normalize().scale();
    let raw = units * t;
    let mut result = raw.round_dp_with_strategy(places, RoundingStrategy::MidpointNearestEven);
    result.rescale(places);
    Ok(result)
}

/// SL/TP from the actual average fill, rounded to tickSize towards the entry.
pub fn protection_prices(
    side: &str,
    avg_entry_price: &str,
    cfg: &ProtectionConfig,
    rules: &TradingRules,
) -> Result<(Decimal, Decimal), TradingRulesError> {
    let entry = d_str(avg_entry_price, None)?;
    if entry <= Decimal::ZERO {
        return Err(TradingRulesError::NonPositiveEntry);
    }
    let sl_pct = d_value(&cfg.stop_loss_pct)?;
    let tp_pct = d_value(&cfg.take_profit_pct)?;
    let tick = rules.tick_size.to_string();
    let one = Decimal::ONE;
    let (stop, take) = if side.to_uppercase() == "LONG" {
        let stop_price = entry * (one - sl_pct);
        let take_price = entry * (one + tp_pct);
        (
            round_price_to_tick(&stop_price.to_string(), &tick, "up")?,
            round_price_to_tick(&take_price.to_string(), &tick, "down")?,
        )
    } else {
        let stop_price = entry * (one + sl_pct);
        let take_price = entry * (one - tp_pct);
        (
            round_price_to_tick(&stop_price.to_string(), &tick, "down")?,
            round_price_to_tick(&take_price.to_string(), &tick, "up")?,
        )
    };
    Ok((stop, take))
}

fn d_value(v: &serde_json::Value) -> Result<Decimal, TradingRulesError> {
    d(Some(v), None)
}

/// Config keys read by `protection_prices` / `estimate_trade_risk`.
#[derive(Debug, Clone, serde::Deserialize)]
pub struct ProtectionConfig {
    pub stop_loss_pct: Value,
    pub take_profit_pct: Value,
}

/// Config keys read by risk estimation and sizing.
#[derive(Debug, Clone, serde::Deserialize)]
pub struct SizingConfig {
    pub stop_loss_pct: Value,
    pub slippage_buffer_pct: Value,
    pub leverage: Value,
    pub max_risk_per_trade_pct: Value,
    pub max_margin_usage_pct: Value,
    pub base_quantity: Value,
    #[serde(default)]
    pub allow_auto_raise_to_exchange_minimum: Option<bool>,
}

/// Result of `estimate_trade_risk`.
#[derive(Debug, Clone, Serialize)]
pub struct RiskEstimate {
    #[serde(serialize_with = "ser_decimal")]
    pub notional: Decimal,
    #[serde(serialize_with = "ser_decimal")]
    pub price_risk: Decimal,
    #[serde(serialize_with = "ser_decimal")]
    pub estimated_entry_fee: Decimal,
    #[serde(serialize_with = "ser_decimal")]
    pub estimated_exit_fee: Decimal,
    #[serde(serialize_with = "ser_decimal")]
    pub slippage_buffer: Decimal,
    #[serde(serialize_with = "ser_decimal")]
    pub estimated_total_risk: Decimal,
}

fn ser_decimal<S: serde::Serializer>(v: &Decimal, s: S) -> Result<S::Ok, S::Error> {
    s.serialize_str(&v.to_string())
}

/// Loss if the stop fills, including taker fees on both legs and slippage.
pub fn estimate_trade_risk(
    price: &str,
    quantity: &str,
    cfg: &SizingConfig,
    taker_fee_rate: &str,
) -> Result<RiskEstimate, TradingRulesError> {
    let p = d_str(price, None)?;
    let qty = d_str(quantity, None)?;
    let sl_pct = d_value(&cfg.stop_loss_pct)?;
    let fee_rate = d_str(taker_fee_rate, None)?;
    let slippage = d_value(&cfg.slippage_buffer_pct)?;
    let notional = p * qty;
    let price_risk = p * sl_pct * qty;
    let entry_fee = notional * fee_rate;
    let exit_fee = p * (Decimal::ONE + sl_pct) * qty * fee_rate;
    let slippage_cost = notional * slippage;
    Ok(RiskEstimate {
        notional,
        price_risk,
        estimated_entry_fee: entry_fee,
        estimated_exit_fee: exit_fee,
        slippage_buffer: slippage_cost,
        estimated_total_risk: price_risk + entry_fee + exit_fee + slippage_cost,
    })
}

fn risk_per_unit(
    price: &str,
    cfg: &SizingConfig,
    taker_fee_rate: &str,
) -> Result<Decimal, TradingRulesError> {
    Ok(estimate_trade_risk(price, "1", cfg, taker_fee_rate)?.estimated_total_risk)
}

/// Result of `validate_and_normalize_order_quantity`.
#[derive(Debug, Clone, Serialize)]
pub struct OrderDecision {
    pub accepted: bool,
    pub sizing_mode: String,
    #[serde(serialize_with = "ser_decimal")]
    pub price: Decimal,
    #[serde(serialize_with = "ser_decimal")]
    pub wallet_balance: Decimal,
    #[serde(serialize_with = "ser_decimal")]
    pub available_balance: Decimal,
    #[serde(serialize_with = "ser_decimal")]
    pub risk_limit: Decimal,
    #[serde(serialize_with = "ser_decimal")]
    pub leverage: Decimal,
    pub skip_reason: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(serialize_with = "ser_opt_decimal")]
    pub requested_quantity: Option<Decimal>,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(serialize_with = "ser_opt_decimal")]
    pub exchange_minimum_quantity: Option<Decimal>,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(serialize_with = "ser_opt_decimal")]
    pub risk_at_exchange_minimum: Option<Decimal>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub raised_to_exchange_minimum: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(serialize_with = "ser_opt_decimal")]
    pub notional: Option<Decimal>,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(serialize_with = "ser_opt_decimal")]
    pub price_risk: Option<Decimal>,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(serialize_with = "ser_opt_decimal")]
    pub estimated_entry_fee: Option<Decimal>,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(serialize_with = "ser_opt_decimal")]
    pub estimated_exit_fee: Option<Decimal>,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(serialize_with = "ser_opt_decimal")]
    pub slippage_buffer: Option<Decimal>,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(serialize_with = "ser_opt_decimal")]
    pub estimated_total_risk: Option<Decimal>,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(serialize_with = "ser_opt_decimal")]
    pub quantity: Option<Decimal>,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(serialize_with = "ser_opt_decimal")]
    pub estimated_margin: Option<Decimal>,
}

fn ser_opt_decimal<S: serde::Serializer>(v: &Option<Decimal>, s: S) -> Result<S::Ok, S::Error> {
    match v {
        Some(d) => s.serialize_str(&d.to_string()),
        None => s.serialize_none(),
    }
}

fn py_format_fixed(value: &Decimal, places: u32) -> String {
    format!("{:.1$}", value, places as usize)
}

/// Decide the order quantity or SKIP with a precise reason.
#[allow(clippy::too_many_arguments)]
pub fn validate_and_normalize_order_quantity(
    rules: &TradingRules,
    price: &str,
    wallet_balance: &str,
    available_balance: &str,
    cfg: &SizingConfig,
    taker_fee_rate: &str,
    sizing_mode: &str,
) -> Result<OrderDecision, TradingRulesError> {
    let price_d = d_str(price, None)?;
    let wallet = d_str(wallet_balance, Some("0"))?;
    let available = d_str(available_balance, Some("0"))?;
    let leverage = d_value(&cfg.leverage)?;
    let risk_limit = wallet * d_value(&cfg.max_risk_per_trade_pct)?;
    let step = rules.effective_step();
    let minimum_qty = rules.effective_min_qty();
    let mut decision = OrderDecision {
        accepted: false,
        sizing_mode: sizing_mode.to_string(),
        price: price_d,
        wallet_balance: wallet,
        available_balance: available,
        risk_limit,
        leverage,
        skip_reason: String::new(),
        requested_quantity: None,
        exchange_minimum_quantity: None,
        risk_at_exchange_minimum: None,
        raised_to_exchange_minimum: None,
        notional: None,
        price_risk: None,
        estimated_entry_fee: None,
        estimated_exit_fee: None,
        slippage_buffer: None,
        estimated_total_risk: None,
        quantity: None,
        estimated_margin: None,
    };
    if !rules.status.is_empty() && rules.status != "TRADING" {
        decision.skip_reason = format!("SYMBOL_NOT_TRADING:{}", rules.status);
        return Ok(decision);
    }
    if price_d <= Decimal::ZERO {
        decision.skip_reason = "INVALID_PRICE".to_string();
        return Ok(decision);
    }
    if wallet <= Decimal::ZERO {
        decision.skip_reason = "WALLET_BALANCE_UNAVAILABLE".to_string();
        return Ok(decision);
    }
    let requested = if sizing_mode == "risk_based" {
        let per_unit = risk_per_unit(price, cfg, taker_fee_rate)?;
        if per_unit.is_zero() {
            Decimal::ZERO
        } else {
            risk_limit / per_unit
        }
    } else {
        d_value(&cfg.base_quantity)?
    };
    let quantity = floor_to_step(&requested.to_string(), &step.to_string())?;
    decision.requested_quantity = Some(requested);

    let min_notional_units = if price_d.is_zero() {
        Decimal::ZERO
    } else {
        rules.min_notional / price_d
    };
    let ceil_min = ceil_to_step(&min_notional_units.to_string(), &step.to_string())?;
    let required_min = minimum_qty.max(ceil_min);
    decision.exchange_minimum_quantity = Some(required_min);
    let mut quantity = quantity;
    if quantity < required_min {
        let risk_at_min =
            estimate_trade_risk(price, &required_min.to_string(), cfg, taker_fee_rate)?;
        let risk_total = risk_at_min.estimated_total_risk;
        decision.risk_at_exchange_minimum = Some(risk_total);
        if !cfg.allow_auto_raise_to_exchange_minimum.unwrap_or(false) {
            decision.skip_reason = format!(
                "BELOW_EXCHANGE_MINIMUM: quantity {} < required {} (minQty {}, minNotional {} USDT @ {}); \
                 risk at required {} vs limit {} USDT; auto raise disabled",
                quantity,
                required_min,
                minimum_qty,
                rules.min_notional,
                price_d,
                py_format_fixed(&risk_total, 4),
                py_format_fixed(&risk_limit, 4)
            );
            return Ok(decision);
        }
        if risk_total > risk_limit {
            decision.skip_reason = format!(
                "EXCHANGE_MINIMUM_EXCEEDS_RISK_LIMIT: required {} risk {} > limit {} USDT",
                required_min,
                py_format_fixed(&risk_total, 4),
                py_format_fixed(&risk_limit, 4)
            );
            return Ok(decision);
        }
        quantity = required_min;
        decision.raised_to_exchange_minimum = Some(true);
    }
    if quantity > rules.effective_max_qty() {
        decision.skip_reason = format!(
            "ABOVE_MAX_QTY: {} > {}",
            quantity,
            rules.effective_max_qty()
        );
        return Ok(decision);
    }
    let risk = estimate_trade_risk(price, &quantity.to_string(), cfg, taker_fee_rate)?;
    decision.notional = Some(risk.notional);
    decision.price_risk = Some(risk.price_risk);
    decision.estimated_entry_fee = Some(risk.estimated_entry_fee);
    decision.estimated_exit_fee = Some(risk.estimated_exit_fee);
    decision.slippage_buffer = Some(risk.slippage_buffer);
    decision.estimated_total_risk = Some(risk.estimated_total_risk);
    decision.quantity = Some(quantity);
    let estimated_margin = if leverage.is_zero() {
        Decimal::ZERO
    } else {
        risk.notional / leverage
    };
    decision.estimated_margin = Some(estimated_margin);
    if risk.estimated_total_risk > risk_limit {
        let max_risk_pct = d_value(&cfg.max_risk_per_trade_pct)?;
        let pct_str = format!("{}", max_risk_pct * Decimal::from(100));
        decision.skip_reason = format!(
            "RISK_LIMIT: estimated total risk {} > {}% of wallet = {} USDT",
            py_format_fixed(&risk.estimated_total_risk, 4),
            pct_str,
            py_format_fixed(&risk_limit, 4)
        );
        return Ok(decision);
    }
    let margin_cap = available * d_value(&cfg.max_margin_usage_pct)?;
    if estimated_margin > margin_cap {
        decision.skip_reason = format!(
            "INSUFFICIENT_MARGIN: needs {} > usable {} USDT",
            py_format_fixed(&estimated_margin, 4),
            py_format_fixed(&margin_cap, 4)
        );
        return Ok(decision);
    }
    decision.accepted = true;
    Ok(decision)
}
