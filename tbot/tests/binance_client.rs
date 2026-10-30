use std::collections::HashMap;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

use tbot::exchange::binance::{BinanceClient, BinanceError, Credentials, NewAlgoOrder, NewOrder};

// ---------------------------------------------------------------------------
// Mock HTTP/1.1 server
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
struct Recorded {
    method: String,
    target: String,
    headers: HashMap<String, String>,
}

/// (status, headers, body)
type MockResponse = (u16, Vec<(String, String)>, String);

struct MockServer {
    base_url: String,
    requests: Arc<Mutex<Vec<Recorded>>>,
    counter: Arc<AtomicUsize>,
}

impl MockServer {
    async fn new(responses: Vec<MockResponse>) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let base_url = format!("http://{}", addr);
        let requests: Arc<Mutex<Vec<Recorded>>> = Arc::new(Mutex::new(Vec::new()));
        let counter = Arc::new(AtomicUsize::new(0));
        let requests_task = requests.clone();
        let counter_task = counter.clone();
        tokio::spawn(async move {
            loop {
                let (mut socket, _) = match listener.accept().await {
                    Ok(v) => v,
                    Err(_) => break,
                };
                let responses = responses.clone();
                let requests = requests_task.clone();
                let counter = counter_task.clone();
                tokio::spawn(async move {
                    let mut buf = vec![0u8; 64 * 1024];
                    let n = match socket.read(&mut buf).await {
                        Ok(n) => n,
                        Err(_) => return,
                    };
                    if n == 0 {
                        return;
                    }
                    let text = String::from_utf8_lossy(&buf[..n]).to_string();
                    let mut lines = text.split("\r\n");
                    let request_line = lines.next().unwrap_or("");
                    let mut parts = request_line.split_whitespace();
                    let method = parts.next().unwrap_or("").to_string();
                    let target = parts.next().unwrap_or("").to_string();
                    let mut headers = HashMap::new();
                    for line in lines {
                        if line.is_empty() {
                            break;
                        }
                        if let Some((k, v)) = line.split_once(':') {
                            headers.insert(k.trim().to_lowercase(), v.trim().to_string());
                        }
                    }
                    requests.lock().unwrap().push(Recorded {
                        method,
                        target,
                        headers,
                    });
                    let idx = counter.fetch_add(1, Ordering::SeqCst);
                    let (status, extra_headers, body) = responses
                        .get(idx)
                        .cloned()
                        .unwrap_or_else(|| (500, Vec::new(), "no more responses".to_string()));
                    let reason = match status {
                        200 => "OK",
                        400 => "Bad Request",
                        404 => "Not Found",
                        418 => "I'm a teapot",
                        429 => "Too Many Requests",
                        500 => "Internal Server Error",
                        503 => "Service Unavailable",
                        _ => "Unknown",
                    };
                    let mut response = format!(
                        "HTTP/1.1 {} {}\r\nContent-Length: {}\r\nConnection: close\r\n",
                        status,
                        reason,
                        body.len()
                    );
                    for (k, v) in extra_headers {
                        response.push_str(&format!("{}: {}\r\n", k, v));
                    }
                    response.push_str("\r\n");
                    response.push_str(&body);
                    let _ = socket.write_all(response.as_bytes()).await;
                    let _ = socket.shutdown().await;
                });
            }
        });
        MockServer {
            base_url,
            requests,
            counter,
        }
    }

    fn take(&self) -> Vec<Recorded> {
        self.requests.lock().unwrap().clone()
    }

    #[allow(dead_code)]
    fn count(&self) -> usize {
        self.counter.load(Ordering::SeqCst)
    }
}

fn ok_json(body: &str) -> (u16, Vec<(String, String)>, String) {
    (200, Vec::new(), body.to_string())
}

