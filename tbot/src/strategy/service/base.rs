//! Base methods ported from `btc_ema_pullback/service.py`
//! plus `BtcMaCrossService.__init__` / `set_entry_mode`.

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex};

use chrono::{TimeZone, Utc};
use serde_json::{json, Map, Value};

use crate::core::rules::rules_from_exchange_info;
use crate::exchange::ExchangeError;
use crate::safety::algo_gateway::BinanceAlgoOrderGateway;
use crate::safety::order_gateway::IdempotentOrderGateway;
use crate::store::ledger::{
    compute_stats, daily_entry_block_reason, trading_day_bounds, StrategyLedger,
};
use crate::store::order_state::{OrderState, SQLiteTradingStateStore};
use crate::strategy::config_base::{effective_trading_mode, NETWORK_TESTNET, TRADING_MODE_LIVE};
use crate::strategy::service::{
    iso_ms, market_key, position_entry, position_qty, position_side, py_f, symbol_pair, BoxFut,
    ServiceError, SmaCrossService, IDLE, MARKET_ID, POSITION_RECONCILIATION,
};
use crate::exchange::FuturesExchange;

impl<E: FuturesExchange + 'static> SmaCrossService<E> {
    // ------------------------------------------------------------------ basics

    /// `config()` — normalized strategy config.
    pub fn config(&self) -> Value {
        let raw = (self.deps.config_getter)();
        self.variant.normalize(&raw)
    }

    /// `mode(cfg=None)`.
    pub fn mode(&self, cfg: Option<&Value>) -> String {
        let owned;
        let cfg = match cfg {
            Some(v) => v,
            None => {
                owned = self.config();
                &owned
            }
        };
        effective_trading_mode(Some(cfg), &self.deps.environ)
    }

    /// `current_network(cfg=None)`.
    pub fn current_network(&self, cfg: Option<&Value>) -> Option<String> {
        if self.deps.main_network_getter.is_none() {
            let owned;
            let cfg = match cfg {
                Some(v) => v,
                None => {
                    owned = self.config();
                    &owned
                }
            };
            return cfg
                .get("network")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string())
                .or_else(|| Some(NETWORK_TESTNET.to_string()));
        }
        self.main_network()
    }

    /// `_signal_bar(signal)`.
    pub fn signal_bar(signal: &Value) -> i64 {
        if let Some(v) = signal.get("bar_open_ms") {
            if !v.is_null() {
                return py_int_of(v);
            }
        }
        if let Some(v) = signal.get("15m_bar_open_ms") {
            if !v.is_null() {
                return py_int_of(v);
            }
        }
        0
    }

    /// `now_ms()`.
    pub fn now_ms(&self) -> i64 {
        ((self.deps.clock)() * 1000.0) as i64
    }

    /// `_notify(text, key=None)`.
    pub async fn notify(&self, text: String, key: Option<&str>) {
        if let Some(key) = key {
            {
                let mut map = self.last_notice.lock().unwrap();
                if map.get(key).map(|s| s.as_str()) == Some(text.as_str()) {
                    return;
                }
                map.insert(key.to_string(), text.clone());
            }
        }
        let Some(notifier) = self.deps.notifier.clone() else {
            return;
        };
        let fut = notifier(text);
        let res = tokio::spawn(async move { fut.await }).await;
        if res.is_err() {
            eprintln!("BTC pullback notification failed");
        }
    }

    /// `ledger(network)`.
    pub fn ledger(&self, network: &str) -> Result<Arc<Mutex<StrategyLedger>>, ServiceError> {
        {
            let map = self.ledgers.lock().unwrap();
            if let Some(l) = map.get(network) {
                return Ok(l.clone());
            }
        }
        std::fs::create_dir_all(&self.runtime_dir).map_err(|e| {
            ServiceError::new("StoreError", format!("ledger dir: {e}"))
        })?;
        let db = self.runtime_dir.join(format!("{network}_trades.sqlite3"));
        let events = self.runtime_dir.join(format!("{network}_events.jsonl"));
        let ledger = StrategyLedger::open(&db, &events, self.deps.store_clock.clone(), self.variant.strategy_version())
            .map_err(|e| ServiceError::new("StoreError", e.to_string()))?;
        let arc = Arc::new(Mutex::new(ledger));
        self.ledgers
            .lock()
            .unwrap()
            .insert(network.to_string(), arc.clone());
        Ok(arc)
    }

    /// `store(network)`.
    pub fn store(
        &self,
        network: &str,
    ) -> Result<Arc<Mutex<SQLiteTradingStateStore>>, ServiceError> {
        {
            let map = self.stores.lock().unwrap();
            if let Some(s) = map.get(network) {
                return Ok(s.clone());
            }
        }
        std::fs::create_dir_all(&self.runtime_dir).map_err(|e| {
            ServiceError::new("StoreError", format!("store dir: {e}"))
        })?;
        let path = self.runtime_dir.join(format!("{network}_orders.sqlite3"));
        let store = SQLiteTradingStateStore::open(&path, self.deps.store_clock.clone())
            .map_err(|e| ServiceError::new("StoreError", e.to_string()))?;
        let arc = Arc::new(Mutex::new(store));
        self.stores
            .lock()
            .unwrap()
            .insert(network.to_string(), arc.clone());
        Ok(arc)
    }

    /// `has_credentials(network)`.
    pub fn has_credentials(&self, network: &str) -> bool {
        let creds = (self.deps.credentials_getter)(network);
        let api = creds
            .get("api_key")
            .and_then(py_str_or_default)
            .unwrap_or_default()
            .trim()
            .to_string();
        let secret = creds
            .get("secret_key")
            .and_then(py_str_or_default)
            .unwrap_or_default()
            .trim()
            .to_string();
        !api.is_empty() && !secret.is_empty()
    }

    /// `exchange(network)`.
    pub async fn exchange(&self, network: &str) -> Result<Arc<E>, ServiceError> {
        {
            let map = self.exchanges.lock().unwrap();
            if let Some(e) = map.get(network) {
                return Ok(e.clone());
            }
        }
        let creds = (self.deps.credentials_getter)(network);
        let exchange = (self.deps.exchange_factory)(network, &creds)?;
        exchange.load_markets().await?;
        self.exchanges
            .lock()
            .unwrap()
            .insert(network.to_string(), exchange.clone());
        Ok(exchange)
    }

    /// `gateway(network)`.
    pub async fn gateway(
        &self,
        network: &str,
    ) -> Result<
        (
            Arc<IdempotentOrderGateway<E>>,
            Arc<BinanceAlgoOrderGateway<E>>,
        ),
        ServiceError,
    > {
        {
            let map = self.gateways.lock().await;
            if let Some(pair) = map.get(network) {
                return Ok(pair.clone());
            }
        }
        let exchange = self.exchange(network).await?;
        let store = self.store(network)?;
        let order_gateway = Arc::new(IdempotentOrderGateway::new(
            exchange.clone(),
            store,
            vec![0.0, 0.5, 1.0],
            "cancel_remainder".to_string(),
        ));
        let algo = Arc::new(BinanceAlgoOrderGateway::new(exchange));
        self.gateways
            .lock()
            .await
            .insert(network.to_string(), (order_gateway.clone(), algo.clone()));
        Ok((order_gateway, algo))
    }

    /// `_state_key(mode)`.
    pub fn state_key(&self, mode: &str) -> String {
        format!("state:{mode}")
    }

    /// `load_state(network, mode)`.
    pub fn load_state(&self, network: &str, mode: &str) -> Result<Map<String, Value>, ServiceError> {
        let ledger = self.ledger(network)?;
        let current = ledger
            .lock()
            .unwrap()
            .get_state(&self.state_key(mode), None)
            .filter(|v| v.is_object());
        let mut state: Map<String, Value> = match current {
            Some(Value::Object(m)) => m,
            _ => Map::new(),
        };
        state
            .entry("phase".to_string())
            .or_insert(Value::String(IDLE.to_string()));
        state
            .entry("trade".to_string())
            .or_insert(Value::Null);
        state
            .entry("processed_signals".to_string())
            .or_insert(Value::Array(Vec::new()));
        state
            .entry("last_evaluated_bar".to_string())
            .or_insert(json!(0));
        state
            .entry("reconciled".to_string())
            .or_insert(Value::Bool(false));
        state
            .entry("last_error".to_string())
            .or_insert(Value::String(String::new()));
        Ok(state)
    }

    /// `save_state(network, mode, state)`.
    pub fn save_state(
        &self,
        network: &str,
        mode: &str,
        state: &mut Map<String, Value>,
    ) -> Result<(), ServiceError> {
        // Trim processed_signals to the last 300 entries.
        let signals: Vec<Value> = state
            .get("processed_signals")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();
        let start = signals.len().saturating_sub(300);
        state.insert(
            "processed_signals".to_string(),
            Value::Array(signals[start..].to_vec()),
        );
        state.insert(
            "updated_at".to_string(),
            Value::String(Utc::now().to_rfc3339()),
        );
        let ledger = self.ledger(network)?;
        let json = Value::Object(state.clone());
        ledger
            .lock()
            .unwrap()
            .set_state(&self.state_key(mode), &json)
            .map_err(|e| ServiceError::new("StoreError", e.to_string()))?;
        Ok(())
    }

    /// `_set_phase(network, mode, state, phase, **extra)`.
    pub fn set_phase(
        &self,
        network: &str,
        mode: &str,
        state: &mut Map<String, Value>,
        phase: &str,
        extra: Map<String, Value>,
    ) -> Result<(), ServiceError> {
        let previous = state
            .get("phase")
            .cloned()
            .unwrap_or(Value::Null);
        state.insert("phase".to_string(), Value::String(phase.to_string()));
        for (k, v) in extra {
            state.insert(k, v);
        }
        self.save_state(network, mode, state)?;
        if previous.as_str() != Some(phase) {
            let mut fields = Map::new();
            fields.insert("mode".to_string(), Value::String(mode.to_string()));
            fields.insert("network".to_string(), Value::String(network.to_string()));
            fields.insert("previous".to_string(), previous);
            fields.insert("phase".to_string(), Value::String(phase.to_string()));
            let ledger = self.ledger(network)?;
            ledger.lock().unwrap().event("STATE", fields);
        }
        Ok(())
    }

    // ------------------------------------------------------------- market data

    /// `trading_rules(network, cfg, market_id=MARKET_ID)`.
    pub async fn trading_rules(
        &self,
        network: &str,
        cfg: &Value,
        market_id: &str,
    ) -> Result<crate::core::rules::TradingRules, ServiceError> {
        let key = format!("{network}:{market_id}");
        let refresh_ms = py_int_of(&cfg["rules_refresh_seconds"]) * 1000;
        let now = self.now_ms();
        {
            let map = self.rules.lock().unwrap();
            if let Some((at, cached)) = map.get(&key) {
                if now - *at < refresh_ms {
                    if let Ok(r) = serde_json::from_value::<crate::core::rules::TradingRules>(cached.clone()) {
                        return Ok(r);
                    }
                }
            }
        }
        let payload_key = format!("{network}:__exchange_info__");
        let payload: Value = {
            let map = self.rules.lock().unwrap();
            map.get(&payload_key)
                .filter(|(at, _)| now - *at < refresh_ms)
                .map(|(_, v)| v.clone())
        }
        .unwrap_or_else(|| Value::Null);
        let payload = if payload.is_null() {
            let exchange = self.exchange(network).await?;
            let fresh = exchange.exchange_info_raw().await?;
            self.rules
                .lock()
                .unwrap()
                .insert(payload_key, (self.now_ms(), fresh.clone()));
            fresh
        } else {
            payload
        };
        let rules = rules_from_exchange_info(&payload, market_id)
            .map_err(|e| ServiceError::new("TradingRulesError", format!("{e:?}")))?;
        let rules_json =
            serde_json::to_value(&rules).map_err(|e| ServiceError::new("TradingRulesError", e.to_string()))?;
        self.rules
            .lock()
            .unwrap()
            .insert(key, (self.now_ms(), rules_json));
        let mut fields = Map::new();
        fields.insert("network".to_string(), Value::String(network.to_string()));
        for (k, v) in rules.as_log() {
            fields.insert(k, v);
        }
        let ledger = self.ledger(network)?;
        ledger.lock().unwrap().event("RULES", fields);
        Ok(rules)
    }

    /// `candles(network, cfg)`.
    pub async fn candles(
        &self,
        network: &str,
        cfg: &Value,
    ) -> Result<(Vec<[f64; 6]>, Vec<[f64; 6]>), ServiceError> {
        let exchange = self.exchange(network).await?;
        let limit = py_int_of(&cfg["candle_history"]) as u32;
        let ex1 = exchange.clone();
        let ex2 = exchange.clone();
        let symbol = crate::strategy::service::CCXT_SYMBOL;
        let (trend, entry) = tokio::try_join!(
            async { ex1.fetch_ohlcv(symbol, "1h", None, Some(limit)).await },
            async { ex2.fetch_ohlcv(symbol, "15m", None, Some(limit)).await },
        )
        .map_err(|e| ServiceError::new("ValueError", format!("invalid kline response: {e}")))?;
        if !trend.is_empty() && trend.is_empty() {
            return Err(ServiceError::new("ValueError", "invalid kline response"));
        }
        Ok((trend, entry))
    }

    /// `premium_index(network, market_id=MARKET_ID)`.
    pub async fn premium_index(
        &self,
        network: &str,
        market_id: &str,
    ) -> Result<Map<String, Value>, ServiceError> {
        let exchange = self.exchange(network).await?;
        let payload = exchange.premium_index_raw(market_id).await?;
        let row = if let Some(arr) = payload.as_array() {
            arr.first().cloned().unwrap_or(Value::Null)
        } else {
            payload
        };
        let obj = row
            .as_object()
            .ok_or_else(|| ServiceError::new("ValueError", "invalid premiumIndex response"))?;
        let mark_price = py_f(obj.get("markPrice"), 0.0);
        if mark_price <= 0.0 {
            return Err(ServiceError::new("ValueError", "invalid premiumIndex response"));
        }
        let mut out = Map::new();
        out.insert("mark_price".to_string(), serde_json::json!(mark_price));
        out.insert(
            "funding_rate".to_string(),
            serde_json::json!(py_f(obj.get("lastFundingRate"), 0.0)),
        );
        out.insert(
            "next_funding_time".to_string(),
            json!(py_f(obj.get("nextFundingTime"), 0.0) as i64),
        );
        Ok(out)
    }

    /// `taker_fee_rate(network, cfg, market_id)`.
    pub async fn taker_fee_rate(
        &self,
        network: &str,
        cfg: &Value,
        market_id: &str,
    ) -> Result<(String, &'static str), ServiceError> {
        if self.has_credentials(network) {
            let attempt: Result<f64, ExchangeError> = async {
                let exchange = self.exchange(network).await?;
                let payload = exchange.commission_rate_raw(market_id).await?;
                Ok(py_f(payload.get("takerCommissionRate"), -1.0))
            }
            .await;
            match attempt {
                Ok(rate) if rate >= 0.0 => return Ok((rate.to_string(), "exchange")),
                Ok(_) => {}
                Err(exc) => eprintln!("BTC pullback commission rate unavailable: {}", exc.message),
            }
        }
        let default = cfg
            .get("estimated_taker_fee_rate")
            .map(|v| v.to_string())
            .unwrap_or_else(|| "0.0005".to_string());
        let s = default.trim_matches('"').to_string();
        Ok((s, "config"))
    }

    /// `balances(network, cfg, mode)` -> (wallet, available, equity, source).
    pub async fn balances(
        &self,
        network: &str,
        cfg: &Value,
        mode: &str,
    ) -> Result<(f64, f64, f64, &'static str), ServiceError> {
        if self.has_credentials(network) {
            let exchange = self.exchange(network).await?;
            let payload = exchange.fetch_balance().await?;
            let info = payload.get("info").cloned().unwrap_or(Value::Null);
            let mut wallet = py_f(info.get("totalWalletBalance"), 0.0);
            let mut available = py_f(info.get("availableBalance"), wallet);
            let mut equity = py_f(info.get("totalMarginBalance"), wallet);
            if wallet <= 0.0 {
                let usdt = payload.get("USDT").cloned().unwrap_or(Value::Null);
                wallet = py_f(usdt.get("total"), 0.0);
                available = py_f(usdt.get("free"), wallet);
                equity = wallet;
            }
            return Ok((wallet, available, equity, "exchange"));
        }
        if mode == TRADING_MODE_LIVE {
            return Err(ServiceError::new("RuntimeError", "API_KEYS_MISSING"));
        }
        let ledger = self.ledger(network)?;
        let closed = {
            let l = ledger.lock().unwrap();
            l.trades(Some("DRY_RUN"), Some(network), None, None, Some("CLOSED"))
                .map_err(|e| ServiceError::new("StoreError", e.to_string()))?
        };
        let mut balance = py_f(cfg.get("dry_run_balance_usdt"), 0.0);
        for t in &closed {
            balance += py_f(t.get("net_pnl"), 0.0);
        }
        Ok((balance, balance, balance, "paper"))
    }

    /// `daily_snapshot(network, mode, cfg, equity)`.
    pub fn daily_snapshot(
        &self,
        network: &str,
        mode: &str,
        cfg: &Value,
        equity: f64,
    ) -> Result<(Vec<Map<String, Value>>, f64, String), ServiceError> {
        let secs = (self.deps.clock)();
        let now_dt = Utc
            .timestamp_opt(secs as i64, 0)
            .single()
            .unwrap_or_else(Utc::now);
        let tz = cfg.get("day_timezone").and_then(|v| v.as_str()).unwrap_or("UTC");
        let (start, end, day_key) = trading_day_bounds(now_dt, tz);
        let key = format!("day_start_equity:{mode}");
        let ledger = self.ledger(network)?;
        let stored = {
            let l = ledger.lock().unwrap();
            l.get_state(&key, Some(Value::Object(Map::new())))
                .unwrap_or(Value::Object(Map::new()))
        };
        let stored_obj = stored.as_object().cloned().unwrap_or_default();
        let matches_day = stored_obj
            .get("day")
            .and_then(|v| v.as_str())
            .map(|d| d == day_key)
            .unwrap_or(false);
        let stored = if !matches_day {
            if equity > 0.0 {
                let mut map = Map::new();
                map.insert("day".to_string(), Value::String(day_key.clone()));
                map.insert("equity".to_string(), json!(equity));
                let v = Value::Object(map.clone());
                ledger
                    .lock()
                    .unwrap()
                    .set_state(&key, &v)
                    .map_err(|e| ServiceError::new("StoreError", e.to_string()))?;
                map
            } else {
                stored_obj
            }
        } else {
            stored_obj
        };
        let trades_today = {
            let l = ledger.lock().unwrap();
            l.trades(Some(mode), Some(network), Some(start), Some(end), None)
                .map_err(|e| ServiceError::new("StoreError", e.to_string()))?
        };
        let day_start_equity = py_f(stored.get("equity"), 0.0);
        Ok((trades_today, day_start_equity, day_key))
    }

    /// `status()`.
    pub async fn status(&self) -> Result<Value, ServiceError> {
        let cfg = self.config();
        let mode = self.mode(Some(&cfg));
        let network = match self.current_network(Some(&cfg)) {
            Some(n) => n,
            None => {
                let mut map = Map::new();
                map.insert("config".to_string(), cfg);
                map.insert("mode".to_string(), Value::String(mode));
                map.insert("network".to_string(), Value::Null);
                map.insert("unsupported".to_string(), Value::Bool(true));
                return Ok(Value::Object(map));
            }
        };
        let state = self.load_state(&network, &mode)?;
        let secs = (self.deps.clock)();
        let now_dt = Utc
            .timestamp_opt(secs as i64, 0)
            .single()
            .unwrap_or_else(Utc::now);
        let tz = cfg.get("day_timezone").and_then(|v| v.as_str()).unwrap_or("UTC");
        let (start, end, day_key) = trading_day_bounds(now_dt, tz);
        let ledger = self.ledger(&network)?;
        let trades_today = {
            let l = ledger.lock().unwrap();
            l.trades(Some(&mode), Some(&network), Some(start), Some(end), None)
                .map_err(|e| ServiceError::new("StoreError", e.to_string()))?
        };
        let signal_kinds: HashSet<String> = ["SIGNAL".to_string()].into_iter().collect();
        let last_signal = {
            let l = ledger.lock().unwrap();
            l.recent_events(1, Some(&signal_kinds)).into_iter().next()
        };
        let mut skip_kinds = HashSet::new();
        skip_kinds.insert("SKIP".to_string());
        skip_kinds.insert("SIGNAL".to_string());
        let last_skip = {
            let l = ledger.lock().unwrap();
            l.recent_events(1, Some(&skip_kinds)).into_iter().next()
        };
        let all_trades = {
            let l = ledger.lock().unwrap();
            l.trades(Some(&mode), Some(&network), None, None, None)
                .map_err(|e| ServiceError::new("StoreError", e.to_string()))?
        };
        let mut map = Map::new();
        map.insert("config".to_string(), cfg);
        map.insert("mode".to_string(), Value::String(mode));
        map.insert("network".to_string(), Value::String(network.clone()));
        map.insert("has_credentials".to_string(), Value::Bool(self.has_credentials(&network)));
        map.insert("state".to_string(), Value::Object(state));
        map.insert("day".to_string(), Value::String(day_key));
        map.insert(
            "trades_today".to_string(),
            Value::Array(trades_today.into_iter().map(Value::Object).collect()),
        );
        map.insert(
            "last_signal".to_string(),
            last_signal.unwrap_or(Value::Null),
        );
        map.insert("last_skip".to_string(), last_skip.unwrap_or(Value::Null));
        map.insert("stats".to_string(), Value::Object(compute_stats(&all_trades)));
        Ok(Value::Object(map))
    }

    /// `_main_network()`.
    pub fn main_network(&self) -> Option<String> {
        // The getter is infallible in Rust (Python swallowed its exceptions).
        let f = self.deps.main_network_getter.clone()?;
        f()
    }

    // -------------------------------------------------------------- main cycle

    /// `run_cycle()`.
    pub async fn run_cycle(&self) -> Result<Value, ServiceError> {
        let _guard = self.cycle_lock().lock().await;
        let cfg = self.config();
        let mode = self.mode(Some(&cfg));
        let Some(network) = self.current_network(Some(&cfg)) else {
            return Ok(json!({"action": "blocked", "reason": "EXCHANGE_MODE_UNSUPPORTED"}));
        };
        let mut state = self.load_state(&network, &mode)?;
        // Python wraps everything below in one try/except: any failure becomes
        // an "error" result recorded in state and the ledger.
        match self.run_cycle_inner(&network, &cfg, &mode, &mut state).await {
            Ok(value) => Ok(value),
            Err(exc) => {
                let msg = format!("{}: {}", exc.class, exc.message);
                eprintln!("{} cycle failed: {}", self.label(), msg);
                state.insert("last_error".to_string(), Value::String(msg.clone()));
                self.save_state(&network, &mode, &mut state)?;
                let ledger = self.ledger(&network)?;
                let mut fields = Map::new();
                fields.insert("mode".to_string(), Value::String(mode.clone()));
                fields.insert("network".to_string(), Value::String(network.clone()));
                fields.insert("error".to_string(), Value::String(msg.clone()));
                ledger.lock().unwrap().event("ERROR", fields);
                Ok(json!({"action": "error", "reason": msg}))
            }
        }
    }

    async fn run_cycle_inner(
        &self,
        network: &str,
        cfg: &Value,
        mode: &str,
        state: &mut Map<String, Value>,
    ) -> Result<Value, ServiceError> {
        if mode == TRADING_MODE_LIVE {
            if !self.has_credentials(network) {
                self.notify(
                    format!(
                        "⚠️ {} 전략: {network} API 키가 없어 LIVE를 진행할 수 없습니다.",
                        self.label()
                    ),
                    Some("no_keys"),
                )
                .await;
                return Ok(json!({"action": "blocked", "reason": "API_KEYS_MISSING"}));
            }
            if !py_truthy(state.get("reconciled")) {
                self.reconcile_live(network, cfg, state).await?;
                if !py_truthy(state.get("reconciled")) {
                    // Exchange state unknown: never open anything new.
                    return Ok(json!({"action": "blocked", "reason": "RECONCILIATION_PENDING"}));
                }
            }
            if py_truthy(state.get("trade")) {
                return self.manage_live(network, cfg, state).await;
            }
            if !py_truthy(state.get("pending_entry")) {
                self.resolve_stale_entry_records(network).await?;
            }
        } else if py_truthy(state.get("trade")) {
            return self.manage_paper(network, cfg, state).await;
        }
        if !py_truthy(cfg.get("enabled")) {
            return Ok(json!({"action": "disabled"}));
        }
        self.maybe_enter(network, cfg, mode, state).await
    }

    // --------------------------------------------------------------- LIVE: io

    /// `fetch_account_positions(network)`.
    pub async fn fetch_account_positions(
        &self,
        network: &str,
    ) -> Result<Vec<Value>, ServiceError> {
        let exchange = self.exchange(network).await?;
        let positions = exchange.fetch_positions(None).await?;
        Ok(positions
            .into_iter()
            .filter(|p| position_qty(Some(p)) > 0.0)
            .collect())
    }

    /// `fetch_position(network, symbol=CCXT_SYMBOL)`.
    pub async fn fetch_position(
        &self,
        network: &str,
        symbol: &str,
    ) -> Result<Option<Value>, ServiceError> {
        let exchange = self.exchange(network).await?;
        let positions = exchange.fetch_positions(Some(vec![symbol.to_string()])).await?;
        let want = market_key(Some(symbol));
        for row in positions {
            let row_market = row
                .get("symbol")
                .and_then(|v| v.as_str())
                .map(|s| market_key(Some(s)))
                .unwrap_or_default();
            if row_market == want && position_qty(Some(&row)) > 0.0 {
                return Ok(Some(row));
            }
        }
        Ok(None)
    }

    /// `ensure_account_setup(network, cfg, symbol=CCXT_SYMBOL)`.
    pub async fn ensure_account_setup(
        &self,
        network: &str,
        cfg: &Value,
        symbol: &str,
    ) -> Result<String, ServiceError> {
        let market = market_key(Some(symbol));
        let verified_key = format!("{network}:{market}");
        let verified_at = self
            .account_verified
            .lock()
            .unwrap()
            .get(&verified_key)
            .map(|v| py_int_of(v))
            .unwrap_or(0);
        if self.now_ms() - verified_at < 600_000 {
            return Ok(String::new());
        }
        let exchange = self.exchange(network).await?;
        match exchange.position_side_dual_raw().await {
            Ok(dual) => {
                let flag = dual
                    .get("dualSidePosition")
                    .map(|v| match v {
                        Value::String(s) => s.clone(),
                        other => other.to_string(),
                    })
                    .unwrap_or_default();
                if flag.to_lowercase() == "true" {
                    return Ok("POSITION_MODE_HEDGE: 바이낸스에서 One-way 모드로 바꾼 뒤 다시 켜세요".to_string());
                }
            }
            Err(exc) => {
                return Ok(format!("POSITION_MODE_UNKNOWN: {}", exc.message));
            }
        }
        if let Err(exc) = exchange.set_margin_mode("isolated", symbol).await {
            let text = exc.message.to_lowercase();
            if !text.contains("no need to change") && !exc.message.contains("-4046") {
                eprintln!("BTC pullback set_margin_mode: {}", exc.message);
            }
        }
        let lev = py_int_of(&cfg["leverage"]);
        if let Err(exc) = exchange.set_leverage(lev as u32, symbol).await {
            eprintln!("BTC pullback set_leverage: {}", exc.message);
        }
        let rows = match exchange.position_risk_v2_raw(&market).await {
            Ok(r) => {
                if let Some(arr) = r.as_array() {
                    arr.clone()
                } else {
                    Vec::new()
                }
            }
            Err(exc) => {
                return Ok(format!("ACCOUNT_SETUP_UNVERIFIED: {}", exc.message));
            }
        };
        let row = rows.iter().find(|r| {
            r.get("symbol")
                .and_then(|v| v.as_str())
                .map(|s| s == market)
                .unwrap_or(false)
        });
        let Some(row) = row else {
            return Ok(format!(
                "ACCOUNT_SETUP_UNVERIFIED: {market} positionRisk missing"
            ));
        };
        let leverage = py_int_of(row.get("leverage").unwrap_or(&Value::Null));
        let margin_type = row
            .get("marginType")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_lowercase();
        let want_lev = py_int_of(&cfg["leverage"]);
        if leverage != want_lev || margin_type != "isolated" {
            let shown = if margin_type.is_empty() { "?" } else { margin_type.as_str() };
            return Ok(format!(
                "ACCOUNT_SETUP_MISMATCH: leverage {leverage}x / {shown} (need {want_lev}x isolated)"
            ));
        }
        self.account_verified
            .lock()
            .unwrap()
            .insert(verified_key, json!(self.now_ms()));
        let ledger = self.ledger(network)?;
        let mut fields = Map::new();
        fields.insert("network".to_string(), Value::String(network.to_string()));
        fields.insert("symbol".to_string(), Value::String(market));
        fields.insert("leverage".to_string(), json!(leverage));
        fields.insert("margin_type".to_string(), Value::String(margin_type));
        fields.insert("position_mode".to_string(), Value::String("ONE_WAY".to_string()));
        ledger.lock().unwrap().event("ACCOUNT_SETUP", fields);
        Ok(String::new())
    }

    /// `open_algo_orders(network, market_id=Some(MARKET_ID))`. Pass `None` for all markets.
    pub async fn open_algo_orders(
        &self,
        network: &str,
        market_id: Option<&str>,
    ) -> Result<Vec<Value>, ServiceError> {
        let (_, algo) = self.gateway(network).await?;
        let snapshot = algo.fetch_open_orders().await;
        if !snapshot.ok {
            let err = snapshot.error.unwrap_or_else(|| "open algo orders unavailable".to_string());
            return Err(ServiceError::new("RuntimeError", err));
        }
        if market_id.is_none() {
            return Ok(snapshot.orders);
        }
        let want = market_id.unwrap().to_uppercase();
        Ok(snapshot
            .orders
            .into_iter()
            .filter(|o| {
                o.get("symbol")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_uppercase() == want)
                    .unwrap_or(false)
            })
            .collect())
    }

    /// `cancel_algo(network, client_algo_id)`.
    pub async fn cancel_algo(
        &self,
        network: &str,
        client_algo_id: &str,
    ) -> Result<bool, ServiceError> {
        let exchange = self.exchange(network).await?;
        match exchange.algo_delete_raw(client_algo_id).await {
            Ok(_) => Ok(true),
            Err(exc) => {
                let text = exc.message.to_lowercase();
                if text.contains("unknown order")
                    || text.contains("-2011")
                    || text.contains("-2013")
                    || text.contains("does not exist")
                {
                    Ok(false)
                } else {
                    Err(exc)
                }
            }
        }
    }

    /// `cancel_stray_protection(network, market_id=MARKET_ID)`.
    pub async fn cancel_stray_protection(
        &self,
        network: &str,
        market_id: &str,
    ) -> Result<String, ServiceError> {
        let orders = match self.open_algo_orders(network, None).await {
            Ok(o) => o,
            Err(exc) => return Ok(format!("OPEN_ORDERS_UNKNOWN: {}", exc.message)),
        };
        let client_prefix = self.client_id();
        let mut foreign = Vec::new();
        for order in &orders {
            let client_id = order
                .get("clientAlgoId")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let sym_ok = order
                .get("symbol")
                .and_then(|v| v.as_str())
                .map(|s| s.to_uppercase() == market_id)
                .unwrap_or(false);
            if !client_id.starts_with(client_prefix) && !sym_ok {
                continue;
            }
            if client_id.starts_with(client_prefix) {
                if let Err(exc) = self.cancel_algo(network, &client_id).await {
                    return Ok(format!("OPEN_ORDERS_CANCEL_FAILED: {client_id}: {}", exc.message));
                }
                let ledger = self.ledger(network)?;
                let mut fields = Map::new();
                fields.insert("network".to_string(), Value::String(network.to_string()));
                fields.insert("client_algo_id".to_string(), Value::String(client_id));
                ledger.lock().unwrap().event("CANCEL_STRAY_PROTECTION", fields);
            } else {
                let id = if client_id.is_empty() {
                    order
                        .get("algoId")
                        .map(|v| v.to_string())
                        .unwrap_or_default()
                } else {
                    client_id
                };
                foreign.push(id);
            }
        }
        if !foreign.is_empty() {
            let head: Vec<String> = foreign.into_iter().take(3).collect();
            return Ok(format!("FOREIGN_OPEN_ORDERS: {}", head.join(", ")));
        }
        Ok(String::new())
    }

    /// `resolve_stale_entry_records(network, *, min_interval_ms=60_000)`.
    pub async fn resolve_stale_entry_records(
        &self,
        network: &str,
    ) -> Result<Vec<String>, ServiceError> {
        self.resolve_stale_entry_records_with(network, 60_000).await
    }

    async fn resolve_stale_entry_records_with(
        &self,
        network: &str,
        min_interval_ms: i64,
    ) -> Result<Vec<String>, ServiceError> {
        let now = self.now_ms();
        {
            let mut map = self.last_stale_check.lock().unwrap();
            let last = *map.get(network).unwrap_or(&0);
            if now - last < min_interval_ms {
                return Ok(Vec::new());
            }
            map.insert(network.to_string(), now);
        }
        let store = self.store(network)?;
        let (order_gateway, _) = self.gateway(network).await?;
        let _ = order_gateway; // Fetched to keep parity with Python call; not awaited here yet.
        let mut resolved: Vec<String> = Vec::new();
        let mut kept: Vec<String> = Vec::new();
        // Placeholder collection of ENTRY_BLOCKING_STATES; order-state list is
        // read inside `resolve_stale_entry_records_with` body in S4.
        let blocking: Vec<&str> = crate::store::order_state::entry_blocking_states()
            .into_iter()
            .collect();
        let records = store
            .lock()
            .unwrap()
            .list_by_states(&blocking)
            .map_err(|e| ServiceError::new("StoreError", e.to_string()))?;
        for record in records {
            let order_result = crate::safety::order_gateway::IdempotentOrderGateway::<E>::fetch_order_by_client_id(
                &order_gateway,
                &record.symbol,
                &record.client_order_id,
            )
            .await;
            let position_result = self.fetch_position(network, &record.symbol).await;
            let order = match (order_result, position_result) {
                (Ok(o), Ok(p)) => (o, p),
                (Err(exc), _) | (_, Err(exc)) => {
                    kept.push(format!(
                        "{}: lookup failed ({})",
                        record.client_order_id, exc.message
                    ));
                    continue;
                }
            };
            let (order, position) = order;
            let status = order
                .as_ref()
                .and_then(|o| o.get("status"))
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_lowercase();
            let pos_open = position.is_some();
            let ord_open = matches!(status.as_str(), "open" | "new" | "partially_filled");
            if pos_open || ord_open {
                let label = if pos_open { "position" } else { "open order" };
                kept.push(format!("{}: {} on exchange", record.client_order_id, label));
                continue;
            }
            let final_state = if record.filled_qty <= 0.0 {
                OrderState::Canceled.as_str()
            } else {
                OrderState::Closed.as_str()
            };
            let prior = record.order_state.clone();
            let mut changes = Map::new();
            changes.insert(
                "last_error".to_string(),
                Value::String(format!("resolved: no open order or position (was {prior})")),
            );
            store
                .lock()
                .unwrap()
                .transition(&record.client_order_id, final_state, changes)
                .map_err(|e| ServiceError::new("StoreError", e.to_string()))?;
            store
                .lock()
                .unwrap()
                .release_entry_lease(&record.client_order_id, final_state)
                .map_err(|e| ServiceError::new("StoreError", e.to_string()))?;
            resolved.push(format!("{}:{}:{}", record.symbol, record.client_order_id, prior));
        }
        if kept.is_empty() {
            let lease = store
                .lock()
                .unwrap()
                .get_entry_lease()
                .map_err(|e| ServiceError::new("StoreError", e.to_string()))?;
            if let Some(lease) = lease {
                let symbol = lease
                    .get("symbol")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string());
                store
                    .lock()
                    .unwrap()
                    .release_entry_lease_for_symbol(symbol.as_deref().unwrap_or(""), true)
                    .map_err(|e| ServiceError::new("StoreError", e.to_string()))?;
            }
            let lock = store
                .lock()
                .unwrap()
                .get_runtime_state("entry_lock_reason", None)
                .map_err(|e| ServiceError::new("StoreError", e.to_string()))?;
            // Python: lock not in (None, "")
            let lock = lock.filter(|v| !v.is_null() && v.as_str() != Some(""));
            if let Some(lock) = lock {
                store
                    .lock()
                    .unwrap()
                    .delete_runtime_state("entry_lock_reason")
                    .map_err(|e| ServiceError::new("StoreError", e.to_string()))?;
                let text = match &lock {
                    Value::String(s) => s.clone(),
                    other => crate::store::ledger::py_str_of_value(other).unwrap_or_default(),
                };
                resolved.push(format!("runtime_lock:{text}"));
            }
        }
        if !resolved.is_empty() {
            let ledger = self.ledger(network)?;
            let mut fields = Map::new();
            fields.insert("network".to_string(), Value::String(network.to_string()));
            fields.insert(
                "resolved".to_string(),
                Value::Array(resolved.iter().map(|s| Value::String(s.clone())).collect()),
            );
            fields.insert(
                "kept".to_string(),
                Value::Array(kept.iter().map(|s| Value::String(s.clone())).collect()),
            );
            ledger.lock().unwrap().event("STALE_ORDER_RECORDS_RESOLVED", fields);
            let head: Vec<String> = resolved.iter().take(3).cloned().collect();
            let body = format!(
                "🧹 {}: 체결되지 않고 남아 있던 주문 기록을 정리했습니다 ({}건). 신규 진입 차단이 풀렸습니다.\n{}",
                self.label(),
                resolved.len(),
                head.join("\n")
            );
            self.notify(body, None).await;
        }
        Ok(resolved)
    }

    /// `needs_cycle()`.
    pub fn needs_cycle(&self) -> bool {
        let cfg = self.config();
        let Some(network) = self.current_network(Some(&cfg)) else {
            return false;
        };
        if cfg.get("enabled").and_then(|v| v.as_bool()).unwrap_or(false) {
            return true;
        }
        let Ok(state) = self.load_state(&network, &self.mode(Some(&cfg))) else {
            return false;
        };
        has_trade(&state) || has_pending(&state) || !phase_idle_or_missing(&state)
    }

    /// `reset_reconciliation()`.
    pub fn reset_reconciliation(&self) -> Result<(), ServiceError> {
        let cfg = self.config();
        if let Some(network) = self.current_network(Some(&cfg)) {
            let mode = TRADING_MODE_LIVE;
            let mut state = self.load_state(&network, mode)?;
            state.insert("reconciled".to_string(), Value::Bool(false));
            self.save_state(&network, mode, &mut state)?;
        }
        self.account_verified.lock().unwrap().clear();
        Ok(())
    }

    /// `owned_position_keys()`.
    pub fn owned_position_keys(&self) -> Result<Vec<String>, ServiceError> {
        let cfg = self.config();
        let Some(network) = self.current_network(Some(&cfg)) else {
            return Ok(Vec::new());
        };
        let state = self.load_state(&network, TRADING_MODE_LIVE)?;
        if let Some(Value::Object(trade)) = state.get("trade") {
            return Ok(vec![symbol_pair(Some(&Value::Object(trade.clone()))).1]);
        }
        if let Some(pending) = state.get("pending_entry").and_then(|v| v.as_object()) {
            let signal = pending.get("signal").cloned().unwrap_or(Value::Null);
            return Ok(vec![symbol_pair(Some(&signal)).1]);
        }
        Ok(Vec::new())
    }

    // ------------------------------------------------- btc_ma_cross overrides

    /// `set_entry_mode(entry_mode)` — persist the forced entry mode.
    pub async fn set_entry_mode(&self, entry_mode: &str) -> bool {
        let writer = match self.deps.config_writer.clone() {
            Some(w) => w,
            None => return false,
        };
        writer(entry_mode.to_string()).await
    }

    // ---------------------------------------------------------------- helpers

    pub(crate) fn set_store_clock_validated(store_clock: Arc<dyn crate::store::clock::Clock>) -> Arc<dyn crate::store::clock::Clock> {
        store_clock
    }
}

