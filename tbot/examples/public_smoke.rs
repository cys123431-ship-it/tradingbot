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
}