fn creds() -> Option<Credentials> {
    Some(Credentials {
        api_key: "TESTAPIKEYabcdef".to_string(),
        secret: "TESTSECRETvalue".to_string(),
    })
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[test]
fn signature_vector() {
    let secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j";
    let query = "symbol=BTCUSDT&side=BUY&type=LIMIT&quantity=1&price=9000&timeInForce=GTC&recvWindow=5000&timestamp=1591702613943";
    let sig = BinanceClient::sign(secret, query);
    assert_eq!(
        sig,
        "b42d282ee714b37b4e0e27a4d58d2185578e76437385d7705e15d3eef5a12a80"
    );
}

#[test]
fn debug_does_not_leak_secret() {
    let c = Credentials {
        api_key: "ABCDEFGHIJKLMNOP".to_string(),
        secret: "SUPERSECRET".to_string(),
    };
    let text = format!("{:?}", c);
    assert!(!text.contains("SUPERSECRET"));
    assert!(!text.contains("ABCDEFGHIJKLMNOP"));
    assert!(text.contains("ABCD"));
}

#[tokio::test]
async fn klines_parse() {
    let body = r#"[[1591702613943,"100.5","110.0","99.0","105.5","1234.5",1591702614943,"0",0,"0","0","0"],[1591702614943,"105.5","111.0","100.0","108.0","500.0",1591702615943,"0",0,"0","0","0"]]"#;
    let server = MockServer::new(vec![ok_json(body)]).await;
    let client = BinanceClient::with_base_url(&server.base_url, None);
    let candles = client.klines("BTCUSDT", "1m", 2).await.unwrap();
    assert_eq!(candles.len(), 2);
    assert!((candles[0][1] - 100.5).abs() < 1e-9);
    assert!((candles[0][0] - 1591702613943.0).abs() < 1e-9);
    assert!((candles[0][4] - 105.5).abs() < 1e-9);
    assert!((candles[1][5] - 500.0).abs() < 1e-9);
}

#[tokio::test]
async fn signed_request_has_api_key_and_signature() {
    let body = r#"{"ok":true}"#;
    let server = MockServer::new(vec![ok_json(body)]).await;
    let client = BinanceClient::with_base_url(&server.base_url, creds());
    client.balance().await.unwrap();
    let reqs = server.take();
    assert_eq!(reqs.len(), 1);
    let r = &reqs[0];
    assert_eq!(r.method, "GET");
    assert!(r.headers.contains_key("x-mbx-apikey"));
    assert_eq!(r.headers.get("x-mbx-apikey").unwrap(), "TESTAPIKEYabcdef");
    let target = &r.target;
    let query = target.split_once('?').map(|(_, q)| q).unwrap_or("");
    let tail_start = query.rfind("recvWindow=").unwrap();
    let tail = &query[tail_start..];
    // recvWindow=5000&timestamp=<digits>&signature=<64 hex>
    let mut segs = tail.split('&');
    let recv = segs.next().unwrap();
    let ts = segs.next().unwrap();
    let sig = segs.next().unwrap();
    assert_eq!(recv, "recvWindow=5000");
    let ts_val = ts.strip_prefix("timestamp=").unwrap();
    assert!(ts_val.parse::<i64>().is_ok());
    let sig_val = sig.strip_prefix("signature=").unwrap();
    assert_eq!(sig_val.len(), 64);
    assert!(sig_val.chars().all(|c| c.is_ascii_hexdigit()));
}

#[tokio::test]
async fn api_error_400() {
    let body = r#"{"code":-2019,"msg":"Margin is insufficient."}"#;
    let server = MockServer::new(vec![(400, Vec::new(), body.to_string())]).await;
    let client = BinanceClient::with_base_url(&server.base_url, creds());
    let err = client.balance().await.unwrap_err();
    match err {
        BinanceError::Api { status, code, .. } => {
            assert_eq!(status, 400);
            assert_eq!(code, -2019);
        }
        other => panic!("unexpected: {other:?}"),
    }
    assert!(!BinanceError::Api {
        status: 400,
        code: -2019,
        msg: "x".to_string()
    }
    .outcome_unknown());
}

#[tokio::test]
async fn http_503_outcome_unknown() {
    let server = MockServer::new(vec![(503, Vec::new(), "boom".to_string())]).await;
    let client = BinanceClient::with_base_url(&server.base_url, creds());
    let err = client.balance().await.unwrap_err();
    assert!(matches!(err, BinanceError::Http { status: 503, .. }));
    assert!(err.outcome_unknown());
}

#[tokio::test]
async fn rate_limited_429() {
    let headers = vec![("Retry-After".to_string(), "7".to_string())];
    let server = MockServer::new(vec![(429, headers, "".to_string())]).await;
    let client = BinanceClient::with_base_url(&server.base_url, creds());
    let err = client.balance().await.unwrap_err();
    match err {
        BinanceError::RateLimited {
            status,
            retry_after_secs,
        } => {
            assert_eq!(status, 429);
            assert_eq!(retry_after_secs, Some(7));
        }
        other => panic!("unexpected: {other:?}"),
    }
}

#[tokio::test]
async fn get_minus_1021_resyncs_and_retries() {
    let err_body =
        r#"{"code":-1021,"msg":"Timestamp for this request is outside of the recvWindow."}"#;
    let server = MockServer::new(vec![
        (400, Vec::new(), err_body.to_string()),
        ok_json(r#"{"serverTime":1700000000000}"#),
        ok_json(r#"{"ok":true}"#),
    ])
    .await;
    let client = BinanceClient::with_base_url(&server.base_url, creds());
    client.balance().await.unwrap();
    let reqs = server.take();
    assert_eq!(reqs.len(), 3);
    assert!(reqs[0].target.starts_with("/fapi/v3/balance"));
    assert!(reqs[1].target.starts_with("/fapi/v1/time"));
    assert!(reqs[2].target.starts_with("/fapi/v3/balance"));
}

#[tokio::test]
async fn post_minus_1021_does_not_retry() {
    let err_body =
        r#"{"code":-1021,"msg":"Timestamp for this request is outside of the recvWindow."}"#;
    let server = MockServer::new(vec![
        (400, Vec::new(), err_body.to_string()),
        ok_json(r#"{"serverTime":1700000000000}"#),
    ])
    .await;
    let client = BinanceClient::with_base_url(&server.base_url, creds());
    let order = NewOrder {
        symbol: "BTCUSDT".to_string(),
        side: "BUY".to_string(),
        order_type: "MARKET".to_string(),
        quantity: Some("0.001".to_string()),
        price: None,
        time_in_force: None,
        reduce_only: false,
        position_side: Some("LONG".to_string()),
        new_client_order_id: "abc123".to_string(),
        new_order_resp_type: Some("RESULT".to_string()),
    };
    let err = client.new_order(&order).await.unwrap_err();
    match err {
        BinanceError::Api { code, .. } => assert_eq!(code, -1021),
        other => panic!("unexpected: {other:?}"),
    }
    let reqs = server.take();
    // order request + time request
    assert_eq!(reqs.len(), 2);
    assert_eq!(
        reqs.iter()
            .filter(|r| r.target.starts_with("/fapi/v1/order"))
            .count(),
        1
    );
    assert_eq!(
        reqs.iter()
            .filter(|r| r.target.starts_with("/fapi/v1/time"))
            .count(),
        1
    );
}

#[tokio::test]
async fn algo_order_parameter_order() {
    let server = MockServer::new(vec![ok_json(r#"{"algoId":1}"#)]).await;
    let client = BinanceClient::with_base_url(&server.base_url, creds());
    let algo = NewAlgoOrder {
        symbol: "BTCUSDT".to_string(),
        side: "buy".to_string(),
        order_type: "STOP_MARKET".to_string(),
        quantity: Some("0.01".to_string()),
        trigger_price: "95000".to_string(),
        client_algo_id: "0123456789012345678901234567890123456789".to_string(),
        reduce_only: true,
        working_type: "mark_price".to_string(),
        price_protect: false,
        position_side: Some("long".to_string()),
        close_position: false,
        price: None,
    };
    client.new_algo_order(&algo).await.unwrap();
    let reqs = server.take();
    assert_eq!(reqs.len(), 1);
    let target = &reqs[0].target;
    let query = target.split_once('?').map(|(_, q)| q).unwrap_or("");
    // Order: algoType,symbol,side,type,triggerPrice,clientAlgoId,workingType,priceProtect,quantity,reduceOnly,positionSide,signature fields
    let keys: Vec<&str> = query
        .split('&')
        .map(|kv| kv.split_once('=').map(|(k, _)| k).unwrap_or(kv))
        .collect();
    assert_eq!(
        &keys[0..11],
        &[
            "algoType",
            "symbol",
            "side",
            "type",
            "triggerPrice",
            "clientAlgoId",
            "workingType",
            "priceProtect",
            "quantity",
            "reduceOnly",
            "positionSide",
        ]
    );
    // clientAlgoId truncated to 36 chars
    let caid = query
        .split('&')
        .find_map(|kv| kv.strip_prefix("clientAlgoId="))
        .unwrap();
    assert_eq!(caid.len(), 36);
}

#[tokio::test]
async fn algo_order_validation_error_no_request() {
    let server = MockServer::new(vec![]).await;
    let client = BinanceClient::with_base_url(&server.base_url, creds());
    let algo = NewAlgoOrder {
        symbol: "BTCUSDT".to_string(),
        side: "BUY".to_string(),
        order_type: "STOP_MARKET".to_string(),
        quantity: Some("0.01".to_string()),
        trigger_price: "95000".to_string(),
        client_algo_id: "abc".to_string(),
        reduce_only: true,
        working_type: "MARK_PRICE".to_string(),
        price_protect: false,
        position_side: None,
        close_position: true,
        price: Some("90000".to_string()),
    };
    let err = client.new_algo_order(&algo).await.unwrap_err();
    match err {
        BinanceError::Invalid(msg) => {
            assert_eq!(
                msg,
                "closePosition=true market triggers must not include price"
            );
        }
        other => panic!("unexpected: {other:?}"),
    }
    assert_eq!(server.take().len(), 0);
}

#[tokio::test]
async fn algo_order_unsupported_type() {
    let server = MockServer::new(vec![]).await;
    let client = BinanceClient::with_base_url(&server.base_url, creds());
    let algo = NewAlgoOrder {
        symbol: "BTCUSDT".to_string(),
        side: "BUY".to_string(),
        order_type: "LIMIT".to_string(),
        quantity: Some("0.01".to_string()),
        trigger_price: "95000".to_string(),
        client_algo_id: "abc".to_string(),
        reduce_only: true,
        working_type: "MARK_PRICE".to_string(),
        price_protect: false,
        position_side: None,
        close_position: false,
        price: None,
    };
    let err = client.new_algo_order(&algo).await.unwrap_err();
    match err {
        BinanceError::Invalid(msg) => {
            assert_eq!(msg, "unsupported Binance conditional order type: LIMIT");
        }
        other => panic!("unexpected: {other:?}"),
    }
    assert_eq!(server.take().len(), 0);
}

#[tokio::test]
async fn set_margin_type_minus_4046_is_ok() {
    let body = r#"{"code":-4046,"msg":"No need to change margin type."}"#;
    let server = MockServer::new(vec![(400, Vec::new(), body.to_string())]).await;
    let client = BinanceClient::with_base_url(&server.base_url, creds());
    let v = client.set_margin_type("BTCUSDT", "ISOLATED").await.unwrap();
    assert_eq!(v.get("code").and_then(|c| c.as_i64()), Some(-4046));
}

#[tokio::test]
async fn missing_credentials_no_request() {
    let server = MockServer::new(vec![]).await;
    let client = BinanceClient::with_base_url(&server.base_url, None);
    let err = client.balance().await.unwrap_err();
    assert!(matches!(err, BinanceError::MissingCredentials));
    assert_eq!(server.take().len(), 0);
}
