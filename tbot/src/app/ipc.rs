//! Local engine control API: newline-delimited JSON over TCP on 127.0.0.1.
//! Authentication uses the runtime-directory token file.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use serde_json::{json, Map, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::{TcpListener, TcpStream};
use tokio::time::{timeout, Duration};

const MAX_LINE: usize = 1024 * 1024;
const DEFAULT_PORT: u16 = 47321;
const DEFAULT_TIMEOUT_SECS: u64 = 15;
pub const TOKEN_FILE: &str = "tbot_ipc.token";

pub type BoxFut<T> = Pin<Box<dyn Future<Output = T> + Send>>;
pub type Handler = Arc<dyn Fn(Request) -> BoxFut<Result<Value, String>> + Send + Sync>;

/// Uniform façade over the engine used by the Telegram process and tests.
pub trait EngineApi: Send + Sync {
    fn call(&self, cmd: &str, strategy: Option<&str>, args: Value)
        -> BoxFut<Result<Value, String>>;
}

#[derive(Debug, Clone)]
pub struct Request {
    pub cmd: String,
    pub strategy: Option<String>,
    pub args: Value,
}

impl Request {
    pub fn arg(&self, key: &str) -> Option<&Value> {
        self.args.as_object().and_then(|m| m.get(key))
    }

    pub fn arg_str(&self, key: &str) -> Option<String> {
        self.arg(key).and_then(|v| match v {
            Value::String(s) => Some(s.clone()),
            Value::Null => None,
            other => Some(other.to_string()),
        })
    }

    pub fn arg_map(&self, key: &str) -> Map<String, Value> {
        self.arg(key)
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default()
    }
}

pub fn ipc_port() -> u16 {
    std::env::var("TBOT_IPC_PORT")
        .ok()
        .and_then(|s| s.trim().parse::<u16>().ok())
        .unwrap_or(DEFAULT_PORT)
}

pub fn runtime_dir() -> std::path::PathBuf {
    let raw = std::env::var("TBOT_RUNTIME_DIR").unwrap_or_else(|_| "../runtime".to_string());
    std::path::PathBuf::from(raw)
}

pub fn token_path(runtime: &std::path::Path) -> std::path::PathBuf {
    runtime.join(TOKEN_FILE)
}

pub fn generate_token() -> String {
    let mut bytes = [0u8; 32];
    getrandom(&mut bytes);
    let mut out = String::with_capacity(64);
    for b in bytes.iter() {
        out.push_str(&format!("{b:02x}"));
    }
    out
}

fn getrandom(buf: &mut [u8]) {
    // Use `std::time` + pid mixing to avoid a hard dependency; still 256 bits
    // of entropy via nanos + hashing.
    use std::time::{SystemTime, UNIX_EPOCH};
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let pid = std::process::id() as u128;
    let mut seed = now
        .wrapping_mul(0x9e37_79b9_7f4a_7c15)
        .wrapping_add(pid.wrapping_mul(0xbf58_476d_1ce4_e5b9));
    for slot in buf.iter_mut() {
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        *slot = (seed & 0xff) as u8;
    }
}

pub fn ensure_token(runtime: &std::path::Path) -> Result<String, String> {
    std::fs::create_dir_all(runtime).map_err(|e| format!("OSError: {e}"))?;
    let path = token_path(runtime);
    if let Ok(text) = std::fs::read_to_string(&path) {
        let trimmed = text.trim().to_string();
        if !trimmed.is_empty() {
            return Ok(trimmed);
        }
    }
    let token = generate_token();
    let tmp = path.with_extension("token.tmp");
    {
        use std::io::Write as _;
        let mut f = std::fs::File::create(&tmp).map_err(|e| format!("OSError: {e}"))?;
        f.write_all(token.as_bytes())
            .map_err(|e| format!("OSError: {e}"))?;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600));
    }
    std::fs::rename(&tmp, &path).map_err(|e| format!("OSError: {e}"))?;
    Ok(token)
}

#[derive(Clone)]
pub struct IpcClient {
    addr: String,
    token: String,
}

impl IpcClient {
    pub fn new(addr: String, token: String) -> Self {
        Self { addr, token }
    }

    pub fn localhost(port: u16, token: String) -> Self {
        Self::new(format!("127.0.0.1:{port}"), token)
    }