// ---------------------------------------------------------------------------
// helpers used above
// ---------------------------------------------------------------------------

fn py_int_of(v: &Value) -> i64 {
    match v {
        Value::Number(n) => n
            .as_i64()
            .or_else(|| n.as_f64().map(|f| f as i64))
            .unwrap_or(0),
        Value::Bool(b) => {
            if *b {
                1
            } else {
                0
            }
        }
        Value::String(s) => s
            .trim()
            .parse::<f64>()
            .map(|f| f as i64)
            .unwrap_or(0),
        _ => 0,
    }
}

fn py_str_or_default(v: &Value) -> Option<String> {
    match v {
        Value::Null => None,
        Value::String(s) => Some(s.clone()),
        other => Some(other.to_string()),
    }
}

fn has_trade(state: &Map<String, Value>) -> bool {
    state
        .get("trade")
        .map(|v| !v.is_null())
        .unwrap_or(false)
}

fn has_pending(state: &Map<String, Value>) -> bool {
    state
        .get("pending_entry")
        .map(|v| !v.is_null())
        .unwrap_or(false)
}

fn phase_idle_or_missing(state: &Map<String, Value>) -> bool {
    match state.get("phase") {
        None | Some(Value::Null) => true,
        Some(Value::String(s)) => s == IDLE,
        _ => false,
    }
}

