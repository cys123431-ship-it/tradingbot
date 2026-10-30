//! Read-only smoke test against the live Binance public API (no keys).
//! `cargo run --example public_smoke -- SOLUSDT 5m`

use tbot::core::candles::closed_candles;
use tbot::exchange::{BinanceClient, Network};

#[tokio::main]
async fn main() {
    let args: Vec<String> = std::env::args().collect();
    let symbol = args.get(1).map(String::as_str).unwrap_or("BTCUSDT");
    let interval = args.get(2).map(String::as_str).unwrap_or("5m");
    let client = BinanceClient::new(Network::Mainnet, None);

    let offset = client.sync_time().await.expect("server time");
    println!("time offset: {offset} ms");

    let rows = client.klines(symbol, interval, 250).await.expect("klines");
    let now = rows.last().map(|r| r[0] as i64).unwrap_or(0) + 10 * 60_000;
    let closed = closed_candles(&rows, interval, now);
    let last = rows.last().expect("at least one candle");
    println!(
        "{symbol} {interval}: {} candles, {} closed, last close {}",
        rows.len(),
        closed.len(),
        last[4]
    );

    let premium = client.premium_index(symbol).await.expect("premium index");
    println!("mark price: {}", premium["markPrice"]);

    let info = client.exchange_info().await.expect("exchange info");
    let count = info["symbols"].as_array().map(Vec::len).unwrap_or(0);
    println!("exchangeInfo symbols: {count}");
    facade_smoke(symbol, interval).await;
}

/// Same checks through the ccxt-shaped facade the strategies use.
async fn facade_smoke(symbol: &str, interval: &str) {
    use tbot::exchange::binance_futures::BinanceFutures;
    use tbot::exchange::FuturesExchange;

    let ex = BinanceFutures::new(BinanceClient::new(Network::Mainnet, None));
    let markets = ex.load_markets().await.expect("load_markets");
    let ccxt_symbol = markets
        .iter()
        .find(|(_, m)| m["id"] == symbol && m["swap"] == true)
        .map(|(s, _)| s.clone())
        .expect("symbol in markets");
    let rows = ex
        .fetch_ohlcv(&ccxt_symbol, interval, None, Some(5))
        .await
        .expect("fetch_ohlcv");
    let tickers = ex.fetch_tickers().await.expect("fetch_tickers");
    println!(
        "facade: {} markets, {ccxt_symbol} {} candles, quoteVolume {}",
        markets.len(),
        rows.len(),
        tickers[&ccxt_symbol]["quoteVolume"]
    );
}
