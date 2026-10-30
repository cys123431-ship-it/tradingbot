use std::io::Write;
use std::sync::Arc;

use tbot::app::engine::Engine;

#[tokio::main]
async fn main() {
    let engine = match Engine::from_env() {
        Ok(e) => Arc::new(e),
        Err(e) => {
            log_line(&format!("engine: startup failed: {e}"));
            std::process::exit(1);
        }
    };
    if let Err(e) = engine.run().await {
        log_line(&format!("engine: run failed: {e}"));
        std::process::exit(1);
    }
}

fn log_line(msg: &str) {
    let line = format!(
        "{} {msg}\n",
        chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, false)
    );
    let _ = std::io::stderr().write_all(line.as_bytes());
}
