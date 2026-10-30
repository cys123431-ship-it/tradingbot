//! Minimal Telegram Bot API client. Token must never appear in errors/logs.

use std::time::Duration;

use serde_json::{json, Map, Value};

const MAX_TEXT_LEN: usize = 4096;

#[derive(Clone)]
pub struct TelegramApi {
    token: String,
    base: String,
    http: reqwest::Client,
}

impl TelegramApi {
    pub fn new(token: String) -> Self {
        let base = format!("https://api.telegram.org/bot{token}/");
        let http = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(40))
            .build()
            .unwrap_or_else(|_| reqwest::Client::new());
        Self { token, base, http }
    }

    pub fn token(&self) -> &str {
        &self.token
    }

    pub async fn send_message(
        &self,
        chat_id: &str,
        text: &str,
        reply_markup: Option<Value>,
    ) -> Result<Value, String> {
        let mut params = Map::new();
        params.insert("chat_id".into(), Value::String(chat_id.to_string()));
        params.insert("text".into(), Value::String(clip_text(text)));
        if let Some(m) = reply_markup {
            params.insert("reply_markup".into(), m);
        }
        self.call("sendMessage", Value::Object(params)).await
    }

    pub async fn edit_message_text(
        &self,
        chat_id: &str,
        message_id: i64,
        text: &str,
        reply_markup: Option<Value>,
    ) -> Result<Value, String> {
        let mut params = Map::new();
        params.insert("chat_id".into(), Value::String(chat_id.to_string()));
        params.insert("message_id".into(), Value::Number(message_id.into()));
        params.insert("text".into(), Value::String(clip_text(text)));
        if let Some(m) = reply_markup {
            params.insert("reply_markup".into(), m);
        }
        self.call("editMessageText", Value::Object(params)).await
    }

    pub async fn answer_callback_query(&self, id: &str) -> Result<Value, String> {
        let mut params = Map::new();
        params.insert("callback_query_id".into(), Value::String(id.to_string()));
        self.call("answerCallbackQuery", Value::Object(params))
            .await
    }

    pub async fn get_updates(
        &self,
        offset: Option<i64>,
        timeout_secs: u64,
    ) -> Result<Vec<Value>, String> {
        let mut params = Map::new();
        if let Some(o) = offset {
            params.insert("offset".into(), Value::Number(o.into()));
        }
        params.insert("timeout".into(), Value::Number(timeout_secs.into()));
        let v = self.call("getUpdates", Value::Object(params)).await?;
        Ok(v.as_array().cloned().unwrap_or_default())
    }

    pub async fn set_my_commands(&self, commands: Vec<(String, String)>) -> Result<Value, String> {
        let list: Vec<Value> = commands
            .into_iter()
            .map(|(cmd, desc)| json!({"command": cmd, "description": desc}))
            .collect();
        let mut params = Map::new();
        params.insert("commands".into(), Value::Array(list));
        self.call("setMyCommands", Value::Object(params)).await
    }

    async fn call(&self, method: &str, params: Value) -> Result<Value, String> {
        let url = format!("{}{}", self.base, method);
        let resp = self
            .http
            .post(&url)
            .json(&params)
            .send()
            .await
            .map_err(|e| format!("TelegramError: {}", redact(&e.to_string(), &self.token)))?;
        let status = resp.status();
        let body = resp
            .text()
            .await
            .map_err(|e| format!("TelegramError: {}", redact(&e.to_string(), &self.token)))?;
        self.parse_body(status.as_u16(), &body)
    }

    fn parse_body(&self, status: u16, body: &str) -> Result<Value, String> {
        let parsed: Value = match serde_json::from_str(body) {
            Ok(v) => v,
            Err(_) => {
                return Err(format!(
                    "TelegramError: http {status} body={}",
                    redact(body, &self.token)
                ));
            }
        };
        let ok = parsed.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
        if !ok {
            let desc = parsed
                .get("description")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let code = parsed
                .get("error_code")
                .and_then(|v| v.as_i64())
                .unwrap_or(status as i64);
            return Err(format!(
                "TelegramError: {}",
                redact(&format!("{code}: {desc}"), &self.token)
            ));
        }
        Ok(parsed.get("result").cloned().unwrap_or(Value::Null))
    }
}

fn clip_text(text: &str) -> String {
    if text.chars().count() <= MAX_TEXT_LEN {
        return text.to_string();
    }
    let mut out: String = text.chars().take(MAX_TEXT_LEN - 1).collect();
    out.push('…');
    out
}

fn redact(message: &str, token: &str) -> String {
    if token.is_empty() {
        return message.to_string();
    }
    message.replace(token, "<redacted>")
}
