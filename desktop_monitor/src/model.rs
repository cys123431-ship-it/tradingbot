use serde::Deserialize;

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct Candle {
    pub ts: i64,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: f64,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct Position {
    pub symbol: String,
    pub side: String,
    pub contracts: f64,
    pub entry_price: f64,
    pub mark_price: f64,
    pub liquidation_price: Option<f64>,
    pub leverage: f64,
    pub margin_usdt: f64,
    pub notional_usdt: f64,
    pub unrealized_pnl: f64,
    pub roe_percent: Option<f64>,
    pub source: String,
    pub strategy: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct Order {
    pub id: String,
    pub symbol: String,
    pub side: String,
    #[serde(rename = "type")]
    pub order_type: String,
    pub price: Option<f64>,
    pub amount: Option<f64>,
    pub reduce_only: bool,
    pub source: String,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct BotState {
    pub paused: bool,
    pub exchange_mode: String,
    pub active_strategy: String,
    pub current_symbol: Option<String>,
    pub scanner_active_symbol: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct StrategyRow {
    pub key: String,
    pub name: String,
    pub state: String,
    pub side: Option<String>,
    pub reason: String,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct StatusRow {
    pub key: String,
    pub symbol: String,
    pub side: String,
    pub price: Option<f64>,
    pub entry_reason: String,
    pub equity: Option<f64>,
    pub free_usdt: Option<f64>,
    pub daily_pnl: Option<f64>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct RuntimeSnapshot {
    pub schema_version: u32,
    pub updated_at: String,
    pub epoch: i64,
    pub bot: BotState,
    pub strategies: Vec<StrategyRow>,
    pub status_rows: Vec<StatusRow>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct WireSnapshot {
    pub kind: String,
    pub ts: f64,
    pub exchange_mode: String,
    pub symbol: Option<String>,
    pub timeframe: String,
    pub position: Option<Position>,
    pub orders: Vec<Order>,
    pub runtime: Option<RuntimeSnapshot>,
    pub candles: Vec<Candle>,
    pub candle: Option<Candle>,
    pub error: Option<String>,
}

#[derive(Clone, Debug)]
pub enum StreamEvent {
    Connecting,
    Connected,
    Data(WireSnapshot),
    Error(String),
}

#[derive(Clone, Debug, Default)]
pub struct MonitorState {
    pub symbol: String,
    pub timeframe: String,
    pub exchange_mode: String,
    pub position: Option<Position>,
    pub orders: Vec<Order>,
    pub runtime: RuntimeSnapshot,
    pub candles: Vec<Candle>,
    pub last_server_ts: f64,
    pub last_error: Option<String>,
}

impl MonitorState {
    const MAX_CANDLES: usize = 300;

    pub fn apply(&mut self, snapshot: WireSnapshot) {
        if let Some(symbol) = snapshot.symbol.filter(|value| !value.is_empty()) {
            if self.symbol != symbol {
                self.candles.clear();
            }
            self.symbol = symbol;
        }
        if !snapshot.timeframe.is_empty() {
            self.timeframe = snapshot.timeframe;
        }
        if !snapshot.exchange_mode.is_empty() {
            self.exchange_mode = snapshot.exchange_mode;
        }
        self.position = snapshot.position;
        self.orders = snapshot.orders;
        if let Some(runtime) = snapshot.runtime {
            self.runtime = runtime;
        }
        if !snapshot.candles.is_empty() {
            self.candles = snapshot.candles;
        }
        if let Some(candle) = snapshot.candle {
            self.upsert_candle(candle);
        }
        if self.candles.len() > Self::MAX_CANDLES {
            self.candles
                .drain(0..self.candles.len().saturating_sub(Self::MAX_CANDLES));
        }
        self.last_server_ts = snapshot.ts;
        self.last_error = snapshot.error.filter(|value| !value.is_empty());
    }

    fn upsert_candle(&mut self, candle: Candle) {
        match self.candles.last_mut() {
            Some(last) if last.ts == candle.ts => *last = candle,
            Some(last) if last.ts < candle.ts => self.candles.push(candle),
            None => self.candles.push(candle),
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn same_timestamp_updates_live_candle_without_growth() {
        let mut state = MonitorState::default();
        state.apply(WireSnapshot {
            symbol: Some("BTC/USDT".into()),
            candles: vec![Candle {
                ts: 1,
                close: 10.0,
                ..Default::default()
            }],
            candle: Some(Candle {
                ts: 1,
                close: 11.0,
                ..Default::default()
            }),
            ..Default::default()
        });
        assert_eq!(state.candles.len(), 1);
        assert_eq!(state.candles[0].close, 11.0);
    }

    #[test]
    fn symbol_change_discards_old_chart() {
        let mut state = MonitorState {
            symbol: "BTC/USDT".into(),
            candles: vec![Candle {
                ts: 1,
                ..Default::default()
            }],
            ..Default::default()
        };
        state.apply(WireSnapshot {
            symbol: Some("ETH/USDT".into()),
            candles: vec![Candle {
                ts: 2,
                ..Default::default()
            }],
            ..Default::default()
        });
        assert_eq!(state.candles.len(), 1);
        assert_eq!(state.candles[0].ts, 2);
    }
}
