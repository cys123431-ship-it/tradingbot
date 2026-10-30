#![allow(unused, clippy::all)] // test helpers
use std::sync::Arc;
use std::time::Duration;

use serde_json::{json, Map, Value};
use tbot::app::config_file::ConfigFile;
use tbot::app::ipc::{self, BoxFut, Handler, IpcClient, Request};

fn sample_handler() -> Handler {
    Arc::new(|req: Request| -> BoxFut<Result<Value, String>> {
        Box::pin(async move {
            match req.cmd.as_str() {
                "echo" => Ok(req.args),
                "big" => {
                    let size = req.arg("size").and_then(|v| v.as_u64()).unwrap_or(1024) as usize;
                    Ok(Value::String("x".repeat(size)))
                }
                "err" => Err("ValueError: boom".to_string()),
                other => Err(format!("ValueError: unknown cmd {other}")),
            }
        })
    })
}

async fn spawn_server(token: String) -> String {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap().to_string();
    let handler = sample_handler();
    let tok = token.clone();
    tokio::spawn(ipc::serve(listener, tok, handler));
    addr
}

#[tokio::test]
async fn ipc_roundtrip_ok() {
    let token = "abc".to_string();
    let addr = spawn_server(token.clone()).await;
    let client = IpcClient::new(addr, token);
    let result = client
        .call("echo", None, json!({"hello": "world"}))
        .await
        .unwrap();
    assert_eq!(result, json!({"hello": "world"}));
}

#[tokio::test]
async fn ipc_unauthorized() {
    let addr = spawn_server("abc".to_string()).await;
    let client = IpcClient::new(addr, "wrong".to_string());
    let err = client.call("echo", None, json!({})).await.unwrap_err();
    assert_eq!(err, "unauthorized");
}

#[tokio::test]
async fn ipc_error_path() {
    let addr = spawn_server("tok".to_string()).await;
    let client = IpcClient::new(addr, "tok".to_string());
    let err = client.call("err", None, json!({})).await.unwrap_err();
    assert_eq!(err, "ValueError: boom");
}

#[tokio::test]
async fn ipc_large_response() {
    let addr = spawn_server("tok".to_string()).await;
    let client = IpcClient::new(addr, "tok".to_string());
    let result = client
        .call("big", None, json!({"size": 200_000}))
        .await
        .unwrap();
    assert_eq!(result.as_str().unwrap().len(), 200_000);
}

#[tokio::test]
async fn ipc_unreachable() {
    let client = IpcClient::new("127.0.0.1:1".to_string(), "tok".to_string());
    let err = tokio::time::timeout(Duration::from_secs(5), client.call("echo", None, json!({})))
        .await
        .unwrap()
        .unwrap_err();
    assert!(err.starts_with("ENGINE_UNREACHABLE:"));
}

#[tokio::test]
async fn config_overwrite_preserves_korean() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("config.json");
    std::fs::write(
        &path,
        r#"{"btc_ma_cross":{"enabled":true,"note":"한글 보존"},"api":{"use_testnet":true}}"#,
    )
    .unwrap();
    let cfg = ConfigFile::new(path.clone());
    let mut changes = Map::new();
    changes.insert("enabled".to_string(), Value::Bool(false));
    changes.insert("entry_mode".to_string(), Value::String("cross".into()));
    let saved = cfg.update_values("btc_ma_cross", changes).await.unwrap();
    assert_eq!(saved.get("enabled"), Some(&Value::Bool(false)));
    assert_eq!(
        saved.get("entry_mode").and_then(|v| v.as_str()),
        Some("cross")
    );
    assert_eq!(
        saved.get("note").and_then(|v| v.as_str()),
        Some("한글 보존")
    );
    let text = std::fs::read_to_string(&path).unwrap();
    assert!(text.contains("한글 보존"));
    // 4-space indent (Python json.dump shape).
    assert!(text.contains("\n    \"btc_ma_cross\": {"));
    // Lock file cleaned up.
    let lock = dir.path().join("config.json.lock");
    assert!(!lock.exists());
}

#[tokio::test]
async fn config_parse_error_keeps_last() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("config.json");
    std::fs::write(&path, r#"{"a":{"enabled":true}}"#).unwrap();
    let cfg = ConfigFile::new(path.clone());
    let first = cfg.section("a");
    assert_eq!(first.get("enabled"), Some(&Value::Bool(true)));
    // Make mtime differ and write invalid JSON.
    std::thread::sleep(Duration::from_millis(20));
    std::fs::write(&path, "{not json").unwrap();
    let second = cfg.section("a");
    assert_eq!(second.get("enabled"), Some(&Value::Bool(true)));
}

#[tokio::test]
async fn config_update_values_atomic() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("config.json");
    std::fs::write(&path, "{}").unwrap();
    let cfg = ConfigFile::new(path.clone());
    let mut changes = Map::new();
    changes.insert("api_key".to_string(), Value::String("k".into()));
    let saved = cfg.update_values("api", changes).await.unwrap();
    assert_eq!(saved.get("api_key").and_then(|v| v.as_str()), Some("k"));
    // No temp files left behind.
    for entry in std::fs::read_dir(dir.path()).unwrap() {
        let name = entry.unwrap().file_name().to_string_lossy().to_string();
        assert!(!name.ends_with(".tmp"), "leftover temp file: {name}");
    }
}