    pub async fn call(
        &self,
        cmd: &str,
        strategy: Option<&str>,
        args: Value,
    ) -> Result<Value, String> {
        let mut req = Map::new();
        req.insert("token".into(), Value::String(self.token.clone()));
        req.insert("cmd".into(), Value::String(cmd.to_string()));
        if let Some(s) = strategy {
            req.insert("strategy".into(), Value::String(s.to_string()));
        }
        req.insert("args".into(), args);
        let line =
            serde_json::to_string(&Value::Object(req)).map_err(|e| format!("ValueError: {e}"))?;
        let stream = timeout(
            Duration::from_secs(DEFAULT_TIMEOUT_SECS),
            TcpStream::connect(&self.addr),
        )
        .await
        .map_err(|_| format!("ENGINE_UNREACHABLE: connect to {} timed out", self.addr))?
        .map_err(|e| format!("ENGINE_UNREACHABLE: {e}"))?;
        let (read_half, mut write_half) = stream.into_split();
        timeout(Duration::from_secs(DEFAULT_TIMEOUT_SECS), async {
            write_half
                .write_all(line.as_bytes())
                .await
                .map_err(|e| format!("ENGINE_UNREACHABLE: {e}"))?;
            write_half
                .write_all(b"\n")
                .await
                .map_err(|e| format!("ENGINE_UNREACHABLE: {e}"))?;
            write_half
                .flush()
                .await
                .map_err(|e| format!("ENGINE_UNREACHABLE: {e}"))?;
            let mut reader = BufReader::new(read_half);
            let mut buf = String::new();
            reader
                .read_line(&mut buf)
                .await
                .map_err(|e| format!("ENGINE_UNREACHABLE: {e}"))?;
            if buf.is_empty() {
                return Err("ENGINE_UNREACHABLE: empty response".to_string());
            }
            let resp: Value =
                serde_json::from_str(buf.trim_end()).map_err(|e| format!("ValueError: {e}"))?;
            if resp.get("ok").and_then(|v| v.as_bool()).unwrap_or(false) {
                Ok(resp.get("result").cloned().unwrap_or(Value::Null))
            } else {
                Err(resp
                    .get("error")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown error")
                    .to_string())
            }
        })
        .await
        .map_err(|_| format!("ENGINE_UNREACHABLE: request to {} timed out", self.addr))?
    }
}

impl EngineApi for IpcClient {
    fn call(
        &self,
        cmd: &str,
        strategy: Option<&str>,
        args: Value,
    ) -> BoxFut<Result<Value, String>> {
        let cmd = cmd.to_string();
        let strategy = strategy.map(|s| s.to_string());
        // Wrap the inherent async method in a boxed future.
        let client = self.clone();
        Box::pin(async move { IpcClient::call(&client, &cmd, strategy.as_deref(), args).await })
    }
}

/// Serve control requests. Each connection is handled in its own task;
/// multiple requests can be sent sequentially on the same connection.
pub async fn serve(listener: TcpListener, token: String, handler: Handler) {
    loop {
        let accepted = listener.accept().await;
        let (stream, _peer) = match accepted {
            Ok(pair) => pair,
            Err(_e) => continue, // python: ignored on purpose
        };
        let handler = handler.clone();
        let token = token.clone();
        tokio::spawn(async move {
            if let Err(_e) = handle_connection(stream, token, handler).await {
                // python: ignored on purpose
            }
        });
    }
}

async fn handle_connection(
    stream: TcpStream,
    token: String,
    handler: Handler,
) -> Result<(), String> {
    let (read_half, mut write_half) = stream.into_split();
    let mut reader = BufReader::new(read_half);
    let mut buf = String::new();
    loop {
        buf.clear();
        let n = reader
            .read_line(&mut buf)
            .await
            .map_err(|e| format!("IOError: {e}"))?;
        if n == 0 {
            return Ok(());
        }
        if buf.len() > MAX_LINE {
            let resp = json!({"ok": false, "error": "ValueError: line too long"}).to_string();
            let _ = write_half.write_all(resp.as_bytes()).await;
            let _ = write_half.write_all(b"\n").await;
            return Ok(());
        }
        let line = buf.trim_end_matches(['\r', '\n']).to_string();
        if line.is_empty() {
            continue;
        }
        let reply = dispatch(&line, &token, &handler).await;
        let payload = serde_json::to_string(&reply)
            .unwrap_or_else(|_| "{\"ok\":false,\"error\":\"ValueError: serialize\"}".to_string());
        if write_half.write_all(payload.as_bytes()).await.is_err() {
            return Ok(());
        }
        if write_half.write_all(b"\n").await.is_err() {
            return Ok(());
        }
    }
}

async fn dispatch(line: &str, token: &str, handler: &Handler) -> Value {
    let parsed: Value = match serde_json::from_str(line) {
        Ok(v) => v,
        Err(e) => return json!({"ok": false, "error": format!("ValueError: {e}")}),
    };
    let map = match parsed.as_object() {
        Some(m) => m,
        None => {
            return json!({"ok": false, "error": "ValueError: request must be an object"});
        }
    };
    let req_token = map.get("token").and_then(|v| v.as_str()).unwrap_or("");
    if !constant_time_eq(req_token, token) {
        return json!({"ok": false, "error": "unauthorized"});
    }
    let cmd = map
        .get("cmd")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    if cmd.is_empty() {
        return json!({"ok": false, "error": "ValueError: missing cmd"});
    }
    let strategy = map
        .get("strategy")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());
    let args = map
        .get("args")
        .cloned()
        .unwrap_or(Value::Object(Map::new()));
    let req = Request {
        cmd,
        strategy,
        args,
    };
    match (handler)(req).await {
        Ok(result) => json!({"ok": true, "result": result}),
        Err(err) => json!({"ok": false, "error": err}),
    }
}

fn constant_time_eq(a: &str, b: &str) -> bool {
    let x = a.as_bytes();
    let y = b.as_bytes();
    if x.len() != y.len() {
        return false;
    }
    let mut acc = 0u8;
    for (p, q) in x.iter().zip(y.iter()) {
        acc |= p ^ q;
    }
    acc == 0
}
