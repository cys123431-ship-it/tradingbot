use serde_json::{json, Map, Value};

use tbot::exchange::binance::BinanceError;
use tbot::exchange::binance_futures::{amount_to_precision, price_to_precision};
use tbot::exchange::ccxt_shape::Markets;
use tbot::exchange::errors::ExchangeError;
use tbot::exchange::facade::FuturesExchange;
use tbot::exchange::fake;

fn api(code: i64, msg: &str) -> BinanceError {
    BinanceError::Api {
        status: 400,
        code,
        msg: msg.to_string(),
    }
}

#[test]
fn mapping_codes() {
    let e = ExchangeError::from_binance(&api(-2013, "Order does not exist."));
    assert_eq!(e.class, "OrderNotFound");
    assert_eq!(
        e.message,
        r#"binance {"code":-2013,"msg":"Order does not exist."}"#
    );

    assert_eq!(
        ExchangeError::from_binance(&api(-2019, "x")).class,
        "InsufficientFunds"
    );
    assert_eq!(
        ExchangeError::from_binance(&api(-2021, "x")).class,
        "OrderImmediatelyFillable"
    );
    assert_eq!(
        ExchangeError::from_binance(&api(-1021, "x")).class,
        "InvalidNonce"
    );

    let e2015 = ExchangeError::from_binance(&api(-2015, "Invalid API-key"));
    // Conservative: the mapped AuthenticationError (ccxt only says DDoS after a login).
    assert_eq!(e2015.class, "AuthenticationError");

    let e_m = ExchangeError::from_binance(&api(-2011, "Unknown order sent."));
    assert_eq!(e_m.class, "OrderNotFound");
}

#[test]
fn mapping_rate_and_network() {
    let r = ExchangeError::from_binance(&BinanceError::RateLimited {
        status: 429,
        retry_after_secs: None,
    });
    assert_eq!(r.class, "DDoSProtection");
    assert_eq!(r.message, "binance 429 ");

    let net = ExchangeError::from_binance(&BinanceError::Network {
        message: "oops".to_string(),
        sent: true,
    });
    assert_eq!(net.class, "RequestTimeout");
    assert!(net.outcome_unknown);

    let net2 = ExchangeError::from_binance(&BinanceError::Network {
        message: "oops".to_string(),
        sent: false,
    });
    assert_eq!(net2.class, "NetworkError");
    assert!(!net2.outcome_unknown);
}

fn fake_with_trend() -> fake::FakeBinance {
    fake::FakeBinance::new(fake::FakeOptions {
        h1: fake::trend_1h_default(),
        m15: fake::long_setup_15m_default(),
        m1: Vec::new(),
        ..Default::default()
    })
}

#[tokio::test]
async fn fake_scenario() {
    let fake = fake_with_trend();

    let mut params: Map<String, Value> = Map::new();
    params.insert("newClientOrderId".into(), json!("cid-1"));
    let order = fake
        .create_order("BTC/USDT:USDT", "MARKET", "buy", 0.1, None, params)
        .await
        .unwrap();
    assert_eq!(order.get("status").and_then(|v| v.as_str()), Some("closed"));

    let positions = fake.fetch_positions(None).await.unwrap();
    assert_eq!(positions.len(), 1);
    assert_eq!(
        positions[0].get("side").and_then(|v| v.as_str()),
        Some("long")
    );

    let mut algo: Map<String, Value> = Map::new();
    algo.insert("type".into(), json!("STOP_MARKET"));
    algo.insert("side".into(), json!("SELL"));
    algo.insert("triggerPrice".into(), json!(79000.0));
    algo.insert("clientAlgoId".into(), json!("algo-1"));
    algo.insert("closePosition".into(), json!(true));
    fake.algo_post_raw(algo.clone()).await.unwrap();
    let open = fake.algo_open_raw().await.unwrap();
    assert_eq!(open.as_array().unwrap().len(), 1);

    fake.trigger("STOP_MARKET", 79_000.0);
    let positions = fake.fetch_positions(None).await.unwrap();
    assert!(positions.is_empty());
    let trades = fake.user_trades_raw("BTCUSDT", None, 100).await.unwrap();
    assert_eq!(trades.as_array().unwrap().len(), 2);

    let err = fake.get_order_raw("BTCUSDT", "missing").await.unwrap_err();
    assert_eq!(err.class, "Exception");
    assert!(err.message.contains("-2013"));

    {
        let mut state = fake.state.lock().unwrap();
        state.breach_sl = true;
    }
    let err = fake.algo_post_raw(algo).await.unwrap_err();
    assert!(err.message.contains("-2021"));
}

fn load_sample_info() -> Value {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join("binance_public_sample.json");
    let text = std::fs::read_to_string(&path).expect("read fixture");
    serde_json::from_str(&text).expect("parse fixture")
}

#[test]
fn precision_floor_and_round() {
    let info = load_sample_info();
    let markets = Markets::from_exchange_info(&info["exchangeInfo"]);
    let sol = markets.market("SOL/USDT:USDT").expect("sol market").clone();
    assert_eq!(amount_to_precision(&sol, 1.239), "1.23");
    let btc = markets.market("BTC/USDT:USDT").expect("btc market").clone();
    assert_eq!(amount_to_precision(&btc, 0.0019), "0.001");

    // Price rounds to the 0.10 tick (BTC) / 0.01 tick (SOL).
    assert_eq!(price_to_precision(&btc, 85_123.456_7), "85123.5");
    assert_eq!(price_to_precision(&sol, 96.715), "96.72");
    assert_eq!(amount_to_precision(&sol, 2.0), "2");
}
