use std::sync::Mutex;

use chrono::{DateTime, Timelike, Utc};

pub trait Clock: Send + Sync {
    fn now_utc(&self) -> DateTime<Utc>;
    fn now_epoch_secs(&self) -> f64;
}

pub struct SystemClock;

impl Clock for SystemClock {
    fn now_utc(&self) -> DateTime<Utc> {
        Utc::now()
    }

    fn now_epoch_secs(&self) -> f64 {
        Utc::now().timestamp_micros() as f64 / 1_000_000.0
    }
}

pub struct FixedClock {
    inner: Mutex<(DateTime<Utc>, f64)>,
}

impl FixedClock {
    pub fn new(dt: DateTime<Utc>, epoch: f64) -> Self {
        Self {
            inner: Mutex::new((dt, epoch)),
        }
    }

    pub fn set(&self, dt: DateTime<Utc>, epoch: f64) {
        let mut guard = self.inner.lock().unwrap();
        *guard = (dt, epoch);
    }
}

impl Clock for FixedClock {
    fn now_utc(&self) -> DateTime<Utc> {
        self.inner.lock().unwrap().0
    }

    fn now_epoch_secs(&self) -> f64 {
        self.inner.lock().unwrap().1
    }
}

/// Format like Python `datetime.isoformat()` (microseconds only when non-zero).
pub fn py_isoformat(dt: &DateTime<Utc>) -> String {
    let micros = dt.nanosecond() / 1000;
    let base = dt.format("%Y-%m-%dT%H:%M:%S");
    if micros != 0 {
        format!("{}.{:06}+00:00", base, micros)
    } else {
        format!("{}+00:00", base)
    }
}