// STUBS summary:
//  - maybe_enter(network,cfg,mode,state) -> Value
//  - manage_paper(network,cfg,state) -> Value
//  - manage_live(network,cfg,state) -> Value
//  - reconcile_live(network,cfg,state) -> ()
//  - verify_protection(network,cfg,state) -> ()
//  - attach_protection(network,cfg,state) -> Value
//  - pre_trade_validation(network,cfg,mode,signal) -> (decision, reason, context)
//  - enter_paper(network,cfg,state,signal,decision,context) -> Value
//  - enter_live(network,cfg,state,signal,decision,context) -> Value
//  - finalize_paper(network,cfg,state,exit_price,exit_reason,exit_ms) -> Value
//  - finalize_live(network,cfg,state,forced_reason) -> Value
//  - close_position(reason) -> Value
//  - scan_universe(network,cfg) -> Vec<(ccxt_symbol, market_id, volume)>
//  - run_pending_entry_mode_switch(network,mode,cfg,state) -> cfg

// `BoxFut` is part of the service's public surface; keep it referenced here so
// it remains dead-code-silent if the S2..S4 stubs are unused in tests.
#[allow(dead_code)]
fn _touch_box_fut(_: BoxFut<()>) {}


/// Python truthiness of an optional JSON value.
pub(crate) fn py_truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().map(|f| f != 0.0).unwrap_or(false),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}
