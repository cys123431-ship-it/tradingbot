use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::Sender;
use std::thread;
use std::time::Duration;

use crate::model::{StreamEvent, WireSnapshot};

const DEFAULT_HOST: &str = "azure-trading-bot";
const REMOTE_COMMAND: &str = "cd /home/azureuser/tradingbot && ./venv/bin/python scripts/desktop_monitor_stream.py --interval 2 --timeframe 1m --candles 240";

fn ssh_program() -> PathBuf {
    if let Ok(windir) = std::env::var("WINDIR") {
        let candidate = PathBuf::from(windir)
            .join("System32")
            .join("OpenSSH")
            .join("ssh.exe");
        if candidate.is_file() {
            return candidate;
        }
    }
    PathBuf::from("ssh")
}

fn command(host: &str) -> Command {
    let mut command = Command::new(ssh_program());
    command.args([
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
        host,
        REMOTE_COMMAND,
    ]);
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000);
    }
    command
}

pub fn spawn(tx: Sender<StreamEvent>, stop: Arc<AtomicBool>) {
    let host = std::env::var("TRADINGBOT_SSH_HOST").unwrap_or_else(|_| DEFAULT_HOST.into());
    thread::spawn(move || {
        while !stop.load(Ordering::Relaxed) {
            let _ = tx.send(StreamEvent::Connecting);
            let mut child = match command(&host).spawn() {
                Ok(child) => child,
                Err(error) => {
                    let _ = tx.send(StreamEvent::Error(format!("SSH 실행 실패: {error}")));
                    wait_reconnect(&stop);
                    continue;
                }
            };
            let Some(stdout) = child.stdout.take() else {
                let _ = tx.send(StreamEvent::Error("SSH 출력 연결 실패".into()));
                let _ = child.kill();
                wait_reconnect(&stop);
                continue;
            };
            let _ = tx.send(StreamEvent::Connected);
            let reader = BufReader::new(stdout);
            for line in reader.lines() {
                if stop.load(Ordering::Relaxed) {
                    let _ = child.kill();
                    return;
                }
                match line {
                    Ok(line) if !line.trim().is_empty() => {
                        match serde_json::from_str::<WireSnapshot>(&line) {
                            Ok(snapshot) => {
                                if tx.send(StreamEvent::Data(snapshot)).is_err() {
                                    let _ = child.kill();
                                    return;
                                }
                            }
                            Err(error) => {
                                let _ = tx.send(StreamEvent::Error(format!(
                                    "서버 데이터 해석 실패: {error}"
                                )));
                            }
                        }
                    }
                    Ok(_) => {}
                    Err(error) => {
                        let _ = tx.send(StreamEvent::Error(format!("SSH 수신 실패: {error}")));
                        break;
                    }
                }
            }
            let _ = child.kill();
            let _ = child.wait();
            if !stop.load(Ordering::Relaxed) {
                let _ = tx.send(StreamEvent::Error("서버 연결 끊김 — 자동 재연결 중".into()));
                wait_reconnect(&stop);
            }
        }
    });
}

fn wait_reconnect(stop: &AtomicBool) {
    for _ in 0..15 {
        if stop.load(Ordering::Relaxed) {
            return;
        }
        thread::sleep(Duration::from_millis(200));
    }
}
