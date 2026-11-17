use serde_json::Value;
use std::time::Duration;

pub const DERIBIT_BASE: &str = "https://www.deribit.com/api/v2";

pub struct DeribitClient {
    http: reqwest::Client,
    base: String,
}

impl DeribitClient {
    pub fn new() -> Self {
        let http = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(15))
            .build()
            .unwrap_or_else(|_| reqwest::Client::new());
        DeribitClient {
            http,
            base: DERIBIT_BASE.to_string(),
        }
    }

    pub fn with_base(base: &str) -> Self {
        let mut c = Self::new();
        c.base = base.to_string();
        c
    }

    async fn get_json(&self, url: &str) -> Result<Value, String> {
        let resp = self.http.get(url).send().await.map_err(|e| e.to_string())?;
        let status = resp.status();
        if !status.is_success() {
            return Err(format!("Deribit HTTP {}", status.as_u16()));
        }
        let v: Value = resp.json().await.map_err(|e| e.to_string())?;
        Ok(v)
    }

    /// GET public/get_book_summary_by_currency -> result array.
    pub async fn btc_option_summary(&self) -> Result<Value, String> {
        let url = format!(
            "{}/public/get_book_summary_by_currency?currency=BTC&kind=option",
            self.base
        );
        let v = self.get_json(&url).await?;
        match v.get("result") {
            Some(r) => Ok(r.clone()),
            None => Err("Deribit: missing result".to_string()),
        }
    }

    /// GET public/get_index_price -> index_price.
    pub async fn btc_index_price(&self) -> Result<f64, String> {
        let url = format!("{}/public/get_index_price?index_name=btc_usd", self.base);
        let v = self.get_json(&url).await?;
        let result = v
            .get("result")
            .ok_or_else(|| "Deribit: missing result".to_string())?;
        result
            .get("index_price")
            .and_then(|x| x.as_f64())
            .ok_or_else(|| "Deribit: missing index_price".to_string())
    }

    /// DVOL now and 24h change.
    pub async fn btc_dvol(&self, now_ms: i64) -> Result<(f64, f64), String> {
        let start = now_ms - 26 * 3600 * 1000;
        let url = format!(
            "{}/public/get_volatility_index_data?currency=BTC&start_timestamp={}&end_timestamp={}&resolution=3600",
            self.base, start, now_ms
        );
        let v = self.get_json(&url).await?;
        let result = v
            .get("result")
            .ok_or_else(|| "Deribit: missing result".to_string())?;
        let data = result
            .get("data")
            .and_then(|d| d.as_array())
            .ok_or_else(|| "Deribit: missing data".to_string())?;
        if data.is_empty() {
            return Err("Deribit: empty data".to_string());
        }

        let mut best_idx = 0usize;
        let target = now_ms - 24 * 3600 * 1000;
        let mut best_diff = i64::MAX;
        for (i, c) in data.iter().enumerate() {
            if let Some(ts) = c.get(0).and_then(|x| x.as_i64()) {
                let diff = (ts - target).abs();
                if diff < best_diff {
                    best_diff = diff;
                    best_idx = i;
                }
            }
        }

        let close_of = |c: &Value| -> Option<f64> {
            c.as_array().and_then(|a| a.get(4)).and_then(|x| x.as_f64())
        };

        let last = data
            .last()
            .and_then(close_of)
            .ok_or_else(|| "Deribit: missing close".to_string())?;
        let past = data
            .get(best_idx)
            .and_then(close_of)
            .ok_or_else(|| "Deribit: missing close".to_string())?;
        Ok((last, last - past))
    }
}

impl Default for DeribitClient {
    fn default() -> Self {
        Self::new()
    }
}

pub async fn btc_options_report(
    client: &DeribitClient,
    now_ms: i64,
    max_expiries: usize,
) -> Result<Value, String> {
    let summary = client.btc_option_summary().await?;
    let spot = client.btc_index_price().await?;
    let dvol = match client.btc_dvol(now_ms).await {
        Ok((now, chg)) => Some((now, chg)),
        Err(_) => None,
    };

    let rows = crate::core::options_micro::rows_from_book_summary(&summary);
    let mut v = crate::core::options_micro::report(&rows, spot, now_ms, max_expiries);

    if let Some(obj) = v.as_object_mut() {
        match dvol {
            Some((now, chg)) => {
                obj.insert("dvol".to_string(), serde_json::json!(now));
                obj.insert("dvol_change_24h".to_string(), serde_json::json!(chg));
            }
            None => {
                obj.insert("dvol".to_string(), Value::Null);
                obj.insert("dvol_change_24h".to_string(), Value::Null);
            }
        }
    }

    Ok(v)
}
